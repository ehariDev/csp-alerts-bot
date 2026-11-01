"""Open-position monitor (spec 8.1, IT-005). Advisory only; independent of entry scans and of the entry gates.

Monitored states: OPEN, CLOSE_WORKING, ADJUSTMENT_REVIEW. Unresolved states (ASSIGNED_PENDING_RECONCILIATION,
PENDING, WORKING) are counted, never reported as NO_POSITIONS; a pending assignment emits an URGENT reminder each run. Calendar triggers (TIME_EXIT, ASSIGNMENT_REVIEW,
ADJUSTMENT_REVIEW notice) need no quote and fire even when the market is closed or the provider is down.
Price triggers (PROFIT_EXIT, LOSS_REVIEW, DELTA_REVIEW) use the Tier R Yahoo ask and are labelled
"INDICATIVE - quote age UNKNOWN - verify at your broker"; invalid/crossed/zero/mismatched/missing quotes
become DATA_ISSUE instead. The policy key exits.tier_r_price_alerts_enabled switches price alerts off.
The market-closed session skips the quote fetch entirely (no price checks, no DATA_ISSUE noise).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from csp_alerts.alerts import templates
from csp_alerts.journal.models import MONITORED_STATES, PositionState, PutPosition
from csp_alerts.journal.reconcile import exchange_date
from csp_alerts.journal.store import Journal
from csp_alerts.market import calendar as mcal
from csp_alerts.market import failures
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.normalize import normalize_rows, standard_symbol
from csp_alerts.market.stockpile import ChainSource, SourceError
from csp_alerts.policy.config import Policy
from csp_alerts.policy.exits import ExitSignal, OptionQuote, Trigger, evaluate_exits

_SEV = {"CRITICAL": 0, "URGENT": 1, "ACTION": 2, "WARN": 3, "INFO": 4}
UNRESOLVED_STATES = frozenset({PositionState.ASSIGNED_PENDING_RECONCILIATION, PositionState.PENDING, PositionState.WORKING})
_PRICE_TRIGGERS = {Trigger.PROFIT_EXIT, Trigger.LOSS_REVIEW, Trigger.DELTA_REVIEW}


@dataclass
class MonitorResult:
    run_id: str
    started_at_utc: datetime
    status: str  # ALERTS | NO_ALERTS | NO_POSITIONS | DATA_UNAVAILABLE
    positions_checked: int = 0  # monitored (open-risk) positions only
    unresolved: int = 0  # assigned-pending / pending / working positions on record (never reported as NO_POSITIONS)
    signals: list[tuple[PutPosition, ExitSignal]] = field(default_factory=list)
    messages: list[templates.AlertMessage] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _quote_for(pos: PutPosition, rows, fetched_at: datetime) -> OptionQuote | None:
    """Quote for the position's own contract. A contradictory row identity (contract symbol disagrees with
    right/expiration/strike/root, SR-02) or more than one matching row yields a quote carrying `identity_problem`
    and NO prices, so exits become DATA_ISSUE instead of advice from the wrong contract."""
    hits = [r for r in rows if r.observation.expiration == pos.expiration and r.observation.strike == pos.strike]
    if not hits:
        return None
    bad = None
    if len(hits) > 1:
        bad = "ambiguous: several upstream rows match this contract"
    else:
        raw = hits[0].raw
        sym = raw.get("contractSymbol") or raw.get("contract_symbol")
        if standard_symbol(sym, pos.ticker, pos.expiration, pos.strike) is False:
            bad = "contract identity conflict: upstream symbol does not match this put"
    o = hits[0].observation
    if bad:
        return OptionQuote(contract_key=pos.contract_key, retrieved_at_utc=fetched_at, identity_problem=bad)
    return OptionQuote(contract_key=pos.contract_key, retrieved_at_utc=fetched_at, bid=o.bid, ask=o.ask,
                       delta=o.delta, spot=o.spot)


def _persist_observations(journal: Journal, res: "MonitorResult", ticker: str, plist, norm, fetched_at: datetime) -> None:
    """Persist the fresh quotes this monitor run fetched into the shared market-data store so the dashboard can show them
    (MD-AT-04). Writes market-data tables only. The exit gates above read the in-memory quotes, not this store, and a
    storage problem can never stop or alter monitoring."""
    from csp_alerts.market import observations as obs
    try:
        if not obs.has_tables(journal.conn):
            return
        obs.store_chain(journal, symbol=ticker, run_id=res.run_id, source="MONITOR", retrieved_at=fetched_at, norm=norm,
                        keep_expiries=frozenset(p.expiration for p in plist))
        obs.touch_success(journal, ticker, fetched_at, res.run_id)
    except Exception:  # noqa: BLE001
        res.notes.append(f"{ticker}: observations not persisted (storage error); monitoring unaffected")


def _record_fetch_failure(journal: Journal, res: "MonitorResult", ticker: str, reason: str, at: datetime) -> None:
    """Make the monitor's own fetch error visible to the dashboard (market-data bookkeeping only; storage errors never stop
    monitoring)."""
    from csp_alerts.market import observations as obs
    try:
        if obs.has_tables(journal.conn):
            obs.record_monitor_fetch_failure(journal, ticker, at, reason)
    except Exception:  # noqa: BLE001
        res.notes.append(f"{ticker}: fetch failure not recorded for the dashboard (storage error); monitoring unaffected")


def run_monitor(*, journal: Journal, policy: Policy, source: ChainSource, now_utc: datetime,
                session: SessionInfo | None = None, tz: str = "America/Chicago") -> MonitorResult:
    res = MonitorResult(run_id=str(uuid.uuid4()), started_at_utc=now_utc, status="NO_POSITIONS")
    book = journal.load_book()
    snap = book.latest_snapshot
    snap_asof = snap.asof_utc if snap else None
    positions = [p for p in book.positions if p.state in MONITORED_STATES]
    # SR-03: unresolved obligations stay visible. They are never "no positions", whatever the market session.
    unresolved = [p for p in book.positions if p.state in UNRESOLVED_STATES]
    res.positions_checked = len(positions)
    res.unresolved = len(unresolved)
    for p in unresolved:
        if p.state is PositionState.ASSIGNED_PENDING_RECONCILIATION:
            res.signals.append((p, ExitSignal(Trigger.ASSIGNMENT_REVIEW, "URGENT", p.trade_id,
                                              {"pending_reconciliation": True})))
            res.messages.append(templates.unresolved_assignment_alert(p, now_utc, policy.account_scope, tz))
        else:
            res.notes.append(f"{p.trade_id}: {p.state.value} order on record (collateral ${p.collateral_usd:,.2f} reserved); "
                             "confirm fill/cancel in the journal")
    if unresolved:
        res.status = "ALERTS" if any(p.state is PositionState.ASSIGNED_PENDING_RECONCILIATION for p in unresolved) else "NO_ALERTS"
    if not positions:
        res.messages.sort(key=lambda m: _SEV.get(m.severity, 9))
        return res
    session = session if session is not None else mcal.session_info(now_utc)
    today = exchange_date(now_utc)
    want_quotes = policy.exits.tier_r_price_alerts_enabled and session.status is SessionStatus.OPEN
    if session.status is SessionStatus.UNKNOWN:
        res.notes.append("calendar unavailable: price checks skipped; calendar alerts still evaluated")
    elif session.status is SessionStatus.CLOSED:
        res.notes.append("market closed: price checks skipped; calendar alerts still evaluated")

    quotes: dict[str, OptionQuote | None] = {}
    fetch_failed: dict[str, str] = {}
    fetched_at: dict[str, datetime] = {}
    if want_quotes:
        by_ticker: dict[str, list[PutPosition]] = {}
        for p in positions:
            if (p.expiration - today).days >= 0:
                by_ticker.setdefault(p.ticker, []).append(p)
        for ticker, plist in by_ticker.items():
            dtes = [(p.expiration - today).days for p in plist]
            try:
                raw = source.fetch_chain(ticker, min(dtes), max(dtes))
            except Exception as exc:  # noqa: BLE001 - any failure, however hostile, becomes a canned code for THIS ticker only
                fetch_failed[ticker] = failures.stored_for(exc)
                _record_fetch_failure(journal, res, ticker, fetch_failed[ticker], now_utc)
                continue
            fetched_at[ticker] = raw.fetched_at_utc
            norm = normalize_rows(ticker, raw.rows, raw.fetched_at_utc, today_exchange=today, earnings_dates=None,
                                  multiplier=None)
            for p in plist:
                quotes[p.trade_id] = _quote_for(p, norm.rows, raw.fetched_at_utc)
            _persist_observations(journal, res, ticker, plist, norm, raw.fetched_at_utc)

    for p in positions:
        q = quotes.get(p.trade_id)
        # the quote is fetched AFTER the job started; evaluate at the later of the two so a fresh quote is never 'from the future'
        sigs = evaluate_exits(p, q, policy, max(now_utc, fetched_at.get(p.ticker, now_utc)))
        if not want_quotes and policy.exits.tier_r_price_alerts_enabled:
            # market closed/unknown: evaluate_exits would flag "no quote"; keep only calendar-based signals
            sigs = [s for s in sigs if s.trigger not in _PRICE_TRIGGERS and s.trigger is not Trigger.DATA_ISSUE]
        if p.ticker in fetch_failed:
            res.issues.append(f"{p.ticker}: {fetch_failed[p.ticker]}")
        for s in sigs:
            res.signals.append((p, s))
            res.messages.append(templates.exit_alert(s, p, q, policy, now_utc, snap_asof, policy.account_scope, tz))
    res.messages.sort(key=lambda m: _SEV.get(m.severity, 9))
    real = [x for _, x in res.signals if x.trigger is not Trigger.DATA_ISSUE]
    data_issue = bool(res.issues) or any(x.trigger is Trigger.DATA_ISSUE for _, x in res.signals)
    res.status = "ALERTS" if real else "DATA_UNAVAILABLE" if data_issue else "NO_ALERTS"
    if real and data_issue:
        res.notes.append("some positions also had data issues (see DATA_ISSUE alerts)")
    return res
