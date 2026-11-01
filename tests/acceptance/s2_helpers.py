"""Slice-2 acceptance helpers (tester-owned). Synthetic data only; no network.

Frozen clock: NOW = 2026-10-30T15:05:02Z = 11:05 EDT = 10:05 CDT, a regular full session (closes 20:00Z, 294 min left).
Row dates: expiry 2026-12-04 is DTE 35 from the exchange date 2026-10-30 (hand count: Oct 31 =1, Nov 30 = 31, Dec 4 = 35).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from typing import Any

from acc_helpers import NOW, TODAY, approval as _approval

from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.store import Journal
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import RawChain, SourceError

OPEN_SESSION = SessionInfo(SessionStatus.OPEN, 294)
EXP35 = date(2026, 12, 4)


def ms(dt: datetime) -> float:
    """Epoch milliseconds as a float, the way pandas/Stockpile delivers `last_trade_ms`."""
    return float(int(dt.timestamp() * 1000))


def raw_row(now: datetime = NOW, **over: Any) -> dict[str, Any]:
    """One Stockpile-shaped put row (floats as upstream gives them). Base = spec fixture: strike 75, spot 80,
    bid .50 / ask .54, delta -0.13, OI 1000, vol 100, last print 25 min before `now`."""
    d: dict[str, Any] = {
        "type": "put", "strike": 75.0, "expiration": EXP35.isoformat(), "dte": 35, "spot": 80.0,
        "bid": 0.5, "ask": 0.54, "mid": 0.52, "last": 0.52, "iv": 0.24, "iv_excess": 0.0, "delta": -0.13,
        "open_interest": 1000, "volume": 100, "last_trade_days": 0.017, "last_trade_ms": ms(now - timedelta(minutes=25)),
        "earnings_count": 0,
    }
    d.update(over)
    return d


class FakeSource:
    """ChainSource double. `chains[ticker]` is a list of raw rows, an Exception to raise, or a callable."""

    def __init__(self, chains: dict[str, Any], earnings: dict[str, Any] | None = None, now: datetime = NOW):
        self.chains = chains
        self.earn = earnings or {}
        self.now = now
        self.calls: list[tuple[str, int, int]] = []
        self.earn_calls: list[str] = []

    def fetch_chain(self, ticker: str, min_dte: int, max_dte: int) -> RawChain:
        self.calls.append((ticker, min_dte, max_dte))
        v = self.chains[ticker]
        if isinstance(v, Exception):
            raise v
        return RawChain(ticker, list(v), self.now, None, [])

    def fetch_earnings_dates(self, ticker: str):
        self.earn_calls.append(ticker)
        v = self.earn.get(ticker)
        if isinstance(v, Exception):
            raise v
        return v


class Boom:
    """A source that must never be touched (fail-closed ordering tests)."""

    def fetch_chain(self, *a, **k):
        raise AssertionError("network/source touched although entries are already blocked")

    fetch_earnings_dates = fetch_chain


def appr(ticker="AAA", sector=None, bucket=None, asset_type="STOCK", confirmed=True, today: date = TODAY, **over):
    kw = dict(ticker=ticker, asset_type=asset_type, sector=sector or f"sec_{ticker}", bucket=bucket or f"bk_{ticker}",
              today=today)
    if confirmed:
        over.setdefault("standard_deliverable_confirmed_on", today.isoformat())
    kw.update(over)
    return _approval(**kw)


def make_journal(now: datetime = NOW, *, equity="100000", cash="70000", baseline="100000", snap=True):
    """Journal with a reconciled single-account ledger: baseline confirmed, fresh confirmed snapshot 2h old."""
    j = Journal(":memory:", clock=lambda: now)
    j.migrate()
    if baseline is not None:
        j.confirm_baseline(D(baseline))
    if snap:
        j.add_snapshot(AccountSnapshot("s0", now - timedelta(hours=2), D(equity), D(cash), True, "v1"))
    return j


def policy_variant(**sections):
    from acc_helpers import policy
    from csp_alerts.policy.config import parse_policy
    data = policy().model_dump(mode="json")
    for sec, upd in sections.items():
        data[sec].update(upd)
    return parse_policy(data)


def count(j: Journal, table: str) -> int:
    return j.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
