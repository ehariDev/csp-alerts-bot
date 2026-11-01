"""Stage A acceptance helpers (tester-owned). SYNTHETIC data only; fake provider; loopback only; no network.

Clock: NOW = Fri 2026-10-30T15:05:02Z = 11:05 EDT = 10:05 CDT, regular session OPEN.
SAT   = Sat 2026-10-31T15:05:02Z (market closed, weekend).
Chain expiry EXP = 2026-12-04 (DTE 35 from exchange date 2026-10-30: Oct 31 = 1, Nov 30 = 31, Dec 4 = 35).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from acc_helpers import NOW, POLICY_PATH
from s2_helpers import raw_row
from web_helpers import Dash, build_book

from csp_alerts.journal.store import Journal
from csp_alerts.market.stockpile import RawChain, SourceError
from csp_alerts.policy.config import load_policy

SAT = datetime(2026, 10, 31, 15, 5, 2, tzinfo=timezone.utc)
MON_OPEN = datetime(2026, 11, 2, 15, 5, 2, tzinfo=timezone.utc)      # Monday 10:05 EST (DST ended Sun 2026-11-01) -> OPEN
EXP = date(2026, 12, 4)
POL = load_policy(POLICY_PATH)


class Prov:
    """Controllable fake ChainSource. script[ticker] = list of rows | Exception. fetched_at is per-call settable."""

    def __init__(self, fetched_at: datetime = NOW):
        self.script: dict[str, Any] = {}
        self.calls: list[tuple[str, int, int]] = []
        self.fetched_at = fetched_at

    def set(self, t: str, spot: float = 61.25, strikes=(55.0,), bid: float = 0.50, ask: float = 0.54, exp: date = EXP, **over):
        self.script[t] = [raw_row(strike=k, spot=spot, bid=bid, ask=ask, expiration=exp.isoformat(), **over) for k in strikes]

    def fail(self, t: str, kind: str = "FETCH_ERROR"):
        self.script[t] = SourceError(kind, "synthetic provider failure")

    def fetch_chain(self, ticker: str, min_dte: int, max_dte: int) -> RawChain:
        self.calls.append((ticker, min_dte, max_dte))
        v = self.script.get(ticker, [])
        if isinstance(v, Exception):
            raise v
        return RawChain(ticker, [dict(r) for r in v], self.fetched_at, None, [])

    def fetch_earnings_dates(self, ticker):
        return None


def hold(db: Path):
    """Run-state snapshot of every non-market table (full row content), to prove nothing else changed."""
    c = sqlite3.connect(db)
    out = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        if t.startswith("md_") or t == "watchlist":
            continue
        out[t] = sorted(tuple(map(str, r)) for r in c.execute(f"SELECT * FROM {t}"))
    c.close()
    return out


def md_dump(db: Path):
    c = sqlite3.connect(db)
    out = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'md_%' OR name='watchlist')").fetchall():
        out[t] = sorted(tuple(map(str, r)) for r in c.execute(f"SELECT * FROM {t}"))
    c.close()
    return out


def jopen(db: Path, now: datetime = NOW) -> Journal:
    return Journal(db, clock=lambda: now)


def refresh(db: Path, prov: Prov, now: datetime, **kw):
    from csp_alerts.jobs.market_refresh import run_market_refresh
    j = jopen(db, now)
    try:
        return run_market_refresh(journal=j, policy=POL, source=prov, now_utc=now, sleep=lambda s: None, pause_seconds=0, **kw)
    finally:
        j.close()


def add_wl(db: Path, *symbols: str, now: datetime = NOW):
    from csp_alerts.journal import watchlist
    j = jopen(db, now)
    for s in symbols:
        watchlist.add_symbol(j, s, actor="test", source="test")
    j.close()


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf"\s+value="([^"]+)"', html) or re.search(r'value="([^"]+)"\s+name="csrf"', html)
    assert m, "no csrf field in page"
    return m.group(1)
