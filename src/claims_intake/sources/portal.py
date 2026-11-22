"""CSV export from a provider portal: one row per claim line, claim fields repeated on each row.

Different conventions from the legacy file on purpose, because that is what real feeds look like:
US dates (MM/DD/YYYY), dollar amounts with $ and commas, diagnosis codes separated by ';'.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from ..codes import normalize_icd10
from ..models import Claim, ClaimLine
from ..money import parse_money

COLUMNS = [
    "claim_id",
    "member_id",
    "member_name",
    "member_dob",
    "provider_npi",
    "service_date",
    "place_of_service",
    "diagnosis_codes",
    "procedure_code",
    "modifier",
    "units",
    "charge",
]

CLAIM_FIELDS = COLUMNS[:8]


def _date(s: str):
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def parse_portal(text: str, source_name: str) -> tuple[list[Claim], list[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        return [], [f"missing column(s): {', '.join(missing)}"]

    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for i, row in enumerate(reader, start=2):
        groups.setdefault(row["claim_id"].strip(), []).append((i, {k: (v or "").strip() for k, v in row.items()}))

    claims: list[Claim] = []
    errors: list[str] = []
    for cid, rows in groups.items():
        first_row, first = rows[0]
        try:
            for rn, r in rows[1:]:
                for k in CLAIM_FIELDS:
                    if r[k] != first[k]:
                        raise ValueError(f"row {rn}: {k} differs from row {first_row} of the same claim")
            lines = [
                ClaimLine(
                    code=r["procedure_code"].upper(),
                    modifier=r["modifier"].upper() or None,
                    units=int(r["units"] or "1"),
                    charge_cents=parse_money(r["charge"]),
                )
                for _, r in rows
            ]
            claims.append(
                Claim(
                    claim_id=cid,
                    member_id=first["member_id"],
                    member_name=first["member_name"] or None,
                    member_dob=_date(first["member_dob"]) if first["member_dob"] else None,
                    provider_npi=first["provider_npi"],
                    service_date=_date(first["service_date"]),
                    place_of_service=first["place_of_service"].zfill(2),
                    diagnosis_codes=[normalize_icd10(d) for d in first["diagnosis_codes"].split(";") if d.strip()],
                    lines=lines,
                    # The portal has no claim total; the sum of the lines is the claim's charge.
                    total_charge_cents=sum(ln.charge_cents for ln in lines),
                    source="portal",
                    source_ref=f"{source_name}#row{first_row}",
                )
            )
        except ValueError as e:
            errors.append(f"claim at row {first_row}: {e}")
    return claims, errors
