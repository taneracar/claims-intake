from __future__ import annotations

import pytest

from claims_intake.codes import normalize_icd10, valid_icd10, valid_npi, valid_procedure
from claims_intake.money import fmt, parse_implied_decimal, parse_money
from claims_intake.sources.legacy import RECORD_LEN, format_record, parse_legacy
from claims_intake.sources.portal import parse_portal


class TestCodes:
    def test_npi_check_digit(self):
        assert valid_npi("1234567893")  # the example NPI CMS publishes
        assert not valid_npi("1234567890")
        assert not valid_npi("123456789")
        assert not valid_npi("12345678a3")

    @pytest.mark.parametrize("code", ["E11.9", "I10", "J45.909", "M54.50", "U07.1", "Z00.00", "S72.001A"])
    def test_icd10_valid(self, code):
        assert valid_icd10(code)

    @pytest.mark.parametrize("code", ["11.9E", "E1.9", "E11.", "E11.12345", "e11.9", ""])
    def test_icd10_invalid(self, code):
        assert not valid_icd10(code)

    def test_icd10_normalized_from_undotted(self):
        assert normalize_icd10("e119") == "E11.9"
        assert normalize_icd10("I10") == "I10"
        assert normalize_icd10(" J45.909 ") == "J45.909"

    @pytest.mark.parametrize(
        ("code", "ok"),
        [
            ("99213", True),
            ("0001F", True),
            ("0042T", True),
            ("G0447", True),
            ("9921", False),
            ("W1234", False),
            ("99213A", False),
        ],
    )
    def test_procedure(self, code, ok):
        assert valid_procedure(code) is ok


class TestMoney:
    def test_parse(self):
        assert parse_money("$1,234.5") == 123_450
        assert parse_money("19.99") == 1_999
        assert parse_implied_decimal("00000012345") == 12_345
        assert fmt(123_450) == "$1,234.50"
        for bad in ["1.234", "abc", "-5"]:
            with pytest.raises(ValueError):
                parse_money(bad)


def _batch(claims: list[tuple[str, int, list[int]]], trailer_count=None, trailer_total=None, with_trailer=True) -> str:
    recs = [format_record("H", {"batch_id": "B1", "created": "20261118"})]
    for cid, total, charges in claims:
        recs.append(
            format_record(
                "C",
                {
                    "claim_id": cid,
                    "member_id": "M10000000",
                    "npi": "1234567893",
                    "service_date": "20261103",
                    "pos": "11",
                    "dx1": "E119",
                    "dx2": "",
                    "total": str(total),
                },
            )
        )
        for i, ch in enumerate(charges, start=1):
            recs.append(
                format_record(
                    "L",
                    {
                        "claim_id": cid,
                        "line_no": str(i),
                        "code": "99213",
                        "modifier": "",
                        "units": "1",
                        "charge": str(ch),
                    },
                )
            )
    if with_trailer:
        recs.append(
            format_record(
                "T",
                {
                    "claim_count": str(trailer_count if trailer_count is not None else len(claims)),
                    "total": str(trailer_total if trailer_total is not None else sum(c[1] for c in claims)),
                },
            )
        )
    return "\n".join(recs) + "\n"


class TestLegacy:
    def test_parses_records_with_implied_decimals(self):
        r = parse_legacy(_batch([("C1", 30_000, [10_000, 20_000])]), "f.txt")
        assert r.batch_errors == [] and r.record_errors == []
        (c,) = r.claims
        assert c.total_charge_cents == 30_000
        assert [ln.charge_cents for ln in c.lines] == [10_000, 20_000]
        assert c.diagnosis_codes == ["E11.9"]
        assert c.source_ref == "f.txt#rec2"

    def test_records_are_fixed_length(self):
        assert all(len(line) == RECORD_LEN for line in _batch([("C1", 100, [100])]).splitlines())

    def test_trailer_count_mismatch_holds_the_batch(self):
        r = parse_legacy(_batch([("C1", 100, [100])], trailer_count=2), "f.txt")
        assert any("trailer says 2 claims" in e for e in r.batch_errors)

    def test_trailer_total_mismatch_holds_the_batch(self):
        r = parse_legacy(_batch([("C1", 100, [100])], trailer_total=101), "f.txt")
        assert any("trailer total" in e for e in r.batch_errors)

    def test_missing_trailer_means_truncated(self):
        r = parse_legacy(_batch([("C1", 100, [100])], with_trailer=False), "f.txt")
        assert any("truncated" in e for e in r.batch_errors)

    def test_record_after_trailer(self):
        text = _batch([("C1", 100, [100])]) + format_record("C", {"claim_id": "C2", "total": "1"}) + "\n"
        assert any("after the trailer" in e for e in parse_legacy(text, "f.txt").batch_errors)

    def test_bad_records_are_reported_not_fatal(self):
        text = _batch([("C1", 100, [100])]).replace("\n", "\nXshort\n", 1)
        r = parse_legacy(text, "f.txt")
        assert r.batch_errors == []
        assert r.record_errors == ["record 2: length 6, expected 80"]
        assert len(r.claims) == 1

    def test_line_for_unknown_claim(self):
        text = _batch([("C1", 100, [100])]).replace("LC1", "LZZ", 1)
        r = parse_legacy(text, "f.txt")
        assert any("unknown claim ZZ" in e for e in r.record_errors)


PORTAL_HEADER = "claim_id,member_id,member_name,member_dob,provider_npi,service_date,place_of_service,diagnosis_codes,procedure_code,modifier,units,charge\n"


class TestPortal:
    def test_groups_lines_and_parses_us_formats(self):
        text = PORTAL_HEADER + (
            'P1,M10000000,Ana Lima,01/19/1969,1234567893,11/03/2026,11,E11.9;I10,99214,,1,"$1,185.00"\n'
            "P1,M10000000,Ana Lima,01/19/1969,1234567893,11/03/2026,11,E11.9;I10,83036,qw,1,31.00\n"
        )
        claims, errors = parse_portal(text, "p.csv")
        assert errors == []
        (c,) = claims
        assert c.total_charge_cents == 121_600
        assert c.diagnosis_codes == ["E11.9", "I10"]
        assert c.lines[1].modifier == "QW"
        assert c.member_dob.isoformat() == "1969-01-19"

    def test_rows_of_one_claim_must_agree(self):
        text = PORTAL_HEADER + (
            "P1,M10000000,Ana Lima,01/19/1969,1234567893,11/03/2026,11,E11.9,99214,,1,10.00\n"
            "P1,M10000000,Ana Lima,01/19/1969,1234567893,11/04/2026,11,E11.9,83036,,1,10.00\n"
        )
        claims, errors = parse_portal(text, "p.csv")
        assert claims == []
        assert "service_date differs" in errors[0]

    def test_missing_columns(self):
        assert parse_portal("claim_id,charge\nP1,1\n", "p.csv")[1][0].startswith("missing column")

    def test_bad_value_is_reported_per_claim(self):
        text = PORTAL_HEADER + "P1,M10000000,Ana Lima,1969-01-19,1234567893,11/03/2026,11,E11.9,99214,,1,10.00\n"
        claims, errors = parse_portal(text, "p.csv")
        assert claims == [] and "row 2" in errors[0]
