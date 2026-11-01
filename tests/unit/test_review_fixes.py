"""Regression tests for the 2026-10-30 retest defects SR-01..SR-11 (synthetic data only, no network)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import NOW, TODAY, book, lot, policy, put, snap
from fake_telegram import FakeTelegram

from csp_alerts import runner
from csp_alerts.alerts import outbox, templates
from csp_alerts.alerts.sendmode import Mode, SendDecision
from csp_alerts.alerts.telegram import TelegramTransport, chat_id_sha256
from csp_alerts.jobs.monitor import _quote_for, run_monitor
from csp_alerts.jobs.status import collect_status, status_lines
from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.normalize import normalize_rows
from csp_alerts.policy.drawdown import DrawdownLevel, compute_drawdown
from csp_alerts.policy.exits import OptionQuote, Trigger, evaluate_exits
from csp_alerts.policy.portfolio import compute_exposure
from csp_alerts.settings import StartupError, settings_from_mapping

P = policy()
OPEN = SessionInfo(SessionStatus.OPEN, 300)
SEND = SendDecision(Mode.SEND)
TOKEN = "123456789:ABCdefGhIJKlmnoPQRstuVWXyz0123"
CHAT = "987654321"


def codes(book_, now=NOW):
    return [i.code for i in reconcile(book_, P, now).issues]


@pytest.fixture
def j():
    jr = Journal(":memory:", clock=lambda: NOW)
    jr.migrate()
    yield jr
    jr.close()


@pytest.fixture
def tg():
    s = FakeTelegram()
    yield s
    s.close()


def _open_position(j, trade_id="T1", ticker="AAA", strike="50"):
    j.create_draft(NewPosition(trade_id, ticker, TODAY + timedelta(days=40), D(strike), 100, 1, "s", "b"))
    j.transition(trade_id, S.PENDING)
    j.transition(trade_id, S.WORKING)
    j.transition(trade_id, S.OPEN, fill_contracts=1, opened_at_utc=NOW, actual_open_credit_per_share=D("0.50"), fees_usd=D("1"))


# ---- SR-01 / SR-02 ----
@pytest.mark.parametrize("bid", [D("0"), None, D("-0.1"), D("NaN")])
def test_sr01_zero_or_invalid_bid_gives_data_issue_not_profit_exit(bid):
    pos = put(strike="75")
    q = OptionQuote(pos.contract_key, NOW, bid=bid, ask=D("0.20"), delta=D("-0.13"), spot=D("80"))
    kinds = [s.trigger for s in evaluate_exits(pos, q, P, NOW)]
    assert Trigger.PROFIT_EXIT not in kinds and Trigger.DATA_ISSUE in kinds


def test_sr01_valid_two_sided_quote_still_profit_exit():
    pos = put(strike="75")
    q = OptionQuote(pos.contract_key, NOW, bid=D("0.18"), ask=D("0.20"), delta=D("-0.13"), spot=D("80"))
    assert Trigger.PROFIT_EXIT in [s.trigger for s in evaluate_exits(pos, q, P, NOW)]


def _row(**over):
    base = dict(type="put", strike="75", expiration=(TODAY + timedelta(days=40)).isoformat(), spot="80", bid="0.18",
                ask="0.20", delta="-0.13", last_trade_ms=int(NOW.timestamp() * 1000), volume=100, open_interest=1000)
    base.update(over)
    return base


def _quote(*raws):
    n = normalize_rows("AAA", list(raws), NOW, today_exchange=TODAY, earnings_dates=None, multiplier=None)
    return _quote_for(put(strike="75"), n.rows, NOW)


def test_sr02_contradictory_symbol_right_gives_identity_problem():
    q = _quote(_row(contractSymbol="AAA261209C00075000"))
    assert q.identity_problem and q.ask is None
    kinds = [s.trigger for s in evaluate_exits(put(strike="75"), q, P, NOW)]
    assert kinds.count(Trigger.DATA_ISSUE) == 1 and Trigger.PROFIT_EXIT not in kinds


def test_sr02_wrong_root_and_duplicates_are_identity_problems_matching_symbol_is_fine():
    exp = TODAY + timedelta(days=40)
    good = f"AAA{exp:%y%m%d}P00075000"
    assert _quote(_row(contractSymbol=good)).identity_problem is None
    assert _quote(_row(contractSymbol=good.replace("AAA", "BBB"))).identity_problem
    assert _quote(_row(), _row()).identity_problem


# ---- SR-03 ----
def test_sr03_assignment_pending_is_exposure_and_alerts():
    pend = put(state=S.ASSIGNED_PENDING_RECONCILIATION, strike="75")
    b = book(positions=[pend])
    assert compute_exposure(b).aggregate_equity_risk_usd == D("7500")

    class J:
        def load_book(self):
            return b

    res = run_monitor(journal=J(), policy=P, source=None, now_utc=NOW, session=OPEN)
    assert res.status == "ALERTS" and res.unresolved == 1 and res.positions_checked == 0
    assert len(res.messages) == 1 and "PENDING RECONCILIATION" in res.messages[0].text
    assert "$7,500" in res.messages[0].text and res.messages[0].meta["trade_id"] == "T1"


def test_sr03_linked_lot_is_not_double_counted_with_pending_obligation():
    pend = put(state=S.ASSIGNED_PENDING_RECONCILIATION, strike="80")
    l = lot(lot_id="L1", ticker="AAA", shares="100", price="80", linked_trade_id="T1")
    assert compute_exposure(book(positions=[pend], lots=[l])).aggregate_equity_risk_usd == D("8000")


def test_sr03_working_order_is_not_no_positions():
    class J:
        def load_book(self):
            return book(positions=[put(state=S.WORKING)])

    res = run_monitor(journal=J(), policy=P, source=None, now_utc=NOW, session=OPEN)
    assert res.status == "NO_ALERTS" and res.unresolved == 1 and res.positions_checked == 0 and res.notes


def test_sr03_status_shows_exposure_and_flag(j):
    j.confirm_baseline(D("100000"))
    j.create_draft(NewPosition("A1", "AAA", TODAY + timedelta(days=10), D("75"), 100, 1, "s", "b"))
    for st, kw in ((S.PENDING, {}), (S.WORKING, {}),
                   (S.OPEN, dict(fill_contracts=1, opened_at_utc=NOW, actual_open_credit_per_share=D("1"))),
                   (S.ASSIGNED_PENDING_RECONCILIATION, {})):
        j.transition("A1", st, **kw)
    st = collect_status(j, P, NOW, "America/Chicago")
    assert "ASSIGNMENT_PENDING_RECONCILIATION" in st["flags"]
    text = "\n".join(status_lines(st, "America/Chicago"))
    assert "assignment pending $7,500.00" in text and "A1 AAA ASSIGNED_PENDING_RECONCILIATION" in text


# ---- SR-06 / SR-07 ----
def _dd(j):
    b = j.load_book()
    return compute_drawdown(b.snapshots, P, baseline_equity_usd=b.baseline_equity_usd, cash_flows=b.cash_flows)


def test_sr06_withdrawal_recorded_before_same_second_snapshot_is_not_a_loss(j):
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(asof=NOW - timedelta(hours=1), sid="s0"))
    j.record_cash_flow(D("-10000"), TODAY, "withdrawal")
    j.add_snapshot(snap(equity="90000", asof=NOW, sid="s1"))
    dd = _dd(j)
    assert dd.level is DrawdownLevel.NONE and dd.peak_equity_usd == D("90000")


def test_sr06_snapshot_recorded_before_same_second_flow_keeps_loss_and_flow_never_clears_latch(j):
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(equity="90000", asof=NOW, sid="s1"))  # a real 10% loss
    j.record_cash_flow(D("-10000"), TODAY, "withdrawal afterwards")
    dd = _dd(j)
    assert dd.level is DrawdownLevel.HARD_HALT and dd.halt_latched


def test_sr06_two_tuple_flows_tie_applies_flow_first():
    s = [snap(asof=NOW - timedelta(hours=1), sid="a"), snap(equity="90000", asof=NOW, sid="b")]
    dd = compute_drawdown(s, P, baseline_equity_usd=D("100000"), cash_flows=[(NOW, D("-10000"))])
    assert dd.level is DrawdownLevel.NONE


def test_sr07_same_second_ledger_event_invalidates_snapshot_and_changes_marker(j):
    j.confirm_baseline(D("100000"))
    j.create_draft(NewPosition("R1", "AAA", TODAY + timedelta(days=40), D("50"), 100, 1, "s", "b"))
    j.transition("R1", S.PENDING)
    j.add_snapshot(snap(asof=NOW))
    assert "CHANGE_AFTER_SNAPSHOT" not in codes(j.load_book())
    before = runner._ledger_marker(j)
    j.transition("R1", S.WORKING)
    assert runner._ledger_marker(j) != before
    assert "CHANGE_AFTER_SNAPSHOT" in codes(j.load_book())


def test_sr07_cash_flow_after_snapshot_same_second_requires_new_snapshot(j):
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(asof=NOW))
    j.record_cash_flow(D("500"), TODAY, "deposit")
    assert "CHANGE_AFTER_SNAPSHOT" in codes(j.load_book())


# ---- SR-05 ----
def _signals_for(j, ask="0.10"):
    pos = j.get_position("T1")
    q = OptionQuote(pos.contract_key, NOW, bid=D("0.08"), ask=D(ask), delta=D("-0.35"), spot=D("40"))
    return [templates.exit_alert(s, pos, q, P, NOW, None, P.account_scope) for s in evaluate_exits(pos, q, P, NOW)]


def _queue_and_flush(j, tg, msgs, flush_at):
    tr = TelegramTransport(TOKEN, CHAT, base_url=tg.url, timeout=5.0, allow_insecure_base_for_tests=True)
    for m in msgs:
        meta = m.meta or {}
        rv = {"trade_id": meta["trade_id"], "trigger": meta.get("trigger"), "state": meta.get("state"),
              "fingerprint": meta.get("fingerprint"), "evidence_at_utc": "2026-10-30T15:05:00Z"}
        outbox.enqueue(j, m, SEND, now=NOW, scope=P.account_scope, policy_sha=P.sha256, revalidate=rv)
    rev = runner.make_revalidator(j, P, lambda: [], lambda: flush_at)
    return outbox.flush(j, tr, now=flush_at, revalidator=rev)


def test_sr05_open_position_alerts_still_send(j, tg):
    _open_position(j)
    msgs = _signals_for(j)
    assert {"PROFIT_EXIT", "DELTA_REVIEW"} <= {m.kind for m in msgs}
    rep = _queue_and_flush(j, tg, msgs, NOW + timedelta(minutes=5))
    assert rep.sent == len(msgs) and rep.dropped == 0


@pytest.mark.parametrize("final", [S.CLOSED, S.EXPIRED])
def test_sr05_closed_position_alerts_are_dropped_and_audited(j, tg, final):
    _open_position(j)
    msgs = _signals_for(j)
    if final is S.CLOSED:
        j.transition("T1", S.CLOSE_WORKING)
        j.transition("T1", S.CLOSED, closed_at_utc=NOW, actual_close_debit_per_share=D("0.10"), fees_usd=D("2"))
    else:
        j.transition("T1", S.EXPIRED, closed_at_utc=NOW + timedelta(days=41), confirmed=True)
    rep = _queue_and_flush(j, tg, msgs, NOW + timedelta(minutes=15))
    assert rep.sent == 0 and rep.dropped == len(msgs)
    assert j.audit_events("ALERT_DROPPED")


def test_sr05_canceled_position_and_missing_reference_dropped(j):
    j.create_draft(NewPosition("C1", "AAA", TODAY + timedelta(days=40), D("50"), 100, 1, "s", "b"))
    j.transition("C1", S.CANCELED)
    chk = runner.make_revalidator(j, P, lambda: [], lambda: NOW)
    assert chk({"revalidate": {"trade_id": "C1", "state": "OPEN", "fingerprint": "x"}}, "LOSS_REVIEW")
    assert chk({"revalidate": {"trade_id": "NOPE"}}, "TIME_EXIT")
    assert chk({}, "PROFIT_EXIT") and chk({"revalidate": {"state": "OPEN"}}, "TIME_EXIT")
    assert chk({}, "TIME_EXIT") is None  # reference-less calendar reminder (legacy row) still sends


def test_sr05_changed_terms_and_old_profit_quote_dropped_safety_alert_kept(j):
    _open_position(j)
    rv = {"trade_id": "T1", "state": "OPEN", "fingerprint": runner.position_fingerprint(j.get_position("T1")),
          "evidence_at_utc": "2026-10-30T15:05:00Z"}
    chk = runner.make_revalidator(j, P, lambda: [], lambda: NOW + timedelta(minutes=5))
    assert chk({"revalidate": rv}, "PROFIT_EXIT") is None
    assert chk({"revalidate": rv}, "LOSS_REVIEW") is None
    assert chk({"revalidate": {**rv, "fingerprint": "x"}}, "LOSS_REVIEW")
    late = runner.make_revalidator(j, P, lambda: [], lambda: NOW + timedelta(minutes=45))
    assert late({"revalidate": rv}, "PROFIT_EXIT")
    assert late({"revalidate": rv}, "LOSS_REVIEW") is None  # safety alert for a still-open position keeps sending


def test_sr05_assignment_pending_alert_dropped_once_reconciled(j):
    _open_position(j)
    j.transition("T1", S.ASSIGNED_PENDING_RECONCILIATION)
    m = templates.unresolved_assignment_alert(j.get_position("T1"), NOW, P.account_scope)
    rv = {"trade_id": "T1", "state": m.meta["state"], "fingerprint": m.meta["fingerprint"]}
    chk = runner.make_revalidator(j, P, lambda: [], lambda: NOW)
    assert chk({"revalidate": rv}, "ASSIGNMENT_REVIEW") is None
    j.complete_assignment("T1", lot(lot_id="L9", ticker="AAA", shares="100", price="50",
                                    cost_basis_reference_usd=D("5000")))
    assert chk({"revalidate": rv}, "ASSIGNMENT_REVIEW")


def test_sr05_dispatch_attaches_position_evidence(j, tg):
    _open_position(j)
    msgs = _signals_for(j)
    st = settings_from_mapping({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT, "CSP_DRY_RUN": "false"})
    tr = TelegramTransport(TOKEN, CHAT, base_url=tg.url, timeout=5.0, allow_insecure_base_for_tests=True)
    j.pin_chat(chat_id_sha256(CHAT), "private")
    rep = runner.dispatch(j, msgs, SEND, st, P, NOW, tr, lambda: [], lambda: NOW + timedelta(minutes=1))
    assert rep.sent == len(msgs)
    row = j.conn.execute("SELECT evidence_json FROM alert_outbox WHERE class='PROFIT_EXIT'").fetchone()
    assert json.loads(row[0])["revalidate"]["trade_id"] == "T1"


# ---- SR-08 / SR-09 / SR-10 / SR-11 ----
def test_sr08_future_stock_mark_blocks():
    assert "STOCK_MARK_FUTURE" in codes(book(lots=[lot(asof=NOW + timedelta(days=365))]))
    assert "STOCK_MARK_FUTURE" in codes(book(lots=[lot(asof=NOW + timedelta(minutes=6))]))
    assert "STOCK_MARK_FUTURE" not in codes(book(lots=[lot(asof=NOW + timedelta(minutes=4))]))


def test_sr08_future_snapshot_blocks():
    assert "SNAPSHOT_FUTURE" in codes(book(snaps=[snap(asof=NOW + timedelta(hours=1))]))


def test_sr09_real_position_rejected_in_paper_scope(j):
    assert "REAL_POSITION_IN_PAPER_SCOPE" in codes(book(positions=[replace(put(), paper=False)]))
    assert "REAL_POSITION_IN_PAPER_SCOPE" not in codes(book(positions=[put()]))
    with pytest.raises(JournalError, match="paper"):
        j.create_draft(NewPosition("X", "AAA", TODAY + timedelta(days=40), D("50"), 100, 1, "s", "b", paper=False))


def test_sr10_prior_session_snapshot_blocks_entries_with_clear_reason():
    # NOW = Fri 11:05 ET; the Thursday close (16:00 ET) was 19 hours earlier
    rep = reconcile(book(snaps=[snap(asof=NOW - timedelta(hours=24))]), P, NOW)
    issue = next(i for i in rep.issues if i.code == "DAILY_CHECKIN_MISSING")
    assert issue.severity == "BLOCK" and issue.reason_code == "LEDGER_STALE" and "daily-check-in" in issue.message
    assert "DAILY_CHECKIN_MISSING" not in codes(book(snaps=[snap(asof=NOW - timedelta(hours=18))]))
    assert "DAILY_CHECKIN_MISSING" not in codes(book(snaps=[snap(asof=NOW - timedelta(hours=19))]))


def test_sr10_monday_requires_snapshot_after_friday_close():
    mon = NOW + timedelta(days=3)  # Mon 2026-11-02 11:05 ET
    assert "DAILY_CHECKIN_MISSING" in codes(book(snaps=[snap(asof=NOW)]), mon)  # Friday-morning snapshot
    assert "DAILY_CHECKIN_MISSING" not in codes(book(snaps=[snap(asof=NOW + timedelta(hours=6))]), mon)  # Fri 17:05 ET


def test_sr11_real_env_rejected_with_clear_message():
    with pytest.raises(StartupError, match="paper-only"):
        settings_from_mapping({"CSP_ENV": "real"})
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_ENV": " REAL "})
    assert settings_from_mapping({"CSP_ENV": "paper"}).env == "paper"
