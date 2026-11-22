"""Fixed-width claim export from a legacy (mainframe-era) claims system.

Record types, one per line, each exactly RECORD_LEN characters:
  H  batch header       batch id, creation date
  C  claim header       claim id, member id, NPI, service date, place of service, 2 diagnosis codes, total
  L  claim line         claim id, line number, procedure code, modifier, units, charge
  T  batch trailer      claim count and total charge: the control totals for the whole file

Amounts use implied decimals (no point): '00000012345' is $123.45. Dates are YYYYMMDD.
If the trailer's control totals do not match the records, the whole batch is held: a short or
corrupted transfer must never be loaded halfway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..codes import normalize_icd10
from ..models import Claim, ClaimLine
from ..money import parse_implied_decimal

RECORD_LEN = 80

# (name, start, end) with 0-based, end-exclusive offsets.
LAYOUT: dict[str, list[tuple[str, int, int]]] = {
    "H": [("type", 0, 1), ("batch_id", 1, 11), ("created", 11, 19)],
    "C": [
        ("type", 0, 1),
        ("claim_id", 1, 13),
        ("member_id", 13, 22),
        ("npi", 22, 32),
        ("service_date", 32, 40),
        ("pos", 40, 42),
        ("dx1", 42, 50),
        ("dx2", 50, 58),
        ("total", 58, 69),
    ],
    "L": [
        ("type", 0, 1),
        ("claim_id", 1, 13),
        ("line_no", 13, 15),
        ("code", 15, 20),
        ("modifier", 20, 22),
        ("units", 22, 25),
        ("charge", 25, 34),
    ],
    "T": [("type", 0, 1), ("claim_count", 1, 7), ("total", 7, 20)],
}


def format_record(kind: str, values: dict[str, str]) -> str:
    """Builds one record; used by the synthetic data generator and the tests."""
    buf = [" "] * RECORD_LEN
    for name, start, end in LAYOUT[kind]:
        v = kind if name == "type" else values.get(name, "")
        width = end - start
        if len(v) > width:
            raise ValueError(f"{kind}.{name} too long: {v!r}")
        cell = v.rjust(width, "0") if name in {"total", "charge", "units", "claim_count", "line_no"} else v.ljust(width)
        buf[start:end] = list(cell)
    return "".join(buf)


def _fields(kind: str, line: str) -> dict[str, str]:
    return {name: line[start:end].strip() for name, start, end in LAYOUT[kind]}


@dataclass
class BatchResult:
    batch_id: str | None
    claims: list[Claim]
    batch_errors: list[str]  # if non-empty, no claim from this file may be loaded
    record_errors: list[str]


def parse_legacy(text: str, source_name: str) -> BatchResult:
    lines = [ln.rstrip("\r\n") for ln in text.splitlines() if ln.strip()]
    batch_errors: list[str] = []
    record_errors: list[str] = []
    header: dict[str, str] | None = None
    trailer: dict[str, str] | None = None
    claims: dict[str, dict] = {}
    order: list[str] = []

    for n, raw in enumerate(lines, start=1):
        if len(raw) != RECORD_LEN:
            record_errors.append(f"record {n}: length {len(raw)}, expected {RECORD_LEN}")
            continue
        kind = raw[0]
        if kind not in LAYOUT:
            record_errors.append(f"record {n}: unknown record type {kind!r}")
            continue
        f = _fields(kind, raw)
        if kind == "H":
            if header is not None or n != 1:
                batch_errors.append("header must be the first record and appear once")
            header = f
        elif kind == "T":
            if trailer is not None:
                batch_errors.append("more than one trailer")
            trailer = f
        elif trailer is not None:
            batch_errors.append(f"record {n} after the trailer")
        elif kind == "C":
            if f["claim_id"] in claims:
                record_errors.append(f"record {n}: claim {f['claim_id']} header repeated")
                continue
            claims[f["claim_id"]] = {"hdr": f, "lines": [], "rec": n}
            order.append(f["claim_id"])
        elif kind == "L":
            c = claims.get(f["claim_id"])
            if c is None:
                record_errors.append(f"record {n}: line for unknown claim {f['claim_id']}")
                continue
            c["lines"].append((n, f))

    if header is None:
        batch_errors.append("missing header record")
    if trailer is None:
        batch_errors.append("missing trailer record (file may be truncated)")

    out: list[Claim] = []
    for cid in order:
        c = claims[cid]
        h = c["hdr"]
        try:
            claim_lines = [
                ClaimLine(
                    code=lf["code"],
                    modifier=lf["modifier"] or None,
                    units=int(lf["units"]),
                    charge_cents=parse_implied_decimal(lf["charge"]),
                )
                for _, lf in sorted(c["lines"], key=lambda x: int(x[1]["line_no"]))
            ]
            out.append(
                Claim(
                    claim_id=cid,
                    member_id=h["member_id"],
                    provider_npi=h["npi"],
                    service_date=datetime.strptime(h["service_date"], "%Y%m%d").date(),
                    place_of_service=h["pos"],
                    diagnosis_codes=[normalize_icd10(d) for d in (h["dx1"], h["dx2"]) if d],
                    lines=claim_lines,
                    total_charge_cents=parse_implied_decimal(h["total"]),
                    source="legacy",
                    source_ref=f"{source_name}#rec{c['rec']}",
                )
            )
        except ValueError as e:
            record_errors.append(f"record {c['rec']}: claim {cid}: {e}")

    if trailer is not None:
        # Control totals are checked against every claim header in the file, parsed or not.
        try:
            want_count = int(trailer["claim_count"])
            want_total = parse_implied_decimal(trailer["total"])
        except ValueError:
            batch_errors.append("trailer control totals are not numeric")
        else:
            got_total = sum(
                parse_implied_decimal(claims[c]["hdr"]["total"]) for c in order if claims[c]["hdr"]["total"].isdigit()
            )
            if want_count != len(order):
                batch_errors.append(f"trailer says {want_count} claims, file has {len(order)}")
            if want_total != got_total:
                batch_errors.append(f"trailer total {want_total} cents, claim headers sum to {got_total}")

    return BatchResult(header["batch_id"] if header else None, out, batch_errors, record_errors)


def today_str(d: date) -> str:
    return d.strftime("%Y%m%d")
