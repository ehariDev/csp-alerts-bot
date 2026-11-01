"""Item A(7): the two test_s3_ledger_cli failures after the wall clock crossed into Saturday.

Finding: the TESTS froze only cli._now(); `reconcile-check` (cli.cmd_reconcile_check) calls datetime.now(timezone.utc) directly,
so it used the REAL clock. The product-side inconsistency is pinned below as a strict xfail (it flips to a failure the day the
command honours the seam, forcing removal of the marker). The test-side fix is in s3_helpers.run_cli (freezes that name too).

Hand oracle (SR-10, reconcile.py contract): snapshot taken Fri 2026-10-30 15:05:02Z (= 11:05 EDT, after the Thu close).
  now = Fri 15:05Z -> previous session close = Thu 2026-10-29 16:00 EDT = 20:00Z < snapshot -> no DAILY_CHECKIN_MISSING
  now = Sat 15:05Z -> previous close = Fri 20:00Z > snapshot -> DAILY_CHECKIN_MISSING
  now = Mon 11-02 15:05Z -> previous close = Fri 20:00Z > snapshot -> DAILY_CHECKIN_MISSING
"""
from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, POLICY_PATH
from s3_helpers import _frozen_datetime_class, run_cli

from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal
from csp_alerts.policy.config import load_policy

SAT = datetime(2026, 10, 31, 15, 5, 2, tzinfo=timezone.utc)
MON = datetime(2026, 11, 2, 15, 5, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("now,expect_missing", [(NOW, False), (SAT, True), (MON, True)])
def test_daily_checkin_rule_follows_the_injected_clock_fri_sat_mon(now, expect_missing):
    j = Journal(":memory:", clock=lambda: NOW)
    j.migrate()
    j.confirm_baseline(D("100000"))
    j.add_snapshot(AccountSnapshot("s", NOW, D("100000"), D("90000"), True, "v1"))
    rep = reconcile(j.load_book(), load_policy(POLICY_PATH), now)
    codes = {i.code for i in rep.issues}
    assert ("DAILY_CHECKIN_MISSING" in codes) is expect_missing, (now, codes)


def _reconcile_check(monkeypatch, tmp_path, now_seam, wall):
    import csp_alerts.cli as cli
    monkeypatch.setattr(cli, "_now", lambda: now_seam)
    monkeypatch.setattr(cli, "datetime", _frozen_datetime_class(wall))
    ef = tmp_path / "t.env"
    ap = tmp_path / "approved.json"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(["--env-file", str(ef), "--policy", str(POLICY_PATH), "--approved", str(ap), "reconcile-check"])
    return code, out.getvalue()


@pytest.mark.parametrize("wall", [SAT, MON])
def test_reconcile_check_uses_the_injected_clock_not_the_wall_clock(monkeypatch, tmp_path, wall):
    c, o, e = run_cli(monkeypatch, tmp_path, ["journal", "init"], now=NOW, frozen_journal=True)
    assert c == 0, o + e
    c, o, e = run_cli(monkeypatch, tmp_path, ["journal", "confirm-baseline", "--equity", "100000"],
                      now=NOW, frozen_journal=True)
    assert c == 0, o + e
    c, o, e = run_cli(monkeypatch, tmp_path, ["daily-check-in", "--equity", "100000", "--free-cash", "100000", "--ack-reconciled",
                                              "--apply", "--reason", "t"], now=NOW, frozen_journal=True)
    assert c == 0, o + e
    c_ok, out_ok = _reconcile_check(monkeypatch, tmp_path, NOW, NOW)
    assert "DAILY_CHECKIN_MISSING" not in out_ok, out_ok
    c2, out2 = _reconcile_check(monkeypatch, tmp_path, NOW, wall)       # same injected clock, different REAL clock
    assert (c2, out2) == (c_ok, out_ok), out2
