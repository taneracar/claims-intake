"""SQLite state: which files were processed (idempotency), every claim's outcome, and the review queue."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .models import Claim, Issue

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  digest TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL,            -- loaded | held (batch-level error)
  detail TEXT
);
CREATE TABLE IF NOT EXISTS claims (
  claim_key TEXT PRIMARY KEY,      -- dedupe key (content), not the sender's claim id
  claim_id TEXT NOT NULL,
  source TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  run_id TEXT NOT NULL,
  decision TEXT NOT NULL,          -- accepted | review | rejected
  issues TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_key TEXT NOT NULL REFERENCES claims(claim_key),
  claim_json TEXT NOT NULL,        -- full claim incl. PHI: only for reviewers
  issues TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  reviewer TEXT,
  note TEXT,
  decided_at TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def file_seen(self, digest: str) -> bool:
        return self.db.execute("SELECT 1 FROM files WHERE digest = ?", (digest,)).fetchone() is not None

    def record_file(
        self, digest: str, name: str, source: str, run_id: str, status: str, detail: str | None = None
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO files (digest, name, source, run_id, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (digest, name, source, run_id, status, detail),
            )

    def claim_exists(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM claims WHERE claim_key = ?", (key,)).fetchone() is not None

    def record_claim(self, key: str, claim: Claim, run_id: str, decision: str, issues: list[Issue]) -> None:
        issues_json = json.dumps([i.model_dump() for i in issues])
        with self.db:
            self.db.execute(
                "INSERT INTO claims (claim_key, claim_id, source, source_ref, run_id, decision, issues) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, claim.claim_id, claim.source, claim.source_ref, run_id, decision, issues_json),
            )
            if decision == "review":
                self.db.execute(
                    "INSERT INTO review_queue (claim_key, claim_json, issues) VALUES (?, ?, ?)",
                    (key, claim.model_dump_json(), issues_json),
                )

    def pending_reviews(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM review_queue WHERE status = 'pending' ORDER BY id").fetchall()

    def decide(self, review_id: int, approve: bool, reviewer: str, note: str, corrected: Claim | None = None) -> Claim:
        row = self.db.execute("SELECT * FROM review_queue WHERE id = ?", (review_id,)).fetchone()
        if row is None or row["status"] != "pending":
            raise ValueError(f"review {review_id} is not pending")
        claim = corrected or Claim.model_validate_json(row["claim_json"])
        with self.db:
            self.db.execute(
                "UPDATE review_queue SET status = ?, reviewer = ?, note = ?, decided_at = ?, claim_json = ? WHERE id = ?",
                (
                    "approved" if approve else "rejected",
                    reviewer,
                    note,
                    datetime.now().isoformat(timespec="seconds"),
                    claim.model_dump_json(),
                    review_id,
                ),
            )
            self.db.execute(
                "UPDATE claims SET decision = ? WHERE claim_key = ?",
                ("accepted" if approve else "rejected", row["claim_key"]),
            )
        return claim

    def count(self, table: str) -> int:
        with closing(self.db.execute(f"SELECT COUNT(*) FROM {table}")) as cur:  # noqa: S608 - table name is internal
            return cur.fetchone()[0]
