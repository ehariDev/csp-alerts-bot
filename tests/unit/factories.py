"""Synthetic builders for unit tests. ALL VALUES ARE SYNTHETIC; none are market data."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from csp_alerts.journal.models import (
    AccountSnapshot,
    BookState,
    PositionState,
    PutPosition,
    StockLot,
)
from csp_alerts.market.models import MarketObservation, SessionInfo, SessionStatus
from csp_alerts.policy.approved import ApprovedUnderlying
from csp_alerts.policy.config import Policy, load_policy

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 30, 15, 5, tzinfo=timezone.utc)  # 11:05 ET, a Friday
TODAY = date(2026, 10, 30)
OPEN_SESSION = SessionInfo(SessionStatus.OPEN, minutes_to_close=300)


def policy(**pilot_over) -> Policy:
    p = load_policy(ROOT / "config" / "policy.yaml")
    if pilot_over:
        p = p.model_copy(update={"pilot": p.pilot.model_copy(update=pilot_over)})
    return p


def obs(**over) -> MarketObservation:
    base = dict(
        underlying="EXAMPLE", expiration=TODAY + timedelta(days=40), strike=D("75.00"), multiplier=100,
        option_right="P", spot=D("80.00"), bid=D("0.50"), ask=D("0.54"), delta=D("-0.130"),
        implied_volatility=D("0.24"), open_interest=1000, daily_volume=100,
        last_option_trade_at_utc=NOW - timedelta(hours=1), quote_at_utc=None, retrieved_at_utc=NOW,
    )
    base.update(over)
    return MarketObservation(**base)


def approval(**over) -> ApprovedUnderlying:
    base = dict(
        ticker="EXAMPLE", asset_type="STOCK", sector="consumer_staples", correlation_bucket="defensive_consumer",
        max_buy_strike=D("75.00"), approved_through=date(2026, 12, 31), events_reviewed_on=date(2026, 10, 30),
        events_cleared_through=date(2027, 1, 31), ownership_reason="synthetic", approved_by="owner",
    )
    base.update(over)
    return ApprovedUnderlying(**base)


def snap(equity="100000", cash="50000", asof=None, confirmed=True, sid="s1") -> AccountSnapshot:
    return AccountSnapshot(sid, asof or NOW - timedelta(hours=1), D(equity), D(cash), confirmed, "v1")


def put(trade_id="T1", ticker="AAA", strike="50", state=PositionState.OPEN, contracts=1, sector="sec_a",
        bucket="bkt_a", exp=None, asset_type="STOCK", multiplier=100, **over) -> PutPosition:
    base = dict(
        trade_id=trade_id, account_scope="paper_primary", paper=True, ticker=ticker,
        expiration=exp or TODAY + timedelta(days=40), strike=D(strike), multiplier=multiplier, contracts=contracts,
        sector=sector, correlation_bucket=bucket, state=state, asset_type=asset_type,
        opened_at_utc=NOW - timedelta(days=5), actual_open_credit_per_share=D("0.50"),
        last_reconciled_at_utc=NOW - timedelta(days=5),
    )
    base.update(over)
    return PutPosition(**base)


def lot(lot_id="L1", ticker="AAA", shares="100", price="50", sector="sec_a", bucket="bkt_a", asof=None,
        asset_type="STOCK", **over) -> StockLot:
    base = dict(lot_id=lot_id, ticker=ticker, shares=D(shares), reference_price=D(price),
                reference_asof_utc=asof or NOW - timedelta(hours=2), sector=sector, correlation_bucket=bucket,
                acquisition_kind="PURCHASE", asset_type=asset_type)
    base.update(over)
    return StockLot(**base)


def book(positions=(), lots=(), snaps=None, last_change=None, reset=None, baseline=D("100000")) -> BookState:
    return BookState(
        positions=tuple(positions), stock_lots=tuple(lots),
        snapshots=tuple(snaps if snaps is not None else [snap()]),
        last_ledger_change_utc=last_change, last_risk_reset_utc=reset, baseline_equity_usd=baseline,
    )


__all__ = ["replace"]
