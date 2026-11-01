"""Glue between jobs, outbox and transport for the CLI: send-mode resolution, revalidation, dispatch.

Nothing here can send unless `resolve_send_mode` returned SEND. Entry alerts are re-validated at flush time
(IT-003): policy revision, owner approval record, ledger changes since queueing, and ledger/drawdown blockers.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from csp_alerts.alerts import outbox
from csp_alerts.alerts.sendmode import Mode, SendDecision, resolve_send_mode
from csp_alerts.alerts.telegram import TelegramTransport, chat_id_sha256
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.journal.models import to_iso
from csp_alerts.journal.store import Journal
from csp_alerts.policy.approved import ApprovedUnderlying
from csp_alerts.policy.config import Policy
from csp_alerts.policy.entry import book_entry_blocks
from csp_alerts.settings import Settings


def approval_sha(a: ApprovedUnderlying) -> str:
    return hashlib.sha256(a.model_dump_json().encode("utf-8")).hexdigest()


def pinned_ok(journal: Journal, settings: Settings) -> bool:
    pin = journal.pinned_chat()
    return bool(pin and settings.telegram_configured and pin.get("chat_type") == "private"
                and pin.get("chat_id_sha256") == chat_id_sha256(settings.telegram_chat_id.reveal()))


def decide(journal: Journal, settings: Settings, *, flag_send: bool, flag_dry_run: bool) -> SendDecision:
    return resolve_send_mode(env_dry_run=settings.dry_run, flag_send=flag_send, flag_dry_run=flag_dry_run,
                             telegram_configured=settings.telegram_configured, chat_pinned_ok=pinned_ok(journal, settings))


def make_transport(settings: Settings) -> TelegramTransport:
    return TelegramTransport(settings.telegram_bot_token.reveal(), settings.telegram_chat_id.reveal())


def _ledger_marker(journal: Journal) -> str:
    """Ledger revision marker. Includes the journal event sequence, so two ledger events inside one clock second
    still produce different markers (SR-07)."""
    ch = journal.last_ledger_change()
    book = journal.load_book()
    snap = book.latest_snapshot
    return (f"{'' if ch is None else ch.isoformat()}|{'' if snap is None else snap.snapshot_id}"
            f"|{book.ledger_revision}")


def position_fingerprint(pos) -> str:
    """Hash of the economic facts a position alert rests on (not its lifecycle state)."""
    blob = "|".join(str(x) for x in (pos.trade_id, pos.ticker, pos.expiration.isoformat(), pos.strike, pos.multiplier,
                                      pos.contracts, pos.actual_open_credit_per_share, pos.paper))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Outbox classes tied to one specific position. A missing reference fails closed (dropped, audited).
CALENDAR_KINDS = {"TIME_EXIT", "ASSIGNMENT_REVIEW"}  # calendar-based; a row with NO evidence at all is still sent
POSITION_KINDS = {"PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW", "TIME_EXIT", "ASSIGNMENT_REVIEW"}


def _position_reason(journal: Journal, policy: Policy, rv: dict[str, Any], kind: str, t: datetime) -> str | None:
    """SR-05: re-check a queued position alert against the CURRENT journal state and evidence."""
    from csp_alerts.journal.models import MONITORED_STATES, PositionState, from_iso
    from csp_alerts.journal.store import JournalError
    trade_id = rv.get("trade_id")
    if not trade_id:
        if kind in CALENDAR_KINDS and not rv:
            return None  # reference-less calendar reminder (legacy row): cannot be checked, a missed one is worse
        return "position reference missing; cannot confirm the position is still open"
    try:
        pos = journal.get_position(trade_id)
    except JournalError:
        return "position no longer exists in the journal"
    queued_state = rv.get("state")
    if queued_state == PositionState.ASSIGNED_PENDING_RECONCILIATION.value:
        if pos.state is not PositionState.ASSIGNED_PENDING_RECONCILIATION:
            return f"assignment already reconciled or changed (now {pos.state.value})"
    elif pos.state not in MONITORED_STATES:
        return f"position is now {pos.state.value}; advice no longer applies"
    if rv.get("fingerprint") != position_fingerprint(pos):
        return "position terms or opening evidence changed since queued"
    if kind == "PROFIT_EXIT":  # indicative buyback price: only valid for about two monitor intervals
        at = rv.get("evidence_at_utc")
        limit = timedelta(minutes=2 * policy.market.exit_monitor_interval_minutes)
        if not at or t - from_iso(at) > limit:
            return "profit-exit quote evidence is older than two monitor intervals or untimed"
    return None


def make_revalidator(journal: Journal, policy: Policy, approved_loader: Callable[[], list[ApprovedUnderlying]],
                     now: Callable[[], datetime]):
    def check(ev: dict[str, Any], kind: str) -> str | None:
        rv = ev.get("revalidate") or {}
        if kind != "ENTRY_RESEARCH":
            if kind in POSITION_KINDS or (kind == "DATA_ISSUE" and rv.get("trade_id")):
                return _position_reason(journal, policy, rv, kind, now())
            return None
        if ev.get("policy_sha") != policy.sha256:
            return "policy revision changed since queued"
        current = {a.ticker: approval_sha(a) for a in approved_loader()}
        if current.get(rv.get("ticker")) != rv.get("approval_sha"):
            return "owner approval record changed or removed since queued"
        if rv.get("ledger_marker") != _ledger_marker(journal):
            return "ledger or snapshot changed since queued"
        t = now()
        codes, _ = book_entry_blocks(journal.load_book(), policy, t)
        if codes:
            return "entries now blocked: " + ",".join(sorted(c.value for c in codes))
        from csp_alerts.market import calendar as mcal
        from csp_alerts.market.models import SessionStatus
        sess = mcal.session_info(t)
        if (sess.status is not SessionStatus.OPEN or sess.minutes_to_close is None
                or sess.minutes_to_close < policy.market.no_entry_in_final_minutes_of_session):
            return f"market session {sess.status.value.lower()} or inside the final {policy.market.no_entry_in_final_minutes_of_session} minutes at send time"
        return None
    return check


def make_chat_check(journal: Journal, settings: Settings) -> Callable[[], str | None]:
    return lambda: None if pinned_ok(journal, settings) else "recipient not pinned/private or does not match TELEGRAM_CHAT_ID"


def dispatch(journal: Journal, messages: list[AlertMessage], decision: SendDecision, settings: Settings, policy: Policy,
             now: datetime, transport: TelegramTransport, approved_loader: Callable[[], list[ApprovedUnderlying]],
             clock: Callable[[], datetime]) -> outbox.FlushReport:
    """Enqueue (only in SEND mode) then flush. Dry-run callers must not call this."""
    assert decision.mode is Mode.SEND
    marker = _ledger_marker(journal)
    for m in messages:
        if m.kind == "ENTRY_RESEARCH":
            reval = {"ticker": (m.meta or {}).get("ticker"), "approval_sha": (m.meta or {}).get("approval_sha"),
                     "ledger_marker": marker}
            outbox.enqueue(journal, m, decision, now=now, scope=policy.account_scope, policy_sha=policy.sha256,
                           expires_in_minutes=policy.alerts.entry_alert_expiry_minutes, revalidate=reval)
        elif (m.meta or {}).get("trade_id"):
            reval = {"trade_id": m.meta["trade_id"], "trigger": m.meta.get("trigger"), "state": m.meta.get("state"),
                     "fingerprint": m.meta.get("fingerprint"), "evidence_at_utc": to_iso(now)}
            outbox.enqueue(journal, m, decision, now=now, scope=policy.account_scope, policy_sha=policy.sha256, revalidate=reval)
        else:
            outbox.enqueue(journal, m, decision, now=now, scope=policy.account_scope, policy_sha=policy.sha256)
    secrets = (settings.telegram_bot_token.reveal(), settings.telegram_chat_id.reveal())
    return outbox.flush(journal, transport, now=clock(), revalidator=make_revalidator(journal, policy, approved_loader, clock),
                        chat_check=make_chat_check(journal, settings), secrets=secrets)
