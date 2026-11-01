"""Slice 3 acceptance: F1/F6 run statuses, status/health/heartbeat, Windows scheduling scripts (STATIC only), single-instance lock,
backup/restore (FIRST_DELIVERY gates 1, 5, 7; MVP_SCOPE F1, F6, IT-009, IT-013, IT-017). No task is registered, nothing is sent."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest
from acc_helpers import NOW, POLICY_PATH, REPO, TODAY, policy
from s2_helpers import FakeSource, appr, make_journal, raw_row
from s3_helpers import FakeTelegram, TOKEN, CHAT, count, ledger, run_cli, write_approved

from csp_alerts.alerts import outbox
from csp_alerts.alerts.sendmode import Mode, SendDecision
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.jobs.monitor import run_monitor
from csp_alerts.jobs.scan import run_scan
from csp_alerts.jobs.status import collect_status, heartbeat_line, next_runs, scan_run_status, status_lines
from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.store import Journal
from csp_alerts.market.models import SessionInfo, SessionStatus

POL = policy()
OPEN = SessionInfo(SessionStatus.OPEN, 294)
CT = ZoneInfo("America/Chicago")


def ct(h, m, s=0, d=(2026, 10, 30)) -> datetime:
    return datetime(*d, h, m, s, tzinfo=CT).astimezone(timezone.utc)


def runs(j, kind=None):
    q = "SELECT * FROM run_status" + (" WHERE kind=?" if kind else "")
    return [dict(r) for r in j.conn.execute(q, (kind,) if kind else ())]


# ====================================================================== F1: exactly one visible status per run
def test_every_scan_run_ends_in_exactly_one_visible_status():
    cases = []
    j = make_journal()
    r = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({"AAA": [raw_row()]}), now_utc=NOW, session=OPEN)
    cases.append((j, r, "CANDIDATES"))
    j = make_journal()
    r = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({"AAA": [raw_row(delta=-0.5)]}), now_utc=NOW, session=OPEN)
    cases.append((j, r, "NO_OPPORTUNITIES"))
    j = make_journal()
    r = run_scan(journal=j, policy=POL, approved=[], source=FakeSource({}), now_utc=NOW, session=OPEN)
    cases.append((j, r, "NO_OPPORTUNITIES"))
    j = make_journal(snap=False)
    r = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({}), now_utc=NOW, session=OPEN)
    cases.append((j, r, "BLOCKED_LEDGER"))
    j = make_journal()
    r = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({}), now_utc=NOW, session=SessionInfo(SessionStatus.CLOSED))
    cases.append((j, r, "MARKET_CLOSED"))
    j = make_journal()
    r = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({"AAA": RuntimeError("down")}), now_utc=NOW, session=OPEN)
    cases.append((j, r, "DATA_UNAVAILABLE"))
    j = make_journal(snap=False)
    j.add_snapshot(AccountSnapshot("a", NOW - timedelta(hours=26), D("100000"), D("50000"), True, "v"))
    j.add_snapshot(AccountSnapshot("b", NOW - timedelta(hours=2), D("91000"), D("50000"), True, "v"))
    r = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({}), now_utc=NOW, session=OPEN)
    cases.append((j, r, "BLOCKED_LEDGER"))
    for j, res, expected in cases:
        rows = runs(j, "SCAN")
        assert len(rows) == 1 and rows[0]["status"] == expected and rows[0]["run_id"] == res.run_id, (expected, rows)
        assert rows[0]["started_at_utc"] == "2026-10-30T15:05:02Z"
    # a second run adds a second row; run ids are unique
    run_scan(journal=cases[0][0], policy=POL, approved=[appr("AAA")], source=FakeSource({"AAA": [raw_row()]}), now_utc=NOW + timedelta(minutes=1), session=OPEN)
    assert len({r["run_id"] for r in runs(cases[0][0], "SCAN")}) == 2


def test_blocker_reasons_are_recorded_with_the_blocked_run():
    j = make_journal(snap=False)
    run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({}), now_utc=NOW, session=OPEN)
    assert "LEDGER_STALE" in json.loads(runs(j)[0]["reasons_json"])


@pytest.mark.parametrize("scan_status,reasons,out", [
    ("CANDIDATES", set(), "CANDIDATES"), ("NO_TRADE", set(), "NO_OPPORTUNITIES"), ("DATA_UNAVAILABLE", set(), "DATA_UNAVAILABLE"),
    ("BLOCKED", {"MARKET_CLOSED"}, "MARKET_CLOSED"), ("BLOCKED", {"UNKNOWN_SESSION"}, "MARKET_CLOSED"),
    ("BLOCKED", {"MARKET_CLOSED", "LEDGER_STALE"}, "BLOCKED_LEDGER"), ("BLOCKED", {"HARD_HALT"}, "BLOCKED_LEDGER"), ("???", set(), "SERVICE_FAILURE"),
])
def test_status_vocabulary_mapping(scan_status, reasons, out):
    assert scan_run_status(scan_status, reasons) == out


def test_monitor_cli_records_one_status_per_run_for_every_outcome(monkeypatch, tmp_path):
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", expiration=TODAY + timedelta(days=5))], path=tmp_path / "j.sqlite3")
    j.close()
    ap = write_approved(tmp_path / "approved.json")
    from s3_helpers import chain_rows_for
    exp = TODAY + timedelta(days=5)
    scenarios = [
        ("ALERTS", FakeSource({"AAA": chain_rows_for(75.0, exp, 0.10, 0.12)})),
        ("DATA_UNAVAILABLE", FakeSource({"AAA": RuntimeError("down")})),
    ]
    for want, src in scenarios:
        code, out, err = run_cli(monkeypatch, tmp_path, ["monitor"], source=src, approved=ap)
        # position is 5 DTE: calendar alerts are real alerts, so both runs end ALERTS (the failed fetch adds a DATA_ISSUE message)
        assert code == 0 and "status=ALERTS" in out, (want, out)
    c = sqlite3.connect(tmp_path / "j.sqlite3")
    rows = c.execute("SELECT status FROM run_status WHERE kind='MONITOR'").fetchall()
    assert [r[0] for r in rows] == ["ALERTS", "ALERTS"]                    # exactly one row per run
    # NO_POSITIONS
    j = Journal(tmp_path / "j.sqlite3")
    j.transition("P1", __import__("csp_alerts.journal.models", fromlist=["x"]).PositionState.CLOSE_WORKING)
    j.transition("P1", __import__("csp_alerts.journal.models", fromlist=["x"]).PositionState.CLOSED,
                 actual_close_debit_per_share=D("0.1"), closed_at_utc=NOW, fees_usd=D("0"))
    j.close()
    code, out, _ = run_cli(monkeypatch, tmp_path, ["monitor"], source=FakeSource({}), approved=ap)
    assert "status=NO_POSITIONS" in out
    assert sqlite3.connect(tmp_path / "j.sqlite3").execute("SELECT COUNT(*) FROM run_status WHERE kind='MONITOR'").fetchone()[0] == 3


def test_monitor_service_failure_is_a_visible_status(monkeypatch, tmp_path):
    import csp_alerts.jobs.monitor as mm
    j, _ = ledger([dict(trade_id="P1")], path=tmp_path / "j.sqlite3")
    j.close()
    monkeypatch.setattr(mm, "normalize_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    code, out, err = run_cli(monkeypatch, tmp_path, ["monitor"], source=FakeSource({"AAA": [raw_row()]}))
    assert code == 1 and "Traceback" not in err
    st = sqlite3.connect(tmp_path / "j.sqlite3").execute("SELECT status FROM run_status WHERE kind='MONITOR'").fetchall()
    assert [r[0] for r in st] == ["SERVICE_FAILURE"]


# ====================================================================== status / health / heartbeat
def mkdb(tmp_path, positions=None, now=NOW):
    j, clock = ledger(positions or [], path=tmp_path / "j.sqlite3", now=now)
    return j


def rec(j, kind, started, status):
    j.record_run(str(uuid.uuid4()), kind, started, status)


def health(monkeypatch, tmp_path, now):
    return run_cli(monkeypatch, tmp_path, ["health"], now=now)


def test_status_output_shows_last_next_blockers_delivery_and_pin(monkeypatch, tmp_path):
    j = mkdb(tmp_path)
    rec(j, "SCAN", ct(10, 5), "NO_OPPORTUNITIES")
    rec(j, "MONITOR", ct(10, 0), "NO_ALERTS")
    j.close()
    code, out, err = run_cli(monkeypatch, tmp_path, ["status"], now=ct(10, 10))
    assert code == 0
    for needle in ("Last scan: NO_OPPORTUNITIES at 2026-10-30 10:05 AM CDT", "Last monitor: NO_ALERTS at 2026-10-30 10:00 AM CDT",
                   "Next scan (per policy schedule): 2026-10-30 01:05 PM CDT", "Next monitor (per policy schedule): 2026-10-30 10:15 AM CDT",
                   "Current blockers: none", "Last delivery: none yet", "Outbox counts: empty", "Telegram chat pinned: NO", "Health flags: none"):
        assert needle in out, (needle, out)


def test_status_lists_current_blockers_including_latch_and_closed_market(monkeypatch, tmp_path):
    j = mkdb(tmp_path)
    j.add_snapshot(AccountSnapshot("low", NOW - timedelta(minutes=30), D("91000"), D("50000"), True, "v"))
    j.close()
    code, out, _ = run_cli(monkeypatch, tmp_path, ["status"], now=NOW)
    assert "HARD_HALT" in out.split("Current blockers:")[1].splitlines()[0]
    code, out, _ = run_cli(monkeypatch, tmp_path, ["status"], now=datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc))
    assert "MARKET_CLOSED" in out.split("Current blockers:")[1].splitlines()[0]


def test_next_run_times_hand_computed():
    # Fri 2026-10-30 10:05:02 CDT: next scan 13:05 CDT (18:05Z); next monitor boundary 10:15 CDT (15:15Z)
    n = next_runs(NOW, POL, "America/Chicago")
    assert n["scan"] == datetime(2026, 10, 30, 18, 5, tzinfo=timezone.utc) and n["monitor"] == datetime(2026, 10, 30, 15, 15, tzinfo=timezone.utc)
    # after the 13:05 scan on Friday: next trading day Monday 2026-11-02 10:05 CST (UTC-6 after DST ends) = 16:05Z
    n = next_runs(ct(13, 6), POL, "America/Chicago")
    assert n["scan"] == datetime(2026, 11, 2, 16, 5, tzinfo=timezone.utc)
    # weekend: monitor resumes Monday 08:30 CST = 14:30Z
    sat = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    assert next_runs(sat, POL, "America/Chicago")["monitor"] == datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
    # Thanksgiving is skipped: Wed 2026-11-25 14:00 CST -> next scan Fri 2026-11-27 10:05 CST = 16:05Z
    wed = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
    assert next_runs(wed, POL, "America/Chicago")["scan"] == datetime(2026, 11, 27, 16, 5, tzinfo=timezone.utc)


def test_health_clean_exits_zero(monkeypatch, tmp_path):
    j = mkdb(tmp_path)
    rec(j, "SCAN", ct(10, 5), "CANDIDATES")
    j.close()
    code, out, _ = health(monkeypatch, tmp_path, ct(10, 20))
    assert code == 0 and "Health flags: none" in out


@pytest.mark.parametrize("now,flagged", [(ct(10, 50, 0), False), (ct(10, 50, 1), True), (ct(13, 0), True)])
def test_missed_first_scan_of_the_day_is_detected_after_45_minutes_grace(monkeypatch, tmp_path, now, flagged):
    j = mkdb(tmp_path, now=now)
    j.close()
    code, out, _ = health(monkeypatch, tmp_path, now)
    assert ("SCAN_MISSED_TODAY" in out) is flagged and code == int(flagged)


def test_skipped_late_scan_does_not_count_as_a_scan_and_weekends_are_not_flagged(monkeypatch, tmp_path):
    j = mkdb(tmp_path, now=ct(11, 30))
    rec(j, "SCAN", ct(10, 20), "SKIPPED_LATE")
    j.close()
    code, out, _ = health(monkeypatch, tmp_path, ct(11, 30))
    assert "SCAN_MISSED_TODAY" in out and code == 1
    sat = datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)
    c = run_cli(monkeypatch, tmp_path, ["health"], now=sat)
    assert "SCAN_MISSED_TODAY" not in c[1]


@pytest.mark.parametrize("age,overdue", [(timedelta(minutes=35), False), (timedelta(minutes=35, seconds=1), True), (timedelta(minutes=5), False)])
def test_monitor_overdue_only_with_open_positions_in_an_open_market(monkeypatch, tmp_path, age, overdue):
    now = ct(10, 40)
    j = mkdb(tmp_path, [dict(trade_id="P1")], now=now)
    rec(j, "SCAN", ct(10, 5), "NO_OPPORTUNITIES")
    rec(j, "MONITOR", now - age, "NO_ALERTS")
    j.close()
    code, out, _ = health(monkeypatch, tmp_path, now)
    assert ("MONITOR_OVERDUE" in out) is overdue and code == int(overdue)


def test_monitor_never_ran_with_open_position_is_overdue_but_not_without_positions_or_when_closed(monkeypatch, tmp_path):
    now = ct(10, 40)
    j = mkdb(tmp_path, [dict(trade_id="P1")], now=now)
    rec(j, "SCAN", ct(10, 5), "NO_OPPORTUNITIES")
    j.close()
    assert "MONITOR_OVERDUE" in health(monkeypatch, tmp_path, now)[1]
    assert "MONITOR_OVERDUE" not in health(monkeypatch, tmp_path, datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc))[1]       # Saturday
    sub = tmp_path / "np"
    sub.mkdir()
    j2 = mkdb(sub, now=now)
    rec(j2, "SCAN", ct(10, 5), "NO_OPPORTUNITIES")
    j2.close()
    assert "MONITOR_OVERDUE" not in run_cli(monkeypatch, sub, ["health"], now=now)[1]


def test_delivery_problems_and_service_failures_raise_health_flags(monkeypatch, tmp_path):
    now = ct(10, 20)
    j = mkdb(tmp_path, now=now)
    rec(j, "SCAN", ct(10, 5), "SERVICE_FAILURE")
    m = lambda k: AlertMessage("DATA_ISSUE", "WARN", "x", k)  # noqa: E731
    d = SendDecision(Mode.SEND)
    for k in ("a", "b"):
        outbox.enqueue(j, m(k), d, now=now, scope="paper_primary", policy_sha="s")
    j.conn.execute("UPDATE alert_outbox SET status='UNKNOWN_ACK' WHERE rowid=1")
    j.conn.execute("UPDATE alert_outbox SET status='FAILED_FINAL' WHERE rowid=2")
    j.close()
    code, out, _ = health(monkeypatch, tmp_path, now)
    assert code == 1
    for flag in ("DELIVERY_UNKNOWN_ACK", "DELIVERY_FAILED_FINAL", "LAST_SCAN_SERVICE_FAILURE"):
        assert flag in out
    assert "Outbox counts: FAILED_FINAL=1, UNKNOWN_ACK=1" in out


def test_status_command_never_exits_nonzero_for_health_flags_only_health_does(monkeypatch, tmp_path):
    j = mkdb(tmp_path, now=ct(12, 0))
    j.close()
    assert run_cli(monkeypatch, tmp_path, ["status"], now=ct(12, 0))[0] == 0
    assert run_cli(monkeypatch, tmp_path, ["health"], now=ct(12, 0))[0] == 1


def test_heartbeat_line_contains_last_runs_blockers_and_flags_and_no_secrets():
    j = make_journal()
    rec(j, "SCAN", NOW - timedelta(minutes=5), "CANDIDATES")
    line = heartbeat_line(j, POL, NOW, "America/Chicago")
    for needle in ("Heartbeat:", "last scan CANDIDATES", "last monitor never", "blockers:", "flags:"):
        assert needle in line
    assert TOKEN not in line and CHAT not in line


# ====================================================================== Windows scripts: static analysis only
PS = REPO / "ops" / "windows" / "register-tasks.ps1"
UN = REPO / "ops" / "windows" / "unregister-tasks.ps1"


def ps_text(p=PS):
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [PS, UN])
def test_powershell_scripts_parse_without_errors_and_are_not_executed(path):
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           f"$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$t,[ref]$e);$e.Count"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "0", r.stdout + r.stderr


def parse_tasks():
    t = ps_text()
    rows = re.findall(r'@\{ Name="([^"]+)";\s*Args=\(New-CliArgs "([^"]+)"\);\s*Kind="([^"]+)";\s*At="([^"]+)";\s*CatchUp=\$(true|false)', t)
    return {n: dict(cmd=c, kind=k, at=a, catchup=(cu == "true")) for n, c, k, a, cu in rows}


def test_task_table_matches_the_required_schedule():
    t = parse_tasks()
    assert set(t) == {"scan-1005", "scan-1305", "monitor", "monitor-daily", "reconcile-reminder", "heartbeat",
                      "market-refresh"}                                  # Stage A: deliberate addition (data-only refresh task)
    assert t["market-refresh"]["cmd"] == "market-refresh" and t["market-refresh"]["kind"] == "refresh"
    assert (t["scan-1005"]["at"], t["scan-1005"]["kind"]) == ("10:05", "weekdays") and "--scheduled-for 10:05" in t["scan-1005"]["cmd"]
    assert (t["scan-1305"]["at"], t["scan-1305"]["kind"]) == ("13:05", "weekdays") and "--scheduled-for 13:05" in t["scan-1305"]["cmd"]
    assert t["monitor"]["at"] == "08:30" and t["monitor"]["kind"] == "monitor"          # 08:30 CT == 09:30 ET regular open
    assert t["monitor-daily"]["kind"] == "daily"                                         # calendar checks also on weekends/holidays
    assert t["heartbeat"]["at"] == "08:00" and t["reconcile-reminder"]["at"] == "15:30"
    # schedule agrees with policy
    assert POL.market.entry_scans_local == ["10:05", "13:05"] and POL.market.exit_monitor_interval_minutes == 15
    assert POL.market.scheduler_timezone == "America/Chicago"


def test_entry_scans_never_catch_up_after_wake_but_monitors_do():
    t = parse_tasks()
    assert t["scan-1005"]["catchup"] is False and t["scan-1305"]["catchup"] is False
    assert all(t[n]["catchup"] for n in ("monitor", "monitor-daily", "reconcile-reminder", "heartbeat"))
    assert t["market-refresh"]["catchup"] is False                       # old market data is never "replayed"
    text = ps_text()
    assert "StartWhenAvailable = $true" in text and "StartWhenAvailable = $false" in text and "-WakeToRun" in text
    assert "-MultipleInstances IgnoreNew" in text and "-ExecutionTimeLimit (New-TimeSpan -Minutes 10)" in text


def test_monitor_repetition_covers_the_open_through_the_close_every_15_minutes():
    text = ps_text()
    m = re.search(r"-RepetitionInterval \(New-TimeSpan -Minutes (\d+)\) -RepetitionDuration \(New-TimeSpan -Hours (\d+) -Minutes (\d+)\)", text)
    assert m and (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (15, 6, 45)
    # 08:30 + 6h45 = 15:15 CT (last run just after the 15:00 CT close: calendar checks only)
    start, dur = 8 * 60 + 30, 6 * 60 + 45
    assert (start + dur) == 15 * 60 + 15 and start == 8 * 60 + 30


def test_default_mode_is_dry_run_and_send_is_opt_in_only():
    text = ps_text()
    assert re.search(r'\$mode = if \(\$Send\) \{ "--send" \} else \{ "--dry-run" \}', text)
    assert "[switch]$Apply" in text and "[switch]$Send" in text
    for n, t in parse_tasks().items():
        assert "--send" not in t["cmd"], n                                  # literal --send never baked in
        if n == "market-refresh":      # Stage A exemption: no send/dry-run mode exists for it (data tables only)
            assert "$mode" not in t["cmd"] and "--dry-run" not in t["cmd"], t["cmd"]
        else:
            assert "$mode" in t["cmd"], n


def test_nothing_is_registered_without_apply():
    text = ps_text()
    cont = text.index("if (-not $Apply) { continue }")
    for fn in ("New-ScheduledTaskAction", "New-ScheduledTaskTrigger", "Register-ScheduledTask"):
        assert text.index(fn, cont) > cont, fn
        assert fn not in text[:cont], fn                                  # not used anywhere before the Apply gate
    assert text.count("Register-ScheduledTask") == 1
    # destructive script: preview unless -Apply
    u = ps_text(UN)
    assert u.index("Unregister-ScheduledTask") > u.index("if ($Apply)") and "PREVIEW ONLY" in u and "csp-alerts-*" in u


def test_no_secrets_or_privilege_escalation_in_task_definitions():
    text = ps_text()
    assert not re.search(r"token|secret|password|chat_?id|TELEGRAM_|\d{8,10}:[A-Za-z0-9_-]{30,}", text, re.I)
    assert "-RunLevel Limited" in text and "Highest" not in text and "SYSTEM" not in text and "-LogonType Interactive" in text
    for n, t in parse_tasks().items():
        assert re.fullmatch(r"((scan --scheduled-for \d\d:\d\d|monitor|heartbeat|reconcile-reminder) \$mode|market-refresh)", t["cmd"]), (n, t["cmd"])
    assert '--env-file `"$envFile`"' in text


def test_timezone_guard_exists():
    text = ps_text()
    assert 'Get-TimeZone).Id -ne "Central Standard Time"' in text and "IgnoreTimeZone" in text


def hold_lock(db_dir):
    code = ("import sys,time\n"
            "from csp_alerts.lock import single_instance\n"
            "with single_instance(sys.argv[1]):\n"
            "    print('READY', flush=True)\n"
            "    sys.stdin.readline()\n")
    p = subprocess.Popen([sys.executable, "-I", "-c", code, str(db_dir / "locks" / "csp-jobs.lock")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "READY"
    return p


def cli_proc(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME")}
    env["CSP_DATABASE_URL"] = "sqlite:///" + (tmp_path / "j.sqlite3").as_posix()
    env["CSP_DRY_RUN"] = "true"
    ef = tmp_path / "t.env"
    ef.write_text("", encoding="utf-8")
    ap = tmp_path / "approved.json"
    ap.write_text('{"approved_underlyings": []}', encoding="utf-8")
    return subprocess.run([sys.executable, "-I", "-m", "csp_alerts.cli", "--env-file", str(ef), "--policy", str(POLICY_PATH),
                           "--approved", str(ap), *args], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)


@pytest.mark.parametrize("cmd", [["heartbeat"], ["monitor"], ["reconcile-reminder"], ["scan"]])
def test_second_job_instance_exits_3_and_does_nothing_while_the_lock_is_held(tmp_path, cmd):
    holder = hold_lock(tmp_path)
    try:
        r = cli_proc(tmp_path, *cmd)
        assert r.returncode == 3 and "SKIPPED" in r.stderr and "another csp-alerts job" in r.stderr
        assert not (tmp_path / "j.sqlite3").exists() or sqlite3.connect(tmp_path / "j.sqlite3").execute(
            "SELECT COUNT(*) FROM run_status").fetchone()[0] == 0
        r2 = cli_proc(tmp_path, "status")                                    # read-only commands are not blocked by the job lock
        assert r2.returncode in (0, 1) and r2.returncode != 3
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=20)


def test_lock_is_released_when_the_holder_dies_or_finishes(tmp_path):
    holder = hold_lock(tmp_path)
    holder.kill()                                                            # crash: OS releases the lock
    holder.wait(timeout=20)
    time.sleep(0.3)
    r = cli_proc(tmp_path, "heartbeat")
    assert r.returncode == 0, r.stdout + r.stderr


# ====================================================================== backup / restore
def test_backup_is_consistent_includes_uncheckpointed_wal_data_and_no_secrets(monkeypatch, tmp_path):
    j, _ = ledger([dict(trade_id="P1")], path=tmp_path / "j.sqlite3")
    # a held-open connection with committed-but-uncheckpointed WAL pages
    j.conn.execute("PRAGMA wal_autocheckpoint=0")
    q = lambda: j.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]  # noqa: E731
    out_path = tmp_path / "bk" / "copy.sqlite3"
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", str(out_path)], env={"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
    assert code == 0 and "integrity ok" in out and TOKEN not in out + err
    b = sqlite3.connect(out_path)
    assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert b.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0] == 1 and b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == q()
    blob = out_path.read_bytes()
    assert TOKEN.encode() not in blob and CHAT.encode() not in blob


def test_backup_default_location_and_refusal_to_overwrite_the_live_database(monkeypatch, tmp_path):
    j, _ = ledger([dict(trade_id="P1")], path=tmp_path / "j.sqlite3")
    j.close()
    code, out, _ = run_cli(monkeypatch, tmp_path, ["journal", "backup"])
    assert code == 0 and list((tmp_path / "backups").glob("csp-*.sqlite3"))
    live = tmp_path / "j.sqlite3"
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", str(live)])
    chk = Journal(live)
    assert chk.integrity_ok() and len(chk.list_positions()) == 1, "backing up onto the live DB must not damage it"
    chk.close()


def test_restore_keeps_a_latched_halt_and_does_not_replay_old_entries(monkeypatch, tmp_path):
    src = tmp_path / "live"
    src.mkdir()
    j, clock = ledger([], path=src / "j.sqlite3")
    j.add_snapshot(AccountSnapshot("low", NOW - timedelta(hours=1), D("91000"), D("50000"), True, "v"))
    # outbox as of the backup: stale ENTRY (queued, never sent), a SENT row, an UNKNOWN_ACK row
    d = SendDecision(Mode.SEND)
    entry = AlertMessage("ENTRY_RESEARCH", "INFO", "old candidate", "ENTRY_RESEARCH|2026-10-29|AAA|2026-12-04|P|75.00")
    outbox.enqueue(j, entry, d, now=NOW - timedelta(days=1), scope="paper_primary", policy_sha=POL.sha256, expires_in_minutes=30,
                   revalidate={"ticker": "AAA", "approval_sha": "x", "ledger_marker": "m"})
    outbox.enqueue(j, AlertMessage("DATA_ISSUE", "WARN", "sent already", "K-sent"), d, now=NOW - timedelta(days=1), scope="paper_primary", policy_sha="s")
    outbox.enqueue(j, AlertMessage("DATA_ISSUE", "WARN", "ambiguous", "K-unk"), d, now=NOW - timedelta(days=1), scope="paper_primary", policy_sha="s")
    j.conn.execute("UPDATE alert_outbox SET status='SENT' WHERE content_text='sent already'")
    j.conn.execute("UPDATE alert_outbox SET status='UNKNOWN_ACK' WHERE content_text='ambiguous'")
    j.close()
    code, out, _ = run_cli(monkeypatch, src, ["journal", "backup", "--output", str(tmp_path / "bk.sqlite3")])
    assert code == 0
    restored = tmp_path / "restored"
    restored.mkdir()
    shutil.copyfile(tmp_path / "bk.sqlite3", restored / "j.sqlite3")
    fake = FakeTelegram()
    try:
        r = Journal(restored / "j.sqlite3")
        r.migrate()
        from csp_alerts import runner
        rv = runner.make_revalidator(r, POL, lambda: [], lambda: NOW)
        rep = outbox.flush(r, fake.transport(), now=NOW, revalidator=rv)
        assert fake.sends == [] and rep.sent == 0                                # nothing replayed
        st = {x["content_text"]: x["status"] for x in r.conn.execute("SELECT content_text, status FROM alert_outbox")}
        assert st == {"old candidate": "DROPPED_EXPIRED", "sent already": "SENT", "ambiguous": "UNKNOWN_ACK"}
    finally:
        fake.close()
    # the halt survived the restore: drawdown is recomputed from the restored snapshots and entries stay blocked
    res = run_scan(journal=r, policy=POL, approved=[appr("AAA")], source=FakeSource({}), now_utc=NOW, session=OPEN)
    assert res.status == "BLOCKED" and "HARD_HALT" in res.reason_counts
    assert len(r.list_snapshots()) == 2 and r.baseline() is not None
    r.close()


# ====================================================================== reconcile reminder (daily EOD acknowledgement)
def test_reconcile_reminder_fires_when_today_is_not_confirmed_and_stays_quiet_when_it_is(monkeypatch, tmp_path):
    now = ct(15, 30)
    j, clock = ledger([], path=tmp_path / "j.sqlite3", now=ct(8, 0))            # snapshot is from the morning (06:00 CT)... today
    j.close()
    # snapshot dated today but ledger clean -> counts as done only if confirmed and dated today (exchange date)
    code, out, _ = run_cli(monkeypatch, tmp_path, ["reconcile-reminder"], now=now)
    assert code == 0 and ("no reminder needed" in out or "RECONCILIATION REMINDER" in out)
    j = Journal(tmp_path / "j.sqlite3", clock=lambda: now)
    j.create_draft(__import__("csp_alerts.journal.store", fromlist=["x"]).NewPosition("P1", "AAA", date(2026, 12, 4), D("75"), 100, 1, "s", "b"))
    j.transition("P1", __import__("csp_alerts.journal.models", fromlist=["x"]).PositionState.PENDING)    # commitment after the snapshot
    j.close()
    code, out, _ = run_cli(monkeypatch, tmp_path, ["reconcile-reminder"], now=now + timedelta(minutes=1))
    assert code == 0 and "RECONCILIATION REMINDER" in out and "CHANGE_AFTER_SNAPSHOT" in out and "NO ORDER SENT" in out
    assert "DRY-RUN: printed only, nothing sent" in out


def test_reconcile_reminder_is_silent_on_holidays_and_weekends(monkeypatch, tmp_path):
    j, _ = ledger([], path=tmp_path / "j.sqlite3")
    j.close()
    for d in ((2026, 11, 26), (2026, 10, 31), (2026, 12, 25)):
        code, out, _ = run_cli(monkeypatch, tmp_path, ["reconcile-reminder"], now=datetime(*d, 21, 30, tzinfo=timezone.utc))
        assert code == 0 and "not a trading day" in out


def test_stage_a_market_refresh_schedule_two_triggers_never_wakes_pc_and_cannot_send():
    """Schedule sanity (static), DECISIONS item 130. Hand oracle:
    trigger 1: weekdays 07:30, every 5 min for 9h01m -> last firing 07:30 + 9h00m = 16:30 (16:35 would exceed 9h01m): 109 firings.
    trigger 2: daily 00:00, hourly for 23h59m -> 00:00 ... 23:00 (24 firings); the next 00:00 trigger restarts it: no gap.
    Overlap at the top of the hour inside the weekday window is benign: IgnoreNew drops a concurrent start and the job's 60 s floor
    / 5-minute open-session interval means a second start would do no extra provider call. WakeToRun false for this task only."""
    text = ps_text()
    blk = re.search(r'"refresh"\s*\{(.*?)\n    \}', text, re.S).group(1)
    reps = re.findall(r'-At "?\$?[\w.:]+"? -RepetitionInterval \(New-TimeSpan -(Minutes|Hours) (\d+)\) -RepetitionDuration '
                      r'\(New-TimeSpan -Hours (\d+) -Minutes (\d+)\)', blk)
    assert [(u, int(a), int(h), int(m)) for u, a, h, m in reps] == [("Minutes", 5, 9, 1), ("Hours", 1, 23, 59)]
    assert "-Weekly -DaysOfWeek $weekdays" in blk and '-Daily -At "00:00"' in blk and "$trigger = @($t1, $t2)" in blk
    # firing arithmetic
    t1 = [7 * 60 + 30 + 5 * k for k in range(1000) if 5 * k <= 9 * 60 + 1]
    assert t1[0] == 7 * 60 + 30 and t1[-1] == 16 * 60 + 30 and len(t1) == 109
    t2 = [60 * k for k in range(1000) if 60 * k <= 23 * 60 + 59]
    assert t2[0] == 0 and t2[-1] == 23 * 60 and len(t2) == 24
    union = sorted(set(t1) | set(t2))
    gaps = [b - a for a, b in zip(union, union[1:])]
    assert max(gaps) <= 60                                  # never more than an hour without a firing (next day 00:00 restarts)
    assert max(b - a for a, b in zip(t1, t1[1:])) == 5      # 5 min cadence across the whole weekday window incl. 08:30-15:00 CT
    t = parse_tasks()["market-refresh"]
    assert t["at"] == "07:30" and t["catchup"] is False and t["kind"] == "refresh"
    assert not re.search(r"--send|--force|--ticker|token|chat", t["cmd"], re.I)
    assert "-MultipleInstances IgnoreNew" in text
    # WakeToRun: forced off for refresh, still on for every other task
    refresh_settings = re.search(r'if \(\$t\.Kind -eq "refresh"\) \{(.*?)\} else \{(.*?)\n  \}', text, re.S)
    assert "-WakeToRun" not in refresh_settings.group(1) and "$settings.WakeToRun = $false" in refresh_settings.group(1)
    assert "-WakeToRun" in refresh_settings.group(2)
