"""Exit triggers for open short puts (spec 8.1). Pure functions; advisory output only.

Formulas (per share, Decimal)
  captured_fraction = (actual_open_credit - current_buyback_ask) / actual_open_credit
  PROFIT_EXIT       : captured_fraction >= 0.60            (60% of ORIGINAL premium earned)
  LOSS_REVIEW       : current_buyback_ask >= 2.0 * actual_open_credit
  DELTA_REVIEW      : abs(current_delta) >= 0.30
  TIME_EXIT         : calendar DTE <= 21     (calendar-only; fires with no quote and a closed market)
  ASSIGNMENT_REVIEW : DTE <= 7 or spot < strike (ITM); detail flags final_day when DTE <= 1
  est_gross_pnl_usd = (actual_open_credit - ask) * multiplier * contracts      (indicative)
  est_net_pnl_usd   = est_gross_pnl_usd - fees_usd
The buyback price is the ASK, never the midpoint or last print. Numeric triggers are suppressed
(and DATA_ISSUE emitted) when no valid current ask exists, when the bid is missing/zero/non-finite (one-sided quote),
when the quote is crossed/stale/for another contract, or when the row's contract identity is contradictory
(quote.identity_problem); calendar triggers are not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from csp_alerts.journal.models import MONITORED_STATES, PositionState, PutPosition
from csp_alerts.journal.reconcile import exchange_date
from csp_alerts.money import fmt
from csp_alerts.policy.config import Policy


class Trigger(StrEnum):
    PROFIT_EXIT = "PROFIT_EXIT"
    TIME_EXIT = "TIME_EXIT"
    LOSS_REVIEW = "LOSS_REVIEW"
    DELTA_REVIEW = "DELTA_REVIEW"
    DATA_ISSUE = "DATA_ISSUE"
    ASSIGNMENT_REVIEW = "ASSIGNMENT_REVIEW"
    ADJUSTMENT_REVIEW = "ADJUSTMENT_REVIEW"


SEVERITY = {
    Trigger.PROFIT_EXIT: "ACTION",
    Trigger.TIME_EXIT: "ACTION",
    Trigger.LOSS_REVIEW: "URGENT",
    Trigger.DELTA_REVIEW: "URGENT",
    Trigger.ASSIGNMENT_REVIEW: "URGENT",
    Trigger.DATA_ISSUE: "WARN",
    Trigger.ADJUSTMENT_REVIEW: "URGENT",
}


@dataclass(frozen=True)
class OptionQuote:
    """Current quote for the position's own contract (None fields = unavailable)."""
    contract_key: str
    retrieved_at_utc: datetime | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    delta: Decimal | None = None
    spot: Decimal | None = None
    # Set by the caller when the upstream row's identity contradicts the position (call/put, symbol, ambiguity).
    identity_problem: str | None = None


@dataclass(frozen=True)
class ExitSignal:
    trigger: Trigger
    severity: str
    trade_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"trigger": self.trigger.value, "severity": self.severity, "trade_id": self.trade_id,
                "detail": {k: (fmt(v) if isinstance(v, Decimal) else v) for k, v in self.detail.items()}}


def _fin(v: Decimal | None) -> bool:
    return v is not None and v.is_finite()


def dte_calendar(pos: PutPosition, now_utc: datetime) -> int:
    return (pos.expiration - exchange_date(now_utc)).days


def estimated_pnl_usd(pos: PutPosition, ask: Decimal) -> tuple[Decimal, Decimal]:
    """(gross, net of recorded fees) indicative P&L if bought to close at `ask`."""
    assert pos.actual_open_credit_per_share is not None
    gross = (pos.actual_open_credit_per_share - ask) * pos.multiplier * pos.contracts
    return gross, gross - pos.fees_usd


def evaluate_exits(
    pos: PutPosition,
    quote: OptionQuote | None,
    policy: Policy,
    now_utc: datetime,
    *,
    max_quote_age: timedelta | None = None,
) -> list[ExitSignal]:
    """Triggers for one position. Only OPEN / CLOSE_WORKING / ADJUSTMENT_REVIEW positions are monitored.

    max_quote_age defaults to 2 x the monitor interval; an older (or untimed) quote is treated as
    stale and suppresses numeric triggers.
    """
    if pos.state not in MONITORED_STATES:
        return []
    x = policy.exits
    out: list[ExitSignal] = []
    dte = dte_calendar(pos, now_utc)
    if max_quote_age is None:
        max_quote_age = timedelta(minutes=2 * policy.market.exit_monitor_interval_minutes)
    credit = pos.actual_open_credit_per_share

    if pos.state is PositionState.ADJUSTMENT_REVIEW:
        out.append(ExitSignal(Trigger.ADJUSTMENT_REVIEW, SEVERITY[Trigger.ADJUSTMENT_REVIEW], pos.trade_id,
                              {"notice": "contract may be non-standard (split/merger/adjusted deliverable); "
                                         "all numbers need manual verification"}))

    # ---- calendar-based triggers: never depend on quotes or market hours ----
    if dte <= x.close_review_at_dte_calendar:
        out.append(ExitSignal(Trigger.TIME_EXIT, SEVERITY[Trigger.TIME_EXIT], pos.trade_id, {"dte": dte}))
    itm = quote is not None and _fin(quote.spot) and quote.spot is not None and quote.spot < pos.strike
    if dte <= x.assignment_warning_dte or itm:  # ITM on any (even stale) spot is a conservative warning
        out.append(ExitSignal(Trigger.ASSIGNMENT_REVIEW, SEVERITY[Trigger.ASSIGNMENT_REVIEW], pos.trade_id,
                              {"dte": dte, "itm_on_indicative_quote": bool(itm),
                               "final_day": dte <= 1}))

    if not x.tier_r_price_alerts_enabled:  # owner switch (IT-005): calendar triggers above still fire
        return out

    # ---- quote validity ----
    problems: list[str] = []
    if credit is None or not credit.is_finite() or credit <= 0:
        problems.append("no recorded opening credit; cannot compute P&L")
    if quote is None:
        problems.append("no quote available")
    else:
        if quote.contract_key != pos.contract_key:
            problems.append("quote is for a different contract")
        if quote.retrieved_at_utc is None or not (timedelta(0) <= now_utc - quote.retrieved_at_utc <= max_quote_age):
            problems.append("quote is stale or untimed")
        if quote.identity_problem:
            problems.append(quote.identity_problem)
        if not _fin(quote.ask) or quote.ask is None or quote.ask <= 0:
            problems.append("missing or invalid ask")
        elif not _fin(quote.bid) or quote.bid is None or quote.bid <= 0:
            # SR-01: a one-sided or zero bid is not a trustworthy two-sided quote; no buyback-price advice from it
            problems.append("missing, zero or invalid bid (one-sided quote)")
        elif quote.ask < quote.bid:
            problems.append("crossed quote (ask < bid)")
    if problems:
        out.append(ExitSignal(Trigger.DATA_ISSUE, SEVERITY[Trigger.DATA_ISSUE], pos.trade_id,
                              {"problems": problems, "action": "check broker manually; cannot estimate exit price"}))
        # Delta may still be usable only if the quote itself is valid; with any problem, suppress numeric triggers.
        return out

    assert quote is not None and credit is not None and quote.ask is not None
    ask = quote.ask
    captured = (credit - ask) / credit
    gross, net = estimated_pnl_usd(pos, ask)
    base = {"actual_open_credit": credit, "buyback_ask": ask, "est_gross_pnl_usd": gross, "est_net_pnl_usd": net,
            "contracts": pos.contracts}

    # Cross-multiplied so the 60% / 2x boundaries are exact.
    if (credit - ask) >= x.profit_captured_fraction * credit:
        out.append(ExitSignal(Trigger.PROFIT_EXIT, SEVERITY[Trigger.PROFIT_EXIT], pos.trade_id,
                              {**base, "captured_fraction": captured}))
    if ask >= x.loss_buyback_ask_multiple * credit:
        out.append(ExitSignal(Trigger.LOSS_REVIEW, SEVERITY[Trigger.LOSS_REVIEW], pos.trade_id,
                              {**base, "ask_multiple": ask / credit}))
    if _fin(quote.delta) and quote.delta is not None and abs(quote.delta) >= x.delta_review_abs:
        out.append(ExitSignal(Trigger.DELTA_REVIEW, SEVERITY[Trigger.DELTA_REVIEW], pos.trade_id,
                              {**base, "delta": quote.delta, "delta_source": "model (verify independently)"}))
    return out
