"""RV-ACC-06/07/08 follow-up edges: monitor fetch-failure mark, touch_success monotonicity, sub-second ordering, run_at eviction.

Synthetic, temp SQLite, no network, no Telegram. Same clock convention (tick N = 2026-10-30T15:05Z + 5N min), held put 40 @ credit 0.50.
Oracles: ask 0.15 -> (0.50-0.15)*100 = 35.00 PROFIT_EXIT; ask 0.34 -> 16.00.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "unit")]
from chain_fixtures import raw_row  # noqa: E402
from test_rv20261031_latest_evidence import Rig, rig, two  # noqa: E402,F401
from test_rv20261031_outage_and_ordering import BAD, GOOD, assert_no_numeric, monitor, old_write, trig  # noqa: E402

from csp_alerts.jobs.monitor import run_monitor  # noqa: E402
from csp_alerts.market import observations as obs  # noqa: E402
from csp_alerts.market.models import SessionInfo, SessionStatus  # noqa: E402

OPEN = SessionInfo(SessionStatus.OPEN, 300)


def status(r):
    s = obs.get_status(r.j.conn, "AAA")
    return None if s is None else {k: s[k] for k in ("status", "last_attempt_at_utc", "last_success_at_utc", "consecutive_failures",
                                                     "next_attempt_after_utc", "last_error")}


def mon_fail(r, n):
    r.p.fetched_at = r.t(n)
    r.p.fail("AAA")
    return run_monitor(journal=r.j, policy=r.pol, source=r.p, now_utc=r.t(n), session=OPEN)


def outbox(r):
    return r.j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0]


# ---------------------------------------------------------------- RV-ACC-07 monitor failure mark
def test_monitor_fetch_failure_blocks_position_then_next_monitor_write_clears(rig):
    rig.hold("HOLD", "40", "0.50")
    monitor(rig, 0, GOOD())
    assert rig.pos("HOLD", 0)["pnl"]["net_of_fees_usd"] == D("35.00")
    res = mon_fail(rig, 1)
    assert [str(s[1].trigger) for s in res.signals] == ["DATA_ISSUE"]
    v = rig.pos("HOLD", 1)
    assert_no_numeric(v)
    assert str(v["price_check"]["evidence"]["retrieved_at_utc"]).startswith("2026-10-30 15:05") and str(v["price_check"]["evidence"]["ask"]) == "0.15"
    d1 = rig.detail(1)
    assert d1["block"]["state"]["primary"] in ("STALE", "ERROR", "UNAVAILABLE")
    assert not any(x["review"]["all_pass"] for x in d1["rows"])
    monitor(rig, 2, GOOD(bid=0.30, ask=0.34))
    v2 = rig.pos("HOLD", 2)
    assert v2["price_check"]["status"] == "usable" and v2["pnl"]["net_of_fees_usd"] == D("16.00")
    assert "fetch" not in rig.evidence()


def test_monitor_fetch_failure_cleared_by_next_valid_refresh(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    mon_fail(rig, 1)
    assert_no_numeric(rig.pos("HOLD", 1))
    assert "fetch" in rig.evidence()
    rig.update(GOOD(bid=0.30, ask=0.34), 2)
    assert rig.pos("HOLD", 2)["pnl"]["net_of_fees_usd"] == D("16.00")
    assert "fetch" not in rig.evidence()
    assert rig.detail(2)["block"]["state"]["primary"] not in ("ERROR", "UNAVAILABLE")


def test_monitor_fetch_failure_does_not_touch_refresh_cadence_backoff_or_counter(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    before = status(rig)
    mon_fail(rig, 1)
    mon_fail(rig, 2)
    assert status(rig) == before, (before, status(rig))
    # and a rate-limited refresh keeps its own backoff / counter semantic regardless of earlier monitor errors
    rig.p.fail("AAA", "RATE_LIMIT")
    from csp_alerts.jobs.market_refresh import run_market_refresh
    run_market_refresh(journal=rig.j, policy=rig.pol, source=rig.p, now_utc=rig.t(3), session=OPEN, sleep=lambda _: None, pause_seconds=0)
    assert status(rig)["consecutive_failures"] == 1 and status(rig)["status"] == "RATE_LIMITED"


def test_older_valid_write_after_monitor_failure_mark_does_not_clear_it(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    mon_fail(rig, 3)                                                   # failure @ tick3
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(2), rid="delayed-older")
    assert "fetch" in rig.evidence()
    assert_no_numeric(rig.pos("HOLD", 3))
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(4), rid="newer")     # newer valid clears
    assert rig.pos("HOLD", 4)["pnl"]["net_of_fees_usd"] == D("16.00")


def test_older_monitor_failure_does_not_overwrite_newer_failure_mark(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    mon_fail(rig, 3)
    at3 = obs.latest_evidence(rig.j.conn, "AAA")["fetch"]["at"]
    obs.record_monitor_fetch_failure(rig.j, "AAA", rig.t(1), "older failure")
    assert obs.latest_evidence(rig.j.conn, "AAA")["fetch"]["at"] == at3


def test_monitor_failure_with_no_status_row_is_safe_and_refresh_still_runs(rig):
    assert obs.get_status(rig.j.conn, "AAA") is None
    rig.hold("HOLD", "40", "0.50")
    obs.record_monitor_fetch_failure(rig.j, "AAA", rig.t(0), "provider error")
    # no quote exists at all: nothing to be current; no crash, view fails closed
    v = rig.pos("HOLD", 0)
    assert v["pnl"] is None and not (set(trig(v)) & {"PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW"})
    res = rig.update(GOOD(), 1)                                         # the refresh is still due and succeeds normally
    assert res.status == "OK", res
    s = status(rig)
    assert s["status"] == "OK" and s["consecutive_failures"] == 0 and s["next_attempt_after_utc"] is None
    assert "fetch" not in rig.evidence()
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("35.00")


def test_monitor_failure_mark_does_not_create_outbox_rows_or_sends(rig):
    rig.hold("HOLD", "40", "0.50")
    monitor(rig, 0, GOOD())
    n = outbox(rig)
    mon_fail(rig, 1)
    rig.update(GOOD(), 2)
    assert outbox(rig) == n, "market bookkeeping must not queue alerts"


def test_monitor_failure_for_unheld_symbol_still_blocks_market_candidates_until_valid_write(rig):
    rig.update(two(), 0)
    obs.record_monitor_fetch_failure(rig.j, "AAA", rig.t(1), "provider error")
    d = rig.detail(1)
    assert not any(x["review"]["all_pass"] for x in d["rows"]) and d["block"]["state"]["primary"] in ("STALE", "ERROR", "UNAVAILABLE")
    rig.update(two(), 2)
    assert all(x["review"]["all_pass"] for x in rig.detail(2)["rows"])


# ---------------------------------------------------------------- touch_success monotonic (RV-ACC-06)
def test_touch_success_older_and_same_second_are_noops(rig):
    rig.update(GOOD(), 2)
    before = status(rig)
    obs.touch_success(rig.j, "AAA", rig.t(1), "older")
    assert status(rig) == before
    obs.touch_success(rig.j, "AAA", rig.t(2) + timedelta(milliseconds=400), "same-second")
    assert status(rig) == before


def test_touch_success_does_not_report_ok_while_newer_refresh_failure_stands(rig):
    rig.update(GOOD(), 0)
    rig.update([], 3)                                                   # newest refresh attempt failed @ tick3
    st_before = status(rig)
    assert st_before["status"] != "OK"
    obs.touch_success(rig.j, "AAA", rig.t(2), "delayed")
    st = status(rig)
    assert st["status"] == st_before["status"] and st["last_error"] == st_before["last_error"]
    assert st["consecutive_failures"] == st_before["consecutive_failures"]
    assert st["next_attempt_after_utc"] == st_before["next_attempt_after_utc"]
    assert st["last_attempt_at_utc"] == st_before["last_attempt_at_utc"]
    d = rig.detail(3)
    assert d["block"]["state"]["primary"] in ("STALE", "ERROR", "UNAVAILABLE")
    assert not any(x["review"]["all_pass"] for x in d["rows"])


def test_touch_success_newer_than_failure_restores_ok(rig):
    rig.update(GOOD(), 0)
    rig.update([], 1)
    obs.touch_success(rig.j, "AAA", rig.t(2), "newer-monitor")
    st = status(rig)
    assert st["status"] == "OK" and st["consecutive_failures"] == 0 and st["next_attempt_after_utc"] is None


def test_touch_success_first_ever_with_no_status_row(rig):
    obs.touch_success(rig.j, "AAA", rig.t(0), "first")
    s = status(rig)
    assert s["status"] == "OK" and s["last_success_at_utc"].startswith("2026-10-30T15:05")


# ---------------------------------------------------------------- sub-second ordering both ways (RV-ACC-08)
def test_subsecond_contract_mark_older_failure_ignored_newer_failure_applied(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(0) + timedelta(microseconds=900000), rid="v900")
    old_write(rig, BAD(), rig.t(0) + timedelta(microseconds=500000), rid="b500")      # older failure: ignored
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("16.00")
    old_write(rig, BAD(), rig.t(0) + timedelta(microseconds=950000), rid="b950")      # newer failure: applies
    assert_no_numeric(rig.pos("HOLD", 1))
    old_write(rig, GOOD(bid=0.13, ask=0.15), rig.t(0) + timedelta(microseconds=990000), rid="v990")   # still same second: recovers
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("35.00")


def test_subsecond_monitor_fetch_mark_older_than_quote_does_not_invalidate(rig):
    """Whole-fetch failure at +0.5 s that is OLDER than a valid quote retrieved at +0.9 s (same stored second)."""
    rig.hold("HOLD", "40", "0.50")
    old_write(rig, GOOD(), rig.t(0) + timedelta(microseconds=900000), rid="v900")
    obs.record_monitor_fetch_failure(rig.j, "AAA", rig.t(0) + timedelta(microseconds=500000), "delayed older failure")
    v = rig.pos("HOLD", 0 + 1)
    assert v["price_check"]["status"] == "usable" and v["pnl"]["net_of_fees_usd"] == D("35.00")


def test_subsecond_monitor_fetch_mark_newer_than_quote_invalidates(rig):
    rig.hold("HOLD", "40", "0.50")
    old_write(rig, GOOD(), rig.t(0) + timedelta(microseconds=500000), rid="v500")
    obs.record_monitor_fetch_failure(rig.j, "AAA", rig.t(0) + timedelta(microseconds=900000), "newer failure same second")
    assert_no_numeric(rig.pos("HOLD", 1))
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(0) + timedelta(microseconds=950000), rid="v950")
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("16.00")


# ---------------------------------------------------------------- run_at map bounds and eviction
def test_run_at_map_is_bounded_and_history_still_orders_after_many_runs(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    for i in range(60):
        old_write(rig, GOOD(bid=0.13, ask=0.15), rig.t(0) + timedelta(seconds=i + 1), rid=f"run-{i:03d}", touch=False)
    import json
    d = json.loads(obs.get_status(rig.j.conn, "AAA")["detail_json"] or "{}")
    assert len(d.get("run_at", {})) <= 50
    # ordering still correct after eviction: a delayed older valid write is ignored, a newer failure applies
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(0) + timedelta(seconds=30), rid="late-old", touch=False)
    row = rig.j.conn.execute("SELECT ask FROM md_option_latest WHERE strike='40'").fetchone()
    assert row["ask"] == "0.15"
    old_write(rig, BAD(), rig.t(0) + timedelta(seconds=120), rid="newest-bad", touch=False)
    assert_no_numeric(rig.pos("HOLD", 2))


def test_run_at_eviction_removes_oldest_runs_not_arbitrary_runs(rig):
    """Eviction must drop the OLDEST run entries. Run ids chosen so the NEWEST run sorts alphabetically first: a sorted-key
    persistence would evict the newest instead."""
    rig.update(GOOD(), 0)   # creates the status row
    for i in range(55):
        rid = f"r{99 - i:02d}"                                         # alphabetical order is the REVERSE of chronological order
        old_write(rig, GOOD(), rig.t(0) + timedelta(seconds=i + 1), rid=rid, touch=False)
    import json
    d = json.loads(obs.get_status(rig.j.conn, "AAA")["detail_json"] or "{}")["run_at"]
    newest_two = {"r45", "r46"}                                         # i=54 -> r45 (newest), i=53 -> r46
    assert newest_two <= set(d), f"newest runs evicted from run_at: kept={sorted(d)[:5]}..."
    assert "r99" not in d, "oldest run should have been evicted"


def test_run_at_eviction_does_not_reintroduce_subsecond_false_positive_for_recent_retained_row(rig):
    """50+ earlier runs fill run_at. Strike 40 (EXP) is then last written by run 'a-recent' at +0.9 s (an id that sorts first). Two later
    runs only carry the 15-day expiry (strike 40 neither rewritten nor marked). A delayed failure for strike 40 stamped +0.5 s (older than
    the quote) must still be ignored."""
    from chain_fixtures import EXP
    from test_rv20261031_latest_evidence import EXP15
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    other = [raw_row(strike=40.0, spot=48.0, exp=EXP15), raw_row(strike=42.0, spot=48.0, exp=EXP15)]
    for i in range(52):
        old_write(rig, other, rig.t(0) + timedelta(seconds=i + 1), rid=f"m{i:02d}", touch=False)
    base = rig.t(0) + timedelta(seconds=100)
    old_write(rig, GOOD(bid=0.30, ask=0.34), base + timedelta(microseconds=900000), rid="a-recent", touch=False)
    for k, rid in enumerate(("z1", "z2"), start=2):
        old_write(rig, other, base + timedelta(seconds=k), rid=rid, touch=False)
    assert "contracts" not in rig.evidence()
    old_write(rig, BAD(), base + timedelta(microseconds=500000), rid="delayed-b", touch=False)
    row = rig.j.conn.execute("SELECT ask, run_id FROM md_option_latest WHERE strike='40' AND expiration=?", (EXP,)).fetchone()
    assert row["ask"] == "0.34" and row["run_id"] == "a-recent"
    assert "contracts" not in rig.evidence(), "an older failure must not mark a newer retained quote unusable"


# ---------------------------------------------------------------- regression review of RV-ACC-09/10 (minimal status row, legacy detail)
def _detail(r):
    import json
    return json.loads(obs.get_status(r.j.conn, "AAA")["detail_json"] or "{}")


def test_store_only_write_creates_minimal_row_that_is_not_ok_and_does_not_change_refresh_due(rig):
    """A symbol that only ever received a store_chain (no refresh attempt, no touch_success) must not look fetched/OK."""
    old_write(rig, GOOD(), rig.t(0), rid="solo", touch=False)
    s = status(rig)
    assert s["status"] != "OK" and s["last_attempt_at_utc"] is None and s["last_success_at_utc"] is None
    assert s["consecutive_failures"] == 0 and s["next_attempt_after_utc"] is None
    calls = len(rig.p.calls)
    res = rig.update(GOOD(bid=0.30, ask=0.34), 1)              # the refresh must still attempt this symbol
    assert len(rig.p.calls) == calls + 1 and res.status == "OK"
    s2 = status(rig)
    assert s2["status"] == "OK" and s2["last_attempt_at_utc"] and s2["last_success_at_utc"]
    assert "run_at" in _detail(rig), "status writers must keep run_at"


def test_refresh_attempt_and_touch_success_preserve_run_at_and_marks(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    rig.update(BAD(), 1)                                        # contract mark present
    d = _detail(rig)
    assert d.get("run_at") and d["latest_evidence"]["contracts"]
    rig.update([], 2)                                           # failed attempt (record_attempt failure path)
    d2 = _detail(rig)
    assert d2.get("run_at") and d2["latest_evidence"]["contracts"]
    obs.touch_success(rig.j, "AAA", rig.t(3), "later-monitor")
    d3 = _detail(rig)
    assert d3.get("run_at") and d3["latest_evidence"]["contracts"]


def test_legacy_detail_without_run_at_orders_by_whole_seconds_without_error(rig):
    import json
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    d = _detail(rig)
    d.pop("run_at", None)
    rig.j.conn.execute("UPDATE md_symbol_status SET detail_json=? WHERE symbol='AAA'", (json.dumps(d),))
    rig.j.conn.commit()
    old_write(rig, GOOD(bid=0.30, ask=0.34), rig.t(1), rid="n1")                      # newer: replaces
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("16.00")
    old_write(rig, GOOD(bid=0.13, ask=0.15), rig.t(0), rid="older")                   # older: ignored
    assert rig.pos("HOLD", 1)["pnl"]["net_of_fees_usd"] == D("16.00")
    old_write(rig, BAD(), rig.t(2), rid="bad")                                         # newer failure applies
    assert_no_numeric(rig.pos("HOLD", 2))
    old_write(rig, GOOD(bid=0.13, ask=0.15), rig.t(1), rid="older2")                   # older valid cannot clear it
    assert_no_numeric(rig.pos("HOLD", 2))
    assert _detail(rig).get("run_at")


def test_corrupt_run_at_value_fails_safe(rig):
    import json
    rig.hold("HOLD", "40", "0.50")
    rig.update(GOOD(), 0)
    d = _detail(rig)
    d["run_at"] = "garbage"
    rig.j.conn.execute("UPDATE md_symbol_status SET detail_json=? WHERE symbol='AAA'", (json.dumps(d),))
    rig.j.conn.commit()
    obs.record_monitor_fetch_failure(rig.j, "AAA", rig.t(1), "x")
    assert_no_numeric(rig.pos("HOLD", 1))
    rig.update(GOOD(bid=0.30, ask=0.34), 2)
    assert rig.pos("HOLD", 2)["pnl"]["net_of_fees_usd"] == D("16.00")


def test_run_at_trim_keeps_newest_by_time_in_both_id_orders(rig):
    rig.update(GOOD(), 0)
    for i in range(55):
        old_write(rig, GOOD(), rig.t(0) + timedelta(seconds=i + 1), rid=f"r{99 - i:02d}", touch=False)     # ids reverse of time
    ra = _detail(rig)["run_at"]
    assert len(ra) <= 50 and {"r45", "r46", "r47"} <= set(ra) and "r99" not in ra
    rig.update(GOOD(), 2)
    for i in range(55):
        old_write(rig, GOOD(), rig.t(10) + timedelta(seconds=i + 1), rid=f"s{i:02d}", touch=False)         # ids same order as time
    rb = _detail(rig)["run_at"]
    assert len(rb) <= 50 and {"s54", "s53"} <= set(rb) and "s00" not in rb


def test_never_fetched_symbol_with_failure_mark_only_does_not_look_fresh(rig):
    obs.record_monitor_fetch_failure(rig.j, "AAA", rig.t(0), "provider error")
    d = rig.detail(0)
    assert d["block"]["state"]["primary"] == "UNAVAILABLE" and not d["rows"]
