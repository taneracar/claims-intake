from __future__ import annotations

from datetime import date

import pytest

from claims_intake.models import Claim, ClaimLine
from claims_intake.synth import TODAY
from claims_intake.validate import dedupe_key, route, validate


@pytest.fixture
def member(roster):
    return roster["M10000000"]


def claim(member, **over) -> Claim:
    base = dict(
        claim_id="X-1",
        member_id=member.member_id,
        member_name=member.name,
        member_dob=member.dob,
        provider_npi="1234567893",
        service_date=date(2026, 11, 3),
        place_of_service="11",
        diagnosis_codes=["E11.9"],
        lines=[ClaimLine(code="99213", charge_cents=12_900)],
        total_charge_cents=12_900,
        source="portal",
        source_ref="t.csv#row2",
    )
    base.update(over)
    return Claim(**base)


def codes(c, roster):
    return sorted(i.code for i in validate(c, roster, TODAY))


def test_clean_claim_has_no_issues_and_is_accepted(member, roster):
    c = claim(member)
    assert validate(c, roster, TODAY) == []
    assert route([], is_duplicate=False) == "accepted"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"member_id": "M99999999"}, "member_unknown"),
        ({"member_id": "X123"}, "member_id_format"),
        ({"provider_npi": "1234567890"}, "npi_invalid"),
        ({"service_date": date(2026, 12, 1)}, "service_in_future"),
        ({"service_date": date(2025, 1, 2)}, "not_eligible"),  # coverage starts 2026-01-01
        ({"place_of_service": "1"}, "pos_format"),
        ({"diagnosis_codes": []}, "dx_missing"),
        ({"diagnosis_codes": ["11.9E"]}, "dx_format"),
        ({"diagnosis_codes": ["E11.9", "E11.9"]}, "dx_repeated"),
        ({"lines": [ClaimLine(code="9921", charge_cents=12_900)]}, "procedure_format"),
        ({"lines": [ClaimLine(code="99213", modifier="2", charge_cents=12_900)]}, "modifier_format"),
        ({"lines": [ClaimLine(code="99213", units=0, charge_cents=12_900)]}, "units"),
        ({"lines": [ClaimLine(code="99213", charge_cents=0)], "total_charge_cents": 0}, "charge"),
        ({"total_charge_cents": 13_900}, "total_mismatch"),
        ({"member_dob": date(1970, 1, 1)}, "dob_mismatch"),
        ({"member_name": "Anna Lima"}, "name_mismatch"),
        ({"unclear_fields": ["member_dob"]}, "unclear_field"),
    ],
)
def test_each_rule(member, roster, over, code):
    assert code in codes(claim(member, **over), roster)


def test_name_match_ignores_case_commas_and_spacing(member, roster):
    for written in ["  ana   LIMA ", "LIMA, ANA", "Lima,Ana"]:
        assert codes(claim(member, member_name=written), roster) == [], written
    assert "name_mismatch" in codes(claim(member, member_name="ANA, LIMA"), roster)  # first/last swapped


def test_eligibility_end_date(roster):
    lapsed = next(m for m in roster.values() if m.coverage_end)
    c = claim(lapsed, service_date=date(2026, 10, 1))
    assert "not_eligible" in codes(c, roster)
    assert "not_eligible" not in codes(claim(lapsed, service_date=date(2026, 9, 30)), roster)


def test_messages_carry_no_patient_data(member, roster):
    c = claim(member, member_name="Anna Lima", member_dob=date(1970, 1, 1), provider_npi="1", total_charge_cents=1)
    text = " ".join(i.message for i in validate(c, roster, TODAY))
    for v in [member.member_id, member.name, "Anna", "Lima", "1970", member.dob.isoformat()]:
        assert v not in text


class TestRouting:
    def test_hard_rejects(self):
        from claims_intake.models import Issue

        for code in ["member_unknown", "not_eligible", "service_in_future", "service_before_birth"]:
            assert route([Issue(code=code, severity="error", message="")], is_duplicate=False) == "rejected"

    def test_fixable_errors_and_warnings_go_to_review(self):
        from claims_intake.models import Issue

        assert route([Issue(code="npi_invalid", severity="error", message="")], is_duplicate=False) == "review"
        assert route([Issue(code="unclear_field", severity="warning", message="")], is_duplicate=False) == "review"

    def test_duplicate_wins(self):
        assert route([], is_duplicate=True) == "duplicate"


class TestDedupe:
    def test_same_visit_from_another_channel_is_a_duplicate(self, member):
        a = claim(member, claim_id="L-1", source="legacy", member_name=None, member_dob=None)
        b = claim(member, claim_id="P-9", source="portal")
        assert dedupe_key(a) == dedupe_key(b)

    def test_line_and_diagnosis_order_do_not_matter(self, member):
        l1, l2 = ClaimLine(code="99213", charge_cents=100), ClaimLine(code="83036", charge_cents=200)
        a = claim(member, lines=[l1, l2], diagnosis_codes=["E11.9", "I10"], total_charge_cents=300)
        b = claim(member, lines=[l2, l1], diagnosis_codes=["I10", "E11.9"], total_charge_cents=300)
        assert dedupe_key(a) == dedupe_key(b)

    def test_any_billed_difference_is_a_different_claim(self, member):
        a = claim(member)
        assert dedupe_key(a) != dedupe_key(
            claim(member, lines=[ClaimLine(code="99213", charge_cents=12_901)], total_charge_cents=12_901)
        )
        assert dedupe_key(a) != dedupe_key(claim(member, service_date=date(2026, 11, 4)))
