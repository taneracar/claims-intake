"""The intake pipeline, independent of the orchestrator.

  read_file(path)   pure: parse a legacy batch or portal CSV, or extract a scan. Safe to run in parallel.
  Pipeline.apply()  sequential: idempotency, validation, routing, review queue, FHIR output, metrics.
  Pipeline.finish() metrics file, drift check, alerts.

flow.py wraps these in Prefect tasks; tests call them directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .extract import Extractor, Usage, file_digest
from .fhir import to_fhir
from .models import Claim
from .monitor import AlertSink, RunMetrics, Thresholds, bucket, evaluate
from .phi import Scrubber
from .sources.legacy import parse_legacy
from .sources.portal import parse_portal
from .store import Store
from .validate import Member, dedupe_key, route, validate

KINDS = {
    ".txt": "legacy",
    ".dat": "legacy",
    ".csv": "portal",
    ".png": "scan",
    ".jpg": "scan",
    ".jpeg": "scan",
    ".pdf": "scan",
}


def classify(path: Path) -> str | None:
    return KINDS.get(path.suffix.lower())


def discover(inbox: Path) -> list[Path]:
    return sorted(p for p in inbox.iterdir() if p.is_file() and classify(p) and not p.name.startswith("."))


@dataclass
class FileResult:
    name: str
    digest: str
    kind: str
    claims: list[Claim] = field(default_factory=list)
    batch_errors: list[str] = field(default_factory=list)
    record_errors: list[str] = field(default_factory=list)
    usage: Usage | None = None
    extraction_error: str | None = None


def read_file(path: Path, extractor: Extractor) -> FileResult:
    kind = classify(path)
    if kind is None:
        raise ValueError(f"not an intake file: {path.name}")
    res = FileResult(path.name, file_digest(path), kind)
    if kind == "legacy":
        batch = parse_legacy(path.read_text(encoding="ascii", errors="replace"), path.name)
        res.claims, res.batch_errors, res.record_errors = batch.claims, batch.batch_errors, batch.record_errors
    elif kind == "portal":
        res.claims, res.record_errors = parse_portal(path.read_text(encoding="utf-8"), path.name)
    else:
        ex = extractor.extract(path)
        res.usage = ex.usage
        res.extraction_error = ex.error
        if ex.claim is not None:
            res.claims = [ex.claim]
    return res


@dataclass
class Config:
    out_dir: Path
    roster: dict[str, Member]
    today: date
    thresholds: Thresholds = field(default_factory=Thresholds)
    baseline: dict | None = None
    webhook_url: str | None = None


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        run_id: str,
        extractor_name: str,
        logger: logging.Logger | logging.LoggerAdapter | None = None,
    ):
        self.cfg = cfg
        self.run_id = run_id
        self.store = Store(cfg.out_dir / "state.db")
        self.scrubber = Scrubber()
        self.metrics = RunMetrics(run_id=run_id, extractor=extractor_name)
        self.log = logger or logging.getLogger("claims_intake")
        self.alerts = AlertSink(cfg.out_dir / "alerts.jsonl", self.scrubber, cfg.webhook_url)
        self.fhir_dir = cfg.out_dir / "fhir"
        self.fhir_dir.mkdir(parents=True, exist_ok=True)

    def _info(self, msg: str) -> None:
        self.log.info(self.scrubber.scrub(msg))

    def _warn(self, msg: str) -> None:
        self.log.warning(self.scrubber.scrub(msg))

    def apply(self, r: FileResult) -> None:
        for c in r.claims:
            self.scrubber.add(c.phi_values())
        if self.store.file_seen(r.digest):
            self.metrics.files_skipped_already_seen += 1
            self._info(f"{r.name}: already processed, skipped")
            return
        self.metrics.files_by_source[r.kind] += 1
        if r.usage is not None:
            self.metrics.scans += 1
            self.metrics.tokens_in += r.usage.input_tokens
            self.metrics.tokens_out += r.usage.output_tokens
            self.metrics.cost_micro_usd += r.usage.cost_micro_usd
            self.metrics.scan_latency_ms.append(r.usage.latency_ms)
        if r.batch_errors:
            # The whole file is held: none of its claims are loaded, and it can be resent after a fix.
            self.metrics.batches_held += 1
            self.store.record_file(r.digest, r.name, r.kind, self.run_id, "held", "; ".join(r.batch_errors))
            self._warn(f"{r.name}: batch held: {'; '.join(r.batch_errors)}")
            return
        self.metrics.parse_errors += len(r.record_errors)
        for e in r.record_errors:
            self._warn(f"{r.name}: {e}")
        if r.kind == "scan" and not r.claims:
            self.metrics.scan_failures += 1
            self._warn(f"{r.name}: extraction failed: {r.extraction_error}")
        self.store.record_file(r.digest, r.name, r.kind, self.run_id, "loaded")
        for c in r.claims:
            self._handle_claim(c)

    def _handle_claim(self, c: Claim) -> None:
        key = dedupe_key(c)
        duplicate = self.store.claim_exists(key)
        issues = [] if duplicate else validate(c, self.cfg.roster, self.cfg.today)
        decision = route(issues, is_duplicate=duplicate)
        self.metrics.claims_by_decision[decision] += 1
        self.metrics.claims_by_source_decision[(c.source, decision)] += 1
        for i in issues:
            self.metrics.issue_codes[i.code] += 1
        if duplicate:
            self._info(f"{c.claim_id} from {c.source_ref}: duplicate of a claim already received, not loaded again")
            return
        self.metrics.charge_buckets[bucket(c.total_charge_cents)] += 1
        self.store.record_claim(key, c, self.run_id, decision, issues)
        if decision == "accepted":
            self.write_fhir(c)
        self._info(
            f"{c.claim_id} from {c.source_ref}: {decision}"
            + (f" ({', '.join(sorted({i.code for i in issues}))})" if issues else "")
        )

    def write_fhir(self, c: Claim) -> Path:
        resource = to_fhir(c, datetime.combine(self.cfg.today, datetime.min.time()).astimezone())
        path = self.fhir_dir / f"{resource['id']}.json"
        path.write_text(json.dumps(resource, indent=2))
        return path

    def finish(self) -> dict:
        alerts = evaluate(self.metrics, self.cfg.thresholds, self.cfg.baseline)
        self.alerts.send(self.run_id, alerts)
        report = self.metrics.to_dict() | {"alerts": [a.__dict__ for a in alerts]}
        mdir = self.cfg.out_dir / "metrics"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / f"{self.run_id}.json").write_text(self.scrubber.scrub(json.dumps(report, indent=2)))
        for a in alerts:
            self._warn(f"ALERT [{a.severity}] {a.rule}: {a.message}")
        self.store.close()
        return report
