"""SQLite cache with per-class TTLs.

yfinance rate-limits bursts aggressively, and a weekly advisory run re-requests
mostly-unchanged data. Caching is therefore about reliability first and speed
second: a cached value keeps a run working when the upstream starts refusing.

Cached entries record their original fetch time, so provenance stays honest —
a stale-but-served value says so rather than claiming to be live.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mybroker.config import CACHE_DB, ensure_dirs

# Seconds. Quotes go stale fast; company fundamentals barely move week to week.
TTL = {
    "quote": 15 * 60,
    "history": 24 * 60 * 60,
    "fundamentals": 7 * 24 * 60 * 60,
    "nav": 24 * 60 * 60,
    "index": 60 * 60,
    "screener_ratios": 24 * 60 * 60,
    "analyst_consensus": 24 * 60 * 60,
}
DEFAULT_TTL = 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_kind ON cache(kind);
"""


class Cache:
    def __init__(self, path: Path | None = None) -> None:
        ensure_dirs()
        self.path = path or CACHE_DB
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _key(kind: str, ident: str) -> str:
        return f"{kind}:{ident}"

    def get(self, kind: str, ident: str) -> tuple[Any, float] | None:
        """Return (payload, age_seconds) if a fresh entry exists, else None."""
        ttl = TTL.get(kind, DEFAULT_TTL)
        with self._conn() as c:
            row = c.execute(
                "SELECT payload, fetched_at FROM cache WHERE key = ?",
                (self._key(kind, ident),),
            ).fetchone()

        if row is None:
            return None

        payload_json, fetched_at = row
        age = time.time() - fetched_at
        if age > ttl:
            return None
        try:
            return json.loads(payload_json), age
        except json.JSONDecodeError:
            return None

    def get_stale(self, kind: str, ident: str) -> tuple[Any, float] | None:
        """Return an entry regardless of age.

        Used as a last resort when the upstream fails: serving a value that
        is honestly labelled stale beats failing the whole run.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT payload, fetched_at FROM cache WHERE key = ?",
                (self._key(kind, ident),),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0]), time.time() - row[1]
        except json.JSONDecodeError:
            return None

    def put(self, kind: str, ident: str, payload: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO cache (key, kind, payload, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (self._key(kind, ident), kind, json.dumps(payload), time.time()),
            )

    def clear(self, kind: str | None = None) -> int:
        with self._conn() as c:
            cur = (
                c.execute("DELETE FROM cache WHERE kind = ?", (kind,))
                if kind
                else c.execute("DELETE FROM cache")
            )
            return cur.rowcount

    def stats(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT kind, COUNT(*) FROM cache GROUP BY kind"
            ).fetchall()
        return dict(rows)
