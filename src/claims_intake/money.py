from __future__ import annotations

import re

_MONEY = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")


def parse_money(raw: str) -> int:
    """'$1,234.5' -> 123450 cents, without floats."""
    s = raw.strip().replace("$", "").replace(",", "")
    m = _MONEY.fullmatch(s)
    if not m:
        raise ValueError(f"not a money amount: {raw!r}")
    return int(m.group(1)) * 100 + int((m.group(2) or "").ljust(2, "0"))


def parse_implied_decimal(raw: str) -> int:
    """Mainframe style: '00012345' means $123.45, so the digits already are cents."""
    s = raw.strip()
    if not s.isdigit():
        raise ValueError(f"not an implied-decimal amount: {raw!r}")
    return int(s)


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"
