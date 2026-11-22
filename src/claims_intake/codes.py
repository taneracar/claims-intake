"""Format checks for the identifiers on a professional claim.

These are format and check-digit rules only. They do not prove a code exists in the current
CPT or ICD-10-CM release (that needs the licensed code sets), or that an NPI is enrolled.
"""

from __future__ import annotations

import re

# ICD-10-CM: letter, digit, digit-or-letter, then optionally a dot and 1-4 characters.
ICD10_RE = re.compile(r"^[A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?$")
# CPT: five digits, or four digits + F (Category II) / T (Category III). HCPCS Level II: letter + four digits.
CPT_RE = re.compile(r"^(?:\d{5}|\d{4}[FT])$")
HCPCS_RE = re.compile(r"^[A-V]\d{4}$")
MODIFIER_RE = re.compile(r"^[0-9A-Z]{2}$")
POS_RE = re.compile(r"^\d{2}$")
MEMBER_ID_RE = re.compile(r"^M\d{8}$")


def luhn_check_digit(digits: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:  # positions that get doubled, counting from the right of the payload
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def npi_check_digit(first9: str) -> int:
    """NPI check digit: Luhn over the 9 digits with the card-issuer prefix 80840 in front."""
    return luhn_check_digit("80840" + first9)


def valid_npi(npi: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", npi)) and npi_check_digit(npi[:9]) == int(npi[9])


def normalize_icd10(code: str) -> str:
    """Accepts 'E119' or 'E11.9' and returns the dotted form."""
    c = code.strip().upper().replace(" ", "")
    if "." not in c and len(c) > 3:
        c = c[:3] + "." + c[3:]
    return c


def valid_icd10(code: str) -> bool:
    return bool(ICD10_RE.fullmatch(code))


def valid_procedure(code: str) -> bool:
    return bool(CPT_RE.fullmatch(code) or HCPCS_RE.fullmatch(code))
