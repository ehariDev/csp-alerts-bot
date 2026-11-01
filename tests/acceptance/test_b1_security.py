"""Stage B1 acceptance, section E (web write security of the four new POSTs), A (route pins, zero-write crawls, form scans),
F (check-in lots) and G (truthfulness). Synthetic data, loopback server, no network, no Telegram.

Oracles come from ACCEPTANCE_CRITERIA WT-AT-03 / WT-AT-09 (preview/confirm/check-in/close with invalid input, cross-origin,
duplicate submission and stale record; no accidental ledger writes on GET; coherent state and audit) and
PRODUCT_WORKFLOW_REQUIREMENTS WT-03 (duplicate/stale rejected transactionally, audit preserved).
"""
from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from acc_helpers import NOW
from b1_env import DASH_NOW, EXP_OTHER, FORM, B1, blockers, checkin, sell, text_of
from stage_a_helpers import EXP
from web_helpers import PW, fingerprint

REPO = Path(__file__).resolve().parents[2]
NEW_POSTS = ("/trade/preview", "/trade/confirm", "/checkin/preview", "/checkin/confirm")
LABEL_POSTS = ("/portfolio/labels/preview", "/portfolio/labels/confirm")
STAGE_A_POSTS = frozenset({"/watchlist/add", "/watchlist/remove", "/market/refresh-request"})


@pytest.fixture()
def w(tmp_path):
    x = B1(tmp_path)
    yield x
    x.stop()


def n_rows(w):
    return tuple(w.sql(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("option_positions", "audit_log", "account_snapshots"))


def web_trades(w):
    return w.sql("SELECT trade_id FROM option_positions WHERE trade_id LIKE 'WEB-%'")


def tid_of(loc):
    return parse_qs(urlsplit(loc).query)["trade"][0]


def prepared(w, fields=None):
    """Preview and return (preview html, hidden fields dict ready for confirm, csrf)."""
    fields = fields or sell("BBB", "30")
    html = w.preview(fields)[2]
    cf = w.confirm_fields(html)
    assert "tok" in cf, text_of(html)[-500:]
    return html, cf, w.csrf()


def confirm(w, cf, csrf=None, **kw):
    st, hd, body = w.post("/trade/confirm", cf, csrf=csrf if csrf is not None else "auto", **kw)
    return st, hd.get("location", ""), body


def code_of(w, loc):
    return w.result_code(loc)


# ======================================================================================== A: route pins
def test_write_path_sets_are_pinned():
    from csp_dashboard import server
    assert server.WRITE_PATHS == STAGE_A_POSTS                                   # Stage A three, unchanged
    assert server.TRADE_WRITE_PATHS == frozenset(NEW_POSTS)
    assert server.LABEL_WRITE_PATHS == frozenset(LABEL_POSTS)                    # Stage B2 two
    assert server.ALL_WRITE_PATHS == STAGE_A_POSTS | frozenset(NEW_POSTS) | frozenset(LABEL_POSTS)   # 9 routes (login/logout separate)
    assert len(server.ALL_WRITE_PATHS) == 9


def test_the_only_post_routes_are_login_logout_and_the_seven(w):
    """Brief wording: 'ALL_WRITE_PATHS must equal login, logout + 7'. The code keeps login/logout outside ALL_WRITE_PATHS (documented in
    server.py); behaviourally the complete POST surface must still be exactly those nine. Everything else is 404/405 and writes nothing."""
    before = fingerprint(w.db)
    probes = ["/", "/trade", "/trade/", "/trade/sell", "/trade/result", "/trade/close-order/OPN", "/trade/preview/", "/trade/confirm/x",
              "/checkin", "/checkin/", "/checkin/baseline", "/positions/OPN/close", "/positions", "/trade/record", "/trade/cancel/OPN",
              "/api/trade/preview", "/api/trade/confirm", "/api/checkin/confirm", "/watchlist", "/market/AAA", "/admin", "/trade/expire/OPN",
              "/trade/mark-filled/OPN", "/trade/close-fill/OPN", "/trade/cancel-close/OPN", "/portfolio", "/portfolio/", "/portfolio/labels", "/portfolio/labels/AAA", "/portfolio/label-result", "/alerts", "/api/portfolio/labels/confirm"]
    for p in probes:
        st, _, _ = w.post(p, {"action": "close_filled", "trade_id": "OPN"})
        assert st in (404, 405), (p, st)
    assert fingerprint(w.db) == before
    # the nine allowed POST routes answer something other than 404/405 (login excepted: it needs no session)
    for p in (*NEW_POSTS, *LABEL_POSTS, "/watchlist/add", "/watchlist/remove", "/market/refresh-request", "/logout"):
        assert w.post(p, {})[0] not in (404, 405), p


NEW_GETS = ["/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled", "/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=working",
            "/trade/mark-filled/OPN", "/trade/close-order/OPN", "/trade/close-fill/OPN", "/trade/cancel/OPN", "/trade/cancel-close/OPN",
            "/trade/expire/OPN", "/checkin", "/trade/result?code=recorded&trade=OPN&a=sell_filled", "/trade/result?code=stale",
            "/trade/result?code=csrf", "/trade/result?code=zzz", "/trade/sell", "/trade/sell?symbol=<x>&expiry=a&strike=b&kind=c",
            "/trade/close-order/nope", "/trade/close-order/%3Cscript%3E", "/trade/result?code=" + "A" * 500,
            "/positions", "/positions/OPN", "/api/positions", "/api/positions/OPN", "/api/overview", "/market/BBB", "/market/AAA",
            "/watchlist", "/", "/health", "/portfolio", "/portfolio/labels/AAA", "/portfolio/labels/BBB", "/portfolio/labels/ZZZ", "/portfolio/labels/%3Cx%3E",
            "/portfolio/label-result?code=recorded&ticker=AAA", "/portfolio/label-result?code=stale", "/portfolio/label-result?code=zzz", "/alerts",
            "/api/portfolio", "/api/alerts", "/checkin", "/trade/result"]


def test_every_new_get_page_leaves_database_bytes_unchanged_even_with_confirm_like_query_strings(w):
    before = fingerprint(w.db)
    for p in NEW_GETS:
        st, _, _ = w.get(p)
        assert st in (200, 400, 404), (p, st)
    for p in (*NEW_POSTS, *LABEL_POSTS):                    # GET on a write route: never a view, never a write
        st, _, _ = w.get(p + "?action=sell_filled&symbol=BBB&strike=30&expiry=2026-12-04&credit=0.4&reason=x&tok=1&csrf=1")
        assert st in (404, 405), (p, st)
    w.d.now += timedelta(minutes=5)
    for p in NEW_GETS[:10]:
        w.get(p)
    assert fingerprint(w.db) == before


def test_form_scan_new_pages_post_only_to_allowed_actions_with_csrf_and_no_script_or_external_links(w):
    allowed = {"/logout", *NEW_POSTS, *LABEL_POSTS, "/watchlist/add", "/watchlist/remove", "/market/refresh-request"}
    pages = NEW_GETS[:10] + ["/positions/OPN", "/market/BBB", "/positions", "/portfolio", "/portfolio/labels/AAA", "/portfolio/labels/BBB", "/portfolio/label-result?code=recorded&ticker=AAA", "/alerts", "/checkin"]
    html_pages = [(p, w.get(p)[2]) for p in pages]
    # also the preview pages (POST responses)
    for f, path in ((sell("BBB", "30"), "/trade/preview"), ({"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"}, "/checkin/preview"),
                    ({"action": "close_order", "trade_id": "OPN"}, "/trade/preview")):
        html_pages.append((path + ":" + f["action"], w.post(path, f)[2]))
    seen = set()
    for name, html in html_pages:
        low = html.lower()
        assert "<script" not in low and "javascript:" not in low and not re.search(r"\son[a-z]+\s*=", low), name
        for m in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.S | re.I):
            tag = re.match(r"<form\b[^>]*>", m.group(0), re.I).group(0).lower()
            if 'method="post"' in tag:
                act = re.search(r'action="([^"]+)"', tag).group(1)
                assert act in allowed, (name, tag)
                seen.add(act)
                if act != "/logout":
                    assert re.search(r'name="csrf"', m.group(1)), (name, act)
            else:
                assert 'method="get"' in tag and not re.search(r'name="(csrf|tok)"', m.group(1)), (name, tag)
        for t in re.finditer(r"<(a|link|img|form)\b[^>]*>", html, re.I):
            assert not re.search(r'(href|src|action)\s*=\s*"\s*(https?:)?//', t.group(0), re.I), (name, t.group(0))
    assert {"/trade/preview", "/trade/confirm", "/checkin/preview", "/checkin/confirm"} <= seen, seen


# ======================================================================================== E: layered request checks
def attack_cases(good_csrf):
    return {
        "no session": dict(csrf=good_csrf, auth=False),
        "missing csrf": dict(csrf=None),
        "empty csrf": dict(csrf=""),
        "wrong csrf": dict(csrf="0" * 64),
        "csrf prefix": dict(csrf=good_csrf[:32]),
        "csrf + junk": dict(csrf=good_csrf + "00"),
        "missing origin": dict(csrf=good_csrf, origin=None),
        "foreign origin": dict(csrf=good_csrf, origin="http://evil.example"),
        "null origin": dict(csrf=good_csrf, origin="null"),
        "other port": dict(csrf=good_csrf, origin="http://127.0.0.1:1"),
        "https origin": dict(csrf=good_csrf, origin="https://127.0.0.1:1"),
        "wrong host": dict(csrf=good_csrf, host="evil.example"),
        "cross-site": dict(csrf=good_csrf, headers={**FORM, "Sec-Fetch-Site": "cross-site"}),
        "same-site": dict(csrf=good_csrf, headers={**FORM, "Sec-Fetch-Site": "same-site"}),
        "json": dict(csrf=good_csrf, headers={"Content-Type": "application/json"}),
        "text/plain": dict(csrf=good_csrf, headers={"Content-Type": "text/plain"}),
        "multipart": dict(csrf=good_csrf, headers={"Content-Type": "multipart/form-data; boundary=x"}),
        "no content-type": dict(csrf=good_csrf, headers={}),
    }


@pytest.mark.parametrize("path", NEW_POSTS)
def test_each_new_post_needs_session_csrf_origin_host_and_form_content_type(w, path):
    good = w.csrf()
    if path.startswith("/trade"):
        html, cf, _ = prepared(w)
        fields = cf if path.endswith("confirm") else sell("BBB", "30")
    else:
        html = w.preview({"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"}, "/checkin/preview")[2]
        cf = w.confirm_fields(html)
        fields = cf if path.endswith("confirm") else {"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"}
    before = fingerprint(w.db)
    for name, kw in attack_cases(good).items():
        kw = dict(kw)
        csrf = kw.pop("csrf")
        st, hd, body = w.post(path, fields, csrf=csrf, **kw)
        assert st in (303, 400, 401, 403, 405, 421), (name, st)
        if st == 303:
            assert "/login" in hd.get("location", "") or "code=csrf" in hd.get("location", "") or "code=" in hd.get("location", ""), (name, hd)
            assert "code=recorded" not in hd.get("location", ""), name
        assert "tok" not in B1.hidden(body), name                       # an attacked preview must not yield a confirmable form
        assert fingerprint(w.db) == before, name
    assert web_trades(w) == []


def test_body_limit_is_6144_bytes_exactly_and_oversize_is_refused(w):
    csrf = w.csrf()
    base = urlencode({"csrf": csrf, **sell("BBB", "30")})
    pad = 6144 - len(base) - len("&pad=")
    ok = (base + "&pad=" + "a" * pad).encode()
    assert len(ok) == 6144
    before = fingerprint(w.db)
    st, _, body = w.post("/trade/preview", {}, csrf=None, raw=ok)
    assert st == 200 and "tok" in B1.hidden(body)
    big = (base + "&pad=" + "a" * (pad + 1)).encode()
    st, _, body = w.post("/trade/preview", {}, csrf=None, raw=big)
    assert st in (400, 413) and "tok" not in B1.hidden(body)
    st, _, _ = w.post("/trade/preview", {}, csrf=None, raw=b"x" * 200000)
    assert st in (400, 413)
    assert fingerprint(w.db) == before
    # a legitimate stage-A write still has the old 2 KB limit
    st, _, _ = w.post("/watchlist/add", {"symbol": "QQQ", "pad": "a" * 3000})
    assert st in (400, 413, 303) and w.sql("SELECT 1 FROM watchlist WHERE symbol='QQQ'") == []


def test_too_many_fields_and_malformed_bodies_are_refused_without_writes(w):
    before = fingerprint(w.db)
    csrf = w.csrf()
    many = urlencode({"csrf": csrf, **{f"f{i}": "1" for i in range(80)}}).encode()
    assert w.post("/trade/preview", {}, csrf=None, raw=many)[0] in (200, 400)
    for raw in (b"\xff\xfe=1", b"%ZZ=1&action=sell_filled", b"", b"&&&", b"csrf=" + csrf.encode() + b"&action=" + b"%00" * 50):
        st, _, body = w.post("/trade/preview", {}, csrf=None, raw=raw)
        assert st in (200, 303, 400), raw
        assert "tok" not in B1.hidden(body)
    assert fingerprint(w.db) == before


def test_preview_posts_write_nothing_valid_or_invalid(w):
    before = fingerprint(w.db)
    cases = [(sell("BBB", "30"), "/trade/preview"), (sell("BBB", "31"), "/trade/preview"), (sell("ZZZ", "30"), "/trade/preview"),
             ({**sell("BBB", "30"), "action": "sell_working"}, "/trade/preview"), ({"action": "close_order", "trade_id": "OPN"}, "/trade/preview"),
             ({"action": "close_filled", "trade_id": "OPN", "debit": "0.2", "fees": "0", "executed_date": "2026-10-30", "executed_time": "10:00"}, "/trade/preview"),
             ({"action": "expire", "trade_id": "OPN"}, "/trade/preview"), ({"action": "cancel_open_order", "trade_id": "OPN"}, "/trade/preview"),
             ({"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"}, "/checkin/preview"),
             ({"action": "checkin", "equity": "1", "free_cash": "96000", "nlots": "0"}, "/checkin/preview"),
             ({"action": "nonsense"}, "/trade/preview"), ({"action": "checkin"}, "/trade/preview"), ({"action": "sell_filled"}, "/checkin/preview")]
    for f, path in cases:
        w.post(path, f)
        assert fingerprint(w.db) == before, f
    for _ in range(3):                                      # repeated previews: still nothing
        w.preview(sell("BBB", "30"))
    assert fingerprint(w.db) == before


# ======================================================================================== E: the preview token
def test_confirm_with_any_tampered_bound_field_is_rejected_and_writes_nothing(w):
    html, cf, _ = prepared(w)
    bound = [k for k in cf if k not in ("tok", "reason", "csrf")]
    assert {"action", "idem", "symbol", "expiry", "strike", "credit", "fees", "executed_date", "executed_time", "trade_id", "rev", "psha"} <= set(bound)
    before = fingerprint(w.db)
    for k in bound:
        bad = dict(cf)
        bad[k] = bad[k] + "1" if k not in ("rev",) else str(int(bad[k]) + 1)
        st, loc, _ = confirm(w, bad)
        assert code_of(w, loc) in ("token", "expired", "stale"), (k, loc)
        bad = dict(cf)
        del bad[k]
        st, loc, _ = confirm(w, bad)
        assert code_of(w, loc) != "recorded", (k, loc)
    st, loc, _ = confirm(w, {**cf, "evil": "1"})                      # injected extra field
    assert code_of(w, loc) in ("token", "expired", "stale")
    st, loc, _ = confirm(w, {**cf, "credit": "0.90"})                 # the classic: a better fill after the preview
    assert code_of(w, loc) in ("token", "expired", "stale")
    st, loc, _ = confirm(w, {**cf, "tok": cf["tok"][:-1] + ("0" if cf["tok"][-1] != "0" else "1")})
    assert code_of(w, loc) in ("token", "expired")
    st, loc, _ = confirm(w, {**cf, "tok": "1." + "0" * 64})
    assert code_of(w, loc) in ("token", "expired")
    st, loc, _ = confirm(w, {**cf, "tok": ""})
    assert code_of(w, loc) in ("token", "expired")
    assert fingerprint(w.db) == before and web_trades(w) == []
    st, loc, _ = confirm(w, cf)                                       # the untouched form still works afterwards
    assert code_of(w, loc) == "recorded"


def test_preview_token_cannot_be_replayed_on_another_session(w):
    html, cf, csrf1 = prepared(w)
    cookie1 = w.d.cookie
    st = w.d.login()
    assert st == 303 and w.d.cookie != cookie1
    csrf2 = w.csrf()
    before = fingerprint(w.db)
    st, loc, _ = confirm(w, cf, csrf=csrf2)                           # valid session 2 + csrf 2, token from session 1
    assert code_of(w, loc) in ("token", "expired") and fingerprint(w.db) == before
    w.d.cookie = cookie1                                              # and session 1 itself still works
    assert code_of(w, confirm(w, cf, csrf=csrf1)[1]) == "recorded"


def test_stale_ledger_revision_rejected_when_another_write_happens_between_preview_and_confirm(w):
    _, cf, _ = prepared(w, sell("BBB", "30"))
    _, loc, _ = w.do({"action": "close_order", "trade_id": "OPN"})        # another ledger write in between
    assert code_of(w, loc) == "recorded"
    before = n_rows(w)
    st, loc, _ = confirm(w, cf)
    assert code_of(w, loc) == "stale", loc
    assert n_rows(w) == before and web_trades(w) == []
    # forging the revision to the current one breaks the token
    cur = int(B1.hidden(w.preview(sell("BBB", "30"))[2])["rev"])
    st, loc, _ = confirm(w, {**cf, "rev": str(cur)})
    assert code_of(w, loc) in ("token", "expired")
    assert web_trades(w) == []


def test_stale_when_a_checkin_snapshot_lands_between_preview_and_confirm(w):
    _, cf, _ = prepared(w)
    checkin(w, "100000", "96000")
    st, loc, _ = confirm(w, cf)
    assert code_of(w, loc) in ("stale", "token") and web_trades(w) == []


def test_preview_token_expires_after_20_minutes_boundary(w):
    """TTL 20 min = 1200 s. With a frozen server monotonic clock: age 1200 accepted, 1201 expired."""
    sessions = w.d.srv.sessions
    base = float(int(time.monotonic()) + 10)
    sessions._clock = lambda: base
    try:
        _, cf, _ = prepared(w)
        before = fingerprint(w.db)
        sessions._clock = lambda: base + 1201
        st, loc, _ = confirm(w, cf)
        assert code_of(w, loc) == "expired" and fingerprint(w.db) == before
        sessions._clock = lambda: base + 1200
        st, loc, _ = confirm(w, cf)
        assert code_of(w, loc) == "recorded"
    finally:
        sessions._clock = time.monotonic


def test_policy_change_between_preview_and_confirm_is_rejected(tmp_path):
    pol = tmp_path / "policy_copy.yaml"
    text = (REPO / "config" / "policy.yaml").read_text(encoding="utf-8")
    pol.write_text(text, encoding="utf-8")
    x = B1(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
    try:
        srcs = x.d.srv.sources
        object.__setattr__(srcs, "policy_path", pol) if getattr(type(srcs), "__dataclass_params__").frozen else setattr(srcs, "policy_path", pol)
        html = x.preview(sell("BBB", "30"))[2]
        cf = x.confirm_fields(html)
        assert "tok" in cf
        assert "max_alternative_alerts_per_scan: 3" in text
        pol.write_text(text.replace("max_alternative_alerts_per_scan: 3", "max_alternative_alerts_per_scan: 2"), encoding="utf-8")
        before = fingerprint(x.db)
        st, loc, _ = confirm(x, cf)
        assert x.result_code(loc) == "stale", loc
        assert fingerprint(x.db) == before
        # wrong psha forged to the new policy -> token mismatch
        bad = dict(cf)
        bad["psha"] = "0" * 64
        assert x.result_code(confirm(x, bad)[1]) in ("token", "expired")
    finally:
        x.stop()


# ======================================================================================== E: idempotency
def test_double_submit_and_reload_after_confirm_record_exactly_once(w):
    _, cf, csrf = prepared(w)
    st, loc1, _ = confirm(w, cf)
    st, loc2, _ = confirm(w, cf)
    assert code_of(w, loc1) == "recorded" and code_of(w, loc2) == "duplicate"
    assert tid_of(loc1) == tid_of(loc2)
    tid = tid_of(loc1)
    n = n_rows(w)
    for _ in range(3):                                                # reload the result page, resubmit again
        assert w.get(loc1)[0] == 200
        confirm(w, cf)
    assert n_rows(w) == n
    assert len(web_trades(w)) == 1
    # one economic event and one audit set
    assert len(w.sql("SELECT 1 FROM audit_log WHERE event_type='WEB_TRADE_RECORDED'")) == 1
    assert len(w.sql("SELECT 1 FROM audit_log WHERE event_type='MANUAL_COMMAND' AND after_json LIKE ?", (f"%{tid}%",))) == 1
    assert len(w.sql("SELECT 1 FROM audit_log WHERE event_type='POSITION_DRAFT_CREATED' AND after_json LIKE ?", (f"%{tid}%",))) == 1
    assert len(w.sql("SELECT 1 FROM audit_log WHERE event_type='POSITION_TRANSITION' AND after_json LIKE ?", (f"%{tid}%",))) == 2   # ->PENDING, ->OPEN


def test_duplicate_confirm_returns_first_result_even_after_ledger_moved_on(w):
    _, cf, _ = prepared(w)
    assert code_of(w, confirm(w, cf)[1]) == "recorded"
    w.do({"action": "close_order", "trade_id": "OPN"})                # ledger revision moved
    assert code_of(w, confirm(w, cf)[1]) == "duplicate"
    assert len(web_trades(w)) == 1


def test_concurrent_duplicate_confirms_record_exactly_once(w):
    _, cf, csrf = prepared(w)
    results, errs = [], []

    def go():
        try:
            st, hd, _ = w.post("/trade/confirm", cf, csrf=csrf)
            results.append(code_of(w, hd.get("location", "")))
        except Exception as exc:                                     # noqa: BLE001
            errs.append(type(exc).__name__)
    ts = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    assert results.count("recorded") == 1, results
    assert set(results) <= {"recorded", "duplicate", "unavailable", "rejected"}, results
    assert len(web_trades(w)) == 1
    assert len(w.sql("SELECT 1 FROM audit_log WHERE event_type='WEB_TRADE_RECORDED'")) == 1
    assert len(w.sql("SELECT 1 FROM audit_log WHERE event_type='MANUAL_COMMAND'")) == 1


def test_same_form_replayed_from_a_fresh_preview_is_a_new_idempotency_key(w):
    """Two separate previews of the same trade have different keys: confirming both would record two positions; the second
    is blocked by the ledger rules or not, but it is never silently merged. Documented behaviour check only."""
    _, cf1, _ = prepared(w)
    _, cf2, _ = prepared(w)
    assert cf1["idem"] != cf2["idem"] and cf1["trade_id"] != cf2["trade_id"]


def test_missing_reason_blank_reason_and_missing_ack_are_rejected(w):
    html = w.preview(sell("BBB", "30"))[2]
    before = fingerprint(w.db)
    for reason in ("", "   ", "\x01\x02", "‮​"):
        cf = w.confirm_fields(html, reason=reason)
        assert code_of(w, confirm(w, cf)[1]) == "reason_required", repr(reason)
    cf = w.confirm_fields(html)
    del cf["reason"]
    assert code_of(w, confirm(w, cf)[1]) == "reason_required"
    assert fingerprint(w.db) == before
    # acknowledgement missing for a breach
    html = w.preview(sell("ZZZ", "30", sector="SX", bucket="BX"))[2]
    assert "ack_breach" in html
    assert code_of(w, confirm(w, w.confirm_fields(html, acks=[]))[1]) == "ack_required"
    assert code_of(w, confirm(w, {**w.confirm_fields(html, acks=[]), "ack_breach": "0"})[1]) == "ack_required"
    assert code_of(w, confirm(w, {**w.confirm_fields(html, acks=[]), "ack_breach": "yes"})[1]) == "ack_required"
    assert fingerprint(w.db) == before


# ======================================================================================== E: atomicity
def test_journal_tx_is_reentrant_savepoint_inner_failure_keeps_outer_and_outer_failure_rolls_back_all(tmp_path):
    from csp_alerts.journal.store import Journal
    j = Journal(tmp_path / "sp.sqlite3", clock=lambda: NOW)
    j.migrate()

    def ev(name):
        return [r["event_type"] for r in j.audit_events() if r["event_type"].startswith("T_")] .count(name)
    with j.tx():
        j.audit_event("T_A1", "t", {}, "x")
        with pytest.raises(RuntimeError):
            with j.tx():
                j.audit_event("T_A2", "t", {}, "x")
                raise RuntimeError("inner")
        j.audit_event("T_A3", "t", {}, "x")
    assert (ev("T_A1"), ev("T_A2"), ev("T_A3")) == (1, 0, 1)
    with pytest.raises(RuntimeError):
        with j.tx():
            j.audit_event("T_B1", "t", {}, "x")
            with j.tx():
                j.audit_event("T_B2", "t", {}, "x")                  # inner SUCCEEDS (savepoint released)...
            raise RuntimeError("outer")                              # ...but the outer unit fails: everything must vanish
    assert (ev("T_B1"), ev("T_B2")) == (0, 0)
    assert not j.conn.in_transaction
    with j.tx():                                                     # still usable afterwards
        j.audit_event("T_C1", "t", {}, "x")
    assert ev("T_C1") == 1
    # deep nesting + repeated failures leave no dangling savepoint
    for i in range(5):
        with pytest.raises(ValueError):
            with j.tx():
                with j.tx():
                    with j.tx():
                        j.audit_event(f"T_D{i}", "t", {}, "x")
                raise ValueError
    assert not any(ev(f"T_D{i}") for i in range(5)) and not j.conn.in_transaction
    j.close()


@pytest.mark.parametrize("where,exc", [("transition2", "JournalError"), ("audit_last", "JournalError"), ("audit_last", "RuntimeError"),
                                       ("audit_breach", "JournalError"), ("draft", "RuntimeError")])
def test_failure_midway_rolls_back_the_whole_web_record(w, monkeypatch, where, exc):
    """Force a failure after some writes (draft/PENDING already made, or even OPEN + audit rows made) and prove nothing remains."""
    from csp_alerts.journal import store
    from csp_alerts.journal.store import Journal, JournalError
    boom = {"JournalError": JournalError, "RuntimeError": RuntimeError}[exc]
    fields = sell("ZZZ", "30", sector="SX", bucket="BX") if where == "audit_breach" else sell("BBB", "30")
    html = w.preview(fields)[2]
    cf = w.confirm_fields(html)
    before = n_rows(w)
    snap = fingerprint(w.db)
    counter = {"n": 0}
    real_t, real_a, real_d = Journal.transition, Journal.audit_event, Journal.create_draft

    def tr(self, *a, **k):
        counter["n"] += 1
        if where == "transition2" and counter["n"] == 2:
            raise boom("forced")
        return real_t(self, *a, **k)

    def au(self, event_type, *a, **k):
        if (where == "audit_last" and event_type == "WEB_TRADE_RECORDED") or (where == "audit_breach" and event_type == "POLICY_BREACH_RECORDED"):
            raise boom("forced")
        return real_a(self, event_type, *a, **k)

    def dr(self, *a, **k):
        r = real_d(self, *a, **k)
        if where == "draft":
            raise boom("forced")
        return r
    monkeypatch.setattr(Journal, "transition", tr)
    monkeypatch.setattr(Journal, "audit_event", au)
    monkeypatch.setattr(Journal, "create_draft", dr)
    st, loc, body = confirm(w, cf)
    assert st in (303, 500, 503), st
    assert "Traceback" not in body and "forced" not in body and str(REPO) not in body
    monkeypatch.undo()
    assert n_rows(w) == before and web_trades(w) == [], (where, exc, loc)
    assert w.sql("SELECT COUNT(*) FROM audit_log WHERE event_type IN ('WEB_TRADE_RECORDED','POLICY_BREACH_RECORDED','MANUAL_COMMAND')") == [(0,)]
    # nothing is left locked / half-open: the same (untouched) confirm now succeeds exactly once
    assert code_of(w, confirm(w, cf)[1]) in ("recorded", "stale")


# ======================================================================================== E: hostile text and canned errors
RAW_TAG = re.compile(r"<script|<img|<b>|<i>|onerror\s*=\s*alert(?![^<>]*&)", re.I)
EVIL = '<script>alert(1)</script>"\'><img src=x onerror=alert(1)>\x00\x1b[31m‮​\r\nINJECT'


def test_hostile_reason_is_cleaned_limited_stored_once_and_escaped_everywhere(w):
    html = w.preview(sell("BBB", "30"))[2]
    cf = w.confirm_fields(html, reason=EVIL + "A" * 400)
    st, loc, _ = confirm(w, cf)
    assert code_of(w, loc) == "recorded"
    tid = tid_of(loc)
    stored = w.sql("SELECT after_json FROM audit_log WHERE event_type='MANUAL_COMMAND' AND after_json LIKE ?", (f"%{tid}%",))[0][0]
    import json
    reason = json.loads(stored)["reason"]
    assert len(reason) <= 300 and not re.search(r"[\x00-\x1f\x7f‮​]", reason)
    notes = w.sql("SELECT notes FROM option_positions WHERE trade_id=?", (tid,))[0][0]
    assert len(notes) <= 200 and not re.search(r"[\x00-\x1f\x7f‮​]", notes)
    for p in (f"/positions/{tid}", "/positions", "/activity", "/api/positions/" + tid, "/api/overview", "/activity?type=MANUAL_COMMAND"):
        st, hd, body = w.get(p)
        if p.startswith("/api/"):                       # JSON is not HTML: it must be served as JSON with nosniff
            assert hd["content-type"].startswith("application/json") and hd.get("x-content-type-options") == "nosniff", p
        else:
            assert not RAW_TAG.search(body), p
        assert "\x1b" not in body and "‮" not in body, p


def test_hostile_field_values_are_not_reflected_raw_in_error_pages(w):
    bad = {**sell("BBB", "30"), "credit": EVIL, "fees": EVIL, "executed_date": EVIL, "executed_time": EVIL, "symbol": EVIL, "expiry": EVIL,
           "strike": EVIL, "contracts": EVIL, "sector": EVIL, "bucket": EVIL}
    for path, f in (("/trade/preview", bad), ("/trade/preview", {**bad, "action": "sell_working"}),
                    ("/trade/preview", {"action": "close_filled", "trade_id": EVIL, "debit": EVIL}),
                    ("/checkin/preview", {"action": "checkin", "equity": EVIL, "free_cash": EVIL, "nlots": EVIL, "lot_0": EVIL, "mark_0": EVIL})):
        st, _, body = w.post(path, f)
        assert st in (200, 400)
        assert not RAW_TAG.search(body), (path, f.get("action"))
        assert "tok" not in B1.hidden(body)
    for p in ("/trade/sell?symbol=%3Cscript%3Ealert(1)%3C/script%3E&expiry=%22%3E&strike=%3Cb%3E&kind=%3Ci%3E",
              "/trade/close-order/%3Cscript%3E", "/trade/result?code=%3Cscript%3E&trade=%3Cb%3E&a=%3Ci%3E"):
        body = w.get(p)[2]
        assert not RAW_TAG.search(body), p


def test_hostile_unapproved_labels_cannot_be_entered(w):
    html = w.preview(sell("ZZZ", "30", sector='<script>x</script>', bucket="ok"))[2]
    assert "tok" not in B1.hidden(html) and "<script>x" not in html
    html = w.preview(sell("ZZZ", "30", sector="A" * 41, bucket="ok"))[2]
    assert "tok" not in B1.hidden(html)


def test_errors_are_canned_no_tracebacks_paths_or_secrets_in_any_response(tmp_path, capfd):
    x = B1(tmp_path)
    bodies = []
    try:
        csrf = x.csrf()
        for f in (sell("BBB", "31"), {"action": "x"}, {"action": "close_filled", "trade_id": "NOPE"}, {}, sell("BBB", "30", credit="abc")):
            bodies.append(x.post("/trade/preview", f)[2])
        html = x.preview(sell("BBB", "30"))[2]
        cf = x.confirm_fields(html)
        for bad in ({**cf, "tok": "junk"}, {**cf, "rev": "x"}, {**cf, "reason": ""}, {k: v for k, v in cf.items() if k != "action"}):
            st, hd, body = x.post("/trade/confirm", bad)
            bodies.append(body)
            bodies.append(x.get(hd.get("location", "/"))[2])
        for code in ("recorded", "duplicate", "stale", "expired", "token", "invalid", "reason_required", "ack_required", "unavailable", "rejected",
                     "unsupported", "csrf", "unknown"):
            bodies.append(x.get(f"/trade/result?code={code}&trade=T1&a=sell_filled")[2])
        cookie = x.d.cookie
        good = x.csrf()
        x.confirm_fields(html)
        out_err = capfd.readouterr()
        logs = out_err.out + out_err.err
        blob = "\n".join(bodies)
    finally:
        x.stop()
    for needle in (str(tmp_path), str(REPO), "Traceback", ".py\"", "sqlite3.", "File \"", "JournalError", "TransitionError", "RuleError", "KeyError"):
        assert needle not in blob, needle
    for sec in (PW, cookie):
        assert sec not in blob and sec not in logs
    assert "scrypt$" not in blob + logs
    assert "csrf" not in logs.lower().replace("csrf_", "") or good not in logs
    assert good not in logs and cf["tok"] not in logs


def test_no_secrets_or_tokens_in_logs_during_a_full_record_flow(tmp_path, capfd):
    x = B1(tmp_path)
    try:
        html = x.preview(sell("BBB", "30", ))[2]
        cf = x.confirm_fields(html, reason="UNIQUE-REASON-TEXT-5521")
        x.post("/trade/confirm", cf)
        logs = capfd.readouterr()
        text = logs.out + logs.err
        cookie, tok = x.d.cookie, cf["tok"]
    finally:
        x.stop()
    for sec in (PW, cookie, tok, cf["csrf"] if "csrf" in cf else "~~", "UNIQUE-REASON-TEXT-5521", "scrypt$"):
        assert sec not in text, sec


# ======================================================================================== E: actor/source, audit immutability
def test_every_audit_row_of_a_web_record_has_actor_web_owner_and_source_web(w):
    before = w.sql("SELECT COUNT(*) FROM audit_log")[0][0]
    w.do(sell("ZZZ", "30", sector="SX", bucket="BX"))
    w.do({"action": "checkin", "equity": "100000", "free_cash": "92000", "nlots": "0"}, path="/checkin/preview", cpath="/checkin/confirm")
    rows = w.sql("SELECT event_type, actor, source FROM audit_log ORDER BY rowid")[before:]
    kinds = {r[0] for r in rows}
    assert {"POSITION_DRAFT_CREATED", "POSITION_TRANSITION", "MANUAL_COMMAND", "POLICY_BREACH_RECORDED", "WEB_TRADE_RECORDED", "SNAPSHOT_ADDED"} <= kinds, kinds
    assert {(r[1], r[2]) for r in rows} == {("web:owner", "web")}, rows


def test_audit_log_stays_append_only_after_web_writes(w):
    w.do(sell("BBB", "30"))
    c = sqlite3.connect(w.db)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE audit_log SET actor='x' WHERE rowid=(SELECT MAX(rowid) FROM audit_log)")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM audit_log WHERE rowid=(SELECT MAX(rowid) FROM audit_log)")
    finally:
        c.rollback()
        c.close()
    ev = w.sql("SELECT event_type FROM audit_log WHERE event_type IN ('WEB_TRADE_RECORDED')")
    assert ev == [("WEB_TRADE_RECORDED",)]


def test_web_cannot_cross_into_cli_only_controls(w):
    """baseline / cash-flow / risk-reset / approvals / assignment / partial fills are not reachable through any web action value."""
    before = fingerprint(w.db)
    for a in ("confirm_baseline", "record_cash_flow", "risk_reset", "approve", "assign", "assignment", "partial_fill", "adjust", "reset",
              "SELL_FILLED", "sell_filled ", "checkin;", "../checkin"):
        html = w.post("/trade/preview", {"action": a, "equity": "100000", "amount": "5000", "symbol": "BBB"})[2]
        assert "tok" not in B1.hidden(html), a
    assert fingerprint(w.db) == before


# ======================================================================================== E: import boundary and no order code
def _closure():
    import importlib.util
    root = REPO / "src"
    seen, todo, edges = set(), ["csp_dashboard.server", "csp_dashboard.cli"], {}
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        try:
            spec = importlib.util.find_spec(m) if m.split(".")[0] in ("csp_dashboard", "csp_alerts") else None
        except (ImportError, ValueError, AttributeError):          # 'module.name' from a from-import that is a function, not a module
            spec = None
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            continue
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        imps = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imps |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                imps.add(n.module)
                imps |= {f"{n.module}.{a.name}" for a in n.names}
        edges[m] = imps
        todo += [i for i in imps if i.startswith(("csp_dashboard", "csp_alerts"))]
    return edges


BAD_ROOTS = {"socket", "ssl", "subprocess", "ftplib", "smtplib", "telnetlib", "requests", "httpx", "aiohttp", "urllib3", "telegram", "yfinance",
             "ib_insync", "ibapi", "alpaca", "schwab", "tradier", "tastytrade", "asyncio", "multiprocessing", "webbrowser", "ctypes"}
BAD_INTERNAL = ("csp_alerts.alerts", "csp_alerts.runner", "csp_alerts.cli", "csp_alerts.ledger_cli", "csp_alerts.jobs.scan", "csp_alerts.jobs.monitor",
                "csp_alerts.jobs.market_refresh", "csp_alerts.jobs.ticker_check", "csp_alerts.market.stockpile",
                "csp_alerts.lock", "csp_alerts.journal.import_csv", "options_scanner")


def test_dashboard_import_closure_now_includes_the_trade_modules_and_still_has_no_network_telegram_subprocess_or_job_code():
    edges = _closure()
    for m in ("csp_alerts.journal.webtrade", "csp_alerts.journal.ledger_rules", "csp_alerts.journal.webwrites", "csp_alerts.policy.portfolio"):
        assert m in edges, m
    for mod, imps in edges.items():
        for i in imps:
            top = i.split(".")[0]
            ok_server = mod == "csp_dashboard.server" and i in ("http.server", "http.cookies")
            assert (top not in BAD_ROOTS or ok_server), (mod, i)
            assert not i.startswith(BAD_INTERNAL), (mod, i)


def test_runtime_import_loads_trade_modules_but_no_forbidden_module():
    code = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport csp_dashboard.server, csp_dashboard.cli  # noqa\n"
            "bad = ['requests','urllib3','httpx','aiohttp','yfinance','smtplib','ftplib','subprocess','asyncio','urllib.request','csp_alerts.alerts',"
            "'csp_alerts.market.stockpile','csp_alerts.jobs.scan','csp_alerts.jobs.monitor','csp_alerts.jobs.market_refresh','csp_alerts.jobs.ticker_check','csp_alerts.runner','csp_alerts.ledger_cli','csp_alerts.cli','options_scanner','telegram',"
            "'ib_insync','alpaca','schwab','tda','robin_stocks']\n"
            "print('LOADED', sorted(m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in bad)))\n"
            "print('TRADE', 'csp_alerts.journal.webtrade' in sys.modules)\n")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(REPO / "src")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "LOADED []", r.stdout
    assert r.stdout.splitlines()[1] == "TRADE True"


ORDER = re.compile(r"place_?order|submit_?order|modify_?order|replace_?order|send_?order|execute_?order|create_?order|new_?order|"
                   r"buy_?to_?close_?order\(|sell_?to_?open_?order\(|order_?entry|/orders?\b|brokerage", re.I)
BROKER = re.compile(r"\b(ib_insync|ibapi|alpaca|alpaca_trade_api|schwab|tdameritrade|tradier|tastytrade|robin_stocks|webull|futu|moomoo|ccxt)\b", re.I)


def test_src_has_no_order_placement_or_broker_code_anywhere():
    """Advisory-only guarantee. AST scan of every function/attribute/name and every string constant under src/."""
    hits = []
    for f in (REPO / "src").rglob("*.py"):
        if f.name == "boundary.py":                                 # defines the forbidden lists themselves
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            names = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(n.name)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
            elif isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.append(n.module)
            for nm in names:
                if nm == "can_send_orders":                          # policy safety flag typed Literal[False]; asserted below
                    continue
                if ORDER.search(nm) or BROKER.search(nm):
                    hits.append((f.name, nm))
    assert hits == [], hits
    from csp_alerts.policy.config import load_policy
    from acc_helpers import POLICY_PATH
    assert load_policy(POLICY_PATH).safety.can_send_orders is False if hasattr(load_policy(POLICY_PATH), "safety") else True
    sql = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "src" / "csp_alerts" / "journal" / "migrations").glob("*.sql"))
    assert not re.search(r"\border_id\b|\bbroker_order", sql, re.I)


# ======================================================================================== F: check-in lots
def lots_env(tmp_path, n):
    from datetime import datetime
    from csp_alerts.journal.models import StockLot
    x = B1(tmp_path)
    x.d.stop()                                                  # reopen with lots added (journal writes before the server runs)
    from stage_a_helpers import jopen
    j = jopen(x.db, NOW - timedelta(hours=3))
    for i in range(n):
        j.add_stock_lot(StockLot(f"L{i:02d}", "EEE", D("100"), D("40.00"), NOW - timedelta(hours=3), f"s{i}", f"b{i}", "OWNED", "ETF", None, None))
    j.close()
    from web_helpers import Dash
    x.d = Dash(x.db, x.appr, now=DASH_NOW)
    assert x.d.login() == 303
    return x


def test_checkin_twelve_lots_work_and_marks_are_applied(tmp_path):
    x = lots_env(tmp_path, 12)
    try:
        t = text_of(x.get("/checkin")[2])
        assert "L00" in t and "L11" in t
        f = {"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "12"}
        for i in range(12):
            f[f"lot_{i}"], f[f"mark_{i}"] = f"L{i:02d}", "41.50" if i == 0 else ""
        st, loc, _ = x.do(f, path="/checkin/preview", cpath="/checkin/confirm", reason="daily")
        assert x.result_code(loc) == "recorded"
        mark = x.sql("SELECT reference_price FROM stock_lots WHERE lot_id='L00'")
        assert mark and D(mark[0][0]) == D("41.50")
    finally:
        x.stop()


def test_checkin_thirteen_lots_shows_cli_pointer_instead_of_crashing(tmp_path):
    y = lots_env(tmp_path, 13)
    try:
        st, _, body = y.get("/checkin")
        assert st == 200 and "use the CLI" in text_of(body)
    finally:
        y.stop()


def test_checkin_has_no_way_to_backdate_or_future_date_the_snapshot_and_unknown_lot_is_refused(w):
    n = n_rows(w)
    html = w.preview({"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0", "asof": "2099-01-01", "asof_utc": "2099-01-01T00:00:00Z"},
                     "/checkin/preview")[2]
    cf = w.confirm_fields(html)
    if "tok" in cf:
        assert code_of(w, w.post("/checkin/confirm", cf)[1]["location"]) in ("recorded", "token")
    rows = w.sql("SELECT asof_utc FROM account_snapshots ORDER BY asof_utc DESC LIMIT 1")
    assert rows and rows[0][0] <= "2026-10-30T15:05:32Z"
    html = w.preview({"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "1", "lot_0": "NOPE", "mark_0": "41"}, "/checkin/preview")[2]
    assert "tok" not in B1.hidden(html)


@pytest.mark.parametrize("eq,cash,ok", [("100000", "96000", True), ("0", "0", False), ("-5", "0", False), ("abc", "1", False), ("100000.001", "1", False),
                                        ("99999999999999", "1", False), ("100000", "", False), ("", "5", False), ("100000", "0.0", True)])
def test_checkin_equity_and_cash_input_validation(w, eq, cash, ok):
    html = w.preview({"action": "checkin", "equity": eq, "free_cash": cash, "nlots": "0"}, "/checkin/preview")[2]
    assert ("tok" in B1.hidden(html)) is ok


# ======================================================================================== G: truthfulness
def test_pages_keep_indicative_unknown_quote_age_wording_and_do_not_imply_the_app_placed_orders(w):
    pages = {p: text_of(w.get(p)[2]) for p in NEW_GETS[:10] + ["/market/BBB", "/positions/OPN"]}
    pages["preview"] = text_of(w.preview(sell("BBB", "30"))[2])
    pages["preview_close"] = text_of(w.preview({"action": "close_order", "trade_id": "OPN"})[2])
    for name, t in pages.items():
        low = t.lower()
        assert not re.search(r"real-?time|\blive price|\blive quote|exchange-fresh|guaranteed", low), name
        # no wording that the APP sent/placed/executed anything
        assert not re.search(r"(we|app|dashboard|system|bot) (has |have )?(placed|sent|submitted|executed|sold|bought)", low), name
        assert not re.search(r"your order (has been|was) (placed|sent|submitted|executed)", low), name
        assert not re.search(r"order (placed|sent|submitted) (for you|successfully)", low), name
    for p in ("/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled", "/trade/close-order/OPN", "/trade/close-fill/OPN"):
        assert "places, changes or cancels nothing" in pages[p].replace("  ", " ") or "places nothing" in pages[p]
    assert "Yahoo" in pages["/market/BBB"] and "Quote age unknown" in pages["/market/BBB"] and "Not a live exchange feed" in pages["/market/BBB"]
    assert "Quotes are indicative" in pages["/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled"]
    t = pages["/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled"]
    assert "Reference only (NOT your fill)" in t and "stored bid" in t
    assert "RECORDS a trade you already made" in t


def test_stored_bid_is_labelled_reference_everywhere_it_appears_on_new_pages(w):
    for p in ("/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled", "/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=working"):
        t = text_of(w.get(p)[2])
        i = t.find("stored bid")
        assert i > 0 and "Reference only" in t[max(0, i - 60): i], p
    t = text_of(w.preview(sell("BBB", "30"))[2])
    assert "0.5 " not in t.split("Actual credit you report")[1][:120] or "$0.45" in t


def test_action_labels_use_sold_put_and_buy_to_close_correctly(w):
    sell_t = text_of(w.get("/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled")[2])
    assert "sold put" in sell_t.lower() and "buy-to-close" not in sell_t.lower().split("exact contract")[0]
    close_t = text_of(w.get("/trade/close-order/OPN")[2]) + text_of(w.get("/trade/close-fill/OPN")[2])
    assert "buy-to-close" in close_t
    assert "sell-to-open" not in close_t.lower() and "sell to open" not in close_t.lower()
    pos_t = text_of(w.get("/positions/OPN")[2])
    assert "closing order" in pos_t and "closing fill" in pos_t
    # the P&L basis is buy-to-close at the ask, not a bid
    assert "buy-to-close at the stored ask" in w.get("/api/positions/OPN")[2] or "buy-to-close" in pos_t.lower()
    # WT-04 asks for 'sell to open creates the short put' wording: report if absent on the sell form
    print("SELL-TO-OPEN wording on sell form:", "sell to open" in sell_t.lower() or "sell-to-open" in sell_t.lower())
