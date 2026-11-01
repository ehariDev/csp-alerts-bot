"""Acceptance: SQLite journal, manual state machine, audit log, reconciliation, CSV (spec 5, 15.1 #20, 15.2)."""
from __future__ import annotations

import csv
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from acc_helpers import policy

from csp_alerts.journal.import_csv import (
    COLUMNS, CsvFormatError, export_positions_csv, import_positions_csv, write_quarantine_csv,
)
from csp_alerts.journal.models import AccountSnapshot, PositionState as S, StockLot
from csp_alerts.journal.reconcile import ledger_reason_codes, reconcile
from csp_alerts.journal.state_machine import TransitionError
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_alerts.policy.portfolio import compute_exposure

T0 = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc)
EXP = date(2026, 12, 4)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def adv(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture()
def clk():
    return Clock()


@pytest.fixture()
def j(clk):
    jj = Journal(":memory:", clock=clk)
    jj.migrate()
    yield jj
    jj.close()


def newpos(tid="T1", ticker="AAA", strike="75.00", contracts=1, mult=100, **kw):
    return NewPosition(tid, ticker, EXP, D(strike), mult, contracts, "tech", "b1", **kw)


def to_open(j, tid="T1", **kw):
    j.create_draft(newpos(tid, **kw))
    j.transition(tid, S.PENDING)
    j.transition(tid, S.WORKING)
    return j.transition(tid, S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.50"),
                        opened_at_utc=T0, fees_usd=D("0.65"))


# ---------------------------------------------------------------- schema / migrations
def test_spec_5_1_tables_exist(j):
    names = {r[0] for r in j.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"policy_versions", "approved_underlyings", "option_positions", "stock_lots", "account_snapshots",
            "market_observations", "alert_outbox", "audit_log"} <= names


def test_migrate_idempotent_and_integrity(j):
    assert j.migrate() == []
    assert j.schema_version() is not None and j.integrity_ok()


def test_wal_enabled_for_file_db(tmp_path):
    with Journal(tmp_path / "x.sqlite3") as jj:
        jj.migrate()
        assert jj.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_db_constraints_reject_bad_rows(j):
    j.create_draft(newpos())
    with pytest.raises(sqlite3.IntegrityError):
        j.conn.execute("UPDATE option_positions SET option_right='C' WHERE trade_id='T1'")
    with pytest.raises(sqlite3.IntegrityError):
        j.conn.execute("UPDATE option_positions SET paper=2 WHERE trade_id='T1'")


def test_decimals_stored_as_text(j):
    j.create_draft(newpos(strike="33.33"))
    r = j.conn.execute("SELECT typeof(strike), strike FROM option_positions").fetchone()
    assert tuple(r) == ("text", "33.33")


def test_audit_log_is_append_only(j):
    j.create_draft(newpos())
    n = j.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert n >= 1
    with pytest.raises(sqlite3.DatabaseError):
        j.conn.execute("UPDATE audit_log SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError):
        j.conn.execute("DELETE FROM audit_log")
    assert j.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == n


def test_every_mutation_is_audited(j, clk):
    to_open(j)
    ev = [e["event_type"] for e in j.audit_events()]
    assert ev.count("POSITION_TRANSITION") == 3 and "POSITION_DRAFT_CREATED" in ev
    assert all(e["actor"] == "owner" and e["created_at_utc"].endswith("Z") for e in j.audit_events())


def test_policy_revision_recorded_once_per_hash_and_new_revision_allowed(j):
    p = policy()
    assert j.record_policy(p) is True and j.record_policy(p) is False
    data = p.model_dump(mode="json")
    data["entries"]["min_open_interest"] = 600
    from csp_alerts.policy.config import parse_policy
    p2 = parse_policy(data)
    assert p2.sha256 != p.sha256 and j.record_policy(p2) is True
    assert j.conn.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0] == 2


# ---------------------------------------------------------------- state machine
def test_happy_path_open_close_collateral_release(j):
    to_open(j)
    assert compute_exposure(j.load_book()).put_collateral_usd == D("7500.00")
    j.transition("T1", S.CLOSE_WORKING)
    assert compute_exposure(j.load_book()).put_collateral_usd == D("7500.00")  # close order does NOT release
    j.transition("T1", S.CLOSED, actual_close_debit_per_share=D("0.20"), closed_at_utc=T0, fees_usd=D("1.30"))
    assert compute_exposure(j.load_book()).put_collateral_usd == D("0")
    p = j.get_position("T1")
    assert p.actual_close_debit_per_share == D("0.20") and p.state is S.CLOSED


def test_pending_working_reserve_cancel_releases(j):
    j.create_draft(newpos())
    assert compute_exposure(j.load_book()).put_collateral_usd == D("0")      # DRAFT reserves nothing
    j.transition("T1", S.PENDING)
    assert compute_exposure(j.load_book()).put_collateral_usd == D("7500.00")
    j.transition("T1", S.WORKING)
    assert compute_exposure(j.load_book()).put_collateral_usd == D("7500.00")
    with pytest.raises(TransitionError):
        j.transition("T1", S.CANCELED)                                       # needs owner-verified cancel
    j.transition("T1", S.CANCELED, confirmed=True)
    assert compute_exposure(j.load_book()).put_collateral_usd == D("0")


@pytest.mark.parametrize("frm,to", [
    (S.DRAFT, S.OPEN), (S.DRAFT, S.WORKING), (S.DRAFT, S.CLOSED), (S.PENDING, S.CLOSED), (S.PENDING, S.EXPIRED),
    (S.WORKING, S.CLOSED), (S.WORKING, S.PENDING), (S.OPEN, S.CLOSED), (S.OPEN, S.WORKING), (S.OPEN, S.CANCELED),
    (S.OPEN, S.ASSIGNED_RECONCILED), (S.CLOSE_WORKING, S.CANCELED), (S.CLOSE_WORKING, S.PENDING),
])
def test_illegal_transitions_rejected(j, frm, to):
    path = {S.DRAFT: [], S.PENDING: [S.PENDING], S.WORKING: [S.PENDING, S.WORKING],
            S.OPEN: "open", S.CLOSE_WORKING: "cw"}
    j.create_draft(newpos())
    if path[frm] == "open" or path[frm] == "cw":
        j.transition("T1", S.PENDING); j.transition("T1", S.OPEN, fill_contracts=1,
                                                   actual_open_credit_per_share=D("0.5"), opened_at_utc=T0)
        if path[frm] == "cw":
            j.transition("T1", S.CLOSE_WORKING)
    else:
        for s in path[frm]:
            j.transition("T1", s)
    before = j.get_position("T1")
    with pytest.raises(TransitionError):
        j.transition("T1", to, confirmed=True, fill_contracts=1, actual_open_credit_per_share=D("0.5"),
                     opened_at_utc=T0, actual_close_debit_per_share=D("0.1"), closed_at_utc=T0, fees_usd=D("0"))
    assert j.get_position("T1") == before


@pytest.mark.parametrize("terminal", ["CLOSED", "CANCELED", "EXPIRED", "ASSIGNED_RECONCILED"])
def test_terminal_states_have_no_exit(j, terminal):
    j.create_draft(newpos())
    if terminal == "CANCELED":
        j.transition("T1", S.CANCELED)
    else:
        to_open(j, "T2")
        tid = "T2"
        if terminal == "CLOSED":
            j.transition(tid, S.CLOSE_WORKING)
            j.transition(tid, S.CLOSED, actual_close_debit_per_share=D("0.2"), closed_at_utc=T0, fees_usd=D("0"))
        elif terminal == "EXPIRED":
            j.transition(tid, S.EXPIRED, confirmed=True, closed_at_utc=datetime(2026, 12, 4, 21, tzinfo=timezone.utc))
        else:
            j.transition(tid, S.ASSIGNED_PENDING_RECONCILIATION)
            j.add_stock_lot(StockLot("L1", "AAA", D("100"), D("75"), T0, "tech", "b1", "ASSIGNMENT",
                                     cost_basis_reference_usd=D("7500"), linked_trade_id=tid))
            j.transition(tid, S.ASSIGNED_RECONCILED, confirmed=True)
    tid = "T1" if terminal == "CANCELED" else "T2"
    for s in S:
        with pytest.raises(TransitionError):
            j.transition(tid, s, confirmed=True, fill_contracts=1, actual_open_credit_per_share=D("0.5"),
                         opened_at_utc=T0, actual_close_debit_per_share=D("0.1"), closed_at_utc=T0, fees_usd=D("0"))


def test_open_requires_explicit_fill_facts(j):
    j.create_draft(newpos())
    j.transition("T1", S.PENDING)
    for kw in (
        {},                                                                                   # nothing
        dict(fill_contracts=1, opened_at_utc=T0),                                             # no credit
        dict(fill_contracts=1, actual_open_credit_per_share=D("0.5")),                        # no time
        dict(actual_open_credit_per_share=D("0.5"), opened_at_utc=T0),                        # no quantity
        dict(fill_contracts=2, actual_open_credit_per_share=D("0.5"), opened_at_utc=T0),      # partial/over fill
        dict(fill_contracts=0, actual_open_credit_per_share=D("0.5"), opened_at_utc=T0),
        dict(fill_contracts=1, actual_open_credit_per_share=D("0"), opened_at_utc=T0),        # zero credit
        dict(fill_contracts=1, actual_open_credit_per_share=D("-0.5"), opened_at_utc=T0),     # negative
        dict(fill_contracts=1, actual_open_credit_per_share=0.5, opened_at_utc=T0),           # float
        dict(fill_contracts=1, actual_open_credit_per_share=D("Infinity"), opened_at_utc=T0),
    ):
        with pytest.raises(Exception):
            j.transition("T1", S.OPEN, **kw)
        assert j.get_position("T1").state is S.PENDING, kw


def test_open_rejects_nan_credit_with_clean_error(j):
    j.create_draft(newpos())
    j.transition("T1", S.PENDING)
    with pytest.raises(TransitionError):
        j.transition("T1", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("NaN"), opened_at_utc=T0)
    assert j.get_position("T1").state is S.PENDING


def test_naive_datetime_rejected_on_open(j):
    j.create_draft(newpos())
    j.transition("T1", S.PENDING)
    with pytest.raises(Exception):
        j.transition("T1", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.5"),
                     opened_at_utc=datetime(2026, 10, 30, 15, 0))
    assert j.get_position("T1").state is S.PENDING


def test_closed_requires_confirmed_fill_facts(j):
    to_open(j)
    j.transition("T1", S.CLOSE_WORKING)
    for kw in ({}, dict(actual_close_debit_per_share=D("0.2"), closed_at_utc=T0),               # fees missing
               dict(closed_at_utc=T0, fees_usd=D("0")), dict(actual_close_debit_per_share=D("-0.1"), closed_at_utc=T0, fees_usd=D("0")),
               dict(actual_close_debit_per_share=0.2, closed_at_utc=T0, fees_usd=D("0")),
               dict(actual_close_debit_per_share=D("0.2"), closed_at_utc=T0, fees_usd=D("-1"))):
        with pytest.raises(Exception):
            j.transition("T1", S.CLOSED, **kw)
        assert j.get_position("T1").state is S.CLOSE_WORKING


def test_expired_requires_confirmation_and_not_before_expiration(j):
    to_open(j)
    with pytest.raises(TransitionError):
        j.transition("T1", S.EXPIRED, closed_at_utc=datetime(2026, 12, 4, 21, tzinfo=timezone.utc))
    with pytest.raises(TransitionError):
        j.transition("T1", S.EXPIRED, confirmed=True, closed_at_utc=datetime(2026, 12, 3, 21, tzinfo=timezone.utc))
    with pytest.raises(TransitionError):
        j.transition("T1", S.EXPIRED, confirmed=True)
    with pytest.raises(TransitionError):
        j.transition("T1", S.EXPIRED, confirmed="yes", closed_at_utc=datetime(2026, 12, 4, 21, tzinfo=timezone.utc))
    assert j.get_position("T1").state is S.OPEN
    j.transition("T1", S.EXPIRED, confirmed=True, closed_at_utc=datetime(2026, 12, 4, 21, tzinfo=timezone.utc))


def test_dte_zero_never_auto_closes_or_releases_collateral(j, clk):
    to_open(j)
    clk.t = datetime(2026, 12, 4, 21, tzinfo=timezone.utc)
    b = j.load_book()
    assert b.positions[0].state is S.OPEN and compute_exposure(b).put_collateral_usd == D("7500.00")
    clk.t = datetime(2026, 12, 6, 21, tzinfo=timezone.utc)
    rep = reconcile(j.load_book(), policy(), clk.t)
    assert "EXPIRY_UNCONFIRMED" in {i.code for i in rep.issues} and rep.blocking


def test_assignment_flow_counts_stock_once_and_requires_lot(j):
    to_open(j)
    j.transition("T1", S.ASSIGNED_PENDING_RECONCILIATION)
    with pytest.raises(TransitionError):                       # no lot yet
        j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)
    b = j.load_book()
    assert compute_exposure(b).put_collateral_usd == D("0")    # pending assignment does not hold collateral (spec)
    assert "ASSIGNMENT_PENDING" in {i.code for i in reconcile(b, policy(), T0).issues}
    with pytest.raises(JournalError):                          # lot of wrong ticker
        j.add_stock_lot(StockLot("L0", "ZZZ", D("100"), D("75"), T0, "tech", "b1", "ASSIGNMENT", linked_trade_id="T1"))
    j.add_stock_lot(StockLot("L1", "AAA", D("50"), D("75"), T0, "tech", "b1", "ASSIGNMENT",
                             cost_basis_reference_usd=D("3750"), linked_trade_id="T1"))
    with pytest.raises(TransitionError):                       # 50 != 1*100
        j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)
    j.conn.execute("UPDATE stock_lots SET shares='100' WHERE lot_id='L1'")
    j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)
    b = j.load_book()
    ex = compute_exposure(b)
    assert ex.put_collateral_usd == D("0") and ex.owned_stock_usd == D("7500")
    assert ex.aggregate_equity_risk_usd == D("7500")


def test_assigned_needs_cost_basis_and_confirmation(j):
    to_open(j)
    j.transition("T1", S.ASSIGNED_PENDING_RECONCILIATION)
    j.add_stock_lot(StockLot("L1", "AAA", D("100"), D("75"), T0, "tech", "b1", "ASSIGNMENT", linked_trade_id="T1"))
    with pytest.raises(TransitionError):
        j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)   # no actual purchase cash
    j.conn.execute("UPDATE stock_lots SET cost_basis_reference_usd='7500' WHERE lot_id='L1'")
    with pytest.raises(TransitionError):
        j.transition("T1", S.ASSIGNED_RECONCILED)                    # not confirmed


def test_lot_cannot_link_to_non_assigned_put_or_missing_put(j):
    to_open(j)
    with pytest.raises(JournalError):
        j.add_stock_lot(StockLot("L1", "AAA", D("100"), D("75"), T0, "tech", "b1", "X", linked_trade_id="T1"))
    with pytest.raises(JournalError):
        j.add_stock_lot(StockLot("L2", "AAA", D("100"), D("75"), T0, "tech", "b1", "X", linked_trade_id="NOPE"))


def test_failed_transition_is_atomic_no_audit_no_change(j):
    to_open(j)
    n = len(j.audit_events())
    with pytest.raises(TransitionError):
        j.transition("T1", S.CLOSED, actual_close_debit_per_share=D("0.1"), closed_at_utc=T0, fees_usd=D("0"))
    assert len(j.audit_events()) == n and j.get_position("T1").state is S.OPEN
    with pytest.raises(TransitionError):
        j.transition("T1", S.CLOSE_WORKING, bogus=1)
    with pytest.raises(JournalError):
        j.transition("NOPE", S.PENDING)


@pytest.mark.parametrize("kw", [
    dict(trade_id=" "), dict(ticker=""), dict(strike=D("0")), dict(strike=D("-1")), dict(strike=D("NaN")),
    dict(strike=75.0), dict(contracts=0), dict(contracts=-1), dict(mult=0), dict(strike=D("Infinity")),
])
def test_new_position_validation(j, kw):
    base = dict(tid="T1", ticker="AAA", strike="75.00", contracts=1, mult=100)
    args = {**base, **kw}
    with pytest.raises(Exception):
        n = NewPosition(args["tid"] if "trade_id" not in kw else kw["trade_id"], args["ticker"], EXP,
                        kw["strike"] if "strike" in kw else D(args["strike"]), args["mult"], args["contracts"], "s", "b")
        j.create_draft(n)
    assert j.list_positions() == []


def test_duplicate_trade_id_rejected(j):
    j.create_draft(newpos())
    with pytest.raises(JournalError):
        j.create_draft(newpos())


def test_unknown_asset_type_counts_as_company_name(j):
    """B2-1 / WT-05: an asset type that is not in the approvals stays UNKNOWN (never silently STOCK). UNKNOWN is still counted
    conservatively as an individual company (fail closed); only a recorded ETF label is exempt."""
    j.create_draft(newpos())
    j.transition("T1", S.PENDING)
    assert j.get_position("T1").asset_type == "UNKNOWN"
    assert j.get_position("T1").asset_type != "ETF"


# ---------------------------------------------------------------- snapshots / reset / reconciliation
def snapshot(sid, asof, eq="100000", cash="50000", conf=True):
    return AccountSnapshot(sid, asof, D(eq), D(cash), conf, "v1")


@pytest.mark.parametrize("eq,cash", [("0", "1"), ("-5", "1"), ("100", "-1")])
def test_snapshot_validation(j, eq, cash):
    with pytest.raises(JournalError):
        j.add_snapshot(snapshot("s", T0, eq, cash))


def test_snapshot_nan_rejected_cleanly_or_error(j):
    with pytest.raises(Exception):
        j.add_snapshot(snapshot("s", T0, "NaN"))
    assert j.list_snapshots() == []


def test_future_snapshot_and_duplicate_rejected(j, clk):
    with pytest.raises(JournalError):
        j.add_snapshot(snapshot("s", T0 + timedelta(hours=1)))
    j.add_snapshot(snapshot("s", T0))
    with pytest.raises(JournalError):
        j.add_snapshot(snapshot("s", T0))


def test_trade_after_snapshot_requires_new_snapshot(j, clk):
    j.confirm_baseline(D("100000"))   # owner decision 2: no baseline -> NO_BASELINE blocks everything
    j.add_snapshot(snapshot("s1", T0))
    pol = policy()
    assert not reconcile(j.load_book(), pol, clk.t).blocking
    clk.adv(minutes=10)
    j.create_draft(newpos())
    assert not reconcile(j.load_book(), pol, clk.t).blocking              # DRAFT is not a ledger change
    clk.adv(minutes=1)
    j.transition("T1", S.PENDING)
    rep = reconcile(j.load_book(), pol, clk.t)
    assert "LEDGER_UNRECONCILED" in ledger_reason_codes(rep)
    clk.adv(minutes=1)
    j.add_snapshot(snapshot("s2", clk.t))
    assert not reconcile(j.load_book(), pol, clk.t).blocking


def test_stock_lot_add_requires_new_snapshot(j, clk):
    j.add_snapshot(snapshot("s1", T0))
    clk.adv(minutes=5)
    j.add_stock_lot(StockLot("L1", "AAA", D("10"), D("5"), clk.t, "tech", "b1", "OWNED"))
    assert "LEDGER_UNRECONCILED" in ledger_reason_codes(reconcile(j.load_book(), policy(), clk.t))


def test_risk_reset_requires_note_and_confirmed_snapshot_and_resets_peak(j, clk):
    with pytest.raises(JournalError):
        j.record_risk_reset("note")                       # no snapshot
    j.add_snapshot(snapshot("s0", T0 - timedelta(days=3), "100000"))
    j.add_snapshot(snapshot("s1", T0 - timedelta(days=2), "91000"))
    j.add_snapshot(snapshot("s2", T0 - timedelta(hours=2), "99000"))
    with pytest.raises(JournalError):
        j.record_risk_reset("  ")
    from csp_alerts.policy.drawdown import DrawdownLevel, compute_drawdown
    pol = policy()
    b = j.load_book()
    assert compute_drawdown(b.snapshots, pol, baseline_asof_utc=b.last_risk_reset_utc).level is DrawdownLevel.HARD_HALT
    j.record_risk_reset("reviewed")
    b = j.load_book()
    assert b.last_risk_reset_utc == T0 - timedelta(hours=2)
    assert compute_drawdown(b.snapshots, pol, baseline_asof_utc=b.last_risk_reset_utc).level is DrawdownLevel.NONE
    assert any(e["event_type"] == "RISK_RESET" for e in j.audit_events())


def test_unconfirmed_snapshot_cannot_be_reset_baseline(j):
    j.add_snapshot(snapshot("s0", T0 - timedelta(hours=3), "100000", conf=True))
    j.add_snapshot(snapshot("s1", T0 - timedelta(hours=2), "50000", conf=False))
    j.record_risk_reset("ok")
    assert j.load_book().last_risk_reset_utc == T0 - timedelta(hours=3)


def test_backup_roundtrip(j, tmp_path):
    to_open(j)
    dest = tmp_path / "b" / "bk.sqlite3"
    j.backup(dest)
    with Journal(dest) as j2:
        assert [p.trade_id for p in j2.list_positions()] == ["T1"] and j2.integrity_ok()


def test_concurrent_transitions_only_one_wins(tmp_path):
    path = tmp_path / "c.sqlite3"
    with Journal(path) as a:
        a.migrate()
        a.create_draft(newpos())
        a.transition("T1", S.PENDING)
    results = []

    def worker():
        with Journal(path) as jj:
            try:
                jj.transition("T1", S.WORKING)
                results.append("ok")
            except TransitionError:
                results.append("rejected")
            except Exception as exc:  # noqa: BLE001
                results.append(type(exc).__name__)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("rejected") == 5, results


# ---------------------------------------------------------------- CSV
def row(**kw):
    d = dict(trade_id="C1", account_scope="paper_primary", paper="1", ticker="AAA", expiration="2026-12-04",
             strike="75.00", multiplier="100", contracts="1", sector="tech", correlation_bucket="b1", state="OPEN",
             opened_at_utc="2026-10-20T15:00:00Z", actual_open_credit_per_share="0.50",
             actual_close_debit_per_share="", fees_usd="0.65", closed_at_utc="", notes="")
    d.update(kw)
    return d


def write_csv(path: Path, rows, header=COLUMNS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(c, "") for c in header] if isinstance(r, dict) else r)
    return path


def test_csv_import_happy_and_roundtrip(j, tmp_path):
    f = write_csv(tmp_path / "in.csv", [row(), row(trade_id="C2", ticker="BBB", state="PENDING", opened_at_utc="",
                                                  actual_open_credit_per_share="", fees_usd="0")])
    rep = import_positions_csv(j, f, dry_run=False)
    assert rep.ok and sorted(rep.accepted) == ["C1", "C2"]
    p = j.get_position("C1")
    assert p.strike == D("75.00") and p.actual_open_credit_per_share == D("0.50") and p.fees_usd == D("0.65")
    out = tmp_path / "out" / "x.csv"
    assert export_positions_csv(j, out) == 2
    with Journal(":memory:", clock=Clock()) as j2:
        j2.migrate()
        rep2 = import_positions_csv(j2, out, dry_run=False)
        assert rep2.ok
        assert {x.trade_id: (x.state, x.strike, x.contracts) for x in j2.list_positions()} == \
               {x.trade_id: (x.state, x.strike, x.contracts) for x in j.list_positions()}


def test_csv_dry_run_writes_nothing(j, tmp_path):
    f = write_csv(tmp_path / "in.csv", [row()])
    rep = import_positions_csv(j, f, dry_run=True)
    assert rep.accepted == ["C1"] and j.list_positions() == []
    assert j.audit_events("POSITION_IMPORTED") == []
    assert j.conn.execute("SELECT COUNT(*) FROM import_quarantine").fetchone()[0] == 0


def test_csv_default_is_dry_run(j, tmp_path):
    import inspect
    assert inspect.signature(import_positions_csv).parameters["dry_run"].default is True


@pytest.mark.parametrize("bad", [
    dict(strike="-75"), dict(strike="0"), dict(strike="abc"), dict(strike="NaN"), dict(strike="Infinity"), dict(strike=""),
    dict(contracts="0"), dict(contracts="-1"), dict(contracts="1.5"), dict(contracts=""), dict(contracts="١"),
    dict(multiplier="0"), dict(multiplier="abc"),
    dict(actual_open_credit_per_share="-0.5"), dict(actual_open_credit_per_share="0"), dict(actual_open_credit_per_share=""),
    dict(opened_at_utc=""), dict(opened_at_utc="2026-10-20T15:00:00"), dict(opened_at_utc="not a time"),
    dict(fees_usd="-1"), dict(fees_usd="x"),
    dict(expiration="12/04/2026"), dict(expiration="2026-02-30"), dict(state="LIVE"), dict(state=""),
    dict(paper="maybe"), dict(trade_id=" "), dict(ticker=""), dict(sector=""), dict(correlation_bucket=""),
    dict(state="CLOSED"),                                                            # no debit / closed time
    dict(state="OPEN", actual_close_debit_per_share="0.1"),                         # open with closing debit
    dict(state="PENDING"),                                                           # pending carrying fill data
    dict(state="DRAFT"), dict(state="CANCELED"),
    dict(state="EXPIRED"),                                                           # no closed_at
    dict(state="CLOSED", actual_close_debit_per_share="0.1", closed_at_utc="2026-10-10T15:00:00Z"),  # before opened
])
def test_csv_invalid_or_contradictory_rows_quarantined(j, tmp_path, bad):
    f = write_csv(tmp_path / "in.csv", [row(trade_id="GOOD", ticker="BBB"), row(**bad)])
    rep = import_positions_csv(j, f, dry_run=False)
    assert [q.row_number for q in rep.quarantined] == [3], bad
    assert [p.trade_id for p in j.list_positions()] == ["GOOD"]
    assert j.conn.execute("SELECT COUNT(*) FROM import_quarantine").fetchone()[0] == 1


def test_csv_open_row_with_closed_at_is_contradictory(j, tmp_path):
    """OPEN position carrying a closed_at_utc timestamp is self-contradictory and must not be silently accepted."""
    f = write_csv(tmp_path / "in.csv", [row(closed_at_utc="2026-10-25T15:00:00Z")])
    rep = import_positions_csv(j, f, dry_run=True)
    assert len(rep.quarantined) == 1


def test_csv_duplicate_ids_within_file_and_existing(j, tmp_path):
    f = write_csv(tmp_path / "in.csv", [row(), row(strike="80.00")])
    rep = import_positions_csv(j, f, dry_run=False)
    assert rep.accepted == ["C1"] and len(rep.quarantined) == 1
    assert j.get_position("C1").strike == D("75.00")                 # first wins, nothing overwritten
    rep2 = import_positions_csv(j, f, dry_run=False)                 # re-import: never overwrites
    assert rep2.accepted == [] and len(rep2.quarantined) == 2
    assert j.get_position("C1").strike == D("75.00")


def test_csv_header_and_shape_errors(j, tmp_path):
    with pytest.raises(CsvFormatError):
        import_positions_csv(j, write_csv(tmp_path / "a.csv", [], header=COLUMNS[:-1]))
    with pytest.raises(CsvFormatError):
        import_positions_csv(j, write_csv(tmp_path / "b.csv", [], header=tuple(reversed(COLUMNS))))
    with pytest.raises(CsvFormatError):
        import_positions_csv(j, tmp_path / "missing.csv")
    (tmp_path / "e.csv").write_text("", encoding="utf-8")
    with pytest.raises(CsvFormatError):
        import_positions_csv(j, tmp_path / "e.csv")
    f = write_csv(tmp_path / "short.csv", [["C1", "paper_primary"], row(trade_id="Z") ])
    rep = import_positions_csv(j, f, dry_run=False)
    assert rep.accepted == ["Z"] and len(rep.quarantined) == 1


def test_csv_import_marks_ledger_change_and_quarantine_file(j, tmp_path, clk):
    j.add_snapshot(snapshot("s1", T0))
    clk.adv(minutes=5)
    f = write_csv(tmp_path / "in.csv", [row(), row(trade_id="BAD", strike="-1")])
    rep = import_positions_csv(j, f, dry_run=False)
    assert "LEDGER_UNRECONCILED" in ledger_reason_codes(reconcile(j.load_book(), policy(), clk.t))
    q = tmp_path / "q.csv"
    write_quarantine_csv(rep, q)
    assert "BAD" in q.read_text(encoding="utf-8")


def test_csv_import_atomic_on_midway_failure(j, tmp_path, monkeypatch):
    f = write_csv(tmp_path / "in.csv", [row(trade_id="A1", ticker="AAA"), row(trade_id="A2", ticker="BBB")])
    orig = j._insert_position
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk full")
        return orig(*a, **k)

    monkeypatch.setattr(j, "_insert_position", boom)
    with pytest.raises(sqlite3.OperationalError):
        import_positions_csv(j, f, dry_run=False)
    assert j.list_positions() == []


def test_csv_ticker_uppercased_and_whitespace_trimmed(j, tmp_path):
    f = write_csv(tmp_path / "in.csv", [row(trade_id=" C9 ", ticker=" aaa ")])
    rep = import_positions_csv(j, f, dry_run=False)
    assert rep.accepted == ["C9"] and j.get_position("C9").ticker == "AAA"


def test_csv_bom_and_crlf_accepted(j, tmp_path):
    f = tmp_path / "bom.csv"
    with open(f, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(COLUMNS)
        r = row()
        w.writerow([r[c] for c in COLUMNS])
    assert import_positions_csv(j, f, dry_run=True).ok


def test_csv_assigned_reconciled_without_lot_is_flagged_by_reconcile(j, tmp_path, clk):
    j.add_snapshot(snapshot("s1", T0))
    f = write_csv(tmp_path / "in.csv", [row(state="ASSIGNED_RECONCILED")])
    clk.adv(minutes=1)
    import_positions_csv(j, f, dry_run=False)
    j.add_snapshot(snapshot("s2", clk.t))
    codes_ = {i.code for i in reconcile(j.load_book(), policy(), clk.t).issues}
    assert "ASSIGNED_WITHOUT_LOT" in codes_
