"""Acceptance: entry gates, spec 4.4 / 4.5 / 6.3 / 15.1. Oracle values hand-computed from the spec.

NOW = 2026-10-30T15:05:02Z (exchange date 2026-10-30). See acc_helpers for date arithmetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import (
    NOW, OPEN_SESSION, S, TODAY, approval, book, codes, exp_for_dte, lot, obs, policy, put, run, snap,
)

from csp_alerts.market.models import TIER_R, TIER_V, SessionInfo, SessionStatus
from csp_alerts.policy.reasons import Decision, Reason

SPEC_CODES = {
    "NOT_APPROVED", "APPROVAL_EXPIRED", "VALUATION_TOO_HIGH", "EVENT_UNVERIFIED", "EARNINGS_IN_WINDOW",
    "DTE_OUT_OF_RANGE", "DELTA_OUT_OF_RANGE", "INSUFFICIENT_OI", "INSUFFICIENT_VOLUME", "BAD_BID_ASK",
    "SPREAD_TOO_WIDE", "MISSING_MARKET_DATA", "NONSTANDARD_CONTRACT", "INSUFFICIENT_FREE_CASH",
    "SINGLE_TICKER_LIMIT", "SECTOR_LIMIT", "CORRELATED_LIMIT", "EXPIRY_WEEK_LIMIT", "CONTRACT_COUNT_LIMIT",
    "INDIVIDUAL_NAME_LIMIT", "NORMAL_EXPOSURE_LIMIT", "PILOT_COLLATERAL_LIMIT", "DRAWDOWN_PAUSE", "HARD_HALT",
    "LEDGER_STALE", "LEDGER_UNRECONCILED", "MARKET_CLOSED", "UNKNOWN_SESSION", "AI_VETO", "AI_UNAVAILABLE",
}


def test_reason_codes_contain_every_spec_code():
    have = {r.value for r in Reason}
    assert SPEC_CODES <= have
    # extras are only the two documented ones
    assert have - SPEC_CODES == {"STALE_LAST_TRADE", "STRIKE_NOT_BELOW_SPOT"}


def test_decision_enum_matches_spec_6_3():
    assert {d.value for d in Decision} == {
        "PASS_RESEARCH_ONLY", "PASS_VERIFIED_DATA", "BLOCKED", "MORE_RESEARCH", "DATA_UNAVAILABLE"}


# ---------------------------------------------------------------- baseline + 15.1 #1
def test_baseline_passes_and_math_matches_spec():
    # strike 75, bid .50, 1 contract: collateral 75*100=7500; premium .50*100=50; breakeven 75-.50=74.50
    # exposure_before 0, after 7500; cash after 50000-7500=42500
    # yield/day = 100*0.50/(75*35)=50/2625=0.019047...%
    d = run()
    assert d.decision is Decision.PASS_RESEARCH_ONLY and d.reason_codes == ()
    assert d.additional_collateral_usd == D("7500")
    assert d.exposure_before_usd == D("0") and d.exposure_after_usd == D("7500")
    assert d.manual_free_cash_after_usd == D("42500")
    assert d.metrics["premium_at_bid_usd"] == D("50")
    assert d.metrics["breakeven"] == D("74.50")
    assert abs(d.metrics["gross_bid_yield_per_calendar_day_pct"] - D("0.0190476")) < D("0.000001")
    assert d.data_tier == TIER_R
    assert d.requires_broker_manual_quote_check is True
    assert d.passed


def test_15_1_item1_strike_85_bid_045():
    # collateral 85*100=8500; premium .45*100=45; breakeven 85-.45=84.55
    d = run(obs(strike=D("85.00"), spot=D("90"), bid=D("0.45"), ask=D("0.49")))
    assert d.decision is Decision.PASS_RESEARCH_ONLY, codes(d)
    assert d.additional_collateral_usd == D("8500")
    assert d.metrics["premium_at_bid_usd"] == D("45")
    assert d.metrics["breakeven"] == D("84.55")


def test_to_dict_has_spec_6_3_keys_and_json_strings():
    dd = run().to_dict()
    for k in ("candidate_key", "policy_version", "decision", "reason_codes", "exposure_before_usd",
              "additional_collateral_usd", "exposure_after_usd", "manual_free_cash_after_usd", "data_tier",
              "requires_broker_manual_quote_check", "evaluated_at_utc"):
        assert k in dd
    assert dd["candidate_key"] == "AAA|2026-12-04|P|75.00|100"
    assert dd["policy_version"] == policy().policy_version   # spec example shows 1.0.0; shipped policy is revised
    assert dd["evaluated_at_utc"] == "2026-10-30T15:05:02Z"
    assert isinstance(dd["additional_collateral_usd"], str) and D(dd["additional_collateral_usd"]) == D("7500")


# ---------------------------------------------------------------- 15.1 #2 per-ticker cap
@pytest.mark.parametrize("strike,spot,expect_block", [
    ("90.00", "95", False),   # 90.00*100 = 9000 == cap -> allowed (cap is "<= 9000")
    ("90.01", "95", True),    # 9001 > 9000
    ("100.00", "105", True),  # spec 4.5 example 2
])
def test_per_ticker_cap_9000(strike, spot, expect_block):
    d = run(obs(strike=D(strike), spot=D(spot)))
    assert ("SINGLE_TICKER_LIMIT" in codes(d)) is expect_block
    if expect_block:
        assert d.decision is Decision.BLOCKED
    else:
        assert d.passed, codes(d)


# ---------------------------------------------------------------- 4.4 gates
@pytest.mark.parametrize("delta,ok", [
    ("-0.10", True), ("-0.100", True), ("-0.15", True), ("-0.150", True),
    ("-0.0999", False), ("-0.1501", False), ("-0.16", False), ("0.13", False), ("0", False), ("-0", False),
])
def test_delta_boundaries(delta, ok):
    d = run(obs(delta=D(delta)))
    assert d.passed is ok, (delta, codes(d))
    if not ok:
        assert "DELTA_OUT_OF_RANGE" in codes(d)


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_delta_nan_inf_blocked(bad):
    d = run(obs(delta=D(bad)))
    assert not d.passed and "DELTA_OUT_OF_RANGE" in codes(d)


def test_delta_missing_blocked():
    d = run(obs(delta=None))
    assert not d.passed and "MISSING_MARKET_DATA" in codes(d)


@pytest.mark.parametrize("dte,ok", [(35, True), (45, True), (34, False), (46, False), (0, False), (-1, False)])
def test_dte_boundaries(dte, ok):
    d = run(obs(expiration=exp_for_dte(dte)))
    assert d.passed is ok, (dte, codes(d))
    assert d.metrics["dte"] == dte


@pytest.mark.parametrize("now,expiry_iso,ok", [
    # now 2026-11-02T04:30Z = 23:30 EST on Sun Nov 1 (post fall-back). Exchange date Nov 1. Dec 6 - Nov 1 = 35 (OK).
    # A UTC-date implementation would compute Dec 6 - Nov 2 = 34 and wrongly block.
    (datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc), "2026-12-06", True),
    # now 2026-11-01T03:30Z = 23:30 EDT Sat Oct 31 (just before fall-back). Exchange date Oct 31; Dec 5 - Oct 31 = 35.
    (datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc), "2026-12-05", True),
    # spring forward: 2026-03-09T03:30Z = 23:30 EDT Sun Mar 8. Apr 12 - Mar 8 = 35.
    (datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc), "2026-04-12", True),
    # one day later is 36? (Apr 13 - Mar 8 = 36) -> still in range; 46 days -> Apr 23 out.
    (datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc), "2026-04-23", False),
])
def test_dte_uses_exchange_date_across_dst(now, expiry_iso, ok):
    from datetime import date
    today = (now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))).date()
    d = run(obs(expiration=date.fromisoformat(expiry_iso), last_option_trade_at_utc=now - timedelta(minutes=5)),
            a=approval(today=today), b=book(snaps=[snap(asof=now - timedelta(hours=2))]), now=now)
    assert ("DTE_OUT_OF_RANGE" not in codes(d)) is ok, codes(d)


@pytest.mark.parametrize("oi,vol,expect", [
    (500, 50, set()), (499, 50, {"INSUFFICIENT_OI"}), (500, 49, {"INSUFFICIENT_VOLUME"}), (0, 0, {"INSUFFICIENT_OI", "INSUFFICIENT_VOLUME"}),
])
def test_oi_and_volume_boundaries_never_relaxed(oi, vol, expect):
    d = run(obs(open_interest=oi, daily_volume=vol))
    assert codes(d) == expect


def test_oi_volume_missing_blocked():
    assert "MISSING_MARKET_DATA" in codes(run(obs(open_interest=None)))
    assert "MISSING_MARKET_DATA" in codes(run(obs(daily_volume=None)))


@pytest.mark.parametrize("bid,ask,ok,why", [
    # 15.1 #12: $0.06 abs spread blocked even though 0.06/1.03=5.8% < 10%
    ("1.00", "1.06", False, "abs"),
    # exactly $0.05 abs, mid .525 -> 9.52% -> pass
    ("0.50", "0.55", True, "abs-boundary"),
    # exactly 10% of mid: bid .38 ask .42 -> spread .04, mid .40, .04/.40 = 10% -> pass (inclusive "<=")
    ("0.38", "0.42", True, "frac-boundary"),
    # 0.45/0.50: abs .05 ok but .05/.475 = 10.53% -> block
    ("0.45", "0.50", False, "frac"),
    # 0.20/0.23: abs .03 ok but .03/.215 = 13.95% -> block (11%+ rule)
    ("0.20", "0.23", False, "frac"),
    # min bid boundary: .20 ok (spread .02/.21=9.5%)
    ("0.20", "0.22", True, "minbid-boundary"),
    ("0.19", "0.21", False, "minbid"),
])
def test_spread_and_min_bid(bid, ask, ok, why):
    d = run(obs(bid=D(bid), ask=D(ask)))
    assert d.passed is ok, (why, codes(d))


def test_spec_4_5_example_oi700_bid40_ask55():
    # spread .15; mid .475; .15/.475 = 31.58% -> block
    d = run(obs(open_interest=700, bid=D("0.40"), ask=D("0.55")))
    assert d.decision is Decision.BLOCKED and "SPREAD_TOO_WIDE" in codes(d)
    assert abs(d.metrics["spread_fraction_of_mid"] - D("0.3158")) < D("0.0001")


@pytest.mark.parametrize("bid,ask", [
    ("0", "0.54"), ("0.50", "0"), ("-0.1", "0.54"), ("0.54", "0.50"), ("NaN", "0.54"), ("0.50", "NaN"), ("0.50", "Infinity"),
    (None, "0.54"), ("0.50", None), (None, None),
])
def test_bad_or_missing_bid_ask_blocked(bid, ask):
    mk = lambda v: None if v is None else D(v)  # noqa: E731
    d = run(obs(bid=mk(bid), ask=mk(ask)))
    assert not d.passed
    assert d.decision in (Decision.BLOCKED, Decision.DATA_UNAVAILABLE)
    assert codes(d) & {"BAD_BID_ASK", "MISSING_MARKET_DATA"}


def test_floats_never_pass_the_engine():
    """Money must be Decimal. A float must not yield a pass (raising is acceptable, silent pass is not)."""
    for kw in ({"bid": 0.5}, {"ask": 0.54}, {"strike": 75.0}, {"delta": -0.13}, {"spot": 80.0}):
        try:
            d = run(obs(**kw))
        except Exception:
            continue
        assert not d.passed, kw


def test_float_failure_mode_is_a_clean_exception_type():
    """Documented-as-low: float input should be rejected with TypeError/ValueError, not AttributeError."""
    try:
        run(obs(bid=0.5))
    except (TypeError, ValueError):
        return
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"float input raised {type(exc).__name__}: {exc} (expected a deliberate TypeError/ValueError)")
    # not raising is also fine as long as it blocks
    assert not run(obs(bid=0.5)).passed


def test_strike_must_be_strictly_below_spot():
    assert run(obs(strike=D("80.00"), spot=D("80.00"))).decision is Decision.BLOCKED
    assert "STRIKE_NOT_BELOW_SPOT" in codes(run(obs(strike=D("80.00"), spot=D("80.00"))))
    assert run(obs(strike=D("79.99"), spot=D("80.00"))).passed
    assert "MISSING_MARKET_DATA" in codes(run(obs(spot=None)))
    assert not run(obs(spot=D("NaN"))).passed and not run(obs(spot=D("0"))).passed


def test_nonstandard_or_unknown_contract_blocked():
    assert "NONSTANDARD_CONTRACT" in codes(run(obs(multiplier=50)))
    assert "NONSTANDARD_CONTRACT" in codes(run(obs(multiplier=10)))
    assert "NONSTANDARD_CONTRACT" in codes(run(obs(option_right="C")))
    assert not run(obs(multiplier=None)).passed
    assert not run(obs(expiration=None)).passed
    assert not run(obs(strike=None)).passed
    assert not run(obs(strike=D("NaN"))).passed
    assert not run(obs(option_right=None)).passed


# ---------------------------------------------------------------- last trade print
def test_last_trade_age_boundary_1_5_days():
    # 1.5 days = 129,600 s
    ok = run(obs(last_option_trade_at_utc=NOW - timedelta(seconds=129600)))
    bad = run(obs(last_option_trade_at_utc=NOW - timedelta(seconds=129601)))
    assert ok.passed, codes(ok)
    assert "STALE_LAST_TRADE" in codes(bad) and not bad.passed


def test_last_trade_in_future_blocked():
    assert not run(obs(last_option_trade_at_utc=NOW + timedelta(minutes=10))).passed


def test_missing_last_trade_print_blocks_per_15_1_item_13():
    """Spec 15.1 #13: 'stale/missing last-print ... blocked'. (4.4 says caution-or-block 'per configured policy';
    no such policy key exists.) Implementation only cautions -> flagged as defect/ambiguity."""
    d = run(obs(last_option_trade_at_utc=None))
    assert not d.passed


# ---------------------------------------------------------------- approval / events
def test_not_approved_and_mismatch():
    assert codes(run(a=None)) >= {"NOT_APPROVED"}
    assert "NOT_APPROVED" in codes(run(a=approval(ticker="ZZZ")))


def test_approval_expiry_boundary_inclusive_today():
    assert run(a=approval(approved_through=TODAY.isoformat())).passed
    d = run(a=approval(approved_through=(TODAY - timedelta(days=1)).isoformat()))
    assert "APPROVAL_EXPIRED" in codes(d)


@pytest.mark.parametrize("age,ok", [(0, True), (7, True), (8, False), (-1, False)])
def test_event_review_age_7_days(age, ok):
    d = run(a=approval(events_reviewed_on=(TODAY - timedelta(days=age)).isoformat()))
    assert d.passed is ok, codes(d)
    if not ok:
        assert "EVENT_UNVERIFIED" in codes(d)


def test_event_clearance_must_cover_expiration():
    exp = exp_for_dte(35)
    assert run(a=approval(events_cleared_through=exp.isoformat())).passed
    d = run(a=approval(events_cleared_through=(exp - timedelta(days=1)).isoformat()))
    assert "EVENT_UNVERIFIED" in codes(d)


def test_valuation_ceiling_boundary():
    assert run(a=approval(max_buy="75.00")).passed
    d = run(a=approval(max_buy="74.99"))
    assert "VALUATION_TOO_HIGH" in codes(d)


def test_earnings_in_window_blocks_and_unknown_earnings_needs_owner_clearance():
    assert "EARNINGS_IN_WINDOW" in codes(run(obs(earnings_in_window=True)))
    # unknown provider earnings with valid owner clearance: allowed only with a caution
    d = run(obs(earnings_in_window=None))
    assert d.passed and any("earnings" in c for c in d.cautions)
    # unknown provider earnings AND no valid clearance -> blocked
    assert not run(obs(earnings_in_window=None), a=approval(events_cleared_through=TODAY.isoformat())).passed


def test_empty_chain_is_data_unavailable_not_no_trade():
    d = run(o=None) if False else __import__("csp_alerts.policy.entry", fromlist=["x"]).evaluate_entry(
        None, approval(), book(), policy(), NOW, OPEN_SESSION)
    assert d.decision is Decision.DATA_UNAVAILABLE and "MISSING_MARKET_DATA" in codes(d)


def test_naive_now_rejected():
    from csp_alerts.policy.entry import evaluate_entry
    with pytest.raises(ValueError):
        evaluate_entry(obs(), approval(), book(), policy(), datetime(2026, 10, 30, 15, 5), OPEN_SESSION)


# ---------------------------------------------------------------- session
@pytest.mark.parametrize("session,expect", [
    (SessionInfo(SessionStatus.CLOSED), "MARKET_CLOSED"),
    (SessionInfo(SessionStatus.UNKNOWN), "UNKNOWN_SESSION"),
    (SessionInfo(SessionStatus.OPEN, None), "UNKNOWN_SESSION"),
    (SessionInfo(SessionStatus.OPEN, 19), "MARKET_CLOSED"),
    (SessionInfo(SessionStatus.OPEN, 0), "MARKET_CLOSED"),
])
def test_session_blocks(session, expect):
    d = run(session=session)
    assert expect in codes(d) and not d.passed


def test_session_20_minutes_to_close_is_ok():
    assert run(session=SessionInfo(SessionStatus.OPEN, 20)).passed


# ---------------------------------------------------------------- account / ledger
def test_no_snapshot_blocks():
    d = run(b=book(snaps=[]))
    assert "LEDGER_STALE" in codes(d) and not d.passed


# SR-10 / docs/DECISIONS.md #81 supersede the old (30h, True) oracle. NOW = Fri 2026-10-30 15:05:02Z = 11:05 ET.
# 30h earlier = Thu 2026-10-29 09:05:02Z = 05:05 ET, i.e. BEFORE Thursday's 16:00 ET close, so it lacks the required
# post-close acknowledgment and must block (the old assertion that it passes is exactly the SR-10 defect).
# Snapshot ages that satisfy the daily rule on this Friday: asof >= Thu 20:00Z (16:00 EDT) = NOW - 19h05m02s.
@pytest.mark.parametrize("age_s,ok", [
    (30 * 3600, False),                      # prior morning: SR-10 -> blocked
    (30 * 3600 + 1, False),                  # also beyond the 30h age limit
    (19 * 3600 + 5 * 60 + 2, True),          # exactly Thu 20:00:00Z = close: at the boundary passes (>=)
    (19 * 3600 + 5 * 60 + 3, False),         # one second before the close: blocked
    (19 * 3600, True),                       # Thu 20:05Z, after the close
    (60, True),                              # fresh
])
def test_snapshot_age_30h(age_s, ok):
    d = run(b=book(snaps=[snap(asof=NOW - timedelta(seconds=age_s))]))
    assert d.passed is ok, codes(d)
    if not ok:
        assert "LEDGER_STALE" in codes(d)


def test_unconfirmed_snapshot_blocks():
    d = run(b=book(snaps=[snap(confirmed=False)]))
    assert "LEDGER_UNRECONCILED" in codes(d)


def test_ledger_change_after_snapshot_blocks_until_new_snapshot():
    change = NOW - timedelta(minutes=30)
    s_old = snap(asof=NOW - timedelta(hours=2))
    assert "LEDGER_UNRECONCILED" in codes(run(b=book(snaps=[s_old], last_change=change)))
    s_new = snap(asof=NOW - timedelta(minutes=10), sid="s2")
    assert run(b=book(snaps=[s_old, s_new], last_change=change)).passed


@pytest.mark.parametrize("cash,ok", [("7500", True), ("7499.99", False), ("0", False)])
def test_free_cash_boundary(cash, ok):
    # collateral 7500; spec 15.1 #15
    d = run(b=book(snaps=[snap(cash=cash)]))
    assert d.passed is ok, codes(d)
    if not ok:
        assert "INSUFFICIENT_FREE_CASH" in codes(d)


def test_stale_stock_mark_blocks():
    # 'unmarked owned stock' (15.1 #15): mark older than 30h
    stale = lot(asof=NOW - timedelta(hours=30, seconds=1))
    assert "LEDGER_STALE" in codes(run(b=book(lots=[stale])))
    assert run(b=book(lots=[lot(asof=NOW - timedelta(hours=30))])).passed


def test_assignment_backlog_blocks():
    p = put(trade_id="A1", ticker="EEE", state=S.ASSIGNED_PENDING_RECONCILIATION)
    d = run(b=book(positions=[p]))
    assert "LEDGER_UNRECONCILED" in codes(d)


def test_expired_but_unconfirmed_open_blocks():
    p = put(trade_id="X1", ticker="EEE", state=S.OPEN, expiration=TODAY - timedelta(days=1))
    assert "LEDGER_UNRECONCILED" in codes(run(b=book(positions=[p])))


def test_open_without_fill_data_blocks():
    p = put(trade_id="X1", ticker="EEE", state=S.OPEN, actual_open_credit_per_share=None)
    assert "LEDGER_UNRECONCILED" in codes(run(b=book(positions=[p])))


def test_lot_linked_to_still_reserving_put_flags_double_count():
    p = put(trade_id="X1", ticker="EEE", state=S.OPEN)
    l = lot(lot_id="L9", ticker="EEE", linked="X1")
    assert "LEDGER_UNRECONCILED" in codes(run(b=book(positions=[p], lots=[l])))


# ---------------------------------------------------------------- tier / provenance
def test_tier_verified_only_with_fresh_timestamp():
    base = dict(data_tier=TIER_V)
    ok = run(obs(**base, quote_at_utc=NOW - timedelta(seconds=120)))
    old = run(obs(**base, quote_at_utc=NOW - timedelta(seconds=121)))
    fut = run(obs(**base, quote_at_utc=NOW + timedelta(seconds=5)))
    nots = run(obs(**base, quote_at_utc=None))
    assert ok.decision is Decision.PASS_VERIFIED_DATA and ok.data_tier == TIER_V
    for d in (old, fut, nots):
        assert d.decision is Decision.PASS_RESEARCH_ONLY and d.data_tier == TIER_R
    # tier R observation that claims a timestamp is still not verified
    assert run(obs(quote_at_utc=NOW)).decision is Decision.PASS_RESEARCH_ONLY
    assert all(d.requires_broker_manual_quote_check for d in (ok, old, fut, nots))


def test_every_nonpass_reason_code_is_machine_stable_string():
    d = run(obs(delta=D("-0.5"), open_interest=1))
    for c in d.reason_codes:
        assert isinstance(c.value, str) and c.value == c.value.upper()


def test_contracts_greater_than_one_blocked():
    assert "CONTRACT_COUNT_LIMIT" in codes(run(contracts=2))
