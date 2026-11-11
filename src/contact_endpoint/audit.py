"""SQLite audit log of every attempt, including silent drops.

Useful for spam-pattern analysis and as a dispute trail. Client IPs are stored
as a salted hash (enough to group attempts, not to identify a person), and rows
older than ``retention_days`` are purged at start and then hourly.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger("contact_endpoint.audit")
PURGE_EVERY_S = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,
    channel      TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT,
    name         TEXT,
    email        TEXT,
    message      TEXT,
    assistant    TEXT,
    ip_hash      TEXT,
    user_agent   TEXT,
    origin       TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_received ON attempts(received_at);
CREATE INDEX IF NOT EXISTS idx_attempts_status   ON attempts(status);
"""


class AuditLog:
    def __init__(self, path: str, retention_days: int = 90, ip_salt: str | None = None):
        self.enabled = bool(path)
        self.path = Path(path) if path else None
        self.retention_days = retention_days
        self._salt = (ip_salt or secrets.token_hex(16)).encode()
        self._lock = Lock()
        self._last_purge = 0.0

    def init(self) -> None:
        if not self.enabled:
            return
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.executescript(_SCHEMA)
        self.purge()

    def purge(self) -> int:
        """Delete rows older than the retention period. Runs at start and hourly."""
        if not self.enabled:
            return 0
        self._last_purge = time.monotonic()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        try:
            with sqlite3.connect(self.path, timeout=5) as conn:  # type: ignore[arg-type]
                purged = conn.execute("DELETE FROM attempts WHERE received_at < ?", (cutoff,)).rowcount
        except sqlite3.Error as exc:
            log.error("audit purge failed: %s", exc)
            return 0
        if purged:
            log.info("audit: purged %d rows older than %d days", purged, self.retention_days)
        return purged

    def hash_ip(self, ip: str) -> str:
        return hashlib.sha256(self._salt + ip.encode()).hexdigest()[:16]

    def record(self, *, channel: str, status: str, reason: str = "", ip: str = "",
               **fields: Any) -> None:
        """Append one row. Failures are logged, never raised to the caller."""
        if not self.enabled:
            return
        if time.monotonic() - self._last_purge > PURGE_EVERY_S:
            self.purge()
        row = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "channel": channel,
            "status": status,
            "reason": reason or None,
            "name": fields.get("name"),
            "email": fields.get("email"),
            "message": fields.get("message"),
            "assistant": fields.get("assistant"),
            "ip_hash": self.hash_ip(ip) if ip else None,
            "user_agent": (fields.get("user_agent") or "")[:300] or None,
            "origin": fields.get("origin"),
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        try:
            with self._lock, sqlite3.connect(self.path, timeout=5) as conn:  # type: ignore[arg-type]
                conn.execute(f"INSERT INTO attempts ({cols}) VALUES ({marks})", tuple(row.values()))
        except sqlite3.Error as exc:
            log.error("audit write failed (%s): %s", status, exc)

    def rows(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with sqlite3.connect(self.path, timeout=5) as conn:  # type: ignore[arg-type]
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM attempts ORDER BY id")]
