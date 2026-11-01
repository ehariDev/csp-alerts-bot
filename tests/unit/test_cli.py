"""CLI smoke tests (no network, tmp databases)."""
from __future__ import annotations

from pathlib import Path

from csp_alerts.cli import main

ROOT = Path(__file__).resolve().parents[2]
POLICY = str(ROOT / "config" / "policy.yaml")
APPROVED = str(ROOT / "config" / "approved.json")


def env_file(tmp_path, extra=""):
    f = tmp_path / ".env"
    f.write_text(f"CSP_DATABASE_URL=sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}\n{extra}", encoding="utf-8")
    return str(f)


def base(tmp_path, extra=""):
    return ["--env-file", env_file(tmp_path, extra), "--policy", POLICY, "--approved", APPROVED]


def test_doctor_ok_then_with_journal(tmp_path, capsys):
    assert main(base(tmp_path) + ["doctor"]) == 0
    assert main(base(tmp_path) + ["journal", "init"]) == 0
    assert main(base(tmp_path) + ["doctor"]) == 0
    out = capsys.readouterr().out
    assert "OK policy" in out and "0 approved underlying" in out and "dry_run=True" in out and "integrity=ok" in out


def test_doctor_fails_when_broker_access_enabled(tmp_path, capsys):
    rc = main(base(tmp_path, "CSP_DISABLE_BROKER_ACCESS=false\n") + ["doctor"])
    assert rc == 2 and "must be true" in capsys.readouterr().out


def test_policy_check_and_approved_list(tmp_path, capsys):
    assert main(base(tmp_path) + ["policy-check"]) == 0
    assert main(base(tmp_path) + ["approved-list"]) == 0
    out = capsys.readouterr().out
    assert "sha256=" in out and "EMPTY" in out


def test_reconcile_check_blocks_on_empty_ledger(tmp_path, capsys):
    assert main(base(tmp_path) + ["journal", "init"]) == 0
    assert main(base(tmp_path) + ["reconcile-check"]) == 1
    assert "NO_SNAPSHOT" in capsys.readouterr().out
    assert main(base(tmp_path) + ["account-check"]) == 0


def test_import_dry_run_via_cli(tmp_path, capsys):
    csv_path = tmp_path / "t.csv"
    from csp_alerts.journal.import_csv import COLUMNS
    csv_path.write_text(",".join(COLUMNS) + "\n" +
                        "T1,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,DRAFT,,,,0,,\n", encoding="utf-8")
    assert main(base(tmp_path) + ["journal", "init"]) == 0
    assert main(base(tmp_path) + ["journal", "import-csv", str(csv_path), "--dry-run"]) == 0
    assert "DRY-RUN" in capsys.readouterr().out
    out = tmp_path / "e.csv"
    assert main(base(tmp_path) + ["journal", "export-csv", "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8").count("\n") == 1  # header only: dry-run wrote nothing


def test_confirm_baseline_command_and_reminders(tmp_path, capsys):
    assert main(base(tmp_path) + ["journal", "init"]) == 0
    assert main(base(tmp_path) + ["reconcile-check"]) == 1
    assert main(base(tmp_path) + ["journal", "confirm-baseline", "--equity", "100000"]) == 0
    assert main(base(tmp_path) + ["journal", "confirm-baseline", "--equity", "90000"]) == 1  # once only
    assert main(base(tmp_path) + ["journal", "confirm-baseline", "--equity", "abc"]) == 2
    capsys.readouterr()
    assert main(base(tmp_path) + ["account-check"]) == 0
    cap = capsys.readouterr()
    assert "NET free unencumbered cash" in cap.err and '"baseline_equity_usd": "100000"' in cap.out
    assert main(base(tmp_path) + ["doctor"]) == 0
    assert "NET free unencumbered cash" in capsys.readouterr().out


def test_alert_preview_and_calendar_check_and_scan_dry_run(tmp_path, capsys):
    fx = str(ROOT / "tests" / "fixtures" / "entry.json")
    assert main(base(tmp_path) + ["alert-preview", "--fixture", fx]) == 0
    out = capsys.readouterr().out
    assert "NO ORDER SENT" in out and "36 DTE" in out and "DRY-RUN" in out
    assert main(base(tmp_path) + ["calendar-check", "--dry-run", "--at", "2026-11-26T16:00:00Z"]) == 0
    assert '"status": "CLOSED"' in capsys.readouterr().out
    assert main(base(tmp_path) + ["calendar-check", "--at", "2026-11-27T17:30:00Z"]) == 0
    assert '"early_close": true' in capsys.readouterr().out
    # scan is dry-run only; with a fresh empty journal it blocks (ledger/calendar) without any network access
    assert main(base(tmp_path) + ["scan", "--dry-run"]) == 0
    assert "status=BLOCKED" in capsys.readouterr().out


def test_alert_preview_blocked_fixture_prints_reasons(tmp_path, capsys):
    import json
    d = json.loads((ROOT / "tests" / "fixtures" / "entry.json").read_text())
    d["observation"]["delta"] = "-0.5"
    f = tmp_path / "f.json"
    f.write_text(json.dumps(d))
    assert main(base(tmp_path) + ["alert-preview", "--fixture", str(f)]) == 1
    assert "DELTA_OUT_OF_RANGE" in capsys.readouterr().out
