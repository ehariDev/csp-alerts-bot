"""Dashboard placement/boundary decision, process-level behaviour, and the assignment-mark side fix (DECISIONS 83/83a).

Placement verdict (tester, independent): a separate package src/csp_dashboard is ACCEPTABLE, provided the guards that
protect src/csp_alerts are matched by an equivalent, deliberate guard over src/csp_dashboard. The existing acceptance
tests (test_cli_has_no_trade_or_send_commands, telegram confinement, no network imports) only walk src/csp_alerts, so
they cannot see a server socket or a broker import added under src/csp_dashboard. They stay as they are (still guarding
csp_alerts against any HTTP server, broker, order and inbound-Telegram code, which is exactly why the dashboard must
not live there); this module adds the dashboard-side mirror plus a pin on the set of top-level packages so that a
third package, or a dashboard subcommand in csp-alerts, needs a deliberate test change."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from acc_helpers import NOW, POLICY_PATH
from s3_helpers import run_cli, write_approved
from test_s3_ledger_cli import L, add_trade, checkin, fill
from web_helpers import PW, build_book, fingerprint, scrypt_hash

from csp_alerts import boundary
from csp_alerts.journal.models import PositionState as S, StockLot
from csp_alerts.journal.store import Journal, JournalError, NewPosition

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
DASH = SRC / "csp_dashboard"
APP = SRC / "csp_alerts"


def py(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imports(path: Path) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add(n.module or "")
            out |= {f"{n.module}.{a.name}" for a in n.names}
    return out


# ============================================================================ placement and boundary
def test_top_level_packages_are_exactly_the_reviewed_pair():
    pk = {p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert pk == {"csp_alerts", "csp_dashboard"}, pk      # a third package must be a deliberate, reviewed change


def test_csp_alerts_does_not_depend_on_the_dashboard_and_has_no_dashboard_command():
    for f in py(APP):
        assert not any(m.split(".")[0] == "csp_dashboard" for m in imports(f)), f.name
    from csp_alerts.cli import build_parser
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    assert not [c for c in sub.choices if "dash" in c or "web" in c or "serve" in c]


def test_pyproject_scripts_and_dependencies_unchanged_in_kind():
    meta = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(meta["project"]["scripts"]) == {"csp-alerts", "csp-dashboard"}, meta["project"]["scripts"]
    assert meta["project"]["scripts"]["csp-dashboard"] == "csp_dashboard.cli:main"
    deps = " ".join(meta["project"]["dependencies"]).lower()
    for web in ("flask", "fastapi", "starlette", "uvicorn", "django", "aiohttp", "tornado", "bottle", "streamlit", "dash", "jinja"):
        assert web not in re.split(r"[\s<>=!~\[\];,]+", deps), web


FORBIDDEN_IMPORT_ROOTS = {"socket", "ssl", "subprocess", "ftplib", "smtplib", "telnetlib", "requests", "httpx", "aiohttp", "urllib3",
                          "telegram", "schwab", "moomoo", "futu", "ib_insync", "ibapi", "alpaca", "tastytrade", "tradier",
                          "importlib", "ctypes", "multiprocessing", "asyncio", "webbrowser", "pickle", "marshal", "shutil"}
FORBIDDEN_IMPORT_PREFIX = ("http.client", "urllib.request", "urllib.error", "xmlrpc", "os.system",
                           "csp_alerts.alerts.telegram", "csp_alerts.alerts.sendmode", "csp_alerts.runner", "csp_alerts.cli",
                           "csp_alerts.ledger_cli", "csp_alerts.jobs.scan", "csp_alerts.jobs.monitor", "csp_alerts.journal.import_csv",
                           "csp_alerts.market.stockpile", "csp_alerts.lock", "options_scanner", "stockpile")


def test_dashboard_imports_no_broker_network_client_subprocess_or_send_path():
    bad = []
    for f in py(DASH):
        for m in imports(f):
            if m.split(".")[0] in FORBIDDEN_IMPORT_ROOTS or m.startswith(FORBIDDEN_IMPORT_PREFIX) or boundary.is_forbidden_module(m):
                bad.append((f.name, m))
    assert bad == [], bad
    # the server socket machinery is confined to server.py
    for f in py(DASH):
        if f.name != "server.py":
            assert not [m for m in imports(f) if m.startswith(("http.server", "socketserver"))], f.name


def test_dashboard_has_no_order_telegram_exec_or_shell_code():
    order = re.compile(r"place_?order|submit_?order|cancel_?order|modify_?order|replace_?order|send_?order|execute_?order|"
                       r"send_?message|send_?telegram|getupdates|setwebhook|webhook|broker|roll_?position|close_?position", re.I)
    for f in py(DASH):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            name = None
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = n.name
            elif isinstance(n, ast.Attribute):
                name = n.attr
            elif isinstance(n, ast.Name):
                name = n.id
            if name:
                assert not order.search(name), (f.name, name)
                assert name not in boundary.FORBIDDEN_CALLABLE_NAMES, (f.name, name)
                assert name not in {"system", "popen", "Popen", "check_output", "check_call", "startfile", "spawn", "fork", "execv",
                                    "urlopen", "create_connection", "connect_ex"}, (f.name, name)
                if isinstance(n, ast.Name):
                    assert name not in {"eval", "exec", "compile", "__import__"}, (f.name, name)
                    assert name not in {"TelegramTransport", "make_transport", "flush", "enqueue", "run_scan", "run_monitor"}, (f.name, name)
                if isinstance(n, ast.Attribute):
                    assert name not in {"flush", "enqueue", "make_transport", "TelegramTransport", "run_scan", "run_monitor",
                                        "complete_assignment", "transition", "add_snapshot", "migrate", "record_run", "record_policy"}, (f.name, name)
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert "api.telegram.org" not in n.value and "sendMessage" not in n.value, f.name
                assert not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", n.value), f.name
    # the dashboard never touches the Telegram or broker secrets even though it shares Settings
    text = "\n".join(f.read_text(encoding="utf-8") for f in py(DASH))
    for secret in ("telegram_bot_token", "telegram_chat_id", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "bot_token", "chat_id"):
        assert secret not in text, secret


def test_dashboard_sql_is_read_only_and_no_float_money():
    write_kw = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM|ATTACH|DETACH|REINDEX|SAVEPOINT|COMMIT)\b")
    for f in py(DASH):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        doc = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
               and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc and "SELECT" not in n.value[:12]:
                if re.match(r"\s*(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|REPLACE\s+INTO|CREATE\s+(TABLE|INDEX|VIEW|TRIGGER)|DROP\s|ALTER\s+TABLE|PRAGMA\s+(?!query_only|busy_timeout))", n.value):
                    raise AssertionError((f.name, n.value[:60]))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc and re.match(r"\s*SELECT", n.value):
                assert not write_kw.search(n.value), (f.name, n.value[:80])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float":
                raise AssertionError((f.name, "float() used; money must stay Decimal"))
    for f in py(DASH):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if f.name == "readonly.py" and isinstance(n, ast.Constant) and n.value == 5.0:
                continue                                     # sqlite connect timeout in seconds, not money (same exemption as journal/store.py)
            assert not (isinstance(n, ast.Constant) and isinstance(n.value, float)), (f.name, n.value)


def test_server_binds_loopback_only_and_refuses_anything_else(tmp_path):
    from csp_dashboard.server import DashboardServer
    from csp_dashboard.views import Sources
    src = Sources(tmp_path / "x.sqlite3", POLICY_PATH, tmp_path / "a.json", "America/Chicago")
    for host in ("0.0.0.0", "", "localhost", "::", "::1", "192.168.1.5", "127.0.0.2", "10.0.0.1"):
        with pytest.raises(ValueError):
            DashboardServer(src, scrypt_hash("x" * 12), port=0, host=host)
    s = DashboardServer(src, scrypt_hash("x" * 12), port=0)
    try:
        assert s.server_address[0] == "127.0.0.1"
    finally:
        s.server_close()
    text = "\n".join(f.read_text(encoding="utf-8") for f in py(DASH))
    assert "0.0.0.0" not in text and "'::'" not in text


def test_importing_dashboard_loads_no_forbidden_module_and_no_send_or_scan_code():
    code = r"""
import socket, sys, importlib, pkgutil
def boom(*a, **k): raise RuntimeError("NETWORK ATTEMPT")
socket.socket.connect = boom; socket.create_connection = boom; socket.getaddrinfo = boom
import csp_dashboard
for m in pkgutil.walk_packages(csp_dashboard.__path__, "csp_dashboard."):
    if not m.name.endswith("__main__"):
        importlib.import_module(m.name)
from csp_alerts import boundary
bad = boundary.loaded_forbidden_modules()
must_not = ["csp_alerts.runner", "csp_alerts.jobs.scan", "csp_alerts.jobs.monitor",
            "csp_alerts.ledger_cli", "csp_alerts.cli", "csp_alerts.market.stockpile", "requests", "httpx", "subprocess"]
loaded = [m for m in must_not if m in sys.modules]
print("BAD", bad, loaded)
sys.exit(1 if (bad or loaded) else 0)
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_existing_csp_alerts_guards_still_active():
    """The csp_alerts side must stay strict: a planted HTTP server would be caught by the pinned token list."""
    pinned = (REPO / "tests" / "acceptance" / "test_settings_policy_boundary.py").read_text(encoding="utf-8")
    for tok in ("http.server", "socketserver", "serve_forever", "getUpdates", "webhook", "SEND_CAPABLE"):
        assert tok in pinned
    for f in py(APP):
        t = f.read_text(encoding="utf-8")
        assert "http.server" not in t and "serve_forever" not in t, f.name


# ============================================================================ process level: CLI, startup/shutdown, WD-AT-07
def _env(tmp: Path, hash_value: str | None, db: Path | None = None, port: int | None = None) -> Path:
    lines = ["CSP_ENV=paper", "CSP_DRY_RUN=true", "CSP_DISABLE_BROKER_ACCESS=true", "CSP_TZ=America/Chicago",
             f"CSP_DATABASE_URL=sqlite:///{(db or tmp / 'j.sqlite3').as_posix()}"]
    if hash_value is not None:
        lines.append(f"CSP_DASHBOARD_PASSWORD_HASH={hash_value}")
    if port:
        lines.append(f"CSP_DASHBOARD_PORT={port}")
    p = tmp / "t.env"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _clean_env() -> dict[str, str]:
    keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME", "PYTHONUTF8", "COMSPEC", "PATHEXT"}
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def _run(args, tmp, stdin=None, timeout=60):
    env = _clean_env()
    return subprocess.run([sys.executable, "-m", "csp_dashboard", *args], capture_output=True, text=True, cwd=str(REPO), env=env,
                          input=stdin, timeout=timeout)


def test_start_refuses_without_a_real_hash_and_never_serves(tmp_path):
    for hv in (None, "", "REPLACE_LOCALLY"):
        r = _run(["--env-file", str(_env(tmp_path, hv)), "start"], tmp_path, timeout=30)
        assert r.returncode == 2, (hv, r.returncode)
        assert "listening" not in r.stdout.lower()


def test_version_info_prints_identity_and_writes_nothing_without_flag(tmp_path):
    r = _run(["version-info"], tmp_path)
    assert r.returncode == 0, r.stderr
    j = json.loads(r.stdout)
    assert re.fullmatch(r"[0-9a-f]{64}", j["manifest_sha256"]) and j["files"] > 20
    assert not list((tmp_path).glob("**/manifest-*.json"))
    assert "manifest written" not in r.stdout    # the manifest hashes source files only: never .env or databases
    from csp_alerts.version import source_manifest
    names = " ".join(source_manifest() if isinstance(source_manifest(), (list, tuple)) else source_manifest().keys())
    assert ".env" not in names and ".sqlite" not in names and "approved.json" not in names


def test_hash_password_command_makes_a_verifiable_scrypt_hash_without_echo(monkeypatch, capsys):
    """getpass reads the Windows console, not a pipe, so the command is driven in-process with getpass patched."""
    import getpass
    from csp_dashboard import cli as dcli
    pw = "a-long-enough-test-password"
    answers = iter([pw, pw])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert dcli.main(["hash-password"]) == 0
    out = capsys.readouterr()
    m = re.search(r"CSP_DASHBOARD_PASSWORD_HASH=(scrypt\$\d+\$\d+\$\d+\$[0-9a-f]+\$[0-9a-f]+)", out.out)
    assert m and pw not in out.out and pw not in out.err
    _, n, rr, p, salt, hh = m.group(1).split("$")
    assert int(n) >= 2 ** 14                                                    # not a trivially cheap work factor
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(rr), p=int(p), maxmem=256 * 1024 * 1024, dklen=32)
    assert got.hex() == hh                                                      # independent check of the documented format
    assert len(bytes.fromhex(salt)) >= 16
    answers = iter(["short", "short"])
    assert dcli.main(["hash-password"]) == 2                                    # min length 12
    answers = iter([pw, "other-other-other"])
    assert dcli.main(["hash-password"]) == 2                                    # mismatch
    # two hashes of the same password differ (random salt)
    answers = iter([pw, pw, pw, pw])
    dcli.main(["hash-password"]); h1 = capsys.readouterr().out
    dcli.main(["hash-password"]); h2 = capsys.readouterr().out
    assert h1 != h2


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_real_process_start_stop_leaves_journal_and_job_state_untouched(tmp_path):
    """WD-AT-07: start the real dashboard process, log in, browse, kill it; scheduled-job state (run_status, outbox,
    audit, positions) and the DB bytes are identical; a restart serves the same data. Loopback only."""
    import http.client
    db = tmp_path / "j.sqlite3"
    j = build_book(db)
    j.record_run("scan-1", "SCAN", NOW - timedelta(minutes=30), "NO_OPPORTUNITIES", [], {"tickers": 0})
    j.close()
    before = fingerprint(db)
    port = _free_port()
    envf = _env(tmp_path, scrypt_hash(PW), db, port)
    envv = _clean_env()

    def start():
        pr = subprocess.Popen([sys.executable, "-m", "csp_dashboard", "--env-file", str(envf), "--approved", str(tmp_path / "a.json"), "start"],
                              cwd=str(REPO), env=envv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/login")
                if c.getresponse().status == 200:
                    return pr
            except OSError:
                time.sleep(0.2)
        pr.kill()
        raise AssertionError("dashboard did not start: " + (pr.stderr.read() if pr.stderr else ""))

    write_approved(tmp_path / "a.json", ())
    outputs = []
    for round_ in range(2):
        if round_:  # restart on a fresh ephemeral port: the server deliberately does not set SO_REUSEADDR, so the old port can sit in TIME_WAIT on Linux
            port = _free_port()
            envf = _env(tmp_path, scrypt_hash(PW), db, port)
        pr = start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/login", body=f"password={PW}", headers={"Origin": f"http://127.0.0.1:{port}",
                      "Content-Type": "application/x-www-form-urlencoded"})
            r = c.getresponse(); r.read()
            assert r.status == 303
            cookie = re.search(r"csp_session=([^;]+)", r.getheader("Set-Cookie")).group(1)
            for p in ("/api/overview", "/api/health", "/api/runs", "/positions"):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", p, headers={"Cookie": f"csp_session={cookie}"})
                r = c.getresponse(); body = r.read().decode()
                assert r.status == 200, (p, r.status)
        finally:
            pr.terminate()
            try:
                out, err = pr.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pr.kill(); out, err = pr.communicate()
        outputs.append(out + err)
        assert fingerprint(db) == before, f"round {round_}: starting/stopping the dashboard changed the journal"
    allout = "\n".join(outputs)
    assert PW not in allout and "scrypt$" not in allout
    assert "0.0.0.0" not in allout


# ============================================================================ assignment side fix (DECISIONS 83 / 83a)
def _assigned_pending(led):
    checkin(led)
    add_trade(led)
    fill(led)
    code, out = led("journal", "assign", "--trade-id", "P1", apply=True, reason="notified")
    assert code == 0 and led.state("P1") == "ASSIGNED_PENDING_RECONCILIATION", out


def _lot_mark(led):
    c = led.db()
    try:
        return c.execute("SELECT reference_asof_utc, linked_trade_id FROM stock_lots WHERE lot_id='LOT-P1'").fetchone()
    finally:
        c.close()


UTC = timezone.utc
STEP2 = ("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500")


@pytest.mark.parametrize("when,expected_mark,label", [
    (datetime(2026, 10, 30, 15, 0, 0, tzinfo=UTC), "2026-10-30T15:00:00Z", "10:00 CDT, midday: mark = now (not a future 21:00Z)"),
    (datetime(2026, 10, 30, 20, 30, 0, tzinfo=UTC), "2026-10-30T20:30:00Z", "16:30 EDT, after the close but before 21:00Z"),
    (datetime(2026, 10, 30, 20, 59, 59, tzinfo=UTC), "2026-10-30T20:59:59Z", "one second before the 21:00Z cap"),
    (datetime(2026, 10, 30, 21, 0, 0, tzinfo=UTC), "2026-10-30T21:00:00Z", "exactly at the cap"),
    (datetime(2026, 10, 30, 22, 0, 0, tzinfo=UTC), "2026-10-30T21:00:00Z", "after the cap: 21:00Z of the exchange date"),
])
def test_assignment_step2_marks_lot_at_min_of_2100z_and_now(monkeypatch, tmp_path, when, expected_mark, label):
    led = L(monkeypatch, tmp_path)
    assert led("journal", "init")[0] == 0 and led("journal", "confirm-baseline", "--equity", "100000")[0] == 0
    write_approved(led.approved, ("AAA",), approved_through="2027-06-30")
    _assigned_pending(led)
    code, out = led(*STEP2, now=when, apply=True, reason="shares received")
    assert code == 0, (label, out[-300:])
    assert led.state("P1") == "ASSIGNED_RECONCILED"
    mark, linked = _lot_mark(led)
    assert mark == expected_mark and linked == "P1", (label, mark)
    # the mark is never in the future relative to the command time (so reconcile cannot flag STOCK_MARK_FUTURE)
    assert datetime.fromisoformat(mark.replace("Z", "+00:00")) <= when
    code, out = led("reconcile-check", now=when)
    assert "STOCK_MARK_FUTURE" not in out, label


def test_assignment_step2_with_explicit_dates(monkeypatch, tmp_path):
    led = L(monkeypatch, tmp_path)
    assert led("journal", "init")[0] == 0 and led("journal", "confirm-baseline", "--equity", "100000")[0] == 0
    write_approved(led.approved, ("AAA",), approved_through="2027-06-30")
    _assigned_pending(led)
    now = datetime(2026, 10, 30, 15, 0, 0, tzinfo=UTC)
    # a FUTURE --date must not produce a future mark
    code, out = led(*STEP2, "--date", "2026-11-05", now=now, apply=True, reason="x")
    assert code == 0, out[-300:]
    mark, _ = _lot_mark(led)
    assert mark == "2026-10-30T15:00:00Z"


def test_assignment_step2_past_date_keeps_2100z_of_that_date(monkeypatch, tmp_path):
    led = L(monkeypatch, tmp_path)
    assert led("journal", "init")[0] == 0 and led("journal", "confirm-baseline", "--equity", "100000")[0] == 0
    write_approved(led.approved, ("AAA",), approved_through="2027-06-30")
    _assigned_pending(led)
    code, out = led(*STEP2, "--date", "2026-10-29", now=datetime(2026, 10, 30, 15, 0, 0, tzinfo=UTC), apply=True, reason="x")
    assert code == 0, out[-300:]
    assert _lot_mark(led)[0] == "2026-10-29T21:00:00Z"


def _assign_ready(tmp_path, clock_now):
    j = Journal(tmp_path / "j.sqlite3", clock=lambda: clock_now)
    j.migrate()
    j.create_draft(NewPosition("P1", "AAA", date(2026, 12, 4), D("75.00"), 100, 1, "s", "b"))
    j.transition("P1", S.PENDING)
    j.transition("P1", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.50"), opened_at_utc=clock_now, fees_usd=D("0.65"))
    j.transition("P1", S.ASSIGNED_PENDING_RECONCILIATION)
    return j


@pytest.mark.parametrize("delta_s,ok", [(-3600, True), (0, True), (299, True), (300, True), (301, False), (3600, False)])
def test_complete_assignment_future_mark_tolerance_is_five_minutes(tmp_path, delta_s, ok):
    now = datetime(2026, 10, 30, 15, 0, 0, tzinfo=UTC)
    j = _assign_ready(tmp_path, now)
    lot = StockLot("LOT-P1", "AAA", D("100"), D("75.00"), now + timedelta(seconds=delta_s), "s", "b", "ASSIGNMENT", "STOCK", D("7500"), "P1")
    before = j.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    if ok:
        p = j.complete_assignment("P1", lot)
        assert p.state is S.ASSIGNED_RECONCILED
        assert j.conn.execute("SELECT COUNT(*) FROM stock_lots").fetchone()[0] == 1
    else:
        with pytest.raises(JournalError, match="future"):
            j.complete_assignment("P1", lot)
        # nothing written: position still pending, no lot, no audit rows
        assert j.get_position("P1").state is S.ASSIGNED_PENDING_RECONCILIATION
        assert j.conn.execute("SELECT COUNT(*) FROM stock_lots").fetchone()[0] == 0
        assert j.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
    j.close()
