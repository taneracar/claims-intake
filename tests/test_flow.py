"""The Prefect flow itself, on a throwaway local Prefect server."""

from __future__ import annotations

import pytest
from prefect.testing.utilities import prefect_test_harness

from claims_intake.flow import intake_flow

from .test_pipeline import EXPECTED, decisions


@pytest.fixture(scope="module", autouse=True)
def prefect_server():
    with prefect_test_harness(server_startup_timeout=120):
        yield


def test_flow_runs_retries_the_flaky_scan_and_matches_the_pipeline(data, tmp_path):
    out = tmp_path / "out"
    report = intake_flow(
        inbox=str(data / "inbox"),
        out_dir=str(out),
        roster_path=str(data / "eligibility.csv"),
        extractor="fake",
        truth_dir=str(data / "truth"),
        today="2026-11-21",
        retry_delays=[0, 0, 0],
    )
    # scan_06 fails on its first attempt; the task retry gets it through.
    assert decisions(out) == EXPECTED
    assert report["claims_by_decision"] == {"accepted": 8, "duplicate": 1, "rejected": 2, "review": 5}
    assert report["cost_usd"] > 0 and report["cost_per_scan_usd"] == pytest.approx(0.0162)


def test_flow_fails_loudly_when_retries_run_out(data, tmp_path):
    import json

    key = data / "truth" / "scan_01.png.json"
    payload = json.loads(key.read_text())
    payload["fail_attempts"] = 10
    key.write_text(json.dumps(payload))
    with pytest.raises(ConnectionError):
        intake_flow(
            inbox=str(data / "inbox"),
            out_dir=str(tmp_path / "out"),
            roster_path=str(data / "eligibility.csv"),
            extractor="fake",
            truth_dir=str(data / "truth"),
            today="2026-11-21",
            retry_delays=[0, 0, 0],
        )
