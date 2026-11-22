"""Prefect flow around the pipeline.

Tasks that touch claim contents set persist_result=False and NO_CACHE: Prefect must not write
patient data into its result storage or use it in cache keys. Only file paths, counts and the
PHI-free metrics report cross into Prefect's own state.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE

from .extract import ClaudeExtractor, Extractor, FakeExtractor
from .monitor import Thresholds
from .pipeline import Config, FileResult, Pipeline, discover, read_file
from .validate import load_roster


def make_extractor(name: str, truth_dir: Path | None, model: str) -> Extractor:
    if name == "claude":
        return ClaudeExtractor(model=model)
    if name == "fake":
        if truth_dir is None:
            raise ValueError("the fake extractor needs --truth-dir")
        return FakeExtractor(truth_dir, model=model)
    raise ValueError(f"unknown extractor {name}")


@task(name="discover-files", cache_policy=NO_CACHE)
def discover_task(inbox: str) -> list[str]:
    return [str(p) for p in discover(Path(inbox))]


@task(
    name="read-file",
    # Retries cover transient extraction failures (rate limits, 5xx, network). Parsing errors are
    # data problems and come back inside FileResult instead of raising, so they are not retried.
    retries=3,
    retry_delay_seconds=[2, 8, 30],
    retry_jitter_factor=0.5,
    cache_policy=NO_CACHE,
    persist_result=False,
)
def read_task(path: str, extractor: Extractor) -> FileResult:
    return read_file(Path(path), extractor)


@flow(name="claims-intake", log_prints=False)
def intake_flow(
    inbox: str,
    out_dir: str,
    roster_path: str,
    extractor: str = "fake",
    truth_dir: str | None = None,
    model: str = "claude-opus-5-5",
    today: str | None = None,
    baseline_path: str | None = None,
    webhook_url: str | None = None,
    retry_delays: list[float] | None = None,
) -> dict:
    logger = get_run_logger()
    run_id = f"run-{date.today():%Y%m%d}-{uuid.uuid4().hex[:8]}"
    cfg = Config(
        out_dir=Path(out_dir),
        roster=load_roster(Path(roster_path).read_text()),
        today=date.fromisoformat(today) if today else date.today(),
        thresholds=Thresholds(),
        baseline=json.loads(Path(baseline_path).read_text()) if baseline_path else None,
        webhook_url=webhook_url,
    )
    ex = make_extractor(extractor, Path(truth_dir) if truth_dir else None, model)
    pipeline = Pipeline(cfg, run_id, ex.name, logger)

    paths = discover_task(inbox)
    reader = read_task.with_options(retry_delay_seconds=retry_delays) if retry_delays is not None else read_task
    futures = [reader.submit(p, ex) for p in paths]  # scans are extracted concurrently
    for fut in futures:
        pipeline.apply(fut.result())  # state changes happen one file at a time, in a stable order
    report = pipeline.finish()
    logger.info(
        "run %s: %s claims, %s accepted, %s review, %s rejected, %s duplicate, %s held batches, $%.4f extraction",
        run_id,
        report["claims_total"],
        report["claims_by_decision"].get("accepted", 0),
        report["claims_by_decision"].get("review", 0),
        report["claims_by_decision"].get("rejected", 0),
        report["claims_by_decision"].get("duplicate", 0),
        report["batches_held"],
        report["cost_usd"],
    )
    return report
