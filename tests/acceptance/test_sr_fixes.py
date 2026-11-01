"""Independent acceptance for the re-verified review defects SR-01..SR-03 and SR-05..SR-11
(oracles: requirements/OPEN_ISSUES.md "Recently corrected probe cases", spec 4.2/8.1/15, FIRST_DELIVERY_SCOPE;
docs/DECISIONS.md is used only for the explicit SR-10 session convention).

Frozen clock NOW = Fri 2026-10-30 15:05:02Z = 11:05:02 EDT (DST ends Sun 2026-11-01 02:00 local).
All data synthetic; the only socket is the loopback FakeTelegram; the real Telegram host is never contacted.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from acc_helpers import NOW, OPEN_SESSION, REPO, TODAY, book, codes, lot, policy, put, run, snap
from s2_helpers import Boom, FakeSource, raw_row
from s3_helpers import CHAT, TOKEN, FakeTelegram, chain_rows_for, dead_port_transport, ledger, run_cli

from csp_alerts import runner
from csp_alerts.alerts import outbox
from csp_alerts.alerts.telegram import chat_id_sha256
from csp_alerts.jobs.monitor import run_monitor
from csp_alerts.journal.models import AccountSnapshot, PositionState as S, StockLot
from csp_alerts.journal.import_csv import COLUMNS, import_positions_csv
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.policy.drawdown import DrawdownLevel
from csp_alerts.policy.entry import book_entry_blocks
from csp_alerts.policy.exits import OptionQuote, Trigger, evaluate_exits
from csp_alerts.policy.portfolio import compute_exposure
from csp_alerts.settings import StartupError, settings_from_mapping

POL = policy()
CLOSED = SessionInfo(SessionStatus.CLOSED)
EXP = date(2026, 12, 4)          # 35 calendar days after NOW's exchange date
from csp_alerts.alerts.sendmode import Mode, SendDecision  # noqa: E402

SEND = SendDecision(Mode.SEND)


def kinds(sigs):
    return [s.trigger.value for s in sigs]


def issue_codes(b, now=NOW):
    return {i.code for i in reconcile(b, POL, now).issues}


# ============================================================================================== SR-01 quote validity
def _pos(**kw):
    # credit 0.50/share, strike 75, 100 multiplier, 1 contract, DTE 35 (no calendar trigger)
    return put(trade_id="P1", ticker="AAA", strike="75.00", expiration=EXP, **kw)


def _q(bid, ask, delta="-0.13", spot="80", at=NOW):
    return OptionQuote(_pos().contract_key, at, bid=None if bid is None else D(bid), ask=None if ask is None else D(ask),
                       delta=D(delta), spot=D(spot))


@pytest.mark.parametrize("bid", ["0", "0.00", "-0.01", "NaN", "Infinity", None])
def test_sr01_zero_negative_nan_or_missing_bid_never_gives_profit_advice(bid):
    # ask 0.20 would be exactly 60% captured ((0.50-0.20)/0.50 = 0.60) but the quote is not two-sided -> DATA_ISSUE only
    k = kinds(evaluate_exits(_pos(), _q(bid, "0.20"), POL, NOW))
    assert "PROFIT_EXIT" not in k and "LOSS_REVIEW" not in k and "DELTA_REVIEW" not in k
    assert "DATA_ISSUE" in k


@pytest.mark.parametrize("ask", ["0", "-0.05", "NaN", None])
def test_sr01_bad_ask_never_gives_profit_advice(ask):
    k = kinds(evaluate_exits(_pos(), _q("0.15", ask), POL, NOW))
    assert "PROFIT_EXIT" not in k and "DATA_ISSUE" in k


def test_sr01_smallest_valid_two_sided_quote_still_fires_at_the_60pct_boundary():
    # bid 0.01 / ask 0.20: captured (0.50-0.20)/0.50 = 0.60 >= 0.60 -> PROFIT_EXIT, no DATA_ISSUE
    k = kinds(evaluate_exits(_pos(), _q("0.01", "0.20"), POL, NOW))
    assert "PROFIT_EXIT" in k and "DATA_ISSUE" not in k
    # locked market bid == ask is not crossed
    assert "PROFIT_EXIT" in kinds(evaluate_exits(_pos(), _q("0.20", "0.20"), POL, NOW))
    # crossed (ask < bid) is a data issue
    k2 = kinds(evaluate_exits(_pos(), _q("0.25", "0.20"), POL, NOW))
    assert "PROFIT_EXIT" not in k2 and "DATA_ISSUE" in k2


def test_sr01_end_to_end_monitor_zero_bid_row_emits_data_issue_not_profit_exit():
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00", expiration=EXP)])
    src = FakeSource({"AAA": chain_rows_for(75.0, EXP, 0.0, 0.20)})
    res = run_monitor(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN_SESSION)
    t = {s.trigger.value for _, s in res.signals}
    assert "PROFIT_EXIT" not in t and "DATA_ISSUE" in t and res.status == "DATA_UNAVAILABLE"


def test_sr01_tradeoff_worthless_zero_bid_is_data_issue_but_calendar_alerts_survive():
    """Implementer trade-off: a 0.00 bid / 0.05 ask put (90% captured) now yields DATA_ISSUE, not PROFIT_EXIT.
    ACCEPTABLE: OPEN_ISSUES SR-01 requires the documented valid two-sided Tier R rule 'consistently'; the owner is told to check
    the broker (fail closed). It is a missed convenience alert, not a wrong one. Calendar alerts must still fire."""
    pos = put(trade_id="P1", ticker="AAA", strike="75.00", expiration=TODAY + timedelta(days=21))   # DTE 21 -> TIME_EXIT
    q = OptionQuote(pos.contract_key, NOW, bid=D("0"), ask=D("0.05"), delta=D("-0.02"), spot=D("90"))
    k = kinds(evaluate_exits(pos, q, POL, NOW))
    assert "PROFIT_EXIT" not in k and "DATA_ISSUE" in k and "TIME_EXIT" in k


# ============================================================================================== SR-02 identity
def _mon_rows(rows):
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00", expiration=EXP)])
    return run_monitor(journal=j, policy=POL, source=FakeSource({"AAA": rows}), now_utc=NOW, session=OPEN_SESSION)


def _sig(res):
    return {s.trigger.value for _, s in res.signals}


def test_sr02_control_matching_put_symbol_still_gets_profit_advice():
    # captured (0.50-0.20)/0.50 = 60%
    res = _mon_rows([raw_row(strike=75.0, expiration=EXP.isoformat(), bid=0.18, ask=0.20, contractSymbol="AAA261204P00075000")])
    assert "PROFIT_EXIT" in _sig(res)


@pytest.mark.parametrize("sym", [
    "AAA261204C00075000",     # CALL symbol on a put row (the reviewer repro)
    "AAA261204P00080000",     # wrong strike
    "AAA261211P00075000",     # wrong expiry
    "ZZZ261204P00075000",     # wrong root
    "AAA1261204P00075000",    # adjusted root
    "garbage",
])
def test_sr02_contradictory_identity_is_data_issue_never_put_advice(sym):
    res = _mon_rows([raw_row(strike=75.0, expiration=EXP.isoformat(), bid=0.18, ask=0.20, contractSymbol=sym)])
    assert not ({"PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW"} & _sig(res)), sym
    assert "DATA_ISSUE" in _sig(res), sym


def test_sr02_call_typed_row_is_not_used_as_a_put_quote():
    res = _mon_rows([raw_row(type="call", strike=75.0, expiration=EXP.isoformat(), bid=0.18, ask=0.20)])
    assert "PROFIT_EXIT" not in _sig(res)


def test_sr02_two_rows_for_the_same_contract_are_ambiguous_not_picked():
    good = raw_row(strike=75.0, expiration=EXP.isoformat(), bid=0.18, ask=0.20)
    other = raw_row(strike=75.0, expiration=EXP.isoformat(), bid=0.90, ask=1.10)
    res = _mon_rows([good, other])
    assert "PROFIT_EXIT" not in _sig(res) and "LOSS_REVIEW" not in _sig(res)


def test_sr02_contradiction_text_is_not_silently_rewritten_to_the_expected_key():
    res = _mon_rows([raw_row(strike=75.0, expiration=EXP.isoformat(), bid=0.18, ask=0.20, contractSymbol="AAA261204C00075000")])
    txt = "\n".join(m.text for m in res.messages)
    assert "PROFIT" not in txt.upper().replace("DATA ISSUE", "") or "DATA" in txt.upper()
    assert "NO ORDER SENT" in txt or "order" in txt.lower()


# ============================================================================================== SR-03 assignment
def _pending_ledger(strike="75.00", contracts=1):
    j, clock = ledger([dict(trade_id="P1", ticker="AAA", strike=strike, expiration=date(2026, 11, 2), contracts=contracts)])
    j.transition("P1", S.ASSIGNED_PENDING_RECONCILIATION)
    return j, clock


@pytest.mark.parametrize("session", [OPEN_SESSION, CLOSED, SessionInfo(SessionStatus.UNKNOWN)])
def test_sr03_pending_assignment_is_visible_in_monitor_in_every_session(session):
    j, _ = _pending_ledger()
    # no fetch is possible: a source that explodes proves visibility does not depend on market data
    res = run_monitor(journal=j, policy=POL, source=FakeSource({"AAA": Boom()}), now_utc=NOW, session=session)
    assert res.status != "NO_POSITIONS" and res.status == "ALERTS"
    assert len(res.messages) == 1 and res.messages[0].severity == "URGENT"
    # obligation: strike 75 x 100 shares = $7,500 (hand arithmetic)
    assert "7,500" in res.messages[0].text and "NO ORDER SENT" in res.messages[0].text


def test_sr03_exposure_is_counted_exactly_once_through_conversion():
    j, clock = _pending_ledger()
    b = j.load_book()
    # pending: put no longer reserves collateral, shares owed: 75 x 100 = 7500 (not 0)
    assert compute_exposure(b).aggregate_equity_risk_usd == D("7500")
    j.complete_assignment("P1", StockLot("L1", "AAA", D("100"), D("75"), clock.t, "sec_AAA", "bk_AAA", "ASSIGNMENT",
                                         cost_basis_reference_usd=D("7500")))
    # after conversion: 100 sh x 75 = 7500 once (not 15000, not 0)
    assert compute_exposure(j.load_book()).aggregate_equity_risk_usd == D("7500")


def test_sr03_pending_assignment_blocks_entries_until_resolved():
    j, clock = _pending_ledger()
    j.add_snapshot(AccountSnapshot("s9", clock.t, D("100000"), D("60000"), True, "v1"))
    codes_, _ = book_entry_blocks(j.load_book(), POL, clock.t)
    assert codes_, "an unresolved assignment must block entry alerts"


def test_sr03_reminder_dedup_is_once_per_exchange_day():
    j, _ = _pending_ledger()
    a = run_monitor(journal=j, policy=POL, source=None, now_utc=NOW, session=CLOSED).messages[0]
    b = run_monitor(journal=j, policy=POL, source=None, now_utc=NOW + timedelta(hours=1), session=CLOSED).messages[0]
    c = run_monitor(journal=j, policy=POL, source=None, now_utc=NOW + timedelta(days=1), session=CLOSED).messages[0]
    assert a.dedup_key == b.dedup_key and a.dedup_key != c.dedup_key


@pytest.mark.parametrize("state", [S.PENDING, S.WORKING])
def test_sr03_pending_working_orders_say_no_alerts_plus_a_note(state):
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", expiration=EXP, state=S.PENDING)])
    if state is S.WORKING:
        j.transition("P1", S.WORKING)
    res = run_monitor(journal=j, policy=POL, source=None, now_utc=NOW, session=CLOSED)
    assert res.status == "NO_ALERTS" and res.messages == [] and res.positions_checked == 0
    assert res.unresolved == 1 and any("P1" in n for n in res.notes)


def test_sr03_assignment_pending_mixed_with_open_position_both_reported():
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00", expiration=date(2026, 11, 2)),
                   dict(trade_id="P2", ticker="BBB", strike="50.00", expiration=EXP)])
    j.transition("P1", S.ASSIGNED_PENDING_RECONCILIATION)
    src = FakeSource({"BBB": chain_rows_for(50.0, EXP, 0.28, 0.30)})
    res = run_monitor(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN_SESSION)
    assert res.positions_checked == 1 and res.unresolved == 1 and res.status == "ALERTS"
    assert len(res.messages) == 1


# ============================================================================================== SR-05 queued dispatch
def _settings():
    return settings_from_mapping({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT, "CSP_DRY_RUN": "false"})


def _queue_then_close_one():
    """P1 (AAA) and P2 (BBB) both open with DTE 21 (TIME_EXIT) and P1 at 80% captured, P2 at 2x loss + delta -0.35.
    Alerts are queued while the transport is down; P1 is then CLOSED; later the transport recovers and the outbox flushes."""
    exp21 = TODAY + timedelta(days=21)
    j, clock = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00", expiration=exp21),
                       dict(trade_id="P2", ticker="BBB", strike="50.00", expiration=exp21)])
    j.pin_chat(chat_id_sha256(CHAT), "private")
    src = FakeSource({"AAA": chain_rows_for(75.0, exp21, 0.08, 0.10),                # (0.50-0.10)/0.50 = 80% captured
                      "BBB": chain_rows_for(50.0, exp21, 0.95, 1.00, delta=-0.35)})  # ask 1.00 = 2.0 x 0.50 ; |delta| .35 >= .30
    res = run_monitor(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN_SESSION)
    k = {(p.trade_id, s.trigger.value) for p, s in res.signals}
    assert {("P1", "PROFIT_EXIT"), ("P1", "TIME_EXIT"), ("P2", "TIME_EXIT"), ("P2", "LOSS_REVIEW"), ("P2", "DELTA_REVIEW")} <= k
    rep = runner.dispatch(j, res.messages, SEND, _settings(), POL, NOW, dead_port_transport(), lambda: [], lambda: NOW)
    assert rep.sent == 0
    rows = j.conn.execute("SELECT class, status FROM alert_outbox").fetchall()
    assert rows and all(r["status"] in ("RETRY", "PENDING", "FAILED_FINAL") for r in rows)
    # owner closes P1 at the broker and records it
    j.transition("P1", S.CLOSE_WORKING)
    j.transition("P1", S.CLOSED, actual_close_debit_per_share=D("0.10"), closed_at_utc=NOW, fees_usd=D("1.30"))
    return j, res


def test_sr05_queued_position_alerts_for_a_closed_position_are_dropped_but_open_position_alerts_are_sent():
    j, res = _queue_then_close_one()
    fake = FakeTelegram()
    try:
        later = NOW + timedelta(minutes=10)
        # sanity: nothing is left to flush if every row had gone FAILED_FINAL (then the test premise is void)
        assert j.conn.execute("SELECT COUNT(*) FROM alert_outbox WHERE status IN ('PENDING','RETRY')").fetchone()[0] > 0
        rv = runner.make_revalidator(j, POL, lambda: [], lambda: later)
        outbox.flush(j, fake.transport(), now=later + timedelta(hours=1), revalidator=rv)
        sent = "\n".join(r["body"]["text"] for r in fake.sends)
        assert "P2" in sent or "BBB" in sent
        assert "AAA" not in sent, "advice for the CLOSED position must not be delivered"
        # exactly P2's three alerts reached the (fake) chat: TIME_EXIT, LOSS_REVIEW, DELTA_REVIEW (safety alerts survive)
        assert len(fake.sends) == 3, [r["body"]["text"][:60] for r in fake.sends]
        dropped = j.conn.execute("SELECT class FROM alert_outbox WHERE status LIKE 'DROPPED%'").fetchall()
        assert {d["class"] for d in dropped} == {"PROFIT_EXIT", "TIME_EXIT"} and len(dropped) == 2
        assert any(e["event_type"] == "ALERT_DROPPED" for e in j.audit_events())
        assert TOKEN not in str(j.audit_events())
    finally:
        fake.close()


def test_sr05_profit_exit_quote_evidence_expires_after_two_monitor_intervals():
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00", expiration=EXP)])
    m = POL.market.exit_monitor_interval_minutes
    ev = {"trade_id": "P1", "trigger": "PROFIT_EXIT", "state": "OPEN", "evidence_at_utc": NOW.isoformat().replace("+00:00", "Z"),
          "fingerprint": runner.position_fingerprint(j.get_position("P1"))}
    ok = runner.make_revalidator(j, POL, lambda: [], lambda: NOW + timedelta(minutes=2 * m))
    late = runner.make_revalidator(j, POL, lambda: [], lambda: NOW + timedelta(minutes=2 * m, seconds=1))
    assert ok({"revalidate": ev}, "PROFIT_EXIT") is None          # exactly at the limit: still valid
    assert late({"revalidate": ev}, "PROFIT_EXIT")                # one second later: dropped
    assert late({"revalidate": ev}, "LOSS_REVIEW") is None        # safety alert about an open position still sent


def test_sr05_position_alert_without_any_position_reference_fails_closed_for_numeric_advice():
    j, _ = ledger()
    rv = runner.make_revalidator(j, POL, lambda: [], lambda: NOW)
    assert rv({"revalidate": {}}, "PROFIT_EXIT")
    assert rv({"revalidate": {"trade_id": "NOPE", "fingerprint": "x"}}, "LOSS_REVIEW")   # unknown position -> dropped


def test_sr05_fingerprint_change_drops_alert():
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00", expiration=EXP)])
    rv = runner.make_revalidator(j, POL, lambda: [], lambda: NOW)
    assert rv({"revalidate": {"trade_id": "P1", "state": "OPEN", "fingerprint": "stale",
                              "evidence_at_utc": NOW.isoformat()}}, "LOSS_REVIEW")


def test_sr05_tradeoff_calendar_alert_with_no_evidence_still_sends():
    """Implementer trade-off: a TIME_EXIT / ASSIGNMENT_REVIEW row with NO revalidate evidence is still sent.
    ACCEPTABLE only for legacy rows queued before evidence existed: new rows from dispatch() always carry trade_id,
    and a missed 21-DTE/assignment reminder is the costlier error. Numeric kinds with no evidence are dropped (above)."""
    j, _ = ledger()
    rv = runner.make_revalidator(j, POL, lambda: [], lambda: NOW)
    assert rv({}, "TIME_EXIT") is None and rv({}, "ASSIGNMENT_REVIEW") is None
    assert rv({}, "PROFIT_EXIT")


# ============================================================================================== SR-06 cash-flow ordering
def _j_frozen():
    box = {"t": NOW - timedelta(hours=6)}
    j = Journal(":memory:", clock=lambda: box["t"])
    j.migrate()
    j.confirm_baseline(D("100000"))
    return j, box


def _snap(j, box, sid, at, equity, cash="1000"):
    """Record a confirmed snapshot whose as-of time is `at`; the journal clock is `at` too (never in the future)."""
    box["t"] = max(box["t"], at)
    j.add_snapshot(AccountSnapshot(sid, at, D(equity), D(cash), True, "v1"))


def _flow(j, box, at, amount):
    box["t"] = max(box["t"], at)
    j.record_cash_flow(D(amount), at.date(), "platform history")


def test_sr06_withdrawal_recorded_before_same_second_snapshot_is_not_a_loss():
    j, box = _j_frozen()
    _snap(j, box, "s0", NOW - timedelta(hours=2), "100000")
    _flow(j, box, NOW, "-10000")
    _snap(j, box, "s1", NOW, "90000")                      # same second, recorded AFTER the flow
    _, dd = book_entry_blocks(j.load_book(), POL, NOW)
    # peak 100000 - 10000 = 90000; equity 90000 -> drawdown 0
    assert dd.level is DrawdownLevel.NONE and dd.drawdown == 0


def test_sr06_withdrawal_recorded_after_same_second_snapshot_is_a_real_drawdown_and_latches():
    j, box = _j_frozen()
    _snap(j, box, "s0", NOW - timedelta(hours=2), "100000")
    _snap(j, box, "s1", NOW, "90000")                      # snapshot first: 10% below peak
    _flow(j, box, NOW, "-10000")                           # flow recorded afterwards in the same second
    _, dd = book_entry_blocks(j.load_book(), POL, NOW)
    assert dd.level is DrawdownLevel.HARD_HALT             # 10% >= 8% and it must stay latched after the later flow


def test_sr06_cash_flows_cannot_clear_or_downgrade_a_latch():
    j, box = _j_frozen()
    _snap(j, box, "s0", NOW - timedelta(hours=5), "100000")
    _snap(j, box, "s1", NOW - timedelta(hours=4), "91000")                       # 9% -> HARD_HALT
    assert book_entry_blocks(j.load_book(), POL, NOW)[1].level is DrawdownLevel.HARD_HALT
    _flow(j, box, NOW - timedelta(hours=3), "20000")                             # deposit cannot hide the loss
    _snap(j, box, "s2", NOW - timedelta(hours=2), "120000")
    _flow(j, box, NOW - timedelta(hours=1), "-90000")                            # nor can a withdrawal reset it
    _snap(j, box, "s3", NOW - timedelta(minutes=30), "30000")
    dd = book_entry_blocks(j.load_book(), POL, NOW)[1]
    assert dd.level is DrawdownLevel.HARD_HALT and dd.entries_blocked
    # only an explicit owner risk reset clears it
    box["t"] = NOW
    j.record_risk_reset("owner reviewed")
    _snap(j, box, "s4", NOW + timedelta(seconds=1), "30000")
    assert book_entry_blocks(j.load_book(), POL, NOW + timedelta(seconds=2))[1].level is DrawdownLevel.NONE


@pytest.mark.parametrize("equity,level", [("97000", DrawdownLevel.PAUSE), ("96999.99", DrawdownLevel.PAUSE),
                                           ("97000.01", DrawdownLevel.NONE), ("95000", DrawdownLevel.REDUCE_REVIEW),
                                           ("92000", DrawdownLevel.HARD_HALT), ("92000.01", DrawdownLevel.REDUCE_REVIEW)])
def test_sr06_regression_drawdown_boundaries_unchanged(equity, level):
    # peak 100000 (baseline): 3% loss = 97000, 5% = 95000, 8% = 92000; thresholds are inclusive (>=)
    j, box = _j_frozen()
    _snap(j, box, "s0", NOW - timedelta(hours=1), equity)
    assert book_entry_blocks(j.load_book(), POL, NOW)[1].level is level


# ============================================================================================== SR-07 revision
def test_sr07_pending_snapshot_working_inside_one_second_invalidates_reconciliation_and_queued_entry():
    box = {"t": NOW - timedelta(hours=3)}
    j = Journal(":memory:", clock=lambda: box["t"])
    j.migrate()
    j.confirm_baseline(D("100000"))
    box["t"] = NOW
    j.create_draft(NewPosition("R1", "AAA", TODAY + timedelta(days=40), D("50"), 100, 1, "s", "b"))
    j.transition("R1", S.PENDING)
    j.add_snapshot(AccountSnapshot("s1", NOW, D("100000"), D("60000"), True, "v1"))
    m1 = runner._ledger_marker(j)
    j.transition("R1", S.WORKING)                      # same wall-clock second
    m2 = runner._ledger_marker(j)
    assert m1 != m2
    assert "CHANGE_AFTER_SNAPSHOT" in issue_codes(j.load_book(), NOW)
    # a new snapshot after the event clears it (not before)
    j.add_snapshot(AccountSnapshot("s2", NOW + timedelta(seconds=1), D("100000"), D("60000"), True, "v1"))
    assert "CHANGE_AFTER_SNAPSHOT" not in issue_codes(j.load_book(), NOW + timedelta(seconds=1))
    assert runner._ledger_marker(j) not in (m1, m2)


# ============================================================================================== SR-08 future marks
@pytest.mark.parametrize("delta_s,blocked", [(299, False), (300, False), (301, True), (3600 * 24 * 365, True)])
def test_sr08_future_stock_mark_five_minute_tolerance_boundary(delta_s, blocked):
    # tolerance 5 min = 300 s: a mark up to and including NOW+300s is allowed; NOW+301s is not
    b = book(lots=[lot(asof=NOW + timedelta(seconds=delta_s))])
    assert ("STOCK_MARK_FUTURE" in issue_codes(b)) is blocked
    assert (not run(b=b).passed) if blocked else True


@pytest.mark.parametrize("delta_s,blocked", [(299, False), (300, False), (301, True), (3600 * 24 * 365, True)])
def test_sr08_future_snapshot_five_minute_tolerance_boundary(delta_s, blocked):
    b = book(snaps=[snap(asof=NOW + timedelta(seconds=delta_s))])
    assert ("SNAPSHOT_FUTURE" in issue_codes(b)) is blocked
    if blocked:
        assert not run(b=b).passed


def test_sr08_input_rejects_future_snapshot():
    j = Journal(":memory:", clock=lambda: NOW)
    j.migrate()
    j.add_snapshot(AccountSnapshot("ok", NOW + timedelta(minutes=5), D("1000"), D("10"), True, "v1"))
    with pytest.raises(JournalError):
        j.add_snapshot(AccountSnapshot("bad", NOW + timedelta(minutes=5, seconds=1), D("1000"), D("10"), True, "v1"))


def test_sr08_input_rejects_future_stock_mark_DEFECT_CHECK():
    """OPEN_ISSUES SR-08: 'Reject future marks at input and reconciliation'. Reconciliation is covered above; this probes the
    Journal input path (add_stock_lot / mark_stock_lot)."""
    j = Journal(":memory:", clock=lambda: NOW)
    j.migrate()
    far = NOW + timedelta(days=365)
    with pytest.raises(JournalError):
        j.add_stock_lot(StockLot("L1", "AAA", D("100"), D("10"), far, "s", "b", "PURCHASE"))
    j.add_stock_lot(StockLot("L2", "AAA", D("100"), D("10"), NOW, "s", "b", "PURCHASE"))
    with pytest.raises(JournalError):
        j.mark_stock_lot("L2", D("11"), far)


# ============================================================================================== SR-09 paper only
def test_sr09_paper_false_rejected_everywhere():
    b = book(positions=[replace(put(), paper=False)])
    assert "REAL_POSITION_IN_PAPER_SCOPE" in issue_codes(b)
    assert not run(b=b).passed
    j = Journal(":memory:", clock=lambda: NOW)
    j.migrate()
    with pytest.raises(JournalError):
        j.create_draft(NewPosition("X", "AAA", EXP, D("50"), 100, 1, "s", "b", paper=False))


def test_sr09_csv_import_cannot_smuggle_a_real_position(tmp_path):
    j = Journal(":memory:", clock=lambda: NOW)
    j.migrate()
    row = dict.fromkeys(COLUMNS, "")
    row.update(trade_id="CSV1", account_scope="paper_primary", paper="false", ticker="AAA", expiration=EXP.isoformat(),
               strike="50.00", multiplier="100", contracts="1", sector="s", correlation_bucket="b", state="PENDING")
    p = tmp_path / "t.csv"
    p.write_text(",".join(COLUMNS) + "\n" + ",".join(row[c] for c in COLUMNS) + "\n", encoding="utf-8")
    try:
        import_positions_csv(j, p, dry_run=False)
    except Exception:  # noqa: BLE001  a refusal is acceptable
        pass
    # whatever happened, the book must not run clean with a paper=False record in it
    pos = [x for x in j.list_positions() if x.trade_id == "CSV1"]
    if pos:
        assert "REAL_POSITION_IN_PAPER_SCOPE" in issue_codes(j.load_book())


# ============================================================================================== SR-10 daily check-in
def _req(day_codes):
    return "DAILY_CHECKIN_MISSING" in day_codes


UTC = timezone.utc
# (label, now, snapshot asof, daily check-in expected missing)
# Required instant = XNYS close of the last session strictly before today's exchange date.
_CASES = [
    # Fri 2026-10-30 now. Previous session Thu 10-29 closes 16:00 EDT = 20:00Z.
    ("fri-thu-1s-before-close", datetime(2026, 10, 30, 15, 5, tzinfo=UTC), datetime(2026, 10, 29, 19, 59, 59, tzinfo=UTC), True),
    ("fri-thu-at-close", datetime(2026, 10, 30, 15, 5, tzinfo=UTC), datetime(2026, 10, 29, 20, 0, 0, tzinfo=UTC), False),
    ("fri-same-morning", datetime(2026, 10, 30, 15, 5, tzinfo=UTC), datetime(2026, 10, 30, 13, 0, tzinfo=UTC), False),
    # Sat 10-31: previous session is Fri 10-30 (close 20:00Z).
    ("sat-fri-before-close", datetime(2026, 10, 31, 15, 0, tzinfo=UTC), datetime(2026, 10, 30, 19, 59, 59, tzinfo=UTC), True),
    ("sat-fri-at-close", datetime(2026, 10, 31, 15, 0, tzinfo=UTC), datetime(2026, 10, 30, 20, 0, 0, tzinfo=UTC), False),
    # Mon 11-02 (EST, 10:05 local): previous session is Fri 10-30 (20:00Z). A Sunday snapshot satisfies both rules.
    ("mon-fri-morning", datetime(2026, 11, 2, 15, 5, tzinfo=UTC), datetime(2026, 10, 30, 14, 0, tzinfo=UTC), True),
    ("mon-sun", datetime(2026, 11, 2, 15, 5, tzinfo=UTC), datetime(2026, 11, 1, 15, 0, tzinfo=UTC), False),
    # Thanksgiving week 2026: Thu 11-26 closed. Fri 11-27 is an early close at 13:00 EST = 18:00Z.
    # Fri 11-27 now: previous session is Wed 11-25 (16:00 EST = 21:00Z).
    ("blackfri-wed-before-close", datetime(2026, 11, 27, 15, 5, tzinfo=UTC), datetime(2026, 11, 25, 20, 59, 59, tzinfo=UTC), True),
    ("blackfri-wed-at-close", datetime(2026, 11, 27, 15, 5, tzinfo=UTC), datetime(2026, 11, 25, 21, 0, 0, tzinfo=UTC), False),
    ("blackfri-holiday-thursday", datetime(2026, 11, 27, 15, 5, tzinfo=UTC), datetime(2026, 11, 26, 15, 0, tzinfo=UTC), False),
    # Mon 11-30: previous session is the EARLY-close Fri 11-27 (18:00Z), not 21:00Z.
    ("mon-after-earlyclose-1s-before", datetime(2026, 11, 30, 15, 5, tzinfo=UTC), datetime(2026, 11, 27, 17, 59, 59, tzinfo=UTC), True),
    ("mon-after-earlyclose-at", datetime(2026, 11, 30, 15, 5, tzinfo=UTC), datetime(2026, 11, 27, 18, 0, 0, tzinfo=UTC), False),
    # MLK Monday 2027-01-18 closed: Tue 01-19 previous session is Fri 01-15 (21:00Z); Monday holiday snapshot is fine.
    ("tue-after-mlk-fri-before", datetime(2027, 1, 19, 15, 5, tzinfo=UTC), datetime(2027, 1, 15, 20, 59, 59, tzinfo=UTC), True),
    ("tue-after-mlk-holiday-snapshot", datetime(2027, 1, 19, 15, 5, tzinfo=UTC), datetime(2027, 1, 18, 15, 0, tzinfo=UTC), False),
    # On the holiday itself (Mon 01-18) the previous session is still Fri 01-15.
    ("mlk-day-fri-before", datetime(2027, 1, 18, 15, 5, tzinfo=UTC), datetime(2027, 1, 15, 20, 59, 59, tzinfo=UTC), True),
    ("mlk-day-fri-at-close", datetime(2027, 1, 18, 15, 5, tzinfo=UTC), datetime(2027, 1, 15, 21, 0, 0, tzinfo=UTC), False),
]


@pytest.mark.parametrize("label,now,asof,missing", _CASES, ids=[c[0] for c in _CASES])
def test_sr10_daily_checkin_rule(label, now, asof, missing):
    cs = issue_codes(book(snaps=[snap(asof=asof)]), now)
    assert _req(cs) is missing, (label, cs)
    if not missing and label not in ("blackfri-wed-at-close", "mon-after-earlyclose-at", "mlk-day-fri-at-close"):
        # (the three excluded cases are also >30 h old, which is a different, spec-mandated block)
        assert cs == set(), (label, cs)


def test_sr10_weekend_gap_needs_a_weekend_checkin_because_of_the_30h_rule():
    """Observation (not a defect): a Friday-after-close snapshot is >30 h old on Monday morning, so SNAPSHOT_STALE (spec
    max_account_snapshot_age_hours: 30) blocks entries even though the daily rule is satisfied. The owner must check in Sunday."""
    now = datetime(2026, 11, 2, 15, 5, tzinfo=UTC)
    cs = issue_codes(book(snaps=[snap(asof=datetime(2026, 10, 30, 20, 30, tzinfo=UTC))]), now)
    assert "SNAPSHOT_STALE" in cs and "DAILY_CHECKIN_MISSING" not in cs


def test_sr10_check_in_gate_blocks_entries_through_the_entry_gate_and_clears_with_a_new_snapshot():
    stale = book(snaps=[snap(asof=NOW - timedelta(hours=24))])   # Thu 15:05Z = 11:05 ET, inside Thursday's session
    d = run(b=stale)
    assert not d.passed and "LEDGER_STALE" in codes(d)
    fresh = book(snaps=[snap(asof=NOW - timedelta(hours=24)), snap(asof=NOW - timedelta(minutes=5), sid="s2")])
    assert run(b=fresh).passed


# ============================================================================================== SR-11 CSP_ENV
@pytest.mark.parametrize("val", ["real", "REAL", " real ", "Real"])
def test_sr11_csp_env_real_is_rejected(val):
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_ENV": val})


@pytest.mark.parametrize("val", ["live", "prod", "brokerage", "paper2"])
def test_sr11_unknown_env_values_are_rejected(val):
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_ENV": val})


@pytest.mark.parametrize("env", [{}, {"CSP_ENV": "paper"}, {"CSP_ENV": "PAPER"}])
def test_sr11_paper_and_default_still_accepted(env):
    assert settings_from_mapping(env).env == "paper"


def test_sr11_broker_access_flag_cannot_be_enabled():
    for v in ("false", "0", "no"):
        with pytest.raises(StartupError):
            settings_from_mapping({"CSP_DISABLE_BROKER_ACCESS": v})


def test_sr11_doctor_fails_with_env_real(monkeypatch, tmp_path):
    code, out, err = run_cli(monkeypatch, tmp_path, ["doctor"], env={"CSP_ENV": "real"})
    assert code != 0, out + err
    assert "FAIL" in out + err or "paper" in (out + err).lower()


# ============================================================================================== advisory-only guarantee
def test_advisory_only_no_order_placement_or_broker_code_in_src():
    pat = re.compile(r"(place_?order|submit_?order|create_?order|cancel_?order|modify_?order|replace_?order|send_?order|"
                     r"order_?send|buy_?to_?close\(|sell_?to_?open\(|ib_insync|ibapi|alpaca|tradier|schwab_?api|"
                     r"robin_?hood|tda|etrade|webull|tastytrade|/v2/orders|/orders\b)", re.I)
    hits = []
    for f in (REPO / "src").rglob("*.py"):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if pat.search(line) and not s.startswith("#") and "can_send_orders: Literal[False]" not in s:
                hits.append((f.name, n, s[:100]))
    # docstrings/forbidden-import lists legitimately name brokers; boundary.py is the single allowed place
    hits = [h for h in hits if h[0] != "boundary.py"]
    assert hits == [], hits


def test_sr08_complete_assignment_future_mark_rejected_or_blocks_entries():
    """SR-08 via Journal.complete_assignment (inserts its own lot). Accept either: rejected at input (JournalError),
    or accepted but entry alerts blocked (reconcile issue / book_entry_blocks non-empty). Silent acceptance is a defect."""
    j, clock = _pending_ledger()
    j.add_snapshot(AccountSnapshot("s9", clock.t, D("100000"), D("60000"), True, "v1"))
    far = clock.t + timedelta(days=365)
    try:
        j.complete_assignment("P1", StockLot("L1", "AAA", D("100"), D("75"), far, "sec_AAA", "bk_AAA", "ASSIGNMENT",
                                             cost_basis_reference_usd=D("7500")))
    except JournalError:
        return  # rejected at input: preferred
    blocks, _ = book_entry_blocks(j.load_book(), POL, clock.t)
    assert blocks, "future-dated stock mark accepted via complete_assignment and entries NOT blocked"
