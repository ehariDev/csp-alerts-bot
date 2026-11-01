"""Read-only journal access. Opens SQLite with mode=ro, PRAGMA query_only and an authorizer that only allows reads,
so a bug anywhere in the dashboard still cannot write. One snapshot() = one consistent read transaction."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from csp_alerts.journal.store import Journal, JournalError

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE,
            sqlite3.SQLITE_TRANSACTION}


def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


class JournalMissing(RuntimeError):
    """The journal file does not exist (the dashboard never creates it)."""


class ReadOnlyJournal(Journal):
    """Journal API (load_book, list_*, last_run, ...) over a read-only connection. Every write path goes through
    tx()/migrate(), which are disabled here."""

    def __init__(self, path: Path | str):
        p = Path(path)
        if not p.is_file():
            raise JournalMissing("journal file not found")
        self.path = str(p)
        self.clock = lambda: datetime.now(timezone.utc)
        self.conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.set_authorizer(_authorizer)

    def last_run(self, kind, statuses=None):  # type: ignore[override]
        """None when run_status does not exist (journal one migration behind): 'not recorded', never an error."""
        try:
            return super().last_run(kind, statuses)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise

    def tx(self):  # type: ignore[override]
        raise JournalError("read-only dashboard connection")

    def migrate(self):  # type: ignore[override]
        raise JournalError("read-only dashboard connection")

    @contextmanager
    def snapshot(self) -> Iterator["ReadOnlyJournal"]:
        """Everything read inside the block sees one committed state (SQLite snapshot isolation in WAL mode)."""
        self.conn.execute("BEGIN")
        try:
            yield self
        finally:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
