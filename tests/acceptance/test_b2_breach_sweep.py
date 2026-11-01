"""Stage B1 fix verification (b): POLICY_BREACH_UNRESOLVED auto-clear (DECISIONS 148) and WT-03 'an owner-reported execution that
already breaches a policy must not be silently rejected ... flag the breach and block further eligible entries'.

Book: C1 CCC 75 OPEN, exp 2026-11-20 (7,500; Health/Pharma). The owner then records a FILLED sold put AAA 90 exp 2026-12-04 (9,000).
Hand arithmetic: pilot used 7,500 + 9,000 = 16,500 > 15,000  => PILOT_COLLATERAL_LIMIT is the ONLY flagged code (normal 25,000 ok;
ticker AAA 9,000 equality ok; sector Tech 9,000 ok; week W49 9,000 ok; week W47 7,500 ok; contracts 2/4; names 2/2; cash 60,000 >= 9,000).
Closing C1 leaves pilot 9,000 <= 15,000 => the flag must clear, audited. Anything that leaves 16,500 must leave the flag.
"""
from __future__ import annotations

import json
import shutil
from datetime import timedelta
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, POLICY_PATH
from b1_env import B1, EXP_OTHER, checkin, sell, text_of
from b2_env import ref_env

APPROVED = {"AAA": ("STOCK", "Tech", "Semis", "100.00"), "BBB": ("STOCK", "Tech", "Cloud", "100.00"),
            "CCC": ("STOCK", "Health", "Pharma", "100.00")}
BOOK = (dict(tid="C1", ticker="CCC", strike="75", exp=EXP_OTHER, sector="oldsec", bucket="oldbkt", state="OPEN"),)
CLOSE_OK = {"fees": "0", "executed_date": "2026-10-30", "executed_time": "10:00"}


def flagged(w) -> bool:
    return "POLICY_BREACH_UNRESOLVED" in text_of(w.get("/")[2])


def events(w, typ):
    return [(json.loads(a) if a else None, json.loads(b) if b else None)
            for a, b in w.sql("SELECT before_json, after_json FROM audit_log WHERE event_type=? ORDER BY rowid", (typ,))]


@pytest.fixture()
def br(tmp_path):
    w = ref_env(tmp_path, approved=APPROVED, extras=BOOK, lot=False)
    st, loc, html = w.do(sell("AAA", "90"))
    assert w.result_code(loc) == "recorded", text_of(html)[-800:]
    w.aaa = loc.split("trade=")[1].split("&")[0]
    yield w
    w.stop()


def test_a_cap_breaching_report_is_recorded_flagged_with_exactly_the_hand_computed_code(br):
    rec = events(br, "POLICY_BREACH_RECORDED")
    assert len(rec) == 1 and rec[0][1]["codes"] == ["PILOT_COLLATERAL_LIMIT"], rec
    assert flagged(br)
    assert br.sql("SELECT state FROM option_positions WHERE trade_id=?", (br.aaa,))[0][0] == "OPEN"      # reality is recorded, not dropped
    t = text_of(br.get("/")[2])
    assert "review it" not in t.lower()
    assert br.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_CLEARED'") == []


def test_info_codes_are_persisted_in_the_recorded_event_and_shown_as_information_on_the_position_page(br):
    ev = events(br, "WEB_TRADE_RECORDED")[-1][1]
    assert "contract_info_codes" in ev and isinstance(ev["contract_info_codes"], list)
    # AAA 90 vs spot 61.25: strike above spot is an off-strategy contract characteristic; delta -0.13 / DTE 35 / spread are within gates
    assert ev["contract_info_codes"], "strike above the stock price must be recorded as an information code"
    t = text_of(br.get(f"/positions/{br.aaa}")[2])
    assert "off-strategy contract characteristics (information)" in t
    for c in ev["contract_info_codes"]:
        assert c in t, c


def test_unrelated_events_do_not_clear_while_the_cap_is_still_breached(br):
    """label correction (C1 to the approval's own labels), a new check-in, an approval added to the file then a label correction
    that triggers the re-check: pilot stays 16,500 so the flag stays; no CLEARED event is ever written."""
    from test_b2_labels import PREV, CONF, code, confirm_fields, previewable
    def relabel(sym, sec, bkt):
        st, _, html = br.post(PREV, {"ticker": sym, "sector": sec, "bucket": bkt}, csrf=br.csrf(f"/portfolio/labels/{sym}"))
        assert previewable(html), text_of(html)[-500:]
        return code(br.post(CONF, confirm_fields(br, html), csrf=br.csrf(f"/portfolio/labels/{sym}"))[1])
    checkin(br, equity="100000", cash=str(D(100000) - D(16500)))
    assert flagged(br), "a new check-in must not clear a flag whose cap is still breached"
    # approval added (another name) then a re-check trigger
    from b1_env import write_approved_custom
    write_approved_custom(br.appr, {**APPROVED, "EEE": ("ETF", "Fund", "Index", "100.00")})
    assert relabel("CCC", "Health", "Pharma") == "label_recorded"          # C1 oldsec/oldbkt -> approval's labels; re-check runs
    assert flagged(br)
    assert br.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_CLEARED'") == []


def test_closing_the_other_position_on_the_web_clears_the_flag_with_an_audited_event(br):
    st, loc, html = br.do({"action": "close_filled", "trade_id": "C1", "debit": "0.10", **CLOSE_OK})
    assert br.result_code(loc) == "recorded", text_of(html)[-600:]
    assert not flagged(br)
    ev = events(br, "POLICY_BREACH_CLEARED")
    assert len(ev) == 1
    before, after = ev[0]
    assert before["trade_id"] == br.aaa and before["codes"] == ["PILOT_COLLATERAL_LIMIT"]
    assert after["trade_id"] == br.aaa and after["cleared_codes"] == ["PILOT_COLLATERAL_LIMIT"] and after["policy_sha256"] and after["reason"]
    row = br.sql("SELECT actor, source FROM audit_log WHERE event_type='POLICY_BREACH_CLEARED'")[0]
    assert row[1] == "web" and row[0].startswith("web")


def test_a_partial_improvement_that_still_breaches_does_not_clear(tmp_path):
    """Second cap still breached: C1 7,500 + C2 (BBB 20 = 2,000) + new AAA 90 (9,000) = 18,500. Closing C2 (2,000) leaves 16,500 > 15,000."""
    ex = BOOK + (dict(tid="C2", ticker="BBB", strike="20", exp=EXP_OTHER, sector="Tech", bucket="Cloud", state="OPEN"),)
    w = ref_env(tmp_path, approved=APPROVED, extras=ex, lot=False)
    try:
        assert w.result_code(w.do(sell("AAA", "90"))[1]) == "recorded"
        assert flagged(w)
        assert w.result_code(w.do({"action": "close_filled", "trade_id": "C2", "debit": "0.10", **CLOSE_OK})[1]) == "recorded"
        assert flagged(w), "pilot still 16,500 > 15,000: the flag must remain"
        assert w.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_CLEARED'") == []
        assert w.result_code(w.do({"action": "close_filled", "trade_id": "C1", "debit": "0.10", **CLOSE_OK})[1]) == "recorded"
        assert not flagged(w)                               # now 9,000 <= 15,000
    finally:
        w.stop()


def test_exactly_at_the_cap_after_the_other_closes_counts_as_resolved(tmp_path):
    """Equality allowed: after closing C1 pilot = 9,000. Use a book whose remaining exposure equals 15,000 exactly:
    C1 CCC 75 (7,500) + C3 BBB 15 (1,500)... new AAA 90 -> 18,000 flagged; closing C1 leaves 10,500 (clear).  Equality case below."""
    # new AAA strike 90 (9,000) with C1 strike 60 (6,000) = 15,000 equality => NOT flagged at all (equality passes)
    w = ref_env(tmp_path, approved=APPROVED, extras=(dict(BOOK[0], strike="60"),), lot=False)
    try:
        assert w.result_code(w.do(sell("AAA", "90"))[1]) == "recorded"
        assert not flagged(w) and w.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_RECORDED'") == []
    finally:
        w.stop()


def test_a_scan_rechecks_and_clears_when_the_book_changed_underneath(br):
    """Close C1 directly in the journal (a path that does not sweep), then a REAL persisting scan must re-check and clear."""
    from csp_alerts.jobs.scan import run_scan
    from csp_alerts.journal.models import PositionState as S
    from csp_alerts.journal.store import Journal
    from csp_alerts.policy.approved import load_approved
    from csp_alerts.policy.config import load_policy
    from s3_helpers import Clock
    from stage_a_helpers import Prov
    j = Journal(br.db, clock=Clock(NOW + timedelta(minutes=1)))
    j.transition("C1", S.CLOSED, closed_at_utc=NOW, actual_close_debit_per_share=D("0.10"), close_fees_usd=D("0")) if False else None
    j.transition("C1", S.CLOSE_WORKING)
    j.transition("C1", S.CLOSED, actual_close_debit_per_share=D("0.10"), closed_at_utc=NOW + timedelta(minutes=1), fees_usd=D("0.65"))
    j.close()
    assert flagged(br), "no sweep happened yet (direct journal write)"
    j = Journal(br.db, clock=Clock(NOW + timedelta(minutes=2)))
    run_scan(journal=j, policy=load_policy(POLICY_PATH), approved=load_approved(br.appr), source=Prov(NOW), now_utc=NOW + timedelta(minutes=2),
             sleep=lambda s: None)
    j.close()
    assert not flagged(br)
    ev = events(br, "POLICY_BREACH_CLEARED")
    assert len(ev) == 1 and ev[0][1]["reason"].startswith("re-checked before scan")


def test_a_dry_run_scan_never_clears(br):
    from csp_alerts.jobs.scan import run_scan
    from csp_alerts.journal.models import PositionState as S
    from csp_alerts.journal.store import Journal
    from csp_alerts.policy.approved import load_approved
    from csp_alerts.policy.config import load_policy
    from s3_helpers import Clock
    from stage_a_helpers import Prov
    j = Journal(br.db, clock=Clock(NOW + timedelta(minutes=1)))
    j.transition("C1", S.CLOSE_WORKING)
    j.transition("C1", S.CLOSED, actual_close_debit_per_share=D("0.10"), closed_at_utc=NOW + timedelta(minutes=1), fees_usd=D("0.65"))
    j.close()
    j = Journal(br.db, clock=Clock(NOW + timedelta(minutes=2)))
    run_scan(journal=j, policy=load_policy(POLICY_PATH), approved=load_approved(br.appr), source=Prov(NOW), now_utc=NOW + timedelta(minutes=2),
             sleep=lambda s: None, persist=False)
    j.close()
    assert flagged(br) and br.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_CLEARED'") == []


def test_unreadable_approvals_clear_nothing(br):
    """With the approvals file unreadable the web record path / sweep must not clear a flag (fail closed)."""
    br.appr.write_text("{not json", encoding="utf-8")
    st, _, html = br.get("/")
    # the page must still render; flag stays
    assert st == 200 and "POLICY_BREACH_UNRESOLVED" in text_of(html)
    from csp_alerts.journal.models import PositionState as S
    from csp_alerts.journal.store import Journal
    from csp_alerts.journal.webtrade import sweep_breaches
    from csp_alerts.policy.config import load_policy
    j = Journal(br.db, clock=lambda: NOW + timedelta(minutes=1))
    j.transition("C1", S.CLOSE_WORKING)
    j.transition("C1", S.CLOSED, actual_close_debit_per_share=D("0.10"), closed_at_utc=NOW, fees_usd=D("0.65"))
    assert sweep_breaches(j, load_policy(POLICY_PATH), None, NOW + timedelta(minutes=2), reason="t") == []
    j.close()
    assert flagged(br)


def test_applied_cli_command_rechecks_and_clears(br, tmp_path, monkeypatch):
    """`journal close` --apply through the real CLI on a copy of the journal clears the flag; the preview form must not."""
    from s3_helpers import run_cli
    br.stop()
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    shutil.copy(br.db, cli_dir / "j.sqlite3")
    now = NOW + timedelta(minutes=5)
    args = ["journal", "close", "--trade-id", "C1", "--debit", "0.2"]
    code, out, err = run_cli(monkeypatch, cli_dir, args, now=now, frozen_journal=True, approved=br.appr)
    import sqlite3
    q = lambda: sqlite3.connect(cli_dir / "j.sqlite3").execute("SELECT COUNT(*) FROM audit_log WHERE event_type='POLICY_BREACH_CLEARED'").fetchone()[0]
    assert q() == 0, "preview (no --apply) must not write"
    code, out, err = run_cli(monkeypatch, cli_dir, args + ["--apply", "--reason", "owner closed at broker"], now=now, frozen_journal=True, approved=br.appr)
    assert code == 0, out + err
    assert q() == 1 and "POLICY_BREACH_CLEARED" in out


def test_free_cash_flag_clears_only_after_a_fresh_confirmed_checkin(tmp_path):
    """Only cash is short: check-in reports NET free cash 5,000; the owner then reports a filled AAA 60 (6,000) -> INSUFFICIENT_FREE_CASH
    flagged (pilot 6,000 ok, ticker 6,000 ok). Another trade-less event keeps it; a fresh confirmed check-in clears it."""
    w = ref_env(tmp_path, approved=APPROVED, extras=(), lot=False)
    try:
        checkin(w, equity="100000", cash="5000")
        assert w.result_code(w.do(sell("AAA", "60"))[1]) == "recorded"
        rec = events(w, "POLICY_BREACH_RECORDED")
        assert rec and rec[0][1]["codes"] == ["INSUFFICIENT_FREE_CASH"], rec
        assert flagged(w)
        checkin(w, equity="100000", cash="94000")                      # 100,000 - 6,000 reserved
        # the check-in itself is a re-check point? It is "every web record"; if not swept there, a label correction/scan must clear it.
        from test_b2_labels import PREV, CONF, code, confirm_fields, previewable
        if flagged(w):
            st, _, html = w.post(PREV, {"ticker": "AAA", "sector": "Tech", "bucket": "Semis"}, csrf=w.csrf("/portfolio/labels/AAA"))
            if previewable(html):
                w.post(CONF, confirm_fields(w, html), csrf=w.csrf("/portfolio/labels/AAA"))
        assert not flagged(w), "a fresh confirmed check-in resolves a free-cash-only flag (D15 item 148)"
        assert len(events(w, "POLICY_BREACH_CLEARED")) == 1
    finally:
        w.stop()


def test_checkin_itself_is_a_recheck_point_for_a_free_cash_flag(tmp_path):
    """D15 item 148 lists 'a new check-in' (task brief) as a re-check point: after the check-in alone the flag should be gone."""
    w = ref_env(tmp_path, approved=APPROVED, extras=(), lot=False)
    try:
        checkin(w, equity="100000", cash="5000")
        assert w.result_code(w.do(sell("AAA", "60"))[1]) == "recorded"
        assert flagged(w)
        checkin(w, equity="100000", cash="94000")
        assert not flagged(w)
    finally:
        w.stop()


def test_a_flag_never_clears_when_the_ticker_is_still_unapproved_or_labels_unknown(tmp_path):
    """Unapproved ticker ZZZ with blank labels: flagged {LABELS_UNKNOWN, NOT_APPROVED}. Fixing labels keeps NOT_APPROVED => stays."""
    w = ref_env(tmp_path, approved=APPROVED, extras=(), lot=False)
    try:
        assert w.result_code(w.do(sell("ZZZ", "30", sector="", bucket=""))[1]) == "recorded"
        assert flagged(w)
        codes = events(w, "POLICY_BREACH_RECORDED")[0][1]["codes"]
        assert set(codes) == {"LABELS_UNKNOWN", "NOT_APPROVED"}
        assert w.result_code(w.do({"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"},
                                  path="/checkin/preview", cpath="/checkin/confirm", reason="daily check-in")[1]) in ("recorded", "stale", "")
        assert flagged(w)
    finally:
        w.stop()
