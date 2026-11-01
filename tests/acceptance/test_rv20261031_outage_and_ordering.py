"""RV-20261031-03 (whole-fetch failure) and RV-20261031-04 (evidence ordering) acceptance. Synthetic, temp SQLite, no network.

Derived from requirements/evidence/2026-10-31-recheck/FINDINGS.md, MD-AT-03/04/06, WT-AT-05/09 (not from the implementer's tests).

Clock: NOW = 2026-10-30T15:05Z; tick N = NOW + 5N minutes. Held put: strike 40, actual credit 0.50, x100, fees 0.
Thresholds (config/policy.yaml exits): profit when (credit-ask)/credit >= 0.60 i.e. ask <= 0.20; loss review when ask >= 2.00*credit = 1.00;
delta review at |delta| >= 0.30.
  ask 0.15 -> (0.50-0.15)*100 = 35.00 -> PROFIT_EXIT      ask 0.34 -> 16.00 (no trigger)
  ask 1.10 -> LOSS_REVIEW (1.10 >= 1.00)                  delta -0.35 -> DELTA_REVIEW
  held 42 credit 0.60 ask 0.54 -> 6.00
"""
from __future__ import annotations

import sys
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "unit")]
from chain_fixtures import raw_row  # noqa: E402
from test_rv20261031_latest_evidence import EXP15, TODAY, Rig, rig, two  # noqa: E402,F401

from csp_alerts.jobs.monitor import run_monitor  # noqa: E402
from csp_alerts.market import observations as obs  # noqa: E402
from csp_alerts.market.models import SessionInfo, SessionStatus  # noqa: E402
from csp_alerts.market.normalize import normalize_rows  # noqa: E402

NUMERIC = {"PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW"}
GOOD = lambda spot=48.0, bid=0.13, ask=0.15, **k: [raw_row(strike=40.0, spot=spot, bid=bid, ask=ask, **k), raw_row(strike=42.0, spot=spot)]  # noqa: E731
BAD = lambda spot=48.0: [raw_row(strike=40.0, spot=spot, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=spot)]  # noqa: E731


def trig(v):
    return [w["trigger"] for w in v["warnings"]]


def assert_no_numeric(v):
    assert not (set(trig(v)) & NUMERIC), trig(v)
    assert v["pnl"] is None
    assert v["price_check"]["status"] == "unavailable" and v["price_check"]["reason"]


def old_write(r, rows, when, rid="delayed-old-writer", src="MONITOR", touch=True):
    """Replay an earlier completed fetch whose persistence was delayed behind a newer result (deterministic interleaving)."""
    norm = normalize_rows("AAA", rows, when, today_exchange=TODAY, earnings_dates=None, multiplier=None)
    res = obs.store_chain(r.j, symbol="AAA", run_id=rid, source=src, retrieved_at=when, norm=norm)
    if touch:
        obs.touch_success(r.j, "AAA", when, rid)
    return res


def monitor(r, n, rows):
    r.p.fetched_at = r.t(n)
    r.p.set("AAA", rows=rows)
    return run_monitor(journal=r.j, policy=r.pol, source=r.p, now_utc=r.t(n), session=SessionInfo(SessionStatus.OPEN, 300))


# ============================================================ RV-03 whole-fetch failure
@pytest.mark.parametrize("how", ["exception", "rate_limit", "empty_chain"])
def test_rv03_whole_fetch_failure_makes_retained_quote_history(rig, how):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    v0 = rig.pos("HOLD", 0)
    assert "PROFIT_EXIT" in trig(v0) and v0["pnl"]["net_of_fees_usd"] == D("35.00")           # control
    if how == "empty_chain":
        rig.update([], 1)
    else:
        rig.p.fetched_at = rig.t(1)
        rig.p.fail("AAA", "RATE_LIMIT" if how == "rate_limit" else "FETCH_ERROR")
        from csp_alerts.jobs.market_refresh import run_market_refresh
        run_market_refresh(journal=rig.j, policy=rig.pol, source=rig.p, now_utc=rig.t(1),
                           session=SessionInfo(SessionStatus.OPEN, 300), sleep=lambda _: None, pause_seconds=0)
    v = rig.pos("HOLD", 1)
    assert_no_numeric(v)
    ev = v["price_check"]["evidence"]
    assert str(ev["retrieved_at_utc"]).startswith("2026-10-30 15:05") and (str(ev["bid"]), str(ev["ask"])) == ("0.13", "0.15")
    assert ev["retained_unusable"] is True
    # market view agrees
    d = rig.detail(1)
    assert not any(x["review"]["all_pass"] for x in d["rows"])
    assert d["block"]["state"]["primary"] in ("STALE", "UNAVAILABLE", "ERROR")
    # recovery on the next valid write: ask 0.34 -> (0.50-0.34)*100 = 16.00
    rec = 5 if how == "rate_limit" else 2        # a rate limit backs the next attempt off to tick 4 (15:25)
    rig.update(GOOD(bid=0.30, ask=0.34), rec)
    v2 = rig.pos("HOLD", rec)
    assert v2["price_check"]["status"] == "usable" and v2["pnl"]["net_of_fees_usd"] == D("16.00")


def test_rv03_loss_and_delta_review_also_suppressed_and_calendar_kept(rig):
    # DTE 15 <= 21 -> TIME_EXIT is calendar-only. ask 1.10 >= 1.00 -> LOSS_REVIEW; delta -0.35 -> DELTA_REVIEW while quote is current.
    rig.hold("T15", "40", "0.50", exp=TODAY + timedelta(days=15))
    rig.update([raw_row(strike=40.0, spot=48.0, bid=1.05, ask=1.10, delta=-0.35, exp=EXP15), raw_row(strike=42.0, spot=48.0, exp=EXP15)], 0)
    v0 = rig.pos("T15", 0)
    assert {"LOSS_REVIEW", "DELTA_REVIEW", "TIME_EXIT"} <= set(trig(v0)), trig(v0)
    rig.update([], 1)
    v = rig.pos("T15", 1)
    assert "TIME_EXIT" in trig(v)
    assert_no_numeric(v)


def test_rv03_assignment_pending_kept_during_outage(rig):
    from csp_alerts.journal.models import PositionState
    rig.hold("ASG", "40", "0.50")
    rig.update(GOOD(), 0)
    rig.j.transition("ASG", PositionState.ASSIGNED_PENDING_RECONCILIATION)
    rig.update([], 1)
    v = rig.pos("ASG", 1)
    assert "ASSIGNMENT_PENDING_RECONCILIATION" in trig(v) and not (set(trig(v)) & NUMERIC) and v["pnl"] is None


def test_rv03_contract_with_own_newer_valid_evidence_is_unaffected(rig):
    rig.hold("H40", "40", "0.50")
    rig.hold("H42", "42", "0.60")
    rig.update(GOOD(), 0)
    rig.update([], 1)                                                  # whole-fetch failure at tick1
    assert_no_numeric(rig.pos("H40", 1))
    assert_no_numeric(rig.pos("H42", 1))
    monitor(rig, 2, [raw_row(strike=42.0, spot=48.0)])                 # newer valid evidence for strike 42 only
    v42 = rig.pos("H42", 2)
    assert v42["price_check"]["status"] == "usable" and v42["pnl"]["net_of_fees_usd"] == D("6.00")   # (0.60-0.54)*100
    assert_no_numeric(rig.pos("H40", 2))                               # 40 not returned: absent, still not current


def test_rv03_repeated_failures_then_recovery(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    for n in (1, 2, 3):
        rig.update([], n)
        assert_no_numeric(rig.pos("HOLD", n))
        assert str(rig.pos("HOLD", n)["price_check"]["evidence"]["retrieved_at_utc"]).startswith("2026-10-30 15:05")
    rig.update(GOOD(bid=0.30, ask=0.34), 4)
    assert rig.pos("HOLD", 4)["pnl"]["net_of_fees_usd"] == D("16.00")


def test_rv03_monitor_side_fetch_error_agrees_with_dashboard(rig):
    """The monitor reports DATA_ISSUE on its own fetch error; the dashboard position must not keep showing numeric advice from the
    previous quote either (MD-AT-03: provider outage is a data warning, not current advice)."""
    rig.hold("HOLD", "40", "0.50")
    monitor(rig, 0, GOOD())
    rig.p.fail("AAA")
    res = run_monitor(journal=rig.j, policy=rig.pol, source=rig.p, now_utc=rig.t(1), session=SessionInfo(SessionStatus.OPEN, 300))
    assert [str(s[1].trigger) for s in res.signals] == ["DATA_ISSUE"]
    assert_no_numeric(rig.pos("HOLD", 1))


# ============================================================ RV-04 ordering
def test_rv04_delayed_older_monitor_write_does_not_replace_rows_or_clear_contract_mark(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    rig.update(BAD(49.0), 2)                                           # invalid @ tick2 (15:15)
    assert_no_numeric(rig.pos("HOLD", 2))
    stock_before = rig.j.conn.execute("SELECT price, retrieved_at_utc FROM md_stock_latest").fetchone()
    old_write(rig, GOOD(47.0), rig.t(1))                               # valid but OLDER (15:10)
    assert_no_numeric(rig.pos("HOLD", 3))
    row = rig.j.conn.execute("SELECT bid, ask, retrieved_at_utc FROM md_option_latest WHERE strike='40'").fetchone()
    assert (row["bid"], row["ask"]) == ("0.13", "0.15") and row["retrieved_at_utc"].startswith("2026-10-30T15:05")
    stock_after = rig.j.conn.execute("SELECT price, retrieved_at_utc FROM md_stock_latest").fetchone()
    assert tuple(stock_after) == tuple(stock_before), "stock row must not regress to the older write (47)"
    assert rig.evidence()["contracts"], "newer failure mark must survive"
    assert not any(x["review"]["all_pass"] for x in rig.detail(3)["rows"] if x["strike"] == "40")
    # a genuinely newer valid write recovers
    rig.update(GOOD(bid=0.30, ask=0.34), 4)
    assert rig.pos("HOLD", 4)["pnl"]["net_of_fees_usd"] == D("16.00")


def test_rv04_older_write_does_not_replace_newer_valid_rows_or_regress_stock(rig):
    rig.update(GOOD(spot=48.0, bid=0.13, ask=0.15), 2)                 # newest, tick2
    old_write(rig, GOOD(spot=50.0, bid=0.20, ask=0.24), rig.t(1))      # older, tick1
    rows = {r["strike"]: r for r in rig.detail(2)["rows"]}
    assert (str(rows["40"]["bid"]), str(rows["40"]["ask"])) == ("0.13", "0.15")
    assert str(rows["40"]["retrieved_at_utc"]).startswith("2026-10-30 15:15")
    s = rig.detail(2)["block"]["stock"]
    assert str(s["price"]) == "48" and str(s["retrieved_at_utc"]).startswith("2026-10-30 15:15")
    hist = [r[0] for r in rig.j.conn.execute("SELECT price FROM md_stock_history ORDER BY obs_id")]
    assert "50" not in hist, f"older write must not enter the latest stock; history={hist}"


def test_rv04_older_write_does_not_clear_stock_conflict_mark(rig):
    rig.update(two(), 0)
    rig.update(two(49.0, 50.0), 2)                                      # conflict @ tick2
    mark = rig.evidence()["stock"]
    old_write(rig, two(48.0, 48.0), rig.t(1))                           # consistent but older
    assert rig.evidence().get("stock") == mark
    d = rig.detail(2)
    assert d["block"].get("stock_failure") is not None
    assert not any(x["review"]["all_pass"] for x in d["rows"])
    assert str(d["block"]["stock"]["price"]) == "48" and str(d["block"]["stock"]["retrieved_at_utc"]).startswith("2026-10-30 15:05")
    rig.update(two(51.0, 51.0), 3)                                      # newer consistent: recovers
    assert all(x["review"]["all_pass"] for x in rig.detail(3)["rows"])


def test_rv04_older_write_does_not_clear_absent_mark_nor_mark_rows_absent(rig):
    rig.update(two(), 0)
    rig.update([raw_row(strike=42.0, spot=48.0)], 2)                    # 40 absent @ tick2
    mark = dict(rig.evidence()["contracts"])
    old_write(rig, two(), rig.t(1))                                     # older full chain
    assert rig.evidence()["contracts"] == mark
    assert rig.rows(2)["40"].get("retained_unusable")


def test_rv04_older_write_missing_strike_does_not_mark_newer_row_absent(rig):
    rig.update(two(), 2)                                                 # both strikes @ tick2
    old_write(rig, [raw_row(strike=42.0, spot=48.0)], rig.t(1))          # older chain without 40
    assert rig.evidence() == {}
    assert all(x["review"]["all_pass"] for x in rig.detail(2)["rows"])


def test_rv04_failure_500ms_after_quote_in_same_stored_second_invalidates(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)                                               # stored as 15:05:00
    later = rig.t(0) + timedelta(microseconds=500000)
    old_write(rig, BAD(), later, rid="monitor-later-same-second", touch=True)
    v = rig.pos("HOLD", 1)
    assert_no_numeric(v)
    assert rig.rows(1)["40"].get("retained_unusable") and not rig.rows(1)["40"]["review"]["all_pass"]
    # chronological recovery: valid at +900ms (still same second) replaces and clears
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(0) + timedelta(microseconds=900000), rid="recover-same-second")
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("16.00")
    assert "contracts" not in rig.evidence()


def test_rv04_same_instant_tie_invalid_after_valid_invalidates_and_valid_after_invalid_recovers(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    old_write(rig, BAD(), rig.t(0), rid="tie-bad")                      # identical timestamp, arrives second
    assert_no_numeric(rig.pos("HOLD", 1))
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(0), rid="tie-good")  # identical timestamp again, arrives third
    v = rig.pos("HOLD", 1)
    assert v["price_check"]["status"] == "usable" and v["pnl"]["net_of_fees_usd"] == D("16.00"), "valid same-instant write must not freeze"


@pytest.mark.parametrize("order", ["monitor_then_refresh", "refresh_then_monitor"])
def test_rv04_monitor_and_refresh_orders_chronological(rig, order):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    if order == "monitor_then_refresh":
        monitor(rig, 1, BAD())                                         # monitor sees invalid @ tick1
        assert_no_numeric(rig.pos("HOLD", 1))
        rig.update(GOOD(bid=0.30, ask=0.34), 2)                         # refresh valid @ tick2 recovers
        assert rig.pos("HOLD", 2)["pnl"]["net_of_fees_usd"] == D("16.00")
        monitor(rig, 3, BAD())                                         # and a later invalid again blocks
        assert_no_numeric(rig.pos("HOLD", 3))
    else:
        rig.update(BAD(), 1)
        assert_no_numeric(rig.pos("HOLD", 1))
        monitor(rig, 2, GOOD(bid=0.30, ask=0.34))
        assert rig.pos("HOLD", 2)["pnl"]["net_of_fees_usd"] == D("16.00")
        rig.update(BAD(), 3)
        assert_no_numeric(rig.pos("HOLD", 3))
        monitor(rig, 4, GOOD())
        v = rig.pos("HOLD", 4)
        assert "PROFIT_EXIT" in trig(v) and v["pnl"]["net_of_fees_usd"] == D("35.00")


def test_rv04_good_bad_good_cycles_never_freeze_and_never_leak(rig):
    rig.hold("HOLD", "40", "0.50")
    n = 0
    for cycle in range(4):
        rig.update(GOOD(bid=0.13, ask=0.15), n)
        v = rig.pos("HOLD", n)
        assert v["pnl"]["net_of_fees_usd"] == D("35.00") and "PROFIT_EXIT" in trig(v), f"cycle {cycle} good"
        n += 1
        rig.update(BAD(), n) if cycle % 2 == 0 else rig.update([], n)
        assert_no_numeric(rig.pos("HOLD", n))
        assert str(rig.pos("HOLD", n)["price_check"]["evidence"]["retrieved_at_utc"]) == str(rig.t(n - 1).replace(tzinfo=rig.t(n - 1).tzinfo))
        n += 1
    assert rig.pos("HOLD", n - 1)["price_check"]["status"] == "unavailable"


def test_rv04_older_write_cannot_resurrect_after_failure_even_via_status_row(rig):
    """A delayed OLDER valid MONITOR write calls touch_success(fetched_at older than the newest failed attempt). The newest failed
    attempt must still govern (no numeric advice)."""
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    rig.update([], 3)                                                   # whole-fetch failure @ tick3
    old_write(rig, GOOD(), rig.t(2))                                    # older valid, delayed
    assert_no_numeric(rig.pos("HOLD", 3))


def test_rv04_delayed_older_write_does_not_regress_symbol_last_success(rig):
    rig.update(GOOD(), 0)
    rig.update(GOOD(), 4)
    before = obs.get_status(rig.j.conn, "AAA")["last_success_at_utc"]
    old_write(rig, GOOD(), rig.t(3))
    after = obs.get_status(rig.j.conn, "AAA")["last_success_at_utc"]
    assert after == before, f"older write moved last_success backwards {before} -> {after}"


def test_rv04_sub_second_older_failure_does_not_invalidate_newer_valid_quote_in_same_second(rig):
    """Failure at +0.5 s arrives AFTER a valid quote at +0.9 s (same stored second): the quote is newer and should stay usable."""
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(0) + timedelta(microseconds=900000), rid="valid-900ms")
    old_write(rig, BAD(), rig.t(0) + timedelta(microseconds=500000), rid="delayed-bad-500ms")
    v = rig.pos("HOLD", 1)
    assert v["price_check"]["status"] == "usable" and v["pnl"]["net_of_fees_usd"] == D("16.00")


def test_rv04_forward_progress_never_frozen_by_ordering_rule(rig):
    """Valid strictly-newer data always replaces older rows (no false 'older' classification), including 1 microsecond later."""
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(bid=0.13, ask=0.15), 0)
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(0) + timedelta(microseconds=1), rid="plus-1us")
    row = rig.j.conn.execute("SELECT ask FROM md_option_latest WHERE strike='40'").fetchone()
    assert row["ask"] == "0.34"
    old_write(rig, GOOD(bid=0.13, ask=0.15), rig.t(1), rid="plus-5min")
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("35.00")
