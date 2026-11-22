from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from claims_intake.extract import ClaudeExtractor, ExtractedClaim, FakeExtractor, Usage, to_claim


def extracted(**over) -> ExtractedClaim:
    base = dict(
        claim_id="S-1",
        member_id="M10000000",
        member_name="ANA LIMA",
        member_dob="1969-01-19",
        provider_npi="1234 567 893",
        service_date="2026-11-13",
        place_of_service="11",
        diagnosis_codes=["e119"],
        lines=[
            {"code": "99214", "modifier": "25", "units": 1, "charge": "185.00"},
            {"code": "83036", "modifier": None, "units": 1, "charge": "31"},
        ],
        total_charge="$216.00",
        unclear_fields=[],
    )
    base.update(over)
    return ExtractedClaim.model_validate(base)


class TestToClaim:
    def test_parses_as_printed_values(self):
        c, unclear, err = to_claim(extracted(), "scan.png")
        assert err is None and unclear == []
        assert c.provider_npi == "1234567893"
        assert c.diagnosis_codes == ["E11.9"]
        assert c.total_charge_cents == 21_600
        assert [ln.charge_cents for ln in c.lines] == [18_500, 3_100]
        assert c.source == "scan"

    def test_missing_required_field_fails_and_is_named(self):
        c, unclear, err = to_claim(extracted(provider_npi=None), "scan.png")
        assert c is None
        assert "provider_npi" in err and "provider_npi" in unclear

    def test_unparseable_value_fails_without_echoing_it(self):
        c, _, err = to_claim(extracted(service_date="11/13/2026"), "scan.png")
        assert c is None and "11/13" not in err

    def test_unclear_fields_are_kept_on_the_claim(self):
        c, unclear, _ = to_claim(extracted(unclear_fields=["member_dob", "member_dob"]), "scan.png")
        assert unclear == ["member_dob"] and c.unclear_fields == ["member_dob"]


class TestUsageCost:
    def test_cost_in_micro_dollars(self):
        assert Usage("claude-opus-5-5", input_tokens=1_000_000).cost_micro_usd == 4_000_000
        assert Usage("claude-sonnet-5-5", input_tokens=2000, output_tokens=500).cost_micro_usd == 2000 * 2 + 500 * 10
        assert Usage("claude-haiku-5-5", output_tokens=1_000_000).cost_micro_usd == 500_000
        assert Usage("unknown-model", input_tokens=10).cost_micro_usd == 0


class FakeMessages:
    """Stands in for client.beta.messages; records the request and returns a canned response."""

    def __init__(self, response=None, error: Exception | None = None):
        self.response, self.error, self.calls = response, error, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(messages: FakeMessages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def response(stop="end_turn", parsed=None):
    return SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason=stop,
        parsed_output=parsed,
        usage=SimpleNamespace(input_tokens=1500, output_tokens=400, cache_read_input_tokens=None),
    )


@pytest.fixture
def png(tmp_path) -> Path:
    p = tmp_path / "form.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return p


class TestClaudeExtractor:
    def test_request_shape_and_result(self, png):
        msgs = FakeMessages(response(parsed=extracted()))
        ex = ClaudeExtractor(client=fake_client(msgs))
        result = ex.extract(png)
        assert result.error is None and result.claim.claim_id == "S-1"
        assert result.usage.input_tokens == 1500 and result.usage.cost_micro_usd == 1500 * 4 + 400 * 20
        call = msgs.calls[0]
        assert call["model"] == "claude-opus-5-5"
        assert call["output_format"] is ExtractedClaim
        assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
        assert call["output_config"] == {"effort": "low"}
        block = call["messages"][0]["content"][0]
        assert block["type"] == "image" and block["source"]["media_type"] == "image/png"

    def test_pdf_goes_as_a_document_block(self, tmp_path):
        pdf = tmp_path / "form.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        msgs = FakeMessages(response(parsed=extracted()))
        ClaudeExtractor(client=fake_client(msgs)).extract(pdf)
        assert msgs.calls[0]["messages"][0]["content"][0]["type"] == "document"

    def test_refusal_is_a_failed_extraction_not_a_crash(self, png):
        result = ClaudeExtractor(client=fake_client(FakeMessages(response(stop="refusal")))).extract(png)
        assert result.claim is None and result.error == "model declined"
        assert result.usage.output_tokens == 400  # still billed, still counted

    def test_truncated_output(self, png):
        result = ClaudeExtractor(client=fake_client(FakeMessages(response(stop="max_tokens")))).extract(png)
        assert result.error == "no structured output"

    def _status_error(self, cls, code):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        return cls("x", response=httpx.Response(code, request=req), body=None)

    def test_rate_limit_and_server_errors_raise_so_the_task_retries(self, png):
        for err in [
            self._status_error(anthropic.RateLimitError, 429),
            self._status_error(anthropic.InternalServerError, 500),
        ]:
            with pytest.raises(anthropic.APIStatusError):
                ClaudeExtractor(client=fake_client(FakeMessages(error=err))).extract(png)

    def test_client_errors_do_not_retry(self, png):
        err = self._status_error(anthropic.BadRequestError, 400)
        result = ClaudeExtractor(client=fake_client(FakeMessages(error=err))).extract(png)
        assert result.claim is None and result.error == "API error 400"


class TestFakeExtractor:
    def test_reads_answer_key_and_simulates_flaky_attempts(self, data):
        ex = FakeExtractor(data / "truth")
        with pytest.raises(ConnectionError):
            ex.extract(data / "inbox" / "scan_06.png")
        assert ex.extract(data / "inbox" / "scan_06.png").claim.claim_id == "S-3006"

    def test_file_without_answer_key(self, data, tmp_path):
        other = tmp_path / "unknown.png"
        other.write_bytes(b"x")
        assert FakeExtractor(data / "truth").extract(other).error == "no answer key for this file"
