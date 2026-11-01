"""P0-03: SQLite journal, state machine, CSV import/export + quarantine, reconciliation."""
from __future__ import annotations

import csv
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from factories import NOW, TODAY, approval, lot, policy, snap

from csp_alerts.journal import import_csv as ic
from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.reconcile import ledger_reason_codes, reconcile
from csp_alerts.journal.state_machine import TRANSITIONS, TransitionError
from csp_alerts.journal.store import Journal, JournalError, NewPosition

P = policy()
EXP = TODAY + timedelta(days=40)


@pytest.fixture
def j(tmp_path):
    clock = {"now": NOW}
    jr = Journal(tmp_path / "j.sqlite3", clock=lambda: clock["now"])
    jr.clock_ref = clock  # type: ignore[attr-defined]
    jr.migrate()
    yield jr
    jr.close()


def newpos(tid="T1", **over):
    base = dict(trade_id=tid, ticker="EXAMPLE", expiration=EXP, strike=D("75.00"), multiplier=100, contracts=1,
                sector="consumer_staples", correlation_bucket="defensive_consumer")
    base.update(over)
    return NewPosition(**base)


def fill(**over):
    base = dict(fill_contracts=1, actual_open_credit_per_share=D("0.50"), opened_at_utc=NOW, fees_usd=D("0.65"))
    base.update(over)
    return base


def to_open(j, tid="T1"):
    j.create_draft(newpos(tid))
    j.transition(tid, S.PENDING)
    j.transition(tid, S.WORKING)
    return j.transition(tid, S.OPEN, **fill())


# ---- storage basics ----
def test_wal_migrations_and_schema(j):
    assert j.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert j.schema_version() == "0005"
    assert j.migrate() == []  # idempotent
    tables = {r[0] for r in j.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"policy_versions", "approved_underlyings", "option_positions", "stock_lots", "account_snapshots",
            "market_observations", "alert_outbox", "audit_log", "import_quarantine", "schema_migrations"} <= tables
    assert j.integrity_ok()


def test_audit_log_is_append_only(j):
    j.create_draft(newpos())
    with pytest.raises(sqlite3.DatabaseError):
        j.conn.execute("UPDATE audit_log SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError):
        j.conn.execute("DELETE FROM audit_log")


def test_decimals_stored_as_text_exactly(j):
    j.create_draft(newpos(strike=D("75.10")))
    row = j.conn.execute("SELECT strike, typeof(strike) t FROM option_positions").fetchone()
    assert row["strike"] == "75.10" and row["t"] == "text"
    assert j.get_position("T1").strike == D("75.10")


def test_policy_recorded_once_with_hash(j):
    assert j.record_policy(P) is True and j.record_policy(P) is False
    row = j.conn.execute("SELECT * FROM policy_versions").fetchone()
    assert row["sha256"] == P.sha256 and row["approved_by"] == "owner"


def test_approved_roundtrip_and_audit(j):
    assert j.list_approved() == []
    a = approval()
    j.upsert_approved(a)
    assert j.list_approved() == [a]
    assert len(j.audit_events("APPROVAL_UPSERT")) == 1


def test_bad_inputs_rejected(j):
    with pytest.raises(JournalError):
        j.create_draft(newpos(strike=D("-1")))
    with pytest.raises(JournalError):
        j.create_draft(newpos(contracts=0))
    j.create_draft(newpos())
    with pytest.raises(JournalError):
        j.create_draft(newpos())  # duplicate trade_id


def test_backup_is_consistent(j, tmp_path):
    j.create_draft(newpos())
    dest = tmp_path / "bk" / "copy.sqlite3"
    j.backup(dest)
    other = Journal(dest)
    assert [p.trade_id for p in other.list_positions()] == ["T1"]
    other.close()


# ---- state machine ----
def test_happy_path_open_close_and_collateral_reservation(j):
    j.create_draft(newpos())
    assert not j.get_position("T1").reserves_collateral  # DRAFT reserves nothing
    j.transition("T1", S.PENDING)
    assert j.get_position("T1").reserves_collateral
    j.transition("T1", S.WORKING)
    p = j.transition("T1", S.OPEN, **fill())
    assert p.actual_open_credit_per_share == D("0.50") and p.fees_usd == D("0.65")
    p = j.transition("T1", S.CLOSE_WORKING)
    assert p.reserves_collateral  # close order does not release collateral
    p = j.transition("T1", S.CLOSED, actual_close_debit_per_share=D("0.20"), closed_at_utc=NOW, fees_usd=D("1.30"))
    assert not p.reserves_collateral and p.actual_close_debit_per_share == D("0.20")
    types = [e["event_type"] for e in j.audit_events()]
    assert types.count("POSITION_TRANSITION") == 5


def test_illegal_transitions_rejected_and_state_unchanged(j):
    j.create_draft(newpos())
    for bad in (S.OPEN, S.CLOSED, S.EXPIRED, S.ASSIGNED_RECONCILED, S.CLOSE_WORKING):
        with pytest.raises(TransitionError):
            j.transition("T1", bad, **fill())
    assert j.get_position("T1").state is S.DRAFT


def test_terminal_states_have_no_exits():
    for s in (S.CLOSED, S.CANCELED, S.EXPIRED, S.ASSIGNED_RECONCILED):
        assert TRANSITIONS[s] == frozenset()


def test_open_requires_explicit_fill_facts(j):
    j.create_draft(newpos())
    j.transition("T1", S.PENDING)
    for missing in ("fill_contracts", "actual_open_credit_per_share", "opened_at_utc"):
        f = fill()
        del f[missing]
        with pytest.raises(TransitionError):
            j.transition("T1", S.OPEN, **f)
    with pytest.raises(TransitionError, match="partial"):
        j.transition("T1", S.OPEN, **fill(fill_contracts=2))
    with pytest.raises(TransitionError):
        j.transition("T1", S.OPEN, **fill(actual_open_credit_per_share=D("0")))
    assert j.get_position("T1").state is S.PENDING


def test_closed_requires_confirmed_fill_facts(j):
    to_open(j)
    j.transition("T1", S.CLOSE_WORKING)
    with pytest.raises(TransitionError):
        j.transition("T1", S.CLOSED, closed_at_utc=NOW, fees_usd=D("0"))
    with pytest.raises(TransitionError):
        j.transition("T1", S.CLOSED, actual_close_debit_per_share=D("0.1"), closed_at_utc=NOW)
    with pytest.raises(TransitionError):
        j.transition("T1", S.CLOSED, actual_close_debit_per_share=D("-0.1"), closed_at_utc=NOW, fees_usd=D("0"))


def test_close_order_cancel_returns_to_open(j):
    to_open(j)
    j.transition("T1", S.CLOSE_WORKING)
    assert j.transition("T1", S.OPEN).state is S.OPEN


def test_cancel_requires_verification_for_live_orders(j):
    j.create_draft(newpos())
    j.transition("T1", S.PENDING)
    with pytest.raises(TransitionError):
        j.transition("T1", S.CANCELED)
    assert j.transition("T1", S.CANCELED, confirmed=True).state is S.CANCELED


def test_expiry_needs_owner_confirmation_and_not_before_expiration(j):
    to_open(j)
    with pytest.raises(TransitionError, match="confirmation"):
        j.transition("T1", S.EXPIRED, closed_at_utc=NOW)
    with pytest.raises(TransitionError, match="before the expiration"):
        j.transition("T1", S.EXPIRED, confirmed=True, closed_at_utc=NOW)
    after = datetime.combine(EXP + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    assert j.transition("T1", S.EXPIRED, confirmed=True, closed_at_utc=after).state is S.EXPIRED


def test_dte_zero_never_auto_expires(j):
    to_open(j)
    j.clock_ref["now"] = datetime.combine(EXP, datetime.min.time(), tzinfo=timezone.utc)
    assert j.get_position("T1").state is S.OPEN
    j.load_book()
    assert j.get_position("T1").state is S.OPEN


def test_unknown_transition_fields_rejected(j):
    j.create_draft(newpos())
    with pytest.raises(TransitionError):
        j.transition("T1", S.PENDING, strike=D("1"))


def test_assignment_flow_links_lot_and_avoids_double_count(j):
    to_open(j)
    j.transition("T1", S.ASSIGNED_PENDING_RECONCILIATION)
    with pytest.raises(TransitionError, match="stock lot"):
        j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)
    with pytest.raises(JournalError, match="ticker differs"):
        j.add_stock_lot(lot("L0", ticker="OTHER", linked_trade_id="T1"))
    j.create_draft(newpos("T9"))
    j.transition("T9", S.PENDING)
    with pytest.raises(JournalError, match="ASSIGNED_PENDING"):
        j.add_stock_lot(lot("L0", ticker="EXAMPLE", linked_trade_id="T9"))
    j.transition("T9", S.CANCELED, confirmed=True)
    j.add_stock_lot(lot("L1", ticker="EXAMPLE", shares="50", price="75", sector="consumer_staples",
                        bucket="defensive_consumer", linked_trade_id="T1", cost_basis_reference_usd=D("3750")))
    with pytest.raises(TransitionError, match="shares"):
        j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)
    j.conn.execute("DELETE FROM stock_lots")  # simulate owner correcting the lot
    j.add_stock_lot(lot("L2", ticker="EXAMPLE", shares="100", price="75", sector="consumer_staples",
                        bucket="defensive_consumer", linked_trade_id="T1"))
    with pytest.raises(TransitionError, match="purchase cash"):
        j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)
    j.conn.execute("UPDATE stock_lots SET cost_basis_reference_usd='7500'")
    p = j.transition("T1", S.ASSIGNED_RECONCILED, confirmed=True)
    book = j.load_book()
    from csp_alerts.policy.portfolio import compute_exposure
    ex = compute_exposure(book)
    assert p.state is S.ASSIGNED_RECONCILED and ex.put_collateral_usd == 0 and ex.owned_stock_usd == D("7500")
    with pytest.raises(JournalError):  # a put converts into at most one lot
        j.add_stock_lot(lot("L3", ticker="EXAMPLE", linked_trade_id="T1"))


def test_failed_transition_does_not_write_audit_or_partial_state(j):
    j.create_draft(newpos())
    n = len(j.audit_events())
    with pytest.raises(TransitionError):
        j.transition("T1", S.OPEN, **fill())
    assert len(j.audit_events()) == n


def test_concurrent_connection_cannot_double_write(tmp_path):
    a = Journal(tmp_path / "c.sqlite3")
    a.migrate()
    b = Journal(tmp_path / "c.sqlite3")
    a.create_draft(newpos())
    with pytest.raises(JournalError):
        b.create_draft(newpos())  # unique key enforced across connections
    a.close(); b.close()


# ---- snapshots / reset ----
def test_snapshot_validation_and_book_load(j):
    with pytest.raises(JournalError):
        j.add_snapshot(snap(equity="0"))
    with pytest.raises(JournalError):
        j.add_snapshot(snap(cash="-1"))
    with pytest.raises(JournalError, match="future"):
        j.add_snapshot(snap(asof=NOW + timedelta(hours=1)))
    j.add_snapshot(snap(asof=NOW - timedelta(hours=1)))
    with pytest.raises(JournalError):
        j.add_snapshot(snap(asof=NOW - timedelta(hours=1), sid="dup"))  # one snapshot per timestamp
    assert j.load_book().latest_snapshot.equity_usd == D("100000")


def test_risk_reset_requires_confirmed_snapshot_and_note(j):
    with pytest.raises(JournalError):
        j.record_risk_reset("reviewed")
    j.add_snapshot(snap(asof=NOW - timedelta(hours=2)))
    with pytest.raises(JournalError):
        j.record_risk_reset("  ")
    base = j.record_risk_reset("reviewed")
    assert base == NOW - timedelta(hours=2) and j.load_book().last_risk_reset_utc == base


def test_ledger_change_marks_book_and_requires_new_snapshot(j):
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(asof=NOW - timedelta(hours=1)))
    assert not reconcile(j.load_book(), P, NOW).blocking
    j.clock_ref["now"] = NOW + timedelta(minutes=10)
    j.create_draft(newpos())  # drafts are not executed trades -> no re-confirmation needed
    assert not reconcile(j.load_book(), P, NOW + timedelta(minutes=10)).blocking
    j.transition("T1", S.PENDING)
    rep = reconcile(j.load_book(), P, NOW + timedelta(minutes=10))
    assert [i.code for i in rep.issues] == ["CHANGE_AFTER_SNAPSHOT"] and rep.blocking
    j.add_snapshot(snap(asof=NOW + timedelta(minutes=9), sid="s2"))  # asof must be after the change
    assert reconcile(j.load_book(), P, NOW + timedelta(minutes=10)).blocking
    j.clock_ref["now"] = NOW + timedelta(minutes=20)
    j.add_snapshot(snap(asof=NOW + timedelta(minutes=15), sid="s3"))
    assert not reconcile(j.load_book(), P, NOW + timedelta(minutes=20)).blocking


# ---- reconcile ----
def test_reconcile_flags(j):
    from factories import book, put
    rep = reconcile(book(snaps=[]), P, NOW)
    assert ledger_reason_codes(rep) == ["LEDGER_STALE"]
    exp_past = put(state=S.OPEN, exp=TODAY - timedelta(days=1))
    assert "EXPIRY_UNCONFIRMED" in [i.code for i in reconcile(book(positions=[exp_past]), P, NOW).issues]
    nofill = put(state=S.OPEN, actual_open_credit_per_share=None)
    assert "OPEN_WITHOUT_FILL" in [i.code for i in reconcile(book(positions=[nofill]), P, NOW).issues]
    adj = put(state=S.ADJUSTMENT_REVIEW)
    assert "ADJUSTMENT_REVIEW_PENDING" in [i.code for i in reconcile(book(positions=[adj]), P, NOW).issues]
    rec = put(trade_id="A", state=S.ASSIGNED_RECONCILED)
    assert "ASSIGNED_WITHOUT_LOT" in [i.code for i in reconcile(book(positions=[rec]), P, NOW).issues]
    active = put(trade_id="A", state=S.OPEN)
    linked = lot(linked_trade_id="A")
    assert "DOUBLE_COUNT_RISK" in [i.code for i in reconcile(book(positions=[active], lots=[linked]), P, NOW).issues]
    dup = [put(trade_id="1", ticker="X"), put(trade_id="2", ticker="X")]
    r = reconcile(book(positions=dup), P, NOW)
    assert any(i.code == "MULTIPLE_ACTIVE_PUTS_SAME_UNDERLYING" and i.severity == "WARN" for i in r.issues)
    assert not r.blocking
    assert not reconcile(book(), P, NOW).issues


# ---- CSV ----
HEADER = ",".join(ic.COLUMNS)
OPEN_ROW = "T1,paper_primary,1,EXAMPLE,{exp},75.00,100,1,consumer_staples,defensive_consumer,OPEN,2026-10-30T14:00:00Z,0.50,,0.65,,note"


def write_csv(path, rows, header=HEADER):
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_import_roundtrip_with_dry_run(j, tmp_path):
    f = write_csv(tmp_path / "in.csv", [OPEN_ROW.format(exp=EXP.isoformat())])
    rep = ic.import_positions_csv(j, f, dry_run=True)
    assert rep.accepted == ["T1"] and rep.ok and j.list_positions() == []  # dry-run wrote nothing
    rep = ic.import_positions_csv(j, f, dry_run=False)
    assert rep.ok and j.get_position("T1").state is S.OPEN
    assert j.audit_events("POSITION_IMPORTED")
    out = tmp_path / "exports" / "out.csv"
    assert ic.export_positions_csv(j, out) == 1
    assert list(csv.reader(out.open(encoding="utf-8")))[1] == OPEN_ROW.format(exp=EXP.isoformat()).split(",")
    j2 = Journal(tmp_path / "other.sqlite3")
    j2.migrate()
    rep2 = ic.import_positions_csv(j2, out, dry_run=False)
    from dataclasses import replace
    stamp = j.get_position("T1").last_reconciled_at_utc
    assert rep2.ok and replace(j2.get_position("T1"), last_reconciled_at_utc=stamp) == j.get_position("T1")
    j2.close()


@pytest.mark.parametrize("row,fragment", [
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,-75.00,100,1,s,b,OPEN,2026-10-30T14:00:00Z,0.50,,0,,", "strike"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,0,s,b,OPEN,2026-10-30T14:00:00Z,0.50,,0,,", "contracts"),
    ("T2,paper_primary,1,EXAMPLE,12/04/2026,75.00,100,1,s,b,OPEN,2026-10-30T14:00:00Z,0.50,,0,,", "expiration"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,OPEN,,,,0,,", "requires"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,OPEN,2026-10-30T14:00:00Z,NaN,,0,,", "actual_open_credit"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,OPEN,2026-10-30T14:00:00Z,0.50,,-1,,", "fees_usd"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,BOGUS,,,,0,,", "state"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,CLOSED,2026-10-30T14:00:00Z,0.50,,0,,", "CLOSED requires"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,OPEN,2026-10-30T14:00:00Z,0.50,0.2,0,,", "closing debit"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,PENDING,2026-10-30T14:00:00Z,0.50,,0,,", "fill data"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,OPEN,2026-10-30 14:00:00,0.50,,0,,", "UTC"),
    ("T2,paper_primary,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,OPEN,2026-10-30T14:00:00Z,0.50,,0,,,extra", "columns"),
    ("T2,paper_primary,maybe,EXAMPLE,2026-12-04,75.00,100,1,s,b,DRAFT,,,,0,,", "paper"),
    ("T2,,1,EXAMPLE,2026-12-04,75.00,100,1,s,b,DRAFT,,,,0,,", "account_scope"),
])
def test_bad_rows_are_quarantined_not_corrected(j, tmp_path, row, fragment):
    f = write_csv(tmp_path / "bad.csv", [OPEN_ROW.format(exp=EXP.isoformat()), row])
    rep = ic.import_positions_csv(j, f, dry_run=False)
    assert rep.accepted == ["T1"] and len(rep.quarantined) == 1
    assert fragment in " ".join(rep.quarantined[0].errors)
    assert rep.quarantined[0].row_number == 3
    assert [p.trade_id for p in j.list_positions()] == ["T1"]
    q = j.conn.execute("SELECT * FROM import_quarantine").fetchall()
    assert len(q) == 1 and q[0]["row_number"] == 3
    ic.write_quarantine_csv(rep, tmp_path / "q.csv")
    assert (tmp_path / "q.csv").read_text(encoding="utf-8").count("\n") >= 2


def test_duplicates_and_existing_ids_quarantined_and_never_overwrite(j, tmp_path):
    row = OPEN_ROW.format(exp=EXP.isoformat())
    f = write_csv(tmp_path / "dup.csv", [row, row.replace("0.50,,0.65", "0.60,,0.65")])
    rep = ic.import_positions_csv(j, f, dry_run=False)
    assert rep.accepted == ["T1"] and "duplicate trade_id" in rep.quarantined[0].errors[0]
    assert j.get_position("T1").actual_open_credit_per_share == D("0.50")
    rep = ic.import_positions_csv(j, f, dry_run=False)  # re-import: everything already exists
    assert rep.accepted == [] and len(rep.quarantined) == 2
    assert j.get_position("T1").actual_open_credit_per_share == D("0.50")


def test_bad_header_rejects_whole_file(j, tmp_path):
    f = write_csv(tmp_path / "h.csv", [OPEN_ROW.format(exp=EXP.isoformat())], header=HEADER.replace("strike", "strik"))
    with pytest.raises(ic.CsvFormatError):
        ic.import_positions_csv(j, f)
    assert j.list_positions() == []
    with pytest.raises(ic.CsvFormatError):
        ic.import_positions_csv(j, tmp_path / "missing.csv")


def test_import_marks_ledger_changed(j, tmp_path):
    j.add_snapshot(snap(asof=NOW - timedelta(hours=1)))
    j.clock_ref["now"] = NOW + timedelta(minutes=1)
    f = write_csv(tmp_path / "in.csv", [OPEN_ROW.format(exp=EXP.isoformat())])
    ic.import_positions_csv(j, f, dry_run=False)
    assert j.load_book().last_ledger_change_utc == NOW + timedelta(minutes=1)


def test_nan_inputs_raise_clean_errors(j):
    j.create_draft(newpos())
    j.transition("T1", S.PENDING)
    with pytest.raises(TransitionError):
        j.transition("T1", S.OPEN, **fill(actual_open_credit_per_share=D("NaN")))
    with pytest.raises(JournalError):
        j.add_snapshot(snap(equity="NaN"))
    with pytest.raises(JournalError):
        j.add_snapshot(snap(cash="Infinity", sid="x"))


def test_csv_closed_at_contradictions(tmp_path, j):
    base = "T1,paper_primary,1,EXAMPLE,{exp},75.00,100,1,s,b,{st},{op},{cr},,0,{cl},"
    rows = [
        base.format(exp="2026-12-04", st="OPEN", op="2026-10-30T14:00:00Z", cr="0.5", cl="2026-10-31T14:00:00Z"),
        base.format(exp="2026-12-04", st="DRAFT", op="", cr="", cl="2026-10-31T14:00:00Z").replace("T1", "T2", 1),
        base.format(exp="2026-12-04", st="EXPIRED", op="2026-10-30T14:00:00Z", cr="0.5", cl="2026-11-01T14:00:00Z").replace("T1", "T3", 1),
    ]
    rep = ic.import_positions_csv(j, write_csv(tmp_path / "c.csv", rows), dry_run=True)
    assert len(rep.quarantined) == 3


def test_baseline_event_audited_once_and_required(j):
    j.add_snapshot(snap(asof=NOW - timedelta(hours=1)))
    rep = reconcile(j.load_book(), P, NOW)
    assert [i.code for i in rep.issues] == ["NO_BASELINE"] and ledger_reason_codes(rep) == ["LEDGER_UNRECONCILED"]
    for bad in (D("0"), D("NaN"), D("-5")):
        with pytest.raises(JournalError):
            j.confirm_baseline(bad)
    j.confirm_baseline(D("100000"), note="owner confirmed")
    ev = j.audit_events("BASELINE_CONFIRMED")
    assert len(ev) == 1 and "100000" in ev[0]["after_json"] and "confirmed_at_utc" in ev[0]["after_json"]
    assert not reconcile(j.load_book(), P, NOW).blocking
    assert j.load_book().baseline_equity_usd == D("100000")
    with pytest.raises(JournalError):
        j.confirm_baseline(D("90000"))


def test_gross_free_cash_is_flagged(j):
    from factories import book, put
    b = book(positions=[put(strike="50")], snaps=[snap(equity="100000", cash="96000")])  # 96,000 + 5,000 > 100,000
    rep = reconcile(b, P, NOW)
    assert [i.code for i in rep.issues] == ["CASH_INCONSISTENT"] and rep.blocking
    ok = book(positions=[put(strike="50")], snaps=[snap(equity="100000", cash="95000")])
    assert not reconcile(ok, P, NOW).issues


def test_synthetic_approval_rejected_by_journal(j):
    with pytest.raises(JournalError):
        j.upsert_approved(approval(synthetic=True))


def test_mixed_account_scope_blocks_entries():
    from factories import book, put
    rep = reconcile(book(positions=[put(account_scope="paper_other")]), P, NOW)
    assert "MIXED_ACCOUNT_SCOPE" in [i.code for i in rep.issues] and rep.blocking
    assert not reconcile(book(positions=[put()]), P, NOW).issues
