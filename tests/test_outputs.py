"""FHIR mapping, PHI scrubbing, metrics, drift and alerts."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime

import pytest
from fhir.resources.R4B.claim import Claim as FhirClaim
from pydantic import ValidationError

from claims_intake.fhir import CPT_SYSTEM, HCPCS_SYSTEM, NPI_SYSTEM, to_fhir
from claims_intake.models import Claim, ClaimLine
from claims_intake.monitor import AlertSink, RunMetrics, Thresholds, bucket, evaluate, psi
from claims_intake.phi import Scrubber, ScrubFilter, mask_member_id


def sample_claim(**over) -> Claim:
    base = dict(
        claim_id="S-3001",
        member_id="M10000000",
        member_name="Ana Lima",
        member_dob=date(1969, 1, 19),
        provider_npi="1234567893",
        service_date=date(2026, 11, 13),
        place_of_service="11",
        diagnosis_codes=["E11.65", "I10"],
        lines=[
            ClaimLine(code="99214", modifier="25", charge_cents=18_500),
            ClaimLine(code="G0447", charge_cents=2_600),
        ],
        total_charge_cents=21_100,
        source="scan",
        source_ref="scan_01.png",
    )
    base.update(over)
    return Claim(**base)


class TestFhir:
    def test_valid_r4b_claim_with_the_right_code_systems(self):
        r = to_fhir(sample_claim(), datetime(2026, 11, 21, 9, 0).astimezone())
        FhirClaim.model_validate(r)
        assert r["provider"]["identifier"] == {"system": NPI_SYSTEM, "value": "1234567893"}
        assert [i["productOrService"]["coding"][0]["system"] for i in r["item"]] == [CPT_SYSTEM, HCPCS_SYSTEM]
        assert r["item"][0]["modifier"][0]["coding"][0]["code"] == "25"
        assert "modifier" not in r["item"][1]
        assert r["total"] == {"value": 211.0, "currency": "USD"}
        assert sum(i["net"]["value"] for i in r["item"]) == r["total"]["value"]
        assert [d["diagnosisCodeableConcept"]["coding"][0]["code"] for d in r["diagnosis"]] == ["E11.65", "I10"]

    def test_only_a_reference_to_the_patient_no_name_or_birth_date(self):
        text = json.dumps(to_fhir(sample_claim(), datetime(2026, 11, 21).astimezone()))
        assert "Ana" not in text and "Lima" not in text and "1969" not in text
        assert '"Patient/M10000000"' in text

    def test_invalid_resource_raises(self):
        with pytest.raises(ValidationError):
            to_fhir(sample_claim(claim_id="has spaces and !!"), datetime(2026, 11, 21).astimezone())


class TestScrubber:
    def test_replaces_known_values_case_insensitively_and_member_id_shapes(self):
        s = Scrubber()
        s.add(sample_claim().phi_values())
        out = s.scrub("ANA LIMA (m10000000, born 01/19/1969 / 1969-01-19); other id M12345678; lima")
        assert out == "[PHI] ([PHI], born [PHI] / [PHI]); other id [PHI]; [PHI]"

    def test_short_values_are_not_used_as_patterns(self):
        s = Scrubber()
        s.add(["Al", "M10000000"])
        assert s.scrub("Albany total") == "Albany total"

    def test_log_filter(self, caplog):
        s = Scrubber()
        s.add(["Ana Lima"])
        logger = logging.getLogger("claims_intake.test.filter")
        logger.addFilter(ScrubFilter(s))
        with caplog.at_level(logging.INFO, logger=logger.name):
            logger.info("claim for %s", "Ana Lima")
        assert caplog.records[-1].getMessage() == "claim for [PHI]"

    def test_mask(self):
        assert mask_member_id("M10000000") == "M******00"


class TestMetricsAndAlerts:
    def metrics(self, **over) -> RunMetrics:
        m = RunMetrics(run_id="r", extractor="fake")
        m.claims_by_decision = Counter({"accepted": 8, "review": 2})
        for k, v in over.items():
            setattr(m, k, v)
        return m

    def test_psi(self):
        a = {"<=$50": 10, "<=$150": 30, "<=$300": 10}
        assert psi(a, a) == pytest.approx(0.0)
        assert psi(a, {"<=$50": 40, "<=$150": 5, "<=$300": 5}) > 0.2
        assert psi(a, {"<=$50": 11, "<=$150": 29, "<=$300": 10}) < 0.02

    def test_buckets(self):
        assert bucket(5_000) == "<=$50" and bucket(5_001) == "<=$150" and bucket(10**7) == ">$2500"

    def test_quiet_run_has_no_alerts(self):
        assert evaluate(self.metrics(), Thresholds(), None) == []

    def test_operational_rules(self):
        m = self.metrics(batches_held=1, scans=10, scan_failures=2, cost_micro_usd=10 * 60_000)
        m.claims_by_decision = Counter({"accepted": 5, "review": 5})
        rules = {a.rule: a for a in evaluate(m, Thresholds(), None)}
        assert set(rules) == {"batch_held", "review_rate", "scan_failures", "cost_per_scan"}
        assert rules["batch_held"].severity == "critical"
        assert rules["cost_per_scan"].value == pytest.approx(0.06)

    def test_duplicates_do_not_count_toward_review_rate(self):
        m = self.metrics()
        m.claims_by_decision["duplicate"] = 50
        assert m.review_rate == pytest.approx(0.2)

    def test_drift_against_baseline(self):
        base = {"charge_buckets": {"<=$150": 9, "<=$300": 1}, "review_rate": 0.1, "issue_codes": {"npi_invalid": 1}}
        m = self.metrics(
            charge_buckets=Counter({"<=$50": 1, ">$2500": 9}), issue_codes=Counter({"npi_invalid": 1, "dx_format": 4})
        )
        m.claims_by_decision = Counter({"accepted": 6, "review": 4})
        rules = {a.rule for a in evaluate(m, Thresholds(max_review_rate=0.9), base)}
        assert rules == {"charge_drift", "review_rate_drift", "new_issue_code"}

    def test_no_drift_verdict_on_tiny_runs(self):
        m = self.metrics(charge_buckets=Counter({">$2500": 3}))
        m.claims_by_decision = Counter({"accepted": 3})
        assert evaluate(m, Thresholds(), {"charge_buckets": {"<=$50": 100}}) == []

    def test_alert_sink_scrubs_and_survives_a_dead_webhook(self, tmp_path):
        s = Scrubber()
        s.add(["Ana Lima"])
        from claims_intake.monitor import Alert

        sink = AlertSink(tmp_path / "alerts.jsonl", s, webhook_url="http://127.0.0.1:9/never")
        sink.send("r", [Alert("warning", "x", "problem with Ana Lima's claim M10000000")])
        line = json.loads((tmp_path / "alerts.jsonl").read_text())
        assert line["message"] == "problem with [PHI]'s claim [PHI]"
