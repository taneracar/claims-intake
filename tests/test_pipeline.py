"""The whole pipeline over the synthetic data set, without the orchestrator."""

from __future__ import annotations

import json
import logging
import sqlite3

from fhir.resources.R4B.claim import Claim as FhirClaim

from claims_intake.cli import main as cli
from claims_intake.sources.legacy import format_record

from .conftest import phi_values, run_pipeline

EXPECTED = {
    "L-1001": "accepted",
    "L-1002": "accepted",
    "L-1003": "accepted",
    "L-1004": "review",
    "L-1005": "rejected",
    "L-1006": "review",
    "P-2001": "accepted",
    "P-2003": "review",
    "P-2004": "rejected",
    "P-2005": "review",
    "S-3001": "accepted",
    "S-3002": "accepted",
    "S-3003": "review",
    "S-3005": "accepted",
    "S-3006": "accepted",
}


def decisions(out) -> dict[str, str]:
    db = sqlite3.connect(out / "state.db")
    try:
        return dict(db.execute("SELECT claim_id, decision FROM claims").fetchall())
    finally:
        db.close()


def test_every_claim_lands_where_planted(data, tmp_path):
    out = tmp_path / "out"
    report, _ = run_pipeline(data, out)
    assert decisions(out) == EXPECTED
    assert report["claims_by_decision"] == {"accepted": 8, "duplicate": 1, "rejected": 2, "review": 5}
    assert report["batches_held"] == 1
    assert report["scans"] == 6 and report["scan_failures"] == 1
    assert report["issue_codes"] == {
        "dx_format": 1,
        "name_mismatch": 1,
        "not_eligible": 1,
        "npi_invalid": 1,
        "service_in_future": 1,
        "total_mismatch": 1,
        "unclear_field": 1,
    }
    assert {a["rule"] for a in report["alerts"]} == {"batch_held", "review_rate", "scan_failures"}


def test_held_batch_loads_nothing_and_is_recorded(data, tmp_path):
    out = tmp_path / "out"
    run_pipeline(data, out)
    assert not any(cid.startswith("L-11") for cid in decisions(out))
    db = sqlite3.connect(out / "state.db")
    status, detail = db.execute("SELECT status, detail FROM files WHERE name = 'legacy_20261119.txt'").fetchone()
    assert status == "held" and "trailer says 4 claims, file has 3" in detail


def test_fhir_written_for_accepted_claims_only_and_valid(data, tmp_path):
    out = tmp_path / "out"
    run_pipeline(data, out)
    files = sorted((out / "fhir").glob("*.json"))
    accepted = sorted(cid.lower() for cid, d in EXPECTED.items() if d == "accepted")
    assert [f.stem for f in files] == accepted
    for f in files:
        FhirClaim.model_validate(json.loads(f.read_text()))


def test_rerun_is_idempotent(data, tmp_path):
    out = tmp_path / "out"
    run_pipeline(data, out)
    report, _ = run_pipeline(data, out)
    assert report["files_skipped_already_seen"] == 9
    assert report["claims_total"] == 0
    assert decisions(out) == EXPECTED


def test_same_claim_in_a_new_file_is_a_duplicate(data, tmp_path):
    out = tmp_path / "out"
    run_pipeline(data, out)
    portal = (data / "inbox" / "portal_20261120.csv").read_text().splitlines()
    (data / "inbox" / "portal_20261121.csv").write_text("\n".join([portal[0], portal[1]]) + "\n")  # P-2001 resent
    report, _ = run_pipeline(data, out)
    assert report["claims_by_decision"] == {"duplicate": 1}


def test_fixed_batch_can_be_resent(data, tmp_path):
    out = tmp_path / "out"
    run_pipeline(data, out)
    f = data / "inbox" / "legacy_20261119.txt"
    lines = f.read_text().splitlines()
    lines[-1] = format_record("T", {"claim_count": "3", "total": lines[-1][7:20]})  # corrected trailer
    f.write_text("\n".join(lines) + "\n")
    report, _ = run_pipeline(data, out)
    assert report["batches_held"] == 0
    assert report["claims_by_decision"] == {"accepted": 3}


def test_no_patient_data_in_logs_metrics_or_alerts(data, tmp_path, caplog):
    out = tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="claims_intake"):
        run_pipeline(data, out, logger=logging.getLogger("claims_intake.leaktest"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "S-3003 from scan_03.png: review (unclear_field)" in text  # the log is useful...
    text += (out / "alerts.jsonl").read_text() + "".join(p.read_text() for p in (out / "metrics").glob("*.json"))
    leaks = [v for v in phi_values(data) if v.lower() in text.lower()]
    assert leaks == []  # ...and has no PHI


def test_drift_alerts_when_compared_with_a_baseline(data, tmp_path):
    base_out = tmp_path / "base"
    baseline, _ = run_pipeline(data, base_out)
    shifted = dict(baseline, charge_buckets={">$2500": 50}, review_rate=0.0, issue_codes={})
    report, _ = run_pipeline(data, tmp_path / "out", baseline=shifted)
    rules = {a["rule"] for a in report["alerts"]}
    assert {"charge_drift", "review_rate_drift"} <= rules
    unchanged, _ = run_pipeline(data, tmp_path / "out2", baseline=baseline)
    assert not {"charge_drift", "review_rate_drift", "new_issue_code"} & {a["rule"] for a in unchanged["alerts"]}


def test_review_queue_approve_with_correction_and_reject(data, tmp_path, capsys, monkeypatch):
    out = tmp_path / "out"
    run_pipeline(data, out)
    monkeypatch.chdir(data.parent)  # the CLI reads data/eligibility.csv
    assert cli(["review", "--out", str(out), "list"]) == 0
    listing = capsys.readouterr().out
    assert listing.count("\n") == 5 and "M10000000" not in listing and "M******" in listing
    ids = {line.split()[1]: int(line.split()[0][1:]) for line in listing.splitlines()}

    # The scan with an unreadable birth date: the reviewer reads the paper form and fills it in.
    dob = next(r for r in (data / "eligibility.csv").read_text().splitlines() if r.startswith("M10001644")).split(",")[
        2
    ]
    rc = cli(
        [
            "review",
            "--out",
            str(out),
            "--today",
            "2026-11-21",
            "approve",
            str(ids["S-3003"]),
            "--reviewer",
            "rn01",
            "--set",
            f"member_dob={dob}",
        ]
    )
    assert rc == 0 and (out / "fhir" / "s-3003.json").exists()

    # The invalid NPI cannot be approved as is.
    assert (
        cli(["review", "--out", str(out), "--today", "2026-11-21", "approve", str(ids["L-1006"]), "--reviewer", "rn01"])
        == 1
    )
    assert "npi_invalid" in capsys.readouterr().err
    assert (
        cli(
            [
                "review",
                "--out",
                str(out),
                "reject",
                str(ids["L-1006"]),
                "--reviewer",
                "rn01",
                "--note",
                "asked provider to resubmit",
            ]
        )
        == 0
    )
    d = decisions(out)
    assert d["S-3003"] == "accepted" and d["L-1006"] == "rejected"
