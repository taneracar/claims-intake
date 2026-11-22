"""Keeping patient data (PHI) out of logs, metrics and alerts.

Two layers:
  1. Messages are written without PHI in the first place (Issue messages, metric names).
  2. A scrubber replaces every known PHI value of the claims in flight, plus anything shaped
     like a member id, with a placeholder, as a net for mistakes in layer 1. It is attached to
     the pipeline's logger and applied to alert payloads before they leave the process.
"""

from __future__ import annotations

import logging
import re

# Same shape as codes.MEMBER_ID_RE, but anywhere in a string.
_MEMBER_ID_ANYWHERE = re.compile(r"\bM\d{8}\b")


class Scrubber:
    def __init__(self) -> None:
        self._values: set[str] = set()
        self._pattern: re.Pattern[str] | None = None

    def add(self, values: list[str]) -> None:
        new = {v for v in values if v and len(v) >= 3} - self._values
        if new:
            self._values |= new
            # Longest first so "Ana Lima" is replaced before "Ana".
            alts = sorted((re.escape(v) for v in self._values), key=len, reverse=True)
            self._pattern = re.compile("|".join(alts), re.IGNORECASE)

    def scrub(self, text: str) -> str:
        if self._pattern is not None:
            text = self._pattern.sub("[PHI]", text)
        return _MEMBER_ID_ANYWHERE.sub("[PHI]", text)


class ScrubFilter(logging.Filter):
    def __init__(self, scrubber: Scrubber) -> None:
        super().__init__()
        self.scrubber = scrubber

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrubber.scrub(record.getMessage())
        record.args = None
        return True


def mask_member_id(member_id: str) -> str:
    """For places a person needs to tell claims apart without seeing the id (e.g. CLI listings)."""
    return member_id[:1] + "*" * (len(member_id) - 3) + member_id[-2:] if len(member_id) > 3 else "***"
