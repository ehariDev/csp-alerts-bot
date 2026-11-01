"""P0-04: deterministic entry gates (spec 4.4, 6.3, 15.1)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import NOW, OPEN_SESSION, TODAY, approval, book, lot, obs, policy, put, snap

from csp_alerts.journal.models import PositionState
from csp_alerts.market.models import TIER_V, SessionInfo, SessionStatus
from csp_alerts.policy.entry import evaluate_entry
from csp_alerts.policy.reasons import SPEC_REASON_CODES, Decision, Reason

P = policy()


def run(o=None, a="default", b=None, session=OPEN_SESSION, pol=P, now=NOW, **kw):
    return evaluate_entry(
        obs() if o is None else o, approval() if a == "default" else a, b if b is not None else book(), pol, now,
        session, **kw)


def codes(d):
    return {r.value for r in d.reason_codes}


def test_baseline_candidate_passes_research_only_with_spec_6_3_fields():
    d = run()
    assert d.decision is Decision.PASS_RESEARCH_ONLY and d.reason_codes == ()
    assert d.requires_broker_manual_quote_check is True
    assert d.data_tier == "INDICATIVE_UNTIMESTAMPED"
    assert d.additional_collateral_usd == D("7500.00")
    assert d.exposure_before_usd == D("0") and d.exposure_after_usd == D("7500.00")
    assert d.manual_free_cash_after_usd == D("42500")
    j = d.to_dict()
    assert j["decision"] == "PASS_RESEARCH_ONLY" and j["policy_version"] == "1.1.0"
    assert j["metrics"]["premium_at_bid_usd"] == "50.00"
    assert j["metrics"]["breakeven"] == "74.50"
    assert any("UNAVAILABLE" in c for c in d.cautions)


def test_reason_code_enum_covers_spec_list():
    expected = """NOT_APPROVED APPROVAL_EXPIRED VALUATION_TOO_HIGH EVENT_UNVERIFIED EARNINGS_IN_WINDOW DTE_OUT_OF_RANGE
    DELTA_OUT_OF_RANGE INSUFFICIENT_OI INSUFFICIENT_VOLUME BAD_BID_ASK SPREAD_TOO_WIDE MISSING_MARKET_DATA
    NONSTANDARD_CONTRACT INSUFFICIENT_FREE_CASH SINGLE_TICKER_LIMIT SECTOR_LIMIT CORRELATED_LIMIT EXPIRY_WEEK_LIMIT
    CONTRACT_COUNT_LIMIT INDIVIDUAL_NAME_LIMIT NORMAL_EXPOSURE_LIMIT PILOT_COLLATERAL_LIMIT DRAWDOWN_PAUSE HARD_HALT
    LEDGER_STALE LEDGER_UNRECONCILED MARKET_CLOSED UNKNOWN_SESSION AI_VETO AI_UNAVAILABLE""".split()
    assert [r.value for r in SPEC_REASON_CODES] == expected


# ---- spec 15.1 #9 delta ----
@pytest.mark.parametrize("delta,ok", [
    ("-0.10", True), ("-0.15", True), ("-0.1", True), ("-0.16", False), ("-0.09", False), ("0.13", False),
    ("0", False), ("NaN", False), ("-Infinity", False),
])
def test_delta_boundaries(delta, ok):
    d = run(obs(delta=D(delta)))
    assert (Reason.DELTA_OUT_OF_RANGE.value in codes(d)) is (not ok)
    assert d.passed is ok


def test_missing_delta_blocks():
    d = run(obs(delta=None))
    assert not d.passed and "MISSING_MARKET_DATA" in codes(d)


# ---- #10 DTE ----
@pytest.mark.parametrize("days,ok", [(35, True), (45, True), (34, False), (46, False), (0, False), (-3, False)])
def test_dte_boundaries(days, ok):
    d = run(obs(expiration=TODAY + timedelta(days=days)), a=approval(events_cleared_through=TODAY + timedelta(days=400)))
    assert (Reason.DTE_OUT_OF_RANGE.value in codes(d)) is (not ok)


def test_dte_uses_exchange_local_date_across_utc_midnight():
    # 2026-10-31 02:30 UTC is still 2026-10-30 22:30 in New York (EDT), so DTE counts from Oct 30.
    from datetime import datetime, timezone
    late = datetime(2026, 10, 31, 2, 30, tzinfo=timezone.utc)
    d = run(obs(expiration=TODAY + timedelta(days=35)), now=late, session=OPEN_SESSION,
            b=book(snaps=[snap(asof=late - timedelta(hours=1))]))
    assert d.metrics["dte"] == 35 and Reason.DTE_OUT_OF_RANGE.value not in codes(d)


def test_dte_across_dst_fall_back_boundary():
    # US DST ended 2026-11-01. 2026-11-02 12:00 UTC = 07:00 EST; expiry 35 calendar days later.
    from datetime import date, datetime, timezone
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    d = run(obs(expiration=date(2026, 12, 7)), now=now, b=book(snaps=[snap(asof=now - timedelta(hours=1))]),
            a=approval(events_reviewed_on=date(2026, 11, 2)))
    assert d.metrics["dte"] == 35 and Reason.DTE_OUT_OF_RANGE.value not in codes(d)


# ---- #11 liquidity ----
@pytest.mark.parametrize("oi,vol,expect", [
    (500, 50, set()), (499, 50, {"INSUFFICIENT_OI"}), (500, 49, {"INSUFFICIENT_VOLUME"}),
    (700, 0, {"INSUFFICIENT_VOLUME"}),
])
def test_oi_volume(oi, vol, expect):
    assert codes(run(obs(open_interest=oi, daily_volume=vol))) == expect


# ---- #12 spread, #13 bid/ask ----
@pytest.mark.parametrize("bid,ask,expect", [
    ("0.50", "0.55", set()),                      # $0.05 / 9.5% -> ok (boundary absolute)
    ("0.50", "0.56", {"SPREAD_TOO_WIDE"}),        # $0.06 absolute
    ("5.00", "5.06", {"SPREAD_TOO_WIDE"}),        # $0.06 absolute even though 1.2% of mid
    ("0.40", "0.55", {"SPREAD_TOO_WIDE"}),        # spec 4.5 example: $0.15, 31.6% of mid
    ("0.19", "0.21", {"BAD_BID_ASK"}),            # bid below $0.20 minimum
    ("0.20", "0.22", set()),
    ("0", "0.54", {"BAD_BID_ASK"}),
    ("0.50", "0", {"BAD_BID_ASK"}),
    ("0.54", "0.50", {"BAD_BID_ASK"}),            # crossed
    ("NaN", "0.54", {"BAD_BID_ASK"}),
    ("0.50", "Infinity", {"BAD_BID_ASK"}),
])
def test_bid_ask_and_spread(bid, ask, expect):
    assert codes(run(obs(bid=D(bid), ask=D(ask)))) == expect


def test_fractional_spread_blocks_even_when_dollar_spread_ok():
    # spread 0.04 (<= $0.05) but 18% of mid 0.22 -> blocked on the fractional rule
    d = run(obs(bid=D("0.20"), ask=D("0.24")))
    assert "SPREAD_TOO_WIDE" in codes(d)


def test_11_percent_of_midpoint_blocked_even_if_dollar_ok():
    # bid .30 ask .34 -> spread .04 (<= .05) but 12.5% of mid -> blocked
    d = run(obs(bid=D("0.30"), ask=D("0.34")))
    assert codes(d) == {"SPREAD_TOO_WIDE"}


def test_missing_bid_or_ask_blocks():
    assert not run(obs(bid=None)).passed and not run(obs(ask=None)).passed
    assert "MISSING_MARKET_DATA" in codes(run(obs(bid=None)))


def test_stale_or_missing_last_trade():
    assert "STALE_LAST_TRADE" in codes(run(obs(last_option_trade_at_utc=NOW - timedelta(days=1, hours=13))))
    assert "STALE_LAST_TRADE" not in codes(run(obs(last_option_trade_at_utc=NOW - timedelta(days=1, hours=12))))
    d = run(obs(last_option_trade_at_utc=None))  # spec 15.1 #13: missing last print blocks
    assert not d.passed and "STALE_LAST_TRADE" in codes(d)


def test_nonstandard_contract_and_missing_identity():
    assert "NONSTANDARD_CONTRACT" in codes(run(obs(multiplier=10)))
    assert "NONSTANDARD_CONTRACT" in codes(run(obs(option_right="C")))
    d = run(obs(multiplier=None))
    assert not d.passed and "MISSING_MARKET_DATA" in codes(d)


def test_strike_must_be_strictly_below_spot():
    assert "STRIKE_NOT_BELOW_SPOT" in codes(run(obs(spot=D("75.00"))))
    assert "STRIKE_NOT_BELOW_SPOT" in codes(run(obs(spot=D("70.00"))))


def test_no_observation_is_data_unavailable_not_no_trade():
    d = evaluate_entry(None, approval(), book(), P, NOW, OPEN_SESSION)
    assert d.decision is Decision.DATA_UNAVAILABLE and "MISSING_MARKET_DATA" in codes(d)


def test_missing_only_market_fields_is_data_unavailable():
    d = run(obs(bid=None, ask=None, open_interest=None, daily_volume=None))
    assert d.decision is Decision.DATA_UNAVAILABLE


def test_upstream_last_trade_fallback_mid_cannot_pass():
    # an upstream row with no two-sided quote arrives with bid/ask unusable -> blocked
    assert not run(obs(bid=D("0"), ask=D("0"))).passed


# ---- #14 approval / events ----
def test_not_approved_and_mismatch():
    assert "NOT_APPROVED" in codes(run(a=None))
    assert "NOT_APPROVED" in codes(run(a=approval(ticker="OTHER")))


def test_approval_expired_and_boundary():
    assert "APPROVAL_EXPIRED" in codes(run(a=approval(approved_through=TODAY - timedelta(days=1))))
    assert "APPROVAL_EXPIRED" not in codes(run(a=approval(approved_through=TODAY)))


def test_valuation_ceiling():
    d = run(a=approval(max_buy_strike=D("74.99")))
    assert "VALUATION_TOO_HIGH" in codes(d)
    assert "VALUATION_TOO_HIGH" not in codes(run(a=approval(max_buy_strike=D("75.00"))))


def test_event_review_age_and_clearance_window():
    assert "EVENT_UNVERIFIED" not in codes(run(a=approval(events_reviewed_on=TODAY - timedelta(days=7))))
    assert "EVENT_UNVERIFIED" in codes(run(a=approval(events_reviewed_on=TODAY - timedelta(days=8))))
    exp = TODAY + timedelta(days=40)
    assert "EVENT_UNVERIFIED" in codes(run(a=approval(events_cleared_through=exp - timedelta(days=1))))
    assert "EVENT_UNVERIFIED" not in codes(run(a=approval(events_cleared_through=exp)))
    assert "EVENT_UNVERIFIED" in codes(run(a=approval(events_reviewed_on=TODAY + timedelta(days=1))))


def test_earnings_flag_and_unknown_earnings_is_not_clearance():
    assert "EARNINGS_IN_WINDOW" in codes(run(obs(earnings_in_window=True)))
    d = run(obs(earnings_in_window=None))
    assert d.passed and any("earnings" in c for c in d.cautions)  # relies on owner clearance, which is still checked
    assert "EVENT_UNVERIFIED" in codes(run(obs(earnings_in_window=None), a=approval(events_cleared_through=TODAY)))


# ---- session ----
def test_session_gates():
    assert "MARKET_CLOSED" in codes(run(session=SessionInfo(SessionStatus.CLOSED)))
    assert "UNKNOWN_SESSION" in codes(run(session=SessionInfo(SessionStatus.UNKNOWN)))
    assert "UNKNOWN_SESSION" in codes(run(session=SessionInfo(SessionStatus.OPEN, None)))
    assert "MARKET_CLOSED" in codes(run(session=SessionInfo(SessionStatus.OPEN, 19)))
    assert run(session=SessionInfo(SessionStatus.OPEN, 20)).passed


# ---- ledger / drawdown through the gate ----
def test_missing_or_stale_or_unconfirmed_snapshot_blocks():
    assert "LEDGER_STALE" in codes(run(b=book(snaps=[])))
    assert "LEDGER_STALE" in codes(run(b=book(snaps=[snap(asof=NOW - timedelta(hours=31))])))
    # 30h passes the age limit but predates the previous session close (SR-10); 19h is after the Thursday close
    assert "LEDGER_STALE" in codes(run(b=book(snaps=[snap(asof=NOW - timedelta(hours=30))])))
    assert "LEDGER_STALE" not in codes(run(b=book(snaps=[snap(asof=NOW - timedelta(hours=19))])))
    assert "LEDGER_UNRECONCILED" in codes(run(b=book(snaps=[snap(confirmed=False)])))


def test_ledger_change_after_snapshot_blocks():
    b = book(snaps=[snap(asof=NOW - timedelta(hours=2))], last_change=NOW - timedelta(hours=1))
    assert "LEDGER_UNRECONCILED" in codes(run(b=b))


def test_assignment_backlog_and_stale_stock_mark_block():
    b = book(positions=[put(state=PositionState.ASSIGNED_PENDING_RECONCILIATION)])
    assert "LEDGER_UNRECONCILED" in codes(run(b=b))
    b = book(lots=[lot(asof=NOW - timedelta(hours=31))])
    assert "LEDGER_STALE" in codes(run(b=b))


def test_insufficient_free_cash():
    assert "INSUFFICIENT_FREE_CASH" in codes(run(b=book(snaps=[snap(cash="7499.99")])))
    assert "INSUFFICIENT_FREE_CASH" not in codes(run(b=book(snaps=[snap(cash="7500")])))


@pytest.mark.parametrize("equity,expect", [
    ("96900", {"DRAWDOWN_PAUSE"}),   # 3.1%
    ("97000", {"DRAWDOWN_PAUSE"}),   # exactly 3%
    ("97001", set()),
    ("94900", {"DRAWDOWN_PAUSE"}),   # 5.1%
    ("91900", {"HARD_HALT"}),        # 8.1%
    ("92000", {"HARD_HALT"}),        # exactly 8%
])
def test_drawdown_through_gate(equity, expect):
    from datetime import timedelta as td
    snaps = [snap("100000", asof=NOW - td(days=3), sid="a"), snap(equity, asof=NOW - td(hours=1), sid="b")]
    assert codes(run(b=book(snaps=snaps))) == expect


def test_hard_halt_latched_after_rebound_until_reset():
    snaps = [snap("100000", asof=NOW - timedelta(days=3), sid="a"), snap("91000", asof=NOW - timedelta(days=2), sid="b"),
             snap("99000", asof=NOW - timedelta(hours=1), sid="c")]
    assert "HARD_HALT" in codes(run(b=book(snaps=snaps)))
    reset = NOW - timedelta(hours=1)  # owner reset; baseline = latest snapshot
    assert run(b=book(snaps=snaps, reset=reset)).passed


# ---- portfolio rules through the gate (spec 4.5, 15.1) ----
def test_per_ticker_cap_example_100_strike_blocked():
    d = run(obs(strike=D("100.00"), spot=D("110"), bid=D("0.50"), ask=D("0.54")), a=approval(max_buy_strike=D("100")))
    assert "SINGLE_TICKER_LIMIT" in codes(d)


def test_strike_90_01_fails_9000_cap_and_90_00_passes():
    ok = run(obs(strike=D("90.00"), spot=D("95")), a=approval(max_buy_strike=D("90.00")))
    assert "SINGLE_TICKER_LIMIT" not in codes(ok)
    bad = run(obs(strike=D("90.01"), spot=D("95")), a=approval(max_buy_strike=D("91")))
    assert "SINGLE_TICKER_LIMIT" in codes(bad)


def test_sector_cap_example_85_strike_with_4000_existing_sector_exposure():
    existing = put(trade_id="X", ticker="OTH", strike="40", sector="consumer_staples", bucket="other_bucket", asset_type="ETF")
    d = run(obs(strike=D("85.00"), spot=D("90")), a=approval(max_buy_strike=D("85")), b=book(positions=[existing]))
    assert "SECTOR_LIMIT" in codes(d)


def test_pilot_blocks_second_large_contract_and_disabled_pilot_allows_up_to_normal():
    existing = put(trade_id="X", ticker="OTH", strike="80", sector="s2", bucket="b2", asset_type="ETF")  # 8000
    d = run(b=book(positions=[existing]))  # 8000 + 7500 = 15500 > 15000
    assert "PILOT_COLLATERAL_LIMIT" in codes(d) and "NORMAL_EXPOSURE_LIMIT" not in codes(d)
    d2 = run(b=book(positions=[existing]), pol=policy(enabled=False))
    assert "PILOT_COLLATERAL_LIMIT" not in codes(d2)


def test_normal_ceiling_blocks_even_though_emergency_has_room():
    stock = lot(ticker="ZZZ", shares="1000", price="20", sector="s3", bucket="b3", asset_type="ETF")  # 20000 stock
    d = run(b=book(lots=[stock]), pol=policy(enabled=False))  # 20000 + 7500 = 27500 > 25000 < 30000
    assert "NORMAL_EXPOSURE_LIMIT" in codes(d)


def test_existing_put_on_same_ticker_blocks_and_includes_close_working():
    for st in (PositionState.WORKING, PositionState.OPEN, PositionState.PENDING, PositionState.CLOSE_WORKING):
        d = run(b=book(positions=[put(ticker="EXAMPLE", strike="50", state=st, sector="x", bucket="y")]))
        assert "CONTRACT_COUNT_LIMIT" in codes(d), st


def test_individual_name_limit_and_etf_exempt():
    a = put(trade_id="1", ticker="AAA", strike="30", sector="s1", bucket="b1")
    b2 = put(trade_id="2", ticker="BBB", strike="30", sector="s2", bucket="b2")
    assert "INDIVIDUAL_NAME_LIMIT" in codes(run(b=book(positions=[a, b2])))
    etf = approval(ticker="EXAMPLE", asset_type="ETF")
    assert "INDIVIDUAL_NAME_LIMIT" not in codes(run(a=etf, b=book(positions=[a, b2])))


def test_max_four_contracts():
    ps = [put(trade_id=str(i), ticker=f"E{i}", strike="10", sector=f"s{i}", bucket=f"b{i}", asset_type="ETF") for i in range(4)]
    assert "CONTRACT_COUNT_LIMIT" in codes(run(b=book(positions=ps), pol=policy(enabled=False)))


def test_expiry_week_cap():
    exp = TODAY + timedelta(days=40)
    existing = put(trade_id="X", ticker="OTH", strike="80", sector="s2", bucket="b2", asset_type="ETF", exp=exp)
    d = run(b=book(positions=[existing]), pol=policy(enabled=False))
    assert "EXPIRY_WEEK_LIMIT" in codes(d)  # 8000 + 7500 > 15000
    other_week = put(trade_id="Y", ticker="OTH", strike="80", sector="s2", bucket="b2", asset_type="ETF", exp=exp + timedelta(days=14))
    assert "EXPIRY_WEEK_LIMIT" not in codes(run(b=book(positions=[other_week]), pol=policy(enabled=False)))


def test_correlated_bucket_limit_even_with_different_sector_label():
    etf_pos = put(trade_id="X", ticker="ETFX", strike="40", sector="generic", bucket="defensive_consumer", asset_type="ETF")
    d = run(b=book(positions=[etf_pos]))  # 4000 + 7500 = 11500 > 10000 correlated cap
    assert "CORRELATED_LIMIT" in codes(d) and "SECTOR_LIMIT" not in codes(d)


def test_owned_stock_counts_toward_ticker_cap():
    held = lot(ticker="EXAMPLE", shares="20", price="80", sector="consumer_staples", bucket="defensive_consumer")
    assert "SINGLE_TICKER_LIMIT" in codes(run(b=book(lots=[held])))


def test_assigned_put_not_double_counted_after_reconcile():
    p = put(trade_id="A", ticker="EXAMPLE", strike="75", state=PositionState.ASSIGNED_RECONCILED, sector="consumer_staples")
    held = lot(ticker="EXAMPLE", shares="100", price="75", sector="consumer_staples", linked_trade_id="A")
    from csp_alerts.policy.portfolio import compute_exposure
    ex = compute_exposure(book(positions=[p], lots=[held]))
    assert ex.put_collateral_usd == 0 and ex.owned_stock_usd == D("7500")


def test_contracts_more_than_one_blocked_in_pilot():
    assert "CONTRACT_COUNT_LIMIT" in codes(run(contracts=2))


def test_tier_v_requires_fresh_timestamp():
    fresh = obs(data_tier=TIER_V, quote_at_utc=NOW - timedelta(seconds=120))
    assert run(fresh).decision is Decision.PASS_VERIFIED_DATA
    stale = obs(data_tier=TIER_V, quote_at_utc=NOW - timedelta(seconds=121))
    d = run(stale)
    assert d.decision is Decision.PASS_RESEARCH_ONLY and d.data_tier == "INDICATIVE_UNTIMESTAMPED"
    future = obs(data_tier=TIER_V, quote_at_utc=NOW + timedelta(seconds=30))
    assert run(future).decision is Decision.PASS_RESEARCH_ONLY


def test_all_codes_reported_not_short_circuited():
    d = run(obs(delta=D("-0.5"), open_interest=1, bid=D("0.1"), ask=D("0.9")), a=None)
    assert {"NOT_APPROVED", "DELTA_OUT_OF_RANGE", "INSUFFICIENT_OI", "BAD_BID_ASK", "SPREAD_TOO_WIDE"} <= codes(d)
    assert d.decision is Decision.BLOCKED


def test_evaluation_is_deterministic_and_inputs_untouched():
    b = book()
    assert run(b=b).to_dict() == run(b=b).to_dict()


def test_naive_now_rejected():
    from datetime import datetime
    with pytest.raises(ValueError):
        evaluate_entry(obs(), approval(), book(), P, datetime(2026, 10, 30, 15, 5), OPEN_SESSION)


def test_float_inputs_raise_type_error():
    for bad in (dict(bid=0.5), dict(delta=-0.13), dict(spot=80.0), dict(strike=75.0), dict(open_interest=500.0),
                dict(daily_volume=True)):
        with pytest.raises(TypeError):
            run(obs(**bad))


def test_label_case_and_whitespace_normalized_on_write():
    from csp_alerts.journal.store import NewPosition
    assert approval(sector=" Tech ").sector == "tech"
    assert put(sector="Tech ", bucket=" B1").sector == "tech"
    assert lot(sector="TECH").sector == "tech"
    n = NewPosition("T", "X", TODAY, D("1"), 100, 1, " Tech", "B1 ")
    assert (n.sector, n.correlation_bucket) == ("tech", "b1")
