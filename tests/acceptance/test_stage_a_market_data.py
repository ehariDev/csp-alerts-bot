"""Stage A acceptance: MD-AT-01..04 and MD-AT-06 (provider -> persistence -> rendered page). Synthetic, fake provider, no network.

MD-AT-05 (real provider, open session, public reference) is NOT covered here: it is BLOCKED (needs a live open-session smoke).

Hand oracles (frozen clock NOW = Fri 2026-10-30T15:05:02Z, session OPEN; expiry EXP = 2026-12-04, DTE 35):
  Observation A: spot 61.25, put 55 bid 0.50 ask 0.54 retrieved 15:05:02Z
  Observation B: spot 62.50, put 55 bid 0.62 ask 0.66 retrieved 15:11:02Z (360 s later; due because 360 >= 300 - 30 grace)
  OCC (derived) = ZZZ + 261204 + P + 00055000   (55 * 1000 = 55000 -> 8 digits)
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import timedelta
from decimal import Decimal as D

import pytest
from acc_helpers import NOW
from s2_helpers import FakeSource, raw_row
from stage_a_env import approved_file, dash, mkt, plain_db, text_of
from stage_a_helpers import EXP, POL, SAT, Prov, add_wl, csrf_of, hold, jopen, md_dump, refresh
from web_helpers import build_book

A_TS = "2026-10-30T15:05:02Z"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def stock_row(db):
    c = sqlite3.connect(db)
    r = c.execute("SELECT price, price_type, provider, market_time_utc, retrieved_at_utc, source FROM md_stock_latest WHERE symbol='ZZZ'").fetchone()
    c.close()
    return r


def opt_row(db, strike="55"):
    c = sqlite3.connect(db)
    r = c.execute("SELECT bid, ask, retrieved_at_utc, occ_symbol, occ_source, source, quote_time_utc FROM md_option_latest "
                  "WHERE symbol='ZZZ' AND strike=?", (strike,)).fetchone()
    c.close()
    return r


def counts(db, *tables):
    c = sqlite3.connect(db)
    out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    c.close()
    return out


# =============================================================================================== MD-AT-01
def test_md_at_01_observation_a_then_b_reaches_the_rendered_page_without_any_entry_candidate(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ", spot=61.25, bid=0.50, ask=0.54)
    refresh(db, p, NOW)
    d = dash(db, approved_file(tmp_path), now=NOW + timedelta(minutes=1))
    try:
        b = mkt(d, "ZZZ")
        s = b["block"]["stock"]
        assert s["price"] == "61.25" and s["price_type"] == "LAST" and "YAHOO" in s["provider"].upper()
        assert s["market_time_utc"] is None                                  # Yahoo gives none: unknown, NEVER the retrieval time
        assert str(s["retrieved_at_utc"]).startswith("2026-10-30T15:05:02")
        assert s["source"].lower() == "refresh"
        (row,) = b["rows"]
        assert (row["strike"], row["bid"], row["ask"]) == ("55", "0.5", "0.54")
        assert str(row["expiration"]) == "2026-12-04" and row["dte"] == 35
        assert row["occ_symbol"] == "ZZZ261204P00055000" and row["occ_source"] == "DERIVED"
        assert str(row["retrieved_at_utc"]).startswith("2026-10-30T15:05:02") and row["quote_time_utc"] is None
        st, _, html = d.req("GET", "/market/ZZZ")
        t = text_of(html)
        assert st == 200 and "61.25" in t and "0.54" in t and "ZZZ261204P00055000" in t and "2026-10-30 10:05 CDT" in t
        # --- B
        p.fetched_at = NOW + timedelta(minutes=6)
        p.set("ZZZ", spot=62.50, bid=0.62, ask=0.66)
        res = refresh(db, p, NOW + timedelta(minutes=6))
        assert [o.status for o in res.outcomes if o.attempted] == ["OK"]
        d.now = NOW + timedelta(minutes=7)
        b = mkt(d, "ZZZ")
        s = b["block"]["stock"]
        assert s["price"] == "62.5" and str(s["retrieved_at_utc"]).startswith("2026-10-30T15:11:02")
        (row,) = b["rows"]
        assert (row["bid"], row["ask"]) == ("0.62", "0.66") and str(row["retrieved_at_utc"]).startswith("2026-10-30T15:11:02")
        t = text_of(d.req("GET", "/market/ZZZ")[2])
        assert "62.5" in t and "0.66" in t and "61.25" not in t and "0.54" not in t
        assert "2026-10-30 10:11 CDT" in t
        assert counts(db, "entry_evaluations", "alert_outbox", "market_observations") == {
            "entry_evaluations": 0, "alert_outbox": 0, "market_observations": 0}          # no entry candidate was needed or created
    finally:
        d.stop()


# =============================================================================================== MD-AT-02
@pytest.mark.parametrize("variant", ["no_approval_and_stale_ledger", "outside_scan_hours", "held_not_approved"])
def test_md_at_02_prices_stay_visible_while_entries_are_blocked_and_gates_do_not_move(tmp_path, variant):
    now = NOW
    if variant == "held_not_approved":
        db = tmp_path / "j.sqlite3"
        build_book(db).close()                                    # AAA OPEN (a held position) ...
        approved = approved_file(tmp_path, ())                    # ... but ZERO approvals
        sym = "AAA"
    else:
        db = plain_db(tmp_path)                                   # no snapshot -> ledger stale
        add_wl(db, "ZZZ")
        approved = approved_file(tmp_path, ())
        sym = "ZZZ"
        if variant == "outside_scan_hours":
            now = NOW.replace(hour=18, minute=30)                 # 14:30 CDT: session OPEN till 20:00Z but after both scans (10:05/13:05 CT)
    p = Prov(now)
    p.set(sym, spot=80.0 if sym == "AAA" else 61.25, strikes=(75.0,) if sym == "AAA" else (55.0,))
    before_non_md = hold(db)
    refresh(db, p, now)
    d = dash(db, approved, now=now + timedelta(seconds=30))
    try:
        b = mkt(d, sym)
        assert b["block"]["stock"]["price"] in ("80", "61.25")
        assert b["rows"], "put rows must stay visible"
        assert "APPROVED" != str(b["block"]["approval"].get("state") if isinstance(b["block"]["approval"], dict) else b["block"]["approval"])
        assert d.api("/api/overview")["data"]["attention"]["entries_blocked"] is True          # gates unchanged: still blocked
        assert b["block"]["reason_tracked"] == ("active holding" if sym == "AAA" else "watchlist")
        assert "not an entry alert" in text_of(d.req("GET", f"/market/{sym}")[2]).lower()
    finally:
        d.stop()
    assert hold(db) == before_non_md                               # nothing but md_* tables changed
    assert counts(db, "alert_outbox")["alert_outbox"] == 0


# =============================================================================================== MD-AT-03
def _view(db, tmp, now, sym="ZZZ"):
    d = dash(db, approved_file(tmp), now=now)
    try:
        b = mkt(d, sym)
        html = d.req("GET", f"/market/{sym}")[2]
    finally:
        d.stop()
    return b, text_of(html)


@pytest.fixture()
def good_a(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ", spot=61.25, bid=0.50, ask=0.54)
    refresh(db, p, NOW)
    return db, p, tmp_path


def test_md_at_03_outage_keeps_the_old_price_with_its_original_time_and_marks_it_stale(good_a):
    db, p, tmp = good_a
    snap = md_dump(db)
    p.fail("ZZZ", "FETCH_ERROR")
    t1 = NOW + timedelta(minutes=6)
    res = refresh(db, p, t1)
    assert [o.status for o in res.outcomes if o.attempted] == ["ERROR"]
    after = md_dump(db)
    for t in ("md_stock_latest", "md_option_latest", "md_stock_history", "md_option_history"):
        assert after[t] == snap[t], t                           # a failed fetch never touches good rows
    b, t = _view(db, tmp, t1 + timedelta(minutes=1))
    assert b["block"]["state"]["primary"] == "STALE" and b["block"]["status"]["status"] == "ERROR"
    assert b["block"]["stock"]["price"] == "61.25" and str(b["block"]["stock"]["retrieved_at_utc"]).startswith("2026-10-30T15:05:02")
    assert "10:05 CDT" in t and "failed" in t.lower()
    assert all(r["gate"]["marker"] == "NOT_EVALUATED" for r in b["rows"])          # gates not evaluated on stale data
    p.calls.clear()                                              # 5 min backoff after the first failure
    refresh(db, p, t1 + timedelta(minutes=4))
    assert p.calls == []
    refresh(db, p, t1 + timedelta(minutes=5))
    assert p.calls and {c[0] for c in p.calls} == {"ZZZ"}        # (a failing fetch is retried in-run per policy.provider_retries)


def test_md_at_03_rate_limit_is_a_distinct_state_from_outage(tmp_path):
    notes, texts = {}, {}
    for kind in ("RATE_LIMIT", "FETCH_ERROR"):
        sub = tmp_path / kind
        sub.mkdir()
        db = plain_db(sub)
        add_wl(db, "ZZZ")
        p = Prov(NOW)
        p.set("ZZZ")
        refresh(db, p, NOW)
        p.fail("ZZZ", kind)
        t1 = NOW + timedelta(minutes=6)
        refresh(db, p, t1)
        b, txt = _view(db, sub, t1 + timedelta(minutes=1))
        notes[kind], texts[kind] = b["block"], txt
    assert notes["RATE_LIMIT"]["status"]["status"] == "RATE_LIMITED" and notes["FETCH_ERROR"]["status"]["status"] == "ERROR"
    assert notes["RATE_LIMIT"]["state"]["primary"] == notes["FETCH_ERROR"]["state"]["primary"] == "STALE"
    assert notes["RATE_LIMIT"]["state"]["note"] != notes["FETCH_ERROR"]["state"]["note"]
    assert "rate limit" in notes["RATE_LIMIT"]["state"]["note"].lower()
    assert "RATE_LIMITED" in texts["RATE_LIMIT"] and "RATE_LIMITED" not in texts["FETCH_ERROR"]


def test_md_at_03_rate_limit_stops_the_whole_run_and_backs_off_15_then_30_minutes(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "AAA", "BBB", "CCC")
    p = Prov(NOW)
    for s in ("AAA", "BBB", "CCC"):
        p.set(s)
    p.fail("AAA", "RATE_LIMIT")                                   # never-attempted tie -> symbol order -> AAA first
    res = refresh(db, p, NOW)
    assert [c[0] for c in p.calls] == ["AAA"]                    # remaining symbols deferred: provider not hammered
    assert sum(1 for o in res.outcomes if o.skipped and "rate limit" in o.skipped) == 2
    p.calls.clear()
    refresh(db, p, NOW + timedelta(minutes=14, seconds=59))      # AAA backoff = 15 min
    assert "AAA" not in [c[0] for c in p.calls]
    p.calls.clear()
    t = NOW + timedelta(minutes=15)
    refresh(db, p, t)
    assert "AAA" in [c[0] for c in p.calls]
    p.calls.clear()                                              # 2nd consecutive rate limit -> 30 min
    refresh(db, p, t + timedelta(minutes=29, seconds=59))
    assert "AAA" not in [c[0] for c in p.calls]


def test_md_at_03_plain_error_backoff_doubles_5_10_20_30_and_caps(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.fail("ZZZ", "FETCH_ERROR")
    t = NOW
    for wait_min in (5, 10, 20, 30, 30):
        p.calls.clear()
        refresh(db, p, t)
        assert p.calls, wait_min
        p.calls.clear()
        refresh(db, p, t + timedelta(minutes=wait_min, seconds=-1))
        assert p.calls == [], wait_min
        t = t + timedelta(minutes=wait_min)


@pytest.mark.parametrize("age_s,stale", [(899, False), (900, False), (901, True)])
def test_md_at_03_stale_boundary_900_seconds_while_open(good_a, age_s, stale):
    db, p, tmp = good_a
    b, _ = _view(db, tmp, NOW + timedelta(seconds=age_s))        # no refresh in between (a page read only)
    assert ("STALE" in b["block"]["state"]["labels"]) is stale
    assert b["block"]["stock"]["age_seconds"] == age_s            # age follows the clock, never resets


def test_md_at_03_reloading_never_resets_age_and_a_refresh_request_does_not_fetch(good_a):
    db, p, tmp = good_a
    d = dash(db, approved_file(tmp), now=NOW + timedelta(minutes=3))
    try:
        a1 = mkt(d, "ZZZ")["block"]["stock"]
        d.now = NOW + timedelta(minutes=4)
        a2 = mkt(d, "ZZZ")["block"]["stock"]
        assert a1["retrieved_at_utc"] == a2["retrieved_at_utc"] and (a1["age_seconds"], a2["age_seconds"]) == (180, 240)
        html = d.req("GET", "/market/ZZZ")[2]
        body = f"csrf={csrf_of(html)}&scope=ZZZ&back=market".encode()
        n = len(p.calls)
        st, hd, _ = d.req("POST", "/market/refresh-request", origin=d.origin, body=body, headers=FORM)
        assert st == 303 and hd["location"] == "/market/ZZZ?msg=queued"
        a3 = mkt(d, "ZZZ")["block"]["stock"]
        assert a3["retrieved_at_utc"] == a1["retrieved_at_utc"] and a3["age_seconds"] == 240 and len(p.calls) == n
    finally:
        d.stop()


def test_md_at_03_market_closed_is_labelled_and_is_not_presented_as_a_live_price(tmp_path):
    db = plain_db(tmp_path, now=SAT)
    add_wl(db, "ZZZ", now=SAT)
    p = Prov(SAT)
    p.set("ZZZ", spot=61.25)
    res = refresh(db, p, SAT)
    assert res.session == "CLOSED" and [o.status for o in res.outcomes] == ["MARKET_CLOSED"]
    b, t = _view(db, tmp_path, SAT + timedelta(minutes=1))
    assert "MARKET-CLOSED" in b["block"]["state"]["labels"] and "UNTIMESTAMPED" in b["block"]["state"]["labels"]
    assert "not a live price" in t.lower()


def test_md_at_03_provider_timestamp_present_gives_delayed_and_absent_gives_untimestamped(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ", spot_asof_utc="2026-10-30T15:00:02Z", quote_asof_utc="2026-10-30T15:00:02Z")   # 300 s before retrieval/now
    refresh(db, p, NOW)
    b, t = _view(db, tmp_path, NOW)                              # market age 300 s > 60 s
    labs = b["block"]["state"]["labels"]
    assert "DELAYED" in labs and "UNTIMESTAMPED" not in labs
    assert str(b["block"]["stock"]["market_time_utc"]).startswith("2026-10-30T15:00:02")
    assert str(b["rows"][0]["quote_time_utc"]).startswith("2026-10-30T15:00:02")


def test_md_at_03_no_fixture_fallback_unavailable_when_provider_never_succeeded(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.fail("ZZZ", "FETCH_ERROR")
    refresh(db, p, NOW)
    b, t = _view(db, tmp_path, NOW + timedelta(minutes=1))
    assert b["block"]["stock"] is None and b["rows"] == [] and b["block"]["state"]["primary"] == "UNAVAILABLE"
    assert counts(db, "md_stock_latest", "md_option_latest") == {"md_stock_latest": 0, "md_option_latest": 0}
    assert not re.search(r"\(LAST\)", t)
    p.script["ZZZ"] = []                                         # an empty chain is PARTIAL (never OK) and stores nothing
    res = refresh(db, p, NOW + timedelta(minutes=10))
    assert [o.status for o in res.outcomes if o.attempted] == ["PARTIAL"]
    assert counts(db, "md_stock_latest", "md_option_latest") == {"md_stock_latest": 0, "md_option_latest": 0}


@pytest.mark.parametrize("label,over", [
    ("zero_bid_ask", dict(bid=0.0, ask=0.0)),
    ("crossed", dict(bid=0.60, ask=0.50)),
    ("nan_both", dict(bid=float("nan"), ask=float("nan"))),
    ("zero_bid_only", dict(bid=0.0, ask=0.54)),
    ("missing_ask", dict(bid=0.5, ask=None)),
    ("negative", dict(bid=-1.0, ask=0.54)),
])
def test_md_at_03_unusable_option_quote_never_replaces_a_good_one(good_a, label, over):
    db, p, tmp = good_a
    good = opt_row(db)
    assert good[:3] == ("0.5", "0.54", A_TS)
    p.fetched_at = NOW + timedelta(minutes=6)
    p.set("ZZZ", spot=61.25, **over)
    refresh(db, p, NOW + timedelta(minutes=6))
    assert opt_row(db) == good, label                            # untouched, ORIGINAL retrieval time kept


@pytest.mark.parametrize("spot", [0.0, float("nan"), -5.0, None])
def test_md_at_03_unusable_stock_price_never_replaces_a_good_one(good_a, spot):
    db, p, tmp = good_a
    before = stock_row(db)
    assert before[0] == "61.25"
    p.fetched_at = NOW + timedelta(minutes=6)
    p.set("ZZZ", spot=spot)
    refresh(db, p, NOW + timedelta(minutes=6))
    assert stock_row(db) == before


def test_md_at_03_inconsistent_stock_prices_across_rows_store_no_price(good_a):
    db, p, tmp = good_a
    before = stock_row(db)
    p.fetched_at = NOW + timedelta(minutes=6)
    p.script["ZZZ"] = [raw_row(strike=55.0, spot=61.25, expiration=EXP.isoformat()),
                       raw_row(strike=50.0, spot=70.0, expiration=EXP.isoformat())]
    refresh(db, p, NOW + timedelta(minutes=6))
    assert stock_row(db) == before


# =============================================================================================== MD-AT-04
def _book_with_scan_obs(tmp, scan_age_min: int):
    from web_helpers import add_observation
    db = tmp / "j.sqlite3"
    build_book(db).close()
    add_observation(db, "AAA", "75", EXP, NOW - timedelta(minutes=scan_age_min), "0.40", "0.44", delta="-0.12", spot="80.00")
    return db


def _opn_price_check(db, tmp, now):
    d = dash(db, approved_file(tmp, ("AAA",)), now=now)
    try:
        return d.api("/api/positions/OPN")["data"]["position"]
    finally:
        d.stop()


def test_md_at_04_newer_monitor_observation_beats_older_scan_and_keeps_contract_identity(tmp_path):
    """OPN: AAA put 75 exp 2026-12-04, actual credit 0.50, 1 contract x100.
    entry-scan obs (NOW-30 min): bid .40 ask .44.   monitor obs (NOW-5 min): bid .28 ask .30, spot 80.
    gross buy-to-close P&L at the NEWER ask 0.30 = (0.50 - 0.30) * 100 = 20.00; at the older ask .44 it would be 6.00."""
    from csp_alerts.jobs.monitor import run_monitor
    db = _book_with_scan_obs(tmp_path, 30)
    t_mon = NOW - timedelta(minutes=5)
    src = FakeSource({"AAA": [raw_row(now=t_mon, strike=75.0, bid=0.28, ask=0.30, delta=-0.12, spot=80.0)]}, now=t_mon)
    j = jopen(db, t_mon)
    run_monitor(journal=j, policy=POL, source=src, now_utc=t_mon)
    j.close()
    c = sqlite3.connect(db)
    src_col, ask = c.execute("SELECT source, ask FROM md_option_latest WHERE symbol='AAA' AND strike='75'").fetchone()
    c.close()
    assert (src_col, ask) == ("MONITOR", "0.3")                 # the monitor persisted its observation
    pos = _opn_price_check(db, tmp_path, NOW)
    ev = pos["price_check"]["evidence"]
    assert pos["price_check"]["status"] == "usable"
    assert D(str(ev["ask"])) == D("0.30") and D(str(ev["bid"])) == D("0.28")
    assert "monitor" in ev["source"].lower() and ev["contract"] == "AAA261204P00075000"          # identity preserved
    assert D(str(pos["pnl"]["gross_usd"])) == D("20.00")


def test_md_at_04_an_older_store_row_does_not_override_a_newer_scan_observation(tmp_path):
    """Reverse ordering: scan obs NOW-1 min (ask .44) newer than store row NOW-20 min (ask .30) -> the newer scan ask wins:
    gross = (0.50 - 0.44) * 100 = 6.00."""
    from web_helpers import add_observation
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    t_old = NOW - timedelta(minutes=20)
    p = Prov(t_old)
    p.set("AAA", spot=80.0, strikes=(75.0,), bid=0.28, ask=0.30, delta=-0.12)
    refresh(db, p, t_old)
    add_observation(db, "AAA", "75", EXP, NOW - timedelta(minutes=1), "0.40", "0.44")
    pos = _opn_price_check(db, tmp_path, NOW)
    assert D(str(pos["price_check"]["evidence"]["ask"])) == D("0.44") and D(str(pos["pnl"]["gross_usd"])) == D("6.00")


def test_md_at_04_a_stock_price_is_never_used_as_an_option_ask(tmp_path):
    """Store has AAA stock 80 and a DIFFERENT contract (strike 70). OPN is strike 75: no quote for it -> P&L unavailable,
    and 80 appears nowhere as an ask/P&L input (gross at ask 80 would be (0.50-80)*100 = -7950)."""
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    p = Prov(NOW)
    p.set("AAA", spot=80.0, strikes=(70.0,), bid=0.28, ask=0.30)
    refresh(db, p, NOW)
    pos = _opn_price_check(db, tmp_path, NOW)
    assert pos["price_check"]["status"] == "unavailable" and pos["pnl"] is None
    assert "-7950" not in str(pos) and "7950" not in str(pos)


def test_md_at_04_stale_stored_option_quote_gives_no_pnl(tmp_path):
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    t_old = NOW - timedelta(minutes=45)                          # > 2 monitor intervals (30 min)
    p = Prov(t_old)
    p.set("AAA", spot=80.0, strikes=(75.0,), bid=0.28, ask=0.30, delta=-0.12)
    refresh(db, p, t_old)
    pos = _opn_price_check(db, tmp_path, NOW)
    assert pos["pnl"] is None and pos["price_check"]["status"] == "unavailable"


# =============================================================================================== MD-AT-06
def test_md_at_06_open_session_interval_is_5_minutes(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ")
    refresh(db, p, NOW)
    p.calls.clear()
    refresh(db, p, NOW + timedelta(minutes=4))                   # 240 s < 270 s (5 min less 30 s grace)
    assert p.calls == []
    refresh(db, p, NOW + timedelta(minutes=5))
    assert len(p.calls) == 1


def test_md_at_06_closed_session_interval_is_hourly(tmp_path):
    db = plain_db(tmp_path, now=SAT)
    add_wl(db, "ZZZ", now=SAT)
    p = Prov(SAT)
    p.set("ZZZ")
    refresh(db, p, SAT)
    p.calls.clear()
    refresh(db, p, SAT + timedelta(minutes=30))
    refresh(db, p, SAT + timedelta(minutes=57))                  # 3420 s < 3570 s
    assert p.calls == []
    refresh(db, p, SAT + timedelta(minutes=60))
    assert len(p.calls) == 1


def test_md_at_06_no_duplicate_execution_and_60_second_floor_even_when_forced(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ")
    refresh(db, p, NOW)
    refresh(db, p, NOW)                                          # double-fired task at the same instant
    refresh(db, p, NOW + timedelta(seconds=59), force=True)      # forced, but inside the 60 s floor
    assert len(p.calls) == 1
    refresh(db, p, NOW + timedelta(seconds=60), force=True)      # forced skips only the regular interval
    assert len(p.calls) == 2


def test_md_at_06_owner_request_bypasses_interval_but_not_backoff_or_the_60s_floor(tmp_path):
    from csp_alerts.market import observations as obs
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ", "YYY")
    p = Prov(NOW)
    p.set("ZZZ")
    p.fail("YYY")
    refresh(db, p, NOW)                                          # ZZZ ok, YYY fails -> 5 min backoff
    p.calls.clear()
    j = jopen(db, NOW + timedelta(seconds=90))
    assert obs.request_refresh(j, "ALL", "t", {"ZZZ", "YYY"})[0] == "queued"
    j.close()
    refresh(db, p, NOW + timedelta(seconds=100))                 # elapsed 100 s >= 60: ZZZ fetched early; YYY still in backoff
    assert [c[0] for c in p.calls] == ["ZZZ"]
    p.calls.clear()
    j = jopen(db, NOW + timedelta(seconds=200))
    assert obs.request_refresh(j, "ZZZ", "t", {"ZZZ", "YYY"})[0] == "queued"
    j.close()
    refresh(db, p, NOW + timedelta(seconds=130))                 # only 30 s after ZZZ's last attempt (100 s): floor blocks it
    assert p.calls == []


def test_md_at_06_per_run_cap_is_16_symbols_longest_waiting_first(tmp_path):
    """build_book holds 7 symbols (AAA..GGG) + 12 watchlist = 19 tracked > 16."""
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    wl = [f"W{chr(65 + i)}" for i in range(12)]
    add_wl(db, *wl)
    p = Prov(NOW)
    for s in wl + ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]:
        p.set(s)
    res = refresh(db, p, NOW)
    assert len(p.calls) == 16 and len({c[0] for c in p.calls}) == 16
    assert sum(1 for o in res.outcomes if o.skipped == "per-run symbol cap") == 3
    fetched = {c[0] for c in p.calls}
    p.calls.clear()
    refresh(db, p, NOW + timedelta(seconds=61))                  # the 3 never-attempted symbols go first; the 16 are not yet due
    assert len(p.calls) == 3 and not ({c[0] for c in p.calls} & fetched)


def test_md_at_06_at_most_8_nearest_expiries_per_symbol_are_stored(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    rows = []
    for k in range(10):                                          # 10 expiries 21, 28, ... 84 days out (all listed by the provider)
        e = (NOW + timedelta(days=21 + 7 * k)).date()
        rows.append(raw_row(strike=55.0, spot=61.25, expiration=e.isoformat(), bid=0.5, ask=0.54))
    p.script["ZZZ"] = rows
    refresh(db, p, NOW)
    c = sqlite3.connect(db)
    exps = [r[0] for r in c.execute("SELECT DISTINCT expiration FROM md_option_latest WHERE symbol='ZZZ' ORDER BY 1")]
    c.close()
    assert len(exps) == 8 and exps[0] == (NOW + timedelta(days=21)).date().isoformat()
    assert exps[-1] == (NOW + timedelta(days=21 + 7 * 7)).date().isoformat()


def test_md_at_06_job_changes_nothing_outside_md_tables_and_sends_nothing(tmp_path):
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    ap = approved_file(tmp_path, ("AAA",))
    ap_bytes = ap.read_bytes()
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ")
    p.set("AAA", spot=80.0, strikes=(75.0,))
    for s in ("BBB", "CCC", "DDD", "EEE", "FFF", "GGG"):
        p.fail(s)
    before = hold(db)
    for i in range(3):
        refresh(db, p, NOW + timedelta(minutes=6 * i))
    assert hold(db) == before                                    # audit_log, positions, snapshots, outbox, approvals, run_status, ...
    assert ap.read_bytes() == ap_bytes                           # config/approved.json equivalent untouched
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM md_refresh_runs").fetchone()[0] >= 1
    c.close()
    # the job module has no way to send: no telegram / outbox / ledger-write names in its import graph
    import ast
    tree = ast.parse((__import__("pathlib").Path(__file__).resolve().parents[2] / "src/csp_alerts/jobs/market_refresh.py").read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    assert not [m for m in mods if re.search(r"telegram|outbox|runner|sendmode|requests|httpx|urllib|socket|subprocess", m, re.I)], mods


def test_md_at_06_cli_market_refresh_has_no_send_and_runs_with_the_injected_source(monkeypatch, tmp_path):
    from s3_helpers import run_cli
    db = tmp_path / "j.sqlite3"
    j = jopen(db, NOW)
    j.migrate()
    from csp_alerts.journal import watchlist
    watchlist.add_symbol(j, "ZZZ", actor="t", source="t")
    j.close()
    before = hold(db)
    src = FakeSource({"ZZZ": [raw_row(strike=55.0, spot=61.25, bid=0.5, ask=0.54)]})
    code, out, err = run_cli(monkeypatch, tmp_path, ["market-refresh", "--send"], now=NOW, source=src)
    assert code == 2 and not src.calls                           # argparse refuses --send
    code, out, err = run_cli(monkeypatch, tmp_path, ["market-refresh", "--dry-run"], now=NOW, source=src)
    assert code == 0, out + err
    assert md_dump(db)["md_stock_latest"] == []                  # dry-run persists nothing at all
    code, out, err = run_cli(monkeypatch, tmp_path, ["market-refresh"], now=NOW, source=src)
    assert code == 0, out + err
    assert "Yahoo indicative data" in out and "Quote age unknown" in out
    assert md_dump(db)["md_stock_latest"] != [] and hold(db) == before
    assert shutil.which("python") is not None


def test_md_at_06_second_instance_exits_3_while_the_lock_is_held(tmp_path):
    import os
    import subprocess
    import sys
    (tmp_path / "locks").mkdir()
    code = ("import sys\nfrom csp_alerts.lock import single_instance\n"
            "with single_instance(sys.argv[1]):\n    print('READY', flush=True)\n    sys.stdin.readline()\n")
    holder = subprocess.Popen([sys.executable, "-I", "-c", code, str(tmp_path / "locks" / "market-refresh.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src")})
    try:
        assert holder.stdout.readline().strip() == "READY"
        j = jopen(tmp_path / "j.sqlite3", NOW)                   # EMPTY journal: no tracked symbol, so nothing could be fetched anyway
        j.migrate()
        j.close()
        ef = tmp_path / "t.env"
        ef.write_text("", encoding="utf-8")
        ap = tmp_path / "approved.json"
        ap.write_text('{"approved_underlyings": []}', encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME")}
        env["CSP_DATABASE_URL"] = "sqlite:///" + (tmp_path / "j.sqlite3").as_posix()
        env["CSP_DRY_RUN"] = "true"
        env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src")
        r = subprocess.run([sys.executable, "-I", "-m", "csp_alerts.cli", "--env-file", str(ef), "--approved", str(ap), "market-refresh"],
                           capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
        assert r.returncode == 3, (r.stdout, r.stderr)
        c = sqlite3.connect(tmp_path / "j.sqlite3")
        assert c.execute("SELECT COUNT(*) FROM md_refresh_runs").fetchone()[0] == 0
        c.close()
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=20)
