"""Controllable fake provider + helpers for the Stage A market-data tests. ALL VALUES ARE SYNTHETIC; none are market data."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from chain_fixtures import EXP, raw_row
from factories import NOW, TODAY

from csp_alerts.journal.store import Journal
from csp_alerts.market.stockpile import RawChain, SourceError


class FakeProvider:
    """Per-ticker script. `set(ticker, rows=[...])` returns a chain next time; `fail(ticker, SourceError)` raises.
    Counts calls so tests can assert cadence and caps. Never touches the network."""

    def __init__(self):
        self.script: dict[str, object] = {}
        self.calls: list[tuple[str, int, int]] = []
        self.fetched_at = NOW

    def set(self, ticker: str, rows: list[dict] | None = None, spot: float = 48.0, strikes=(40.0, 42.0), exp: str = EXP,
            bid: float = 0.50, ask: float = 0.54, **over) -> None:
        if rows is None:
            rows = [raw_row(strike=k, spot=spot, bid=bid, ask=ask, exp=exp, **over) for k in strikes]
        self.script[ticker] = rows

    def fail(self, ticker: str, kind: str = "FETCH_ERROR", message: str = "boom") -> None:
        self.script[ticker] = SourceError(kind, message)

    def fetch_chain(self, ticker: str, min_dte: int, max_dte: int) -> RawChain:
        self.calls.append((ticker, min_dte, max_dte))
        item = self.script.get(ticker)
        if isinstance(item, Exception):
            raise item
        return RawChain(ticker, list(item or []), self.fetched_at)

    def fetch_earnings_dates(self, ticker):
        return None


def make_journal(tmp_path, name="m.sqlite3", clock_value=NOW) -> Journal:
    j = Journal(tmp_path / name, clock=lambda: clock_value)
    j.migrate()
    return j


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def dump_non_market(j: Journal) -> dict[str, list[tuple]]:
    """Every table that is NOT a market-data (md_*) / watchlist table, as sorted row tuples (bytes-level equality of content)."""
    out = {}
    for (t,) in j.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        if t.startswith("md_") or t == "watchlist":
            continue
        out[t] = sorted(tuple(r) for r in j.conn.execute(f"SELECT * FROM {t}"))
    return out


__all__ = ["FakeProvider", "make_journal", "utc", "dump_non_market", "NOW", "TODAY", "EXP", "D", "timedelta"]
