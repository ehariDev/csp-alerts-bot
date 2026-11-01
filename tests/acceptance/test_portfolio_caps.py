"""Acceptance: collateral / exposure / concentration caps, spec 4.1, 4.5, 15.1 #3-#8, #20.

Default policy: pilot ON (put collateral cap 15,000), normal ceiling 25,000, emergency 30,000, ticker 9,000,
sector 10,000, bucket 10,000, week 15,000, 4 contracts, 1 per underlying, 2 company names.
All existing positions below use ETF asset_type (so the 2-company-name cap is not accidentally hit) and distinct
sectors/buckets/weeks unless a test targets those dimensions.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from acc_helpers import (
    NOW, S, approval, book, codes, exp_for_dte, lot, obs, policy, policy_no_pilot, put, run, snap,
)

from csp_alerts.policy.portfolio import ProposedPut, check_portfolio_limits, compute_exposure, emergency_breached


def three_etf_puts_24k():
    # 3 x (80*100) = 24,000 spread across weeks 47/48/50, sectors, buckets
    return [
        put("E1", "ET1", "80.00", expiration=date(2026, 11, 20), sector="x1", bucket="y1"),
        put("E2", "ET2", "80.00", expiration=date(2026, 11, 27), sector="x2", bucket="y2"),
        put("E3", "ET3", "80.00", expiration=date(2026, 12, 11), sector="x3", bucket="y3"),
    ]


# ---------------------------------------------------------------- 15.1 #3  $25K normal vs $30K emergency
def test_normal_25k_cap_boundary_pilot_disabled():
    # existing 24,000; new strike 10 -> 1,000 -> total 25,000 (== cap, allowed)
    # NET free cash must satisfy free + active collateral <= equity: 70,000 + 24,000 = 94,000 <= 100,000
    b = book(positions=three_etf_puts_24k(), snaps=[snap(cash="70000")])
    ok = run(obs(strike=D("10.00"), spot=D("12")), b=b, pol=policy_no_pilot())
    assert ok.passed, codes(ok)
    assert ok.exposure_after_usd == D("25000")
    # 10.01 -> 1,001 -> 25,001 > 25,000 although far below 30,000 emergency
    bad = run(obs(strike=D("10.01"), spot=D("12")), b=b, pol=policy_no_pilot())
    assert codes(bad) == {"NORMAL_EXPOSURE_LIMIT"}
    assert bad.exposure_after_usd == D("25001") < D("30000")


def test_emergency_30k_is_never_headroom_and_flag_boundary():
    pol = policy_no_pilot()
    # existing 26,000 aggregate (above normal, below emergency): any new put blocked
    b = book(positions=[put("E1", "ET1", "80.00", expiration=date(2026, 11, 20), sector="x1", bucket="y1")],
             lots=[lot(shares="200", price="90.00")], snaps=[snap(cash="90000")])
    assert compute_exposure(b).aggregate_equity_risk_usd == D("26000")
    d = run(obs(strike=D("10.00"), spot=D("12")), b=b, pol=pol)
    assert "NORMAL_EXPOSURE_LIMIT" in codes(d)
    assert emergency_breached(b, pol) is False
    b30 = book(lots=[lot(shares="300", price="100.00")])
    assert emergency_breached(b30, pol) is False     # exactly 30,000 not > 30,000
    b31 = book(lots=[lot(shares="300", price="100.01")])
    assert emergency_breached(b31, pol) is True


def test_owned_stock_counts_toward_aggregate_25k():
    # stock lot 200 sh * 100 = 20,000. new 5,000 -> 25,000 pass; 5,001 -> block (put collateral alone is only 5,001)
    b = book(lots=[lot(shares="200", price="100.00")], snaps=[snap(cash="90000")])
    pol = policy_no_pilot()
    assert run(obs(strike=D("50.00"), spot=D("55")), b=b, pol=pol).passed
    d = run(obs(strike=D("50.01"), spot=D("55")), b=b, pol=pol)
    assert codes(d) == {"NORMAL_EXPOSURE_LIMIT"}


# ---------------------------------------------------------------- 15.1 #4 pilot 15K
def test_pilot_15k_blocks_second_large_contract_and_boundary():
    one = [put("E1", "ET1", "70.00", expiration=date(2026, 11, 20), sector="x1", bucket="y1")]  # 7,000
    # second: 80 -> 8,000 -> total 15,000 == pilot cap -> allowed
    ok = run(obs(strike=D("80.00"), spot=D("85")), b=book(positions=one, snaps=[snap(cash="90000")]))
    assert ok.passed, codes(ok)
    bad = run(obs(strike=D("80.01"), spot=D("85")), b=book(positions=one, snaps=[snap(cash="90000")]))
    assert codes(bad) == {"PILOT_COLLATERAL_LIMIT"}
    # spec: "pilot cap blocks a second large contract": 8,000 existing + 8,000 new = 16,000
    eight = [put("E1", "ET1", "80.00", expiration=date(2026, 11, 20), sector="x1", bucket="y1")]
    d = run(obs(strike=D("80.00"), spot=D("85")), b=book(positions=eight, snaps=[snap(cash="90000")]))
    assert "PILOT_COLLATERAL_LIMIT" in codes(d) and not d.passed
    # pilot explicitly disabled in policy -> same entry allowed (normal cap 25,000 applies)
    assert run(obs(strike=D("80.00"), spot=D("85")), b=book(positions=eight, snaps=[snap(cash="90000")]),
               pol=policy_no_pilot()).passed


def test_pilot_cap_applies_to_put_collateral_only_not_stock():
    # stock 8,000 + new put 7,500 = aggregate 15,500 (<25k) and put collateral 7,500 (<15k) -> no pilot block
    b = book(lots=[lot(shares="100", price="80.00")], snaps=[snap(cash="90000")])
    assert run(b=b).passed


# ---------------------------------------------------------------- 15.1 #5 same ticker / pending / close-working
@pytest.mark.parametrize("state", [S.PENDING, S.WORKING, S.OPEN, S.CLOSE_WORKING])
def test_existing_active_put_on_same_ticker_blocks(state):
    p = put("A1", "AAA", "50.00", state=state, asset_type="STOCK", sector="tech", bucket="b1")
    d = run(b=book(positions=[p], snaps=[snap(cash="90000")]))
    assert "CONTRACT_COUNT_LIMIT" in codes(d) and not d.passed


@pytest.mark.parametrize("state", [S.DRAFT, S.CLOSED, S.CANCELED, S.EXPIRED])
def test_inactive_states_do_not_reserve_or_block(state):
    p = put("A1", "AAA", "50.00", state=state, asset_type="STOCK", sector="tech", bucket="b1",
            actual_close_debit_per_share=D("0.1") if state is S.CLOSED else None)
    b = book(positions=[p], snaps=[snap(cash="90000")])
    assert compute_exposure(b).put_collateral_usd == D("0")
    assert run(b=b).passed


@pytest.mark.parametrize("state", [S.PENDING, S.WORKING, S.OPEN, S.CLOSE_WORKING])
def test_active_states_reserve_full_collateral(state):
    b = book(positions=[put("E1", "ET1", "80.00", state=state)])
    ex = compute_exposure(b)
    assert ex.put_collateral_usd == D("8000") and ex.active_contracts == 1


def test_pending_and_close_working_count_in_aggregate_for_new_entry():
    # PENDING 8,000 + CLOSE_WORKING 8,000 + WORKING 8,000 = 24,000; new 1,001 -> 25,001 blocked
    ps = [put("E1", "ET1", "80.00", state=S.PENDING, expiration=date(2026, 11, 20), sector="x1", bucket="y1"),
          put("E2", "ET2", "80.00", state=S.CLOSE_WORKING, expiration=date(2026, 11, 27), sector="x2", bucket="y2"),
          put("E3", "ET3", "80.00", state=S.WORKING, expiration=date(2026, 12, 11), sector="x3", bucket="y3")]
    assert compute_exposure(book(positions=ps)).put_collateral_usd == D("24000")
    d = run(obs(strike=D("10.01"), spot=D("12")), b=book(positions=ps, snaps=[snap(cash="70000")]), pol=policy_no_pilot())
    assert codes(d) == {"NORMAL_EXPOSURE_LIMIT"}


def test_adjustment_review_reserves_collateral():
    b = book(positions=[put("E1", "ET1", "80.00", state=S.ADJUSTMENT_REVIEW)])
    assert compute_exposure(b).put_collateral_usd == D("8000")


# ---------------------------------------------------------------- 15.1 #6 stock counts; assignment once
def test_owned_stock_counts_toward_ticker_sector_and_bucket():
    # AAA lot 50 sh * 90 = 4,500 + new 7,500 = 12,000 > 9,000 ticker, > 10,000 sector, > 10,000 bucket
    l = lot(ticker="AAA", shares="50", price="90.00", sector="tech", bucket="b1", asset_type="STOCK")
    d = run(b=book(lots=[l], snaps=[snap(cash="90000")]), pol=policy_no_pilot())
    assert {"SINGLE_TICKER_LIMIT", "SECTOR_LIMIT", "CORRELATED_LIMIT"} <= codes(d)


@pytest.mark.parametrize("state,price,expected", [
    (S.ASSIGNED_RECONCILED, "80.00", "8000"),          # 100 * 80 once
    (S.ASSIGNED_PENDING_RECONCILIATION, "80.00", "8000"),
    (S.ASSIGNED_RECONCILED, "78.00", "7800"),          # re-marked lot: 100 * 78
])
def test_assigned_put_and_its_stock_lot_counted_once(state, price, expected):
    p = put("A1", "AAA", "80.00", state=state, asset_type="STOCK", sector="tech", bucket="b1")
    l = lot(lot_id="L1", ticker="AAA", shares="100", price=price, sector="tech", bucket="b1", asset_type="STOCK",
            linked="A1", cost="8000")
    ex = compute_exposure(book(positions=[p], lots=[l]))
    assert ex.aggregate_equity_risk_usd == D(expected)
    assert ex.put_collateral_usd == D("0")  # collateral released into stock, not kept in both


# ---------------------------------------------------------------- 15.1 #7
def test_company_name_cap_two():
    p = put("N1", "N1T", "20.00", asset_type="STOCK", sector="sa", bucket="ba")          # name 1 (put)
    l = lot(lot_id="L2", ticker="N2T", shares="10", price="10.00", asset_type="STOCK", sector="sb", bucket="bb")  # name 2 (lot)
    b = book(positions=[p], lots=[l], snaps=[snap(cash="90000")])
    d = run(obs(strike=D("30.00"), spot=D("35")), b=b)
    assert codes(d) == {"INDIVIDUAL_NAME_LIMIT"}
    # ETF is categorized separately -> allowed
    assert run(obs(strike=D("30.00"), spot=D("35")), a=approval(asset_type="ETF"), b=b).passed
    # only 1 name -> third not reached
    assert run(obs(strike=D("30.00"), spot=D("35")), b=book(positions=[p], snaps=[snap(cash="90000")])).passed


def test_four_contract_cap_and_one_per_underlying():
    four = [put(f"E{i}", f"ET{i}", "10.00", expiration=date(2026, 11, 20) + timedelta(days=7 * (i % 3)), sector=f"x{i}", bucket=f"y{i}")
            for i in range(4)]
    d = run(obs(strike=D("30.00"), spot=D("35")), b=book(positions=four, snaps=[snap(cash="90000")]))
    assert "CONTRACT_COUNT_LIMIT" in codes(d)
    three = four[:3]
    assert run(obs(strike=D("30.00"), spot=D("35")), b=book(positions=three, snaps=[snap(cash="90000")])).passed


def test_expiry_week_cap_15k_and_boundary():
    # new expiry Fri 2026-12-04 (ISO wk 49). existing Mon 2026-11-30 is the same ISO week.
    ex = lambda strike, exp: [put("E1", "ET1", strike, expiration=exp, sector="x1", bucket="y1")]  # noqa: E731
    pol = policy_no_pilot()
    b_ok = book(positions=ex("75.00", date(2026, 11, 30)), snaps=[snap(cash="90000")])
    assert run(b=b_ok, pol=pol).passed                                   # 7,500 + 7,500 = 15,000 == cap
    d = run(obs(strike=D("75.01"), spot=D("80")), b=b_ok, pol=pol)         # 15,001
    assert codes(d) == {"EXPIRY_WEEK_LIMIT"}
    # different ISO week (Mon Dec 7 = wk 50) with 8,000 -> no week conflict
    b_other = book(positions=ex("80.00", date(2026, 12, 7)), snaps=[snap(cash="90000")])
    assert "EXPIRY_WEEK_LIMIT" not in codes(run(b=b_other, pol=pol))


def test_expiry_week_uses_iso_year_not_calendar_year():
    # 2026-12-31 (Thu) and 2027-01-01 (Fri) are both ISO 2026-W53.
    b = book(positions=[put("E1", "ET1", "80.00", expiration=date(2026, 12, 31), sector="x1", bucket="y1")])
    prop = ProposedPut("AAA", "STOCK", "tech", "b1", date(2027, 1, 1), D("80.00"), 100, 1)
    got = {r.value for r in check_portfolio_limits(b, prop, policy_no_pilot(), D("90000"))}
    assert "EXPIRY_WEEK_LIMIT" in got     # 8,000 + 8,000 = 16,000 > 15,000


# ---------------------------------------------------------------- 15.1 #8 sector / bucket (+ spec 4.5 example 1)
def test_spec_4_5_example_1_sector_cap():
    # active same-sector exposure 4,000 (40 sh * 100 stock lot of another name) + new $85 (8,500) = 12,500 > 10,000
    l = lot(lot_id="L1", ticker="DDD", shares="40", price="100.00", sector="tech", bucket="bd", asset_type="STOCK")
    d = run(obs(strike=D("85.00"), spot=D("90")), b=book(lots=[l], snaps=[snap(cash="90000")]))
    assert codes(d) == {"SECTOR_LIMIT"}
    assert d.exposure_after_usd == D("12500")


def test_correlation_bucket_cap_catches_generic_etf_sector_label():
    # existing ETF put labelled with a *generic* sector but same correlation bucket: 6,000 + 5,000 = 11,000
    p = put("E1", "ETF1", "60.00", sector="broad_market", bucket="b1", expiration=date(2026, 11, 20))
    d = run(obs(strike=D("50.00"), spot=D("55")), b=book(positions=[p], snaps=[snap(cash="90000")]))
    assert codes(d) == {"CORRELATED_LIMIT"}


@pytest.mark.parametrize("label", ["Tech", "tech ", " TECH", "TECH"])
def test_sector_label_case_and_whitespace_cannot_evade_cap(label):
    """Owner-typed free text: 'Tech' vs 'tech' must be the same sector. 6,000 + 5,000 = 11,000 > 10,000."""
    p = put("E1", "ETF1", "60.00", sector=label, bucket="zz", expiration=date(2026, 11, 20))
    d = run(obs(strike=D("50.00"), spot=D("55")), b=book(positions=[p], snaps=[snap(cash="90000")]))
    assert "SECTOR_LIMIT" in codes(d)


@pytest.mark.parametrize("label", ["B1", "b1 ", " B1"])
def test_bucket_label_case_and_whitespace_cannot_evade_cap(label):
    p = put("E1", "ETF1", "60.00", sector="other", bucket=label, expiration=date(2026, 11, 20))
    d = run(obs(strike=D("50.00"), spot=D("55")), b=book(positions=[p], snaps=[snap(cash="90000")]))
    assert "CORRELATED_LIMIT" in codes(d)


def test_money_results_are_decimal():
    d = run()
    for v in (d.exposure_before_usd, d.additional_collateral_usd, d.exposure_after_usd, d.manual_free_cash_after_usd):
        assert isinstance(v, D)
    ex = compute_exposure(book(positions=three_etf_puts_24k(), lots=[lot()]))
    assert isinstance(ex.aggregate_equity_risk_usd, D)
    assert ex.aggregate_equity_risk_usd == D("24000") + D("4000")


def test_fractional_strikes_sum_exactly():
    # 33.33*100*3 = 9,999.00 exactly (no float drift); three existing @33.33 = 9,999 each; total 29,997.00
    ps = [put(f"E{i}", f"ET{i}", "33.33", contracts=3) for i in range(3)]
    assert compute_exposure(book(positions=ps)).put_collateral_usd == D("29997.00")
