"""Claim validation and routing.

Every check returns an Issue with a machine code. Messages never contain patient data, because
they end up in logs, metrics and alerts; the review queue holds the claim itself for a person
with access to look at.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date

from .codes import MEMBER_ID_RE, MODIFIER_RE, POS_RE, valid_icd10, valid_npi, valid_procedure
from .models import Claim, Decision, Issue


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    dob: date
    coverage_start: date
    coverage_end: date | None


def load_roster(text: str) -> dict[str, Member]:
    out: dict[str, Member] = {}
    for r in csv.DictReader(io.StringIO(text)):
        out[r["member_id"]] = Member(
            r["member_id"],
            r["name"],
            date.fromisoformat(r["dob"]),
            date.fromisoformat(r["coverage_start"]),
            date.fromisoformat(r["coverage_end"]) if r["coverage_end"] else None,
        )
    return out


def dedupe_key(c: Claim) -> str:
    """Same patient, provider, day, codes and charges = the same claim, whichever channel sent it."""
    parts = [
        c.member_id,
        c.provider_npi,
        c.service_date.isoformat(),
        ",".join(sorted(c.diagnosis_codes)),
        "|".join(sorted(f"{ln.code}:{ln.modifier or ''}:{ln.units}:{ln.charge_cents}" for ln in c.lines)),
    ]
    return "sha256:" + hashlib.sha256("\n".join(parts).encode()).hexdigest()


def validate(c: Claim, roster: dict[str, Member], today: date) -> list[Issue]:
    issues: list[Issue] = []

    def err(code: str, field: str | None, message: str) -> None:
        issues.append(Issue(code=code, field=field, severity="error", message=message))

    def warn(code: str, field: str | None, message: str) -> None:
        issues.append(Issue(code=code, field=field, severity="warning", message=message))

    if not MEMBER_ID_RE.fullmatch(c.member_id):
        err("member_id_format", "member_id", "member id is not M followed by 8 digits")
    member = roster.get(c.member_id)
    if member is None:
        err("member_unknown", "member_id", "member id is not on the eligibility file")
    else:
        if c.service_date < member.coverage_start or (member.coverage_end and c.service_date > member.coverage_end):
            err("not_eligible", "service_date", "member was not covered on the service date")
        if c.member_dob and c.member_dob != member.dob:
            warn("dob_mismatch", "member_dob", "date of birth on the claim differs from eligibility")
        if c.member_name and _norm(c.member_name) != _norm(member.name):
            warn("name_mismatch", "member_name", "member name on the claim differs from eligibility")

    if not valid_npi(c.provider_npi):
        err("npi_invalid", "provider_npi", "NPI is not 10 digits with a valid check digit")
    if c.service_date > today:
        err("service_in_future", "service_date", "service date is after today")
    elif (today - c.service_date).days > 365:
        warn("timely_filing", "service_date", "service date is more than 365 days ago")
    if member and c.service_date < member.dob:
        err("service_before_birth", "service_date", "service date is before the member's date of birth")
    if not POS_RE.fullmatch(c.place_of_service):
        err("pos_format", "place_of_service", "place of service is not a 2-digit code")

    if not c.diagnosis_codes:
        err("dx_missing", "diagnosis_codes", "no diagnosis code")
    for i, dx in enumerate(c.diagnosis_codes, start=1):
        if not valid_icd10(dx):
            err("dx_format", "diagnosis_codes", f"diagnosis {i} is not ICD-10-CM format")
    if len(set(c.diagnosis_codes)) != len(c.diagnosis_codes):
        warn("dx_repeated", "diagnosis_codes", "a diagnosis code is listed twice")

    if not c.lines:
        err("no_lines", "lines", "claim has no service lines")
    for i, ln in enumerate(c.lines, start=1):
        if not valid_procedure(ln.code):
            err("procedure_format", "lines", f"line {i}: procedure code is not CPT/HCPCS format")
        if ln.modifier and not MODIFIER_RE.fullmatch(ln.modifier):
            err("modifier_format", "lines", f"line {i}: modifier is not 2 characters")
        if ln.units < 1:
            err("units", "lines", f"line {i}: units must be at least 1")
        if ln.charge_cents <= 0:
            err("charge", "lines", f"line {i}: charge must be positive")
    line_sum = sum(ln.charge_cents for ln in c.lines)
    if line_sum != c.total_charge_cents:
        err("total_mismatch", "total_charge", f"lines sum to {line_sum} cents, claim total is {c.total_charge_cents}")

    for f in c.unclear_fields:
        warn("unclear_field", f, f"extractor could not read {f} with confidence")
    return issues


def _norm(name: str) -> str:
    """'LIMA, ANA', ' ana  lima ' and 'Ana Lima' compare equal; claims often carry 'LAST, FIRST'."""
    n = name.strip().upper()
    if "," in n:
        last, _, first = n.partition(",")
        n = f"{first} {last}"
    return " ".join(n.split())


def route(issues: list[Issue], *, is_duplicate: bool) -> Decision:
    """Errors a person could fix from the document go to review; data that can never be valid is rejected.

    A claim is auto-accepted only with no issue at all. Anything a reviewer could resolve by
    looking at the document (format errors, unclear fields, mismatches) goes to review, so
    the pipeline never silently drops a claim a person could have saved.
    """
    if is_duplicate:
        return "duplicate"
    hard_reject = {"member_unknown", "not_eligible", "service_in_future", "service_before_birth"}
    if any(i.code in hard_reject for i in issues):
        return "rejected"
    if issues:
        return "review"
    return "accepted"
