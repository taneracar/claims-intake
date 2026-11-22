"""Deterministic synthetic data: an eligibility file, two legacy batches, a portal CSV and scanned forms.

Every person, provider and claim is made up. Each planted problem is listed in PLANTED so the
README and the tests can check that the pipeline finds exactly these.
"""

from __future__ import annotations

import csv
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .codes import npi_check_digit
from .sources.legacy import format_record
from .sources.portal import COLUMNS

TODAY = date(2026, 11, 21)

FIRST = [
    "Ana",
    "Ben",
    "Carla",
    "Dev",
    "Elena",
    "Femi",
    "Grace",
    "Hugo",
    "Iris",
    "Jamal",
    "Kira",
    "Luis",
    "Maya",
    "Noor",
    "Owen",
    "Priya",
]
LAST = [
    "Lima",
    "Okafor",
    "Reyes",
    "Shah",
    "Novak",
    "Adeyemi",
    "Kim",
    "Brandt",
    "Costa",
    "Wright",
    "Sato",
    "Mendez",
    "Patel",
    "Haddad",
    "Burke",
    "Rao",
]


def npi(base9: str) -> str:
    return base9 + str(npi_check_digit(base9))


PROVIDERS = [npi("123456789"), npi("198765432"), npi("145236987"), npi("176543210"), npi("154321098")]
BAD_NPI = PROVIDERS[0][:9] + str((int(PROVIDERS[0][9]) + 1) % 10)  # check digit off by one

PLANTED = {
    "L-1004": "total_mismatch",
    "L-1005": "not_eligible",
    "L-1006": "npi_invalid",
    "legacy_20261119.txt": "batch_held (trailer count)",
    "P-2002": "duplicate (same claim as L-1001)",
    "P-2003": "dx_format",
    "P-2004": "service_in_future",
    "P-2005": "name_mismatch",
    "scan_03.png": "unclear_field (member_dob)",
    "scan_04.png": "extraction failure (NPI missing)",
    "scan_06.png": "transient extractor error on first attempt (retried)",
}


def members() -> list[dict]:
    rng = random.Random(7)
    out = []
    for i in range(16):
        dob = date(1940, 1, 1) + timedelta(days=rng.randrange(0, 365 * 60))
        out.append(
            {
                "member_id": f"M{10_000_000 + i * 137:08d}",
                "name": f"{FIRST[i]} {LAST[i]}",
                "dob": dob.isoformat(),
                "plan": "MAPD-01",
                "coverage_start": "2026-01-01",
                # Member 4 lost coverage at the end of September.
                "coverage_end": "2026-09-30" if i == 4 else "",
            }
        )
    return out


def _claim(cid, m, prov, svc, dx, lines, pos="11"):
    return {"claim_id": cid, "member": m, "npi": prov, "service_date": svc, "pos": pos, "dx": dx, "lines": lines}


def legacy_claims(ms: list[dict]) -> list[dict]:
    return [
        _claim("L-1001", ms[0], PROVIDERS[0], date(2026, 11, 3), ["E11.9", "I10"], [("99214", "", 1, 18500)]),
        _claim(
            "L-1002",
            ms[1],
            PROVIDERS[1],
            date(2026, 11, 4),
            ["J45.909"],
            [("99213", "", 1, 12900), ("94010", "", 1, 6400)],
        ),
        _claim(
            "L-1003",
            ms[2],
            PROVIDERS[2],
            date(2026, 11, 5),
            ["M54.50"],
            [("97110", "GP", 3, 9600), ("97140", "GP", 2, 7000)],
        ),
        _claim(
            "L-1004",
            ms[3],
            PROVIDERS[3],
            date(2026, 11, 6),
            ["E78.5"],
            [("99213", "", 1, 12900), ("80061", "", 1, 4200)],
        ),
        _claim("L-1005", ms[4], PROVIDERS[1], date(2026, 11, 7), ["I25.10"], [("93000", "", 1, 7500)]),
        _claim("L-1006", ms[5], BAD_NPI, date(2026, 11, 7), ["N18.3"], [("99214", "", 1, 18500)]),
    ]


def legacy_text(
    claims: list[dict], batch_id: str, created: date, trailer_count_delta: int = 0, total_off: dict | None = None
) -> str:
    recs = [format_record("H", {"batch_id": batch_id, "created": created.strftime("%Y%m%d")})]
    grand = 0
    for c in claims:
        total = sum(ch for *_, ch in c["lines"]) + (total_off or {}).get(c["claim_id"], 0)
        grand += total
        recs.append(
            format_record(
                "C",
                {
                    "claim_id": c["claim_id"],
                    "member_id": c["member"]["member_id"],
                    "npi": c["npi"],
                    "service_date": c["service_date"].strftime("%Y%m%d"),
                    "pos": c["pos"],
                    "dx1": c["dx"][0].replace(".", ""),
                    "dx2": c["dx"][1].replace(".", "") if len(c["dx"]) > 1 else "",
                    "total": str(total),
                },
            )
        )
        for n, (code, mod, units, charge) in enumerate(c["lines"], start=1):
            recs.append(
                format_record(
                    "L",
                    {
                        "claim_id": c["claim_id"],
                        "line_no": str(n),
                        "code": code,
                        "modifier": mod,
                        "units": str(units),
                        "charge": str(charge),
                    },
                )
            )
    recs.append(format_record("T", {"claim_count": str(len(claims) + trailer_count_delta), "total": str(grand)}))
    return "\n".join(recs) + "\n"


def portal_claims(ms: list[dict], l1001: dict) -> list[dict]:
    dup = dict(l1001, claim_id="P-2002")  # same visit, resent through the portal with its own id
    return [
        _claim("P-2001", ms[6], PROVIDERS[4], date(2026, 11, 10), ["Z00.00"], [("99396", "", 1, 24500)]),
        dup,
        _claim("P-2003", ms[7], PROVIDERS[2], date(2026, 11, 11), ["11.9E"], [("99213", "", 1, 12900)]),
        _claim("P-2004", ms[8], PROVIDERS[3], date(2026, 12, 5), ["R51.9"], [("99212", "", 1, 8800)]),
        _claim(
            "P-2005",
            ms[9],
            PROVIDERS[0],
            date(2026, 11, 12),
            ["K21.9"],
            [("99213", "", 1, 12900), ("G0447", "", 1, 2600)],
        ),
    ]


def portal_text(claims: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for c in claims:
        m = c["member"]
        name = m["name"]
        if c["claim_id"] == "P-2005":
            name = name.replace("a", "e", 1)  # typo in the name the office typed
        for code, mod, units, charge in c["lines"]:
            w.writerow(
                [
                    c["claim_id"],
                    m["member_id"],
                    name,
                    date.fromisoformat(m["dob"]).strftime("%m/%d/%Y"),
                    c["npi"],
                    c["service_date"].strftime("%m/%d/%Y"),
                    c["pos"],
                    ";".join(c["dx"]),
                    code,
                    mod,
                    units,
                    f"${charge / 100:,.2f}",
                ]
            )
    return buf.getvalue()


def scan_claims(ms: list[dict]) -> list[tuple[str, dict, dict]]:
    """(file name, claim, overrides for the answer key)."""
    return [
        (
            "scan_01.png",
            _claim(
                "S-3001",
                ms[10],
                PROVIDERS[1],
                date(2026, 11, 13),
                ["E11.65"],
                [("99214", "25", 1, 18500), ("83036", "", 1, 3100)],
            ),
            {},
        ),
        (
            "scan_02.png",
            _claim("S-3002", ms[11], PROVIDERS[4], date(2026, 11, 14), ["M17.11"], [("20610", "RT", 1, 21000)]),
            {},
        ),
        (
            "scan_03.png",
            _claim("S-3003", ms[12], PROVIDERS[2], date(2026, 11, 14), ["J06.9"], [("99213", "", 1, 12900)]),
            {"unclear_fields": ["member_dob"]},
        ),
        (
            "scan_04.png",
            _claim("S-3004", ms[13], PROVIDERS[3], date(2026, 11, 15), ["L03.115"], [("10060", "", 1, 26000)]),
            {"provider_npi": None},
        ),
        (
            "scan_05.pdf",
            _claim("S-3005", ms[14], PROVIDERS[0], date(2026, 11, 16), ["F41.1"], [("90834", "", 1, 15500)]),
            {},
        ),
        (
            "scan_06.png",
            _claim("S-3006", ms[15], PROVIDERS[1], date(2026, 11, 17), ["H52.4"], [("92004", "", 1, 19800)]),
            {"_fail_attempts": 1},
        ),
    ]


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def render_form(c: dict, path: Path, rng: random.Random, hide_npi: bool, smudge_dob: bool) -> None:
    W, H = 1275, 1650  # US Letter at 150 dpi
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    big, mid, small = _font(40), _font(28), _font(22)
    d.text((60, 50), "HEALTH INSURANCE CLAIM FORM (SYNTHETIC SAMPLE)", font=big, fill=0)
    d.text((60, 105), "Not a real claim. Generated test data.", font=small, fill=90)
    m = c["member"]

    def field(x, y, label, value, w=520):
        d.rectangle([x, y, x + w, y + 70], outline=0, width=2)
        d.text((x + 8, y + 4), label, font=small, fill=60)
        if value:
            d.text((x + 12, y + 32), value, font=mid, fill=0)

    field(60, 160, "1. CLAIM NUMBER", c["claim_id"])
    field(640, 160, "2. INSURED'S ID NUMBER", m["member_id"])
    field(60, 250, "3. PATIENT'S NAME", m["name"].upper())
    dob = date.fromisoformat(m["dob"]).strftime("%m / %d / %Y")
    field(640, 250, "4. PATIENT'S BIRTH DATE (MM DD YYYY)", dob)
    if smudge_dob:
        box = (650, 280, 1150, 318)
        img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(6)), box)
    field(60, 340, "24J. RENDERING PROVIDER NPI", "" if hide_npi else c["npi"])
    field(640, 340, "24B. PLACE OF SERVICE", c["pos"])
    field(60, 430, "21. DIAGNOSIS (ICD-10)", "   ".join(c["dx"]), w=1100)
    d.text((60, 530), "24. SERVICE LINES", font=mid, fill=0)
    cols = [("DATE OF SERVICE", 60), ("CPT/HCPCS", 330), ("MOD", 560), ("UNITS", 680), ("CHARGES", 840)]
    for label, x in cols:
        d.text((x, 575), label, font=small, fill=60)
    y = 610
    for code, mod, units, charge in c["lines"]:
        d.line([60, y - 6, 1160, y - 6], fill=150)
        vals = [c["service_date"].strftime("%m/%d/%Y"), code, mod, str(units), f"{charge / 100:,.2f}"]
        for (_, x), v in zip(cols, vals, strict=True):
            d.text((x, y), v, font=mid, fill=0)
        y += 50
    total = sum(ch for *_, ch in c["lines"])
    field(640, y + 30, "28. TOTAL CHARGE", f"$ {total / 100:,.2f}")
    # A little scanner character: slight skew and noise.
    img = img.rotate(rng.uniform(-1.2, 1.2), fillcolor=255, expand=False)
    px = img.load()
    for _ in range(4000):
        px[rng.randrange(W), rng.randrange(H)] = rng.choice([0, 80, 200])
    if path.suffix == ".pdf":
        img.convert("RGB").save(path, "PDF", resolution=150)
    else:
        img.save(path, optimize=True)


def answer_key(c: dict, overrides: dict) -> dict:
    m = c["member"]
    extracted = {
        "claim_id": c["claim_id"],
        "member_id": m["member_id"],
        "member_name": m["name"].upper(),
        "member_dob": m["dob"],
        "provider_npi": c["npi"],
        "service_date": c["service_date"].isoformat(),
        "place_of_service": c["pos"],
        "diagnosis_codes": c["dx"],
        "lines": [
            {"code": code, "modifier": mod or None, "units": units, "charge": f"{charge / 100:.2f}"}
            for code, mod, units, charge in c["lines"]
        ],
        "total_charge": f"{sum(ch for *_, ch in c['lines']) / 100:.2f}",
        "unclear_fields": [],
    }
    key = {"extracted": extracted}
    for k, v in overrides.items():
        if k == "_fail_attempts":
            key["fail_attempts"] = v
        else:
            extracted[k] = v
    return key


def generate(root: Path) -> None:
    rng = random.Random(42)
    inbox, truth = root / "inbox", root / "truth"
    inbox.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)
    ms = members()
    with (root / "eligibility.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ms[0]))
        w.writeheader()
        w.writerows(ms)

    lc = legacy_claims(ms)
    (inbox / "legacy_20261118.txt").write_text(
        legacy_text(lc, "B000118", date(2026, 11, 18), total_off={"L-1004": 1000})
    )
    extra = [
        _claim("L-1101", ms[0], PROVIDERS[2], date(2026, 11, 9), ["I10"], [("99212", "", 1, 8800)]),
        _claim("L-1102", ms[1], PROVIDERS[3], date(2026, 11, 9), ["E66.9"], [("99213", "", 1, 12900)]),
        _claim("L-1103", ms[2], PROVIDERS[4], date(2026, 11, 10), ["Z23"], [("90686", "", 1, 3500)]),
    ]
    (inbox / "legacy_20261119.txt").write_text(legacy_text(extra, "B000119", date(2026, 11, 19), trailer_count_delta=1))
    (inbox / "portal_20261120.csv").write_text(portal_text(portal_claims(ms, lc[0])))

    for name, c, overrides in scan_claims(ms):
        render_form(
            c,
            inbox / name,
            rng,
            hide_npi=overrides.get("provider_npi", "x") is None,
            smudge_dob="unclear_fields" in overrides,
        )
        (truth / f"{name}.json").write_text(json.dumps(answer_key(c, overrides), indent=2) + "\n")
    (root / "PLANTED.json").write_text(json.dumps(PLANTED, indent=2) + "\n")


if __name__ == "__main__":
    import sys

    generate(Path(sys.argv[1] if len(sys.argv) > 1 else "data"))
