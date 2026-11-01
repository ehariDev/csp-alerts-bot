"""Manual-ledger CLI (F2/F3/F4, IT-007, IT-008). Owner-run only; every command previews by default.

* Nothing is written without --apply AND --reason; every applied command is audited (MANUAL_COMMAND + the
  journal's own events) with the actor (default "owner").
* After any commitment-changing command the F3 reconciliation prompt is printed; entries stay blocked until a
  new confirmed snapshot is recorded with `daily-check-in`.
* No command contacts a broker or Telegram. Prices/fills are what the owner reports.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from csp_alerts.journal.models import (
    COLLATERAL_STATES,
    AccountSnapshot,
    PositionState as S,
    StockLot,
)
from csp_alerts.journal import ledger_rules as rules
from csp_alerts.journal.reconcile import exchange_date, reconcile
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_alerts.journal.state_machine import TransitionError
from csp_alerts.money import MoneyError, dec, fmt, usd2
from csp_alerts.policy.approved import ApprovedUnderlying, looks_synthetic
from csp_alerts.policy.config import Policy
from csp_alerts.policy.drawdown import compute_drawdown

LEDGER_COMMANDS = ("snapshot", "approve", "add-trade", "fill", "cancel", "close", "expire", "assign", "reset", "record-cash-flow")

F3_PROMPT = (
    "RECONCILIATION REQUIRED (F3): this changed your commitments. New-entry alerts stay blocked "
    "(LEDGER_UNRECONCILED) until you confirm a new snapshot:\n"
    "  daily-check-in --equity <total equity> --free-cash <NET free cash> --ack-reconciled --apply --reason \"...\""
)


class CmdError(ValueError):
    pass


def _d(v: str, name: str) -> Decimal:
    try:
        return dec(v)
    except MoneyError as exc:
        raise CmdError(f"{name} must be a plain decimal number (got {v!r})") from exc


def _date(v: str, name: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise CmdError(f"{name} must be YYYY-MM-DD") from exc


def _when(v: str | None, now: datetime) -> datetime:
    if not v:
        return now
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CmdError("time must be ISO, e.g. 2026-10-30T15:05:00Z") from exc
    if t.tzinfo is None:
        raise CmdError("time must include a UTC offset or Z")
    return t.astimezone(timezone.utc)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--apply", action="store_true", help="write (default is preview only)")
    p.add_argument("--actor", default="owner")
    p.add_argument("--reason", default="", help="required with --apply; stored in the audit log")


def add_parsers(js: argparse._SubParsersAction, sub: argparse._SubParsersAction) -> None:
    p = js.add_parser("snapshot", help="record an equity/NET free cash snapshot (daily-check-in is the guided form)")
    _common(p)
    p.add_argument("--equity", required=True)
    p.add_argument("--free-cash", required=True)
    p.add_argument("--asof", default=None)
    p.add_argument("--unconfirmed", action="store_true")

    p = js.add_parser("approve", help="add/update an owner approval in the whitelist file (preview first)")
    _common(p)
    for f in ("ticker", "asset-type", "sector", "bucket", "max-buy-strike", "approved-through", "events-reviewed-on",
              "events-cleared-through", "ownership-reason"):
        p.add_argument(f"--{f}", required=True)
    p.add_argument("--standard-deliverable-confirmed-on", default=None,
                   help="YYYY-MM-DD: you checked the contract deliverable is standard 100 shares (needed for entries)")

    p = js.add_parser("add-trade", help="record a put you placed at your broker (1 contract, standard 100 shares)")
    _common(p)
    p.add_argument("--trade-id", required=True)
    p.add_argument("--ticker", required=True)
    p.add_argument("--expiration", required=True)
    p.add_argument("--strike", required=True)
    p.add_argument("--contracts", default="1")
    p.add_argument("--multiplier", default="100")
    p.add_argument("--sector", default=None)
    p.add_argument("--bucket", default=None)
    p.add_argument("--state", choices=["DRAFT", "PENDING", "WORKING"], default="PENDING")
    p.add_argument("--notes", default="")

    p = js.add_parser("fill", help="record the opening fill (PENDING/WORKING -> OPEN)")
    _common(p)
    p.add_argument("--trade-id", required=True)
    p.add_argument("--credit", required=True, help="actual credit per share")
    p.add_argument("--fees", default="0")
    p.add_argument("--time", default=None)

    p = js.add_parser("cancel", help="record a verified cancellation of a pending/working order")
    _common(p)
    p.add_argument("--trade-id", required=True)
    p.add_argument("--verified", action="store_true", help="you verified the cancellation at your broker")

    p = js.add_parser("close", help="record a close order (--working) or the confirmed closing fill (--debit)")
    _common(p)
    p.add_argument("--trade-id", required=True)
    p.add_argument("--working", action="store_true", help="OPEN -> CLOSE_WORKING (order placed, not filled)")
    p.add_argument("--debit", default=None, help="actual closing debit per share")
    p.add_argument("--fees", default="0")
    p.add_argument("--time", default=None)

    p = js.add_parser("expire", help="record a verified expiry")
    _common(p)
    p.add_argument("--trade-id", required=True)
    p.add_argument("--date", default=None)
    p.add_argument("--confirm-expired", action="store_true", help="expired with no late exercise/assignment outstanding")

    p = js.add_parser("assign", help="guided assignment: step 1 record it; step 2 record shares + cash atomically")
    _common(p)
    p.add_argument("--trade-id", required=True)
    p.add_argument("--date", default=None)
    p.add_argument("--shares", default=None)
    p.add_argument("--cost-basis-usd", default=None, help="total cash paid for the shares")
    p.add_argument("--price", default=None, help="price per share (default: strike)")
    p.add_argument("--fees", default="0")

    p = js.add_parser("reset", help="manual drawdown risk reset after review (clears the latch)")
    _common(p)
    p.add_argument("--reviewed", action="store_true", help="you reviewed the account and accept resuming entries")

    p = js.add_parser("record-cash-flow", help="log an external deposit(+)/withdrawal(-); shifts the drawdown peak")
    _common(p)
    p.add_argument("--amount", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--note", required=True)

    c = sub.add_parser("daily-check-in", help="F2: review positions/cash and confirm a new snapshot (preview first)")
    _common(c)
    c.add_argument("--equity", default=None)
    c.add_argument("--free-cash", default=None, help="NET free unencumbered cash, free for NEW puts")
    c.add_argument("--mark", action="append", default=[], help="LOT_ID=PRICE (re-mark owned shares)")
    c.add_argument("--ack-reconciled", action="store_true",
                   help="I confirm positions, orders, shares, cash and equity above are complete")


@dataclass
class Ctx:
    args: argparse.Namespace
    j: Journal
    pol: Policy
    now: datetime
    approved_path: Path

    def gate(self, summary: list[str]) -> bool:
        """Print the preview; return True only when the write should happen."""
        print("\n".join(summary))
        if not self.args.apply:
            print("PREVIEW ONLY - nothing written. Re-run with --apply --reason \"...\" to record.")
            return False
        if not self.args.reason.strip():
            raise CmdError("--reason is required with --apply (it is stored in the audit log)")
        return True

    def done(self, command: str, args: dict[str, Any], commitment: bool) -> None:
        self.j.audit_manual(command, self.args.actor, self.args.reason, args)
        print(f"RECORDED ({command}) by {self.args.actor}.")
        if commitment:
            print(F3_PROMPT)


def _position(c: Ctx, trade_id: str):
    try:
        return c.j.get_position(trade_id)
    except JournalError as exc:
        raise CmdError(str(exc)) from exc


def _committed(j: Journal) -> Decimal:
    return rules.committed_collateral(j.list_positions())


def _check_cash(j: Journal, equity: Decimal, cash: Decimal) -> None:
    try:
        rules.check_cash(_committed(j), equity, cash)      # shared with the web check-in (journal/ledger_rules.py)
    except rules.RuleError as exc:
        raise CmdError(str(exc)) from exc


def cmd_snapshot(c: Ctx) -> int:
    a = c.args
    eq, cash = _d(a.equity, "equity"), _d(a.free_cash, "free-cash")
    asof = _when(a.asof, c.now)
    _check_cash(c.j, eq, cash)
    if not c.gate([f"Snapshot: equity {eq}, NET free cash {cash}, as of {asof.isoformat()}, "
                   f"{'UNCONFIRMED' if a.unconfirmed else 'manually confirmed'}"]):
        return 0
    c.j.add_snapshot(AccountSnapshot(str(uuid.uuid4()), asof, eq, cash, not a.unconfirmed, "cli"), actor=a.actor)
    c.done("snapshot", {"equity": fmt(eq), "free_cash": fmt(cash)}, commitment=False)
    return 0


def cmd_daily_check_in(c: Ctx) -> int:
    a = c.args
    book = c.j.load_book()
    today = exchange_date(c.now)
    lines = ["DAILY CHECK-IN (review, then confirm equity and NET free cash)", "Positions / orders on record:"]
    live = [p for p in book.positions if p.state in COLLATERAL_STATES or p.state in (S.ASSIGNED_PENDING_RECONCILIATION,)]
    for p in live:
        lines.append(f"  {p.trade_id}: {p.ticker} {p.expiration} PUT ${p.strike} x{p.contracts} [{p.state.value}] "
                     f"collateral ${p.collateral_usd:,.2f}; DTE {(p.expiration - today).days}")
    if not live:
        lines.append("  (none)")
    lines.append("Owned stock lots:")
    for lot in book.stock_lots:
        lines.append(f"  {lot.lot_id}: {lot.ticker} {lot.shares} sh @ ref ${lot.reference_price} (marked {lot.reference_asof_utc:%Y-%m-%d %H:%MZ})")
    if not book.stock_lots:
        lines.append("  (none)")
    committed = _committed(c.j)
    lines.append(f"Expected put collateral reserved: ${committed:,.2f}")
    snap = book.latest_snapshot
    lines.append("Last snapshot: " + ("none" if snap is None else
                 f"{snap.asof_utc:%Y-%m-%d %H:%MZ} equity {snap.equity_usd} NET free cash {snap.free_unencumbered_cash_usd} "
                 f"({'confirmed' if snap.manual_confirmed else 'UNCONFIRMED'})"))
    for i in reconcile(book, c.pol, c.now).issues:
        if i.code not in ("SNAPSHOT_STALE", "DAILY_CHECKIN_MISSING", "SNAPSHOT_FUTURE", "CHANGE_AFTER_SNAPSHOT", "NO_SNAPSHOT", "SNAPSHOT_UNCONFIRMED"):
            lines.append(f"  ISSUE {i.severity} {i.code}: {i.message} {i.ref}".rstrip())
    lines.append("REMINDER: enter NET free unencumbered cash (free for NEW puts, after reserving collateral for the puts above).")
    if a.equity is None or a.free_cash is None:
        print("\n".join(lines))
        print("To confirm: daily-check-in --equity <n> --free-cash <n> [--mark LOT=PRICE] --ack-reconciled --apply --reason \"...\"")
        return 0
    eq, cash = _d(a.equity, "equity"), _d(a.free_cash, "free-cash")
    _check_cash(c.j, eq, cash)
    marks: list[tuple[str, Decimal]] = []
    for m in a.mark:
        lot_id, _, price = m.partition("=")
        marks.append((lot_id, _d(price, f"mark price for {lot_id}")))
    lines.append(f"To record: equity {eq}, NET free cash {cash}, marks {marks or 'none'}, acknowledged={a.ack_reconciled}")
    if a.apply and not a.ack_reconciled:
        raise CmdError("--ack-reconciled is required: confirm that positions, orders, shares, cash and equity are complete")
    if not c.gate(lines):
        return 0
    for lot_id, price in marks:
        c.j.mark_stock_lot(lot_id, price, c.now, actor=a.actor)
    c.j.add_snapshot(AccountSnapshot(str(uuid.uuid4()), c.now, eq, cash, True, "daily-check-in"), actor=a.actor)
    c.done("daily-check-in", {"equity": fmt(eq), "free_cash": fmt(cash)}, commitment=False)
    rep = reconcile(c.j.load_book(), c.pol, c.now)
    print("Ledger status: " + ("CLEAN - entries may resume if no drawdown latch/other blocker (see: status)" if not rep.blocking else
                               "STILL BLOCKED: " + ", ".join(sorted({i.code for i in rep.issues if i.severity == 'BLOCK'}))))
    return 0


def _load_approved_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"approved_underlyings": []}
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_approve(c: Ctx) -> int:
    a = c.args
    rec = {
        "ticker": a.ticker.upper(), "asset_type": a.asset_type.upper(), "sector": a.sector, "correlation_bucket": a.bucket,
        "max_buy_strike": a.max_buy_strike, "approved_through": a.approved_through,
        "events_reviewed_on": a.events_reviewed_on, "events_cleared_through": a.events_cleared_through,
        "ownership_reason": a.ownership_reason, "approved_by": "owner",
    }
    if a.standard_deliverable_confirmed_on:
        rec["standard_deliverable_confirmed_on"] = a.standard_deliverable_confirmed_on
    try:
        model = ApprovedUnderlying.model_validate(rec)
    except ValueError as exc:
        raise CmdError(f"invalid approval: {exc}") from exc
    if looks_synthetic(model):
        raise CmdError("record looks synthetic/example (ticker EXAMPLE or marker words in the reason); not a live approval")
    raw = _load_approved_raw(c.approved_path)
    items = [x for x in raw.get("approved_underlyings", []) if x.get("ticker") != model.ticker]
    replaced = len(items) != len(raw.get("approved_underlyings", []))
    items.append(json.loads(model.model_dump_json(exclude_defaults=False, exclude_none=True)))
    items = [{k: v for k, v in x.items() if k != "synthetic"} for x in items]
    lines = [f"{'UPDATE' if replaced else 'ADD'} approval in {c.approved_path}:", json.dumps(items[-1], indent=2),
             "Entry alerts need: approval current, event review <= 7 days old, events cleared through expiration, "
             "and standard_deliverable_confirmed_on within 7 days."]
    if model.standard_deliverable_confirmed_on is None:
        lines.append("NOTE: no --standard-deliverable-confirmed-on: every contract will be blocked (NONSTANDARD_CONTRACT).")
    if not c.gate(lines):
        return 0
    c.approved_path.parent.mkdir(parents=True, exist_ok=True)
    if c.approved_path.exists():
        c.approved_path.with_suffix(".json.bak").write_text(c.approved_path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = c.approved_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"approved_underlyings": items}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, c.approved_path)
    c.done("approve", {"ticker": model.ticker}, commitment=False)
    return 0


def cmd_add_trade(c: Ctx) -> int:
    a = c.args
    contracts, mult = a.contracts, a.multiplier
    if contracts != "1":
        raise CmdError("MVP records 1 contract per position (IT-007). Multi-contract/partial positions are not supported; "
                       "resolve at your broker and record separate 1-contract puts only if they are truly separate orders.")
    if mult != "100":
        raise CmdError("only standard 100-share contracts can be recorded (non-standard deliverables are not modeled)")
    sector, bucket = a.sector, a.bucket
    if sector is None or bucket is None:
        match = [x for x in _load_approved_raw(c.approved_path).get("approved_underlyings", []) if x.get("ticker") == a.ticker.upper()]
        if not match:
            raise CmdError("ticker is not in the approved list; pass --sector and --bucket explicitly")
        sector, bucket = sector or match[0]["sector"], bucket or match[0]["correlation_bucket"]
    n = NewPosition(a.trade_id, a.ticker.upper(), _date(a.expiration, "expiration"), _d(a.strike, "strike"), 100, 1,
                    sector, bucket, account_scope=c.pol.account_scope, notes=a.notes)
    lines = [f"ADD TRADE {n.trade_id}: {n.ticker} {n.expiration} PUT ${n.strike} x1, state {a.state}, scope {n.account_scope}",
             f"Collateral reserved once PENDING/WORKING/OPEN: ${n.strike * 100:,.2f}",
             "This only records what you did at your broker; it places nothing."]
    if not c.gate(lines):
        return 0
    c.j.create_draft(n, actor=a.actor)
    if a.state in ("PENDING", "WORKING"):
        c.j.transition(n.trade_id, S.PENDING, actor=a.actor)
    if a.state == "WORKING":
        c.j.transition(n.trade_id, S.WORKING, actor=a.actor)
    c.done("add-trade", {"trade_id": n.trade_id, "state": a.state}, commitment=a.state != "DRAFT")
    return 0


def cmd_fill(c: Ctx) -> int:
    a = c.args
    p = _position(c, a.trade_id)
    credit, fees, when = _d(a.credit, "credit"), _d(a.fees, "fees"), _when(a.time, c.now)
    lines = [f"FILL {p.trade_id}: {p.state.value} -> OPEN, credit ${credit}/sh x{p.contracts} = ${credit * p.multiplier * p.contracts:,.2f}, "
             f"fees ${fees}, at {when.isoformat()}"]
    if not c.gate(lines):
        return 0
    c.j.transition(p.trade_id, S.OPEN, actor=a.actor, fill_contracts=p.contracts, actual_open_credit_per_share=credit,
                   opened_at_utc=when, fees_usd=fees)
    c.done("fill", {"trade_id": p.trade_id}, commitment=True)
    return 0


def cmd_cancel(c: Ctx) -> int:
    p = _position(c, c.args.trade_id)
    if not c.gate([f"CANCEL {p.trade_id}: {p.state.value} -> CANCELED (collateral released). Verified at broker: {c.args.verified}"]):
        return 0
    if p.state in (S.PENDING, S.WORKING) and not c.args.verified:
        raise CmdError("--verified is required: confirm at your broker that the order is cancelled")
    c.j.transition(p.trade_id, S.CANCELED, actor=c.args.actor, confirmed=c.args.verified)
    c.done("cancel", {"trade_id": p.trade_id}, commitment=True)
    return 0


def cmd_close(c: Ctx) -> int:
    a = c.args
    p = _position(c, a.trade_id)
    if a.working == (a.debit is not None):
        raise CmdError("use exactly one of --working (order placed) or --debit (confirmed closing fill)")
    if a.working:
        if not c.gate([f"CLOSE ORDER {p.trade_id}: {p.state.value} -> CLOSE_WORKING (collateral stays reserved until the fill)"]):
            return 0
        c.j.transition(p.trade_id, S.CLOSE_WORKING, actor=a.actor)
        c.done("close", {"trade_id": p.trade_id, "stage": "working"}, commitment=True)
        return 0
    debit, fees, when = _d(a.debit, "debit"), _d(a.fees, "fees"), _when(a.time, c.now)
    credit = p.actual_open_credit_per_share or Decimal("0")
    pnl = (credit - debit) * p.multiplier * p.contracts - p.fees_usd - fees
    if not c.gate([f"CLOSE FILL {p.trade_id}: -> CLOSED, debit ${debit}/sh, fees ${fees}; realized P&L about {usd2(pnl)} "
                   "(opening fees included; assignment/stock effects excluded)"]):
        return 0
    if p.state is S.OPEN:
        c.j.transition(p.trade_id, S.CLOSE_WORKING, actor=a.actor)
    c.j.transition(p.trade_id, S.CLOSED, actor=a.actor, actual_close_debit_per_share=debit, closed_at_utc=when, fees_usd=p.fees_usd + fees)
    c.done("close", {"trade_id": p.trade_id, "stage": "filled"}, commitment=True)
    return 0


def _event_time(c: Ctx, a, *, refuse_future_date: bool = False) -> datetime:
    """Event time for expire/assign: 21:00Z of the exchange date, never later than the journal clock (SR-08: a record made
    during the day, e.g. 10:00 CT on expiration day, is dated "now", not in the future)."""
    try:
        return rules.event_time(c.now, _date(a.date, "date") if a.date else None, refuse_future_date=refuse_future_date)
    except rules.RuleError as exc:
        raise CmdError(str(exc)) from exc


def cmd_expire(c: Ctx) -> int:
    a = c.args
    p = _position(c, a.trade_id)
    when = _event_time(c, a, refuse_future_date=True)
    if not c.gate([f"EXPIRE {p.trade_id}: {p.state.value} -> EXPIRED at {when.date()} (expiration {p.expiration}); "
                   f"you confirm no late exercise/assignment remains: {a.confirm_expired}"]):
        return 0
    if not a.confirm_expired:
        raise CmdError("--confirm-expired is required")
    c.j.transition(p.trade_id, S.EXPIRED, actor=a.actor, confirmed=True, closed_at_utc=when)
    c.done("expire", {"trade_id": p.trade_id}, commitment=True)
    return 0


def cmd_assign(c: Ctx) -> int:
    """F4: (1) record assignment -> ASSIGNED_PENDING_RECONCILIATION; (2) shares + cash atomically; (3) daily-check-in."""
    a = c.args
    p = _position(c, a.trade_id)
    shares_total = Decimal(p.contracts * p.multiplier)
    if p.state is S.ASSIGNED_PENDING_RECONCILIATION and a.shares is None:
        print(f"{p.trade_id} is ASSIGNED_PENDING_RECONCILIATION: you still owe the share/cash record (obligation: "
              f"{shares_total} shares x ${p.strike} = ${p.collateral_usd:,.2f}).\n"
              f"  Step 2: journal assign --trade-id {p.trade_id} --shares {shares_total} --cost-basis-usd <cash paid> "
              "[--price P --fees F] --apply --reason \"...\"\n  Step 3: daily-check-in (new snapshot).")
        return 0
    if p.state is not S.ASSIGNED_PENDING_RECONCILIATION:
        when = _event_time(c, a)
        if not c.gate([f"STEP 1/3 ASSIGNMENT {p.trade_id}: {p.state.value} -> ASSIGNED_PENDING_RECONCILIATION on {when.date()}.",
                       f"Obligation stays visible: {shares_total} shares of {p.ticker} at ${p.strike} (${p.collateral_usd:,.2f}). "
                       "New entries are frozen until steps 2 and 3 are done."]):
            return 0
        c.j.transition(p.trade_id, S.ASSIGNED_PENDING_RECONCILIATION, actor=a.actor)
        c.done("assign-step1", {"trade_id": p.trade_id}, commitment=True)
        print(f"Next: journal assign --trade-id {p.trade_id} --shares {shares_total} --cost-basis-usd <cash paid> --apply --reason ...")
        return 0
    shares, cost = _d(a.shares, "shares"), _d(a.cost_basis_usd, "cost-basis-usd") if a.cost_basis_usd else None
    if cost is None:
        raise CmdError("--cost-basis-usd (total cash paid for the shares) is required for step 2")
    price, fees = (_d(a.price, "price") if a.price else p.strike), _d(a.fees, "fees")
    when = _event_time(c, a)
    lot = StockLot(f"LOT-{p.trade_id}", p.ticker, shares, price, when, p.sector, p.correlation_bucket, "ASSIGNMENT",
                   cost_basis_reference_usd=cost, linked_trade_id=p.trade_id)
    if not c.gate([f"STEP 2/3 SHARES {p.trade_id}: record {shares} {p.ticker} @ ${price} (cash paid ${cost}, fees ${fees}) and mark the put "
                   "ASSIGNED_RECONCILED in ONE transaction. Collateral is released and the shares counted exactly once."]):
        return 0
    c.j.complete_assignment(p.trade_id, lot, fees_usd=fees, actor=a.actor)
    c.done("assign-step2", {"trade_id": p.trade_id, "lot": lot.lot_id}, commitment=True)
    print("Step 3/3: run daily-check-in to confirm a new snapshot; the entry freeze lifts only when reconcile-check is clean.")
    return 0


def cmd_reset(c: Ctx) -> int:
    book = c.j.load_book()
    dd = compute_drawdown(book.snapshots, c.pol, baseline_asof_utc=book.last_risk_reset_utc,
                          baseline_equity_usd=book.baseline_equity_usd, cash_flows=book.cash_flows)
    snap = book.latest_snapshot
    lines = [f"RISK RESET: current latched level {dd.level.value} (drawdown {dd.drawdown}, peak {dd.peak_equity_usd}).",
             "Applying sets the new drawdown baseline to your latest CONFIRMED snapshot "
             f"({'none' if snap is None else f'{snap.asof_utc:%Y-%m-%d %H:%MZ} equity {snap.equity_usd}'}) and clears the latch.",
             "Do this only after reviewing the account. Reviewed: " + str(c.args.reviewed)]
    if not c.gate(lines):
        return 0
    if not c.args.reviewed:
        raise CmdError("--reviewed is required: confirm you reviewed the account before resuming entries")
    c.j.record_risk_reset(c.args.reason, actor=c.args.actor)
    c.done("reset", {"previous_level": dd.level.value}, commitment=False)
    return 0


def cmd_cash_flow(c: Ctx) -> int:
    a = c.args
    amount, eff = _d(a.amount, "amount"), _date(a.date, "date")
    book = c.j.load_book()

    def peak(flows):
        return compute_drawdown(book.snapshots, c.pol, baseline_asof_utc=book.last_risk_reset_utc,
                                baseline_equity_usd=book.baseline_equity_usd, cash_flows=flows)
    before = peak(book.cash_flows)
    after = peak(book.cash_flows + ((c.now, amount),))  # applies from the moment it is recorded
    lines = [f"CASH FLOW {amount:+} on {eff} ({a.note}).",
             f"Drawdown peak {before.peak_equity_usd} -> {after.peak_equity_usd}; level {before.level.value} -> {after.level.value} "
             "(a cash flow can never clear or downgrade a latched level; only `reset` does).",
             "IMPORTANT: this takes effect from your NEXT snapshot only; it never changes a snapshot already recorded.",
             "Record it BEFORE the check-in whose equity already contains the flow; otherwise that check-in reads the money moved as a gain or loss."]
    if after.level != before.level and before.level.value not in ("UNKNOWN",) and \
            ("NONE PAUSE REDUCE_REVIEW HARD_HALT".split().index(after.level.value)
             < "NONE PAUSE REDUCE_REVIEW HARD_HALT".split().index(before.level.value)):
        raise CmdError("internal check failed: cash flow would downgrade the latched level")
    if not c.gate(lines):
        return 0
    c.j.record_cash_flow(amount, eff, a.note, actor=a.actor)
    c.done("record-cash-flow", {"amount": fmt(amount), "date": eff.isoformat()}, commitment=True)
    return 0


HANDLERS = {
    "snapshot": cmd_snapshot, "approve": cmd_approve, "add-trade": cmd_add_trade, "fill": cmd_fill, "cancel": cmd_cancel,
    "close": cmd_close, "expire": cmd_expire, "assign": cmd_assign, "reset": cmd_reset, "record-cash-flow": cmd_cash_flow,
    "daily-check-in": cmd_daily_check_in,
}


def _recheck_breach_flags(j: Journal, pol: Policy, approved_path: Path, now: datetime) -> None:
    """After an applied command, clear recorded web policy-breach flags that no longer reproduce (audited). Never fails the
    command; unreadable approvals clear nothing."""
    try:
        from csp_alerts.journal.webtrade import sweep_breaches
        from csp_alerts.policy.approved import load_approved
        cleared = sweep_breaches(j, pol, load_approved(approved_path), now, reason="re-checked after a ledger command",
                                 actor="system:ledger-cli", source="cli")
        for t in cleared:
            print(f"POLICY_BREACH_CLEARED for {t}: the recorded breach no longer reproduces.")
    except Exception:  # noqa: BLE001
        pass


def execute(command: str, args: argparse.Namespace, j: Journal, pol: Policy, approved_path: Path, now: datetime) -> int:
    try:
        rc = HANDLERS[command](Ctx(args, j, pol, now, approved_path))
        if rc == 0 and getattr(args, "apply", False):
            _recheck_breach_flags(j, pol, approved_path, now)
        return rc
    except (CmdError, JournalError, TransitionError) as exc:
        print(f"FAIL {exc}")
        return 1
