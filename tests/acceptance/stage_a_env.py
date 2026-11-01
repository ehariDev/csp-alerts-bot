"""Stage A acceptance environment helpers (tester-owned). Synthetic only."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from acc_helpers import NOW
from stage_a_helpers import Dash, jopen  # noqa: F401  (re-exported)
from web_helpers import Dash as _Dash

from csp_alerts.journal.store import Journal


def plain_db(tmp: Path, name: str = "j.sqlite3", now: datetime = NOW) -> Path:
    """Migrated, EMPTY journal: no baseline, no snapshot (ledger stale), no positions."""
    db = tmp / name
    j = Journal(db, clock=lambda: now)
    j.migrate()
    j.close()
    return db


def approved_file(tmp: Path, tickers=(), **over) -> Path:
    from s3_helpers import write_approved
    return write_approved(tmp / "approved.json", tuple(tickers), **over)


def dash(db: Path, approved: Path, now: datetime = NOW) -> _Dash:
    d = _Dash(db, approved, now=now)
    assert d.login() == 303
    return d


def mkt(d: _Dash, sym: str) -> dict:
    return d.api(f"/api/market/{sym}")["data"]


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
