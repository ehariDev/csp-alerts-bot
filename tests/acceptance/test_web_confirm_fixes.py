"""Confirmation pass: dashboard import graph, startup banner under a pipe, and event-time clamping for expire/assign.

Expected values hand-derived (no reading of src first):
  2026-12-04 is EST (UTC-5) and CST (UTC-6). 10:00 CT = 16:00Z; 21:00Z of the exchange date is 16:00 ET (session close).
  At 16:00Z the 21:00Z stamp is 5h in the FUTURE -> must be clamped to the journal clock 16:00Z.
  After the close, 22:30Z (17:30 ET): 21:00Z of Dec 4 is in the past -> stored as 21:00Z.
  02:00Z Dec 5 = 21:00 ET Dec 4 -> exchange date is still Dec 4 -> 21:00Z Dec 4.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from datetime import datetime, timezone

import pytest
from s3_helpers import write_approved
from test_s3_ledger_cli import L, add_trade, checkin, fill
from test_web_boundary_and_cli import REPO, _clean_env, _env, _free_port
from web_helpers import PW, scrypt_hash

UTC = timezone.utc


def _z(*a):
    return datetime(*a, tzinfo=UTC)


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------------------------------- import graph
def test_dashboard_server_import_graph_is_clean_in_isolated_interpreter():
    code = (
        "import sys, csp_dashboard.server\n"
        "names = ('urllib.request','csp_alerts.runner','csp_alerts.jobs.scan','csp_alerts.jobs.monitor','csp_alerts.cli',"
        "'csp_alerts.ledger_cli','csp_alerts.market.stockpile','requests','httpx','subprocess')\n"
        "bad = [m for m in sys.modules if m in names or m.startswith('csp_alerts.alerts.telegram')]\n"
        "print('BAD', bad); sys.exit(1 if bad else 0)\n")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------------------- banner under a pipe
def test_startup_banner_visible_promptly_when_stdout_is_a_pipe(tmp_path):
    port = _free_port()
    envf = _env(tmp_path, scrypt_hash(PW), tmp_path / "j.sqlite3", port)
    write_approved(tmp_path / "a.json", ())
    pr = subprocess.Popen([sys.executable, "-m", "csp_dashboard", "--env-file", str(envf), "--approved", str(tmp_path / "a.json"), "start"],
                          cwd=str(REPO), env=_clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: q.put(pr.stdout.readline()), daemon=True).start()
    try:
        line = q.get(timeout=30)           # server is still running; only a flushed line can arrive
        assert pr.poll() is None
        assert "listening on http://127.0.0.1:" in line and str(port) in line
    finally:
        pr.kill()
        pr.communicate()


# --------------------------------------------------------------------------------------- expire / assign clamping
@pytest.fixture()
def led(monkeypatch, tmp_path):
    d = L(monkeypatch, tmp_path)
    assert d("journal", "init")[0] == 0
    assert d("journal", "confirm-baseline", "--equity", "100000")[0] == 0
    write_approved(d.approved, ("AAA",), approved_through="2027-06-30")
    return d


def _closed_at(led):
    return led.db().execute("SELECT closed_at_utc FROM option_positions").fetchone()[0]


def _open_p1(led, expiry="2026-12-04"):
    checkin(led)
    assert add_trade(led, expiry=expiry)[0] == 0
    assert fill(led)[0] == 0


def test_expire_at_10am_ct_on_expiration_day_is_not_future_dated(led):
    _open_p1(led)
    now = _z(2026, 12, 4, 16, 0)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", apply=True, reason="x", now=now)
    assert code == 0, out
    assert led.state("P1") == "EXPIRED"
    assert _parse(_closed_at(led)) == now          # clamped to the journal clock, not 21:00Z


def test_expire_after_close_stores_2100z_of_exchange_date(led):
    _open_p1(led)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", apply=True, reason="x", now=_z(2026, 12, 4, 22, 30))
    assert code == 0, out
    assert _parse(_closed_at(led)) == _z(2026, 12, 4, 21, 0)


def test_expire_at_2100_et_uses_exchange_date_not_utc_date(led):
    _open_p1(led)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", apply=True, reason="x", now=_z(2026, 12, 5, 2, 0))
    assert code == 0, out
    assert _parse(_closed_at(led)) == _z(2026, 12, 4, 21, 0)


def test_expire_explicit_later_date_is_still_refused_by_state_machine(led):
    _open_p1(led)
    now = _z(2026, 12, 1, 16, 0)
    for d in ("2026-12-03", "2026-12-02"):          # later than today but before expiration -> refused
        code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", "--date", d, apply=True, reason="x", now=now)
        assert code == 1 and led.state("P1") == "OPEN", (d, out)


def test_expire_typed_with_expiration_date_days_early_is_not_accepted_or_future_dated(led):
    """Strict expectation: 3 days before expiry nothing may be recorded as expired (and never with a closed_at after the clock)."""
    _open_p1(led)
    now = _z(2026, 12, 1, 16, 0)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", "--date", "2026-12-04", apply=True, reason="x", now=now)
    ca = _closed_at(led)
    assert not (ca is not None and _parse(ca) > now), f"future closed_at stored: {ca}"
    assert led.state("P1") == "OPEN" and code == 1, (code, out)


def test_expire_before_expiration_day_without_date_is_refused(led):
    _open_p1(led)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", apply=True, reason="x", now=_z(2026, 12, 3, 16, 0))
    assert code == 1 and led.state("P1") == "OPEN"


def test_assign_step1_preview_and_apply_at_10am_ct(led):
    _open_p1(led)
    now = _z(2026, 12, 4, 16, 0)
    code, out = led("journal", "assign", "--trade-id", "P1", now=now)
    assert code == 0 and "2026-12-04" in out and "PREVIEW ONLY" in out
    code, out = led("journal", "assign", "--trade-id", "P1", apply=True, reason="notified", now=now)
    assert code == 0 and led.state("P1") == "ASSIGNED_PENDING_RECONCILIATION", out


def test_assign_step1_after_close_succeeds(led):
    _open_p1(led)
    code, out = led("journal", "assign", "--trade-id", "P1", apply=True, reason="notified", now=_z(2026, 12, 4, 22, 30))
    assert code == 0 and led.state("P1") == "ASSIGNED_PENDING_RECONCILIATION", out


@pytest.mark.parametrize("now,expected", [
    (_z(2026, 12, 4, 16, 0), _z(2026, 12, 4, 16, 0)),        # 10:00 CT: clamped to clock
    (_z(2026, 12, 4, 22, 30), _z(2026, 12, 4, 21, 0)),       # after 16:00 ET: session close stamp
])
def test_assign_step2_lot_reference_time(led, now, expected):
    _open_p1(led)
    assert led("journal", "assign", "--trade-id", "P1", apply=True, reason="n", now=now)[0] == 0
    code, out = led("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500", apply=True, reason="s", now=now)
    assert code == 0 and led.state("P1") == "ASSIGNED_RECONCILED", out
    ref = led.db().execute("SELECT reference_asof_utc FROM stock_lots").fetchone()[0]
    assert _parse(ref) == expected
    assert _parse(ref) <= now
