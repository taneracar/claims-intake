"""Run metrics, cost, drift against a baseline run, and alerts.

Nothing in here carries patient data: counts, rates, token totals, code names and money
buckets only. Alert payloads still go through the PHI scrubber before they are written or sent.
"""

from __future__ import annotations

import json
import math
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .phi import Scrubber

# Upper bounds (cents) of the claim-charge buckets used for drift.
CHARGE_BUCKETS = [5_000, 15_000, 30_000, 60_000, 120_000, 250_000]


def bucket(cents: int) -> str:
    for upper in CHARGE_BUCKETS:
        if cents <= upper:
            return f"<=${upper // 100}"
    return f">${CHARGE_BUCKETS[-1] // 100}"


@dataclass
class RunMetrics:
    run_id: str
    extractor: str
    files_by_source: Counter = field(default_factory=Counter)
    files_skipped_already_seen: int = 0
    batches_held: int = 0
    claims_by_decision: Counter = field(default_factory=Counter)
    claims_by_source_decision: Counter = field(default_factory=Counter)
    issue_codes: Counter = field(default_factory=Counter)
    parse_errors: int = 0
    scans: int = 0
    scan_failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_micro_usd: int = 0
    scan_latency_ms: list[int] = field(default_factory=list)
    charge_buckets: Counter = field(default_factory=Counter)

    @property
    def claims_total(self) -> int:
        return sum(v for k, v in self.claims_by_decision.items() if k != "duplicate")

    @property
    def review_rate(self) -> float:
        return self.claims_by_decision["review"] / self.claims_total if self.claims_total else 0.0

    @property
    def scan_failure_rate(self) -> float:
        return self.scan_failures / self.scans if self.scans else 0.0

    @property
    def cost_per_scan_micro_usd(self) -> int:
        return self.cost_micro_usd // self.scans if self.scans else 0

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["claims_by_source_decision"] = {
            f"{s}/{dec}": n for (s, dec), n in sorted(self.claims_by_source_decision.items())
        }
        for k, v in list(d.items()):
            if isinstance(v, Counter):
                d[k] = dict(sorted(v.items()))
        d["claims_total"] = self.claims_total
        d["review_rate"] = round(self.review_rate, 4)
        d["scan_failure_rate"] = round(self.scan_failure_rate, 4)
        d["cost_usd"] = round(self.cost_micro_usd / 1_000_000, 6)
        d["cost_per_scan_usd"] = round(self.cost_per_scan_micro_usd / 1_000_000, 6)
        lat = sorted(self.scan_latency_ms)
        d["scan_latency_p50_ms"] = lat[len(lat) // 2] if lat else None
        d["scan_latency_p95_ms"] = lat[min(len(lat) - 1, math.ceil(len(lat) * 0.95) - 1)] if lat else None
        del d["scan_latency_ms"]
        return d


def psi(expected: dict[str, int], actual: dict[str, int], floor: float = 0.005) -> float:
    """Population stability index between two bucketed distributions. >0.2 is usually read as a real shift."""
    keys = set(expected) | set(actual)
    e_total = sum(expected.values()) or 1
    a_total = sum(actual.values()) or 1
    total = 0.0
    for k in keys:
        e = max(expected.get(k, 0) / e_total, floor)
        a = max(actual.get(k, 0) / a_total, floor)
        total += (a - e) * math.log(a / e)
    return total


@dataclass
class Thresholds:
    max_review_rate: float = 0.30
    max_scan_failure_rate: float = 0.10
    max_cost_per_scan_usd: float = 0.05
    max_charge_psi: float = 0.20
    max_review_rate_increase: float = 0.15  # absolute, vs baseline
    min_claims_for_drift: int = 10


@dataclass
class Alert:
    severity: str  # critical | warning | info
    rule: str
    message: str
    value: float | int | None = None
    threshold: float | int | None = None


def evaluate(m: RunMetrics, t: Thresholds, baseline: dict | None) -> list[Alert]:
    alerts: list[Alert] = []
    if m.batches_held:
        alerts.append(
            Alert(
                "critical",
                "batch_held",
                f"{m.batches_held} batch file(s) held: control totals or structure failed",
                m.batches_held,
                0,
            )
        )
    if m.claims_total and m.review_rate > t.max_review_rate:
        alerts.append(
            Alert(
                "warning",
                "review_rate",
                f"review rate {m.review_rate:.0%} is above {t.max_review_rate:.0%}",
                round(m.review_rate, 4),
                t.max_review_rate,
            )
        )
    if m.scans and m.scan_failure_rate > t.max_scan_failure_rate:
        alerts.append(
            Alert(
                "warning",
                "scan_failures",
                f"{m.scan_failures} of {m.scans} scans could not be extracted",
                round(m.scan_failure_rate, 4),
                t.max_scan_failure_rate,
            )
        )
    per_scan = m.cost_per_scan_micro_usd / 1_000_000
    if m.scans and per_scan > t.max_cost_per_scan_usd:
        alerts.append(
            Alert(
                "warning",
                "cost_per_scan",
                f"extraction cost ${per_scan:.4f}/scan is above ${t.max_cost_per_scan_usd:.4f}",
                round(per_scan, 6),
                t.max_cost_per_scan_usd,
            )
        )

    if baseline and m.claims_total >= t.min_claims_for_drift:
        score = psi(baseline.get("charge_buckets", {}), dict(m.charge_buckets))
        if score > t.max_charge_psi:
            alerts.append(
                Alert(
                    "warning",
                    "charge_drift",
                    f"claim charge distribution shifted (PSI {score:.2f})",
                    round(score, 4),
                    t.max_charge_psi,
                )
            )
        base_rr = baseline.get("review_rate", 0.0)
        if m.review_rate - base_rr > t.max_review_rate_increase:
            alerts.append(
                Alert(
                    "warning",
                    "review_rate_drift",
                    f"review rate {m.review_rate:.0%} vs {base_rr:.0%} in the baseline run",
                    round(m.review_rate, 4),
                    round(base_rr + t.max_review_rate_increase, 4),
                )
            )
        base_codes = set(baseline.get("issue_codes", {}))
        for code, n in sorted(m.issue_codes.items()):
            if code not in base_codes and n >= 3:
                alerts.append(
                    Alert("info", "new_issue_code", f"issue '{code}' seen {n} times, never in the baseline run", n, 0)
                )
    return alerts


class AlertSink:
    def __init__(self, path: Path, scrubber: Scrubber, webhook_url: str | None = None) -> None:
        self.path = path
        self.scrubber = scrubber
        self.webhook_url = webhook_url
        path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, run_id: str, alerts: list[Alert]) -> None:
        for a in alerts:
            payload = self.scrubber.scrub(json.dumps({"run_id": run_id, **asdict(a)}))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(payload + "\n")
            if self.webhook_url:
                req = urllib.request.Request(
                    self.webhook_url, data=payload.encode(), headers={"content-type": "application/json"}, method="POST"
                )
                try:
                    urllib.request.urlopen(req, timeout=5).close()  # noqa: S310 - URL comes from the operator's config
                except OSError:
                    pass  # the alert is already on disk; a webhook outage must not fail the run
