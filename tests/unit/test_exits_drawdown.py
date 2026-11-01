"""P0-04: exit triggers (spec 8.1, 15.1 #17-19) and drawdown rules (4.2)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from factories import NOW, TODAY, book, policy, put, snap

from csp_alerts.journal.models import PositionState
from csp_alerts.policy.drawdown import DrawdownLevel, compute_drawdown, escalated
from csp_alerts.policy.exits import OptionQuote, Trigger, estimated_pnl_usd, evaluate_exits
from csp_alerts.policy.portfolio import compute_exposure, emergency_breached

P = policy()


def pos(**over):
    base = dict(trade_id="T1", ticker="EXAMPLE", strike="75", exp=TODAY + timedelta(days=30), sector="s", bucket="b",
                actual_open_credit_per_share=D("0.50"), contracts=1)
    base.update(over)
    return put(**base)


def quote(p=None, **over):
    p = p or pos()
    base = dict(contract_key=p.contract_key, retrieved_at_utc=NOW - timedelta(minutes=1), bid=D("0.10"), ask=D("0.35"),
                delta=D("-0.12"), spot=D("80"))
    base.update(over)
    return OptionQuote(**base)


def trig(signals):
    return {s.trigger for s in signals}


def test_profit_exit_exactly_60_percent_and_just_below():
    p = pos()
    assert Trigger.PROFIT_EXIT in trig(evaluate_exits(p, quote(p, ask=D("0.20")), P, NOW))
    assert Trigger.PROFIT_EXIT not in trig(evaluate_exits(p, quote(p, ask=D("0.21")), P, NOW))


def test_profit_exit_uses_ask_not_bid_or_mid():
    p = pos()
    # bid is tiny and mid would be 0.17 (>=60%) but ask 0.30 is only 40% captured
    sigs = evaluate_exits(p, quote(p, bid=D("0.05"), ask=D("0.30")), P, NOW)
    assert Trigger.PROFIT_EXIT not in trig(sigs)


def test_profit_exit_multi_contract_pnl_and_fees():
    p = pos(contracts=3, fees_usd=D("4.00"))
    sig = [s for s in evaluate_exits(p, quote(p, ask=D("0.20")), P, NOW) if s.trigger is Trigger.PROFIT_EXIT][0]
    assert sig.detail["est_gross_pnl_usd"] == D("90.00")
    assert sig.detail["est_net_pnl_usd"] == D("86.00")
    assert sig.severity == "ACTION"
    assert estimated_pnl_usd(p, D("0.20")) == (D("90.00"), D("86.00"))


def test_loss_review_at_2x_and_below():
    p = pos()
    assert Trigger.LOSS_REVIEW in trig(evaluate_exits(p, quote(p, bid=D("0.95"), ask=D("1.00")), P, NOW))
    assert Trigger.LOSS_REVIEW not in trig(evaluate_exits(p, quote(p, bid=D("0.95"), ask=D("0.99")), P, NOW))
    sig = [s for s in evaluate_exits(p, quote(p, ask=D("1.00")), P, NOW) if s.trigger is Trigger.LOSS_REVIEW][0]
    assert sig.severity == "URGENT"


def test_delta_review_at_030_abs():
    p = pos()
    assert Trigger.DELTA_REVIEW in trig(evaluate_exits(p, quote(p, delta=D("-0.30")), P, NOW))
    assert Trigger.DELTA_REVIEW not in trig(evaluate_exits(p, quote(p, delta=D("-0.29")), P, NOW))


def test_time_exit_at_21_dte_inclusive_even_without_quote():
    p21 = pos(exp=TODAY + timedelta(days=21))
    p22 = pos(exp=TODAY + timedelta(days=22))
    s = evaluate_exits(p21, None, P, NOW)
    assert Trigger.TIME_EXIT in trig(s) and Trigger.DATA_ISSUE in trig(s)  # both emitted, independently
    assert Trigger.TIME_EXIT not in trig(evaluate_exits(p22, quote(p22), P, NOW))


def test_time_exit_survives_invalid_quote_and_closed_market():
    p = pos(exp=TODAY + timedelta(days=10))
    s = evaluate_exits(p, quote(p, ask=None), P, NOW)
    assert Trigger.TIME_EXIT in trig(s) and Trigger.DATA_ISSUE in trig(s)


@pytest.mark.parametrize("q", [
    lambda p: None,
    lambda p: quote(p, ask=None),
    lambda p: quote(p, ask=D("0")),
    lambda p: quote(p, ask=D("NaN")),
    lambda p: quote(p, bid=D("0.50"), ask=D("0.40")),
    lambda p: quote(p, retrieved_at_utc=None),
    lambda p: quote(p, retrieved_at_utc=NOW - timedelta(minutes=31)),
    lambda p: quote(p, retrieved_at_utc=NOW + timedelta(minutes=5)),
    lambda p: OptionQuote("OTHER|2026-12-04|P|75.00|100", NOW, D("0.1"), D("0.15"), D("-0.1"), D("80")),
])
def test_bad_quote_suppresses_numeric_triggers_and_emits_data_issue(q):
    p = pos()
    s = evaluate_exits(p, q(p), P, NOW)
    assert Trigger.DATA_ISSUE in trig(s)
    assert not trig(s) & {Trigger.PROFIT_EXIT, Trigger.LOSS_REVIEW, Trigger.DELTA_REVIEW}


def test_assignment_review_near_expiry_and_itm():
    p = pos(exp=TODAY + timedelta(days=7))
    assert Trigger.ASSIGNMENT_REVIEW in trig(evaluate_exits(p, quote(p), P, NOW))
    p30 = pos()
    assert Trigger.ASSIGNMENT_REVIEW not in trig(evaluate_exits(p30, quote(p30), P, NOW))
    assert Trigger.ASSIGNMENT_REVIEW in trig(evaluate_exits(p30, quote(p30, spot=D("74")), P, NOW))
    p1 = pos(exp=TODAY + timedelta(days=1))
    sig = [s for s in evaluate_exits(p1, None, P, NOW) if s.trigger is Trigger.ASSIGNMENT_REVIEW][0]
    assert sig.detail["final_day"] is True


def test_only_open_and_close_working_are_monitored():
    for st in PositionState:
        got = evaluate_exits(pos(state=st, exp=TODAY + timedelta(days=5)), None, P, NOW)
        assert bool(got) is (st in (PositionState.OPEN, PositionState.CLOSE_WORKING, PositionState.ADJUSTMENT_REVIEW)), st


def test_missing_credit_gives_data_issue_not_crash():
    p = pos(actual_open_credit_per_share=None)
    s = evaluate_exits(p, quote(p), P, NOW)
    assert Trigger.DATA_ISSUE in trig(s)


def test_exit_dte_uses_exchange_date():
    from datetime import datetime, timezone
    late_utc = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)  # still Oct 30 in New York
    p = pos(exp=date(2026, 11, 20))  # 21 days from Oct 30 NY
    assert Trigger.TIME_EXIT in trig(evaluate_exits(p, None, P, late_utc))


# ---- drawdown ----
def dd(equities, **kw):
    snaps = [snap(str(e), asof=NOW - timedelta(days=len(equities) - i), sid=f"s{i}") for i, e in enumerate(equities)]
    return compute_drawdown(snaps, P, **kw)


@pytest.mark.parametrize("latest,level", [
    (100000, DrawdownLevel.NONE), (97001, DrawdownLevel.NONE), (97000, DrawdownLevel.PAUSE),
    (96900, DrawdownLevel.PAUSE), (95000, DrawdownLevel.REDUCE_REVIEW), (94900, DrawdownLevel.REDUCE_REVIEW),
    (92001, DrawdownLevel.REDUCE_REVIEW), (92000, DrawdownLevel.HARD_HALT), (91900, DrawdownLevel.HARD_HALT),
])
def test_drawdown_levels_from_100k_peak(latest, level):
    assert dd([100000, latest]).level is level


def test_no_snapshots_is_unknown_not_zero():
    s = compute_drawdown([], P)
    assert s.level is DrawdownLevel.UNKNOWN and s.drawdown is None and not s.entries_blocked


def test_unconfirmed_snapshots_ignored_for_peak():
    snaps = [snap("200000", asof=NOW - timedelta(days=2), confirmed=False, sid="a"),
             snap("100000", asof=NOW - timedelta(days=1), sid="b")]
    assert compute_drawdown(snaps, P).level is DrawdownLevel.NONE


def test_peak_is_chronological_high_and_rebound_lowers_drawdown_below_halt_only_with_latch():
    s = dd([100000, 105000, 100000])
    assert s.peak_equity_usd == D("105000") and s.level is DrawdownLevel.PAUSE and not s.halt_latched
    s = dd([100000, 90000, 100000])
    assert s.level is DrawdownLevel.HARD_HALT and s.halt_latched


def test_manual_reset_baseline_clears_latch():
    snaps = [snap("100000", asof=NOW - timedelta(days=3), sid="a"), snap("90000", asof=NOW - timedelta(days=2), sid="b"),
             snap("99000", asof=NOW - timedelta(days=1), sid="c")]
    assert compute_drawdown(snaps, P).level is DrawdownLevel.HARD_HALT
    s = compute_drawdown(snaps, P, baseline_asof_utc=NOW - timedelta(days=1))
    assert s.level is DrawdownLevel.NONE and s.peak_equity_usd == D("99000")


def test_escalation_ordering():
    assert escalated(DrawdownLevel.PAUSE, DrawdownLevel.REDUCE_REVIEW)
    assert escalated(DrawdownLevel.REDUCE_REVIEW, DrawdownLevel.HARD_HALT)
    assert escalated(DrawdownLevel.NONE, DrawdownLevel.PAUSE)
    assert not escalated(DrawdownLevel.HARD_HALT, DrawdownLevel.PAUSE)
    assert not escalated(DrawdownLevel.PAUSE, DrawdownLevel.PAUSE)


# ---- exposure math ----
def test_collateral_and_pending_close_working_reserve():
    ps = [put(trade_id="1", ticker="A", strike="85", state=PositionState.PENDING),
          put(trade_id="2", ticker="B", strike="50", state=PositionState.CLOSE_WORKING),
          put(trade_id="3", ticker="C", strike="40", state=PositionState.CLOSED),
          put(trade_id="4", ticker="D", strike="40", state=PositionState.DRAFT),
          put(trade_id="5", ticker="E", strike="40", state=PositionState.EXPIRED)]
    ex = compute_exposure(book(positions=ps))
    assert ex.put_collateral_usd == D("13500") and ex.active_contracts == 2


def test_gross_collateral_85_strike_is_8500_and_breakeven_example():
    assert put(strike="85").collateral_usd == D("8500")
    assert D("85") - D("0.45") == D("84.55")


def test_emergency_flag_is_informational():
    from factories import lot
    b = book(lots=[lot(shares="1000", price="31")])
    assert emergency_breached(b, P) is True
    assert emergency_breached(book(), P) is False


def test_adjustment_review_is_monitored_with_urgent_notice_and_price_triggers():
    p = pos(state=PositionState.ADJUSTMENT_REVIEW, exp=TODAY + timedelta(days=5))
    s = evaluate_exits(p, quote(p, ask=D("0.20"), spot=D("70")), P, NOW)
    t = trig(s)
    assert {Trigger.ADJUSTMENT_REVIEW, Trigger.TIME_EXIT, Trigger.ASSIGNMENT_REVIEW, Trigger.PROFIT_EXIT} <= t
    assert [x.severity for x in s if x.trigger is Trigger.ADJUSTMENT_REVIEW] == ["URGENT"]
    assert Trigger.DATA_ISSUE in trig(evaluate_exits(p, None, P, NOW))


def test_every_drawdown_level_latches_until_reset():
    for equity, level in ((96000, DrawdownLevel.PAUSE), (94000, DrawdownLevel.REDUCE_REVIEW), (90000, DrawdownLevel.HARD_HALT)):
        s = dd([100000, equity, 100000, 101000])  # full rebound
        assert s.level is level and s.latched and s.current_level is DrawdownLevel.NONE and s.entries_blocked
    assert not dd([100000, 99000]).latched
    snaps = [snap("100000", asof=NOW - timedelta(days=3), sid="a"), snap("96000", asof=NOW - timedelta(days=2), sid="b"),
             snap("100500", asof=NOW - timedelta(days=1), sid="c")]
    assert compute_drawdown(snaps, P).level is DrawdownLevel.PAUSE
    assert compute_drawdown(snaps, P, baseline_asof_utc=NOW - timedelta(days=1)).level is DrawdownLevel.NONE


def test_baseline_seeds_peak_so_first_snapshot_can_halt():
    first = [snap("85000", asof=NOW - timedelta(hours=1))]
    s = compute_drawdown(first, P, baseline_equity_usd=D("100000"))
    assert s.level is DrawdownLevel.HARD_HALT and s.drawdown == D("0.15") and s.peak_equity_usd == D("100000")
    assert compute_drawdown(first, P).level is DrawdownLevel.NONE  # pure fn without a baseline: no invented peak
    hi = [snap("120000", asof=NOW - timedelta(days=1), sid="a"), snap("110000", asof=NOW - timedelta(hours=1), sid="b")]
    assert compute_drawdown(hi, P, baseline_equity_usd=D("100000")).peak_equity_usd == D("120000")
    # after a reset the original baseline no longer seeds the peak
    after = compute_drawdown(first, P, baseline_equity_usd=D("100000"), baseline_asof_utc=NOW - timedelta(hours=1))
    assert after.level is DrawdownLevel.NONE
