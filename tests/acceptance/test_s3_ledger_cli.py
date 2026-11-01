"""Slice 3 acceptance: manual-ledger CLI (F2/F3/F4, IT-007, IT-008, IT-012; spec 5; AT-02, AT-07, AT-08, AT-09, AT-12, AT-13).
In-process CLI with a frozen, advancing clock so audit stamps and snapshots are deterministic.

Hand-computed oracles are in the comments (equity 100,000 baseline; AAA 75 put = 7,500 collateral)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from acc_helpers import NOW, POLICY_PATH, TODAY
from s3_helpers import run_cli, write_approved

from csp_alerts.journal.store import Journal

T0 = NOW - timedelta(hours=8)
F3 = "RECONCILIATION REQUIRED (F3)"


class L:
    """Tiny driver: each call advances the clock by `step` minutes."""

    def __init__(self, monkeypatch, tmp_path, step=1):
        self.mp, self.tmp, self.t, self.step = monkeypatch, tmp_path, T0, timedelta(minutes=step)
        self.approved = tmp_path / "approved.json"

    def __call__(self, *args, now=None, apply=False, reason="because", **kw):
        self.t += self.step
        a = list(args)
        if apply:
            a += ["--apply", "--reason", reason]
        code, out, err = run_cli(self.mp, self.tmp, a, now=now or self.t, frozen_journal=True, approved=self.approved, **kw)
        return code, out + err

    def db(self):
        c = sqlite3.connect(self.tmp / "j.sqlite3")
        c.row_factory = sqlite3.Row
        return c

    def n(self, table, where=""):
        c = self.db()
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
        finally:
            c.close()

    def state(self, tid):
        c = self.db()
        try:
            r = c.execute("SELECT state FROM option_positions WHERE trade_id=?", (tid,)).fetchone()
            return None if r is None else r[0]
        finally:
            c.close()

    def blocked_codes(self):
        code, out = self(("reconcile-check"))
        return {w.split()[1] for w in out.splitlines() if w.startswith("BLOCK ")}


@pytest.fixture()
def led(monkeypatch, tmp_path):
    d = L(monkeypatch, tmp_path)
    assert d("journal", "init")[0] == 0
    assert d("journal", "confirm-baseline", "--equity", "100000")[0] == 0
    write_approved(d.approved, ("AAA",), approved_through="2027-06-30")
    return d


def checkin(d, equity="100000", cash="100000"):
    return d("daily-check-in", "--equity", equity, "--free-cash", cash, "--ack-reconciled", apply=True, reason="daily")


def add_trade(d, tid="P1", strike="75.00", expiry="2026-12-04", extra=()):
    return d("journal", "add-trade", "--trade-id", tid, "--ticker", "AAA", "--expiration", expiry, "--strike", strike,
             "--sector", "s", "--bucket", "b", *extra, apply=True, reason="placed at broker")


def fill(d, tid="P1", credit="0.50", fees="0.65"):
    return d("journal", "fill", "--trade-id", tid, "--credit", credit, "--fees", fees, apply=True, reason="filled")


# ====================================================================== preview default / reason / audit / F3
def test_every_command_previews_by_default_and_writes_nothing(led):
    assert checkin(led)[0] == 0
    assert add_trade(led)[0] == 0 and fill(led)[0] == 0
    before = (led.n("audit_log"), led.n("account_snapshots"), led.n("option_positions"), led.n("stock_lots"))
    previews = [
        ("journal", "snapshot", "--equity", "100000", "--free-cash", "90000"),
        ("journal", "add-trade", "--trade-id", "P2", "--ticker", "AAA", "--expiration", "2026-12-11", "--strike", "50", "--sector", "s2", "--bucket", "b2"),
        ("journal", "fill", "--trade-id", "P1", "--credit", "0.5"),
        ("journal", "cancel", "--trade-id", "P1", "--verified"),
        ("journal", "close", "--trade-id", "P1", "--working"),
        ("journal", "close", "--trade-id", "P1", "--debit", "0.2"),
        ("journal", "expire", "--trade-id", "P1", "--confirm-expired"),
        ("journal", "assign", "--trade-id", "P1"),
        ("journal", "reset", "--reviewed"),
        ("journal", "record-cash-flow", "--amount", "-1000", "--date", TODAY.isoformat(), "--note", "wd"),
        ("journal", "approve", "--ticker", "BBB", "--asset-type", "STOCK", "--sector", "s", "--bucket", "b", "--max-buy-strike", "50",
         "--approved-through", "2027-01-01", "--events-reviewed-on", TODAY.isoformat(), "--events-cleared-through", "2027-01-01",
         "--ownership-reason", "thesis"),
        ("daily-check-in", "--equity", "100000", "--free-cash", "90000", "--ack-reconciled"),
    ]
    approved_before = led.approved.read_text(encoding="utf-8")
    for p in previews:
        code, out = led(*p)
        assert code == 0 and "PREVIEW ONLY" in out, (p, out)
    assert (led.n("audit_log"), led.n("account_snapshots"), led.n("option_positions"), led.n("stock_lots")) == before
    assert led.state("P1") == "OPEN" and led.approved.read_text(encoding="utf-8") == approved_before


@pytest.mark.parametrize("cmd", [
    ["journal", "snapshot", "--equity", "100000", "--free-cash", "90000"],
    ["journal", "reset", "--reviewed"],
    ["journal", "record-cash-flow", "--amount", "-1000", "--date", TODAY.isoformat(), "--note", "wd"],
    ["daily-check-in", "--equity", "100000", "--free-cash", "90000", "--ack-reconciled"],
])
def test_apply_without_a_reason_is_refused_and_writes_nothing(led, cmd):
    checkin(led)
    before = led.n("audit_log")
    code, out = led(*cmd, "--apply")
    assert code == 1 and "--reason is required" in out and led.n("audit_log") == before
    code, out = led(*cmd, "--apply", "--reason", "   ")
    assert code == 1 and led.n("audit_log") == before


def test_applied_commands_are_audited_with_actor_reason_and_args(led):
    code, out = led("journal", "add-trade", "--trade-id", "P1", "--ticker", "AAA", "--expiration", "2026-12-04", "--strike", "75",
                    "--sector", "s", "--bucket", "b", "--actor", "tester", apply=True, reason="sold at broker 10:02")
    assert code == 0 and "RECORDED (add-trade) by tester" in out
    c = led.db()
    rows = [dict(r) for r in c.execute("SELECT * FROM audit_log WHERE event_type='MANUAL_COMMAND'")]
    row = [r for r in rows if json.loads(r["after_json"])["command"] == "add-trade"][0]
    after = json.loads(row["after_json"])
    assert row["actor"] == "tester" and after["reason"] == "sold at broker 10:02" and after["args"]["trade_id"] == "P1"
    assert any(r["event_type"] == "POSITION_TRANSITION" and r["actor"] == "tester" for r in c.execute("SELECT * FROM audit_log"))


def test_f3_prompt_after_commitment_changes_and_entries_blocked_until_a_new_check_in(led):
    first = checkin(led)
    assert first[0] == 0 and F3 not in first[1]
    outs = {}
    outs["add"] = add_trade(led)[1]
    outs["fill"] = fill(led)[1]
    outs["closew"] = led("journal", "close", "--trade-id", "P1", "--working", apply=True, reason="x")[1]
    outs["flow"] = led("journal", "record-cash-flow", "--amount", "-100", "--date", TODAY.isoformat(), "--note", "n", apply=True, reason="x")[1]
    for k, o in outs.items():
        assert F3 in o and "daily-check-in" in o, k
    assert "CHANGE_AFTER_SNAPSHOT" in led.blocked_codes()                       # entries stay blocked
    # non-commitment commands do not print the prompt
    assert F3 not in led("journal", "snapshot", "--equity", "100000", "--free-cash", "90000", apply=True, reason="x")[1]
    assert F3 not in checkin(led, cash="92500")[1]
    assert "CHANGE_AFTER_SNAPSHOT" not in led.blocked_codes()


def test_cancel_requires_verified_and_releases_collateral(led):
    checkin(led)
    add_trade(led)
    code, out = led("journal", "cancel", "--trade-id", "P1", apply=True, reason="x")
    assert code == 1 and "--verified" in out and led.state("P1") == "PENDING"
    code, out = led("journal", "cancel", "--trade-id", "P1", "--verified", apply=True, reason="x")
    assert code == 0 and led.state("P1") == "CANCELED" and F3 in out


def test_close_preview_pnl_arithmetic_and_two_step_close(led):
    checkin(led)
    add_trade(led)
    fill(led)                                                         # credit .50, open fees .65
    # realized: (0.50 - 0.20) * 100 * 1 - 0.65 - 1.30 = 28.05
    code, out = led("journal", "close", "--trade-id", "P1", "--debit", "0.20", "--fees", "1.30")
    assert code == 0 and "$28.05" in out and led.state("P1") == "OPEN"
    code, out = led("journal", "close", "--trade-id", "P1", apply=True)                       # neither --working nor --debit
    assert code == 1 and "exactly one" in out
    code, out = led("journal", "close", "--trade-id", "P1", "--working", "--debit", "0.2", apply=True)
    assert code == 1
    assert led("journal", "close", "--trade-id", "P1", "--working", apply=True, reason="order in")[0] == 0 and led.state("P1") == "CLOSE_WORKING"
    c = led.db()
    assert c.execute("SELECT state FROM option_positions").fetchone()[0] == "CLOSE_WORKING"
    assert led("journal", "close", "--trade-id", "P1", "--debit", "0.20", "--fees", "1.30", apply=True, reason="filled")[0] == 0
    assert led.state("P1") == "CLOSED"
    r = led.db().execute("SELECT actual_close_debit_per_share, fees_usd FROM option_positions").fetchone()
    assert (r[0], r[1]) == ("0.20", "1.95")                            # opening fee .65 + closing fee 1.30


def test_expire_needs_confirmation_and_not_before_expiration(led):
    checkin(led)
    add_trade(led, expiry="2026-12-04")
    fill(led)
    code, out = led("journal", "expire", "--trade-id", "P1", apply=True, reason="x")
    assert code == 1 and "--confirm-expired" in out
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", "--date", "2026-12-03", apply=True, reason="x")
    assert code == 1 and led.state("P1") == "OPEN"
    # a future-dated expiry (clock 2026-10-30) cannot be recorded: nothing has happened yet (D3)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", "--date", "2026-12-04", apply=True, reason="x")
    assert code == 1 and led.state("P1") == "OPEN"
    # on the expiration day (clock 2026-12-04 22:00Z, after the close) it is accepted
    on_day = datetime(2026, 12, 4, 22, 0, tzinfo=timezone.utc)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", "--date", "2026-12-04", apply=True, reason="x", now=on_day)
    assert code == 0 and led.state("P1") == "EXPIRED"


@pytest.mark.parametrize("hour_utc,use_date", [(15, True), (16, True), (22, True), (22, False)])
def test_expire_on_expiration_day_flows(led, hour_utc, use_date):
    """Clock 15Z/16Z = 10:00/11:00 CT (before the 21:00Z close; EST), 22Z = after close; with and without --date."""
    checkin(led)
    add_trade(led, expiry="2026-12-04")
    fill(led)
    now = datetime(2026, 12, 4, hour_utc, 0, tzinfo=timezone.utc)
    args = ["journal", "expire", "--trade-id", "P1", "--confirm-expired"] + (["--date", "2026-12-04"] if use_date else [])
    code, out = led(*args, apply=True, reason="x", now=now)
    assert code == 0 and led.state("P1") == "EXPIRED", out
    c = led.db()
    try:
        closed = c.execute("SELECT closed_at_utc FROM option_positions WHERE trade_id='P1'").fetchone()[0]
    finally:
        c.close()
    assert closed is not None and str(closed)[:19] <= now.strftime("%Y-%m-%dT%H:%M:%S"), closed   # never future-dated


def test_expire_without_date_before_expiration_refused(led):
    checkin(led)
    add_trade(led, expiry="2026-12-04")
    fill(led)
    code, out = led("journal", "expire", "--trade-id", "P1", "--confirm-expired", apply=True, reason="x")
    assert code == 1 and led.state("P1") == "OPEN"


# ====================================================================== add-trade rejections / multi-contract
@pytest.mark.parametrize("extra,needle", [
    (["--contracts", "2"], "1 contract"), (["--contracts", "0"], "1 contract"), (["--multiplier", "50"], "100-share"),
])
def test_add_trade_rejects_multi_contract_and_nonstandard(led, extra, needle):
    checkin(led)
    code, out = add_trade(led, extra=extra)
    assert code == 1 and needle in out and led.n("option_positions") == 0


@pytest.mark.parametrize("kw,why", [
    (dict(strike="-5"), "negative"), (dict(strike="0"), "zero"), (dict(strike="abc"), "garbage"), (dict(strike="NaN"), "nan"),
    (dict(expiry="12/04/2026"), "date"), (dict(expiry="2026-02-30"), "date2"),
])
def test_add_trade_rejects_bad_numbers_and_dates(led, kw, why):
    checkin(led)
    code, out = add_trade(led, **kw)
    assert code == 1 and led.n("option_positions") == 0, (why, out)


def test_add_trade_duplicate_id_unknown_ticker_and_draft(led):
    checkin(led)
    assert add_trade(led)[0] == 0
    code, out = add_trade(led)
    assert code == 1 and led.n("option_positions") == 1
    code, out = led("journal", "add-trade", "--trade-id", "P9", "--ticker", "ZZZ", "--expiration", "2026-12-04", "--strike", "10", apply=True, reason="x")
    assert code == 1 and "not in the approved list" in out
    code, out = led("journal", "add-trade", "--trade-id", "P8", "--ticker", "AAA", "--expiration", "2026-12-11", "--strike", "50", "--state", "DRAFT", apply=True, reason="x")
    assert code == 0 and led.state("P8") == "DRAFT" and F3 not in out                        # draft is not a commitment


def test_multi_contract_position_imported_by_csv_freezes_entries(led, tmp_path):
    from csp_alerts.journal.import_csv import COLUMNS
    f = tmp_path / "mc.csv"
    row = ["MC1", "paper_primary", "1", "AAA", "2026-12-04", "50.00", "100", "2", "s", "b", "OPEN", "2026-10-20T15:00:00Z", "0.50", "", "0", "", ""]
    f.write_text(",".join(COLUMNS) + "\n" + ",".join(row) + "\n", encoding="utf-8")
    checkin(led)
    code, out = led("journal", "import-csv", str(f))
    assert code == 0
    checkin(led, cash="90000")                                                             # fresh snapshot: only multi-contract remains
    assert "MULTI_CONTRACT_POSITION" in led.blocked_codes()
    # collateral stays visible: 2 x 100 x 50 = 10,000
    code, out = led("account-check")
    assert json.JSONDecoder().raw_decode(out[out.index("{"):])[0]["put_collateral_usd"] == "10000.00"


def test_duplicate_csv_import_is_idempotent(led, tmp_path):
    from csp_alerts.journal.import_csv import COLUMNS
    f = tmp_path / "d.csv"
    row = ["D1", "paper_primary", "1", "AAA", "2026-12-04", "75.00", "100", "1", "s", "b", "OPEN", "2026-10-20T15:00:00Z", "0.50", "", "0.65", "", ""]
    f.write_text(",".join(COLUMNS) + "\n" + ",".join(row) + "\n", encoding="utf-8")
    assert led("journal", "import-csv", str(f))[0] == 0
    snapshot = (led.n("option_positions"), led.n("audit_log", "WHERE event_type='POSITION_IMPORTED'"))
    code, out = led("journal", "import-csv", str(f))
    assert code == 1 and "already exists" in out
    assert (led.n("option_positions"), led.n("audit_log", "WHERE event_type='POSITION_IMPORTED'")) == snapshot
    assert led.db().execute("SELECT strike, state FROM option_positions").fetchone()[:] == ("75.00", "OPEN")


# ====================================================================== daily check-in (F2)
def test_daily_check_in_preview_shows_positions_and_expected_collateral(led):
    checkin(led)
    add_trade(led)
    fill(led)
    code, out = led("daily-check-in")
    assert code == 0
    for needle in ("P1: AAA 2026-12-04 PUT $75.00 x1 [OPEN]", "collateral $7,500.00", "Expected put collateral reserved: $7,500.00",
                   "NET free unencumbered cash", "Last snapshot:"):
        assert needle in out, needle


@pytest.mark.parametrize("cash,ok", [("92500", True), ("92500.01", False), ("100000", False)])
def test_daily_check_in_rejects_cash_inconsistent_with_equity(led, cash, ok):
    checkin(led)
    add_trade(led)
    fill(led)                                                                   # 7,500 reserved: cash + 7,500 must be <= equity
    code, out = led("daily-check-in", "--equity", "100000", "--free-cash", cash, "--ack-reconciled", apply=True, reason="x")
    assert (code == 0) is ok
    if not ok:
        assert "CASH_INCONSISTENT" in out and led.n("account_snapshots") == 1


def test_daily_check_in_requires_acknowledgement_to_apply(led):
    checkin(led)
    code, out = led("daily-check-in", "--equity", "100000", "--free-cash", "90000", apply=True, reason="x")
    assert code == 1 and "--ack-reconciled" in out and led.n("account_snapshots") == 1
    for bad in (("0", "0"), ("-5", "1"), ("100", "-1"), ("abc", "1"), ("NaN", "1")):
        code, out = led("daily-check-in", "--equity", bad[0], "--free-cash", bad[1], "--ack-reconciled", apply=True, reason="x")
        assert code == 1 and led.n("account_snapshots") == 1, bad


def test_daily_check_in_clean_message_and_stock_marks(led):
    checkin(led)
    add_trade(led)
    fill(led)
    code, out = led("daily-check-in", "--equity", "100000", "--free-cash", "92500", "--ack-reconciled", apply=True, reason="x")
    assert code == 0 and "CLEAN" in out and led.blocked_codes() == set()


def test_stale_state_is_reported_as_still_blocked_when_other_problems_remain(led):
    checkin(led)
    add_trade(led)
    fill(led)
    led("journal", "assign", "--trade-id", "P1", apply=True, reason="assigned")
    code, out = led("daily-check-in", "--equity", "100000", "--free-cash", "100000", "--ack-reconciled", apply=True, reason="x")
    assert code == 0 and "STILL BLOCKED" in out and "ASSIGNMENT_PENDING" in out


# ====================================================================== assignment (F4)
def assigned_pending(led):
    checkin(led)
    add_trade(led)
    fill(led)
    code, out = led("journal", "assign", "--trade-id", "P1", apply=True, reason="notified")
    assert code == 0 and led.state("P1") == "ASSIGNED_PENDING_RECONCILIATION" and F3 in out


def test_assignment_flow_end_to_end_counts_exposure_once_and_unfreezes_only_after_check_in(led):
    assigned_pending(led)
    assert "ASSIGNMENT_PENDING" in led.blocked_codes()
    code, out = led("journal", "assign", "--trade-id", "P1")                                  # step-2 guidance
    assert code == 0 and "still owe the share/cash record" in out and "$7,500.00" in out
    code, out = led("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500", apply=True, reason="shares received")
    assert code == 0 and led.state("P1") == "ASSIGNED_RECONCILED" and led.n("stock_lots") == 1 and F3 in out
    code, out = led("account-check")
    j = json.JSONDecoder().raw_decode(out[out.index("{"):])[0]
    assert j["put_collateral_usd"] == "0" or D(j["put_collateral_usd"]) == 0
    assert D(j["aggregate_equity_risk_usd"]) == D("7500")                                     # 100 sh x $75 once, not 15,000
    assert "CHANGE_AFTER_SNAPSHOT" in led.blocked_codes()                                     # step 3 (check-in) still needed
    code, out = led("daily-check-in", "--equity", "100000", "--free-cash", "92500", "--ack-reconciled", "--mark", "LOT-P1=75", apply=True, reason="step 3")
    assert code == 0 and "CLEAN" in out and led.blocked_codes() == set()


@pytest.mark.parametrize("extra,needle", [
    (["--shares", "50", "--cost-basis-usd", "3750"], "contracts x multiplier"),
    (["--shares", "100"], "cost-basis-usd"),
    (["--shares", "100", "--cost-basis-usd", "0"], "cash paid"),
    (["--shares", "100", "--cost-basis-usd", "-7500"], "cash paid"),
    (["--shares", "-100", "--cost-basis-usd", "7500"], "positive"),
    (["--shares", "100", "--cost-basis-usd", "7500", "--price", "0"], "positive"),
])
def test_assignment_step2_validation_changes_nothing(led, extra, needle):
    assigned_pending(led)
    code, out = led("journal", "assign", "--trade-id", "P1", *extra, apply=True, reason="x")
    assert code == 1 and led.state("P1") == "ASSIGNED_PENDING_RECONCILIATION" and led.n("stock_lots") == 0, out


def test_assignment_step2_before_step1_and_twice_are_rejected(led):
    checkin(led)
    add_trade(led)
    fill(led)
    # step 2 arguments on an OPEN position are treated as step 1 only (no lot can be created without the assignment)
    code, out = led("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500", apply=True, reason="x")
    assert led.n("stock_lots") == 0
    assigned = led.state("P1")
    assert assigned in ("ASSIGNED_PENDING_RECONCILIATION", "OPEN")
    if assigned == "ASSIGNED_PENDING_RECONCILIATION":
        assert led("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500", apply=True, reason="x")[0] == 0
    code, out = led("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500", apply=True, reason="again")
    assert led.n("stock_lots") <= 1


def test_assignment_step2_is_atomic_when_a_failure_hits_midway(led, monkeypatch):
    assigned_pending(led)
    before_audit = led.n("audit_log")
    orig = Journal._audit
    calls = {"n": 0}

    def boom(self, event_type, *a, **k):
        calls["n"] += 1
        raise RuntimeError("disk full")                      # first audit write inside the transaction fails
    monkeypatch.setattr(Journal, "_audit", boom)
    code, out = led("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500", apply=True, reason="x")
    monkeypatch.setattr(Journal, "_audit", orig)
    assert code != 0 and calls["n"] >= 1
    assert led.state("P1") == "ASSIGNED_PENDING_RECONCILIATION", "state must roll back with the lot"
    assert led.n("stock_lots") == 0 and led.n("audit_log") == before_audit
    c = led.db()
    assert c.execute("SELECT COUNT(*) FROM option_positions WHERE state='ASSIGNED_RECONCILED'").fetchone()[0] == 0
    # obligation still visible and entries still frozen
    assert "ASSIGNMENT_PENDING" in led.blocked_codes()
    # retry after the fault succeeds exactly once
    code, out = led("journal", "assign", "--trade-id", "P1", "--shares", "100", "--cost-basis-usd", "7500", apply=True, reason="retry")
    assert code == 0 and led.n("stock_lots") == 1 and led.state("P1") == "ASSIGNED_RECONCILED"


# ====================================================================== cash flows (IT-008) with hand-computed examples
def acct(led):
    code, out = led("account-check")
    j, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
    return j


def snap(led, eq, cash=None):
    return led("journal", "snapshot", "--equity", eq, "--free-cash", cash or eq, apply=True, reason="s")


def flow(led, amount, note="platform history"):
    return led("journal", "record-cash-flow", "--amount", amount, "--date", TODAY.isoformat(), "--note", note, apply=True, reason="f")


def test_withdrawal_is_not_a_loss(led):
    # baseline 100,000; snapshot 100,000; withdraw 10,000 (recorded BEFORE the check-in that includes it); snapshot 90,000
    # peak = 100,000 - 10,000 = 90,000 -> drawdown 0, level NONE.   Without the flow: (100,000-90,000)/100,000 = 10% -> HARD_HALT.
    snap(led, "100000")
    assert flow(led, "-10000")[0] == 0
    snap(led, "90000")
    a = acct(led)
    assert a["drawdown_level"] == "NONE" and D(a["drawdown"]) == 0 and a["latched"] is False


def test_same_equity_drop_without_a_flow_latches_halt_control_case(led):
    snap(led, "100000")
    snap(led, "90000")
    assert acct(led)["drawdown_level"] == "HARD_HALT"


def test_deposit_does_not_hide_a_loss(led):
    # deposit 10,000 -> peak 110,000; equity 105,000 is 5,000 below it: 5,000/110,000 = 4.545% -> PAUSE
    snap(led, "100000")
    assert flow(led, "10000")[0] == 0
    snap(led, "105000")
    a = acct(led)
    assert a["drawdown_level"] == "PAUSE" and abs(D(a["drawdown"]) - D("0.0454545")) < D("0.000001")


def test_cash_flow_can_never_clear_or_downgrade_a_latched_level(led):
    snap(led, "100000")
    snap(led, "96900")                                                   # 3.1% -> PAUSE latched
    assert acct(led)["drawdown_level"] == "PAUSE"
    flow(led, "-5000")                                                   # peak -> 95,000: equity 96,900 is above peak
    snap(led, "96900")
    a = acct(led)
    assert a["drawdown_level"] == "PAUSE" and a["latched"] is True, "withdrawal must not clear the latch"
    flow(led, "-100000")                                                 # peak floored at zero, then lifted to equity
    snap(led, "96900")
    assert acct(led)["drawdown_level"] == "PAUSE"


def test_a_flow_cannot_retroactively_change_snapshots_already_taken(led):
    snap(led, "100000")
    snap(led, "90000")                                                   # HALT latched at this snapshot
    flow(led, "-10000")                                                  # recorded AFTER the snapshot that already contains it
    assert acct(led)["drawdown_level"] == "HARD_HALT"


def test_cash_flow_validation_preview_and_ledger_effect(led):
    snap(led, "100000")
    code, out = led("journal", "record-cash-flow", "--amount", "-10000", "--date", TODAY.isoformat(), "--note", "wd")
    assert code == 0 and "PREVIEW ONLY" in out and "Drawdown peak" in out and led.n("audit_log", "WHERE event_type='CASH_FLOW'") == 0
    for amount, date_, note in (("0", TODAY.isoformat(), "n"), ("abc", TODAY.isoformat(), "n"), ("NaN", TODAY.isoformat(), "n"),
                                ("-1", "2030-01-01", "n"), ("-1", "bad", "n"), ("-1", TODAY.isoformat(), "  ")):
        code, out = led("journal", "record-cash-flow", "--amount", amount, "--date", date_, "--note", note, apply=True, reason="x")
        assert code == 1 and led.n("audit_log", "WHERE event_type='CASH_FLOW'") == 0, (amount, date_, note)
    assert flow(led, "-10000")[0] == 0
    assert "CHANGE_AFTER_SNAPSHOT" in led.blocked_codes()                    # a cash flow is a ledger change: re-confirm


# ====================================================================== reset is the only way to clear
def test_only_reviewed_reset_clears_a_latch_and_it_rebaselines(led):
    snap(led, "100000")
    snap(led, "91000")
    snap(led, "99900")                                                   # rebound
    assert acct(led)["drawdown_level"] == "HARD_HALT"
    flow(led, "5000")
    checkin(led, "99900", "99900")
    assert acct(led)["drawdown_level"] == "HARD_HALT"                    # flow + new snapshot + restart: still halted
    code, out = led("journal", "reset", apply=True, reason="reviewed account")
    assert code == 1 and "--reviewed is required" in out and acct(led)["drawdown_level"] == "HARD_HALT"
    code, out = led("journal", "reset", "--reviewed", apply=True, reason="reviewed account, accept resuming")
    assert code == 0 and acct(led)["drawdown_level"] == "NONE"
    assert any(json.loads(r["after_json"])["command"] == "reset" for r in led.db().execute("SELECT * FROM audit_log WHERE event_type='MANUAL_COMMAND'"))
    snap(led, "96000")                                                   # new decline measured from the NEW baseline (99,900 + flow)
    assert acct(led)["drawdown_level"] != "NONE"


# ====================================================================== approve
def approve_args(**over):
    d = dict(ticker="BBB", asset_type="STOCK", sector="s", bucket="b", max_buy_strike="50", approved_through="2027-06-30",
             events_reviewed_on=TODAY.isoformat(), events_cleared_through="2027-06-30", ownership_reason="long-term thesis")
    d.update(over)
    out = ["journal", "approve"]
    for k, v in d.items():
        out += ["--" + k.replace("_", "-"), v]
    return out


def test_approve_preview_apply_backup_and_replace(led):
    original = led.approved.read_text(encoding="utf-8")
    code, out = led(*approve_args())
    assert code == 0 and "PREVIEW ONLY" in out and "NONSTANDARD_CONTRACT" in out and led.approved.read_text(encoding="utf-8") == original
    code, out = led(*approve_args(), apply=True, reason="reviewed 10-K")
    assert code == 0
    data = json.loads(led.approved.read_text(encoding="utf-8"))
    assert [a["ticker"] for a in data["approved_underlyings"]] == ["AAA", "BBB"]
    assert led.approved.with_suffix(".json.bak").read_text(encoding="utf-8") == original
    assert not led.approved.with_suffix(".json.tmp").exists()
    from csp_alerts.policy.approved import load_approved
    assert {a.ticker for a in load_approved(led.approved)} == {"AAA", "BBB"}
    first = led.approved.read_text(encoding="utf-8")
    code, out = led(*approve_args(max_buy_strike="45"), "--standard-deliverable-confirmed-on", TODAY.isoformat(), apply=True, reason="lower ceiling")
    assert code == 0 and "UPDATE" in out
    data = json.loads(led.approved.read_text(encoding="utf-8"))
    assert [a["ticker"] for a in data["approved_underlyings"]].count("BBB") == 1
    assert led.approved.with_suffix(".json.bak").read_text(encoding="utf-8") == first


@pytest.mark.parametrize("over", [
    dict(ticker="EXAMPLE"), dict(ownership_reason="Fictional example only"), dict(ownership_reason="SYNTHETIC test data"),
    dict(ownership_reason="placeholder"), dict(max_buy_strike="-5"), dict(max_buy_strike="NaN"), dict(approved_through="12/31/2027"),
    dict(asset_type="CRYPTO"), dict(ticker="bad ticker!"), dict(ownership_reason=""),
])
def test_approve_rejects_synthetic_or_invalid_content_and_never_writes(led, over):
    original = led.approved.read_text(encoding="utf-8")
    code, out = led(*approve_args(**over), apply=True, reason="x")
    assert code == 1 and led.approved.read_text(encoding="utf-8") == original and not led.approved.with_suffix(".json.bak").exists()
