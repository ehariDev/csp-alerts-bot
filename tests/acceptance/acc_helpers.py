"""Independent acceptance helpers (tester-owned). Built from the spec, not from tests/unit/factories.py.

Reference scenario (all synthetic):
  NOW = 2026-10-30T15:05:02Z  -> 11:05 EDT, exchange date 2026-10-30 (Friday). DST ends 2026-11-01.
  DTE 35 -> 2026-12-04 (Fri, ISO week 49)   [Oct 30 + 1 = Oct 31; + 30 (Nov) = Nov 30 is +31; + 4 = Dec 4 -> 35]
  DTE 45 -> 2026-12-14 (Mon, ISO week 51)
  DTE 34 -> 2026-12-03, DTE 46 -> 2026-12-15
"""
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
from csp_alerts.policy.config import load_policy, parse_policy

REPO = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO / "config" / "policy.yaml"

NOW = datetime(2026, 10, 30, 15, 5, 2, tzinfo=timezone.utc)
TODAY = date(2026, 10, 30)
OPEN_SESSION = SessionInfo(SessionStatus.OPEN, 120)
S = PositionState


def policy(**pilot_over):
    p = load_policy(POLICY_PATH)
    if pilot_over:
        data = p.model_dump(mode="json")
        data["pilot"].update(pilot_over)
        p = parse_policy(data)
    return p


def policy_no_pilot():
    return policy(enabled=False)


def exp_for_dte(dte: int, today: date = TODAY) -> date:
    return today + timedelta(days=dte)


def approval(ticker="AAA", asset_type="STOCK", sector="tech", bucket="b1", max_buy="100.00", today: date = TODAY,
             **over) -> ApprovedUnderlying:
    d = dict(
        ticker=ticker, asset_type=asset_type, sector=sector, correlation_bucket=bucket,
        max_buy_strike=max_buy, approved_through=(today + timedelta(days=60)).isoformat(),
        events_reviewed_on=today.isoformat(), events_cleared_through=(today + timedelta(days=90)).isoformat(),
        ownership_reason="synthetic test", approved_by="owner",
    )
    d.update(over)
    return ApprovedUnderlying(**d)


def obs(**over) -> MarketObservation:
    """Base: strike 75, spot 80, bid .50 ask .54 (spread .04, mid .52 -> 7.7%), delta -0.13, OI 1000, vol 100, DTE 35."""
    d = dict(
        underlying="AAA", expiration=exp_for_dte(35), strike=D("75.00"), multiplier=100, option_right="P",
        spot=D("80.00"), bid=D("0.50"), ask=D("0.54"), delta=D("-0.13"), open_interest=1000, daily_volume=100,
        last_option_trade_at_utc=NOW - timedelta(minutes=25), quote_at_utc=None, retrieved_at_utc=NOW,
        earnings_in_window=False,
    )
    d.update(over)
    return MarketObservation(**d)


def snap(equity="100000", cash="50000", asof=None, confirmed=True, sid="s1") -> AccountSnapshot:
    return AccountSnapshot(sid, asof or NOW - timedelta(hours=2), D(equity), D(cash), confirmed, "v1")


def put(trade_id="P1", ticker="BBB", strike="50.00", state=S.OPEN, expiration=None, contracts=1, multiplier=100,
        sector="s_other", bucket="b_other", asset_type="ETF", credit="0.50", **over) -> PutPosition:
    filled = state in (S.OPEN, S.CLOSE_WORKING, S.CLOSED, S.EXPIRED, S.ASSIGNED_PENDING_RECONCILIATION,
                       S.ASSIGNED_RECONCILED, S.ADJUSTMENT_REVIEW)
    d = dict(
        trade_id=trade_id, account_scope="paper_primary", paper=True, ticker=ticker,
        expiration=expiration or date(2026, 11, 20), strike=D(strike), multiplier=multiplier, contracts=contracts,
        sector=sector, correlation_bucket=bucket, state=state, asset_type=asset_type,
        opened_at_utc=NOW - timedelta(days=5) if filled else None,
        actual_open_credit_per_share=D(credit) if filled else None,
    )
    d.update(over)
    return PutPosition(**d)


def lot(lot_id="L1", ticker="CCC", shares="100", price="40.00", sector="s_lot", bucket="b_lot", asset_type="ETF",
        asof=None, linked=None, cost=None) -> StockLot:
    return StockLot(lot_id, ticker, D(shares), D(price), asof or NOW - timedelta(hours=1), sector, bucket,
                    "OWNED", asset_type, None if cost is None else D(cost), linked)


BASELINE = D("100000")
_UNSET = object()


def book(positions=(), lots=(), snaps=None, last_change=None, last_reset=None, baseline=_UNSET) -> BookState:
    """Default book carries the owner-confirmed $100,000 baseline (owner decision 2). Pass baseline=None for none."""
    b = BASELINE if baseline is _UNSET else baseline
    return BookState(tuple(positions), tuple(lots), tuple(snaps if snaps is not None else [snap()]), last_change,
                     last_reset, b)


def run(o=None, a="default", b=None, pol=None, now=NOW, session=OPEN_SESSION, contracts=1):
    from csp_alerts.policy.entry import evaluate_entry
    return evaluate_entry(obs() if o is None else o, approval() if a == "default" else a,
                          book() if b is None else b, pol or policy(), now, session, contracts=contracts)


def codes(dec) -> set[str]:
    return {c.value for c in dec.reason_codes}
