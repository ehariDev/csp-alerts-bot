"""Owner-reported trade recording for the local web page (Stage B1): PLAN (preview) and RECORD (confirm).

The owner executes trades at the broker by hand; this module only records what happened. It places nothing, contacts
nothing and decides nothing financial: every rule is reused from the backend.

* validation / state changes  -> the same `Journal.create_draft` / `Journal.transition` calls the CLI makes
  (`add-trade` + `fill`, `close`, `cancel`, `expire`, `daily-check-in`), inside ONE outer transaction;
* limits and capacity          -> `policy.entry.evaluate_entry`, `policy.portfolio.check_portfolio_limits` / `capacity_rows`;
* cash / timestamp rules       -> `journal.ledger_rules` (shared with the CLI).

Formulas
  collateral reserved = strike * multiplier * contracts  (multiplier 100 and contracts 1 in the MVP)
  realized P&L (close) = (open credit - close debit) * multiplier * contracts - opening fees - closing fees  (as `journal close`)

Flow
  plan = build_plan(read-only journal, ...)    writes NOTHING; returns the exact transition, collateral, capacity rows,
                                               every policy breach, the validation errors and the ledger revision bound to it
  record(writer journal, plan inputs, ...)     re-plans INSIDE the write transaction, refuses a stale revision, is idempotent
                                               per idempotency key, writes everything + audit (actor web:owner) or nothing.

Everything shown to the browser is a fixed phrase or a validated value: no raw exception text, no path, no echo of input.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from csp_alerts.journal import ledger_rules as rules
from csp_alerts.journal.models import (
    COLLATERAL_STATES,
    AccountSnapshot,
    BookState,
    PositionState as S,
    PutPosition,
    ReconcileReport,
)
from csp_alerts.journal.reconcile import exchange_date, reconcile
from csp_alerts.journal.state_machine import TransitionError, validate_transition
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_alerts.market import calendar as mcal
from csp_alerts.market import observations as obs
from csp_alerts.market.models import MarketObservation
from csp_alerts.money import MoneyError, dec, fmt, usd2
from csp_alerts.policy.approved import ApprovedUnderlying, asset_type_map, resolve_asset_type
from csp_alerts.policy.browse import observation_from_row
from csp_alerts.policy.config import Policy
from csp_alerts.policy.entry import evaluate_entry
from csp_alerts.policy.portfolio import CapRow, ProposedPut, capacity_rows, check_portfolio_limits
from csp_alerts.policy.reasons import Reason

ACTOR = "web:owner"
SOURCE = "web"
MVP_CONTRACTS, MVP_MULTIPLIER = 1, 100
MAX_REASON = 300
MAX_LOTS = 12
PREVIEW_TTL_SECONDS = 20 * 60
FUTURE_TOLERANCE = timedelta(minutes=5)

SELL_FILLED, SELL_WORKING, MARK_FILLED = "sell_filled", "sell_working", "mark_filled"
CLOSE_ORDER, CLOSE_FILLED = "close_order", "close_filled"
CANCEL_OPEN, CANCEL_CLOSE, EXPIRE, CHECKIN = "cancel_open_order", "cancel_close_order", "expire", "checkin"
ACTIONS = (SELL_FILLED, SELL_WORKING, MARK_FILLED, CLOSE_ORDER, CLOSE_FILLED, CANCEL_OPEN, CANCEL_CLOSE, EXPIRE, CHECKIN)
NEW_POSITION_ACTIONS = (SELL_FILLED, SELL_WORKING)
TITLES = {
    SELL_FILLED: "Record a sold put that FILLED", SELL_WORKING: "Record a put order placed, not filled yet",
    MARK_FILLED: "Mark a working order as filled", CLOSE_ORDER: "Record a buy-to-close order placed (not filled)",
    CLOSE_FILLED: "Record a confirmed buy-to-close fill", CANCEL_OPEN: "Record that an unfilled order was canceled",
    CANCEL_CLOSE: "Record that the closing order was canceled", EXPIRE: "Record a verified expiry",
    CHECKIN: "Account check-in",
}
# Plain order wording shown on the forms and previews (what the owner does at the broker).
ORDER_WORDS = {SELL_FILLED: "Sell to open", SELL_WORKING: "Sell to open", MARK_FILLED: "Sell to open (the order from earlier)",
               CLOSE_ORDER: "Buy to close", CLOSE_FILLED: "Buy to close", CANCEL_CLOSE: "Buy to close (canceled order)"}
# Reason groups. Portfolio + owner-approval codes are FLAGGED (acknowledgement required, entries blocked while the position
# reserves collateral). Contract codes depend on the stored quote and are shown for information. Context codes describe the
# account state, not the trade.
PORTFOLIO_CODES = frozenset({
    Reason.NORMAL_EXPOSURE_LIMIT, Reason.PILOT_COLLATERAL_LIMIT, Reason.SINGLE_TICKER_LIMIT, Reason.SECTOR_LIMIT,
    Reason.CORRELATED_LIMIT, Reason.EXPIRY_WEEK_LIMIT, Reason.CONTRACT_COUNT_LIMIT, Reason.INDIVIDUAL_NAME_LIMIT,
    Reason.INSUFFICIENT_FREE_CASH})
OWNER_CODES = frozenset({Reason.NOT_APPROVED, Reason.APPROVAL_EXPIRED, Reason.VALUATION_TOO_HIGH, Reason.EVENT_UNVERIFIED,
                         Reason.EARNINGS_IN_WINDOW})
CONTEXT_CODES = frozenset({Reason.LEDGER_STALE, Reason.LEDGER_UNRECONCILED, Reason.DRAWDOWN_PAUSE, Reason.HARD_HALT,
                           Reason.MARKET_CLOSED, Reason.UNKNOWN_SESSION})
LABELS_UNKNOWN = "LABELS_UNKNOWN"

CLI_ONLY = ("Not available on this page (restricted controls, use the documented CLI procedure in RUNBOOK.md): baseline "
            "confirmation, cash-flow recording, drawdown risk reset, approvals, assignment, partial fills, adjustments, "
            "and any quantity other than 1 contract.")
UNSUPPORTED_STATE = ("This position is in a state the web page does not handle (assignment, adjustment review or another "
                     "unresolved state). The obligation stays visible and new entries stay blocked. Use the documented CLI "
                     "procedure (RUNBOOK.md, assignment / adjustments).")
F3_PROMPT = ("RECONCILIATION REQUIRED: this changed your commitments. New-entry alerts stay blocked (LEDGER_UNRECONCILED) "
             "until you confirm a new account snapshot on the check-in page.")

# canned result texts (code -> text). Nothing here ever contains user input.
MESSAGES = {
    "recorded": "Recorded. The journal now shows this state (audited as web:owner).",
    "duplicate": "That confirmation was already recorded; nothing was written a second time.",
    "stale": "The ledger changed since this preview, so it was rejected. Nothing was written; ledger changed, preview again.",
    "expired": "The preview expired or does not match this session. Nothing was written; preview again.",
    "token": "The preview token is missing or does not match the form. Nothing was written; preview again.",
    "invalid": "The form has errors. Nothing was written; fix them and preview again.",
    "reason_required": "An explicit reason is required (it is stored in the audit log). Nothing was written.",
    "ack_required": "A required acknowledgement box was not ticked. Nothing was written.",
    "unavailable": "The journal is missing or not upgraded; nothing was written.",
    "rejected": "The ledger rules rejected this record. Nothing was written; preview again.",
    "unsupported": UNSUPPORTED_STATE,
}


@dataclass
class Check:
    """One acknowledgement the owner must tick at confirm time."""
    name: str
    text: str


@dataclass
class Plan:
    action: str
    title: str
    fields: dict[str, str] = field(default_factory=dict)         # normalized values bound by the preview token
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trade_id: str | None = None
    contract: dict[str, Any] = field(default_factory=dict)
    state_change: str = ""
    lines: list[str] = field(default_factory=list)
    collateral_usd: Decimal | None = None
    caps: list[CapRow] = field(default_factory=list)
    breach_codes: list[str] = field(default_factory=list)       # flagged: ack + entries blocked
    info_codes: list[str] = field(default_factory=list)         # contract-level, from the stored quote
    context_codes: list[str] = field(default_factory=list)      # account state now
    checks: list[Check] = field(default_factory=list)
    needs_reason: bool = True
    commitment_changed: bool = False
    ledger_revision: int = 0
    policy_sha: str = ""
    unsupported: bool = False
    still_blocked: list[str] = field(default_factory=list)       # check-in: reconcile issues expected to remain

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unsupported


@dataclass(frozen=True)
class TradeResult:
    code: str
    trade_id: str | None = None
    state: str | None = None
    commitment_changed: bool = False
    breach_codes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.code in ("recorded", "duplicate")

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, MESSAGES["rejected"])


# ---------------------------------------------------------------- input parsing (fixed phrases only)
# ASCII-only on purpose: `\d` would accept any Unicode digit (e.g. Arabic-Indic U+0660) and `$` a trailing newline.
_IDEM = re.compile(r"[0-9a-f]{32}\Z")
_TRADE = re.compile(r"[A-Za-z0-9._:\-]{1,60}\Z")
_SYM = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")
_PRICE = re.compile(r"[0-9]{1,7}(\.[0-9]{1,4})?\Z")
_MONEY = re.compile(r"[0-9]{1,12}(\.[0-9]{1,2})?\Z")
_FEES = re.compile(r"[0-9]{1,5}(\.[0-9]{1,4})?\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]\Z")
_LABEL = re.compile(r"[A-Za-z0-9 _&.\-]{0,40}\Z")
_COUNT = re.compile(r"[0-9]{1,3}\Z")


def clean_text(s: Any, limit: int = MAX_REASON) -> str:
    """Untrusted free text: control/format characters dropped, whitespace collapsed, length limited."""
    t = "".join(c for c in str(s) if unicodedata.category(c) not in ("Cc", "Cf") or c in " ")
    return " ".join(t.split())[:limit]


def _get(form: Mapping[str, str], k: str) -> str:
    v = form.get(k, "")
    return v.strip() if isinstance(v, str) else ""


def _d(raw: str, rx: re.Pattern, what: str, errs: list[str], *, positive: bool = False) -> Decimal | None:
    if not rx.match(raw or ""):
        errs.append(f"{what} must be a plain number (digits and an optional decimal point, no commas or signs).")
        return None
    try:
        v = dec(raw)
    except MoneyError:
        errs.append(f"{what} is not a valid number.")
        return None
    if positive and v <= 0:
        errs.append(f"{what} must be greater than zero.")
        return None
    return v


FEES_REQUIRED = "Enter the actual fees; type 0 if none."


def _fees(form: Mapping[str, str], errs: list[str]) -> Decimal | None:
    """Fees are the owner's number and are never defaulted: blank or missing is refused; an explicit 0 / 0.00 is accepted."""
    raw = _get(form, "fees")
    if raw == "":
        errs.append(FEES_REQUIRED)
        return None
    return _d(raw, _FEES, "Fees (USD)", errs)


def _when(form: Mapping[str, str], tz: str, now: datetime, errs: list[str], what: str) -> datetime | None:
    d, t = _get(form, "executed_date"), _get(form, "executed_time")
    if not _DATE.match(d) or not _TIME.match(t):
        errs.append(f"{what} date (YYYY-MM-DD) and time (HH:MM, 24-hour) are required; they are never defaulted.")
        return None
    try:
        local = datetime.fromisoformat(f"{d}T{t}:00").replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        errs.append(f"{what} date or time is not a real calendar value.")
        return None
    utc = local.astimezone(timezone.utc)
    if utc > now + FUTURE_TOLERANCE:
        errs.append(f"{what} is in the future; an execution cannot be recorded before it happened.")
        return None
    return utc


def _stored_contract(conn, symbol: str, expiry: date, strike: Decimal) -> dict[str, Any] | None:
    if not obs.has_tables(conn):
        return None
    return obs.option_for_contract(conn, symbol, expiry, strike)


def contract_context(conn, symbol: str, expiry: date, strike: Decimal) -> dict[str, Any] | None:
    """Exact contract facts for the form, from the stored chain. `reference_bid` is a labelled reference ONLY: it is never
    used as a fill price. None when the contract is not in the stored chain."""
    r = _stored_contract(conn, symbol, expiry, strike)
    if r is None:
        return None
    return {"symbol": symbol, "occ_symbol": r["occ_symbol"], "occ_source": r["occ_source"], "right": "P", "expiration": expiry,
            "strike": strike, "multiplier": MVP_MULTIPLIER, "contracts": MVP_CONTRACTS, "reference_bid": r.get("bid"),
            "reference_ask": r.get("ask"), "reference_retrieved_at_utc": r.get("retrieved_at_utc"), "row": r,
            "retained_unusable": r.get("retained_unusable")}


def _labels(approved: list[ApprovedUnderlying] | None, symbol: str, form: Mapping[str, str], errs: list[str],
            journal_types: Mapping[str, str] | None = None) -> tuple[str, str, str, ApprovedUnderlying | None, bool]:
    """(sector, bucket, asset_type, approval, labels_unknown). Approved names take the owner's approval labels; others take
    owner-typed labels (length limited) or 'unknown' (which is flagged, never silently diversified)."""
    ap = next((a for a in (approved or []) if a.ticker == symbol), None)
    if ap is not None:
        return ap.sector, ap.correlation_bucket, ap.asset_type, ap, False
    sec, bkt = _get(form, "sector"), _get(form, "bucket")
    if not _LABEL.match(sec) or not _LABEL.match(bkt):
        errs.append("Sector and bucket labels may use letters, digits, spaces and _ & . - (at most 40 characters).")
        sec = bkt = ""
    unknown = not sec or not bkt
    return (sec or "unknown"), (bkt or "unknown"), resolve_asset_type(symbol, journal_types, asset_type_map(approved))[0], None, unknown


def _observation(symbol: str, expiry: date, strike: Decimal, row: dict | None) -> MarketObservation | None:
    """The stored quote as an observation (standard 100-share assumption). No stored row: a contract-identity-only observation,
    so approval and valuation rules can still be evaluated (its missing market data only produces information codes).
    An unreadable stored row returns None, which evaluate_entry reports as MISSING_MARKET_DATA, never as a pass."""
    if row is None:
        return MarketObservation(underlying=symbol, expiration=expiry, strike=strike, multiplier=MVP_MULTIPLIER, option_right="P")
    try:
        return observation_from_row(symbol, row)
    except Exception:  # noqa: BLE001
        return None


def _assess(book: BookState, ap: ApprovedUnderlying | None, row: dict | None, symbol: str, expiry: date, strike: Decimal, sector: str,
            bucket: str, asset_type: str, labels_unknown: bool, policy: Policy, now: datetime, *, cash_is_net: bool = False
            ) -> dict[str, Any]:
    """Every rule the proposed put touches, from the backend: approval + contract gates (evaluate_entry) and portfolio caps
    (check_portfolio_limits, always run, also for an unapproved name). `cash_is_net`: the latest confirmed snapshot post-dates
    this position, so its NET free cash already excludes this collateral and is not compared with it again."""
    snap = book.latest_snapshot
    cash = snap.free_unencumbered_cash_usd if (snap is not None and not cash_is_net) else None
    proposed = ProposedPut(symbol, asset_type, sector, bucket, expiry, strike, MVP_MULTIPLIER, MVP_CONTRACTS)
    session = mcal.session_detail(now).info
    codes: set[Reason] = set()
    try:
        codes |= set(evaluate_entry(_observation(symbol, expiry, strike, row), ap, book, policy, now, session,
                                    contracts=MVP_CONTRACTS).reason_codes)
    except Exception:  # noqa: BLE001 - an unreadable stored value is shown as such, never as a pass
        codes.add(Reason.MISSING_MARKET_DATA)
    codes |= check_portfolio_limits(book, proposed, policy, cash)
    flagged = {c.value for c in codes if c in PORTFOLIO_CODES or c in OWNER_CODES}
    notes: list[str] = []
    if cash is None:
        flagged.discard(Reason.INSUFFICIENT_FREE_CASH.value)
        if not cash_is_net:
            notes.append("No account snapshot exists yet, so free cash is unknown (not treated as a breach). Confirm it on the "
                         "check-in page.")
    if labels_unknown:
        flagged.add(LABELS_UNKNOWN)
    return {"flagged": sorted(flagged), "info": sorted(c.value for c in codes if c not in PORTFOLIO_CODES | OWNER_CODES | CONTEXT_CODES),
            "context": sorted(c.value for c in codes if c in CONTEXT_CODES), "caps": capacity_rows(book, proposed, policy, cash),
            "notes": notes}


def _revision(j: Journal) -> int:
    return j.ledger_seqs()[1]


# ---------------------------------------------------------------- policy-breach flags: automatic clearing
UNKNOWN_LABEL = "unknown"
_SNAPSHOT_NOT_CURRENT = frozenset({"NO_SNAPSHOT", "SNAPSHOT_UNCONFIRMED", "CHANGE_AFTER_SNAPSHOT", "SNAPSHOT_FUTURE"})


def breach_codes_now(book: BookState, p: PutPosition, ap: ApprovedUnderlying | None, policy: Policy, now: datetime,
                     row: dict | None = None, *, cash_is_net: bool = False) -> set[str]:
    """The flagged codes (portfolio caps, owner-approval codes, LABELS_UNKNOWN) that position `p` triggers against the CURRENT book,
    approvals, labels and policy: the same `_assess` the preview uses, with `p` evaluated as the proposed put against the rest of
    the book (so a cap counts `p` exactly once)."""
    rest = replace(book, positions=tuple(x for x in book.positions if x.trade_id != p.trade_id))
    unknown = UNKNOWN_LABEL in (p.sector, p.correlation_bucket)
    a = _assess(rest, ap, row, p.ticker, p.expiration, p.strike, p.sector, p.correlation_bucket,
                ap.asset_type if ap else p.asset_type, unknown, policy, now, cash_is_net=cash_is_net)
    return set(a["flagged"])


def sweep_breaches(j: Journal, policy: Policy, approved: list[ApprovedUnderlying] | None, now: datetime, *, reason: str,
                   actor: str = ACTOR, source: str = SOURCE) -> list[str]:
    """Clear (audited POLICY_BREACH_CLEARED) every recorded policy-breach flag whose position no longer triggers ANY flagged
    code against the current book/approvals/labels/policy. A flag stays while any cap (or approval/label rule) is still
    breached. Fails closed: unreadable approvals clear nothing. Call inside the caller's write transaction. Returns the
    trade ids cleared.

    Free cash: a recorded INSUFFICIENT_FREE_CASH is re-checked only while the latest snapshot pre-dates the position; once a
    confirmed snapshot post-dates every ledger change its NET free cash already excludes this collateral, so comparing it again
    would block forever."""
    if approved is None:
        return []
    book = j.load_book(approved)
    if not book.policy_breaches:
        return []
    issues = {i.code for i in reconcile(book, policy, now).issues}
    net = book.latest_snapshot is not None and not (issues & _SNAPSHOT_NOT_CURRENT)
    cleared: list[str] = []
    for trade_id, recorded in book.policy_breaches:
        p = next((x for x in book.positions if x.trade_id == trade_id), None)
        if p is None or p.state not in COLLATERAL_STATES:
            continue                                  # no longer reserving collateral: the flag is moot
        ap = next((a for a in approved if a.ticker == p.ticker), None)
        row = _stored_contract(j.conn, p.ticker, p.expiration, p.strike)
        still = breach_codes_now(book, p, ap, policy, now, row, cash_is_net=net)
        if still:
            continue
        j.audit_event("POLICY_BREACH_CLEARED", actor,
                      {"trade_id": trade_id, "cleared_codes": list(recorded), "reason": clean_text(reason, 200),
                       "policy_sha256": policy.sha256, "evaluated_against": "current book, approvals, labels and policy"},
                      source, before={"trade_id": trade_id, "codes": list(recorded)})
        cleared.append(trade_id)
    return cleared


def build_plan(j: Journal, policy: Policy, approved: list[ApprovedUnderlying] | None, form: Mapping[str, str], now: datetime,
               tz: str) -> Plan:
    """Validate `form` and describe exactly what recording it would do. Reads only; never writes."""
    action = _get(form, "action")
    plan = Plan(action=action if action in ACTIONS else "", title=TITLES.get(action, "Unknown action"),
                policy_sha=policy.sha256, ledger_revision=_revision(j))
    if action not in ACTIONS:
        plan.errors.append("Unknown action.")
        return plan
    idem = _get(form, "idem")
    if not _IDEM.match(idem):
        plan.errors.append("The form is missing its idempotency key; reload the form page.")
        return plan
    plan.fields = {"action": action, "idem": idem}
    if action == CHECKIN:
        return _plan_checkin(j, policy, form, now, plan)
    if action in NEW_POSITION_ACTIONS:
        return _plan_new(j, policy, approved, form, now, tz, plan)
    return _plan_existing(j, policy, approved, form, now, tz, plan)


# ---------------------------------------------------------------- new position (sold put filled / order working)
def _plan_new(j: Journal, policy: Policy, approved, form, now, tz, plan: Plan) -> Plan:
    errs = plan.errors
    symbol = _get(form, "symbol")
    symbol = symbol.upper() if symbol.isascii() else ""      # str.upper() can map non-ASCII letters onto ASCII ones
    if not _SYM.match(symbol):
        errs.append("The ticker is not a valid symbol.")
        return plan
    exp_raw, strike_raw = _get(form, "expiry"), _get(form, "strike")
    strike = _d(strike_raw, _PRICE, "Strike", errs, positive=True)
    expiry: date | None = None
    if _DATE.match(exp_raw):
        try:
            expiry = date.fromisoformat(exp_raw)
        except ValueError:
            expiry = None
    if expiry is None:
        errs.append("The expiration is not a valid date.")
    elif expiry < exchange_date(now):
        errs.append("That contract has already expired; it cannot be sold now.")
    qty = _get(form, "contracts") or "1"
    if qty != "1":
        errs.append("The MVP records exactly 1 contract per position. Multi-contract, partial or other quantities are not "
                    "supported here: use the documented CLI procedure (RUNBOOK.md) and keep entries blocked until it is resolved.")
    if strike is None or expiry is None:
        return plan
    plan.fields.update({"symbol": symbol, "expiry": expiry.isoformat(), "strike": fmt(strike), "contracts": "1"})
    ctx = contract_context(j.conn, symbol, expiry, strike)
    if ctx is None:
        errs.append("That exact contract is not in the stored chain. Open it from the market page after a refresh; the contract "
                    "is never typed by hand.")
        return plan
    plan.contract = {k: v for k, v in ctx.items() if k != "row"}
    sector, bucket, asset_type, ap, unknown = _labels(approved, symbol, form, errs, j.journal_asset_types())
    if ap is None:
        plan.fields.update({"sector": _get(form, "sector"), "bucket": _get(form, "bucket")})
    credit = fees = when = None
    if plan.action == SELL_FILLED:
        credit = _d(_get(form, "credit"), _PRICE, "Actual credit per share", errs, positive=True)
        fees = _fees(form, errs)
        when = _when(form, tz, now, errs, "Execution")
        if credit is not None and credit > strike:
            errs.append("The credit per share cannot exceed the strike.")
        plan.fields.update({"credit": _get(form, "credit"), "fees": _get(form, "fees"),
                            "executed_date": _get(form, "executed_date"), "executed_time": _get(form, "executed_time")})
    idem = plan.fields["idem"]
    plan.trade_id = f"WEB-{symbol}-{expiry:%Y%m%d}-{idem[:8]}"
    plan.fields["trade_id"] = plan.trade_id
    book = j.load_book(approved)
    plan.collateral_usd = strike * MVP_MULTIPLIER * MVP_CONTRACTS
    a = _assess(book, ap, ctx["row"], symbol, expiry, strike, sector, bucket, asset_type, unknown, policy, now)
    _fill_assessment(plan, a, ap, sector, bucket)
    if not errs:
        probe = PutPosition(plan.trade_id, policy.account_scope, True, symbol, expiry, strike, MVP_MULTIPLIER, MVP_CONTRACTS,
                            sector, bucket, S.PENDING)
        if plan.action == SELL_FILLED:
            try:
                validate_transition(probe, S.OPEN, {"fill_contracts": 1, "actual_open_credit_per_share": credit,
                                                    "opened_at_utc": when, "fees_usd": fees})
            except TransitionError as exc:
                errs.append(str(exc))
        if not _scope_ok(policy, errs):
            return plan
    if plan.action == SELL_FILLED:
        net = (credit or Decimal(0)) * MVP_MULTIPLIER * MVP_CONTRACTS - (fees or Decimal(0))
        plan.state_change = "(new position) -> OPEN   (recorded through PENDING, exactly as `journal add-trade` + `journal fill`)"
        plan.lines = [f"Contract: {symbol} {expiry} PUT ${fmt(strike)} x 1 contract, multiplier 100, scope {policy.account_scope} (paper).",
                      f"Actual credit you report: ${credit}/share = ${(credit or Decimal(0)) * 100:,.2f} for 1 contract; fees ${fees}; "
                      f"net premium after fees about {usd2(net)}.",
                      "It becomes OPEN and is monitored by the next monitor run; no CLI step is needed."]
    else:
        plan.state_change = "(new position) -> WORKING   (collateral reserved; no fill is recorded)"
        plan.lines = [f"Contract: {symbol} {expiry} PUT ${fmt(strike)} x 1 contract, multiplier 100, scope {policy.account_scope} (paper).",
                      "You placed an order that has NOT filled. It reserves collateral like an open put, but it is not monitored as an "
                      "open position and shows no premium. Use 'Mark filled' on the position when your broker confirms the actual fill."]
    plan.commitment_changed = True
    return plan


def _scope_ok(policy: Policy, errs: list[str]) -> bool:
    if not policy.account_scope.startswith("paper"):
        errs.append("The configured account scope is not a paper scope; web recording supports the paper scope only.")
        return False
    return True


def _fill_assessment(plan: Plan, a: dict[str, Any], ap, sector: str, bucket: str) -> None:
    plan.caps, plan.breach_codes, plan.info_codes, plan.context_codes = a["caps"], a["flagged"], a["info"], a["context"]
    plan.warnings += a["notes"]
    plan.contract.update({"sector": sector, "bucket": bucket, "approval": "APPROVED LIST" if ap else "NOT on your approved list"})
    if plan.breach_codes:
        plan.checks.append(Check("ack_breach", "I acknowledge this reported execution BREACHES policy (" + ", ".join(plan.breach_codes)
                                 + "). Record it as it happened; it is flagged and new entries stay blocked while it reserves collateral."))


# ---------------------------------------------------------------- existing position actions
def _plan_existing(j: Journal, policy: Policy, approved, form, now, tz, plan: Plan) -> Plan:
    errs = plan.errors
    tid = _get(form, "trade_id")
    if not _TRADE.match(tid):
        errs.append("The trade id is not valid.")
        return plan
    try:
        p = j.get_position(tid)
    except JournalError:
        errs.append("That position does not exist.")
        return plan
    plan.trade_id = tid
    plan.fields["trade_id"] = tid
    plan.contract = {"symbol": p.ticker, "right": "P", "expiration": p.expiration, "strike": p.strike, "multiplier": p.multiplier,
                     "contracts": p.contracts, "state": p.state.value, "sector": p.sector, "bucket": p.correlation_bucket}
    if not rules.position_is_single_contract(p) or p.state in (S.ASSIGNED_PENDING_RECONCILIATION, S.ADJUSTMENT_REVIEW,
                                                                S.ASSIGNED_RECONCILED):
        plan.unsupported = True
        plan.lines = [UNSUPPORTED_STATE]
        return plan
    plan.collateral_usd = p.collateral_usd
    plan.commitment_changed = True
    act = plan.action
    allowed = {MARK_FILLED: (S.WORKING, S.PENDING), CLOSE_ORDER: (S.OPEN,), CLOSE_FILLED: (S.OPEN, S.CLOSE_WORKING),
               CANCEL_OPEN: (S.WORKING, S.PENDING), CANCEL_CLOSE: (S.CLOSE_WORKING,), EXPIRE: (S.OPEN, S.CLOSE_WORKING)}[act]
    if p.state not in allowed:
        errs.append(f"This action does not apply to a position in state {p.state.value}.")
        return plan
    credit = p.actual_open_credit_per_share
    to: S
    fields: dict[str, Any] = {}
    if act == MARK_FILLED:
        c = _d(_get(form, "credit"), _PRICE, "Actual credit per share", errs, positive=True)
        fees = _fees(form, errs)
        when = _when(form, tz, now, errs, "Execution")
        if c is not None and c > p.strike:
            errs.append("The credit per share cannot exceed the strike.")
        plan.fields.update({"credit": _get(form, "credit"), "fees": _get(form, "fees"),
                            "executed_date": _get(form, "executed_date"), "executed_time": _get(form, "executed_time")})
        to = S.OPEN
        fields = {"fill_contracts": p.contracts, "actual_open_credit_per_share": c, "opened_at_utc": when, "fees_usd": fees}
        plan.state_change = f"{p.state.value} -> OPEN"
        plan.lines = [f"Actual credit ${c}/share, fees ${fees}, executed {plan.fields['executed_date']} {plan.fields['executed_time']} "
                      f"({tz}). It becomes OPEN and is monitored by the next monitor run."]
        book = j.load_book(approved)
        ap = next((a for a in (approved or []) if a.ticker == p.ticker), None)
        row = _stored_contract(j.conn, p.ticker, p.expiration, p.strike)
        if row is not None and not errs:
            rest = replace(book, positions=tuple(x for x in book.positions if x.trade_id != tid))
            a = _assess(rest, ap, row, p.ticker, p.expiration, p.strike, p.sector, p.correlation_bucket, ap.asset_type if ap else p.asset_type,
                        p.sector == "unknown" or p.correlation_bucket == "unknown", policy, now)
            prior = next((codes for t, codes in book.policy_breaches if t == tid), ())
            a["flagged"] = sorted(set(a["flagged"]) | set(prior))
            _fill_assessment(plan, a, ap, p.sector, p.correlation_bucket)
    elif act == CLOSE_ORDER:
        to = S.CLOSE_WORKING
        plan.state_change = "OPEN -> CLOSE_WORKING"
        plan.lines = ["You placed a buy-to-close order that has NOT filled. Collateral stays reserved and the position stays "
                      "monitored until you record the confirmed fill."]
    elif act == CLOSE_FILLED:
        debit = _d(_get(form, "debit"), _PRICE, "Actual closing debit per share", errs)
        fees = _fees(form, errs)
        when = _when(form, tz, now, errs, "Execution")
        plan.fields.update({"debit": _get(form, "debit"), "fees": _get(form, "fees"),
                            "executed_date": _get(form, "executed_date"), "executed_time": _get(form, "executed_time")})
        to = S.CLOSED
        fields = {"actual_close_debit_per_share": debit, "closed_at_utc": when,
                  "fees_usd": None if fees is None else p.fees_usd + fees}
        pnl = None
        if debit is not None and fees is not None:
            pnl = ((credit or Decimal(0)) - debit) * p.multiplier * p.contracts - p.fees_usd - fees
        plan.state_change = ("OPEN -> CLOSE_WORKING -> CLOSED (both recorded, as `journal close --debit`)" if p.state is S.OPEN
                           else "CLOSE_WORKING -> CLOSED")
        plan.lines = [f"Actual closing debit ${debit}/share, closing fees ${fees}. Realized P&L about "
                      f"{'unavailable' if pnl is None else usd2(pnl)} (opening fees included; assignment and stock effects excluded). "
                      "Collateral is released and open-position alerts stop."]
        if debit is not None and debit == 0:
            plan.warnings.append("A closing debit of 0 means the option was closed for nothing; use 'expiry' for a worthless expiry.")
    elif act == CANCEL_OPEN:
        to = S.CANCELED
        fields = {"confirmed": True}
        plan.state_change = f"{p.state.value} -> CANCELED (collateral released)"
        plan.lines = ["Only record this after you verified at your broker that the order is canceled."]
        plan.checks.append(Check("ack_verified", "I verified at my broker that this order is canceled and did not fill."))
    elif act == CANCEL_CLOSE:
        to = S.OPEN
        plan.state_change = "CLOSE_WORKING -> OPEN"
        plan.lines = ["The buy-to-close order was canceled; the short put is still open and monitored."]
        plan.checks.append(Check("ack_verified", "I verified at my broker that the closing order is canceled and did not fill."))
    else:  # EXPIRE
        on_raw = _get(form, "expiry_date")      # required: never defaulted
        try:
            on = date.fromisoformat(on_raw) if _DATE.match(on_raw) else None
        except ValueError:
            on = None
        when2 = None
        if on is None:
            errs.append("The expiry date (YYYY-MM-DD) is required and must be a valid date; it is never defaulted.")
        else:
            try:
                when2 = rules.event_time(now, on, refuse_future_date=True)
            except rules.RuleError:
                errs.append("That date is after today's exchange date. An event cannot be recorded in the future; record it on or "
                            "after the expiration date.")
        plan.fields["expiry_date"] = on_raw
        to = S.EXPIRED
        fields = {"confirmed": True, "closed_at_utc": when2}
        plan.state_change = f"{p.state.value} -> EXPIRED"
        plan.lines = [f"Expiration date on record: {p.expiration}. The ledger refuses an expiry dated before it."]
        plan.checks.append(Check("ack_expired", "I verified the put expired with no late exercise or assignment outstanding."))
    if not errs:
        probe = replace(p, state=S.CLOSE_WORKING) if (act == CLOSE_FILLED and p.state is S.OPEN) else p   # OPEN is closed via CLOSE_WORKING
        try:
            validate_transition(probe, to, {k: v for k, v in fields.items() if v is not None})
        except TransitionError as exc:
            errs.append(str(exc))
    return plan


# ---------------------------------------------------------------- check-in
def _plan_checkin(j: Journal, policy: Policy, form, now, plan: Plan) -> Plan:
    errs = plan.errors
    eq = _d(_get(form, "equity"), _MONEY, "Total equity", errs, positive=True)
    cash = _d(_get(form, "free_cash"), _MONEY, "NET free cash", errs)
    plan.fields.update({"equity": _get(form, "equity"), "free_cash": _get(form, "free_cash")})
    book = j.load_book()
    marks: list[tuple[str, Decimal]] = []
    lots = {lot.lot_id: lot for lot in book.stock_lots}
    n = _get(form, "nlots")
    if _COUNT.match(n) and int(n) > MAX_LOTS:
        errs.append(f"More than {MAX_LOTS} stock lots on record: use the CLI check-in for this one.")
    n_i = int(n) if _COUNT.match(n) and int(n) <= MAX_LOTS else 0
    plan.fields["nlots"] = str(n_i)
    for i in range(n_i):
        lot_id, raw = _get(form, f"lot_{i}"), _get(form, f"mark_{i}")
        plan.fields[f"lot_{i}"], plan.fields[f"mark_{i}"] = lot_id, raw
        if raw == "":
            continue
        if lot_id not in lots:
            errs.append("A stock lot in the form no longer exists.")
            continue
        m = _d(raw, _PRICE, f"Mark for {lot_id}", errs, positive=True)
        if m is not None:
            marks.append((lot_id, m))
    committed = rules.committed_collateral(book.positions)
    if eq is not None and cash is not None:
        try:
            rules.check_cash(committed, eq, cash)
        except rules.RuleError as exc:
            errs.append(str(exc))
    plan.checks.append(Check("ack_reconciled", "I confirm the positions, orders, shares, cash and equity listed on this page are "
                                               "complete and match my broker records."))
    plan.state_change = "new confirmed account snapshot (the CLI `daily-check-in`)"
    plan.lines = [f"To record: equity {eq}, NET free cash {cash}, stock marks {', '.join(f'{k}={v}' for k, v in marks) or 'none'}, "
                  f"expected put collateral reserved ${committed:,.2f}."]
    if eq is not None and cash is not None and not errs:
        snap = AccountSnapshot("preview", now, eq, cash, True, "daily-check-in")
        hyp = replace(book, snapshots=book.snapshots + (snap,))
        rep: ReconcileReport = reconcile(hyp, policy, now)
        plan.still_blocked = sorted({i.code for i in rep.issues if i.severity == "BLOCK"})
    plan.commitment_changed = False
    return plan


# ---------------------------------------------------------------- confirm
def _clean_reason(raw: Any) -> str:
    return clean_text(raw, MAX_REASON)


def record(j: Journal, policy: Policy, approved: list[ApprovedUnderlying] | None, form: Mapping[str, str], *, now: datetime,
           tz: str, bound_revision: int, bound_policy_sha: str, reason: str, acks: frozenset[str]) -> TradeResult:
    """Write the plan described by `form`, or nothing. Runs in ONE transaction: idempotency check, stale-revision check,
    re-validation, the domain calls (the same ones the CLI makes) and the audit events. Never raises for owner-fixable
    problems; any unexpected domain error becomes the canned `rejected` result and rolls everything back."""
    reason = _clean_reason(reason)
    try:
        with j.tx():
            idem = _get(form, "idem")
            if _IDEM.match(idem):
                done = j.audit_find("WEB_TRADE_RECORDED", "idempotency_key", idem)
                if done is not None:
                    return TradeResult("duplicate", done.get("trade_id"), done.get("state"), False)
            if policy.sha256 != bound_policy_sha or _revision(j) != bound_revision:
                return TradeResult("stale")
            plan = build_plan(j, policy, approved, form, now, tz)
            if plan.unsupported:
                return TradeResult("unsupported")
            if plan.errors:
                return TradeResult("invalid")
            if not reason:
                return TradeResult("reason_required")
            if any(c.name not in acks for c in plan.checks):
                return TradeResult("ack_required")
            state = _apply(j, plan, policy, approved, form, now, tz, reason)
            j.audit_manual(plan.action, ACTOR, reason, {"trade_id": plan.trade_id, "idempotency_key": plan.fields["idem"]}, SOURCE)
            if plan.breach_codes:
                j.audit_event("POLICY_BREACH_RECORDED", ACTOR,
                              {"trade_id": plan.trade_id, "codes": plan.breach_codes, "idempotency_key": plan.fields["idem"]}, SOURCE)
            sweep_breaches(j, policy, approved, now, reason=f"re-checked after {plan.action}")
            j.audit_event("WEB_TRADE_RECORDED", ACTOR, {"idempotency_key": plan.fields["idem"], "action": plan.action,
                                                        "trade_id": plan.trade_id, "state": state,
                                                        "contract_info_codes": plan.info_codes}, SOURCE)
            return TradeResult("recorded", plan.trade_id, state, plan.commitment_changed, tuple(plan.breach_codes))
    except (JournalError, TransitionError, rules.RuleError, ValueError, MoneyError):
        return TradeResult("rejected")


def _apply(j: Journal, plan: Plan, policy: Policy, approved, form, now: datetime, tz: str, reason: str) -> str | None:
    f, a, tid = plan.fields, plan.action, plan.trade_id
    if a == CHECKIN:
        book = j.load_book()
        for i in range(int(f.get("nlots", "0"))):
            if f.get(f"mark_{i}"):
                j.mark_stock_lot(f[f"lot_{i}"], dec(f[f"mark_{i}"]), now, actor=ACTOR, source=SOURCE)
        eq, cash = dec(f["equity"]), dec(f["free_cash"])
        rules.check_cash(rules.committed_collateral(book.positions), eq, cash)
        j.add_snapshot(AccountSnapshot(str(uuid.uuid5(uuid.NAMESPACE_URL, "csp-web-checkin:" + f["idem"])), now, eq, cash, True,
                                       "daily-check-in"), actor=ACTOR, source=SOURCE)
        return None
    if a in NEW_POSITION_ACTIONS:
        ap = next((x for x in (approved or []) if x.ticker == f["symbol"]), None)
        sector, bucket, _t, _ap, _u = _labels(approved, f["symbol"], f, [])
        note = clean_text(f"web-recorded; {reason}", 200)
        n = NewPosition(tid, f["symbol"], date.fromisoformat(f["expiry"]), dec(f["strike"]), MVP_MULTIPLIER, MVP_CONTRACTS, sector,
                        bucket, account_scope=policy.account_scope, notes=note)
        j.create_draft(n, actor=ACTOR, source=SOURCE)
        j.transition(tid, S.PENDING, actor=ACTOR, source=SOURCE)
        if a == SELL_WORKING:
            j.transition(tid, S.WORKING, actor=ACTOR, source=SOURCE)
            return S.WORKING.value
        j.transition(tid, S.OPEN, actor=ACTOR, source=SOURCE, fill_contracts=1, actual_open_credit_per_share=dec(f["credit"]),
                     opened_at_utc=_when(f, tz, now, [], "x"), fees_usd=dec(f["fees"]))
        return S.OPEN.value
    p = j.get_position(tid)
    if a == MARK_FILLED:
        j.transition(tid, S.OPEN, actor=ACTOR, source=SOURCE, fill_contracts=p.contracts, actual_open_credit_per_share=dec(f["credit"]),
                     opened_at_utc=_when(f, tz, now, [], "x"), fees_usd=dec(f["fees"]))
        return S.OPEN.value
    if a == CLOSE_ORDER:
        j.transition(tid, S.CLOSE_WORKING, actor=ACTOR, source=SOURCE)
        return S.CLOSE_WORKING.value
    if a == CLOSE_FILLED:
        if p.state is S.OPEN:
            j.transition(tid, S.CLOSE_WORKING, actor=ACTOR, source=SOURCE)
        j.transition(tid, S.CLOSED, actor=ACTOR, source=SOURCE, actual_close_debit_per_share=dec(f["debit"]),
                     closed_at_utc=_when(f, tz, now, [], "x"), fees_usd=p.fees_usd + dec(f["fees"]))
        return S.CLOSED.value
    if a == CANCEL_OPEN:
        j.transition(tid, S.CANCELED, actor=ACTOR, source=SOURCE, confirmed=True)
        return S.CANCELED.value
    if a == CANCEL_CLOSE:
        j.transition(tid, S.OPEN, actor=ACTOR, source=SOURCE)
        return S.OPEN.value
    when = rules.event_time(now, date.fromisoformat(f["expiry_date"]), refuse_future_date=True)
    j.transition(tid, S.EXPIRED, actor=ACTOR, source=SOURCE, confirmed=True, closed_at_utc=when)
    return S.EXPIRED.value
