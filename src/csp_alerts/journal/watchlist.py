"""Owner watchlist (WT-01): symbols the owner chose to TRACK for prices. Tracking is NOT approval.

  * A watchlist symbol shows market data only. Entry alerts come exclusively from the owner-approved whitelist
    (config/approved.json) via the scan, which never reads this table.
  * Active holdings (open/working/pending/close-working/adjustment/assignment-pending puts and owned stock lots) are
    tracked automatically whether or not they are on the watchlist and whether or not their approval has expired.
  * Every add/remove is one audit event (WATCHLIST_ADDED / WATCHLIST_REMOVED) written in the same transaction. These
    event types are NOT ledger events: they never require a new account snapshot or touch reconciliation, drawdown
    or the alert outbox.
Shared by the CLI (`watchlist add|remove|list`) and the small web write module (`journal/webwrites.py`).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from csp_alerts.journal.models import MONITORED_STATES, PositionState, to_iso
from csp_alerts.journal.store import Journal
from csp_alerts.market.freshness import MAX_WATCHLIST

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")   # same rule as ticker-check
HELD_STATES = frozenset(MONITORED_STATES) | {PositionState.PENDING, PositionState.WORKING,
                                              PositionState.ASSIGNED_PENDING_RECONCILIATION}


class WatchlistError(ValueError):
    """Rejected request. `code` is a stable, non-reflecting identifier; the message never echoes raw input."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def normalize_symbol(raw: object) -> str:
    """Strip + upper-case only; anything else that is not a plain ticker is rejected (no echo of the raw text)."""
    if not isinstance(raw, str):
        raise WatchlistError("invalid", "symbol must be text")
    s = raw.strip()
    if not s.isascii():          # validate BEFORE case folding: U+017F / U+0131 upper-case to ASCII S / I
        s = ""
    s = s.upper()
    if len(raw) > 64 or not _SYMBOL.match(s):
        raise WatchlistError("invalid", "not a valid ticker symbol (letters, digits, '.' or '-'; first character a letter; "
                                        "at most 10 characters)")
    return s


def safe_echo(raw: object, limit: int = 24) -> str:
    """Untrusted text for messages: control/format characters dropped, whitespace collapsed, truncated."""
    s = "".join(c for c in str(raw) if unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(s.split())[:limit]


@dataclass(frozen=True)
class Tracked:
    symbol: str
    on_watchlist: bool
    held: bool

    @property
    def reason(self) -> str:
        return "both" if self.on_watchlist and self.held else "watchlist" if self.on_watchlist else "active holding"


def watchlist_symbols(j: Journal) -> list[tuple[str, str, str]]:
    """[(symbol, added_at_utc, added_by)] ordered by symbol."""
    return [(r[0], r[1], r[2]) for r in j.conn.execute("SELECT symbol, added_at_utc, added_by FROM watchlist ORDER BY symbol")]


def held_symbols(j: Journal) -> set[str]:
    """Underlyings of positions that are still live obligations plus owned shares. Read-only."""
    states = [s.value for s in HELD_STATES]
    q = f"SELECT DISTINCT ticker FROM option_positions WHERE state IN ({','.join('?' * len(states))})"
    out = {r[0] for r in j.conn.execute(q, states)}
    out |= {r[0] for r in j.conn.execute("SELECT DISTINCT ticker FROM stock_lots WHERE CAST(shares AS REAL) > 0")}
    return out


def tracked(j: Journal) -> list[Tracked]:
    wl = {s for s, _, _ in watchlist_symbols(j)}
    held = held_symbols(j)
    return [Tracked(s, s in wl, s in held) for s in sorted(wl | held)]


def add_symbol(j: Journal, raw_symbol: object, *, actor: str, source: str, reason: str = "") -> tuple[str, bool]:
    """Add a symbol. Returns (symbol, created). Idempotent: an existing symbol is not re-added and not re-audited
    (a double click or a replayed form changes nothing). Raises WatchlistError for invalid input or a full list."""
    sym = normalize_symbol(raw_symbol)
    with j.tx():
        if j.conn.execute("SELECT 1 FROM watchlist WHERE symbol=?", (sym,)).fetchone():
            return sym, False
        n = j.conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        if n >= MAX_WATCHLIST:
            raise WatchlistError("full", f"the watchlist is limited to {MAX_WATCHLIST} symbols (provider rate limits)")
        now = to_iso(j.clock())
        j.conn.execute("INSERT INTO watchlist(symbol, added_at_utc, added_by) VALUES (?,?,?)", (sym, now, actor))
        j._audit("WATCHLIST_ADDED", actor, None, {"symbol": sym, "reason": safe_echo(reason, 200)}, source)
    return sym, True


def remove_symbol(j: Journal, raw_symbol: object, *, actor: str, source: str, reason: str = "") -> tuple[str, bool]:
    """Remove a symbol from the watchlist. Returns (symbol, removed). Idempotent. Stored market data is kept (history);
    an active holding stays tracked and monitored regardless."""
    sym = normalize_symbol(raw_symbol)
    with j.tx():
        row = j.conn.execute("SELECT added_at_utc FROM watchlist WHERE symbol=?", (sym,)).fetchone()
        if row is None:
            return sym, False
        j.conn.execute("DELETE FROM watchlist WHERE symbol=?", (sym,))
        j._audit("WATCHLIST_REMOVED", actor, {"symbol": sym, "added_at_utc": row[0]},
                 {"symbol": sym, "reason": safe_echo(reason, 200)}, source)
    return sym, True


def has_table(j: Journal) -> bool:
    return j.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone() is not None


def describe(t: Tracked) -> dict[str, Any]:
    return {"symbol": t.symbol, "on_watchlist": t.on_watchlist, "held": t.held, "reason": t.reason}


__all__ = ["Tracked", "WatchlistError", "add_symbol", "remove_symbol", "tracked", "held_symbols", "watchlist_symbols",
           "normalize_symbol", "safe_echo", "has_table"]
