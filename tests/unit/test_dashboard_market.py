"""Stage A web: watchlist page, market page, positions quote selection, CSRF/Origin/duplicate-submit, hostile text. Synthetic data."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from chain_fixtures import raw_row
from dash_fixture import POLICY, Running, build
from factories import NOW, TODAY, policy
from market_fixtures import FakeProvider

from csp_alerts.jobs.market_refresh import run_market_refresh
from csp_alerts.journal import store, watchlist
from csp_alerts.journal.store import Journal
from csp_alerts.market import observations as obs
from csp_alerts.market.models import SessionInfo, SessionStatus

OPEN = SessionInfo(SessionStatus.OPEN, 300)
SAT = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
POL = policy()


def refresh(db, src, now=NOW, session=OPEN, **kw):
    j = Journal(db, clock=lambda: now)
    try:
        return run_market_refresh(journal=j, policy=POL, source=src, now_utc=now, session=session, sleep=lambda s: None, pause_seconds=0, **kw)
    finally:
        j.close()


def track(db, *syms):
    j = Journal(db, clock=lambda: NOW)
    for s in syms:
        watchlist.add_symbol(j, s, actor="owner", source="test")
    j.close()


def dump_all(db):
    c = sqlite3.connect(db)
    out = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        out[t] = sorted(c.execute(f"SELECT * FROM {t}").fetchall())
    c.close()
    return out


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([0-9a-f]{64})"', html)
    assert m, "no csrf field on the page"
    return m.group(1)


def post(c, path, **fields):
    from urllib.parse import urlencode
    return c.req("POST", path, body=urlencode(fields))


@pytest.fixture
def env(tmp_path):
    p = build(tmp_path)
    return p


# ---------------------------------------------------------------- A then B reaches the page; states are distinct and truthful
def test_provider_observation_a_then_b_reaches_the_page_and_json_without_any_entry_candidate(env):
    track(env.db, "AAA", "SPYM")
    src = FakeProvider()
    src.set("AAA", spot=48.0, bid=0.50, ask=0.54)
    src.set("SPYM", spot=30.0)
    refresh(env.db, src)
    with Running(env) as c:
        st, html, hd = c.get("/watchlist")
        assert st == 200 and "Auto-refreshing page · Yahoo indicative data · Quote age unknown" in html
        rows = {r["symbol"]: r for r in c.api("/watchlist")["data"]["rows"]}
        a = rows["AAA"]
        assert a["stock"]["price"] == "48" and a["stock"]["price_type"] == "LAST" and a["stock"]["provider"] == "YAHOO_VIA_STOCKPILE"
        assert a["stock"]["market_time_utc"] is None and a["stock"]["retrieved_at_utc"] == "2026-10-30T15:05:00Z"
        assert "UNTIMESTAMPED" in a["state"]["labels"] and "unknown (provider gives none)" in html
        # B arrives; a plain reload (no restart, no scan) shows it
        src.set("AAA", spot=49.25, bid=0.60, ask=0.66)
        src.fetched_at = NOW + timedelta(minutes=5)
        refresh(env.db, src, now=NOW + timedelta(minutes=5))
        a2 = {r["symbol"]: r for r in c.api("/watchlist")["data"]["rows"]}["AAA"]
        assert a2["stock"]["price"] == "49.25" and a2["stock"]["retrieved_at_utc"] == "2026-10-30T15:10:00Z"
        mk = c.api("/market/AAA")["data"]
        row = [r for r in mk["rows"] if r["strike"] == "40"][0]
        assert (row["bid"], row["ask"], row["occ_symbol"], row["occ_source"], row["right"], row["source"], row["tier"]) == (
            "0.6", "0.66", "AAA261209P00040000", "DERIVED", "P", "REFRESH", "INDICATIVE_UNTIMESTAMPED")
        assert row["quote_time_utc"] is None and row["retrieved_at_utc"] == "2026-10-30T15:10:00Z"
    # no entry scan ran and nothing was queued
    j = Journal(env.db)
    assert j.conn.execute("SELECT COUNT(*) FROM alert_outbox WHERE class='ENTRY_RESEARCH' AND created_at_utc > '2026-10-30T15:00:00Z'").fetchone()[0] == 0


def test_reloading_never_makes_old_evidence_current(env):
    track(env.db, "AAA")
    src = FakeProvider()
    src.set("AAA")
    refresh(env.db, src)
    with Running(env, now=NOW + timedelta(minutes=2)) as c:
        r = c.api("/watchlist")["data"]["rows"][0]
        assert r["stock"]["age_seconds"] == 120 and r["state"]["primary"] != "STALE"
    with Running(env, now=NOW + timedelta(minutes=40)) as c:        # same stored row, 40 minutes later, market still open
        r = c.api("/watchlist")["data"]["rows"][0]
        assert r["stock"]["retrieved_at_utc"] == "2026-10-30T15:05:00Z" and r["stock"]["age_seconds"] == 2400
        assert r["state"]["primary"] == "STALE" and "UNTIMESTAMPED" in r["state"]["labels"]


def test_distinct_states_unavailable_stale_market_closed_untimestamped_delayed(env):
    track(env.db, "AAA", "CCCX")
    src = FakeProvider()
    src.set("AAA")
    src.set("CCCX", spot=20.0, spot_asof_utc="2026-10-30T15:00:00Z")           # provider supplies a market time, 5 min old
    refresh(env.db, src)
    src.fail("AAA", "FETCH_ERROR", "connection reset")
    refresh(env.db, src, now=NOW + timedelta(minutes=10), force=True, only="AAA")   # outage on AAA after a success
    track(env.db, "BBBX")                                                      # tracked, never fetched: UNAVAILABLE
    with Running(env, now=NOW + timedelta(minutes=11)) as c:
        rows = {r["symbol"]: r for r in c.api("/watchlist")["data"]["rows"]}
        assert rows["BBBX"]["state"]["labels"] == ["UNAVAILABLE"] and rows["BBBX"]["stock"] is None
        assert rows["BBBX"]["status"]["status"] == "NEVER_FETCHED"
        assert rows["AAA"]["state"]["primary"] == "STALE" and rows["AAA"]["status"]["status"] == "ERROR"
        assert rows["AAA"]["stock"]["price"] == "48" and rows["AAA"]["stock"]["retrieved_at_utc"] == "2026-10-30T15:05:00Z"   # original
        e = rows["AAA"]["status"]["last_error"]
        assert e.startswith("NETWORK_ERROR") and "connection reset" not in e
        assert "DELAYED" in rows["CCCX"]["state"]["labels"] and "UNTIMESTAMPED" not in rows["CCCX"]["state"]["labels"]
        assert rows["CCCX"]["stock"]["market_time_utc"] == "2026-10-30T15:00:00Z"
    with Running(env, now=SAT) as c:
        rows = {r["symbol"]: r for r in c.api("/watchlist")["data"]["rows"]}
        assert rows["CCCX"]["state"]["labels"][0] == "MARKET-CLOSED"
        html = c.get("/watchlist")[1]
        assert "MARKET-CLOSED" in html and "not a live price" in html


def test_rate_limit_and_backoff_are_shown_distinctly(env):
    track(env.db, "AAA")
    src = FakeProvider()
    src.fail("AAA", "RATE_LIMIT", "Yahoo rate limit hit")
    refresh(env.db, src)
    with Running(env, now=NOW + timedelta(minutes=1)) as c:
        r = c.api("/watchlist")["data"]["rows"][0]
        assert r["status"]["status"] == "RATE_LIMITED" and r["status"]["backoff_until_utc"] == "2026-10-30T15:20:00Z"
        assert r["status"]["next_update_estimate_utc"] == "2026-10-30T15:20:00Z" and "estimate" in c.get("/watchlist")[1]


def test_prices_stay_visible_when_entries_are_blocked_and_no_contract_passes(env):
    track(env.db, "AAA")                                                       # AAA is approved in the fixture; the book has blockers
    src = FakeProvider()
    src.set("AAA", rows=[raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0, bid=0.01, ask=0.9)])
    refresh(env.db, src)
    with Running(env) as c:
        st, html, _ = c.get("/market/AAA")
        assert st == 200 and "48" in html and "Current blockers and next manual steps" in html
        assert "No stored contract passes the contract-level gates" in html and "passes contract gates (research only)" not in html.replace(
            "Gate marker = every CONTRACT-level entry gate passes", "")
        d = c.api("/market/AAA")["data"]
        assert d["passing_in_view"] == 0 and len(d["rows"]) == 2 and all(r["gate"]["marker"] == "BROWSE" for r in d["rows"])
        assert any("BAD_BID_ASK" in r["gate"]["codes"] for r in d["rows"])
        assert [r["bid"] for r in d["rows"]][0] == "0" and "no quote (0)" in html


def test_a_passing_contract_is_marked_research_only_and_distinct_from_browsable(env):
    track(env.db, "AAA")
    src = FakeProvider()
    src.set("AAA", rows=[raw_row(strike=40.0, spot=48.0, bid=0.50, ask=0.54), raw_row(strike=42.0, spot=48.0, bid=0.0, ask=0.0)])
    refresh(env.db, src)
    with Running(env) as c:
        d = c.api("/market/AAA")["data"]
        by = {r["strike"]: r for r in d["rows"]}
        assert by["40"]["gate"]["marker"] == "PASS" and by["42"]["gate"]["marker"] == "BROWSE" and d["passing_in_view"] == 1
        assert "research only" in c.get("/market/AAA")[1]
        # filters: expiry, strike range, only-passing
        assert [r["strike"] for r in c.api("/market/AAA?show=pass")["data"]["rows"]] == ["40"]
        assert [r["strike"] for r in c.api("/market/AAA?strike_min=41&strike_max=45")["data"]["rows"]] == ["42"]
        assert c.api("/market/AAA?expiry=2020-01-01")["data"]["rows"] == []
        assert c.get("/market/AAA?strike_min=abc")[0] == 400 and c.get("/market/AAA?expiry=garbage")[0] == 400


def test_stale_data_is_never_marked_as_passing(env):
    track(env.db, "AAA")
    src = FakeProvider()
    src.set("AAA", rows=[raw_row(strike=40.0, spot=48.0)])
    refresh(env.db, src)
    src.fail("AAA", "FETCH_ERROR", "down")
    refresh(env.db, src, now=NOW + timedelta(minutes=10), force=True)
    with Running(env, now=NOW + timedelta(minutes=11)) as c:
        d = c.api("/market/AAA")["data"]
        assert d["rows"][0]["gate"]["marker"] == "NOT_EVALUATED" and d["passing_in_view"] == 0


def test_unapproved_ticker_shows_data_with_explicit_warning_and_cannot_alert(env):
    track(env.db, "SPYM")
    src = FakeProvider()
    src.set("SPYM", spot=30.0, strikes=(25.0,))
    refresh(env.db, src)
    before = dump_all(env.db)
    with Running(env) as c:
        html = c.get("/market/SPYM")[1]
        assert "Not approved for entry alerts" in html and "can never produce an entry alert" in html and "30" in html
        wl = c.get("/watchlist")[1]
        assert "NOT APPROVED" in wl and "not approved for entry alerts" in wl
        rows = {r["symbol"]: r for r in c.api("/watchlist")["data"]["rows"]}
        assert rows["SPYM"]["approval"]["state"] == "NOT_APPROVED"
    assert dump_all(env.db) == before                                          # browsing produced no evaluation/alert/outbox row


def test_approved_ticker_state_and_expired_approval(env):
    track(env.db, "AAA")
    with Running(env) as c:
        assert {r["symbol"]: r["approval"]["state"] for r in c.api("/watchlist")["data"]["rows"]}["AAA"] == "APPROVED"
    with Running(env, now=NOW + timedelta(days=90)) as c:                      # approved_through 2026-12-31
        assert {r["symbol"]: r["approval"]["state"] for r in c.api("/watchlist")["data"]["rows"]}["AAA"] == "EXPIRED"


def test_holding_stays_listed_after_watchlist_removal_with_reason(env):
    # the fixture has an OPEN put on BBB and shares of EEE: both are tracked with reason "active holding" without any watchlist row
    with Running(env) as c:
        rows = {r["symbol"]: r for r in c.api("/watchlist")["data"]["rows"]}
        assert rows["BBB"]["reason_tracked"] == "active holding" and rows["EEE"]["reason_tracked"] == "active holding"
        assert "held: always tracked" in c.get("/watchlist")[1]
        assert c.get("/market/BBB")[0] == 200


# ---------------------------------------------------------------- browsing never writes
def test_get_pages_and_json_twins_never_write(env):
    track(env.db, "AAA", "SPYM")
    src = FakeProvider()
    src.set("AAA")
    src.set("SPYM")
    refresh(env.db, src)
    before = dump_all(env.db)
    with Running(env) as c:
        for p in ("/watchlist", "/watchlist?msg=added", "/market/AAA", "/market/SPYM?show=pass", "/market/BBB", "/api/watchlist",
                  "/api/market/AAA", "/positions", "/"):
            for _ in range(2):
                st, *_ = c.get(p)
                assert st == 200, p
        for p in ("/watchlist/add", "/watchlist/remove", "/market/refresh-request"):
            assert c.get(p)[0] == 404 or c.get(p)[0] == 405                    # a GET can never reach a write route
    assert dump_all(env.db) == before


def test_auto_refresh_is_a_meta_tag_only_and_json_has_no_csrf_token(env):
    track(env.db, "AAA")
    with Running(env) as c:
        html = c.get("/watchlist")[1]
        assert re.search(r'<meta http-equiv="refresh" content="60;url=/watchlist">', html) and "<script" not in html.lower()
        assert re.search(r'url=/market/AAA', c.get("/market/AAA")[1])
        assert "csrf" not in c.get("/api/watchlist")[1].lower() and "csrf" not in c.get("/api/market/AAA")[1].lower()


# ---------------------------------------------------------------- authenticated, CSRF- and Origin-protected writes
def test_add_remove_flow_prg_duplicate_submit_and_audit(env):
    with Running(env) as c:
        tok = csrf_of(c.get("/watchlist")[1])
        st, body, hd = post(c, "/watchlist/add", csrf=tok, symbol="tqqq")
        assert st == 303 and hd["location"] == "/watchlist?msg=added"
        st, body, hd = post(c, "/watchlist/add", csrf=tok, symbol="TQQQ")        # double click / replayed form
        assert st == 303 and hd["location"] == "/watchlist?msg=already"
        html = c.get("/watchlist?msg=added")[1]
        assert "TQQQ" in html and "Added to the watchlist" in html and "watchlist" in html
        assert [r["symbol"] for r in c.api("/watchlist")["data"]["rows"] if r["reason_tracked"] == "watchlist"] == ["TQQQ"]
        st, _, hd = post(c, "/watchlist/remove", csrf=tok, symbol="TQQQ")
        assert hd["location"] == "/watchlist?msg=removed"
        assert post(c, "/watchlist/remove", csrf=tok, symbol="TQQQ")[2]["location"] == "/watchlist?msg=not_tracked"
    j = Journal(env.db)
    ev = [(e["event_type"], e["actor"], e["source"]) for e in j.audit_events() if e["event_type"].startswith("WATCHLIST")]
    assert ev == [("WATCHLIST_ADDED", "web:owner", "web"), ("WATCHLIST_REMOVED", "web:owner", "web")]


def test_csrf_missing_wrong_or_from_another_session_writes_nothing(env):
    before = dump_all(env.db)
    with Running(env) as c, Running(env) as other:
        tok_other = csrf_of(other.get("/watchlist")[1])
        for fields in ({"symbol": "AAA"}, {"symbol": "AAA", "csrf": ""}, {"symbol": "AAA", "csrf": "0" * 64},
                       {"symbol": "AAA", "csrf": tok_other}):                    # a token minted for a different session
            st, _, hd = post(c, "/watchlist/add", **fields)
            assert st == 303 and hd["location"] == "/watchlist?msg=csrf", fields
    assert dump_all(env.db) == before


def test_origin_host_auth_and_content_type_are_enforced_on_every_write_route(env):
    before = dump_all(env.db)
    with Running(env) as c:
        tok = csrf_of(c.get("/watchlist")[1])
        body = f"csrf={tok}&symbol=AAA&scope=ALL"
        for path in ("/watchlist/add", "/watchlist/remove", "/market/refresh-request"):
            assert c.req("POST", path, body=body, origin="http://evil.example")[0] == 403
            assert c.req("POST", path, body=body, origin="null")[0] == 403
            assert c.req("POST", path, body=body, origin=None)[0] == 403                      # Origin required
            assert c.req("POST", path, body=body, host="evil.example:80")[0] == 421
            assert c.req("POST", path, body=body, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
            assert c.req("POST", path, body=body, headers={"Content-Type": "application/json"})[0] == 400
            assert c.req("POST", path, body="x" * 5000)[0] in (400, 403)                    # oversized
            assert c.req("PUT", path, body=body)[0] == 405 and c.req("DELETE", path)[0] == 405
        # not logged in: bounced to the login page, nothing written
        saved, c.cookie = c.cookie, None
        for path in ("/watchlist/add", "/watchlist/remove", "/market/refresh-request"):
            st, _, hd = c.req("POST", path, body=body)
            assert st == 303 and hd["location"] == "/login"
        c.cookie = saved
        # and after logout the old session token is dead
        c.req("POST", "/logout", body="")
        assert c.req("POST", "/watchlist/add", body=body)[0] == 303
    assert dump_all(env.db) == before


def test_the_post_route_set_is_exactly_login_logout_and_the_three_stage_a_writes():
    from csp_dashboard import server
    assert server.WRITE_PATHS == {"/watchlist/add", "/watchlist/remove", "/market/refresh-request"}


def test_hostile_ticker_text_is_rejected_not_stored_not_reflected(env):
    before = dump_all(env.db)
    hostile = ["<script>alert(1)</script>", "A\x00B", "AAA'; DROP TABLE watchlist;--", "‮SPY", "a" * 500, "%3Cscript%3E", "SPY%0d%0aSet-Cookie:x=1"]
    with Running(env) as c:
        tok = csrf_of(c.get("/watchlist")[1])
        for h in hostile:
            st, _, hd = post(c, "/watchlist/add", csrf=tok, symbol=h)
            assert st == 303 and hd["location"] == "/watchlist?msg=invalid", h
            assert "\n" not in hd["location"] and "script" not in hd["location"].lower()
        st, html, _ = c.get("/watchlist?msg=invalid")
        assert st == 200 and "<script" not in html.lower() and "DROP TABLE" not in html
        assert c.get("/watchlist?msg=%3Cscript%3E")[0] == 400                    # msg is a closed whitelist, never reflected
    assert dump_all(env.db) == before


def test_stored_hostile_error_text_is_escaped_on_both_pages(env):
    track(env.db, "AAA")
    c0 = sqlite3.connect(env.db)
    evil = "<img src=x onerror=alert(1)> ‮ <script>alert(2)</script>"
    c0.execute("INSERT INTO md_symbol_status VALUES ('AAA','ERROR','2026-10-30T15:00:00Z',NULL,?,1,NULL,'OPEN','r','{}')", (evil,))
    c0.execute("INSERT INTO md_stock_latest VALUES ('AAA','48','LAST','YAHOO_VIA_STOCKPILE','INDICATIVE_UNTIMESTAMPED',NULL,'2026-10-30T14:00:00Z','REFRESH','r',NULL)")
    c0.execute("INSERT INTO md_option_latest VALUES ('AAA',?, 'P','40',?, 'PROVIDER','0.5','0.54',NULL,100,1000,'-0.13',NULL,'48',NULL,NULL,"
               "'YAHOO_VIA_STOCKPILE','INDICATIVE_UNTIMESTAMPED','2026-10-30T14:00:00Z','REFRESH','r',NULL,?)",
               ((TODAY + timedelta(days=40)).isoformat(), "<script>x</script>", evil))
    c0.commit()
    c0.close()
    with Running(env) as c:
        for p in ("/watchlist", "/market/AAA"):
            html = c.get(p)[1]
            assert "<script>alert" not in html and "<img src=x" not in html and "‮" not in html, p
            assert "onerror" not in html and "alert(" not in html.replace("&lt;script&gt;x&lt;/script&gt;", ""), p
        assert "older record, text withheld" in c.get("/watchlist")[1] and "older record, text withheld" in c.get("/market/AAA")[1]


def test_refresh_request_flow_writes_only_a_request_and_audit_and_shows_status(env):
    track(env.db, "AAA")
    before = dump_all(env.db)
    with Running(env) as c:
        tok = csrf_of(c.get("/watchlist")[1])
        st, _, hd = post(c, "/market/refresh-request", csrf=tok, scope="ALL")
        assert hd["location"] == "/watchlist?msg=queued"
        assert post(c, "/market/refresh-request", csrf=tok, scope="ALL")[2]["location"] == "/watchlist?msg=pending"
        st, _, hd = post(c, "/market/refresh-request", csrf=tok, scope="AAA", back="market")
        assert hd["location"] == "/market/AAA?msg=pending"
        html = c.get("/watchlist?msg=queued")[1]
        assert "PENDING" in html and "web page itself never contacts the provider" in html
        assert post(c, "/market/refresh-request", csrf=tok, scope="NOPE!")[2]["location"].endswith("msg=invalid")
        assert post(c, "/market/refresh-request", csrf=tok, scope="ZZZ")[2]["location"].endswith("msg=unknown_symbol")
    after = dump_all(env.db)
    changed = {t for t in after if after[t] != before.get(t)}
    assert changed == {"md_refresh_requests", "audit_log"}                       # nothing else moved
    new_events = [r for r in after["audit_log"] if r not in before["audit_log"]]
    assert len(new_events) == 1 and new_events[0][2] == "MARKET_REFRESH_REQUESTED"


def test_journal_not_upgraded_degrades_and_never_migrates(tmp_path, monkeypatch):
    import shutil
    old = tmp_path / "mig"
    old.mkdir()
    for f in store.MIGRATIONS_DIR.iterdir():
        if f.suffix == ".sql" and f.name[:4] <= "0004":
            shutil.copy(f, old / f.name)
    monkeypatch.setattr(store, "MIGRATIONS_DIR", old)
    p = build(tmp_path)
    monkeypatch.undo()
    before = dump_all(p.db)
    with Running(p) as c:
        st, html, _ = c.get("/watchlist")
        assert st == 200 and "csp journal init" in html and "never migrates" in html
        assert c.get("/market/AAA")[0] == 200 or c.get("/market/AAA")[0] == 404
        tok = csrf_of(html) if "csrf" in html else "0" * 64
        st, _, hd = post(c, "/watchlist/add", csrf=tok, symbol="AAA")
        assert st == 303 and hd["location"].endswith("msg=csrf") or hd["location"].endswith("msg=unavailable")
    assert dump_all(p.db) == before
    assert Journal(p.db).schema_version() == "0004"


# ---------------------------------------------------------------- positions: newest applicable observation, exact contract
def _bbb_contract(db):
    return (TODAY + timedelta(days=40), D("40"))


def test_positions_show_the_newer_of_monitor_and_scan_evidence_with_contract_identity(env):
    # older entry-scan observation for the held BBB 40 put
    c0 = sqlite3.connect(env.db)
    q = {"bid": "0.30", "ask": "0.34", "delta": "-0.2", "spot": "48"}
    c0.execute("INSERT INTO market_observations VALUES (?,?,?,?,?,?,?,?)",
               ("obs-scan", f"BBB|{TODAY + timedelta(days=40)}|P|40.00|100", "2026-10-30T14:50:00Z", None, "YAHOO_VIA_STOCKPILE",
                "INDICATIVE_UNTIMESTAMPED", "x", json.dumps(q)))
    c0.commit()
    c0.close()
    with Running(env) as c:
        ev = [p for p in c.api("/positions")["data"]["rows"] if p["trade_id"] == "P-OPEN"][0]["price_check"]["evidence"]
        assert ev["source"] == "last stored entry-scan observation" and ev["bid"] == "0.30" and ev["retrieved_at_utc"] == "2026-10-30T14:50:00Z"
    # the monitor then retrieves a newer one (persisted to the shared store)
    src = FakeProvider()
    src.set("BBB", rows=[raw_row(strike=40.0, spot=44.0, bid=0.10, ask=0.12)])
    src.fetched_at = NOW - timedelta(minutes=1)
    from csp_alerts.jobs.monitor import run_monitor
    j = Journal(env.db, clock=lambda: NOW)
    run_monitor(journal=j, policy=POL, source=src, now_utc=NOW - timedelta(minutes=1), session=OPEN)
    j.close()
    with Running(env) as c:
        rows = {p["trade_id"]: p for p in c.api("/positions")["data"]["rows"]}
        ev = rows["P-OPEN"]["price_check"]["evidence"]
        assert ev["source"] == "monitor observation (market-data store)" and (ev["bid"], ev["ask"]) == ("0.1", "0.12")
        assert ev["contract"] == "BBB" + (TODAY + timedelta(days=40)).strftime("%y%m%d") + "P00040000"
        assert ev["retrieved_at_utc"] == "2026-10-30T15:04:00Z" and rows["P-OPEN"]["price_check"]["status"] == "usable"
        html = c.get("/positions")[1]
        assert "0.1 / 0.12" in html and "monitor observation" in html and "Option bid / ask (observed)" in html
        assert "monitor observation (market-data store)" in c.get("/positions/P-OPEN")[1]
    # reverse: a scan observation retrieved AFTER the monitor wins
    c0 = sqlite3.connect(env.db)
    c0.execute("INSERT INTO market_observations VALUES (?,?,?,?,?,?,?,?)",
               ("obs-scan2", f"BBB|{TODAY + timedelta(days=40)}|P|40.00|100", "2026-10-30T15:04:30Z", None, "YAHOO_VIA_STOCKPILE",
                "INDICATIVE_UNTIMESTAMPED", "y", json.dumps({"bid": "0.20", "ask": "0.22", "delta": "-0.2", "spot": "44"})))
    c0.commit()
    c0.close()
    with Running(env) as c:
        ev = {p["trade_id"]: p for p in c.api("/positions")["data"]["rows"]}["P-OPEN"]["price_check"]["evidence"]
        assert ev["source"] == "last stored entry-scan observation" and ev["ask"] == "0.22"


def test_a_stock_price_is_never_used_as_an_option_ask(env):
    # only a stock price exists for BBB (no option row): the position has NO option quote, P&L stays unavailable
    c0 = sqlite3.connect(env.db)
    c0.execute("INSERT INTO md_stock_latest VALUES ('BBB','0.01','LAST','YAHOO_VIA_STOCKPILE','INDICATIVE_UNTIMESTAMPED',NULL,'2026-10-30T15:04:00Z','MONITOR','r',NULL)")
    c0.commit()
    c0.close()
    with Running(env) as c:
        p = [r for r in c.api("/positions")["data"]["rows"] if r["trade_id"] == "P-OPEN"][0]
        assert p["pnl"] is None and p["price_check"]["status"] == "unavailable" and p["price_check"].get("evidence") is None


def test_quote_for_a_different_contract_is_not_attached_to_the_position(env):
    src = FakeProvider()
    src.set("BBB", rows=[raw_row(strike=41.0, spot=44.0, bid=0.10, ask=0.12)])              # the held strike is 40
    src.fetched_at = NOW - timedelta(minutes=1)
    track(env.db, "BBB")
    refresh(env.db, src, now=NOW - timedelta(minutes=1))
    with Running(env) as c:
        p = [r for r in c.api("/positions")["data"]["rows"] if r["trade_id"] == "P-OPEN"][0]
        assert p["pnl"] is None and p["price_check"].get("evidence") is None


def test_stale_monitor_evidence_is_shown_but_gives_no_pnl(env):
    src = FakeProvider()
    src.set("BBB", rows=[raw_row(strike=40.0, spot=44.0, bid=0.10, ask=0.12)])
    src.fetched_at = NOW - timedelta(hours=3)
    from csp_alerts.jobs.monitor import run_monitor
    j = Journal(env.db, clock=lambda: NOW)
    run_monitor(journal=j, policy=POL, source=src, now_utc=NOW - timedelta(hours=3), session=OPEN)
    j.close()
    with Running(env) as c:
        p = [r for r in c.api("/positions")["data"]["rows"] if r["trade_id"] == "P-OPEN"][0]
        assert p["price_check"]["status"] == "unavailable" and p["pnl"] is None
        assert p["price_check"]["evidence"]["retrieved_at_utc"] == "2026-10-30T12:05:00Z"         # original time, not 'now'
