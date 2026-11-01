"""Backend market-data refresh (Stage A). Advisory only; independent of entry eligibility.

For every TRACKED symbol (owner watchlist plus active holdings) this fetches the stock price and the put chain through
the existing Stockpile adapter (provider forced to Yahoo, same normalization and retry rule as `ticker-check`) and
persists the observations (market/observations.py). It deliberately does NOT consult: approval, the entry scan
window, reconciliation, drawdown, capacity or the entry gates, and it relaxes none of them. It writes ONLY market-data
tables (md_*): never the ledger, snapshots, audit log, outbox or Telegram (there is no send path at all).

Cadence and limits are in market/freshness.py (5 min in the regular session, 60 min outside, backoff on failure,
60 s minimum gap per symbol, per-run symbol cap, nearest-expiry cap). The Windows task fires every 5 minutes and this
job decides what is due, so a double-fired or woken task makes no extra provider calls. An owner "refresh now" request
(written by the web page) is picked up here; it bypasses only the regular interval, never backoff or the minimum gap.

Browsing window: the fetch window is wider than the 35-45 DTE entry window (BROWSE_MIN_DTE..BROWSE_MAX_DTE, widened to
cover any held contract). It is for display only: no gate reads it.
Per-symbol status: OK | PARTIAL | RATE_LIMITED | ERROR | MARKET_CLOSED. A failed fetch writes no observation, so prior
rows stay with their original timestamps (the page shows them STALE).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from csp_alerts.jobs.ticker_check import _fetch
from csp_alerts.market import failures
from csp_alerts.journal import watchlist
from csp_alerts.journal.reconcile import exchange_date
from csp_alerts.journal.store import Journal
from csp_alerts.market import calendar as mcal
from csp_alerts.market import observations as obs
from csp_alerts.market.freshness import (
    BROWSE_MAX_DTE,
    BROWSE_MIN_DTE,
    MAX_SYMBOLS_PER_RUN,
    is_due,
)
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.normalize import normalize_rows
from csp_alerts.market.stockpile import ChainSource
from csp_alerts.journal.models import from_iso
from csp_alerts.policy.config import Policy

PAUSE_BETWEEN_SYMBOLS_SECONDS = 2


@dataclass
class SymbolOutcome:
    symbol: str
    reasons: str                      # watchlist | active holding | both
    attempted: bool = False
    status: str | None = None         # OK | PARTIAL | RATE_LIMITED | ERROR | MARKET_CLOSED
    skipped: str | None = None        # why it was not attempted (not due / backoff / cap / rate limit earlier in the run)
    price: str | None = None
    contracts_written: int = 0
    contracts_retained: int = 0
    error: str | None = None


@dataclass
class RefreshResult:
    run_id: str
    started_at_utc: datetime
    session: str
    trigger: str
    status: str = "IDLE"              # OK | PARTIAL | RATE_LIMITED | ERROR | IDLE
    outcomes: list[SymbolOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _window(positions, ticker: str, today: date) -> tuple[tuple[int, int], frozenset[date]]:
    held = [p.expiration for p in positions if p.ticker == ticker and p.state in watchlist.HELD_STATES and p.expiration >= today]
    lo, hi = BROWSE_MIN_DTE, BROWSE_MAX_DTE
    if held:
        dtes = [(e - today).days for e in held]
        lo, hi = min(lo, min(dtes)), max(hi, max(dtes))
    return (max(0, lo), hi), frozenset(held)


IDLE_RECORD_EVERY_SECONDS = 3300


def _idle_heartbeat_due(journal: Journal, now_utc: datetime) -> bool:
    """An IDLE run (nothing due) is recorded at most about hourly, so the 5-minute task proves it is alive without drowning
    the real runs in the bounded run history."""
    last = obs.last_runs(journal.conn, 1)
    return not last or (now_utc - from_iso(last[0]["started_at_utc"])).total_seconds() >= IDLE_RECORD_EVERY_SECONDS


def run_market_refresh(*, journal: Journal, policy: Policy, source: ChainSource, now_utc: datetime,
                       session: SessionInfo | None = None, force: bool = False, only: str | None = None,
                       persist: bool = True, sleep: Callable[[float], None] = time.sleep,
                       pause_seconds: int = PAUSE_BETWEEN_SYMBOLS_SECONDS,
                       clock: Callable[[], datetime] | None = None) -> RefreshResult:
    session = session if session is not None else mcal.session_info(now_utc)
    sess = session.status
    run_id = str(uuid.uuid4())
    res = RefreshResult(run_id, now_utc, sess.value, "FORCED" if force else "SCHEDULED")
    today = exchange_date(now_utc)

    tracked = watchlist.tracked(journal)
    if only is not None:
        only = watchlist.normalize_symbol(only)
        tracked = [t for t in tracked if t.symbol == only]
        if not tracked:
            res.notes.append("symbol is not tracked (not on the watchlist and not an active holding): nothing fetched")
            return res
    scopes: list[str] = []
    if persist and only is None:          # a single-symbol CLI run must not consume an owner's ALL request
        scopes = obs.claim_requests(journal, run_id, now_utc)
        if scopes:
            res.trigger = "REQUEST"
    forced = {t.symbol for t in tracked} if (force or "ALL" in scopes) else {s for s in scopes}

    positions = journal.list_positions()
    due: list[tuple[datetime, watchlist.Tracked, tuple[int, int], frozenset[date]]] = []
    for t in tracked:
        st = obs.get_status(journal.conn, t.symbol)
        last_attempt = from_iso(st["last_attempt_at_utc"]) if st and st["last_attempt_at_utc"] else None
        nxt = from_iso(st["next_attempt_after_utc"]) if st and st["next_attempt_after_utc"] else None
        ok, why = is_due(now=now_utc, last_attempt=last_attempt, next_attempt_after=nxt, session=sess,
                         force=t.symbol in forced)
        o = SymbolOutcome(t.symbol, t.reason)
        if not ok:
            o.skipped = why
            res.outcomes.append(o)
            continue
        win, keep = _window(positions, t.symbol, today)
        due.append((last_attempt or datetime.min.replace(tzinfo=timezone.utc), t, win, keep))
    due.sort(key=lambda x: (x[0], x[1].symbol))          # longest-waiting first, so a cap never starves a symbol
    for extra in due[MAX_SYMBOLS_PER_RUN:]:
        res.outcomes.append(SymbolOutcome(extra[1].symbol, extra[1].reason, skipped="per-run symbol cap"))
    due = due[:MAX_SYMBOLS_PER_RUN]

    rate_limited = False
    for i, (_, t, win, keep) in enumerate(due):
        o = SymbolOutcome(t.symbol, t.reason)
        res.outcomes.append(o)
        if rate_limited:
            o.skipped = "deferred: provider rate limit earlier in this run"
            continue
        if i:
            sleep(pause_seconds)
        o.attempted = True
        raw, err = _fetch(source, t.symbol, policy, sleep, win)
        if raw is None:
            kind = getattr(err, "kind", "FETCH_ERROR")
            o.status = "RATE_LIMITED" if kind == "RATE_LIMIT" else "ERROR"
            o.error = failures.stored(failures.classify(kind, err))   # canned text only; raw text is never stored
            rate_limited = o.status == "RATE_LIMITED"
            if persist:
                obs.record_attempt(journal, t.symbol, now=max(now_utc, clock()) if clock else now_utc, status=o.status,
                                   error=o.error, session=sess.value, run_id=run_id, success=False)
            continue
        try:
            norm = normalize_rows(t.symbol, raw.rows, raw.fetched_at_utc, today_exchange=today, earnings_dates=None,
                                  multiplier=None, rows_seen_upstream=raw.rows_seen_upstream)
            detail: dict = {"rows_kept": len(norm.rows), "calls_ignored": norm.calls_ignored, "rejected": norm.rejected,
                            "degraded": norm.degraded, "window_dte": list(win)}
            if not raw.rows or not norm.rows:
                o.status = "PARTIAL"
                o.error = failures.stored(failures.NO_OPTIONS_LISTED)
                if persist:
                    obs.record_attempt(journal, t.symbol, now=max(now_utc, raw.fetched_at_utc), status=o.status, error=o.error,
                                       session=sess.value, run_id=run_id, success=False, detail=detail)
                continue
            if persist:
                sr = obs.store_chain(journal, symbol=t.symbol, run_id=run_id, source="REFRESH", retrieved_at=raw.fetched_at_utc,
                                     norm=norm, keep_expiries=keep)
            else:
                price, _note, _mt = obs.stock_price_from(norm.rows)
                sr = obs.StoreResult(t.symbol, stock_written=price is not None, stock_price=price, options_written=len(norm.rows))
            o.price = None if sr.stock_price is None else format(sr.stock_price, "f")
            o.contracts_written, o.contracts_retained = sr.options_written, sr.options_retained
            detail.update(contracts_written=sr.options_written, contracts_retained=sr.options_retained,
                          dropped_by_expiry_cap=sr.options_dropped_by_cap, stock_stored=sr.stock_written)
            notes = []
            if not sr.stock_written:
                notes.append(sr.stock_note or "no underlying price")
            if norm.degraded:
                notes.append("chain mostly unquoted (zero/missing bid and ask)" + ("; prior usable quotes retained" if sr.options_retained else ""))
            if sess is SessionStatus.UNKNOWN:
                notes.append("exchange calendar could not say whether the market is open")
            if not sr.stock_written or (sess is SessionStatus.UNKNOWN):
                o.status = "PARTIAL"
            elif sess is SessionStatus.CLOSED:
                o.status = "MARKET_CLOSED"
            elif norm.degraded:
                o.status = "PARTIAL"
            else:
                o.status = "OK"
            o.error = failures.stored(failures.PARTIAL_DATA) if notes else None
            if persist:
                obs.record_attempt(journal, t.symbol, now=max(now_utc, raw.fetched_at_utc), status=o.status, error=o.error,
                                   session=sess.value, run_id=run_id, success=bool(sr.stock_written or sr.options_written), detail=detail)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the others; type name only (no payload)
            o.status, o.error = "ERROR", failures.stored(failures.PROVIDER_FORMAT_ERROR)
            if persist:
                obs.record_attempt(journal, t.symbol, now=now_utc, status="ERROR", error=o.error, session=sess.value,
                                   run_id=run_id, success=False)

    attempted = [o for o in res.outcomes if o.attempted]
    if not attempted:
        res.status = "IDLE"
    elif any(o.status == "RATE_LIMITED" for o in attempted):
        res.status = "RATE_LIMITED"
    elif all(o.status == "ERROR" for o in attempted):
        res.status = "ERROR"
    elif any(o.status in ("ERROR", "PARTIAL") for o in attempted):
        res.status = "PARTIAL"
    else:
        res.status = "OK"
    if persist and (attempted or _idle_heartbeat_due(journal, now_utc)):
        obs.record_run(journal, run_id, now_utc, max(now_utc, clock()) if clock else now_utc, res.trigger, sess.value,
                       res.status, [o.symbol for o in attempted],
                       {"attempted": len(attempted), "skipped": {o.symbol: o.skipped for o in res.outcomes if o.skipped},
                        "results": {o.symbol: {"status": o.status, "contracts": o.contracts_written, "retained": o.contracts_retained,
                                               "error": o.error} for o in attempted}})
    return res
