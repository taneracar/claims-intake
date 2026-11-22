"""Normalized claim model shared by every source. Money is integer cents; dates are real dates."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["legacy", "portal", "scan"]


class ClaimLine(BaseModel):
    code: str  # CPT or HCPCS
    modifier: str | None = None
    units: int = 1
    charge_cents: int


class Claim(BaseModel):
    claim_id: str
    member_id: str
    member_name: str | None = None
    member_dob: date | None = None
    provider_npi: str
    service_date: date
    place_of_service: str
    diagnosis_codes: list[str]
    lines: list[ClaimLine]
    total_charge_cents: int
    source: Source
    source_ref: str  # file name (and record or row number) the claim came from
    # Fields a scan extractor could not read with confidence. Empty for structured sources.
    unclear_fields: list[str] = Field(default_factory=list)

    def phi_values(self) -> list[str]:
        """Every value in this claim that identifies the patient; used to scrub logs and alerts."""
        out = [self.member_id]
        if self.member_name:
            out.append(self.member_name)
            out.extend(p for p in self.member_name.split() if len(p) > 2)
        if self.member_dob:
            out += [
                self.member_dob.isoformat(),
                self.member_dob.strftime("%m/%d/%Y"),
                self.member_dob.strftime("%Y%m%d"),
            ]
        return out


class Issue(BaseModel):
    code: str  # machine-readable, e.g. "npi_check_digit"
    field: str | None = None
    severity: Literal["error", "warning"]
    message: str  # must not contain PHI; it is logged and shown in metrics


Decision = Literal["accepted", "review", "rejected", "duplicate"]
