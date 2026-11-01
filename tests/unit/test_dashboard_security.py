"""Dashboard access boundary, auth, zero writes, static boundary audit, version identity (WD-002, WD-016..WD-018, WD-AT-05)."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from dash_fixture import HASH, PASSWORD, Client, Running, build
from factories import ROOT

from csp_alerts import version
from csp_dashboard import auth
from csp_dashboard.server import DashboardServer

PAGES = ["/", "/candidates", "/candidates?bucket=rejected", "/candidates?bucket=stale", "/candidates?bucket=data", "/positions",
         "/positions?group=all", "/positions?group=history", "/positions/P-OPEN", "/positions/P-DONE", "/positions/P-PEND",
         "/alerts", "/alerts?status=SENT", "/alerts/alert-0", "/alerts/alert-1", "/activity", "/activity?event_type=SNAPSHOT_ADDED",
         "/runs", "/runs?kind=SCAN", "/health", "/static/app.css"]


def anon(authed: Client) -> Client:
    c = Client.__new__(Client)
    c.port, c.cookie = authed.port, None
    return c


def fingerprint(db: Path) -> dict:
    con = sqlite3.connect(db)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    rows = {t: con.execute(f'SELECT COUNT(*), COALESCE(SUM(rowid),0) FROM "{t}"').fetchone() for t in tables}
    audit = con.execute("SELECT COUNT(*), MAX(rowid) FROM audit_log").fetchone()
    con.close()
    return {"bytes": hashlib.sha256(db.read_bytes()).hexdigest(), "rows": rows, "audit": audit, "mtime": db.stat().st_mtime_ns}


# ---------------------------------------------------------------- WD-AT-05: zero writes
def test_browsing_every_view_and_filter_writes_nothing(tmp_path):
    p = build(tmp_path)
    before = fingerprint(p.db)
    with Running(p) as c:
        for path in PAGES:
            assert c.get(path)[0] == 200, path
            assert c.get("/api" + path)[0] in (200, 404), path        # JSON twin (static css has none)
        for path in PAGES[:-1]:
            c.get(path)                                               # "refresh" = just GET again
        c.get("/candidates?run=nope")
    assert fingerprint(p.db) == before                                # db bytes, mtime, row counts and audit rowcount identical


def test_dashboard_never_reaches_scan_send_migrate_or_write_paths(tmp_path, monkeypatch):
    import csp_alerts.alerts.outbox as outbox
    import csp_alerts.alerts.telegram as tg
    import csp_alerts.jobs.scan as scan
    from csp_alerts.journal.store import Journal

    p = build(tmp_path)

    def boom(*a, **k):
        raise AssertionError("forbidden call from the dashboard")
    for obj, name in ((scan, "run_scan"), (outbox, "enqueue"), (outbox, "flush"), (tg.TelegramTransport, "send"),
                      (Journal, "migrate"), (Journal, "record_run"), (Journal, "sync_approved"), (Journal, "record_policy"),
                      (Journal, "upsert_approved"), (Journal, "add_snapshot")):
        monkeypatch.setattr(obj, name, boom)
    with Running(p) as c:
        for path in PAGES:
            assert c.get(path)[0] == 200


def test_dashboard_source_has_no_write_sql_network_client_or_subprocess():
    root = ROOT / "src" / "csp_dashboard"
    bad_imports = {"socket", "ssl", "http.client", "urllib.request", "ftplib", "smtplib", "subprocess", "requests", "httpx", "aiohttp",
                   "importlib"}
    write_sql = re.compile(r"^\s*(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s|ALTER\s|DROP\s|REPLACE\s+INTO|VACUUM|ATTACH\s|PRAGMA\s+(?!busy_timeout|query_only)\w+\s*=)", re.I)
    order = re.compile(r"(place|submit|cancel|send|create)_?(an_?)?(option_?)?(order|trade)s?", re.I)
    for f in root.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else []
            assert not [m for m in mods if m in bad_imports or m.split(".")[0] in {"schwab", "moomoo", "streamlit", "options_scanner"}], (f.name, mods)
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert not write_sql.match(n.value), (f.name, n.value[:60])
            if isinstance(n, (ast.Name, ast.Attribute)):
                nm = n.id if isinstance(n, ast.Name) else n.attr
                assert nm not in {"eval", "exec", "system", "Popen", "executescript", "executemany"}, (f.name, nm)
                assert not order.search(nm), (f.name, nm)
    text = (root / "readonly.py").read_text(encoding="utf-8")
    assert "COMMIT" not in text and '"BEGIN"' in text                      # the read layer only ever BEGINs and ROLLBACKs


# ---------------------------------------------------------------- binding
def test_binds_loopback_only(tmp_path):
    p = build(tmp_path, scan=False)
    for host in ("0.0.0.0", "", "::", "192.168.1.5", "localhost"):
        with pytest.raises(ValueError):
            DashboardServer(p.sources(), HASH, port=0, host=host)
    s = DashboardServer(p.sources(), HASH, port=0)
    assert s.server_address[0] == "127.0.0.1"
    s.server_close()


# ---------------------------------------------------------------- auth
def test_unauthenticated_access_is_denied_everywhere(tmp_path):
    p = build(tmp_path, scan=False)
    with Running(p) as authed:
        c = anon(authed)
        for path in ("/", "/candidates", "/positions/P-OPEN", "/alerts/alert-0", "/activity", "/runs", "/health"):
            st, body, h = c.get(path)
            assert st == 303 and h["location"] == "/login" and "P-OPEN" not in body
            st, body, _ = c.get("/api" + path)
            assert st == 401 and "equity" not in body
        c.cookie = "forged"
        assert c.get("/")[0] == 303
        assert c.get("/static/app.css")[0] == 200 and c.get("/login")[0] == 200       # only the login page and stylesheet are open


def test_login_wrong_password_lockout_and_logout(tmp_path, capsys):
    p = build(tmp_path, scan=False)
    with Running(p) as authed:
        c = anon(authed)
        wrong = "WrongPassw0rd!!"
        for _ in range(5):
            assert c.login(wrong) == 401
        assert c.login(PASSWORD) == 429                                   # locked out even with the right password
        assert c.get("/")[0] == 303
        authed.req("POST", "/logout")
        assert authed.get("/")[0] == 303                                 # session dropped
    err = capsys.readouterr().err
    assert wrong not in err and PASSWORD not in err and "csp_session" not in err


def test_login_flow_cookie_flags_and_no_password_in_url_or_page(tmp_path, capsys):
    p = build(tmp_path, scan=False)
    with Running(p) as authed:
        c = anon(authed)
        st, body, h = c.req("POST", "/login", body="password=" + PASSWORD.replace(" ", "+"))
        assert st == 303 and h["location"] == "/"
        sc = h["set-cookie"]
        assert "HttpOnly" in sc and "SameSite=Strict" in sc and "Path=/" in sc and PASSWORD not in sc
        c.cookie = sc.split(";")[0].split("=", 1)[1]
        st, page, _ = c.get("/?junk=SECRETQUERYVALUE")
        assert st == 200 and PASSWORD not in page and "SECRETQUERYVALUE" not in page
        assert c.get("/login?next=http://evil.example/")[0] == 200
        assert c.req("POST", "/login", body="password=" + PASSWORD.replace(" ", "+"))[2]["location"] == "/"   # fixed target
    err = capsys.readouterr().err
    assert "SECRETQUERYVALUE" not in err and PASSWORD not in err           # query strings and bodies are never logged


def test_login_rejects_bad_content_type_and_oversized_body(tmp_path):
    p = build(tmp_path, scan=False)
    with Running(p) as authed:
        c = anon(authed)
        assert c.req("POST", "/login", body="password=x", headers={"Content-Type": "application/json"})[0] == 400
        assert c.req("POST", "/login", body="password=" + "a" * 5000)[0] == 400


def test_password_hash_helpers():
    pw = "a long enough passphrase"
    h = auth.make_hash(pw, n=16)
    assert h.startswith("scrypt$16$") and pw not in h
    assert auth.verify(pw, h) and not auth.verify("another passphrase!!", h)
    for junk in ("", "x", "scrypt$1$8$1$aa$bb", "scrypt$99999999999$8$1$aa$bb", "md5$16$8$1$aa$bb", h[:-3], "scrypt$16$8$1$zz$zz"):
        assert auth.verify(pw, junk) is False
    with pytest.raises(ValueError):
        auth.make_hash("short")
    assert auth.make_hash(pw, n=16) != h                                    # salted


# ---------------------------------------------------------------- host / origin / methods
def test_host_header_must_be_loopback_with_the_server_port(tmp_path):
    p = build(tmp_path, scan=False)
    with Running(p) as c:
        for bad in ("evil.example", "127.0.0.1", "127.0.0.1:1", f"0.0.0.0:{c.port}", f"evil.example:{c.port}"):
            assert c.get("/", host=bad)[0] == 421, bad
        assert c.get("/", host=f"localhost:{c.port}")[0] == 200
        assert c.get("/", host=f"LOCALHOST:{c.port}")[0] == 200


def test_origin_and_fetch_site_checks_and_no_cors(tmp_path):
    p = build(tmp_path, scan=False)
    with Running(p) as c:
        assert c.get("/", headers={"Origin": "http://evil.example"})[0] == 403
        assert c.get("/", headers={"Origin": f"http://127.0.0.1:{c.port}"})[0] == 200
        assert c.get("/", headers={"Origin": "null"})[0] == 403
        assert c.get("/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert c.get("/api/health", headers={"Sec-Fetch-Site": "same-site"})[0] == 403
        assert c.req("POST", "/logout", origin=None)[0] == 403               # POST must carry Origin
        assert c.req("POST", "/logout", origin="http://evil.example")[0] == 403
        assert c.get("/")[0] == 200                                         # the refused logouts did not end the session
        st, body, h = c.get("/")
        assert not [k for k in h if k.startswith("access-control-")]
        st, _, h = c.req("OPTIONS", "/", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
        assert st in (403, 405) and not [k for k in h if k.startswith("access-control-")]
        st, _, h = c.req("OPTIONS", "/", headers={"Origin": f"http://127.0.0.1:{c.port}"})
        assert st == 405 and not [k for k in h if k.startswith("access-control-")] and h["allow"] == "GET, POST"


def test_only_get_for_views_and_security_headers(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        for m in ("POST", "PUT", "DELETE", "PATCH", "HEAD"):
            for path in ("/", "/positions", "/api/overview", "/health"):
                st = c.req(m, path, body="x=1" if m in ("POST", "PUT", "PATCH") else None)[0]
                assert st == 405, (m, path, st)
        st, _, h = c.get("/")
        assert h["cache-control"] == "no-store" and h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        csp = h["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src" not in csp and "unsafe-inline" not in csp and "http" not in csp
        assert h["referrer-policy"] == "same-origin" and "python" not in h["server"].lower()


def test_routes_expose_no_paths_secrets_sql_or_files(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        for path in ("/positions/..%2f..%2f.env", "/positions/../../.env", "/static/../../.env", "/static/app.css/../x", "/db", "/sql?q=select",
                     "/file?path=C:/x", "/candidates/%27%20OR%201=1", "/runs/x;DROP", "/api/../..", "/.env", "/data/j.sqlite3"):
            st, body, _ = c.get(path)
            assert st in (400, 404), (path, st)
        for path in PAGES[:-1]:
            body = c.get(path)[1] + c.get("/api" + path)[1]
            assert str(p.db.parent) not in body and "j.sqlite3" not in body and "TELEGRAM_BOT_TOKEN" not in body
            assert HASH not in body and "scrypt$" not in body
        assert c.get("/positions?ticker=%27%3BDROP%20TABLE")[0] == 400       # filters are validated and bound as parameters
        assert c.get("/" + "a" * 3000)[0] == 414


def test_policy_error_does_not_leak_file_path(tmp_path):
    p = build(tmp_path)
    srv = DashboardServer(p.sources(), HASH, port=0)
    srv.sources.policy_path = tmp_path / "missing-secret-dir" / "policy.yaml"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv)
        assert c.login() == 303
        body = c.get("/")[1]
        assert "POLICY_INVALID" in body and "missing-secret-dir" not in body
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- CLI
def _run_cli(tmp_path, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME")}
    env.update({"PYTHONPATH": str(ROOT / "src"), "CSP_DATABASE_URL": "sqlite:///" + (tmp_path / "x.sqlite3").as_posix()})
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-I", "-m", "csp_dashboard", "--env-file", str(tmp_path / "none.env"), *args],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)


def test_start_refuses_without_a_configured_password_hash(tmp_path):
    r = _run_cli(tmp_path, "start")
    assert r.returncode == 2 and "CSP_DASHBOARD_PASSWORD_HASH" in r.stderr
    r = _run_cli(tmp_path, "start", env_extra={"CSP_DASHBOARD_PASSWORD_HASH": "REPLACE_LOCALLY"})
    assert r.returncode == 2                                           # the template placeholder counts as "not configured"
    r = _run_cli(tmp_path, "start", env_extra={"CSP_DASHBOARD_PASSWORD_HASH": HASH, "CSP_DASHBOARD_PORT": "80"})
    assert r.returncode == 2 and HASH not in r.stdout + r.stderr       # privileged port rejected; hash never echoed


def test_version_info_and_manifest(tmp_path):
    r = _run_cli(tmp_path, "version-info", "--write-manifest")
    assert r.returncode == 0 and "manifest_sha256" in r.stdout
    written = list((tmp_path / "data" / "manifests").glob("manifest-*.json"))
    assert len(written) == 1 and "csp_alerts/version.py" in written[0].read_text(encoding="utf-8")


def test_env_example_has_only_placeholders():
    txt = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CSP_DASHBOARD_PASSWORD_HASH=REPLACE_LOCALLY" in txt and "scrypt$" not in txt


# ---------------------------------------------------------------- WD-002 version identity
def test_git_revision_from_repository_files_without_git_binary(tmp_path):
    sha = "a" * 40
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert version.git_revision(repo / ".git") is None                      # branch has no commit yet
    (repo / ".git" / "refs" / "heads" / "main").write_text(sha + "\n")
    assert version.git_revision(repo / ".git") == sha
    (repo / "src" / "pkg").mkdir(parents=True)
    assert version.find_git_dir(repo / "src" / "pkg") == (repo / ".git").resolve()
    (repo / ".git" / "refs" / "heads" / "main").unlink()
    (repo / ".git" / "packed-refs").write_text(f"# pack-refs\n{'b' * 40} refs/heads/main\n")
    assert version.git_revision(repo / ".git") == "b" * 40
    (repo / ".git" / "HEAD").write_text("c" * 40 + "\n")                     # detached
    assert version.git_revision(repo / ".git") == "c" * 40
    (repo / ".git" / "HEAD").write_text("garbage")
    assert version.git_revision(repo / ".git") is None


def test_manifest_covers_source_only_and_changes_with_content(tmp_path):
    pkg = tmp_path / "csp_alerts"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "a.py").write_text("x=1")
    (pkg / "m.sql").write_text("select 1;")
    (pkg / "secret.env").write_text("TOKEN=1")
    (pkg / "data.sqlite3").write_bytes(b"db")
    (pkg / "__pycache__" / "a.pyc").write_bytes(b"c")
    m = version.source_manifest(tmp_path)
    assert set(m) == {"csp_alerts/a.py", "csp_alerts/m.sql"}                # never .env, databases or caches
    h1 = version.manifest_hash(m)
    (pkg / "a.py").write_text("x=2")
    assert version.manifest_hash(version.source_manifest(tmp_path)) != h1
    ident = version.code_identity()
    assert ident.label.startswith(("git:", "manifest:")) and len(ident.manifest_sha256) == 64 and ident.file_count > 20


def test_referrer_policy_lets_browsers_send_a_real_origin_and_null_origin_is_still_refused(tmp_path):
    """no-referrer makes browsers send `Origin: null` on form POSTs (Fetch spec), which broke the owner's login with 403."""
    from csp_dashboard.server import SECURITY_HEADERS
    assert SECURITY_HEADERS["Referrer-Policy"] not in ("no-referrer", "") and SECURITY_HEADERS["Referrer-Policy"] == "same-origin"
    with Running(build(tmp_path)) as c:
        for bad in ("null", "http://evil.example", "http://127.0.0.1:1"):
            st, _, _ = c.req("POST", "/login", body="password=x", origin=bad)
            assert st == 403, bad
        assert c.req("POST", "/login", body="password=x", origin=None)[0] == 403       # POST without Origin
        assert c.login() == 303                                                       # real same-origin Origin works
