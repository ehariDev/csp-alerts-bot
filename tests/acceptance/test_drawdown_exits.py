"""Acceptance: drawdown (spec 4.2, 15.1 #16, #21) and exit triggers (spec 8.1, 15.1 #17-#19)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, S, TODAY, approval, book, codes, obs, policy, put, run, snap

from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.policy.drawdown import DrawdownLevel as L
from csp_alerts.policy.drawdown import compute_drawdown, escalated
from csp_alerts.policy.exits import OptionQuote, Trigger, evaluate_exits

POL = policy()


def snaps(*equities, confirmed=True, start_h=72, step_h=24):
    out = []
    n = len(equities)
    for i, e in enumerate(equities):
        # last one is 2h old, earlier ones step back
        age = 2 + (n - 1 - i) * step_h
        out.append(AccountSnapshot(f"s{i}", NOW - timedelta(hours=age), D(e), D("50000"),
                                   confirmed if not isinstance(confirmed, list) else confirmed[i], "v1"))
    return out


# ---------------------------------------------------------------- 15.1 #16 thresholds
@pytest.mark.parametrize("equity,level", [
    ("100000", L.NONE), ("97000.01", L.NONE),
    ("97000", L.PAUSE),        # loss 3000 == 3% * 100000 exactly -> ">=3%"
    ("96900", L.PAUSE),        # 3.1%
    ("95000.01", L.PAUSE),
    ("95000", L.REDUCE_REVIEW),  # exactly 5%
    ("94900", L.REDUCE_REVIEW),  # 5.1%
    ("92000.01", L.REDUCE_REVIEW),
    ("92000", L.HARD_HALT),    # exactly 8%
    ("91900", L.HARD_HALT),    # 8.1%
    ("120000", L.NONE),        # equity above peak
])
def test_drawdown_levels_from_100k_peak(equity, level):
    s = snaps("100000", equity)
    assert compute_drawdown(s, POL).level is level


def test_drawdown_value_hand_computed():
    # peak 110,000 then 105,000: 5,000/110,000 = 0.0454545... -> PAUSE
    st = compute_drawdown(snaps("100000", "110000", "105000"), POL)
    assert st.peak_equity_usd == D("110000") and st.level is L.PAUSE
    assert abs(st.drawdown - D("0.0454545")) < D("0.0000001")


def test_drawdown_unordered_input_and_unconfirmed_ignored():
    s = snaps("100000", "120000", "100000", confirmed=[True, False, True])
    st = compute_drawdown(list(reversed(s)), POL)
    assert st.peak_equity_usd == D("100000") and st.level is L.NONE


def test_no_confirmed_snapshot_is_unknown_not_invented_peak():
    st = compute_drawdown(snaps("90000", confirmed=False), POL)
    assert st.level is L.UNKNOWN and st.drawdown is None
    st2 = compute_drawdown([], POL)
    assert st2.level is L.UNKNOWN


def test_entries_blocked_at_3_1pct_with_zero_positions():
    # spec 4.5 item 4 / 15.1 #16
    d = run(b=book(snaps=snaps("100000", "96900")))
    assert "DRAWDOWN_PAUSE" in codes(d) and not d.passed


def test_5pct_and_8pct_codes_at_entry():
    assert "DRAWDOWN_PAUSE" in codes(run(b=book(snaps=snaps("100000", "94900"))))
    assert "HARD_HALT" in codes(run(b=book(snaps=snaps("100000", "91900"))))


def test_just_under_3pct_does_not_block():
    assert run(b=book(snaps=snaps("100000", "97000.01"))).passed


def test_8pct_halt_is_latched_after_rebound():
    # 100k -> 91k (9%) -> 99k (1%): no automatic re-enable from a transient rebound
    st = compute_drawdown(snaps("100000", "91000", "99000"), POL)
    assert st.level is L.HARD_HALT and st.halt_latched
    d = run(b=book(snaps=snaps("100000", "91000", "99000")))
    assert "HARD_HALT" in codes(d) and not d.passed


def test_manual_reset_baseline_clears_latch_only_through_explicit_baseline():
    s = snaps("100000", "91000", "99000")
    base = s[-1].asof_utc
    st = compute_drawdown(s, POL, baseline_asof_utc=base)
    assert st.level is L.NONE and st.peak_equity_usd == D("99000")
    # a baseline reset to the low point, then fresh decline measured from the new peak
    s2 = snaps("100000", "91000", "91000", "86000")
    st2 = compute_drawdown(s2, POL, baseline_asof_utc=s2[1].asof_utc)
    # peak 91000; 86000 -> 5000/91000 = 5.49% -> REDUCE_REVIEW (not HALT)
    assert st2.level is L.REDUCE_REVIEW


def test_3pct_pause_now_latched_after_rebound():
    """Owner decision 1: every level latches until a manual reset. 100k -> 96.9k (3.1%) -> 99.5k stays PAUSE."""
    st = compute_drawdown(snaps("100000", "96900", "99500"), POL)
    assert st.level is L.PAUSE and st.latched


def test_first_snapshot_without_any_baseline_is_not_a_peak_source_function_level():
    """compute_drawdown alone (no baseline passed) still invents no peak; the baseline gate lives in reconcile
    (NO_BASELINE) - see test_owner_decisions.py for the end-to-end baseline behavior."""
    assert compute_drawdown(snaps("85000"), POL).level is L.NONE


def test_escalation_ordering_for_alert_policy():
    assert escalated(L.PAUSE, L.REDUCE_REVIEW) and escalated(L.REDUCE_REVIEW, L.HARD_HALT)
    assert escalated(L.NONE, L.PAUSE) and escalated(L.UNKNOWN, L.NONE)
    assert not escalated(L.REDUCE_REVIEW, L.PAUSE) and not escalated(L.PAUSE, L.PAUSE)


# ---------------------------------------------------------------- exits
def pos(**kw):
    d = dict(trade_id="PAPER-1", ticker="AAA", strike="75.00", state=S.OPEN, expiration=TODAY + timedelta(days=40),
             asset_type="STOCK", credit="0.50", sector="tech", bucket="b1")
    d.update(kw)
    return put(**d)


def quote(p, ask="0.30", bid="0.28", delta="-0.20", spot="80.00", age_s=60, key=None, retrieved="default"):
    r = NOW - timedelta(seconds=age_s) if retrieved == "default" else retrieved
    mk = lambda v: None if v is None else D(v)  # noqa: E731
    return OptionQuote(key or p.contract_key, r, mk(bid), mk(ask), mk(delta), mk(spot))


def sig(p, q, now=NOW, **kw):
    return {s.trigger: s for s in evaluate_exits(p, q, POL, now, **kw)}


@pytest.mark.parametrize("ask,fires", [("0.20", True), ("0.21", False), ("0.19", True), ("0.10", True), ("0.30", False)])
def test_profit_exit_60pct_boundary(ask, fires):
    # credit .50: (0.50-0.20)/0.50 = 0.60 exactly -> fires; (0.50-0.21)/0.50=0.58 -> no
    p = pos()
    assert (Trigger.PROFIT_EXIT in sig(p, quote(p, ask=ask, bid="0.01"))) is fires


def test_profit_exit_spec_example_pnl_31():
    p = pos()
    s = sig(p, quote(p, ask="0.19", bid="0.15"))[Trigger.PROFIT_EXIT]
    assert s.detail["est_gross_pnl_usd"] == D("31.00") and s.severity == "ACTION"
    assert abs(s.detail["captured_fraction"] - D("0.62")) < D("0.0000001")


def test_multi_contract_and_fees_pnl():
    # 3 contracts, fees 4.20, ask .20: gross (0.50-0.20)*100*3 = 90.00 ; net 85.80
    p = pos(contracts=3, fees_usd=D("4.20"))
    s = sig(p, quote(p, ask="0.20", bid="0.18"))[Trigger.PROFIT_EXIT]
    assert s.detail["est_gross_pnl_usd"] == D("90.00") and s.detail["est_net_pnl_usd"] == D("85.80")
    assert Trigger.PROFIT_EXIT in sig(p, quote(p, ask="0.20", bid="0.18"))  # fees do not shift the threshold (spec formula)


def test_profit_uses_ask_not_mid():
    # bid .05 ask .25 -> mid .15 would be 70% captured; ask gives 50% -> must NOT fire
    p = pos()
    assert Trigger.PROFIT_EXIT not in sig(p, quote(p, ask="0.25", bid="0.05"))


@pytest.mark.parametrize("ask,fires", [("1.00", True), ("0.99", False), ("1.01", True), ("2.50", True)])
def test_loss_review_2x_boundary(ask, fires):
    # credit .50 -> 2x = 1.00
    p = pos()
    r = sig(p, quote(p, ask=ask, bid="0.90"))
    assert (Trigger.LOSS_REVIEW in r) is fires
    if fires:
        assert r[Trigger.LOSS_REVIEW].severity == "URGENT"


def test_loss_uses_ask_not_mid():
    # bid .90 ask .99: mid .945; neither reaches 1.00. bid .98 ask 1.00: mid .99 (<1.00) but ask = 2x -> fires
    p = pos()
    assert Trigger.LOSS_REVIEW not in sig(p, quote(p, ask="0.99", bid="0.90"))
    assert Trigger.LOSS_REVIEW in sig(p, quote(p, ask="1.00", bid="0.98"))


@pytest.mark.parametrize("delta,fires", [("-0.30", True), ("-0.2999", False), ("-0.29", False), ("0.30", True), ("-0.45", True)])
def test_delta_review_030(delta, fires):
    p = pos()
    r = sig(p, quote(p, delta=delta))
    assert (Trigger.DELTA_REVIEW in r) is fires
    if fires:
        assert r[Trigger.DELTA_REVIEW].severity == "URGENT"


@pytest.mark.parametrize("dte,fires", [(21, True), (22, False), (20, True), (0, True), (-3, True)])
def test_time_exit_dte_21_boundary(dte, fires):
    p = pos(expiration=TODAY + timedelta(days=dte))
    assert (Trigger.TIME_EXIT in sig(p, quote(p))) is fires


def test_time_exit_with_no_quote_and_data_issue_separately():
    p = pos(expiration=TODAY + timedelta(days=21))
    r = sig(p, None)
    assert Trigger.TIME_EXIT in r and Trigger.DATA_ISSUE in r
    assert r[Trigger.TIME_EXIT].severity == "ACTION" and r[Trigger.DATA_ISSUE].severity == "WARN"
    assert Trigger.PROFIT_EXIT not in r and Trigger.LOSS_REVIEW not in r


@pytest.mark.parametrize("kw", [
    dict(ask=None), dict(ask="0"), dict(ask="NaN"), dict(ask="-1"), dict(ask="Infinity"),
    dict(ask="0.10", bid="0.50"),                    # crossed
    dict(age_s=1801), dict(age_s=-5), dict(retrieved=None),
    dict(key="BBB|2026-12-04|P|75.00|100"),            # wrong contract
])
def test_invalid_stale_or_mismatched_quote_emits_data_issue_and_suppresses_numeric(kw):
    p = pos(expiration=TODAY + timedelta(days=21))
    base = dict(ask="0.10", bid="0.05", delta="-0.5")  # would trigger profit and delta if trusted
    base.update(kw)
    r = sig(p, quote(p, **base))
    assert Trigger.DATA_ISSUE in r
    assert not ({Trigger.PROFIT_EXIT, Trigger.LOSS_REVIEW, Trigger.DELTA_REVIEW} & set(r))
    assert Trigger.TIME_EXIT in r   # calendar trigger survives


def test_quote_age_boundary_30_minutes_default():
    p = pos()
    assert Trigger.DATA_ISSUE not in sig(p, quote(p, age_s=1800))
    assert Trigger.DATA_ISSUE in sig(p, quote(p, age_s=1801))


def test_missing_or_zero_credit_never_crashes():
    for c in (None, "0"):
        p = pos(actual_open_credit_per_share=None if c is None else D(c))
        r = sig(p, quote(p))
        assert Trigger.DATA_ISSUE in r and Trigger.PROFIT_EXIT not in r


def test_assignment_review_dte7_itm_and_final_day():
    p = pos(expiration=TODAY + timedelta(days=7))
    assert Trigger.ASSIGNMENT_REVIEW in sig(p, quote(p))
    assert Trigger.ASSIGNMENT_REVIEW not in sig(pos(expiration=TODAY + timedelta(days=8)), quote(pos(expiration=TODAY + timedelta(days=8))))
    far = pos(expiration=TODAY + timedelta(days=30))
    r = sig(far, quote(far, spot="74.99"))       # ITM: spot < strike 75
    assert Trigger.ASSIGNMENT_REVIEW in r and r[Trigger.ASSIGNMENT_REVIEW].severity == "URGENT"
    assert Trigger.ASSIGNMENT_REVIEW not in sig(far, quote(far, spot="75.00"))
    p1 = pos(expiration=TODAY + timedelta(days=1))
    assert sig(p1, None)[Trigger.ASSIGNMENT_REVIEW].detail["final_day"] is True
    assert Trigger.ASSIGNMENT_REVIEW in sig(pos(expiration=TODAY + timedelta(days=1)), None)  # no quote needed


def test_only_open_and_close_working_are_monitored():
    for st in (S.OPEN, S.CLOSE_WORKING):
        p = pos(state=st, expiration=TODAY + timedelta(days=10))
        assert Trigger.TIME_EXIT in sig(p, None)
    for st in (S.DRAFT, S.PENDING, S.WORKING, S.CLOSED, S.CANCELED, S.EXPIRED,
               S.ASSIGNED_PENDING_RECONCILIATION, S.ASSIGNED_RECONCILED):
        p = pos(state=st, expiration=TODAY + timedelta(days=10))
        assert evaluate_exits(p, None, POL, NOW) == [], st


def test_adjustment_review_position_is_monitored_with_urgent_notice():
    """Owner decision 4: ADJUSTMENT_REVIEW positions are monitored (calendar + assignment + notice)."""
    p = pos(state=S.ADJUSTMENT_REVIEW, expiration=TODAY + timedelta(days=3))
    r = sig(p, None)
    assert {Trigger.ADJUSTMENT_REVIEW, Trigger.TIME_EXIT, Trigger.ASSIGNMENT_REVIEW, Trigger.DATA_ISSUE} <= set(r)
    assert r[Trigger.ADJUSTMENT_REVIEW].severity == "URGENT"


def test_dte_uses_exchange_date_not_utc_date_dst():
    # 2026-11-02T04:30Z is 23:30 EST on Nov 1. Expiry Nov 23: DTE 22 on exchange date (no TIME_EXIT);
    # a UTC-date implementation would say 21 and fire early.
    now = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)
    p = pos(expiration=date(2026, 11, 23))
    assert Trigger.TIME_EXIT not in sig(p, None, now=now)
    p2 = pos(expiration=date(2026, 11, 22))
    assert Trigger.TIME_EXIT in sig(p2, None, now=now)


def test_weekend_now_time_exit_still_fires_without_quote():
    sat = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)  # Saturday, market closed
    p = pos(expiration=date(2026, 11, 20))                    # DTE 20
    assert Trigger.TIME_EXIT in sig(p, None, now=sat)


def test_stale_snapshot_blocks_entry_but_exits_still_fire():
    stale = book(snaps=[snap(asof=NOW - timedelta(hours=40))])
    d = run(b=stale)
    assert "LEDGER_STALE" in codes(d) and not d.passed
    p = pos(expiration=TODAY + timedelta(days=20))
    assert Trigger.TIME_EXIT in sig(p, quote(p))


def test_exit_signal_to_dict_money_strings():
    p = pos()
    d = evaluate_exits(p, quote(p, ask="0.20", bid="0.15"), POL, NOW)[0].to_dict()
    assert d["trade_id"] == "PAPER-1"
    assert all(isinstance(v, (str, int, bool, list)) for v in d["detail"].values())
