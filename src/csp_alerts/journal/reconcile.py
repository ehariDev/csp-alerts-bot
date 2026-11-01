"""Reconciliation basics (spec 4.2, 5.2, 5.3). Pure checks over a BookState.

Reconciliation is manual by design: this module only reports what the owner must fix.
It never mutates the journal and never contacts any broker.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from csp_alerts.journal.models import (
    COLLATERAL_STATES,
    BookState,
    PositionState,
    ReconcileIssue,
    ReconcileReport,
)
from csp_alerts.policy.config import Policy

NY = ZoneInfo("America/New_York")


def exchange_date(now_utc: datetime) -> date:
    return now_utc.astimezone(NY).date()


FUTURE_TOLERANCE = timedelta(minutes=5)  # same tolerance Journal.add_snapshot applies to the owner-entered as-of time


def _prev_trading_close(today: date) -> datetime:
    """Close (UTC) of the latest regular session strictly before `today` (exchange date).
    Falls back to 16:00 New York on the previous weekday when the calendar cannot answer."""
    from csp_alerts.market import calendar as mcal

    d = today
    for _ in range(14):
        d -= timedelta(days=1)
        if mcal.is_trading_day(d) is True:
            try:
                import pandas as pd
                return mcal._calendar().session_close(pd.Timestamp(d)).to_pydatetime().astimezone(NY)
            except Exception:  # noqa: BLE001
                break
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime.combine(d, time(16, 0), tzinfo=NY)


def reconcile(book: BookState, policy: Policy, now_utc: datetime) -> ReconcileReport:
    issues: list[ReconcileIssue] = []
    add = issues.append
    today = exchange_date(now_utc)

    snap = book.latest_snapshot
    if snap is None:
        add(ReconcileIssue("NO_SNAPSHOT", "LEDGER_STALE", "BLOCK", "no account snapshot recorded"))
    else:
        age = now_utc - snap.asof_utc
        if age > timedelta(hours=policy.manual_data.max_account_snapshot_age_hours):
            add(ReconcileIssue("SNAPSHOT_STALE", "LEDGER_STALE", "BLOCK",
                               f"latest snapshot is {age.total_seconds() / 3600:.1f}h old", snap.snapshot_id))
        if not snap.manual_confirmed:
            add(ReconcileIssue("SNAPSHOT_UNCONFIRMED", "LEDGER_UNRECONCILED", "BLOCK",
                               "latest snapshot is not manually confirmed", snap.snapshot_id))
        if age < -FUTURE_TOLERANCE:
            add(ReconcileIssue("SNAPSHOT_FUTURE", "LEDGER_STALE", "BLOCK",
                               "latest snapshot is dated in the future", snap.snapshot_id))
        # SR-10: the required daily/EOD check-in. On a trading morning the snapshot must have been taken at/after the
        # close of the previous session; a snapshot from inside that session has no end-of-day acknowledgment.
        required_after = _prev_trading_close(today)
        if snap.asof_utc < required_after:
            add(ReconcileIssue("DAILY_CHECKIN_MISSING", "LEDGER_STALE", "BLOCK",
                               f"no snapshot since the previous session close ({required_after.astimezone(NY):%Y-%m-%d %H:%M %Z}); "
                               "run daily-check-in (required end-of-day / morning acknowledgment)", snap.snapshot_id))
        ch = book.last_ledger_change_utc
        # SR-07: same-second ties are broken by journal sequence (a change recorded AFTER the snapshot row)
        if ch is not None and (snap.asof_utc < ch or (snap.asof_utc == ch and snap.seq > 0
                                                       and book.last_ledger_change_seq > snap.seq)):
            add(ReconcileIssue("CHANGE_AFTER_SNAPSHOT", "LEDGER_UNRECONCILED", "BLOCK",
                               "a trade/position change was recorded after the latest snapshot; re-confirm equity and cash",
                               snap.snapshot_id))

    if book.baseline_equity_usd is None and book.last_risk_reset_utc is None:
        add(ReconcileIssue("NO_BASELINE", "LEDGER_UNRECONCILED", "BLOCK",
                           "owner has not confirmed the starting equity baseline (journal confirm-baseline)"))

    if snap is not None:
        committed = sum((p.collateral_usd for p in book.positions if p.state in COLLATERAL_STATES), Decimal("0"))
        if snap.free_unencumbered_cash_usd + committed > snap.equity_usd:
            add(ReconcileIssue("CASH_INCONSISTENT", "LEDGER_UNRECONCILED", "BLOCK",
                               "free cash + active put collateral exceeds equity; enter NET free unencumbered cash "
                               "(after reserving collateral for existing puts)", snap.snapshot_id))

    for p in book.positions:
        if p.account_scope != policy.account_scope:
            add(ReconcileIssue("MIXED_ACCOUNT_SCOPE", "LEDGER_UNRECONCILED", "BLOCK",
                               f"position scope {p.account_scope!r} != configured {policy.account_scope!r} "
                               "(MVP supports one account scope)", p.trade_id))

    for p in book.positions:
        if not p.paper and policy.account_scope.startswith("paper"):  # SR-09
            add(ReconcileIssue("REAL_POSITION_IN_PAPER_SCOPE", "LEDGER_UNRECONCILED", "BLOCK",
                               "position is flagged paper=False but the initial delivery is paper-only "
                               f"(scope {policy.account_scope!r}); correct the record", p.trade_id))

    for p in book.positions:
        if p.contracts != 1 and p.state in COLLATERAL_STATES:
            add(ReconcileIssue("MULTI_CONTRACT_POSITION", "LEDGER_UNRECONCILED", "BLOCK",
                               f"{p.contracts} contracts recorded; the MVP supports 1 contract per position. "
                               "Resolve with your broker records, then correct the journal", p.trade_id))

    max_mark = timedelta(hours=policy.manual_data.max_stock_mark_age_hours)
    for lot in book.stock_lots:
        if now_utc - lot.reference_asof_utc > max_mark:
            add(ReconcileIssue("STOCK_MARK_STALE", "LEDGER_STALE", "BLOCK", "owned stock mark is too old", lot.lot_id))
        if lot.reference_asof_utc - now_utc > FUTURE_TOLERANCE:  # SR-08
            add(ReconcileIssue("STOCK_MARK_FUTURE", "LEDGER_UNRECONCILED", "BLOCK",
                               "owned stock mark is dated in the future", lot.lot_id))

    active_by_ticker: dict[str, list[str]] = {}
    for p in book.positions:
        if p.state is PositionState.ASSIGNED_PENDING_RECONCILIATION:
            add(ReconcileIssue("ASSIGNMENT_PENDING", "LEDGER_UNRECONCILED", "BLOCK",
                               "assignment awaiting owner reconciliation of shares and cash", p.trade_id))
        if p.state is PositionState.ADJUSTMENT_REVIEW:
            add(ReconcileIssue("ADJUSTMENT_REVIEW_PENDING", "LEDGER_UNRECONCILED", "BLOCK",
                               "split/merger/non-standard contract under owner review", p.trade_id))
        if p.state in (PositionState.OPEN, PositionState.CLOSE_WORKING) and p.expiration < today:
            add(ReconcileIssue("EXPIRY_UNCONFIRMED", "LEDGER_UNRECONCILED", "BLOCK",
                               "expiration date has passed; confirm expired, closed, or assigned", p.trade_id))
        if p.state is PositionState.ASSIGNED_RECONCILED and not any(l.linked_trade_id == p.trade_id for l in book.stock_lots):
            add(ReconcileIssue("ASSIGNED_WITHOUT_LOT", "LEDGER_UNRECONCILED", "BLOCK",
                               "assigned put has no linked stock lot", p.trade_id))
        if p.state in COLLATERAL_STATES:
            active_by_ticker.setdefault(p.ticker, []).append(p.trade_id)
        if p.state in (PositionState.OPEN, PositionState.CLOSE_WORKING) and (
            p.actual_open_credit_per_share is None or p.opened_at_utc is None
        ):
            add(ReconcileIssue("OPEN_WITHOUT_FILL", "LEDGER_UNRECONCILED", "BLOCK",
                               "open position lacks actual fill credit/time", p.trade_id))

    state_by_trade = {p.trade_id: p.state for p in book.positions}
    for lot in book.stock_lots:
        if lot.linked_trade_id is not None and state_by_trade.get(lot.linked_trade_id) in COLLATERAL_STATES:
            add(ReconcileIssue("DOUBLE_COUNT_RISK", "LEDGER_UNRECONCILED", "BLOCK",
                               "stock lot is linked to a put that still reserves collateral", lot.lot_id))

    # An owner-reported execution that breached a policy rule is recorded as it happened (never rejected); the exposure is
    # real, so new entries stay blocked until that position is no longer reserving collateral (closed/expired/canceled).
    live_state = {p.trade_id: p.state for p in book.positions}
    for trade_id, codes in book.policy_breaches:
        if live_state.get(trade_id) in COLLATERAL_STATES:
            add(ReconcileIssue("POLICY_BREACH_UNRESOLVED", "LEDGER_UNRECONCILED", "BLOCK",
                               "owner-reported trade breaches policy (" + ", ".join(codes) + "); new entries stay blocked while it reserves "
                               "collateral and the breach still reproduces (it is re-checked automatically)", trade_id))

    for ticker, ids in active_by_ticker.items():
        if len(ids) > policy.portfolio.max_contracts_per_underlying:
            add(ReconcileIssue("MULTIPLE_ACTIVE_PUTS_SAME_UNDERLYING", "LEDGER_UNRECONCILED", "WARN",
                               f"{ticker} has {len(ids)} active puts (limit {policy.portfolio.max_contracts_per_underlying})",
                               ",".join(sorted(ids))))

    return ReconcileReport(tuple(issues))


def ledger_reason_codes(report: ReconcileReport) -> list[str]:
    """Distinct entry-gate reason codes from blocking issues, stable order."""
    seen: list[str] = []
    for i in report.issues:
        if i.severity == "BLOCK" and i.reason_code not in seen:
            seen.append(i.reason_code)
    return seen
