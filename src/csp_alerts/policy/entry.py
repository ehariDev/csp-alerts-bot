"""Deterministic entry gate (spec 4.4, 6.3, 7.1). Pure: no I/O, no clock reads, no network.

Every gate is evaluated (no short-circuit) so the decision lists ALL reason codes. Anything
unknown or non-finite blocks. No threshold is ever relaxed implicitly.

Formulas (per share unless noted)
  dte               = expiration - exchange-local (America/New_York) date of `now_utc`, calendar days
  mid               = (bid + ask) / 2            (used only for the spread ratio, never as the premium)
  spread            = ask - bid
  premium_at_bid    = bid * multiplier * contracts          (indicative; fees/slippage separate)
  collateral        = strike * multiplier * contracts
  breakeven         = strike - bid                          (before fees; research alert uses indicative bid)
  gross_bid_yield_per_calendar_day = 100 * bid / (strike * dte)   (ordering indicator, NOT a return forecast)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from csp_alerts.journal.models import BookState
from csp_alerts.journal.reconcile import exchange_date, ledger_reason_codes, reconcile
from csp_alerts.market.models import TIER_R, TIER_V, MarketObservation, SessionInfo, SessionStatus
from csp_alerts.money import fmt
from csp_alerts.policy.approved import ApprovedUnderlying
from csp_alerts.policy.config import Policy
from csp_alerts.policy.drawdown import DrawdownLevel, compute_drawdown
from csp_alerts.policy.portfolio import ProposedPut, check_portfolio_limits, compute_exposure
from csp_alerts.policy.reasons import Decision, Reason, ordered

TWO = Decimal("2")


@dataclass(frozen=True)
class GateDecision:
    candidate_key: str
    policy_version: str
    decision: Decision
    reason_codes: tuple[Reason, ...]
    exposure_before_usd: Decimal | None
    additional_collateral_usd: Decimal | None
    exposure_after_usd: Decimal | None
    manual_free_cash_after_usd: Decimal | None
    data_tier: str
    requires_broker_manual_quote_check: bool
    evaluated_at_utc: datetime
    cautions: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.decision in (Decision.PASS_RESEARCH_ONLY, Decision.PASS_VERIFIED_DATA)

    def to_dict(self) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return None if v is None else fmt(v)
        return {
            "candidate_key": self.candidate_key,
            "policy_version": self.policy_version,
            "decision": self.decision.value,
            "reason_codes": [r.value for r in self.reason_codes],
            "exposure_before_usd": s(self.exposure_before_usd),
            "additional_collateral_usd": s(self.additional_collateral_usd),
            "exposure_after_usd": s(self.exposure_after_usd),
            "manual_free_cash_after_usd": s(self.manual_free_cash_after_usd),
            "data_tier": self.data_tier,
            "requires_broker_manual_quote_check": self.requires_broker_manual_quote_check,
            "evaluated_at_utc": self.evaluated_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cautions": list(self.cautions),
            "metrics": {k: (fmt(v) if isinstance(v, Decimal) else v) for k, v in self.metrics.items()},
        }


def book_entry_blocks(book: BookState, policy: Policy, now_utc: datetime):
    """Ledger + drawdown reason codes that block ALL new entries (independent of any contract)."""
    codes: set[Reason] = {Reason(c) for c in ledger_reason_codes(reconcile(book, policy, now_utc))}
    dd = compute_drawdown(book.snapshots, policy, baseline_asof_utc=book.last_risk_reset_utc,
                          baseline_equity_usd=book.baseline_equity_usd, cash_flows=book.cash_flows)
    if dd.level is DrawdownLevel.HARD_HALT:
        codes.add(Reason.HARD_HALT)
    elif dd.level in (DrawdownLevel.PAUSE, DrawdownLevel.REDUCE_REVIEW):
        codes.add(Reason.DRAWDOWN_PAUSE)
    return codes, dd


def _check_types(o: MarketObservation) -> None:
    """Deliberate TypeError for float/str money inputs and non-int counts (never silently coerce)."""
    for name in ("strike", "spot", "bid", "ask", "delta"):
        v = getattr(o, name)
        if v is not None and not isinstance(v, Decimal):
            raise TypeError(f"MarketObservation.{name} must be Decimal or None, got {type(v).__name__}")
    for name in ("multiplier", "open_interest", "daily_volume"):
        v = getattr(o, name)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise TypeError(f"MarketObservation.{name} must be int or None, got {type(v).__name__}")


def _fin(v: Decimal | None) -> bool:
    return v is not None and v.is_finite()


def evaluate_entry(
    obs: MarketObservation | None,
    approval: ApprovedUnderlying | None,
    book: BookState,
    policy: Policy,
    now_utc: datetime,
    session: SessionInfo,
    *,
    contracts: int = 1,
) -> GateDecision:
    """Run every hard gate for one candidate short put. `PASS_*` means "eligible for research alert",
    never "approved to trade"."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    if obs is not None:
        _check_types(obs)
    e = policy.entries
    codes: set[Reason] = set()
    cautions: list[str] = []
    metrics: dict[str, Any] = {}
    today = exchange_date(now_utc)

    def done(decision: Decision, key: str, tier: str, before=None, add=None, after=None, cash_after=None) -> GateDecision:
        return GateDecision(
            candidate_key=key, policy_version=policy.policy_version, decision=decision,
            reason_codes=tuple(ordered(codes)), exposure_before_usd=before, additional_collateral_usd=add,
            exposure_after_usd=after, manual_free_cash_after_usd=cash_after, data_tier=tier,
            requires_broker_manual_quote_check=True, evaluated_at_utc=now_utc,
            cautions=tuple(cautions), metrics=metrics,
        )

    if obs is None:
        codes.add(Reason.MISSING_MARKET_DATA)
        return done(Decision.DATA_UNAVAILABLE, "UNKNOWN", TIER_R)

    # ---- session ----
    if session.status is SessionStatus.UNKNOWN:
        codes.add(Reason.UNKNOWN_SESSION)
    elif session.status is SessionStatus.CLOSED:
        codes.add(Reason.MARKET_CLOSED)
    else:
        if session.minutes_to_close is None:
            codes.add(Reason.UNKNOWN_SESSION)
        elif session.minutes_to_close < policy.market.no_entry_in_final_minutes_of_session:
            codes.add(Reason.MARKET_CLOSED)

    # ---- ledger / account ----
    book_codes, dd = book_entry_blocks(book, policy, now_utc)
    codes.update(book_codes)
    metrics["drawdown_level"] = dd.level.value
    metrics["drawdown_latched"] = dd.latched
    if dd.drawdown is not None:
        metrics["drawdown"] = dd.drawdown

    # ---- approval / valuation / events ----
    if approval is None or approval.ticker != obs.underlying:
        codes.add(Reason.NOT_APPROVED)
    else:
        if approval.approved_through < today:
            codes.add(Reason.APPROVAL_EXPIRED)
        age = (today - approval.events_reviewed_on).days
        if age < 0 or age > e.manual_event_review_max_age_calendar_days:
            codes.add(Reason.EVENT_UNVERIFIED)
        if obs.expiration is not None and approval.events_cleared_through < obs.expiration:
            codes.add(Reason.EVENT_UNVERIFIED)
        if obs.strike is not None and _fin(obs.strike) and obs.strike > approval.max_buy_strike:
            codes.add(Reason.VALUATION_TOO_HIGH)
    if obs.earnings_in_window is True:
        codes.add(Reason.EARNINGS_IN_WINDOW)
    if obs.earnings_in_window is None:
        cautions.append("provider earnings data unknown; relying on owner event clearance only")

    # ---- contract identity ----
    if obs.expiration is None or not _fin(obs.strike) or obs.multiplier is None or obs.option_right is None:
        codes.add(Reason.MISSING_MARKET_DATA)
        if obs.multiplier is None:
            codes.add(Reason.NONSTANDARD_CONTRACT)  # unverified deliverable is never assumed standard (spec 4.3)
    else:
        if obs.option_right != "P" or obs.multiplier != 100:
            codes.add(Reason.NONSTANDARD_CONTRACT)

    # ---- DTE ----
    dte: int | None = None
    if obs.expiration is not None:
        dte = (obs.expiration - today).days
        metrics["dte"] = dte
        if not (e.min_dte_calendar <= dte <= e.max_dte_calendar):
            codes.add(Reason.DTE_OUT_OF_RANGE)

    # ---- delta ----
    if obs.delta is None:
        codes.add(Reason.MISSING_MARKET_DATA)
    elif not obs.delta.is_finite() or obs.delta >= 0 or not (e.min_abs_delta <= abs(obs.delta) <= e.max_abs_delta):
        codes.add(Reason.DELTA_OUT_OF_RANGE)

    # ---- spot / moneyness ----
    if obs.spot is None:
        codes.add(Reason.MISSING_MARKET_DATA)
    elif not _fin(obs.spot) or obs.spot <= 0:
        codes.add(Reason.MISSING_MARKET_DATA)
    elif _fin(obs.strike) and obs.strike is not None and not obs.strike < obs.spot:
        codes.add(Reason.STRIKE_NOT_BELOW_SPOT)

    # ---- liquidity ----
    if obs.open_interest is None or obs.daily_volume is None:
        codes.add(Reason.MISSING_MARKET_DATA)
    else:
        if obs.open_interest < e.min_open_interest:
            codes.add(Reason.INSUFFICIENT_OI)
        if obs.daily_volume < e.min_daily_volume:
            codes.add(Reason.INSUFFICIENT_VOLUME)

    # ---- bid / ask / spread ----
    bid, ask = obs.bid, obs.ask
    if bid is None or ask is None:
        codes.add(Reason.MISSING_MARKET_DATA)
        codes.add(Reason.BAD_BID_ASK)
    elif not (_fin(bid) and _fin(ask)) or bid <= 0 or ask <= 0 or ask < bid:
        codes.add(Reason.BAD_BID_ASK)
    else:
        if bid < e.min_bid_usd_per_share:
            codes.add(Reason.BAD_BID_ASK)
        spread = ask - bid
        mid = (bid + ask) / TWO
        metrics["spread"] = spread
        metrics["spread_fraction_of_mid"] = spread / mid
        if spread > e.max_absolute_spread_usd_per_share or spread > e.max_fractional_spread_of_mid * mid:
            codes.add(Reason.SPREAD_TOO_WIDE)

    # ---- last option trade print (weak secondary check; never a quote timestamp) ----
    if obs.last_option_trade_at_utc is None:
        codes.add(Reason.STALE_LAST_TRADE)  # spec 15.1 #13: missing last print blocks
    else:
        age = now_utc - obs.last_option_trade_at_utc
        age_seconds = Decimal(age // timedelta(seconds=1))  # integer seconds; no float
        if age_seconds < 0 or age_seconds > e.max_last_trade_age_calendar_days * 86400:
            codes.add(Reason.STALE_LAST_TRADE)

    # ---- tier / quote timestamp ----
    tier = TIER_R
    if obs.data_tier == TIER_V and obs.quote_at_utc is not None:
        qage = now_utc - obs.quote_at_utc
        if timedelta(0) <= qage <= timedelta(seconds=policy.market.quote_ttl_seconds_if_timestamped):
            tier = TIER_V
        else:
            cautions.append("timestamped quote older than TTL; downgraded to indicative")
    if obs.quote_at_utc is None:
        cautions.append("bid/ask exchange timestamp UNAVAILABLE - verify live")

    # ---- portfolio / capital (only if the contract identity is usable) ----
    before = add = after = cash_after = None
    exp = compute_exposure(book)
    before = exp.aggregate_equity_risk_usd
    if approval is not None and _fin(obs.strike) and obs.strike is not None and obs.expiration is not None and obs.multiplier:
        proposed = ProposedPut(
            ticker=obs.underlying, asset_type=approval.asset_type, sector=approval.sector,
            correlation_bucket=approval.correlation_bucket, expiration=obs.expiration, strike=obs.strike,
            multiplier=obs.multiplier, contracts=contracts,
        )
        snap = book.latest_snapshot
        free_cash = snap.free_unencumbered_cash_usd if snap is not None else None
        codes.update(check_portfolio_limits(book, proposed, policy, free_cash))
        add = proposed.collateral_usd
        after = before + add
        cash_after = None if free_cash is None else free_cash - add
        metrics["collateral_usd"] = add
        if bid is not None and _fin(bid):
            metrics["premium_at_bid_usd"] = bid * obs.multiplier * contracts
            metrics["breakeven"] = obs.strike - bid
            if dte and dte > 0:
                metrics["gross_bid_yield_per_calendar_day_pct"] = Decimal(100) * bid / (obs.strike * dte)
    elif approval is not None:
        codes.add(Reason.MISSING_MARKET_DATA)

    if not codes:
        decision = Decision.PASS_VERIFIED_DATA if tier == TIER_V else Decision.PASS_RESEARCH_ONLY
        return done(decision, obs.contract_key, tier, before, add, after, cash_after)
    if codes <= {Reason.AI_UNAVAILABLE}:
        decision = Decision.MORE_RESEARCH
    elif Reason.MISSING_MARKET_DATA in codes and codes <= {
        Reason.MISSING_MARKET_DATA, Reason.BAD_BID_ASK, Reason.INSUFFICIENT_OI, Reason.INSUFFICIENT_VOLUME,
        Reason.DELTA_OUT_OF_RANGE, Reason.DTE_OUT_OF_RANGE,
    }:
        decision = Decision.DATA_UNAVAILABLE
    else:
        decision = Decision.BLOCKED
    return done(decision, obs.contract_key, tier, before, add, after, cash_after)
