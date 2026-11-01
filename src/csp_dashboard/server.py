"""Loopback-only HTTP server for the local dashboard (stdlib http.server, server-rendered HTML, no JavaScript).

Guarantees (each covered by tests/unit/test_dashboard_*.py):
  * binds 127.0.0.1 only; Host must be exactly 127.0.0.1:<port> or localhost:<port> (DNS-rebinding guard);
  * Origin (when sent) must be the same origin; every POST REQUIRES it; no CORS headers, ever;
  * every view is GET and GETs never write. The ONLY POST routes are /login, /logout, the three Stage A writes
    (WRITE_PATHS: watchlist add/remove, refresh request) and the four Stage B1 recording routes (TRADE_WRITE_PATHS:
    /trade/preview, /trade/confirm, /checkin/preview, /checkin/confirm). All need a valid session AND a per-session CSRF
    token; the Stage A writes redirect (POST-redirect-GET) and are idempotent. A preview POST writes nothing. A confirm POST
    also needs a signed preview token (binds session + exact fields + ledger revision + policy hash, 20 min) and an
    idempotency key, and records through the one small write module csp_alerts.journal.webwrites (no network I/O here:
    a refresh request is only a row that a separate job picks up);
  * no route accepts a path, SQL, file name or DB location from the request;
  * every response carries no-store + CSP default-src 'none' (only the same-origin stylesheet is allowed);
  * the password never appears in a URL, log line, page, or the process environment dump (only a scrypt hash is configured).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from csp_alerts.journal import weblabels, webtrade, webwrites
from csp_alerts.policy.approved import asset_type_map, load_approved
from csp_alerts.policy.config import load_policy

from csp_dashboard import auth, render, views
from csp_dashboard.readonly import JournalMissing, ReadOnlyJournal

LOOPBACK = "127.0.0.1"
COOKIE = "csp_session"
MAX_URL = 2000
MAX_BODY = 2048
_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,80}$")
_TICKER = re.compile(r"^[A-Z0-9.\-]{1,10}$")
_TYPE = re.compile(r"^[A-Z_]{1,40}$")
_CURSOR = re.compile(r"^[0-9]{1,18}$")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")      # ASCII digits only (\d accepts any Unicode digit)
_NUM = re.compile(r"[0-9]{1,7}(\.[0-9]{1,4})?\Z")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
WRITE_PATHS = frozenset({"/watchlist/add", "/watchlist/remove", "/market/refresh-request"})
# Stage B1: owner-reported trade recording and web check-in. Kept apart from WRITE_PATHS (the Stage A set); ALL_WRITE_PATHS is
# the complete list of write routes besides login/logout.
TRADE_WRITE_PATHS = frozenset({"/trade/preview", "/trade/confirm", "/checkin/preview", "/checkin/confirm"})
# Stage B2: owner-confirmed sector / correlation-bucket label correction (never approvals, never asset type).
LABEL_WRITE_PATHS = frozenset({"/portfolio/labels/preview", "/portfolio/labels/confirm"})
ALL_WRITE_PATHS = WRITE_PATHS | TRADE_WRITE_PATHS | LABEL_WRITE_PATHS
MAX_FORM_FIELDS = 6
MAX_TRADE_BODY = 6144
MAX_TRADE_FIELDS = 70
_FIELD = re.compile(r"^[a-z][a-z_0-9]{0,23}$")
_SLUG_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,60}$")
SECURITY_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


class BadRequest(Exception):
    pass


def _opt(params: dict[str, list[str]], key: str, rx: re.Pattern | None = None, allowed: set[str] | frozenset[str] | None = None) -> str | None:
    vals = params.get(key)
    if not vals or vals[0] == "":
        return None
    v = vals[0]
    if (rx is not None and not rx.match(v)) or (allowed is not None and v not in allowed):
        raise BadRequest(key)
    return v


def _paging(p) -> dict[str, str | None]:
    return {"before": _opt(p, "before", _CURSOR), "after": _opt(p, "after", _CURSOR)}


# route name -> (builder(env, params, ident) -> data | None, renderer(data, meta, tz, params))
def _routes() -> dict[str, tuple[Callable, Callable]]:
    V, R = views, render
    return {
        "overview": (lambda e, p, i: V.overview(e), lambda d, m, tz, p: R.page_overview(d, m, tz)),
        "watchlist": (lambda e, p, i: V.watchlist(e, msg=_opt(p, "msg", allowed=V.MSG_CODES)), lambda d, m, tz, p: R.page_watchlist(d, m, tz)),
        "market": (lambda e, p, i: V.market_detail(
            e, i if i and _SYMBOL.match(i) else "?", expiry=_opt(p, "expiry", _DATE), strike_min=_opt(p, "strike_min", _NUM),
            strike_max=_opt(p, "strike_max", _NUM), show=_opt(p, "show", allowed={"all", "pass"}), msg=_opt(p, "msg", allowed=V.MSG_CODES))
            if i and _SYMBOL.match(i) else None, lambda d, m, tz, p: R.page_market(d, m, tz, p)),
        "candidates": (lambda e, p, i: V.candidates(e, run=_opt(p, "run", _ID), bucket=_opt(p, "bucket", allowed={"pass", "stale", "rejected", "data"}) or "pass",
                                                    ticker=_opt(p, "ticker", _TICKER), **_paging(p)),
                       lambda d, m, tz, p: R.page_candidates(d, m, tz, p)),
        "candidate": (lambda e, p, i: V.candidate_detail(e, i), lambda d, m, tz, p: R.page_candidate_detail(d, m, tz)),
        "positions": (lambda e, p, i: V.positions(e, group=_opt(p, "group", allowed=set(V.GROUPS)) or "live",
                                                  ticker=_opt(p, "ticker", _TICKER), **_paging(p)),
                      lambda d, m, tz, p: R.page_positions(d, m, tz)),
        "position": (lambda e, p, i: V.position_detail(e, i, **_paging(p)), lambda d, m, tz, p: R.page_position_detail(d, m, tz)),
        "alerts": (lambda e, p, i: V.alerts(e, status=_opt(p, "status", allowed=set(V.OUTBOX_LABEL)), severity=_opt(p, "severity", _TYPE),
                                            klass=_opt(p, "type", _TYPE), **_paging(p)), lambda d, m, tz, p: R.page_alerts(d, m, tz)),
        "alert": (lambda e, p, i: V.alert_detail(e, i), lambda d, m, tz, p: R.page_alert_detail(d, m, tz)),
        "activity": (lambda e, p, i: V.activity(e, event_type=_opt(p, "event_type", _TYPE), **_paging(p)),
                     lambda d, m, tz, p: R.page_activity(d, m, tz)),
        "event": (lambda e, p, i: V.activity_detail(e, i), lambda d, m, tz, p: R.page_activity_detail(d, m, tz)),
        "runs": (lambda e, p, i: V.runs(e, kind=_opt(p, "kind", allowed={"SCAN", "MONITOR", "HEARTBEAT", "RECONCILE"}), **_paging(p)),
                 lambda d, m, tz, p: R.page_runs(d, m, tz)),
        "run": (lambda e, p, i: V.run_detail(e, i), lambda d, m, tz, p: R.page_run_detail(d, m, tz)),
        "health": (lambda e, p, i: V.health(e), lambda d, m, tz, p: R.page_health(d, m, tz)),
        "trade_sell": (lambda e, p, i: V.trade_sell_form(e, _opt(p, "symbol", _SYMBOL) or "?", _opt(p, "expiry", _DATE), _opt(p, "strike", _NUM),
                                                          _opt(p, "kind", allowed={"filled", "working"})),
                       lambda d, m, tz, p: R.page_trade_form(d, m, tz)),
        "checkin": (lambda e, p, i: V.checkin_form(e), lambda d, m, tz, p: R.page_checkin(d, m, tz)),
        "trade_result": (lambda e, p, i: V.trade_result(e, _opt(p, "code", allowed=V.RESULT_CODES) or "bad_request",
                                                        _opt(p, "trade", _ID), _opt(p, "a", allowed=frozenset(webtrade.ACTIONS))),
                         lambda d, m, tz, p: R.page_trade_result(d, m, tz)),
        "portfolio": (lambda e, p, i: V.portfolio(e), lambda d, m, tz, p: R.page_portfolio(d, m, tz)),
        "label_form": (lambda e, p, i: V.label_form(e, i) if i and _SYMBOL.match(i) else None, lambda d, m, tz, p: R.page_label_form(d, m, tz)),
        "label_result": (lambda e, p, i: V.label_result(e, _opt(p, "code", allowed=V.LABEL_RESULT_CODES) or "bad_request",
                                                        _opt(p, "ticker", _SYMBOL)), lambda d, m, tz, p: R.page_label_result(d, m, tz)),
        **{f"trade:{slug}": ((lambda e, p, i, slug=slug: V.trade_position_form(e, slug, i)), lambda d, m, tz, p: R.page_trade_form(d, m, tz))
           for slug in V.ACT_SLUGS},
    }


V_SLUGS = tuple(views.ACT_SLUGS)
ROUTES = _routes()
PATHS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^/$"), "overview"), (re.compile(r"^/watchlist$"), "watchlist"), (re.compile(r"^/market/([^/]+)$"), "market"),
    (re.compile(r"^/candidates$"), "candidates"), (re.compile(r"^/candidates/([^/]+)$"), "candidate"),
    (re.compile(r"^/positions$"), "positions"), (re.compile(r"^/positions/([^/]+)$"), "position"),
    (re.compile(r"^/alerts$"), "alerts"), (re.compile(r"^/alerts/([^/]+)$"), "alert"),
    (re.compile(r"^/activity$"), "activity"), (re.compile(r"^/activity/([^/]+)$"), "event"),
    (re.compile(r"^/runs$"), "runs"), (re.compile(r"^/runs/([^/]+)$"), "run"), (re.compile(r"^/health$"), "health"),
    (re.compile(r"^/portfolio$"), "portfolio"), (re.compile(r"^/portfolio/label-result$"), "label_result"),
    (re.compile(r"^/portfolio/labels/([^/]+)$"), "label_form"),
    (re.compile(r"^/trade/sell$"), "trade_sell"), (re.compile(r"^/checkin$"), "checkin"), (re.compile(r"^/trade/result$"), "trade_result"),
    *[(re.compile(rf"^/trade/{slug}/([^/]+)$"), f"trade:{slug}") for slug in V_SLUGS],
]
TITLES = {"portfolio": "Portfolio", "checkin": "Account check-in", "overview": "Overview", "candidates": "Candidates and reasons", "positions": "Positions", "alerts": "Alerts",
          "activity": "Activity", "runs": "Run history", "health": "Health and reconciliation"}
JSON_PREFIX = "/api"
DETAIL = {"label_form", "candidate", "position", "alert", "event", "run", "market", *[f"trade:{x}" for x in V_SLUGS]}


def _safe_err(exc: Exception, *paths: Any) -> str:
    """Error text for the page: never a file-system path (read failures are reduced to a fixed phrase)."""
    s = str(exc)
    if "cannot read" in s or isinstance(exc, OSError):
        return "the file cannot be read (missing or unreadable)"
    for p in paths:
        s = s.replace(str(p), "<file>")
    return re.sub(r"[A-Za-z]:[\/][^\s'\"]*", "<path>", s)[:300]


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, sources: views.Sources, password_hash: str, port: int = 8765, host: str = LOOPBACK):
        if host != LOOPBACK:
            raise ValueError("the dashboard binds to 127.0.0.1 only")
        super().__init__((host, port), Handler)
        self.sources = sources
        self.password_hash = password_hash
        self.sessions = auth.Sessions()
        p = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    # ---- data access: one fresh read-only connection + one read transaction per request
    def load_view(self, name: str, params: dict, ident: str | None) -> tuple[dict | None, dict]:
        return self.with_env(lambda env: ROUTES[name][0](env, params, ident), name in DETAIL, name)

    def with_env(self, fn: Callable, detail: bool = False, name: str = "x") -> tuple[Any, dict]:
        """Run `fn(env)` inside one read-only snapshot (policy and approvals loaded fresh) and return (result, meta)."""
        src = self.sources
        now = src.clock()
        try:
            rj = ReadOnlyJournal(src.db_path)
        except JournalMissing:
            att = {"items": [{"code": "JOURNAL_MISSING", "text": "No journal database exists yet.", "severity": "BLOCK"}],
                   "steps": ["Run `csp journal init` (the dashboard never creates or migrates the journal)."], "ok": False}
            m = {"rendered_at_utc": now, "display_timezone": src.tz, "schema_version": None, "schema_expected": views.LATEST_SCHEMA,
                 "code_revision": views.code_identity().label, "observed": {}}
            return (None if detail else {"view": name, "attention": att}), m
        try:
            with rj.snapshot():
                policy, perr = None, None
                try:
                    policy = load_policy(src.policy_path)
                except Exception as exc:  # noqa: BLE001
                    perr = _safe_err(exc, src.policy_path)
                approved, aerr = None, None
                try:
                    approved = load_approved(src.approved_path)
                except Exception as exc:  # noqa: BLE001
                    aerr = _safe_err(exc, src.approved_path)
                rj.approval_file_types = asset_type_map(approved)      # read-only fallback for the asset type
                env = views.Env(rj, now, src.tz, policy, perr, approved, aerr)
                m = views.meta(env)
                data = fn(env)
                return data, m
        finally:
            rj.close()


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "csp-dashboard"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 15
    _body: bytes = b""

    # ---- logging: method, path without query, status only. Never headers, cookies, bodies or query strings.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        path = re.sub(r"[^A-Za-z0-9/._:\-]", "?", self.path.split("?", 1)[0])[:120]
        sys.stderr.write(f"dashboard {self.command} {path} {code}\n")

    # ---- plumbing
    def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", extra: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _text(self, code: int, text: str) -> None:
        self._send(code, render.message_page(f"Error {code}", text))

    def _redirect(self, to: str, cookie: str | None = None) -> None:
        extra = {"Location": to}
        if cookie:
            extra["Set-Cookie"] = cookie
        self._send(303, "", extra=extra)

    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
            return c[COOKIE].value if COOKIE in c else None
        except Exception:  # noqa: BLE001
            return None

    def _authed(self) -> bool:
        return self.server.sessions.valid(self._session_token())

    def _boundary_ok(self, method: str) -> bool:
        s = self.server
        host = (self.headers.get("Host") or "").lower()
        if host not in s.allowed_hosts:
            self._text(421, "Unexpected Host header.")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in s.allowed_origins:
            self._text(403, "Cross-origin request refused.")
            return False
        if method == "POST" and origin is None:
            self._text(403, "Origin header required.")
            return False
        if (self.headers.get("Sec-Fetch-Site") or "same-origin") not in ("same-origin", "none"):
            self._text(403, "Cross-site request refused.")
            return False
        return True

    # ---- verbs
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _not_allowed(self) -> None:
        self._send(405, render.message_page("Error 405", "Method not allowed."), extra={"Allow": "GET, POST"})

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _read_body(self) -> bytes:
        """Read (and bound) the request body up front so an early refusal never leaves unread bytes (which resets the connection)."""
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        limit = MAX_TRADE_BODY if self.path.split("?", 1)[0] in TRADE_WRITE_PATHS | LABEL_WRITE_PATHS else MAX_BODY
        if n < 0 or n > limit:
            self.close_connection = True
            return b"x" * (limit + 1)              # marker: oversized or malformed; callers treat it as a bad request
        return self.rfile.read(n) if n else b""

    def _handle(self, method: str) -> None:
        try:
            if len(self.path) > MAX_URL:
                return self._text(414, "URL too long.")
            self._body = self._read_body()
            if not self._boundary_ok(method):
                return
            if method not in ("GET", "POST"):
                return self._not_allowed()
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/static/app.css" and method == "GET":
                return self._send(200, render.CSS, "text/css; charset=utf-8", {"Cache-Control": "max-age=300"})
            if path == "/login":
                return self._login(method)
            if path == "/logout":
                if method != "POST":
                    return self._not_allowed()
                self.server.sessions.drop(self._session_token())
                return self._redirect("/login", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            as_json = path.startswith(JSON_PREFIX + "/")
            if as_json and method == "POST":
                return self._not_allowed()     # the JSON mirror is read-only: no write route has an /api alias
            if as_json:
                path = "/" if path == JSON_PREFIX + "/overview" else path[len(JSON_PREFIX):]
            if not self._authed():
                if as_json:
                    return self._send(401, json.dumps({"error": "authentication required"}), "application/json")
                return self._redirect("/login")
            if method == "POST" and path in WRITE_PATHS:
                return self._write(path)
            if method == "POST" and path in TRADE_WRITE_PATHS:
                return self._trade_write(path)
            if method == "POST" and path in LABEL_WRITE_PATHS:
                return self._label_write(path)
            if method != "GET":
                return self._not_allowed()
            for rx, name in PATHS:
                mt = rx.match(path)
                if mt:
                    ident = mt.group(1) if mt.groups() else None
                    if ident is not None and not _ID.match(ident):
                        return self._text(404, "Not found.")
                    try:
                        query = parse_qs(parts.query, max_num_fields=12)
                    except ValueError:
                        raise BadRequest from None
                    return self._view(name, query, ident, as_json, path)
            return self._text(404, "Not found.")
        except BadRequest:
            self._text(400, "Invalid filter value.")
        except sqlite3.Error:
            self._text(503, "The journal could not be read right now (locked or unreadable). Try Refresh.")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"dashboard internal error: {type(exc).__name__}\n")
            self._text(500, "Internal error.")

    def _login(self, method: str) -> None:
        srv = self.server
        if method == "GET":
            return self._send(200, render.login_page())
        if method != "POST":
            return self._not_allowed()
        if srv.sessions.locked_out():
            return self._send(429, render.login_page("Too many failed attempts. Wait a minute."), extra={"Retry-After": "60"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (0 < len(self._body) <= MAX_BODY) or ctype != "application/x-www-form-urlencoded":
            return self._text(400, "Bad login request.")
        form = parse_qs(self._body.decode("utf-8", "replace"), max_num_fields=4)
        pw = (form.get("password") or [""])[0]
        if pw and auth.verify(pw, srv.password_hash):
            tok = srv.sessions.create()
            return self._redirect("/", f"{COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={auth.SESSION_SECONDS}")
        srv.sessions.record_failure()
        self._send(401, render.login_page("Sign-in failed."))

    def _write(self, path: str) -> None:
        """Stage A writes. Reached only after Host/Origin checks and authentication. Every outcome is a 303 redirect with a
        fixed message CODE (never reflected text); the HTML form is the only client, so JSON is not offered."""
        srv = self.server
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (0 < len(self._body) <= MAX_BODY) or ctype != "application/x-www-form-urlencoded":
            return self._text(400, "Bad request.")
        try:
            form = parse_qs(self._body.decode("utf-8", "strict"), max_num_fields=MAX_FORM_FIELDS, keep_blank_values=True)
        except (ValueError, UnicodeDecodeError):
            return self._text(400, "Bad request.")

        def one(k: str) -> str:
            v = form.get(k) or [""]
            return v[0]
        back = "/watchlist"
        if path == "/market/refresh-request" and one("back") == "market" and _SYMBOL.match(one("scope")):
            back = f"/market/{one('scope')}"
        if not srv.sessions.csrf_ok(self._session_token(), one("csrf")):
            return self._redirect(f"{back}?msg=csrf")                # nothing written
        db, clk = srv.sources.db_path, srv.sources.clock
        if path == "/watchlist/add":
            res = webwrites.add_watchlist_symbol(db, one("symbol"), clock=clk)
        elif path == "/watchlist/remove":
            res = webwrites.remove_watchlist_symbol(db, one("symbol"), clock=clk)
        else:
            res = webwrites.request_market_refresh(db, one("scope") or "ALL", clock=clk)
        self._redirect(f"{back}?msg={res.code}")

    # ---- Stage B1: trade recording and check-in (preview writes nothing; confirm needs a signed preview token)
    def _trade_write(self, path: str) -> None:
        srv = self.server
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (0 < len(self._body) <= MAX_TRADE_BODY) or ctype != "application/x-www-form-urlencoded":
            return self._text(400, "Bad request.")
        try:
            form = parse_qs(self._body.decode("utf-8", "strict"), max_num_fields=MAX_TRADE_FIELDS, keep_blank_values=True)
        except (ValueError, UnicodeDecodeError):
            return self._text(400, "Bad request.")
        flat = {k: v[0] for k, v in form.items() if _FIELD.match(k) and v}
        sess = self._session_token()
        if not srv.sessions.csrf_ok(sess, flat.get("csrf")):
            return self._redirect("/trade/result?code=csrf")                  # nothing written
        if path.endswith("/preview"):
            return self._trade_preview(flat, sess, path.startswith("/checkin"))
        return self._trade_confirm(flat, sess)

    def _back_for(self, fields: dict[str, str]) -> str:
        from urllib.parse import urlencode
        act, tid = fields.get("action", ""), fields.get("trade_id", "")
        if act == webtrade.CHECKIN:
            return "/checkin"
        if act in webtrade.NEW_POSITION_ACTIONS:
            q = {"symbol": fields.get("symbol", ""), "expiry": fields.get("expiry", ""), "strike": fields.get("strike", ""),
                 "kind": "working" if act == webtrade.SELL_WORKING else "filled"}
            return "/trade/sell?" + urlencode(q)
        slug = views.SLUG_OF.get(act)
        return f"/trade/{slug}/{tid}" if slug and _SLUG_ID.match(tid) else "/positions"

    def _trade_preview(self, flat: dict[str, str], sess: str | None, is_checkin: bool) -> None:
        """Validate and describe; WRITES NOTHING. Re-renders the form with fixed-phrase errors when invalid."""
        srv = self.server
        fields = {k: v for k, v in flat.items() if k != "csrf"}
        fields["idem"] = uuid.uuid4().hex
        if is_checkin != (fields.get("action") == webtrade.CHECKIN):
            return self._text(400, "Bad request.")
        box: dict[str, Any] = {}

        def work(env):
            plan = webtrade.build_plan(env.rj, env.policy, env.approved, fields, env.now, env.tz) if env.policy is not None else None
            box["plan"] = plan
            if plan is None:
                return views.checkin_form(env) if is_checkin else views.trade_sell_form(env, "?", None, None, None)
            if plan.ok:
                return views.trade_preview(env, plan, self._back_for(plan.fields))
            errs = list(plan.errors) or ([webtrade.UNSUPPORTED_STATE] if plan.unsupported else ["Bad request."])
            act = fields.get("action", "")
            if is_checkin:
                return views.checkin_form(env, fields, errs)
            if act in webtrade.NEW_POSITION_ACTIONS:
                return views.trade_sell_form(env, fields.get("symbol", "?").upper(), fields.get("expiry"), fields.get("strike"),
                                             "working" if act == webtrade.SELL_WORKING else "filled", fields, errs)
            slug = views.SLUG_OF.get(act)
            d = views.trade_position_form(env, slug, fields.get("trade_id", ""), fields, errs) if slug else None
            return d if d is not None else views.trade_sell_form(env, "?", None, None, None)
        data, m = srv.with_env(work)
        plan = box.get("plan")
        m = {**m, "_csrf": srv.sessions.csrf(sess)}
        if plan is not None and plan.ok:
            data["token"] = srv.sessions.preview_token(sess, plan.fields, plan.ledger_revision, plan.policy_sha)
            data["confirm_path"] = "/checkin/confirm" if is_checkin else "/trade/confirm"
            return self._send(200, render.page_trade_preview(data, m, srv.sources.tz))
        page = render.page_checkin if data.get("view") == "checkin" else render.page_trade_form
        self._send(200, page(data, m, srv.sources.tz))

    def _trade_confirm(self, flat: dict[str, str], sess: str | None) -> None:
        """The only trade-recording write. Session + CSRF are already checked; here: signed preview token over the exact
        fields, then webwrites.record_web_trade (idempotent, stale-checked, one transaction)."""
        from urllib.parse import urlencode
        srv = self.server
        special = {"csrf", "tok", "rev", "psha", "reason"}
        fields = {k: v for k, v in flat.items() if k not in special and not k.startswith("ack_")}
        acks = frozenset(k for k, v in flat.items() if k.startswith("ack_") and v == "1")
        rev = flat.get("rev", "")

        def done(code: str, trade_id: str | None = None) -> None:
            q = {"code": code}
            if trade_id and _ID.match(trade_id):
                q["trade"] = trade_id
            if fields.get("action") in webtrade.ACTIONS:
                q["a"] = fields["action"]
            self._redirect("/trade/result?" + urlencode(q))
        if not rev.isdigit() or len(rev) > 15 or fields.get("action") not in webtrade.ACTIONS:
            return done("token")
        status = srv.sessions.preview_status(sess, flat.get("tok"), fields, int(rev), flat.get("psha", ""), webtrade.PREVIEW_TTL_SECONDS)
        if status != "ok":
            return done("expired" if status == "expired" else "token")
        src = srv.sources
        try:
            policy = load_policy(src.policy_path)
        except Exception:  # noqa: BLE001
            return done("unavailable")
        try:
            approved = load_approved(src.approved_path)
        except Exception:  # noqa: BLE001 - unreadable approvals count as 'not approved' (flagged), never as approved
            approved = None
        res = webwrites.record_web_trade(src.db_path, policy, approved, fields, tz=src.tz, bound_revision=int(rev),
                                         bound_policy_sha=flat.get("psha", ""), reason=flat.get("reason", ""), acks=acks, clock=src.clock)
        done(res.code, res.trade_id)

    # ---- Stage B2: label correction (preview writes nothing; confirm needs a signed preview token)
    def _label_write(self, path: str) -> None:
        srv = self.server
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (0 < len(self._body) <= MAX_TRADE_BODY) or ctype != "application/x-www-form-urlencoded":
            return self._text(400, "Bad request.")
        try:
            form = parse_qs(self._body.decode("utf-8", "strict"), max_num_fields=20, keep_blank_values=True)
        except (ValueError, UnicodeDecodeError):
            return self._text(400, "Bad request.")
        flat = {k: v[0] for k, v in form.items() if _FIELD.match(k) and v}
        sess = self._session_token()
        if not srv.sessions.csrf_ok(sess, flat.get("csrf")):
            return self._redirect("/portfolio/label-result?code=csrf")                # nothing written
        if path.endswith("/preview"):
            return self._label_preview(flat, sess)
        return self._label_confirm(flat, sess)

    def _label_preview(self, flat: dict[str, str], sess: str | None) -> None:
        srv = self.server
        fields = {k: v for k, v in flat.items() if k != "csrf"}
        fields["idem"] = uuid.uuid4().hex
        fields["action"] = weblabels.ACTION
        box: dict[str, Any] = {}

        def work(env):
            ticker = fields.get("ticker", "")
            if env.policy is None:
                return views.label_form(env, ticker if _SYMBOL.match(ticker) else "?")
            plan = weblabels.build_plan(env.rj, env.policy, env.approved, fields)
            box["plan"] = plan
            if plan.ok:
                return views.label_preview(env, plan)
            return views.label_form(env, ticker if _SYMBOL.match(ticker) else "?", fields, list(plan.errors))
        data, m = srv.with_env(work)
        plan = box.get("plan")
        m = {**m, "_csrf": srv.sessions.csrf(sess)}
        if plan is not None and plan.ok:
            data["token"] = srv.sessions.preview_token(sess, plan.fields, plan.ledger_revision, plan.policy_sha)
            return self._send(200, render.page_label_preview(data, m, srv.sources.tz))
        self._send(200, render.page_label_form(data, m, srv.sources.tz))

    def _label_confirm(self, flat: dict[str, str], sess: str | None) -> None:
        from urllib.parse import urlencode
        srv = self.server
        special = {"csrf", "tok", "rev", "psha", "reason"}
        fields = {k: v for k, v in flat.items() if k not in special and not k.startswith("ack_")}
        acks = frozenset(k for k, v in flat.items() if k.startswith("ack_") and v == "1")
        rev = flat.get("rev", "")

        def done(code: str, ticker: str | None = None) -> None:
            q = {"code": code}
            if ticker and _SYMBOL.match(ticker):
                q["ticker"] = ticker
            self._redirect("/portfolio/label-result?" + urlencode(q))
        if not rev.isdigit() or len(rev) > 15 or fields.get("action") != weblabels.ACTION:
            return done("token")
        status = srv.sessions.preview_status(sess, flat.get("tok"), fields, int(rev), flat.get("psha", ""), webtrade.PREVIEW_TTL_SECONDS)
        if status != "ok":
            return done("expired" if status == "expired" else "token")
        src = srv.sources
        try:
            policy = load_policy(src.policy_path)
        except Exception:  # noqa: BLE001
            return done("unavailable")
        try:
            approved = load_approved(src.approved_path)
        except Exception:  # noqa: BLE001 - unreadable approvals: the correction is refused (build_plan), nothing assumed
            approved = None
        res = webwrites.correct_labels(src.db_path, policy, approved, fields, bound_revision=int(rev), bound_policy_sha=flat.get("psha", ""),
                                       reason=flat.get("reason", ""), acks=acks, clock=src.clock)
        done(res.code, res.ticker)

    def _view(self, name: str, params: dict, ident: str | None, as_json: bool, path: str) -> None:
        data, m = self.server.load_view(name, params, ident)
        if data is None:
            return self._text(404, "Not found.")
        if as_json:
            return self._send(200, json.dumps({"meta": views.jsonable(m), "data": views.jsonable(data)}, indent=1, sort_keys=True),
                              "application/json")
        m = {**m, "_csrf": self.server.sessions.csrf(self._session_token())}   # only for the HTML forms; never in the JSON twin
        self._send(200, ROUTES[name][1](data, m, self.server.sources.tz, params))


def serve(server: DashboardServer) -> None:
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
