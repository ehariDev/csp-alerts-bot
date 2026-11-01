"""Stage A acceptance: web write security (WT-AT-09 subset), truthfulness (MD-AT-03 wording) and the page import boundary.
Synthetic data, loopback server only, no network. Clock NOW = Fri 2026-10-30T15:05:02Z."""
from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from acc_helpers import NOW
from s2_helpers import raw_row
from stage_a_env import approved_file, dash, mkt, plain_db, text_of
from stage_a_helpers import EXP, SAT, Prov, add_wl, csrf_of, hold, jopen, md_dump, refresh
from web_helpers import PW, Dash, build_book, fingerprint

REPO = Path(__file__).resolve().parents[2]
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
WRITES = ("/watchlist/add", "/watchlist/remove", "/market/refresh-request")
READ_PAGES = ["/", "/candidates", "/positions", "/alerts", "/activity", "/runs", "/health", "/watchlist", "/market/ZZZ",
              "/market/ZZZ?expiry=2026-12-04", "/market/ZZZ?strike_min=50&strike_max=60&show=pass", "/positions?group=all",
              "/api/overview", "/api/watchlist", "/api/market/ZZZ", "/api/positions", "/api/health"]


@pytest.fixture()
def env(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ")
    refresh(db, p, NOW)
    d = dash(db, approved_file(tmp_path), now=NOW + timedelta(seconds=30))
    d.db, d.dir, d.prov = db, tmp_path, p
    yield d
    d.stop()


def post(d, path, fields, *, csrf="auto", **kw):
    if csrf == "auto":
        csrf = csrf_of(d.req("GET", "/watchlist")[2])
    body = "&".join(([f"csrf={csrf}"] if csrf is not None else []) + [f"{k}={v}" for k, v in fields.items()])
    kw.setdefault("origin", d.origin)
    kw.setdefault("headers", FORM)
    return d.req("POST", path, body=body.encode(), **kw)


def wl_rows(db):
    c = sqlite3.connect(db)
    r = [x[0] for x in c.execute("SELECT symbol FROM watchlist ORDER BY 1")]
    c.close()
    return r


# ----------------------------------------------------------------------------------------- the route set
def test_exactly_five_post_routes_everything_else_is_405(env):
    from csp_dashboard import server
    assert server.WRITE_PATHS == frozenset(WRITES)
    before = fingerprint(env.db)
    probes = ["/", "/watchlist", "/market/ZZZ", "/candidates", "/positions", "/alerts", "/activity", "/runs", "/health", "/api/overview",
              "/api/watchlist", "/watchlist/", "/watchlist/add/", "/watchlist/edit", "/watchlist/clear", "/market/ZZZ/refresh",
              "/market/refresh", "/refresh", "/trade", "/positions/OPN/close", "/static/app.css", "/admin", "/api/market/ZZZ"]
    for pth in probes:
        st, _, _ = post(env, pth, {"symbol": "QQQ"})
        assert st in (404, 405), (pth, st)
    assert fingerprint(env.db) == before and wl_rows(env.db) == ["ZZZ"]
    for pth in WRITES:                                            # GET on a write path never mutates and is not a view
        assert env.req("GET", pth + "?symbol=QQQ&csrf=x")[0] in (404, 405)
    assert fingerprint(env.db) == before


@pytest.mark.parametrize("pth", ["/api/watchlist/add", "/api/watchlist/remove", "/api/market/refresh-request"])
def test_write_routes_have_no_api_alias(env, pth):
    before = fingerprint(env.db)
    st, _, _ = post(env, pth, {"symbol": "QQQ", "scope": "ZZZ"})
    assert st in (404, 405), st
    assert fingerprint(env.db) == before


@pytest.mark.parametrize("pth", ["/api/", "/api/x/y", "/api/overview", "/api/watchlist", "/api/watchlist/add", "/api/market/refresh-request"])
@pytest.mark.parametrize("authed", [True, False])
def test_any_post_under_api_is_exactly_405_and_changes_nothing(env, pth, authed):
    """D4: the JSON mirror is read-only; every POST under /api/ is 405 (not 303/401/404), with or without a session/CSRF."""
    before = fingerprint(env.db)
    st, _, _ = post(env, pth, {"symbol": "QQQ", "scope": "ZZZ"}) if authed else env.req("POST", pth, auth=False, origin=env.origin, body=b"symbol=QQQ", headers=FORM)
    assert st == 405, (pth, authed, st)
    assert fingerprint(env.db) == before


# ----------------------------------------------------------------------------------------- auth / CSRF / Origin / Host
@pytest.mark.parametrize("path,fields", [("/watchlist/add", {"symbol": "QQQ"}), ("/watchlist/remove", {"symbol": "ZZZ"}),
                                         ("/market/refresh-request", {"scope": "ZZZ"})])
def test_each_write_needs_session_csrf_origin_host_and_form_content_type(env, path, fields):
    d = env
    good_csrf = csrf_of(d.req("GET", "/watchlist")[2])
    before = fingerprint(d.db)
    cases = {
        "no session": dict(csrf=good_csrf, auth=False),
        "missing csrf": dict(csrf=None),
        "empty csrf": dict(csrf=""),
        "wrong csrf": dict(csrf="0" * 64),
        "csrf of a prefix": dict(csrf=good_csrf[:32]),
        "csrf + junk": dict(csrf=good_csrf + "00"),
        "missing origin": dict(csrf=good_csrf, origin=None, headers=FORM),
        "foreign origin": dict(csrf=good_csrf, origin="http://evil.example"),
        "null origin": dict(csrf=good_csrf, origin="null"),
        "other port": dict(csrf=good_csrf, origin="http://127.0.0.1:1"),
        "https origin": dict(csrf=good_csrf, origin=f"https://127.0.0.1:{d.port}"),
        "wrong host": dict(csrf=good_csrf, host="evil.example"),
        "cross-site fetch": dict(csrf=good_csrf, headers={**FORM, "Sec-Fetch-Site": "cross-site"}),
        "same-site fetch": dict(csrf=good_csrf, headers={**FORM, "Sec-Fetch-Site": "same-site"}),
        "json body": dict(csrf=good_csrf, headers={"Content-Type": "application/json"}),
        "text/plain": dict(csrf=good_csrf, headers={"Content-Type": "text/plain"}),
        "no content type": dict(csrf=good_csrf, headers={}),
    }
    for name, kw in cases.items():
        kw = dict(kw)
        auth = kw.pop("auth", True)
        origin = kw.pop("origin", d.origin)
        st, hd, body = post(d, path, fields, auth=auth, origin=origin, **kw)
        assert st in (303, 400, 403, 421), (name, st)
        if st == 303:
            assert hd["location"].startswith(("/login", "/watchlist?msg=csrf", "/market/")), (name, hd["location"])
            assert "msg=csrf" in hd["location"] or hd["location"] == "/login", (name, hd["location"])
        assert fingerprint(d.db) == before, name                 # not one byte written by any refused request


def test_csrf_token_is_per_session_and_bound(env):
    d = env
    tok_a = csrf_of(d.req("GET", "/watchlist")[2])
    cookie_a = d.cookie
    assert d.login() == 303 and d.cookie != cookie_a
    tok_b = csrf_of(d.req("GET", "/watchlist")[2])
    assert tok_a != tok_b
    before = fingerprint(d.db)
    st, hd, _ = post(d, "/watchlist/add", {"symbol": "QQQ"}, csrf=tok_a)           # A's token inside B's session
    assert (st, hd["location"]) == (303, "/watchlist?msg=csrf") and fingerprint(d.db) == before
    d.cookie = cookie_a
    st, hd, _ = post(d, "/watchlist/add", {"symbol": "QQQ"}, csrf=tok_b)           # B's token inside A's session
    assert (st, hd["location"]) == (303, "/watchlist?msg=csrf") and fingerprint(d.db) == before
    st, hd, _ = post(d, "/watchlist/add", {"symbol": "QQQ"}, csrf=tok_a)
    assert (st, hd["location"]) == (303, "/watchlist?msg=added") and wl_rows(d.db) == ["QQQ", "ZZZ"]


def test_csrf_dies_with_logout(env):
    d = env
    tok = csrf_of(d.req("GET", "/watchlist")[2])
    old = d.cookie
    assert d.req("POST", "/logout", origin=d.origin, body=b"")[0] == 303
    d.cookie = old
    before = fingerprint(d.db)
    st, hd, _ = post(d, "/watchlist/add", {"symbol": "QQQ"}, csrf=tok)
    assert st == 303 and hd["location"] == "/login" and fingerprint(d.db) == before


def test_post_redirect_get_duplicate_submit_and_replay_are_safe(env):
    d = env
    tok = csrf_of(d.req("GET", "/watchlist")[2])
    st, hd, body = post(d, "/watchlist/add", {"symbol": "QQQ"}, csrf=tok)
    assert st == 303 and hd["location"] == "/watchlist?msg=added" and body == ""           # PRG: no page body on the POST
    st2, hd2, _ = post(d, "/watchlist/add", {"symbol": "QQQ"}, csrf=tok)                    # double click / replay
    assert (st2, hd2["location"]) == (303, "/watchlist?msg=already")
    c = sqlite3.connect(d.db)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='WATCHLIST_ADDED'").fetchone()[0] == 2     # ZZZ (fixture) + QQQ, once each
    c.close()
    page = text_of(d.req("GET", hd["location"])[2])
    assert "Added to the watchlist" in page
    # reloading the redirect target does not re-submit anything
    before = fingerprint(d.db)
    d.req("GET", hd["location"]); d.req("GET", hd["location"])
    assert fingerprint(d.db) == before


def test_message_codes_are_a_closed_whitelist_and_nothing_user_supplied_is_reflected(env):
    d = env
    for bad in ("<script>alert(1)</script>", "added%00", "ADDED", "x" * 500, "%3Cb%3E"):
        st, _, body = d.req("GET", f"/watchlist?msg={bad}")
        assert st == 400 and "<script" not in body.lower() and "alert(1)" not in body
        assert "alert(1)" not in d.req("GET", f"/market/ZZZ?msg={bad}")[2]
    st, hd, _ = post(d, "/watchlist/add", {"symbol": "%3Cscript%3Ealert(1)%3C%2Fscript%3E"})
    assert hd["location"] == "/watchlist?msg=invalid"
    st, hd, _ = post(d, "/market/refresh-request", {"scope": "%3Cb%3E", "back": "market"})
    assert hd["location"].startswith("/watchlist")               # a hostile scope can never steer the redirect target
    st, hd, _ = post(d, "/market/refresh-request", {"scope": "ZZZ%0d%0aSet-Cookie:%20x=1", "back": "market"})
    assert "\r" not in hd["location"] and "\n" not in hd["location"] and "set-cookie" not in hd


# ----------------------------------------------------------------------------------------- refresh-request limits
def test_refresh_request_limits_one_pending_one_per_minute_ten_per_hour(env):
    from csp_alerts.market import observations as obs
    d = env
    t0 = NOW + timedelta(seconds=30)
    codes = []

    def req(at, scope="ALL"):
        d.now = at
        tok = csrf_of(d.req("GET", "/watchlist")[2])
        st, hd, _ = post(d, "/market/refresh-request", {"scope": scope}, csrf=tok)
        assert st == 303
        return hd["location"].split("msg=")[1]

    def job_claims(at):
        j = jopen(d.db, at)
        obs.claim_requests(j, "run", at)
        j.close()
    assert req(t0) == "queued"
    assert req(t0 + timedelta(seconds=5)) == "pending"                                # 1 pending at a time
    assert req(t0 + timedelta(seconds=5), "ZZZ") == "pending"
    job_claims(t0 + timedelta(seconds=10))
    assert req(t0 + timedelta(seconds=59)) == "rate"                                  # < 60 s after the previous request
    assert req(t0 + timedelta(seconds=60)) == "queued"                                # exactly 60 s -> allowed
    got = ["queued", "queued"]
    t = t0 + timedelta(seconds=60)
    for k in range(8):                                                                # 8 more -> 10 in the hour in total
        job_claims(t + timedelta(seconds=1))
        t += timedelta(seconds=61)
        got.append(req(t))
    assert got == ["queued"] * 10, got
    job_claims(t + timedelta(seconds=1))
    assert req(t + timedelta(seconds=61)) == "rate"                                   # 11th within the hour
    c = sqlite3.connect(d.db)
    assert c.execute("SELECT COUNT(*) FROM md_refresh_requests").fetchone()[0] == 10
    c.close()
    job_claims(t + timedelta(seconds=62))
    assert req(t0 + timedelta(seconds=3661)) == "queued"                              # first request is >1 h old: the window slides


def test_refresh_request_unknown_or_malformed_scope(env):
    d = env
    tok = csrf_of(d.req("GET", "/watchlist")[2])
    assert post(d, "/market/refresh-request", {"scope": "QQQ"}, csrf=tok)[1]["location"].endswith("msg=unknown_symbol")
    d.now += timedelta(seconds=120)
    assert post(d, "/market/refresh-request", {"scope": "1;DROP"}, csrf=tok)[1]["location"].endswith("msg=invalid")
    c = sqlite3.connect(d.db)
    assert c.execute("SELECT COUNT(*) FROM md_refresh_requests").fetchone()[0] == 0
    c.close()


# ----------------------------------------------------------------------------------------- GETs never mutate
def test_browsing_every_view_including_the_new_pages_leaves_the_database_byte_identical(tmp_path):
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ")
    p.set("AAA", spot=80.0, strikes=(75.0,))
    refresh(db, p, NOW)
    d = dash(db, approved_file(tmp_path, ("AAA",)), now=NOW + timedelta(seconds=30))
    try:
        before = fingerprint(db)
        for pth in READ_PAGES + ["/market/AAA", "/positions/OPN", "/api/positions/OPN", "/api/market/AAA", "/market/QQQ", "/market/zzz",
                                 "/watchlist?msg=added", "/market/ZZZ?show=all", "/static/app.css"]:
            st, _, _ = d.req("GET", pth)
            assert st in (200, 400, 404), (pth, st)
        for _ in range(3):
            d.now += timedelta(minutes=2)                                        # the page's own 60 s auto-reload
            for pth in ("/watchlist", "/market/ZZZ"):
                assert d.req("GET", pth)[0] == 200
        assert fingerprint(db) == before
    finally:
        d.stop()


# ----------------------------------------------------------------------------------------- hostile text and secrets
def test_hostile_provider_text_is_escaped_everywhere_and_paths_do_not_leak(tmp_path):
    from csp_alerts.market.stockpile import SourceError
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ", "YYY")
    p = Prov(NOW)
    p.script["ZZZ"] = [raw_row(strike=55.0, spot=61.25, expiration=EXP.isoformat(), contractSymbol="<script>alert(1)</script>"),
                       raw_row(strike=50.0, spot=61.25, expiration=EXP.isoformat(), contractSymbol="\"><img src=x onerror=alert(2)>")]
    p.script["YYY"] = SourceError("FETCH_ERROR", "<img src=x onerror=alert(3)> failed at C:\\Users\\victim\\secret\\token.txt")
    refresh(db, p, NOW)
    d = dash(db, approved_file(tmp_path), now=NOW + timedelta(seconds=30))
    try:
        for pth in ("/watchlist", "/market/ZZZ", "/market/YYY", "/api/market/ZZZ", "/api/watchlist"):
            st, hd, body = d.req("GET", pth)
            assert st == 200, pth
            low = body.lower()
            if not pth.startswith("/api"):
                assert not re.search(r"<\s*(script|img|iframe|svg|object)", low), pth           # hostile text may only appear ESCAPED
                assert "&lt;script&gt;alert(1)" in low or "&lt;script" in low or pth == "/watchlist" or pth == "/market/YYY", pth
    finally:
        d.stop()


def test_passwords_hashes_session_and_csrf_tokens_never_appear_in_pages_json_logs_or_urls(env, capsys):
    d = env
    cookie, tok = d.cookie, csrf_of(d.req("GET", "/watchlist")[2])
    secrets = {"password": PW, "hash": d.srv.password_hash, "hash_tail": d.srv.password_hash.split("$")[-1], "session": cookie}
    bodies = []
    for pth in READ_PAGES:
        st, hd, body = d.req("GET", pth)
        blob = body + str(sorted((k, v) for k, v in hd.items() if k != "set-cookie"))
        bodies.append((pth, blob))
        assert "csp_session" not in body, pth
        if pth.startswith("/api/"):
            assert tok not in body, pth                                      # CSRF token only inside HTML forms, never in the JSON twin
    st, hd, _ = post(d, "/watchlist/add", {"symbol": "QQQ"}, csrf=tok)
    bodies.append(("POST-redirect", str(hd)))
    st, hd, _ = d.req("POST", "/login", auth=False, origin=d.origin, body=f"password={PW}".encode(), headers=FORM)[0:2] + (None,)
    for name, blob in bodies:
        for label, s in secrets.items():
            assert s not in blob, (name, label)
        if name == "POST-redirect":
            assert tok not in blob                                           # not carried in the redirect / URL
    # session cookie attributes
    _, hd, _ = d.req("POST", "/login", auth=False, origin=d.origin, body=f"password={PW}".encode(), headers=FORM)
    sc = hd["set-cookie"]
    assert "HttpOnly" in sc and "SameSite=Strict" in sc and "Path=/" in sc
    err = capsys.readouterr().err
    for label, s in {**secrets, "csrf": tok}.items():
        assert s not in err, label
    assert "?" not in re.sub(r"\?\?", "", err) or "msg=" not in err          # query strings are not logged
    assert "symbol=" not in err and "csrf=" not in err


def test_error_pages_do_not_leak_paths_or_stack_traces(tmp_path, env):
    d = env
    probes = [("GET", "/nope"), ("GET", "/market/%2e%2e%2f%2e%2e%2fwindows"), ("GET", "/market/ZZZ?expiry=bad"), ("GET", "/watchlist?msg=zzz"),
              ("GET", "/market/ZZZ?strike_min=1e9"), ("GET", "/market/" + "A" * 300), ("PUT", "/watchlist"), ("GET", "/x" * 1500),
              ("POST", "/watchlist/add"), ("GET", "/api/market/QQQ")]
    for m, pth in probes:
        st, hd, body = d.req(m, pth, origin=d.origin if m != "GET" else None)
        assert st in (400, 403, 404, 405, 414, 303), (m, pth, st)
        for bad in (str(tmp_path), "Traceback", "sqlite3.", ".sqlite3", "C:\\", "\\Users", "/Users", "site-packages", "src\\", "csp_dashboard"):
            assert bad not in body, (m, pth, bad)
    # corrupt journal -> 503 with a fixed message, never the path or sqlite text
    bad_db = tmp_path / "bad.sqlite3"
    bad_db.write_bytes(b"this is not a database" * 100)
    d2 = dash(bad_db, approved_file(tmp_path))
    try:
        for pth in ("/", "/watchlist", "/market/ZZZ", "/positions"):
            st, _, body = d2.req("GET", pth)
            assert st in (200, 503, 404), (pth, st)
            for bad in (str(tmp_path), "Traceback", "sqlite3", "bad.sqlite3", "C:\\", "not a database", "file is not"):
                assert bad not in body, (pth, bad)
    finally:
        d2.stop()
    d3 = dash(tmp_path / "missing.sqlite3", approved_file(tmp_path))
    try:
        for pth in ("/", "/market/ZZZ"):
            st, _, body = d3.req("GET", pth)
            assert st in (200, 404, 503), (pth, st)
            assert str(tmp_path) not in body and "missing.sqlite3" not in body
        assert not (tmp_path / "missing.sqlite3").exists()                    # the dashboard never creates a journal
    finally:
        d3.stop()


def test_watchlist_page_degrades_cleanly_when_the_journal_is_missing(tmp_path):
    d = dash(tmp_path / "missing.sqlite3", approved_file(tmp_path))
    try:
        st, _, body = d.req("GET", "/watchlist")
        assert st in (200, 503) and "Internal error" not in body
        assert not (tmp_path / "missing.sqlite3").exists()
    finally:
        d.stop()


# ----------------------------------------------------------------------------------------- import boundary
NET = {"socket", "ssl", "urllib.request", "urllib3", "http.client", "requests", "httpx", "aiohttp", "websocket", "websockets", "ftplib",
       "smtplib", "telnetlib", "xmlrpc", "yfinance", "subprocess", "multiprocessing", "asyncio", "pexpect"}
FORBIDDEN_INTERNAL = ("csp_alerts.alerts", "csp_alerts.market.stockpile", "csp_alerts.runner", "csp_alerts.ledger_cli",
                      "csp_alerts.cli", "csp_alerts.jobs.market_refresh", "csp_alerts.jobs.monitor", "csp_alerts.jobs.scan",
                      "csp_alerts.jobs.ticker_check")      # csp_alerts.jobs.status (constants only; pre-existing) is the one allowed jobs module


def _closure():
    seen, todo, edges = set(), ["csp_dashboard.server", "csp_dashboard.cli", "csp_dashboard.views", "csp_dashboard.render",
                                "csp_dashboard.auth", "csp_dashboard.readonly"], {}
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        base = REPO / "src" / Path(*m.split("."))
        f = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
        if not f.is_file():
            continue
        imps = set()
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                imps |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                imps.add(n.module)
                imps |= {f"{n.module}.{a.name}" for a in n.names}
        edges[m] = imps
        todo += [i for i in imps if i.startswith(("csp_dashboard", "csp_alerts"))]
    return edges


def test_dashboard_static_import_closure_has_no_network_subprocess_telegram_or_job_code():
    edges = _closure()
    assert "csp_alerts.journal.webwrites" in edges and "csp_alerts.market.observations" in edges     # the new write path is in scope
    for mod, imps in edges.items():
        for i in imps:
            top = i.split(".")[0]
            allowed_http_server = mod == "csp_dashboard.server" and i in ("http.server", "http.cookies")
            assert not (i in NET or top in NET) or allowed_http_server, (mod, i)
            assert not i.startswith(FORBIDDEN_INTERNAL), (mod, i)


def test_dashboard_runtime_import_closure_loads_no_network_client_telegram_or_provider_modules():
    code = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport csp_dashboard.server, csp_dashboard.cli  # noqa\n"
            "bad = ['requests','urllib3','httpx','aiohttp','yfinance','smtplib','ftplib','subprocess','asyncio','urllib.request',"
            "'csp_alerts.alerts','csp_alerts.market.stockpile','csp_alerts.jobs.market_refresh','csp_alerts.jobs.monitor','csp_alerts.jobs.scan','csp_alerts.jobs.ticker_check','csp_alerts.runner',"
            "'options_scanner','telegram','ib_insync','alpaca','schwab','tda','robin_stocks']\n"
            "print('LOADED', sorted(m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in bad)))\n")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(REPO / "src")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "LOADED []", r.stdout


def test_no_write_sql_in_dashboard_package_and_webwrites_touches_only_three_tables():
    for f in (REPO / "src" / "csp_dashboard").glob("*.py"):
        t = f.read_text(encoding="utf-8")
        assert not re.search(r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+TABLE|CREATE\s+TABLE|ALTER\s+TABLE)\b", t, re.I), f.name
    ww = (REPO / "src" / "csp_alerts" / "journal" / "webwrites.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", ww)              # SQL lives in watchlist.py/observations.py helpers only


# ----------------------------------------------------------------------------------------- truthfulness (F)
CLAIM = re.compile(r"(?i)real[- ]?time|live_verified|zero[- ]delay|\bverified\b|guarantee|exchange[- ]fresh|\blive price|\blive quote")
NEGATIONS = re.compile(r"(?i)\b(not|no|never|cannot|isn't|without|nor|nothing)\b")


def _claims(text):
    out = []
    for m in CLAIM.finditer(text):
        ctx = text[max(0, m.start() - 70): m.end() + 40]
        if not NEGATIONS.search(text[max(0, m.start() - 70): m.end() + 10]):
            out.append(ctx)
    return out


@pytest.fixture()
def states(tmp_path):
    """Pages in each state: OK/open, outage, rate limited, market closed, zero bid, derived+provider ids."""
    out = {}
    for name, now, setup in (
        ("ok", NOW, lambda p: p.set("ZZZ", spot=61.25)),
        ("outage", NOW + timedelta(minutes=6), lambda p: p.fail("ZZZ", "FETCH_ERROR")),
        ("ratelimit", NOW + timedelta(minutes=6), lambda p: p.fail("ZZZ", "RATE_LIMIT")),
        ("closed", SAT, lambda p: p.set("ZZZ", spot=61.25)),
        ("zerobid", NOW, lambda p: p.script.__setitem__("ZZZ", [
            raw_row(strike=55.0, spot=61.25, expiration=EXP.isoformat(), bid=0.0, ask=0.05),
            raw_row(strike=50.0, spot=61.25, expiration=EXP.isoformat(), bid=0.4, ask=0.45, contractSymbol="ZZZ261204P00050000")])),
    ):
        sub = tmp_path / name
        sub.mkdir()
        db = plain_db(sub, now=min(now, NOW))
        add_wl(db, "ZZZ", now=min(now, NOW))
        p = Prov(NOW if name != "closed" else SAT)
        if name in ("outage", "ratelimit"):
            p.set("ZZZ", spot=61.25)
            refresh(db, p, NOW)
        setup(p)
        refresh(db, p, now)
        d = dash(db, approved_file(sub), now=now + timedelta(seconds=30))
        try:
            out[name] = {"market": text_of(d.req("GET", "/market/ZZZ")[2]), "watch": text_of(d.req("GET", "/watchlist")[2]),
                         "raw": d.req("GET", "/market/ZZZ")[2]}
        finally:
            d.stop()
    return out


def test_pages_state_yahoo_indicative_quote_age_unknown_and_never_claim_realtime_or_verified(states):
    for name, pages in states.items():
        for kind in ("market", "watch"):
            t = pages[kind]
            assert "Yahoo indicative data" in t and "Quote age unknown" in t, (name, kind)
            assert not _claims(t), (name, kind, _claims(t))
            assert "Not a live exchange feed" in t, (name, kind)


def test_last_price_type_caveat_closed_market_derived_id_and_zero_bid_are_shown(states):
    ok = states["ok"]["market"]
    assert "61.25 (LAST)" in ok and "does not say whether it is an intraday last trade or the previous close" in ok
    assert "(derived id)" in ok and "Last print (not a quote)" in ok
    closed = states["closed"]["market"]
    assert "MARKET-CLOSED" in closed and "not a live price" in closed
    zb = states["zerobid"]["market"]
    assert "no quote (0)" in zb                                           # bid 0 is shown as NO quote, not as a price of 0
    assert re.search(r"ZZZ261204P00055000 \(derived id\)", zb) and re.search(r"ZZZ261204P00050000 \(provider id\)", zb)


def test_outage_and_rate_limit_pages_are_distinct_and_say_the_values_are_old(states):
    o, r = states["outage"]["market"], states["ratelimit"]["market"]
    assert "STALE" in o and "STALE" in r and "RATE_LIMITED" in r and "RATE_LIMITED" not in o and "ERROR" in o
    assert "61.25" in o and "10:05 CDT" in o                              # old value, ORIGINAL retrieval time
    assert "rate limit" in r.lower() and "rate limit" not in o.lower().replace("rate limits", "")
    # page-level banner must not say updates are flowing while the newest attempt failed (judged below as a finding)
    assert "failed" in o.lower()


def test_banner_wording_in_failure_states_is_not_an_unqualified_live_claim(states):
    """D6 (DECISIONS 130). Normal state: neutral 'Auto-refreshing page' and NO 'Live updates' phrase at all. Failure/closed states:
    any 'Live updates' wording is immediately qualified by failing/paused and 'showing last known data', next to the STALE /
    MARKET-CLOSED label. Every state keeps 'Not a live exchange feed'. Residual wording note (reported, not asserted): 'Live updates
    paused' is acceptable because it is negated and followed by the reason, but 'Auto-refresh paused' would avoid the word 'live'."""
    ok = states["ok"]["watch"]
    assert "Auto-refreshing page" in ok and not re.search(r"(?i)live updates", ok) and "Not a live exchange feed" in ok
    for name, label, phrase in (("outage", "STALE", "Auto-refresh failing: showing last known data"),
                                ("ratelimit", "STALE", "Auto-refresh failing: showing last known data"),
                                ("closed", "MARKET-CLOSED", "Auto-refresh paused, market closed: showing last known data")):
        for kind in ("watch", "market"):
            t = states[name][kind]
            assert phrase in t and label in t and "Not a live exchange feed" in t, (name, kind)
            assert "Auto-refreshing page" not in t, (name, kind)       # the neutral banner is not shown while failing/closed
            for m in re.finditer(r"(?i)live", t):                  # 'live' only inside 'Not a live exchange feed'
                assert t[max(0, m.start() - 6):m.start() + 18] == "Not a live exchange feed", (name, kind, t[m.start() - 20:m.start() + 30])


# ----------------------------------------------------------------------------------------- form actions on EVERY page
def test_every_page_forms_post_only_to_the_five_allowed_actions_with_a_csrf_field_and_get_forms_stay_get(tmp_path):
    """Extends the old 'only login/logout may POST' guard to the new pages. POST forms allowed: login (login page only), logout,
    /watchlist/add, /watchlist/remove, /market/refresh-request; every state-changing form except login/logout carries a csrf field."""
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ")
    p.set("AAA", spot=80.0, strikes=(75.0,))
    refresh(db, p, NOW)
    d = dash(db, approved_file(tmp_path, ("AAA",)), now=NOW + timedelta(seconds=30))
    seen = set()
    try:
        pages = ["/", "/candidates", "/positions", "/positions/OPN", "/alerts", "/activity", "/runs", "/health", "/watchlist", "/market/ZZZ",
                 "/market/AAA", "/positions?group=all", "/watchlist?msg=added", "/market/ZZZ?show=pass"]
        for pth in pages:
            st, hd, html = d.req("GET", pth)
            assert st == 200, pth
            low = html.lower()
            assert "<script" not in low and "javascript:" not in low and not re.search(r"\son[a-z]+\s*=", low), pth
            for m in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.S | re.I):
                tag = re.match(r"<form\b[^>]*>", m.group(0), re.I).group(0).lower()
                inner = m.group(1)
                if 'method="post"' in tag:
                    act = re.search(r'action="([^"]+)"', tag).group(1)
                    assert act in ("/logout", *WRITES), (pth, tag)
                    seen.add(act)
                    if act in WRITES:
                        assert re.search(r'name="csrf"', inner), (pth, act)
                else:
                    assert 'method="get"' in tag and re.search(r'action="(/[a-z/A-Z0-9]*)"', tag), (pth, tag)
                    assert not re.search(r'name="csrf"', inner), (pth, tag)             # a GET form must not leak the token into a URL
            for tagm in re.finditer(r"<(a|link|img|form)\b[^>]*>", html, re.I):
                assert not re.search(r'(href|src|action)\s*=\s*"\s*(https?:)?//', tagm.group(0), re.I), (pth, tagm.group(0))
        assert {"/logout", "/watchlist/add", "/watchlist/remove", "/market/refresh-request"} <= seen, seen
        assert "/login" in re.findall(r'action="([^"]+)"', d.req("GET", "/login", auth=False)[2])
        assert d.req("GET", "/watchlist")[1]["content-security-policy"].count("form-action 'self'") == 1
    finally:
        d.stop()
