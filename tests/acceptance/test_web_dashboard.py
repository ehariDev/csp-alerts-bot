"""Acceptance for the local read-only dashboard, WEB-0/WEB-1 (WD-010..018; WD-AT-02, 03, 04, 05, 06, 07, 08).
Oracles come from requirements/WEB_DASHBOARD_ROADMAP.md and the backend rules; see web_helpers.py for the hand arithmetic.
Synthetic journal, loopback only, no Telegram, frozen clock. Secrets/hashes are never printed."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from acc_helpers import NOW, POLICY_PATH, TODAY, policy
from s2_helpers import OPEN_SESSION, FakeSource, appr, raw_row
from s3_helpers import Clock, ledger, write_approved
from web_helpers import (EXP35, EXPECT, PW, Dash, add_observation, build_book, fingerprint, insert_alerts, no_floats,
                         scrypt_hash)

from csp_alerts.jobs.scan import run_scan
from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.store import Journal
from csp_alerts.market.stockpile import SourceError

REPO = Path(__file__).resolve().parents[2]
VIEWS = ["/", "/candidates", "/positions", "/alerts", "/activity", "/runs", "/health"]


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "j.sqlite3"
    j = build_book(db)
    j.close()
    ap = write_approved(tmp_path / "approved.json", ("AAA",))
    d = Dash(db, ap)
    assert d.login() == 303
    d.db, d.dir, d.approved = db, tmp_path, ap
    yield d
    d.stop()


def codes(items) -> set[str]:
    return {i["code"] for i in items}


# ============================================================================ WD-AT-02: numbers equal backend/hand oracle
def test_overview_numbers_match_hand_oracle(env):
    o = env.api("/api/overview")["data"]
    ex = o["exposure"]
    assert D(ex["put_collateral_usd"]) == EXPECT["put_collateral"]          # 7500+5000+2000+1000+500
    assert D(ex["owned_stock_usd"]) == EXPECT["owned_stock"]                # 100 x 40
    assert D(ex["assigned_pending_usd"]) == EXPECT["assigned_pending"]      # 30 x 100
    assert D(ex["aggregate_usd"]) == EXPECT["aggregate"]                    # 16000+4000+3000
    assert ex["active_contracts"] == EXPECT["contracts"]
    lim = {l["name"]: l for l in o["limits"]}
    pilot = lim["Pilot put-collateral cap"]
    assert (D(pilot["limit"]), D(pilot["used"]), D(pilot["room"])) == (D("15000"), D("16000"), D("-1000"))
    assert pilot["binding"] is True and sum(1 for l in o["limits"] if l["binding"]) == 1   # smallest room is binding
    assert D(lim["Normal ceiling: aggregate exposure"]["room"]) == D("2000")               # 25000-23000
    assert D(lim["Normal ceiling: put collateral"]["room"]) == D("9000")                   # 25000-16000
    ctr = lim["Active short-put contracts"]
    assert (ctr["used"], ctr["limit"], ctr["room"]) == (5, 4, -1)
    em = next(l for n, l in lim.items() if n.startswith("Emergency"))
    assert D(em["limit"]) == D("30000") and em["breached"] is False and em["room"] is None   # 23000 <= 30000; never headroom
    dd = o["drawdown"]
    assert D(dd["peak_equity_usd"]) == D("100000") and D(dd["latest_equity_usd"]) == D("96500")
    assert D(dd["drawdown_fraction"]) == D("0.035")                          # (100000-96500)/100000
    assert dd["level"] == "PAUSE"                                            # 3% <= 3.5% < 5%
    assert (D(dd["thresholds"]["pause"]), D(dd["thresholds"]["halt"])) == (D("0.03"), D("0.08"))
    s = o["latest_confirmed_snapshot"]
    assert D(s["equity_usd"]) == D("96500") and D(s["free_cash_usd"]) == D("60000") and s["confirmed"] is True
    assert s["asof_utc"] == "2026-10-30T14:05:02Z" and D(s["age_hours"]) == D("1")
    assert o["account"]["scope"] == "paper_primary" and o["account"]["paper"] is True and o["account"]["profile"] == "pilot"
    assert D(o["account"]["baseline_equity_usd"]) == D("100000")


def test_overview_equals_backend_functions_on_same_journal(env):
    """Cross-check against the existing backend output (exposure, drawdown, reconcile) computed directly."""
    from csp_alerts.journal.reconcile import reconcile
    from csp_alerts.policy.config import load_policy
    from csp_alerts.policy.drawdown import compute_drawdown
    from csp_alerts.policy.portfolio import compute_exposure
    j = Journal(env.db, clock=lambda: NOW)
    book = j.load_book()
    pol = load_policy(POLICY_PATH)
    ex = compute_exposure(book)
    rep = reconcile(book, pol, NOW)
    dd = compute_drawdown(book.snapshots, pol, baseline_asof_utc=book.last_risk_reset_utc,
                          baseline_equity_usd=book.baseline_equity_usd, cash_flows=book.cash_flows)
    j.close()
    o = env.api("/api/overview")["data"]
    assert D(o["exposure"]["aggregate_usd"]) == ex.aggregate_equity_risk_usd == EXPECT["aggregate"]
    assert {i["code"] for i in o["reconciliation"]["issues"]} == {i.code for i in rep.issues}
    assert o["reconciliation"]["blocking"] == rep.blocking
    assert D(o["drawdown"]["drawdown_fraction"]) == dd.drawdown
    # every blocking reconcile issue is shown in the blockers block that sits first on the page
    assert {i.code for i in rep.issues if i.severity == "BLOCK"} <= codes(o["attention"]["items"])


def test_reserved_and_review_states_visible_and_exposure_labelled_budget(env):
    api = env.api("/api/overview")["data"]
    by = {x["trade_id"]: x for x in api["obligations"]}
    # PENDING, WORKING, CLOSE_WORKING, ADJUSTMENT_REVIEW, ASSIGNED_PENDING_RECONCILIATION must all be listed
    for tid, state in [("PND", "PENDING"), ("WRK", "WORKING"), ("CLW", "CLOSE_WORKING"), ("ADJ", "ADJUSTMENT_REVIEW"),
                       ("ASG", "ASSIGNED_PENDING_RECONCILIATION")]:
        assert by[tid]["state"] == state, (tid, by.get(tid))
    assert D(by["WRK"]["collateral_usd"]) == D("5000") and by["WRK"]["reserves_collateral"] is True
    assert by["ASG"]["reserves_collateral"] is False                       # spec: assigned-pending does not reserve collateral
    st, _, html = env.req("GET", "/")
    assert st == 200
    low = html.lower()
    for tid, state in [("PND", "PENDING"), ("WRK", "WORKING"), ("CLW", "CLOSE_WORKING"), ("ADJ", "ADJUSTMENT_REVIEW"),
                       ("ASG", "ASSIGNED_PENDING_RECONCILIATION")]:
        assert tid in html and state in html, tid
    assert "exposure budget" in low and "not maximum possible loss" in low
    assert not re.search(r"maximum (possible )?loss\s*[:=]\s*\$", low)
    assert "binding" in low


def test_positions_numbers_and_groups(env):
    allp = {r["trade_id"]: r for r in env.api("/api/positions?group=all")["data"]["rows"]}
    assert set(allp) == {"OPN", "WRK", "CLW", "ADJ", "PND", "ASG", "CLS", "DRF"}
    expect = {"OPN": ("75.00", 35), "WRK": ("50.00", 35), "CLW": ("20.00", 21), "ADJ": ("10.00", 35), "PND": ("5.00", 35),
              "ASG": ("30.00", 35)}
    for tid, (strike, dte) in expect.items():
        r = allp[tid]
        assert D(r["strike"]) == D(strike) and D(r["collateral_usd"]) == D(strike) * 100, tid
        assert r["dte"] == dte, (tid, r["dte"])          # Oct30 -> Dec4 = 35 ; Oct30 -> Nov20 = 21
    o = allp["OPN"]
    assert (D(o["open_credit_per_share"]), D(o["fees_usd"]), o["expiration"], o["contracts"], o["multiplier"]) == (
        D("0.50"), D("0.65"), "2026-12-04", 1, 100)
    groups = {"active": {"OPN", "CLW"}, "reserved": {"PND", "WRK"}, "review": {"ADJ", "ASG"}, "draft": {"DRF"},
              "history": {"CLS"}, "live": {"OPN", "CLW", "PND", "WRK", "ADJ", "ASG"}}
    for g, want in groups.items():
        got = {r["trade_id"] for r in env.api(f"/api/positions?group={g}")["data"]["rows"]}
        assert got == want, (g, got)
    assert {r["trade_id"] for r in env.api("/api/positions?group=all&ticker=AAA")["data"]["rows"]} == {"OPN"}


def test_position_warnings_triggered_at_exact_boundaries(env):
    allp = {r["trade_id"]: r for r in env.api("/api/positions?group=all")["data"]["rows"]}
    trig = lambda tid: {w["trigger"] for w in allp[tid]["warnings"]}
    assert "TIME_EXIT" in trig("CLW")           # DTE 21 == close_review_at_dte_calendar 21: at the limit triggers
    assert "TIME_EXIT" not in trig("OPN")       # DTE 35
    assert "ADJUSTMENT_REVIEW" in trig("ADJ")
    assert "ASSIGNMENT_PENDING_RECONCILIATION" in trig("ASG")
    obl = next(w for w in allp["ASG"]["warnings"] if w["trigger"] == "ASSIGNMENT_PENDING_RECONCILIATION")
    assert "3000" in obl["detail"]               # 30.00 x 100 x 1
    assert allp["CLS"]["warnings"] == [] and "no open-position advice" in allp["CLS"]["advice_note"].lower()


def test_position_detail_links_audit_and_closed_position_has_no_advice(env):
    d = env.api("/api/positions/OPN")["data"]
    types = [a["event_type"] for a in d["audit"]]
    assert "POSITION_DRAFT_CREATED" in types and types.count("POSITION_TRANSITION") >= 2     # DRAFT->PENDING, PENDING->OPEN
    st, _, html = env.req("GET", "/positions/CLS")
    assert st == 200 and "no open-position advice" in html.lower()
    assert env.req("GET", "/positions/NOPE")[0] == 404


# ============================================================================ WD-AT-04: stale/unknown data, P&L
def _pos(env, tid):
    return next(r for r in env.api("/api/positions?group=all")["data"]["rows"] if r["trade_id"] == tid)


def test_missing_pnl_is_unavailable_not_zero(env):
    for tid in ("OPN", "CLW", "ADJ"):
        p = _pos(env, tid)
        assert p["pnl"] is None and p["price_check"]["status"] == "unavailable", tid
    st, _, html = env.req("GET", "/positions/OPN")
    assert "unavailable" in html.lower()
    assert not re.search(r"P&amp;L[^<]{0,40}\$?\s*0(\.00)?\b", html)


def test_fresh_quote_gives_backend_pnl_stale_quote_does_not(env):
    # credit 0.50, ask 0.30 -> gross (0.50-0.30)*100*1 = 20.00 ; net of recorded fees 0.65 = 19.35
    add_observation(env.db, "AAA", "75.00", EXP35, NOW - timedelta(minutes=10), bid="0.28", ask="0.30")
    p = _pos(env, "OPN")
    assert p["price_check"]["status"] == "usable"
    assert D(p["pnl"]["gross_usd"]) == D("20.00") and D(p["pnl"]["net_of_fees_usd"]) == D("19.35")
    assert "indicative" in p["pnl"]["basis"].lower()
    # stale quote on another position: 3 hours old (> 2 x 15 min) -> unavailable, never a number
    add_observation(env.db, "CCC", "20.00", date(2026, 11, 20), NOW - timedelta(hours=3), bid="0.10", ask="0.12")
    c = _pos(env, "CLW")
    assert c["pnl"] is None and c["price_check"]["status"] == "unavailable"
    # the page refreshing later does NOT make the old quote live: 40 minutes after the quote it becomes unavailable
    env.now = NOW + timedelta(minutes=31)          # quote now 41 min old
    assert _pos(env, "OPN")["pnl"] is None


@pytest.mark.parametrize("bid,ask", [("0.28", "0"), ("0.28", None), ("0.40", "0.30"), (None, "0.30")])
def test_invalid_quotes_never_produce_pnl(env, bid, ask):
    add_observation(env.db, "AAA", "75.00", EXP35, NOW - timedelta(minutes=5), bid=bid, ask=ask)
    p = _pos(env, "OPN")
    assert p["pnl"] is None, (bid, ask, p["price_check"])      # ask 0 would fabricate a +50.00 gain; crossed/one-sided is unusable


def test_data_time_and_refresh_time_are_distinct_and_never_upgrade_staleness(env):
    first = env.api("/api/overview")
    env.now = NOW + timedelta(hours=40)            # 41h after the snapshot; policy max age 30h
    second = env.api("/api/overview")
    assert first["meta"]["rendered_at_utc"] != second["meta"]["rendered_at_utc"]
    assert first["meta"]["observed"]["latest_snapshot_asof_utc"] == second["meta"]["observed"]["latest_snapshot_asof_utc"]
    assert first["data"]["latest_snapshot"]["asof_utc"] == second["data"]["latest_snapshot"]["asof_utc"]
    assert D(second["data"]["latest_snapshot"]["age_hours"]) == D("41")
    assert "SNAPSHOT_STALE" in codes(second["data"]["attention"]["items"]) and "SNAPSHOT_STALE" not in codes(first["data"]["attention"]["items"])
    assert second["data"]["attention"]["ok"] is False
    st, _, html = env.req("GET", "/")
    low = html.lower()
    assert "refreshed" in low and "not data time" in low


def test_central_time_with_zone_and_utc_evidence_across_dst(env):
    st, _, html = env.req("GET", "/")
    # NOW 15:05:02Z in CDT (UTC-5) is 10:05:02 ; snapshot 14:05:02Z is 09:05:02 CDT
    assert "10:05" in html and "CDT" in html and "15:05" in html and "09:05" in html
    env.now = datetime(2026, 11, 2, 15, 5, 2, tzinfo=timezone.utc)     # after DST ends Nov 1: CST (UTC-6) -> 09:05
    st, _, html = env.req("GET", "/")
    assert re.search(r"11-02 09:05(:02)? CST", html), "refresh time must be 09:05 CST after DST ends"
    assert re.search(r"10-30 09:05(:02)? CDT", html), "snapshot (before DST end) must keep CDT"


# ============================================================================ WD-AT-03: distinct states
class Scn:
    def __init__(self, tmp):
        self.tmp = tmp
        self.db = tmp / "j.sqlite3"


def _scan(tmp_path, chains, tickers, *, snapshot="ok"):
    db = tmp_path / "j.sqlite3"
    if snapshot == "ok":
        j, _ = ledger(path=db)
    else:
        clock = Clock(NOW - timedelta(hours=45))
        j = Journal(db, clock=clock)
        j.migrate()
        j.confirm_baseline(D("100000"))
        if snapshot == "stale":
            clock.t = NOW - timedelta(hours=40)
            j.add_snapshot(AccountSnapshot("s0", NOW - timedelta(hours=40), D("100000"), D("70000"), True, "v1"))
        elif snapshot == "unconfirmed":
            clock.t = NOW - timedelta(hours=2)
            j.add_snapshot(AccountSnapshot("s0", clock.t, D("100000"), D("70000"), False, "v1"))
        clock.t = NOW
    res = run_scan(journal=j, policy=policy(), approved=[appr(t) for t in tickers], source=FakeSource(chains), now_utc=NOW,
                   session=OPEN_SESSION, sleep=lambda s: None)
    j.close()
    ap = write_approved(tmp_path / "approved.json", tickers)
    d = Dash(db, ap)
    assert d.login() == 303
    return d, res


def _states(d):
    c = d.api("/api/candidates")["data"]
    return c, {s["code"] for s in c.get("states", [])}


GOOD = [raw_row()]                                                 # spec fixture: passes every gate (bid .50 ask .54 delta -.13)
BAD_DELTA = [raw_row(delta=-0.30)]                                  # |delta| .30 > .15


def test_wd_at_03_states_are_distinct_and_nothing_is_invented(tmp_path):
    results = {}
    for name, kw in {
        "empty": dict(chains={}, tickers=()),
        "noopp": dict(chains={"AAA": BAD_DELTA}, tickers=("AAA",)),
        "partial": dict(chains={"AAA": GOOD, "BBB": SourceError("TIMEOUT", "provider timeout")}, tickers=("AAA", "BBB")),
        "outage": dict(chains={"AAA": SourceError("TIMEOUT", "x"), "BBB": SourceError("HTTP_5XX", "y")}, tickers=("AAA", "BBB")),
        "stale": dict(chains={"AAA": GOOD}, tickers=("AAA",), snapshot="stale"),
        "unconfirmed": dict(chains={"AAA": GOOD}, tickers=("AAA",), snapshot="unconfirmed"),
    }.items():
        sub = tmp_path / name
        sub.mkdir()
        d, _ = _scan(sub, **kw)
        try:
            c, st = _states(d)
            results[name] = (c, st, d.api("/api/overview")["data"]["attention"])
            html = d.req("GET", "/candidates")[2]
            results[name] += (html,)
        finally:
            d.stop()
    c, st, att, html = results["empty"]
    assert "EMPTY_WHITELIST" in st and not st & {"PROVIDER_OUTAGE", "PARTIAL_COVERAGE", "NO_QUALIFYING_CONTRACTS"}
    assert c["rows"] == [] and "empty whitelist" in html.lower()
    c, st, att, html = results["noopp"]
    assert "NO_QUALIFYING_CONTRACTS" in st and not st & {"EMPTY_WHITELIST", "PROVIDER_OUTAGE", "PARTIAL_COVERAGE", "BLOCKED_LEDGER"}
    assert c["counts"]["passing"] == 0 and c["counts"]["rejected"] >= 1 and c["rows"] == []        # criteria never relaxed
    c, st, att, html = results["partial"]
    assert "PARTIAL_COVERAGE" in st and "PROVIDER_OUTAGE" not in st
    assert [r["ticker"] for r in c["rows"]] == ["AAA"] and D(c["rows"][0]["strike"]) == D("75.00")
    assert D(c["rows"][0]["bid"]) == D("0.5") and D(c["rows"][0]["ask"]) == D("0.54") and c["rows"][0]["dte"] == 35
    assert {u["ticker"] for u in c["coverage"]["unavailable"]} == {"BBB"} and "BBB" in html
    c, st, att, html = results["outage"]
    assert "PROVIDER_OUTAGE" in st and "PARTIAL_COVERAGE" not in st and "EMPTY_WHITELIST" not in st
    assert c["rows"] == [] and c["counts"]["passing"] == 0 and c["counts"]["stale"] == 0
    assert "not a no-trade result" in html.lower() or "outage" in html.lower()
    c, st, att, html = results["stale"]
    assert "BLOCKED_LEDGER" in st and "SNAPSHOT_STALE" in codes(att["items"]) and att["ok"] is False
    c, st, att, html = results["unconfirmed"]
    assert "BLOCKED_LEDGER" in st and "SNAPSHOT_UNCONFIRMED" in codes(att["items"])
    # all state sets differ from one another (distinct explanations)
    sets = [frozenset(r[1]) for k, r in results.items() if k not in ("stale", "unconfirmed")]
    assert len(set(sets)) == len(sets), sets
    assert results["stale"][2]["items"] != results["unconfirmed"][2]["items"]


def test_passing_decision_goes_stale_and_is_never_listed_as_live(tmp_path):
    d, _ = _scan(tmp_path, {"AAA": GOOD}, ("AAA",))
    try:
        d.now = NOW + timedelta(minutes=1)
        c = d.api("/api/candidates")["data"]
        assert c["counts"]["passing"] == 1 and len(c["rows"]) == 1 and c["selected_run"]["stale"] is False
        det = d.api("/api/candidates/" + c["rows"][0]["evaluation_id"])["data"]
        assert det["row"]["decision"] == "PASS_RESEARCH_ONLY" and det["row"]["data_tier"] != "VERIFIED"      # Tier R: research only
        d.now = NOW + timedelta(minutes=31)                  # entry_alert_expiry_minutes = 30
        c = d.api("/api/candidates")["data"]
        assert c["counts"]["passing"] == 0 and c["counts"]["stale"] == 1 and c["rows"] == []
        assert c["selected_run"]["stale"] is True
        c2 = d.api("/api/candidates?bucket=stale")["data"]
        assert len(c2["rows"]) == 1 and c2["rows"][0]["bucket"] == "stale"
        # evidence time is unchanged by refresh
        assert c2["rows"][0]["evaluated_at_utc"] == "2026-10-30T15:05:02Z"
    finally:
        d.stop()


# ============================================================================ WD-AT-05: security and zero writes
ROUTES_HTML = VIEWS + ["/positions?group=all", "/positions?group=reserved", "/candidates?bucket=rejected", "/alerts?status=SENT",
                       "/activity?event_type=POSITION_TRANSITION", "/runs?kind=SCAN", "/health"]


def _detail_paths(env):
    p = ["/positions/OPN", "/positions/ASG", "/positions/CLS"]
    ev = env.api("/api/activity")["data"]["rows"][0]["event_id"]
    p.append(f"/activity/{ev}")
    return p


def test_browsing_and_refreshing_every_view_and_api_twin_writes_nothing(env, capsys):
    insert_alerts(env.db, 3)
    before = fingerprint(env.db)
    paths = ROUTES_HTML + _detail_paths(env) + ["/alerts/AL-00001"]
    for _ in range(2):                                       # second pass = "refresh"
        for p in paths:
            st, hd, body = env.req("GET", p)
            assert st == 200, (p, st)
            api = "/api" + (p if p != "/" else "/overview")
            st, hd, body = env.req("GET", api)
            assert st == 200 and hd["content-type"].startswith("application/json"), (api, st)
    assert fingerprint(env.db) == before                    # bytes, WAL, full logical dump, row counts, mtime
    # no -journal / migration artefacts either
    assert not list(env.dir.glob("*.sqlite3-journal"))


def test_dashboard_never_writes_even_when_started_on_a_fresh_or_missing_journal(tmp_path):
    ap = write_approved(tmp_path / "a.json", ())
    missing = tmp_path / "none.sqlite3"
    d = Dash(missing, ap)
    try:
        assert d.login() == 303
        for p in ["/", "/candidates", "/positions", "/alerts", "/activity", "/runs", "/health"]:
            st, _, html = d.req("GET", p)
            assert st == 200 and "journal" in html.lower(), p
        assert not missing.exists() and not list(tmp_path.glob("*.sqlite3*"))     # never creates the journal
        o = d.api("/api/overview")["data"]
        assert "JOURNAL_MISSING" in codes(o["attention"]["items"]) and o["attention"]["ok"] is False
    finally:
        d.stop()


def test_unauthenticated_access_denied_everywhere_including_api(env):
    env.cookie = None
    secret_markers = ["OPN", "AAA", "16000", "WRK"]
    paths = ROUTES_HTML + ["/positions/OPN", "/activity/x", "/alerts/x", "/runs/x", "/candidates/x"]
    for p in paths:
        st, hd, body = env.req("GET", p)
        assert st == 303 and hd["location"] == "/login", (p, st)
        assert not any(m in body for m in secret_markers)
        st, hd, body = env.req("GET", "/api" + (p if p != "/" else "/overview"))
        assert st == 401, p
        assert not any(m in body for m in secret_markers) and "error" in json.loads(body)
    # forged / garbage session cookies
    for bad in ("csp_session=", "csp_session=abc", "csp_session=" + "A" * 43, "other=1"):
        assert env.req("GET", "/api/overview", auth=False, headers={"Cookie": bad})[0] == 401
    # the only unauthenticated resources: login page and stylesheet
    assert env.req("GET", "/login")[0] == 200 and env.req("GET", "/static/app.css")[0] == 200
    # not even directory-ish / traversal / encoded routes give data
    for p in ("/static/../../.env", "/static/..%2f..%2f.env", "/%2e%2e/.env", "/.env", "/config/policy.yaml", "/j.sqlite3",
              "/static/app.css/../../pyproject.toml"):
        st, _, body = env.req("GET", p)
        assert st in (303, 404, 400) and "CSP_" not in body and "[pilot]" not in body, p


def test_authenticated_traversal_sql_and_file_exposure_blocked(env):
    for p in ("/static/../../../.env", "/static/%2e%2e/%2e%2e/.env", "/positions/..%2f..%2f.env", "/positions/OPN%27%20OR%201=1",
              "/positions/%27;DROP%20TABLE%20audit_log;--", "/activity?event_type=x%27%20OR%20%271%27=%271",
              "/activity?before=1%20OR%201=1", "/positions?ticker=AAA%27--", "/positions?group=../../etc/passwd",
              "/candidates?run=%27%20OR%201=1--", "/alerts?status=SENT%27%20OR%201=1", "/runs?kind=SCAN;DROP"):
        st, _, body = env.req("GET", p)
        assert st in (400, 404), (p, st)
        assert "Traceback" not in body and str(env.db) not in body and "CSP_" not in body
    assert env.api("/api/overview")["meta"]["schema_version"] == "0005"    # tables intact (Stage A migration 0005_market_data is the current head)
    # parameter smuggling: a path / db location parameter is ignored, never honoured
    st, _, body = env.req("GET", "/api/overview?db=C:/Windows/win.ini&path=/etc/passwd&policy=x")
    assert st == 200 and "win.ini" not in body and "[fonts]" not in body
    # no route leaks the database location
    for p in ROUTES_HTML:
        body = env.req("GET", "/api" + (p if p != "/" else "/overview"))[2]
        assert str(env.db) not in body and env.db.name not in body and str(env.dir) not in body


def test_host_header_and_origin_boundaries(env):
    port = env.port
    assert env.req("GET", "/api/overview", host=f"localhost:{port}")[0] == 200
    for host in ("evil.example", f"evil.example:{port}", "127.0.0.1", f"127.0.0.1:{port + 1}", f"[::1]:{port}",
                 f"127.0.0.1.evil.com:{port}", "", f"0.0.0.0:{port}"):
        for auth in (True, False):
            st, _, body = env.req("GET", "/api/overview", host=host, auth=auth)
            assert st in (421, 400), (host, auth, st)
            assert "AAA" not in body
    for origin in ("http://evil.example", f"http://127.0.0.1:{port + 1}", f"https://127.0.0.1:{port}", "null",
                   f"http://127.0.0.1:{port}.evil.com", f"http://localhost.evil.com:{port}"):
        assert env.req("GET", "/api/overview", origin=origin)[0] == 403, origin
    assert env.req("GET", "/api/overview", origin=env.origin)[0] == 200
    # Sec-Fetch-Site: cross-site is refused
    assert env.req("GET", "/api/overview", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    # login POST: wrong or missing Origin rejected; the password is not even checked
    body = ("password=" + PW).encode()
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert env.req("POST", "/login", auth=False, body=body, headers=form)[0] == 403                      # missing Origin
    assert env.req("POST", "/login", auth=False, body=body, headers=form, origin="http://evil.example")[0] == 403
    assert env.req("POST", "/logout", body=b"", origin="http://evil.example")[0] == 403
    assert env.req("POST", "/logout", body=b"")[0] == 403                                                # missing Origin
    # DNS rebinding: right path, attacker host, attacker origin
    assert env.req("POST", "/login", auth=False, body=body, headers=form, host=f"evil.example:{port}", origin=f"http://evil.example:{port}")[0] == 421


def test_browser_realistic_login_works_and_bad_origins_still_403(env):
    """Real browsers: with Referrer-Policy no-referrer a same-origin form POST carries `Origin: null` -> 403 (past bug).
    Drive login with Chrome-like headers on both loopback names; hostile/absent Origin must still be refused."""
    port = env.port
    form = {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "User-Agent": "Mozilla/5.0 (test)"}
    body = ("password=" + PW).encode()
    # the policy the server announces on the login page decides what a browser sends; forbid the Origin:null causes
    st, hd, _ = env.req("GET", "/login", auth=False, host=f"127.0.0.1:{port}")
    assert st == 200
    rp = hd["referrer-policy"].strip().lower()
    assert rp in {"same-origin", "origin-when-cross-origin", "strict-origin-when-cross-origin", "origin", "unsafe-url",
                  "no-referrer-when-downgrade"}, rp
    assert rp != "no-referrer" and rp not in {"strict-origin", ""}, rp
    for name in ("127.0.0.1", "localhost"):
        host = f"{name}:{port}"
        st, hd, _ = env.req("POST", "/login", auth=False, host=host, origin=f"http://{host}", body=body, headers=form)
        assert st == 303, (name, st)
        assert hd["location"] in ("/", f"http://{host}/"), hd["location"]
        m = re.search(r"csp_session=([^;]+)", hd.get("set-cookie", ""))
        assert m, name
        assert "referrer-policy" in hd and hd["referrer-policy"].strip().lower() == rp
        st, hd, page = env.req("GET", "/", auth=False, host=host, origin=f"http://{host}",
                               headers={"Cookie": f"csp_session={m.group(1)}", "Sec-Fetch-Site": "same-origin"})
        assert st == 200 and "<html" in page.lower(), (name, st)
    host = f"127.0.0.1:{port}"
    for origin in ("null", "http://evil.example", f"http://evil.example:{port}", None):
        st, _, _ = env.req("POST", "/login", auth=False, host=host, origin=origin, body=body, headers=form)
        assert st == 403, (origin, st)
    # wrong password with a proper Origin is a normal failure, never a 5xx/303-with-cookie
    st, hd, _ = env.req("POST", "/login", auth=False, host=host, origin=f"http://{host}",
                        body=b"password=wrong", headers=form)
    assert st in (200, 401, 403) and "csp_session=" not in hd.get("set-cookie", "")


def test_non_get_verbs_405_except_login_logout_and_no_cors(env):
    for auth in (True, False):
        for verb in ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE", "CONNECT"):
            for p in ("/", "/positions", "/api/overview", "/api/positions", "/positions/OPN", "/login"):
                if verb in ("TRACE", "CONNECT"):
                    continue
                st, hd, _ = env.req(verb, p, auth=auth, origin=env.origin, body=b"x=1" if verb in ("PUT", "PATCH", "DELETE") else None)
                assert st == 405, (verb, p, auth, st)
        for p in ("/", "/positions", "/api/overview", "/api/alerts", "/candidates", "/runs", "/health", "/activity", "/alerts"):
            st, hd, _ = env.req("POST", p, auth=auth, origin=env.origin, body=b"a=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
            # authenticated: strictly 405. Unauthenticated HTML routes may redirect to /login first (no state change either way).
            assert st == 405 or (not auth and st in (303, 401)), ("POST", p, auth, st)
            if auth:
                assert st == 405
            assert not any(k.startswith("access-control-") for k in hd)
    # preflight style request
    st, hd, _ = env.req("OPTIONS", "/api/overview", origin=env.origin,
                        headers={"Access-Control-Request-Method": "GET"})
    assert st == 405 and not any(k.startswith("access-control-") for k in hd)
    # no response anywhere carries CORS headers
    for p in ROUTES_HTML + ["/login", "/api/overview", "/static/app.css"]:
        hd = env.req("GET", p, origin=env.origin)[1]
        assert not any(k.startswith("access-control-") for k in hd), p


def test_brute_force_lockout_and_wrong_password_leaks_nothing(tmp_path):
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    d = Dash(db, write_approved(tmp_path / "a.json", ("AAA",)))
    try:
        for i in range(5):
            assert d.login("wrong-password-%d" % i) == 401
        # sixth attempt is locked out even with the right password
        st = d.login()
        assert st == 429 and d.cookie is None
        st, hd, body = d.req("POST", "/login", auth=False, origin=d.origin, body=("password=" + PW).encode(),
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert st == 429 and "set-cookie" not in hd and "AAA" not in body
        assert d.req("GET", "/api/overview")[0] == 401
    finally:
        d.stop()


def test_login_cookie_attributes_logout_and_bad_login_shapes(tmp_path):
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    d = Dash(db, write_approved(tmp_path / "a.json", ("AAA",)))
    try:
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        for body, ct in ((b"", "application/x-www-form-urlencoded"), (b"password=" + b"x" * 5000, "application/x-www-form-urlencoded"),
                         (("password=" + PW).encode(), "application/json"), (b'{"password":"%s"}' % PW.encode(), "application/json")):
            st, hd, _ = d.req("POST", "/login", auth=False, origin=d.origin, body=body, headers={"Content-Type": ct})
            assert st in (400, 401, 413) and "set-cookie" not in hd, (len(body), ct, st)
        # password in the URL / query is never accepted
        assert d.req("GET", "/login?password=" + PW, auth=False)[0] == 200
        assert d.req("GET", "/api/overview?password=" + PW, auth=False)[0] == 401
        st, hd, _ = d.req("POST", "/login", auth=False, origin=d.origin, body=("password=" + PW).encode(), headers=form)
        assert st == 303 and hd["location"] == "/"
        sc = hd["set-cookie"]
        assert "HttpOnly" in sc and "SameSite=Strict" in sc and "Path=/" in sc and PW not in sc
        tok = re.search(r"csp_session=([^;]+)", sc).group(1)
        assert len(tok) >= 32
        d.cookie = tok
        assert d.req("GET", "/api/overview")[0] == 200
        assert d.req("POST", "/logout", origin=d.origin, body=b"")[0] == 303
        assert d.req("GET", "/api/overview")[0] == 401            # session actually dropped server-side
    finally:
        d.stop()


def test_html_and_script_injection_in_notes_tickers_and_alert_text_is_escaped(tmp_path):
    xss = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "<b>bold</b>&amp;", "'; DROP TABLE x;--", "<svg/onload=alert(1)>"]
    db = tmp_path / "j.sqlite3"
    j = build_book(db, notes={"OPN": xss[0], "WRK": xss[1], "CLW": xss[4]}, tickers={"PND": "<b>ZZ</b>"})
    j.audit_manual("note-test", xss[2], xss[3], {"note": xss[0], "x": xss[1]})
    j.close()
    insert_alerts(db, 2, content=xss[0] + xss[1])
    c = sqlite3.connect(db)
    c.execute("UPDATE alert_outbox SET class=?, severity=? WHERE alert_id='AL-00000'", (xss[4], xss[1]))
    c.execute("INSERT INTO run_status VALUES (?,?,?,?,?,?)", (xss[0], "SCAN", "2026-10-30T15:00:00Z", "NO_OPPORTUNITIES", json.dumps([xss[1]]),
              json.dumps({"coverage": [{"ticker": xss[4], "status": "UNAVAILABLE", "detail": xss[0]}], "tickers": 1})))
    c.commit(); c.close()
    d = Dash(db, write_approved(tmp_path / "a.json", ("AAA",)))
    try:
        assert d.login() == 303
        ev = d.api("/api/activity?event_type=MANUAL")  # may be empty, just must not break
        evs = d.api("/api/activity")["data"]["rows"]
        paths = ROUTES_HTML + ["/positions/OPN", "/positions/WRK", "/positions/CLW", "/positions?group=all", "/alerts/AL-00000",
                               "/alerts/AL-00001", "/runs/" + "%3Cscript%3E"] + [f"/activity/{e['event_id']}" for e in evs[:12]]
        saw_payload = False
        for p in paths:
            st, hd, body = d.req("GET", p)
            if st != 200:
                continue
            for bad in ("<script", "<img", "<svg", "<b>zz", "<b>bold"):
                assert bad.lower() not in body.lower(), (p, bad)
            assert not re.search(r"<[a-z]+[^>]*\sonerror\s*=", body.lower()), p
            if "&lt;script&gt;" in body or "&lt;svg" in body or "&lt;b&gt;" in body:
                saw_payload = True
        assert saw_payload, "the untrusted text was not displayed at all (cannot tell escaping from omission)"
        st, hd, body = d.req("GET", "/api/positions?group=all")
        assert hd["content-type"].startswith("application/json") and hd["x-content-type-options"] == "nosniff"
        assert xss[0] in json.loads(body)["data"]["rows"][0].get("notes", "") or xss[0] in body or "\\u003c" in body
    finally:
        d.stop()


def test_security_headers_no_external_assets_no_javascript(env):
    for p in ROUTES_HTML + ["/login", "/positions/OPN", "/positions/CLS"]:
        st, hd, body = env.req("GET", p, auth=(p != "/login"))
        assert st == 200
        assert hd["cache-control"] == "no-store" and hd["x-content-type-options"] == "nosniff"
        assert hd["x-frame-options"] == "DENY" and hd["referrer-policy"] == "same-origin"      # not no-referrer: that makes browsers send Origin: null on same-origin form POSTs
        csp = hd["content-security-policy"]
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "script-src" not in csp
        assert not re.search(r"unsafe-inline|unsafe-eval|\*", csp)
        assert "text/html" in hd["content-type"]
        low = body.lower()
        assert "<script" not in low and "javascript:" not in low and not re.search(r"\son[a-z]+\s*=", low), p
        assert not re.search(r"""(src|href|action|data)\s*=\s*["']?\s*(https?:)?//""", low), p       # no external assets/links
        assert "http://" not in low.replace("http://127.0.0.1", "") and "https://" not in low, p
        assert not re.search(r"@import|url\(", low)
        assert "<iframe" not in low
        for m in re.finditer(r"<form[^>]*>", low):                                                # only login/logout forms may exist
            tag = m.group(0)
            if 'method="post"' in tag:
                assert re.search(r'action="/(login|logout)"', tag), (p, tag)               # only login/logout may POST
            else:
                assert 'method="get"' in tag and re.search(r'action="/(candidates|positions|alerts|activity|runs)"', tag), (p, tag)
    css = env.req("GET", "/static/app.css", auth=False)
    assert css[0] == 200 and "text/css" in css[1]["content-type"]
    assert not re.search(r"https?://|@import|url\(", css[2])


def test_secrets_never_in_pages_urls_logs_or_json(env, capsys):
    env.req("POST", "/login", auth=False, origin=env.origin, body=b"password=totally-wrong-pass-123",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
    texts = []
    for p in ROUTES_HTML + _detail_paths(env) + ["/login"]:
        texts.append(env.req("GET", p)[2])
        texts.append(env.req("GET", "/api" + (p if p != "/" else "/overview"))[2])
    blob = "\n".join(texts)
    h = env.srv.password_hash
    salt_hex, hash_hex = h.split("$")[4], h.split("$")[5]
    for s in (PW, h, salt_hex, hash_hex, env.cookie, "totally-wrong-pass-123", "scrypt$"):
        assert s not in blob, "secret material present in a response"
    cap = capsys.readouterr()
    logs = cap.out + cap.err
    assert logs.strip(), "expected some access log lines to exist"
    for s in (PW, h, salt_hex, hash_hex, env.cookie, "totally-wrong-pass-123", "csp_session", "password=", "Cookie"):
        assert s not in logs, "secret material in the server log"
    # query strings never logged
    env.req("GET", "/positions?ticker=AAA&before=5")
    log2 = capsys.readouterr()
    assert "ticker=AAA" not in log2.out + log2.err and "before=5" not in log2.out + log2.err
    # settings object masks the hash
    from csp_alerts.settings import load_settings
    ef = env.dir / "t.env"
    ef.write_text(f"CSP_DASHBOARD_PASSWORD_HASH={h}\nCSP_DATABASE_URL=sqlite:///{(env.dir / 'j.sqlite3').as_posix()}\n", encoding="utf-8")
    s = load_settings(ef)
    assert h not in repr(s) and h not in str(s) and h not in repr(s.dashboard_password_hash) and h not in str(s.dashboard_password_hash)
    assert s.dashboard_password_hash.reveal() == h


# ============================================================================ money as exact decimal strings; no JS
def test_json_has_no_floats_and_money_is_exact_decimal_strings(env):
    add_observation(env.db, "AAA", "75.00", EXP35, NOW - timedelta(minutes=10), bid="0.28", ask="0.30")
    insert_alerts(env.db, 2)
    for p in ["/overview", "/candidates", "/positions?group=all", "/positions/OPN", "/alerts", "/activity", "/runs", "/health"]:
        body = env.req("GET", "/api" + p)[2]
        assert no_floats(body) == [], (p, no_floats(body)[:5])
    o = env.api("/api/overview")["data"]
    for k in ("put_collateral_usd", "owned_stock_usd", "aggregate_usd", "assigned_pending_usd"):
        assert isinstance(o["exposure"][k], str) and re.fullmatch(r"-?\d+(\.\d+)?", o["exposure"][k]), k
    p = _pos(env, "OPN")
    assert p["pnl"]["gross_usd"] == "20.00" and p["pnl"]["net_of_fees_usd"] == "19.35"      # exact, not 19.349999..
    assert p["open_credit_per_share"] == "0.50" and p["fees_usd"] == "0.65"


# ============================================================================ WD-AT-06: coherent reads and pagination
def test_read_during_concurrent_writer_is_coherent(env):
    stop = threading.Event()
    errors: list[str] = []

    def writer():
        j = Journal(env.db, clock=lambda: NOW)
        n = 0
        while not stop.is_set():
            j.audit_manual("w", "writer", "r", {"i": n})
            n += 1
        j.close()
    t = threading.Thread(target=writer)
    t.start()
    try:
        seen_revs = []
        for _ in range(25):
            st, _, body = env.req("GET", "/api/activity")
            if st != 200:
                errors.append(f"status {st}")
                continue
            j = json.loads(body)
            rows = j["data"]["rows"]
            rev = j["meta"]["audit_revision"]
            rids = [r["rid"] for r in rows]
            if rids[0] != rev:
                errors.append(f"newest row {rids[0]} != meta audit_revision {rev}")      # rows from a later state than meta
            if rids != list(range(rids[0], rids[0] - len(rids), -1)):
                errors.append("non-contiguous page")
            seen_revs.append(rev)
            o = env.api("/api/overview")
            if D(o["data"]["exposure"]["aggregate_usd"]) != EXPECT["aggregate"]:
                errors.append("overview numbers changed under audit-only writer")
        assert seen_revs == sorted(seen_revs)
        assert seen_revs[-1] > seen_revs[0], "writer made no progress; test inconclusive"
    finally:
        stop.set()
        t.join()
    assert errors == [], errors[:5]


def _walk_older(env, path, key, between=None):
    out, cursor, pages = [], None, 0
    while True:
        q = path + (("&" if "?" in path else "?") + f"before={cursor}" if cursor else "")
        d = env.api(q)["data"]
        out += [r[key] for r in d["rows"]]
        pages += 1
        assert len(d["rows"]) <= 100, "page not bounded"
        if between:
            between(pages)
        if not d["older"]:
            return out, pages
        cursor = d["older"]


def test_audit_pagination_500_plus_rows_loses_and_duplicates_nothing_even_with_inserts(env):
    j = Journal(env.db, clock=lambda: NOW)
    for i in range(520):
        j.audit_manual("bulk", "t", "r", {"i": i})
    j.close()
    c = sqlite3.connect(env.db)
    original = {r[0] for r in c.execute("SELECT event_id FROM audit_log")}
    c.close()
    assert len(original) >= 520

    def inserter(page_no):
        jj = Journal(env.db, clock=lambda: NOW)
        for i in range(7):
            jj.audit_manual("late", "t", "r", {"p": page_no, "i": i})
        jj.close()
    walked, pages = _walk_older(env, "/api/activity", "event_id", between=inserter)
    assert len(walked) == len(set(walked)), "duplicate rows across pages"
    assert original <= set(walked), f"missing {len(original - set(walked))} rows"
    assert set(walked) == original               # rows inserted later have higher ids and never leak into an older-ward walk
    assert pages >= 6                                         # 520+ rows cannot fit in a bounded page
    # filter keeps working with pagination
    bulk, _ = _walk_older(env, "/api/activity?event_type=MANUAL_COMMAND", "event_id")
    c = sqlite3.connect(env.db)
    n_manual = c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='MANUAL_COMMAND'").fetchone()[0]
    c.close()
    assert n_manual >= 520 and len(bulk) == n_manual == len(set(bulk))


def test_forward_pagination_after_cursor_is_contiguous(env):
    j = Journal(env.db, clock=lambda: NOW)
    for i in range(130):
        j.audit_manual("bulk", "t", "r", {"i": i})
    j.close()
    all_rids, cursor = [], None
    # start from the oldest page then move to newer pages
    d = env.api("/api/activity")["data"]
    while d["older"]:
        d = env.api(f"/api/activity?before={d['older']}")["data"]
    all_rids += [r["rid"] for r in d["rows"]]
    while d["newer"]:
        d = env.api(f"/api/activity?after={d['newer']}")["data"]
        all_rids += [r["rid"] for r in d["rows"]]
    all_rids = sorted(set(all_rids))
    assert all_rids == list(range(all_rids[0], all_rids[-1] + 1))


def test_alert_pagination_500_plus_rows_with_inserts_between_pages(env):
    insert_alerts(env.db, 540)
    state = {"next": 540}

    def inserter(page_no):
        insert_alerts(env.db, 3, start=state["next"])
        state["next"] += 3
    walked, pages = _walk_older(env, "/api/alerts", "alert_id", between=inserter)
    original = {f"AL-{i:05d}" for i in range(540)}
    assert len(walked) == len(set(walked)) and original <= set(walked)
    assert pages >= 6
    d = env.api("/api/alerts")["data"]
    assert sum(d["status_counts"].values()) == d["total_rows"] and d["status_counts"]["SENT"] >= 540
    row = d["rows"][0]
    assert row["delivery_state"] == "acknowledged by Telegram API" and row["alert_id"].startswith("AL-")
    st, _, html = env.req("GET", "/alerts")
    assert "does not prove" in html.lower() or "not prove" in html.lower()


def test_alert_delivery_states_are_labelled_distinctly(env):
    labels = {}
    for i, s in enumerate(["PENDING", "RETRY", "SENT", "UNKNOWN_ACK", "FAILED_FINAL", "DROPPED_EXPIRED", "DROPPED_STALE"]):
        insert_alerts(env.db, 1, start=i, status=s)
    for r in env.api("/api/alerts")["data"]["rows"]:
        labels[r["status"]] = r["delivery_state"]
    assert len(set(labels.values())) == 7, labels
    assert labels["PENDING"] == "queued" and labels["RETRY"].startswith("retry") and "unknown" in labels["UNKNOWN_ACK"]
    assert labels["SENT"] != labels["UNKNOWN_ACK"] and "dropped" in labels["DROPPED_STALE"] and "failed" in labels["FAILED_FINAL"]
    assert env.req("GET", "/alerts?status=NOPE")[0] == 400
    # no control to resend / acknowledge / trade
    html = env.req("GET", "/alerts")[2].lower()
    for m in re.finditer(r"<form[^>]*>", html):
        assert 'action="/logout"' in m.group(0), m.group(0)
    for m in re.finditer(r"<form[^>]*>", html):
        tag = m.group(0)
        assert 'action="/logout"' in tag or 'method="get"' in tag, tag               # nothing but logout may POST; filters are GET
    assert "resend" not in re.sub(r"cannot resend[^<]*", "", html.lower())


# ============================================================================ WD-AT-07 / WD-AT-08
def test_health_labels_next_runs_as_policy_derived_and_lists_unverified_items(env):
    h = env.api("/api/health")["data"]
    assert "does not show" in h["next_runs"]["basis"].lower() and "scheduled" in h["next_runs"]["basis"].lower()
    text = " ".join(h["unverified"]).lower()
    assert "windows scheduled tasks" in text and "telegram receipt" in text
    lim = " ".join(h["limits"]).lower()
    assert "powered on" in lim and "cannot announce its own outage" in lim
    html = env.req("GET", "/health")[2].lower()
    assert "estimated" in html and "policy" in html and "not verified" in html or "unverified" in html or "not checked" in html
    assert h["runs"]["last_scan_attempt"] is None and h["runs"]["last_monitor_completed"] is None      # nothing invented


def test_stopping_and_restarting_the_dashboard_leaves_job_state_untouched(env):
    j = Journal(env.db, clock=lambda: NOW)
    j.record_run("scan-1", "SCAN", NOW - timedelta(minutes=30), "NO_OPPORTUNITIES", [], {"tickers": 1})
    j.record_run("mon-1", "MONITOR", NOW - timedelta(minutes=10), "NO_ALERTS", [], {})
    j.close()
    insert_alerts(env.db, 2, status="PENDING")
    before = fingerprint(env.db)
    env.req("GET", "/health")
    env.stop()
    assert fingerprint(env.db) == before                                  # stop touches nothing
    d2 = Dash(env.db, env.approved)
    try:
        d2.login()
        d2.req("GET", "/health")
        h = d2.api("/api/health")["data"]
        assert h["runs"]["last_scan_attempt"]["run_id"] == "scan-1" and h["runs"]["last_monitor_attempt"]["run_id"] == "mon-1"
        c = sqlite3.connect(env.db)
        assert c.execute("SELECT COUNT(*) FROM alert_outbox WHERE status='PENDING'").fetchone()[0] == 2   # nothing sent/changed
        c.close()
        assert fingerprint(env.db) == before
    finally:
        d2.stop()


def test_no_schema_change_and_older_schema_degrades_without_migrating(tmp_path):
    mig = sorted(p.name for p in (REPO / "src" / "csp_alerts" / "journal" / "migrations").glob("0*.sql"))
    assert mig == ["0001_init.sql", "0002_import_quarantine.sql", "0003_entry_evaluations.sql", "0004_run_status.sql",
                   "0005_market_data.sql"]   # Stage A added exactly one additive migration; the dashboard itself still adds none
    db = tmp_path / "old.sqlite3"
    j = build_book(db)
    j.close()
    c = sqlite3.connect(db)
    c.execute("DROP TABLE run_status")
    # Stage A: a journal older than 0005 also has none of the market-data tables or the watchlist
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if t.startswith("md_") or t == "watchlist":
            c.execute(f"DROP TABLE {t}")
    c.execute("DELETE FROM schema_migrations WHERE version IN ('0004','0005')")
    c.commit(); c.close()
    before = fingerprint(db)
    d = Dash(db, write_approved(tmp_path / "a.json", ("AAA",)))
    try:
        d.login()
        o = d.api("/api/overview")
        assert o["meta"]["schema_version"] == "0003"
        assert "SCHEMA_BEHIND" in codes(o["data"]["attention"]["items"])
        for p in VIEWS + ["/watchlist", "/market/AAA"]:       # Stage A pages must also degrade, never 500, never migrate
            assert d.req("GET", p)[0] in (200, 503, 404), p
        assert D(o["data"]["exposure"]["aggregate_usd"]) == EXPECT["aggregate"]
    finally:
        d.stop()
    assert fingerprint(db) == before                           # opened and browsed, not migrated
    c = sqlite3.connect(db)
    assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == "0003"
    assert c.execute("SELECT name FROM sqlite_master WHERE name='run_status'").fetchone() is None
    c.close()


def test_existing_journal_keeps_state_halt_and_outbox_after_dashboard_use(env):
    insert_alerts(env.db, 3, status="UNKNOWN_ACK")
    c = sqlite3.connect(env.db)
    rows_before = c.execute("SELECT alert_id, status, attempts, dedup_key FROM alert_outbox ORDER BY 1").fetchall()
    pos_before = c.execute("SELECT trade_id, state, fees_usd FROM option_positions ORDER BY 1").fetchall()
    c.close()
    for p in VIEWS + ["/positions?group=all"]:
        env.req("GET", p)
    c = sqlite3.connect(env.db)
    assert c.execute("SELECT alert_id, status, attempts, dedup_key FROM alert_outbox ORDER BY 1").fetchall() == rows_before
    assert c.execute("SELECT trade_id, state, fees_usd FROM option_positions ORDER BY 1").fetchall() == pos_before
    c.close()
    assert env.api("/api/overview")["data"]["drawdown"]["latched"] is True      # latch state still reported, not reset


def test_wrong_input_does_not_crash_server_and_errors_have_no_internals(env):
    for p in ("/positions?before=abc", "/positions?before=-1", "/positions?before=99999999999999999999999", "/activity?after=0",
              "/activity?before=" + "9" * 40, "/candidates?run=" + "A" * 200, "/a" * 1500, "/positions?" + "&".join(f"k{i}=1" for i in range(40))):
        st, _, body = env.req("GET", p)
        assert st in (200, 400, 404, 414), (p[:40], st)
        assert "Traceback" not in body and "sqlite3" not in body and str(env.db) not in body
    assert env.req("GET", "/api/overview")[0] == 200            # still alive


def test_scan_backed_views_have_exact_decimal_strings_no_floats_and_tier_r_is_not_called_verified(tmp_path):
    d, _ = _scan(tmp_path, {"AAA": GOOD}, ("AAA",))
    try:
        for p in ("/api/candidates", "/api/runs", "/api/health", "/api/activity", "/api/overview"):
            assert no_floats(d.req("GET", p)[2]) == [], p
        row = d.api("/api/candidates")["data"]["rows"][0]
        # fixture: bid .50 ask .54 delta -.13 spot 80 strike 75 -> collateral 75*100 = 7500 ; premium at bid 0.50*100 = 50
        assert (D(row["bid"]), D(row["ask"]), D(row["delta"]), D(row["strike"]), D(row["spot"])) == (
            D("0.5"), D("0.54"), D("-0.13"), D("75"), D("80"))
        assert D(row["collateral_usd"]) == D("7500") and D(row["premium_at_bid_usd"]) == D("50")
        assert all(isinstance(row[k], str) for k in ("bid", "ask", "delta", "strike", "collateral_usd"))
        html = d.req("GET", "/candidates/" + row["evaluation_id"])[2]
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        assert "INDICATIVE_UNTIMESTAMPED" in text and "verify at your broker" in text and "UNAVAILABLE" in text
        assert "PASS_VERIFIED_DATA" not in text and "Exchange quote time unavailable" in text
        listing = re.sub(r"<[^>]+>", " ", d.req("GET", "/candidates")[2])
        assert "not approval to trade" in listing
    finally:
        d.stop()
