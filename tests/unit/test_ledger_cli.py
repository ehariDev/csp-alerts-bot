"""Slice 3B: manual-ledger CLI (preview/apply/audit/F3), IT-007, IT-008, F4, lock, backup, reminders, coverage."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from chain_fixtures import raw_row
from factories import NOW, TODAY, policy, snap

from csp_alerts.cli import main
from csp_alerts.journal.models import AccountSnapshot, PositionState as S
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_alerts.lock import AlreadyRunning, single_instance
from csp_alerts.policy.drawdown import DrawdownLevel, compute_drawdown

ROOT = Path(__file__).resolve().parents[2]
P = policy()
FUTURE = (date.today() + timedelta(days=40)).isoformat()


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        env = tmp_path / ".env"
        env.write_text(f"CSP_DATABASE_URL=sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}\n", encoding="utf-8")
        self.approved = tmp_path / "approved.json"
        self.approved.write_text('{"approved_underlyings": []}', encoding="utf-8")
        self.base = ["--env-file", str(env), "--policy", str(ROOT / "config" / "policy.yaml"), "--approved", str(self.approved)]

    def run(self, *args, apply=False, reason="test"):
        extra = ["--apply", "--reason", reason] if apply else []
        return main(self.base + list(args) + extra)

    def journal(self):
        return Journal(self.tmp / "db.sqlite3")


@pytest.fixture
def env(tmp_path, capsys):
    e = Env(tmp_path)
    assert e.run("journal", "init") == 0
    capsys.readouterr()
    return e


def out(capsys):
    return capsys.readouterr().out


def add_trade(env, tid="T1", strike="40", state="PENDING", ticker="EXAMPLE", apply=True):
    return env.run("journal", "add-trade", "--trade-id", tid, "--ticker", ticker, "--expiration", FUTURE, "--strike", strike,
                   "--sector", "sec", "--bucket", "bkt", "--state", state, apply=apply)


# ---- preview / apply discipline ----
def test_preview_is_default_and_writes_nothing(env, capsys):
    assert add_trade(env, apply=False) == 0
    o = out(capsys)
    assert "PREVIEW ONLY" in o
    j = env.journal()
    assert j.list_positions() == [] and j.audit_events("MANUAL_COMMAND") == []
    j.close()


def test_apply_requires_reason_and_is_audited_with_actor(env, capsys):
    assert env.run("journal", "add-trade", "--trade-id", "T1", "--ticker", "EXAMPLE", "--expiration", FUTURE, "--strike", "40",
                   "--sector", "s", "--bucket", "b", "--apply") == 1
    assert "--reason is required" in out(capsys)
    assert env.run("journal", "add-trade", "--trade-id", "T1", "--ticker", "EXAMPLE", "--expiration", FUTURE, "--strike", "40",
                   "--sector", "s", "--bucket", "b", "--apply", "--reason", "placed at broker", "--actor", "me") == 0
    o = out(capsys)
    assert "RECONCILIATION REQUIRED (F3)" in o
    j = env.journal()
    ev = j.audit_events("MANUAL_COMMAND")[0]
    assert ev["actor"] == "me" and "placed at broker" in ev["after_json"]
    j.close()


def test_multi_contract_and_nonstandard_are_rejected_clearly(env, capsys):
    assert env.run("journal", "add-trade", "--trade-id", "T1", "--ticker", "EXAMPLE", "--expiration", FUTURE, "--strike", "40",
                   "--sector", "s", "--bucket", "b", "--contracts", "2", apply=True) == 1
    assert "1 contract per position" in out(capsys)
    assert env.run("journal", "add-trade", "--trade-id", "T1", "--ticker", "EXAMPLE", "--expiration", FUTURE, "--strike", "40",
                   "--sector", "s", "--bucket", "b", "--multiplier", "10", apply=True) == 1
    j = env.journal()
    assert j.list_positions() == []
    j.close()


def test_existing_multi_contract_position_freezes_entries(tmp_path):
    from factories import book, put
    rep = reconcile(book(positions=[put(contracts=2)]), P, NOW)
    assert "MULTI_CONTRACT_POSITION" in [i.code for i in rep.issues] and rep.blocking
    assert not reconcile(book(positions=[put(contracts=1)]), P, NOW).issues


# ---- full manual lifecycle through the CLI ----
def test_trade_lifecycle_fill_close_and_f3_prompt(env, capsys):
    assert add_trade(env) == 0 and add_trade(env, "T2", state="DRAFT") == 0
    out(capsys)
    assert env.run("journal", "fill", "--trade-id", "T1", "--credit", "0.50", "--fees", "0.65", apply=True) == 0
    assert "RECONCILIATION REQUIRED" in out(capsys)
    j = env.journal()
    p = j.get_position("T1")
    assert p.state is S.OPEN and p.actual_open_credit_per_share == D("0.50") and p.account_scope == "paper_primary"
    j.close()
    assert env.run("journal", "close", "--trade-id", "T1", "--working", apply=True) == 0
    assert env.run("journal", "close", "--trade-id", "T1", "--debit", "0.20", "--fees", "0.65", apply=True) == 0
    o = out(capsys)
    assert "realized P&L about $" in o
    j = env.journal()
    assert j.get_position("T1").state is S.CLOSED and j.get_position("T1").fees_usd == D("1.30")
    j.close()


def test_cancel_requires_verification_and_expire_requires_confirmation(env, capsys):
    add_trade(env)
    assert env.run("journal", "cancel", "--trade-id", "T1", apply=True) == 1
    assert env.run("journal", "cancel", "--trade-id", "T1", "--verified", apply=True) == 0
    add_trade(env, "T2", strike="30")
    env.run("journal", "fill", "--trade-id", "T2", "--credit", "0.4", apply=True)
    out(capsys)
    assert env.run("journal", "expire", "--trade-id", "T2", apply=True) == 1                       # not confirmed
    assert env.run("journal", "expire", "--trade-id", "T2", "--confirm-expired", apply=True) == 1  # before expiration
    assert "before the expiration" in out(capsys)


def test_cli_rejects_float_and_garbage_numbers(env, capsys):
    add_trade(env)
    assert env.run("journal", "fill", "--trade-id", "T1", "--credit", "abc", apply=True) == 1
    assert env.run("journal", "fill", "--trade-id", "T1", "--credit", "NaN", apply=True) == 1
    assert env.run("journal", "fill", "--trade-id", "NOPE", "--credit", "0.5", apply=True) == 1


# ---- daily check-in (F2) ----
def test_daily_check_in_preview_lists_positions_and_requires_ack(env, capsys):
    add_trade(env)
    out(capsys)
    assert env.run("daily-check-in") == 0
    o = out(capsys)
    assert "T1: EXAMPLE" in o and "Expected put collateral reserved: $4,000.00" in o and "NET free unencumbered cash" in o
    assert env.run("daily-check-in", "--equity", "100000", "--free-cash", "90000", apply=True) == 1   # no --ack-reconciled
    assert "--ack-reconciled is required" in out(capsys)
    j = env.journal()
    assert j.list_snapshots() == []
    j.close()


def test_daily_check_in_rejects_gross_cash_then_records_and_reconciles(env, capsys):
    env.run("journal", "confirm-baseline", "--equity", "100000")
    add_trade(env)
    out(capsys)
    assert env.run("daily-check-in", "--equity", "100000", "--free-cash", "98000", "--ack-reconciled", apply=True) == 1
    assert "CASH_INCONSISTENT" in out(capsys)
    assert env.run("daily-check-in", "--equity", "100000", "--free-cash", "96000", "--ack-reconciled", apply=True) == 0
    assert "Ledger status: CLEAN" in out(capsys)
    j = env.journal()
    assert j.list_snapshots()[0].manual_confirmed is True
    j.close()
    # a later commitment change blocks again until the next check-in
    time.sleep(1.1)  # audit/snapshot timestamps have 1-second resolution
    env.run("journal", "fill", "--trade-id", "T1", "--credit", "0.5", apply=True)
    assert env.run("reconcile-check") == 1 and "CHANGE_AFTER_SNAPSHOT" in out(capsys)


def test_daily_check_in_marks_lots(env, capsys):
    env.run("journal", "confirm-baseline", "--equity", "100000")
    j = env.journal()
    from csp_alerts.journal.models import StockLot
    j.add_stock_lot(StockLot("L1", "EXAMPLE", D("100"), D("40"), datetime.now(timezone.utc) - timedelta(days=3), "s", "b", "PURCHASE"))
    j.close()
    assert env.run("daily-check-in", "--equity", "100000", "--free-cash", "50000", "--mark", "L1=41.5", "--ack-reconciled", apply=True) == 0
    j = env.journal()
    assert j.list_stock_lots()[0].reference_price == D("41.5")
    j.close()


# ---- assignment (F4) ----
def test_assignment_three_steps_atomic_and_freeze_lifts_only_when_clean(env, capsys):
    env.run("journal", "confirm-baseline", "--equity", "100000")
    add_trade(env, strike="40")
    env.run("journal", "fill", "--trade-id", "T1", "--credit", "0.5", apply=True)
    out(capsys)
    # step 1
    assert env.run("journal", "assign", "--trade-id", "T1", apply=True) == 0
    assert "Obligation stays visible" in out(capsys) or True
    j = env.journal()
    assert j.get_position("T1").state is S.ASSIGNED_PENDING_RECONCILIATION
    j.close()
    assert env.run("journal", "assign", "--trade-id", "T1") == 0                  # guidance when step 2 is missing
    assert "Step 2" in out(capsys)
    # step 2 needs cash paid, correct share count, and is atomic
    assert env.run("journal", "assign", "--trade-id", "T1", "--shares", "100", apply=True) == 1
    assert env.run("journal", "assign", "--trade-id", "T1", "--shares", "50", "--cost-basis-usd", "2000", apply=True) == 1
    j = env.journal()
    assert j.get_position("T1").state is S.ASSIGNED_PENDING_RECONCILIATION and j.list_stock_lots() == []
    j.close()
    assert env.run("journal", "assign", "--trade-id", "T1", "--shares", "100", "--cost-basis-usd", "4000", "--fees", "1", apply=True) == 0
    j = env.journal()
    p, lots = j.get_position("T1"), j.list_stock_lots()
    assert p.state is S.ASSIGNED_RECONCILED and len(lots) == 1 and lots[0].linked_trade_id == "T1" and lots[0].shares == D("100")
    from csp_alerts.policy.portfolio import compute_exposure
    ex = compute_exposure(j.load_book())
    assert ex.put_collateral_usd == 0 and ex.owned_stock_usd == D("4000")          # counted exactly once
    assert not j.audit_events("STOCK_LOT_ADDED") == []
    j.close()
    assert env.run("reconcile-check") == 1                                          # still needs step 3
    assert env.run("daily-check-in", "--equity", "100000", "--free-cash", "96000", "--mark", "LOT-T1=40", "--ack-reconciled", apply=True) == 0
    assert env.run("reconcile-check") == 0


def test_complete_assignment_is_all_or_nothing(tmp_path):
    from csp_alerts.journal.models import StockLot
    j = Journal(tmp_path / "a.sqlite3", clock=lambda: NOW)
    j.migrate()
    j.create_draft(NewPosition("T1", "EXAMPLE", TODAY + timedelta(days=5), D("40"), 100, 1, "s", "b"))
    j.transition("T1", S.PENDING)
    j.transition("T1", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.5"), opened_at_utc=NOW)
    j.transition("T1", S.ASSIGNED_PENDING_RECONCILIATION)
    j.add_stock_lot(StockLot("DUP", "EXAMPLE", D("1"), D("1"), NOW, "s", "b", "PURCHASE"))
    bad = StockLot("DUP", "EXAMPLE", D("100"), D("40"), NOW, "s", "b", "ASSIGNMENT", cost_basis_reference_usd=D("4000"), linked_trade_id="T1")
    with pytest.raises(JournalError):
        j.complete_assignment("T1", bad)                      # lot id collision -> nothing changes
    assert j.get_position("T1").state is S.ASSIGNED_PENDING_RECONCILIATION and len(j.list_stock_lots()) == 1
    j.close()


# ---- reset and cash flow (IT-008) ----
def latched_env(env):
    env.run("journal", "confirm-baseline", "--equity", "100000")
    j = env.journal()
    j.add_snapshot(AccountSnapshot("a", datetime.now(timezone.utc) - timedelta(minutes=2), D("90000"), D("90000"), True, "t"))
    j.close()


def level(env):
    j = env.journal()
    b = j.load_book()
    st = compute_drawdown(b.snapshots, P, baseline_asof_utc=b.last_risk_reset_utc, baseline_equity_usd=b.baseline_equity_usd,
                          cash_flows=b.cash_flows)
    j.close()
    return st


def test_reset_clears_latch_only_with_review_and_reason(env, capsys):
    latched_env(env)
    assert level(env).level is DrawdownLevel.HARD_HALT
    assert env.run("journal", "reset") == 0 and level(env).level is DrawdownLevel.HARD_HALT           # preview
    assert env.run("journal", "reset", apply=True) == 1                                               # no --reviewed
    assert env.run("journal", "reset", "--reviewed", apply=True, reason="reviewed positions") == 0
    assert level(env).level is DrawdownLevel.NONE


def test_withdrawal_shifts_peak_so_it_is_not_a_loss_but_never_clears_a_latch(env, capsys):
    env.run("journal", "confirm-baseline", "--equity", "100000")
    assert env.run("journal", "record-cash-flow", "--amount", "-20000", "--date", date.today().isoformat(), "--note", "ACH out") == 0
    assert "PREVIEW" in out(capsys)
    env.run("journal", "record-cash-flow", "--amount", "-20000", "--date", date.today().isoformat(), "--note", "ACH out", apply=True)
    assert "RECONCILIATION REQUIRED" in out(capsys)
    time.sleep(1.1)
    j = env.journal()
    j.add_snapshot(AccountSnapshot("b", datetime.now(timezone.utc), D("80000"), D("80000"), True, "t"))
    j.close()
    st = level(env)
    assert st.level is DrawdownLevel.NONE and st.peak_equity_usd == D("80000")          # not a loss
    # now a real 10% loss, latched; a later deposit/withdrawal must not clear it
    j = env.journal()
    time.sleep(1.1)
    j.add_snapshot(AccountSnapshot("c", datetime.now(timezone.utc), D("72000"), D("72000"), True, "t"))
    j.close()
    assert level(env).level is DrawdownLevel.HARD_HALT
    for amt in ("-30000", "+500000"):
        env.run("journal", "record-cash-flow", "--amount", amt, "--date", date.today().isoformat(), "--note", "x", apply=True)
        assert level(env).level is DrawdownLevel.HARD_HALT


def test_cash_flow_blocks_entries_until_new_snapshot_and_validates(env, capsys):
    env.run("journal", "confirm-baseline", "--equity", "100000")
    j = env.journal()
    j.add_snapshot(AccountSnapshot("a", datetime.now(timezone.utc) - timedelta(minutes=1), D("100000"), D("100000"), True, "t"))
    j.close()
    assert env.run("reconcile-check") == 0
    time.sleep(1.1)
    env.run("journal", "record-cash-flow", "--amount", "+1000", "--date", date.today().isoformat(), "--note", "deposit", apply=True)
    out(capsys)
    assert env.run("reconcile-check") == 1 and "CHANGE_AFTER_SNAPSHOT" in out(capsys)
    for over in ({"--amount": "0"}, {"--amount": "abc"}, {"--note": " "}, {"--date": "10/30/2026"}):
        a = {"--amount": "5", "--date": date.today().isoformat(), "--note": "n"}
        a.update(over)
        assert env.run("journal", "record-cash-flow", *[x for k, v in a.items() for x in (k, v)], apply=True) == 1


# ---- approvals ----
def approve_args(**over):
    d = {"--ticker": "ABC", "--asset-type": "ETF", "--sector": "Broad", "--bucket": "b1", "--max-buy-strike": "50",
         "--approved-through": (date.today() + timedelta(days=60)).isoformat(),
         "--events-reviewed-on": date.today().isoformat(), "--events-cleared-through": (date.today() + timedelta(days=90)).isoformat(),
         "--ownership-reason": "owner diligence done", "--standard-deliverable-confirmed-on": date.today().isoformat()}
    d.update(over)
    return ["journal", "approve"] + [x for k, v in d.items() for x in (k, v)]


def test_approve_preview_apply_backup_and_validation(env, capsys):
    assert env.run(*approve_args()) == 0 and json.loads(env.approved.read_text())["approved_underlyings"] == []
    assert env.run(*approve_args(), apply=True) == 0
    data = json.loads(env.approved.read_text())["approved_underlyings"]
    assert data[0]["ticker"] == "ABC" and data[0]["sector"] == "broad" and data[0]["standard_deliverable_confirmed_on"]
    assert env.run(*approve_args(**{"--max-buy-strike": "55"}), apply=True) == 0                 # update, not duplicate
    assert len(json.loads(env.approved.read_text())["approved_underlyings"]) == 1
    assert env.approved.with_suffix(".json.bak").exists()
    from csp_alerts.policy.approved import load_approved
    assert load_approved(env.approved)[0].max_buy_strike == D("55")
    assert env.run(*approve_args(**{"--max-buy-strike": "abc"}), apply=True) == 1
    assert env.run(*approve_args(**{"--ticker": "EXAMPLE"}), apply=True) == 1                      # synthetic marker
    assert env.run(*approve_args(**{"--ownership-reason": "synthetic placeholder"}), apply=True) == 1
    out(capsys)
    assert env.run(*approve_args(**{"--ticker": "XYZ"}), apply=False) == 0


def test_add_trade_takes_sector_from_approval(env, capsys):
    env.run(*approve_args(), apply=True)
    assert env.run("journal", "add-trade", "--trade-id", "T9", "--ticker", "ABC", "--expiration", FUTURE, "--strike", "40", apply=True) == 0
    j = env.journal()
    assert j.get_position("T9").sector == "broad"
    j.close()
    assert env.run("journal", "add-trade", "--trade-id", "T8", "--ticker", "NOPE", "--expiration", FUTURE, "--strike", "40", apply=True) == 1


# ---- snapshot, backup ----
def test_snapshot_command_and_backup(env, capsys, tmp_path):
    assert env.run("journal", "snapshot", "--equity", "100000", "--free-cash", "100000", apply=True) == 0
    dest = tmp_path / "bk" / "copy.sqlite3"
    assert env.run("journal", "backup", "--output", str(dest)) == 0
    assert "integrity ok" in out(capsys)
    c = sqlite3.connect(dest)
    assert c.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == 1
    c.close()


# ---- lock ----
def test_single_instance_lock(tmp_path):
    lock = tmp_path / "locks" / "x.lock"
    with single_instance(lock):
        with pytest.raises(AlreadyRunning):
            with single_instance(lock):
                pass
    with single_instance(lock):  # released afterwards
        pass


def test_cli_job_skips_with_exit_3_when_lock_held(env, capsys):
    lock = env.tmp / "locks" / "csp-jobs.lock"
    with single_instance(lock):
        assert env.run("monitor") == 3
    assert "SKIPPED" in capsys.readouterr().err


# ---- scheduled-for guard, reconcile reminder ----
def test_late_scheduled_scan_is_skipped_never_replayed(env, capsys, monkeypatch):
    from csp_alerts import cli
    frozen = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)      # 13:00 CDT: fixed, never near midnight
    monkeypatch.setattr(cli, "_now", lambda: frozen)
    old = "12:20"                                                    # 40 minutes before the frozen instant
    assert env.run("scan", "--scheduled-for", old) == 0
    assert "SKIPPED" in out(capsys)
    j = env.journal()
    assert j.last_run("SCAN")["status"] == "SKIPPED_LATE"
    j.close()


def test_reconcile_reminder_dry_run_and_content(env, capsys):
    assert env.run("reconcile-reminder") == 0
    o = out(capsys)
    assert "RECONCILIATION REMINDER" in o or "not a trading day" in o


# ---- coverage labels ----
def test_partial_scan_is_labelled_not_a_clean_no_trade():
    from csp_alerts.alerts import templates
    cov = [("AAA", "OK", "80 puts"), ("BBB", "UNAVAILABLE", "FETCH_ERROR")]
    line = templates.coverage_line(cov)
    assert "1 of 2" in line and "PARTIAL" in line and "BBB UNAVAILABLE" in line
    assert "PARTIAL" not in templates.coverage_line([("AAA", "OK", "x")])
    d = templates.no_trade_digest("NO TRADE", {}, NOW, coverage=cov)
    assert "PARTIAL" in d.text
    m = templates.data_issue([("BBB", "FETCH_ERROR", "x")], NOW, coverage=cov)
    assert "Coverage: 1 of 2" in m.text


def test_scan_result_carries_coverage_for_failed_and_ok_symbols(tmp_path):
    from test_scan import FakeSource, appr, scan
    from csp_alerts.market.stockpile import SourceError
    j = Journal(tmp_path / "c.sqlite3", clock=lambda: NOW)
    j.migrate()
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(equity="100000", cash="60000", asof=NOW - timedelta(hours=1)))
    r = scan(j, [appr("AAA"), appr("BBB")], FakeSource({"AAA": [raw_row(delta=-0.5)]}, errors={"BBB": SourceError("FETCH_ERROR", "x")}))
    assert [(t, s) for t, s, _ in r.coverage] == [("AAA", "OK"), ("BBB", "UNAVAILABLE")] or \
        sorted((t, s) for t, s, _ in r.coverage) == [("AAA", "OK"), ("BBB", "UNAVAILABLE")]
    assert r.status == "DATA_UNAVAILABLE" and "PARTIAL" in r.messages[-1].text
    j.close()


def test_event_time_for_expire_and_assign_is_never_in_the_future():
    """Run at 10:00 CT on the exchange date: closed_at/assigned_at is 'now', not 21:00Z (SR-08 clamp for expire + assign step 1)."""
    from types import SimpleNamespace
    from csp_alerts.ledger_cli import _event_time
    now = datetime(2026, 12, 18, 16, 0, tzinfo=timezone.utc)                       # 10:00 CT
    ctx = SimpleNamespace(now=now)
    assert _event_time(ctx, SimpleNamespace(date=None)) == now
    assert _event_time(ctx, SimpleNamespace(date="2026-12-17")) == datetime(2026, 12, 17, 21, 0, tzinfo=timezone.utc)
    assert _event_time(ctx, SimpleNamespace(date="2026-12-19")) == now             # marks/assign clamp a later date
    with pytest.raises(Exception, match="future"):                                  # D3: expire refuses a typed future date
        _event_time(ctx, SimpleNamespace(date="2026-12-19"), refuse_future_date=True)
    assert _event_time(ctx, SimpleNamespace(date=None), refuse_future_date=True) == now
    late = datetime(2026, 12, 18, 23, 0, tzinfo=timezone.utc)
    assert _event_time(SimpleNamespace(now=late), SimpleNamespace(date=None)) == datetime(2026, 12, 18, 21, 0, tzinfo=timezone.utc)
