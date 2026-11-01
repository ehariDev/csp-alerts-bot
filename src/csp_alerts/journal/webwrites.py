"""The ONLY database write path of the local web dashboard (Stage A), deliberately tiny and separate.

Operations: add a watchlist symbol, remove a watchlist symbol, queue a market-data refresh request (Stage A), and RECORD an
owner-reported trade / account check-in (Stage B1, `record_web_trade`, which delegates all rules to `journal.webtrade`) and
an owner-confirmed label correction (Stage B2, `correct_labels`, `journal.weblabels`).
Each opens its own short-lived connection, validates input, writes in one transaction with an audit event, and returns a
stable result code (never reflected user text). Callers (the HTTP layer) have ALREADY enforced authentication, CSRF and
Origin/Host checks. This module cannot touch positions, snapshots, approvals, policy, the outbox or Telegram, performs no
network I/O, and never creates or migrates the journal: a missing file or a journal without the Stage A tables returns
`unavailable`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from csp_alerts.journal import watchlist, weblabels, webtrade
from csp_alerts.journal.store import Journal
from csp_alerts.market import observations as obs

ACTOR = "web:owner"
MESSAGES = {
    "added": "Added to the watchlist. Prices appear after the next market-refresh run.",
    "already": "Already on the watchlist; nothing changed.",
    "removed": "Removed from the watchlist. Stored prices are kept; an active holding stays tracked and monitored.",
    "not_tracked": "That symbol was not on the watchlist; nothing changed.",
    "invalid": "Rejected: not a valid ticker symbol (letters, digits, '.' or '-'; first character a letter; at most 10 characters).",
    "full": "Rejected: the watchlist is full (provider rate limits). Remove a symbol first.",
    "queued": "Refresh requested. The next scheduled market-refresh run picks it up (within about 5 minutes); this page does not fetch.",
    "pending": "A refresh request is already waiting for the next market-refresh run.",
    "rate": "Refresh requests are rate limited (one per minute, ten per hour). Try again shortly.",
    "unknown_symbol": "Refresh not requested: that symbol is not tracked.",
    "unavailable": "The journal is missing or has not been upgraded for the watchlist (run `csp journal init`; a backup is taken first).",
    "bad_request": "Bad request.",
}


@dataclass(frozen=True)
class WriteResult:
    code: str
    symbol: str | None = None

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, MESSAGES["bad_request"])

    @property
    def ok(self) -> bool:
        return self.code in ("added", "removed", "queued")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open(db_path: Path | str, clock: Callable[[], datetime]) -> Journal | None:
    p = Path(db_path)
    if not p.is_file():
        return None
    j = Journal(p, clock=clock)
    if not watchlist.has_table(j) or not obs.has_tables(j.conn):
        j.close()
        return None
    return j


def add_watchlist_symbol(db_path: Path | str, raw_symbol: object, *, clock: Callable[[], datetime] = _now) -> WriteResult:
    j = _open(db_path, clock)
    if j is None:
        return WriteResult("unavailable")
    try:
        sym, created = watchlist.add_symbol(j, raw_symbol, actor=ACTOR, source="web")
        return WriteResult("added" if created else "already", sym)
    except watchlist.WatchlistError as exc:
        return WriteResult(exc.code)
    finally:
        j.close()


def remove_watchlist_symbol(db_path: Path | str, raw_symbol: object, *, clock: Callable[[], datetime] = _now) -> WriteResult:
    j = _open(db_path, clock)
    if j is None:
        return WriteResult("unavailable")
    try:
        sym, removed = watchlist.remove_symbol(j, raw_symbol, actor=ACTOR, source="web")
        return WriteResult("removed" if removed else "not_tracked", sym)
    except watchlist.WatchlistError as exc:
        return WriteResult(exc.code)
    finally:
        j.close()


def request_market_refresh(db_path: Path | str, raw_scope: object, *, clock: Callable[[], datetime] = _now) -> WriteResult:
    """Queue a refresh REQUEST row. Never fetches: the market-refresh job (a separate process) does the network I/O."""
    j = _open(db_path, clock)
    if j is None:
        return WriteResult("unavailable")
    try:
        scope = "ALL"
        if raw_scope not in (None, "", "ALL"):
            scope = watchlist.normalize_symbol(raw_scope)
        code, _rid = obs.request_refresh(j, scope, ACTOR, {t.symbol for t in watchlist.tracked(j)})
        return WriteResult(code, None if scope == "ALL" else scope)
    except watchlist.WatchlistError as exc:
        return WriteResult(exc.code)
    finally:
        j.close()


def record_web_trade(db_path: Path | str, policy, approved, form, *, tz: str, bound_revision: int, bound_policy_sha: str,
                     reason: str, acks: frozenset[str], clock: Callable[[], datetime] = _now) -> webtrade.TradeResult:
    """Stage B1: record one owner-reported trade or check-in in ONE transaction (or nothing). The caller has already enforced
    authentication, CSRF, Origin/Host and verified the signed preview token; this function re-validates inside the
    transaction, rejects a stale ledger revision, and is idempotent per idempotency key. Never creates or migrates the
    journal. The result carries a code and a fixed message only (never exception text)."""
    j = _open(db_path, clock)
    if j is None:
        return webtrade.TradeResult("unavailable")
    try:
        return webtrade.record(j, policy, approved, form, now=clock(), tz=tz, bound_revision=bound_revision,
                               bound_policy_sha=bound_policy_sha, reason=reason, acks=acks)
    finally:
        j.close()


def correct_labels(db_path: Path | str, policy, approved, form, *, bound_revision: int, bound_policy_sha: str, reason: str,
                   acks: frozenset[str], clock: Callable[[], datetime] = _now) -> weblabels.LabelResult:
    """Stage B2: record one owner-confirmed sector / correlation-bucket label correction (audited) in ONE transaction, or nothing.
    Caller has enforced authentication, CSRF, Origin/Host and the signed preview token. Changes labels on held rows only; never
    approvals, never asset type. Never creates or migrates the journal."""
    j = _open(db_path, clock)
    if j is None:
        return weblabels.LabelResult("unavailable")
    try:
        return weblabels.record(j, policy, approved, form, now=clock(), bound_revision=bound_revision,
                                bound_policy_sha=bound_policy_sha, reason=reason, acks=acks)
    finally:
        j.close()
