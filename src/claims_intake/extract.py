"""Field extraction from scanned claim forms (PNG/JPEG images and PDFs).

Two extractors share one interface:
  ClaudeExtractor  sends the page to Claude and gets back a schema-validated ExtractedClaim
                   (structured outputs), plus token usage for cost tracking.
  FakeExtractor    OFFLINE STAND-IN for tests and CI. It reads the answer key the synthetic
                   data generator wrote next to each scan. It exercises everything *around*
                   extraction (routing, review, cost and drift accounting) and says nothing
                   about how well a model reads the forms.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from .codes import normalize_icd10
from .models import Claim, ClaimLine
from .money import parse_money

# Per million tokens, USD (Anthropic first-party list prices). Stored as micro-dollars per token
# so cost arithmetic stays in integers.
PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}

REQUIRED_FIELDS = [
    "claim_id",
    "member_id",
    "provider_npi",
    "service_date",
    "place_of_service",
    "diagnosis_codes",
    "lines",
]


class ExtractedLine(BaseModel):
    code: str = Field(description="CPT or HCPCS code exactly as printed")
    modifier: str | None = Field(default=None, description="Two-character modifier, or null if blank")
    units: int
    charge: str = Field(description="Line charge as printed, e.g. '125.00'")


class ExtractedClaim(BaseModel):
    """What the model returns. Strings are kept as printed; the pipeline parses and validates them."""

    claim_id: str | None
    member_id: str | None
    member_name: str | None
    member_dob: str | None = Field(description="YYYY-MM-DD, or null if not legible")
    provider_npi: str | None
    service_date: str | None = Field(description="YYYY-MM-DD, or null if not legible")
    place_of_service: str | None
    diagnosis_codes: list[str]
    lines: list[ExtractedLine]
    total_charge: str | None = Field(description="Total charge as printed, e.g. '250.00'")
    unclear_fields: list[str] = Field(
        description="Names of fields you could not read with confidence (smudged, cut off, ambiguous "
        "characters, handwriting). Leave a field out of this list only if you are sure of every character."
    )


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    latency_ms: int = 0

    @property
    def cost_micro_usd(self) -> int:
        inp, out = PRICES_PER_MTOK.get(self.model, (0.0, 0.0))
        # $/MTok == micro-dollars per token. Cache reads are billed at 10% of input.
        uncached = self.input_tokens
        return round(uncached * inp + self.cache_read_input_tokens * inp * 0.1 + self.output_tokens * out)


@dataclass
class Extraction:
    claim: Claim | None
    unclear: list[str]
    usage: Usage
    error: str | None = None


class Extractor(Protocol):
    name: str

    def extract(self, path: Path) -> Extraction: ...


def to_claim(x: ExtractedClaim, source_ref: str) -> tuple[Claim | None, list[str], str | None]:
    """Turns the as-printed extraction into a Claim. Fields that do not parse are added to `unclear`."""
    unclear = sorted(set(x.unclear_fields))
    missing = [f for f in REQUIRED_FIELDS if getattr(x, f) in (None, "", [])]
    if missing:
        return None, sorted(set(unclear) | set(missing)), f"required field(s) not found: {', '.join(missing)}"
    try:
        lines = [
            ClaimLine(
                code=ln.code.strip().upper(),
                modifier=(ln.modifier or "").strip().upper() or None,
                units=ln.units,
                charge_cents=parse_money(ln.charge),
            )
            for ln in x.lines
        ]
        claim = Claim(
            claim_id=x.claim_id.strip(),
            member_id=x.member_id.strip(),
            member_name=x.member_name,
            member_dob=date.fromisoformat(x.member_dob) if x.member_dob else None,
            provider_npi=x.provider_npi.replace(" ", ""),
            service_date=date.fromisoformat(x.service_date),
            place_of_service=x.place_of_service.strip().zfill(2),
            diagnosis_codes=[normalize_icd10(d) for d in x.diagnosis_codes],
            lines=lines,
            total_charge_cents=parse_money(x.total_charge) if x.total_charge else sum(ln.charge_cents for ln in lines),
            source="scan",
            source_ref=source_ref,
            unclear_fields=unclear,
        )
    except (ValueError, AttributeError) as e:
        return None, unclear, f"extracted values do not parse: {type(e).__name__}"
    return claim, unclear, None


SYSTEM_PROMPT = """You transcribe scanned professional medical claim forms into structured data for a claims intake pipeline.
Copy values exactly as printed. Do not correct, complete, or infer anything: a wrong value that looks right is worse than a missing one, because a person reviews only what you flag.
If a value is missing, write null (or an empty list). If you can read it but are not certain of every character, give your best reading AND add the field name to unclear_fields."""


class ClaudeExtractor:
    """Real extraction with Claude. Needs Anthropic credentials and spends API credits."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", client=None):
        import anthropic

        self.name = f"claude:{model}"
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic(max_retries=3)

    def extract(self, path: Path) -> Extraction:
        import anthropic

        media_type = MEDIA_TYPES[path.suffix.lower()]
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        block_type = "document" if media_type == "application/pdf" else "image"
        started = time.monotonic()
        usage = Usage(self.model)
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                # Effort is low: this is transcription, not reasoning. Measure before raising it.
                output_config={"effort": self.effort},
                output_format=ExtractedClaim,
                # If a safety classifier declines, the API retries on a fallback model in the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data}},
                            {"type": "text", "text": "Transcribe this claim form."},
                        ],
                    }
                ],
            )
        except anthropic.RateLimitError:
            raise  # retryable: let the orchestrator's task retry handle it
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise
            return Extraction(None, [], usage, f"API error {e.status_code}")
        usage.latency_ms = int((time.monotonic() - started) * 1000)
        usage.model = response.model
        usage.input_tokens = response.usage.input_tokens
        usage.output_tokens = response.usage.output_tokens
        usage.cache_read_input_tokens = response.usage.cache_read_input_tokens or 0
        if response.stop_reason == "refusal":
            return Extraction(None, [], usage, "model declined")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            return Extraction(None, [], usage, "no structured output")
        claim, unclear, err = to_claim(response.parsed_output, path.name)
        return Extraction(claim, unclear, usage, err)


class FakeExtractor:
    """OFFLINE STAND-IN: returns the generator's answer key for each scan (see module docstring)."""

    name = "fake"

    def __init__(self, truth_dir: Path, tokens_per_page: tuple[int, int] = (1800, 450), model: str = "claude-opus-5-5"):
        self.truth_dir = truth_dir
        self.tokens = tokens_per_page
        self.model = model
        self._attempts: dict[str, int] = {}

    def extract(self, path: Path) -> Extraction:
        truth_file = self.truth_dir / (path.name + ".json")
        # Simulated usage so cost accounting can be tested; NOT a measurement.
        usage = Usage(self.model, input_tokens=self.tokens[0], output_tokens=self.tokens[1], latency_ms=0)
        if not truth_file.exists():
            return Extraction(None, [], usage, "no answer key for this file")
        payload = json.loads(truth_file.read_text())
        # Simulates a flaky API: the first N attempts for this file fail, then it succeeds.
        failures_left = payload.get("fail_attempts", 0) - self._attempts.get(path.name, 0)
        self._attempts[path.name] = self._attempts.get(path.name, 0) + 1
        if failures_left > 0:
            raise ConnectionError("simulated transient extractor failure")
        x = ExtractedClaim.model_validate(payload["extracted"])
        claim, unclear, err = to_claim(x, path.name)
        return Extraction(claim, unclear, usage, err)


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
