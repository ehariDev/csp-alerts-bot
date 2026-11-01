"""Stage B1 web: record a manually executed paper trade, check-in, close/cancel, through the real HTTP server as a browser would
(Origin, CSRF, cookie). Synthetic data; loopback only. Security: session, CSRF, Origin/Host, signed preview token, idempotency,
stale preview, zero writes on GET/preview, hostile text, no raw errors."""
from __future__ import annotations

import hashlib
import html as htmllib
import re
import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import pytest
from dash_fixture import HASH, Client, approved_file, build
from dash_fixture import Running as _Running
from factories import NOW, TODAY, policy
from market_fixtures import FakeProvider

from csp_alerts import runner
from csp_alerts.jobs.market_refresh import run_market_refresh
from csp_alerts.journal import watchlist, webtrade as wt
from csp_alerts.journal.store import Journal
from csp_alerts.market.models import SessionInfo, SessionStatus

POL = policy()
OPEN = SessionInfo(SessionStatus.OPEN, 300)
EXP = (TODAY + timedelta(days=40)).isoformat()
HIDDEN = re.compile(r'<input type="hidden" name="([a-z_0-9]+)" value="([^"]*)">')


@pytest.fixture
def env(tmp_path):
    paths = build(tmp_path, scan=False, evil_notes=False)
    j = Journal(paths.db, clock=lambda: NOW)
    for s in ("AAA", "ZZZ"):
        watchlist.add_symbol(j, s, actor="t", source="t")
    src = FakeProvider()
    src.set("AAA", spot=48.0, strikes=(40.0, 42.0))
    src.set("ZZZ", spot=48.0, strikes=(40.0,))
    run_market_refresh(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN, sleep=lambda s: None, pause_seconds=0)
    j.close()
    return paths


class Web:
    """A running dashboard plus a logged-in browser-like client."""

    def __init__(self, env, now=NOW):
        self.env, self.run = env, _Running(env, now)
        self.c = self.run.__enter__()
        self.srv = self.run.srv

    def close(self):
        self.run.__exit__()

    def csrf(self, path="/checkin"):
        st, body, _ = self.c.get(path)
        assert st == 200, (path, st, body[:300])
        return re.search(r'name="csrf" value="([0-9a-f]{64})"', body).group(1)

    def post(self, path, fields, **kw):
        return self.c.req("POST", path, body=urlencode(fields), **kw)

    def other_client(self):
        c2 = Client(self.srv)
        assert c2.login() == 303
        return c2


@pytest.fixture
def web(env):
    w = Web(env)
    yield w
    w.close()


def hidden(html: str) -> dict[str, str]:
    return {k: htmllib.unescape(v) for k, v in HIDDEN.findall(html)}


def sell_fields(**over):
    f = {"action": "sell_filled", "symbol": "AAA", "expiry": EXP, "strike": "40", "contracts": "1", "credit": "0.45", "fees": "0.65",
         "executed_date": "2026-10-30", "executed_time": "09:30"}
    f.update(over)
    return f


def preview(web, fields, path="/trade/preview", csrf=None):
    return web.post(path, {"csrf": csrf or web.csrf(), **fields})


def confirm_fields(html, csrf, reason="paper fill reported", **extra):
    h = hidden(html)
    h.pop("csrf", None)
    return {"csrf": csrf, **h, "reason": reason, **extra}


def location(hd):
    return hd["location"]


def counts(env):
    c = sqlite3.connect(env.db)
    try:
        return tuple(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("option_positions", "audit_log", "account_snapshots"))
    finally:
        c.close()


def db_digest(env):
    c = sqlite3.connect(env.db)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return hashlib.sha256(Path(env.db).read_bytes()).hexdigest()


def do_sell(web, **over):
    """preview -> confirm; returns (confirm status, result location, preview html)."""
    csrf = web.csrf()
    st, html, _ = preview(web, sell_fields(**over), csrf=csrf)
    assert st == 200 and "Confirm and record" in html, html[:500]
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf))
    return st, location(hd), html


def trade_id_of(loc):
    return parse_qs(urlsplit(loc).query)["trade"][0]


# ---------------------------------------------------------------- GET pages and links (zero writes)
def test_put_rows_offer_record_links_with_the_exact_contract_and_get_pages_write_nothing(env, web):
    d0 = db_digest(env)
    st, page, _ = web.c.get("/market/AAA")
    assert st == 200 and "Record sold put" in page and "I placed an order, not filled yet" in page
    assert f"/trade/sell?symbol=AAA&amp;expiry={EXP}&amp;strike=40&amp;kind=filled" in page
    for path in ("/trade/sell?symbol=AAA&expiry=%s&strike=40&kind=filled" % EXP, "/trade/sell?symbol=AAA&expiry=%s&strike=42&kind=working" % EXP,
                 "/checkin", "/trade/result?code=stale", "/trade/mark-filled/P-PEND", "/trade/close-fill/P-OPEN", "/trade/expire/P-OPEN",
                 "/positions/P-OPEN", "/positions"):
        st, body, _ = web.c.get(path)
        assert st == 200, (path, st)
    assert db_digest(env) == d0                                                    # GETs never mutate


def test_form_shows_the_fixed_contract_reference_bid_and_no_default_fill(env, web):
    st, form, _ = web.c.get(f"/trade/sell?symbol=AAA&expiry={EXP}&strike=40&kind=filled")
    assert st == 200
    for needle in ("AAA", "AAA261209P00040000", "Right", "P (put)", "100 / 1", "paper_primary (paper)", "Reference only (NOT your fill)",
                   "ACTUAL credit per share received", "never the quoted bid", "Preview (writes nothing)"):
        assert needle in form, needle
    assert re.search(r'name="credit" value=""', form) and re.search(r'name="executed_time" value=""', form)    # never defaulted
    assert 'name="contracts" value="1"' in form and 'type="hidden" name="strike" value="40"' in form


def test_unknown_contract_gives_a_clear_message_not_a_form(env, web):
    st, page, _ = web.c.get(f"/trade/sell?symbol=AAA&expiry={EXP}&strike=41&kind=filled")
    assert st == 200 and "not in the stored chain" in page and "Preview (writes nothing)" not in page


# ---------------------------------------------------------------- the full loop through HTTP
def test_full_loop_preview_confirm_open_positions_page_close_confirmed(env, web):
    before = counts(env)
    csrf = web.csrf()
    st, html, _ = preview(web, sell_fields(), csrf=csrf)
    assert st == 200 and counts(env) == before                                     # PREVIEW WRITES NOTHING
    for needle in ("PREVIEW", "(new position) -&gt; OPEN", "WEB-AAA-", "$4,000.00", "Pilot put collateral", "$11,000.00", "$4,000.00",
                   "Reason (required", "Confirm and record", "reconcile"):
        assert needle in html, needle
    assert "BREACH" not in html or "within" in html
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf))
    assert st == 303 and "code=recorded" in location(hd)
    tid = trade_id_of(location(hd))
    st, res, _ = web.c.get(location(hd))
    assert st == 200 and "RECORDED" in res and f"/positions/{tid}" in res and "Reconcile now" in res and "is now OPEN" in res
    assert counts(env)[0] == before[0] + 1
    # the position is OPEN, monitored states include it, and the positions page shows its facts
    st, pos, _ = web.c.get("/positions?group=active")
    assert tid in pos and "OPEN" in pos and "$4,000.00" in pos
    st, det, _ = web.c.get(f"/positions/{tid}")
    assert "Record what happened" in det and "Record the confirmed closing fill" in det and "web:owner" in det
    # entries are blocked until a new snapshot (existing LEDGER_UNRECONCILED behaviour), and the page offers the web check-in
    st, ov, _ = web.c.get("/")
    assert "CHANGE_AFTER_SNAPSHOT" in ov and "/checkin" in ov
    # check-in through the web clears it
    csrf = web.csrf("/checkin")
    ci = {"action": "checkin", "nlots": "1", "lot_0": "L-1", "mark_0": "", "equity": "100000", "free_cash": "50000"}
    st, html, _ = preview(web, ci, path="/checkin/preview", csrf=csrf)
    assert st == 200 and "After this snapshot" in html
    st, _, hd = web.post("/checkin/confirm", confirm_fields(html, csrf, reason="eod check", ack_reconciled="1"))
    assert "code=recorded" in location(hd)
    # close order placed, then the confirmed fill
    csrf = web.csrf(f"/trade/close-order/{tid}")
    st, html, _ = preview(web, {"action": "close_order", "trade_id": tid}, csrf=csrf)
    assert "OPEN -&gt; CLOSE_WORKING" in html
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason="placed GTC buy to close"))
    assert "code=recorded" in location(hd)
    csrf = web.csrf(f"/trade/close-fill/{tid}")
    st, html, _ = preview(web, {"action": "close_filled", "trade_id": tid, "debit": "0.10", "fees": "0.65", "executed_date": "2026-10-30",
                                "executed_time": "10:00"}, csrf=csrf)
    assert "CLOSE_WORKING -&gt; CLOSED" in html and "Realized P&amp;L" in html
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason="closed at broker"))
    assert "code=recorded" in location(hd)
    st, det, _ = web.c.get(f"/positions/{tid}")
    assert "CLOSED" in det and "no open-position advice" in det.lower()
    j = Journal(env.db)
    p = j.get_position(tid)
    j.close()
    assert p.actual_open_credit_per_share == D("0.45") and p.actual_close_debit_per_share == D("0.10") and p.fees_usd == D("1.30")


def test_closing_in_the_web_makes_queued_open_position_advice_stale(env, web):
    st, loc, _ = do_sell(web)
    tid = trade_id_of(loc)
    j = Journal(env.db, clock=lambda: NOW)
    pos = j.get_position(tid)
    rv = {"trade_id": tid, "trigger": "TIME_EXIT", "state": "OPEN", "fingerprint": runner.position_fingerprint(pos), "evidence_at_utc": "2026-10-30T15:05:00Z"}
    assert runner._position_reason(j, POL, rv, "TIME_EXIT", NOW) is None          # a valid OPEN control still alerts
    j.close()
    csrf = web.csrf()
    f = {"action": "close_filled", "trade_id": tid, "debit": "0.2", "fees": "0", "executed_date": "2026-10-30", "executed_time": "10:00"}
    st, html, _ = preview(web, f, csrf=csrf)
    web.post("/trade/confirm", confirm_fields(html, csrf))
    j = Journal(env.db, clock=lambda: NOW)
    assert "advice no longer applies" in runner._position_reason(j, POL, rv, "TIME_EXIT", NOW)
    j.close()


def test_working_order_then_mark_filled_and_cancel_through_the_web(env, web):
    csrf = web.csrf()
    st, html, _ = preview(web, {"action": "sell_working", "symbol": "AAA", "expiry": EXP, "strike": "42", "contracts": "1"}, csrf=csrf)
    assert "WORKING" in html and "has NOT filled" in html
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason="placed limit order"))
    tid = trade_id_of(location(hd))
    st, det, _ = web.c.get(f"/positions/{tid}")
    assert "WORKING" in det and "Mark filled" in det and "Order was canceled" in det and "$4,200.00" in det
    csrf = web.csrf(f"/trade/mark-filled/{tid}")
    st, html, _ = preview(web, {"action": "mark_filled", "trade_id": tid, "credit": "0.52", "fees": "0.70", "executed_date": "2026-10-30",
                                "executed_time": "09:45"}, csrf=csrf)
    assert "WORKING -&gt; OPEN" in html
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason="filled"))
    assert "code=recorded" in location(hd)
    # a second order, canceled instead
    csrf = web.csrf()
    st, html, _ = preview(web, {"action": "sell_working", "symbol": "ZZZ", "expiry": EXP, "strike": "40", "contracts": "1", "sector": "S", "bucket": "B"}, csrf=csrf)
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason="order", ack_breach="1"))
    t2 = trade_id_of(location(hd))
    csrf = web.csrf()
    st, html, _ = preview(web, {"action": "cancel_open_order", "trade_id": t2}, csrf=csrf)
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason="canceled at broker"))
    assert "code=ack_required" in location(hd)                                      # verification box is required
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason="canceled at broker", ack_verified="1"))
    assert "code=recorded" in location(hd)


# ---------------------------------------------------------------- validation messages
def test_quantity_not_one_is_rejected_with_the_cli_message_and_nothing_is_written(env, web):
    before = counts(env)
    st, html, _ = preview(web, sell_fields(contracts="2"))
    assert st == 200 and "exactly 1 contract" in html and "documented CLI procedure" in html and "Confirm and record" not in html
    assert counts(env) == before


def test_bad_numbers_and_future_time_show_fixed_phrases_and_keep_the_form(env, web):
    for over, needle in (({"credit": "abc"}, "plain number"), ({"credit": ""}, "plain number"), ({"executed_date": "2999-01-01"}, "future"),
                         ({"executed_time": ""}, "never defaulted")):
        st, html, _ = preview(web, sell_fields(**over))
        assert st == 200 and needle in html and "Preview (writes nothing)" in html, (over, html[:200])


# ---------------------------------------------------------------- security
def test_confirm_without_valid_csrf_session_origin_or_token_writes_nothing(env, web):
    csrf = web.csrf()
    st, html, _ = preview(web, sell_fields(), csrf=csrf)
    good = confirm_fields(html, csrf)
    before = counts(env)
    # CSRF missing / wrong: redirected, nothing written
    for bad in ({**good, "csrf": ""}, {**good, "csrf": "0" * 64}):
        st, _, hd = web.post("/trade/confirm", bad)
        assert st == 303 and "code=csrf" in location(hd)
    # missing Origin / foreign Origin / bad Host: refused before routing
    assert web.c.req("POST", "/trade/confirm", body=urlencode(good), origin=None)[0] == 403
    assert web.c.req("POST", "/trade/confirm", body=urlencode(good), origin="http://evil.example")[0] == 403
    assert web.c.req("POST", "/trade/confirm", body=urlencode(good), host="evil.example:80")[0] == 421
    # cross-site fetch metadata
    assert web.c.req("POST", "/trade/confirm", body=urlencode(good), headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    # no session
    anon = Client(web.srv)
    st, _, _ = anon.req("POST", "/trade/confirm", body=urlencode(good))
    assert st in (303, 401) and counts(env) == before
    # tampered field: the signed token no longer matches
    st, _, hd = web.post("/trade/confirm", {**good, "credit": "9.99"})
    assert "code=token" in location(hd)
    st, _, hd = web.post("/trade/confirm", {**good, "extra_field": "x"})
    assert "code=token" in location(hd)
    st, _, hd = web.post("/trade/confirm", {**good, "tok": "1." + "0" * 64})
    assert "code=token" in location(hd)
    # token issued in another session is refused
    c2 = web.other_client()
    st, body, _ = c2.get("/checkin")
    csrf2 = re.search(r'name="csrf" value="([0-9a-f]{64})"', body).group(1)
    st, _, hd = c2.req("POST", "/trade/confirm", body=urlencode({**good, "csrf": csrf2}))
    assert "code=token" in location(hd)
    # wrong content type / oversized body
    assert web.c.req("POST", "/trade/confirm", body=urlencode(good), headers={"Content-Type": "application/json"})[0] == 400
    assert web.c.req("POST", "/trade/confirm", body="a=" + "x" * 7000)[0] == 400
    assert counts(env) == before


def test_preview_post_also_needs_csrf_and_origin_and_writes_nothing(env, web):
    before = counts(env)
    st, _, hd = web.post("/trade/preview", {"csrf": "", **sell_fields()})
    assert st == 303 and "code=csrf" in location(hd)
    assert web.c.req("POST", "/trade/preview", body=urlencode({"csrf": web.csrf(), **sell_fields()}), origin="http://evil.example")[0] == 403
    assert counts(env) == before


def test_expired_preview_token_is_rejected(env):
    w = Web(env)
    try:
        now = [1000.0]
        w.srv.sessions._clock = lambda: now[0]
        c = w.other_client()
        st, body, _ = c.get("/checkin")
        csrf = re.search(r'name="csrf" value="([0-9a-f]{64})"', body).group(1)
        st, html, _ = c.req("POST", "/trade/preview", body=urlencode({"csrf": csrf, **sell_fields()}))
        good = confirm_fields(html, csrf)
        now[0] += wt.PREVIEW_TTL_SECONDS + 5
        before = counts(env)
        st, _, hd = c.req("POST", "/trade/confirm", body=urlencode(good))
        assert "code=expired" in location(hd) and counts(env) == before
    finally:
        w.close()


def test_duplicate_confirm_double_click_and_reload_create_one_trade(env, web):
    csrf = web.csrf()
    st, html, _ = preview(web, sell_fields(), csrf=csrf)
    body = confirm_fields(html, csrf)
    st1, _, h1 = web.post("/trade/confirm", body)
    n = counts(env)
    st2, _, h2 = web.post("/trade/confirm", body)                                  # reload / double click
    assert "code=recorded" in location(h1) and "code=duplicate" in location(h2)
    assert counts(env) == n
    assert trade_id_of(location(h1)) == trade_id_of(location(h2))
    st, res, _ = web.c.get(location(h2))
    assert "already recorded" in res


def test_stale_preview_after_another_change_is_rejected_with_the_plain_message(env, web):
    csrf = web.csrf()
    st, html_a, _ = preview(web, sell_fields(), csrf=csrf)
    a = confirm_fields(html_a, csrf)
    st, html_b, _ = preview(web, sell_fields(strike="42"), csrf=csrf)
    st, _, hd = web.post("/trade/confirm", confirm_fields(html_b, csrf, ack_breach="1"))   # B is recorded first
    assert "code=recorded" in location(hd)
    before = counts(env)
    st, _, hd = web.post("/trade/confirm", a)
    assert "code=stale" in location(hd) and counts(env) == before
    st, res, _ = web.c.get(location(hd))
    assert "ledger changed, preview again" in res


def test_policy_breach_requires_acknowledgement_is_flagged_and_visible(env, web):
    st, loc, _ = do_sell(web)                                                          # first AAA put is fine
    csrf = web.csrf()
    st, html, _ = preview(web, sell_fields(strike="42"), csrf=csrf)
    assert "BREACHES policy" in html and "PILOT_COLLATERAL_LIMIT" in html and "BREACH" in html and "acknowledge" in html.lower()
    before = counts(env)
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf))                 # no acknowledgement
    assert "code=ack_required" in location(hd) and counts(env) == before
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, ack_breach="1"))
    assert "code=recorded" in location(hd)
    tid = trade_id_of(location(hd))
    st, res, _ = web.c.get(location(hd))
    assert "Flagged as a policy breach" in res
    st, det, _ = web.c.get(f"/positions/{tid}")
    assert "POLICY_BREACH_RECORDED" in det and "Flagged" in det
    st, ov, _ = web.c.get("/")
    assert "POLICY_BREACH_UNRESOLVED" in ov                                            # entries blocked, visible


def test_unsupported_states_show_the_cli_message_and_keep_the_obligation(env, web):
    j = Journal(env.db, clock=lambda: NOW)
    from csp_alerts.journal.models import PositionState as S
    j.transition("P-OPEN", S.ASSIGNED_PENDING_RECONCILIATION)
    j.close()
    st, page, _ = web.c.get("/trade/close-order/P-OPEN")
    assert st == 200 and "documented CLI procedure" in page and "Preview (writes nothing)" not in page
    st, det, _ = web.c.get("/positions/P-OPEN")
    assert "Use the documented CLI procedure" in det
    st, ov, _ = web.c.get("/")
    assert "ASSIGNMENT_PENDING" in ov


def test_hostile_text_is_escaped_limited_and_never_reflected_raw(env, web):
    evil = "<script>alert(1)</script>\x1b[31m‮"
    csrf = web.csrf()
    st, html, _ = preview(web, sell_fields(symbol="ZZZ", sector=evil, bucket="B"), csrf=csrf)
    assert st == 200 and "<script>" not in html and "\x1b" not in html and "‮" not in html
    st, html, _ = preview(web, sell_fields(symbol="ZZZ", sector="Tech", bucket="B"), csrf=csrf)
    st, _, hd = web.post("/trade/confirm", confirm_fields(html, csrf, reason=evil + "A" * 2000, ack_breach="1"))
    assert "code=recorded" in location(hd)
    tid = trade_id_of(location(hd))
    for path in (location(hd), f"/positions/{tid}", "/activity"):
        st, page, _ = web.c.get(path)
        assert "<script>" not in page and "\x1b" not in page and "‮" not in page, path
    # an unknown action / garbage never reflects input or raises
    st, html, _ = preview(web, {"action": "<img src=x>"}, csrf=csrf)
    assert st == 400 or "<img" not in html


def test_checkin_validation_matches_the_cli_and_requires_acknowledgement(env, web):
    csrf = web.csrf("/checkin")
    ci = {"action": "checkin", "nlots": "1", "lot_0": "L-1", "mark_0": "", "equity": "100000", "free_cash": "99000"}
    st, html, _ = preview(web, ci, path="/checkin/preview", csrf=csrf)
    assert "CASH_INCONSISTENT" in html and "Confirm and record" not in html
    ci["free_cash"] = "50000"
    st, html, _ = preview(web, ci, path="/checkin/preview", csrf=csrf)
    before = counts(env)
    st, _, hd = web.post("/checkin/confirm", confirm_fields(html, csrf, reason="x"))
    assert "code=ack_required" in location(hd) and counts(env) == before
    st, _, hd = web.post("/checkin/confirm", confirm_fields(html, csrf, reason="x", ack_reconciled="1"))
    assert "code=recorded" in location(hd) and counts(env)[2] == before[2] + 1
    st, page, _ = web.c.get("/checkin")
    assert "stay CLI-only" in page and "restricted controls" in page


def test_the_write_routes_are_exactly_the_documented_set(env):
    from csp_dashboard import server
    assert server.WRITE_PATHS == frozenset({"/watchlist/add", "/watchlist/remove", "/market/refresh-request"})
    assert server.TRADE_WRITE_PATHS == frozenset({"/trade/preview", "/trade/confirm", "/checkin/preview", "/checkin/confirm"})
    assert server.LABEL_WRITE_PATHS == frozenset({"/portfolio/labels/preview", "/portfolio/labels/confirm"})      # Stage B2
    assert server.ALL_WRITE_PATHS == server.WRITE_PATHS | server.TRADE_WRITE_PATHS | server.LABEL_WRITE_PATHS


def test_other_verbs_and_the_json_mirror_cannot_reach_the_new_writes(env, web):
    csrf = web.csrf()
    for verb in ("PUT", "DELETE", "PATCH"):
        assert web.c.req(verb, "/trade/confirm", body="a=1")[0] == 405
    assert web.c.req("POST", "/api/trade/confirm", body=urlencode({"csrf": csrf}))[0] == 405
    assert web.c.req("GET", "/trade/confirm")[0] in (404, 405)
    assert web.c.req("GET", "/trade/preview")[0] in (404, 405)
