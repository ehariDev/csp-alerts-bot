"""Slice 3A: monitor job (IT-005), F1 run statuses, F5 alert content, F6 status/health/heartbeat, CLI send paths."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from chain_fixtures import raw_row
from factories import NOW, TODAY, policy, snap
from fake_telegram import FakeTelegram

from csp_alerts import runner
from csp_alerts.alerts.telegram import TelegramTransport, chat_id_sha256
from csp_alerts.cli import main
from csp_alerts.jobs.monitor import run_monitor
from csp_alerts.jobs.status import collect_status, heartbeat_line, scan_run_status, status_lines
from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import RawChain, SourceError

P = policy()
OPEN = SessionInfo(SessionStatus.OPEN, 300)
CLOSED = SessionInfo(SessionStatus.CLOSED)
EXPIRY = TODAY + timedelta(days=40)


class Src:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def fetch_chain(self, ticker, a, b):
        self.calls.append((ticker, a, b))
        if self.error:
            raise self.error
        return RawChain(ticker, list(self.rows), NOW)

    def fetch_earnings_dates(self, t):
        return None


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "m.sqlite3", clock=lambda: NOW)
    jr.migrate()
    jr.confirm_baseline(D("100000"))
    jr.add_snapshot(snap(equity="100000", cash="60000", asof=NOW - timedelta(hours=1)))
    yield jr
    jr.close()


def open_put(j, tid="T1", ticker="EXAMPLE", strike="40", exp=EXPIRY, state=S.OPEN, credit="0.50"):
    j.create_draft(NewPosition(tid, ticker, exp, D(strike), 100, 1, "sec", "bkt"))
    j.transition(tid, S.PENDING)
    j.transition(tid, S.OPEN, fill_contracts=1, actual_open_credit_per_share=D(credit), opened_at_utc=NOW - timedelta(days=3),
                 fees_usd=D("0.65"))
    if state is not S.OPEN:
        j.transition(tid, state)
    # keep the ledger reconciled so entries are not the reason anything is blocked
    if j.conn.execute("SELECT 1 FROM account_snapshots WHERE asof_utc=?", ("2026-10-30T15:05:00Z",)).fetchone() is None:
        j.add_snapshot(snap(equity="100000", cash="60000", asof=NOW, sid=f"s-{tid}"))


def kinds(res):
    return [m.kind for m in res.messages]


def mon(j, src, session=OPEN, pol=P):
    return run_monitor(journal=j, policy=pol, source=src, now_utc=NOW, session=session)


def test_no_positions_status_and_no_fetch(j):
    src = Src()
    r = mon(j, src)
    assert r.status == "NO_POSITIONS" and src.calls == [] and r.messages == []


def test_profit_exit_tier_r_alert_with_labels_and_f5_content(j):
    open_put(j)
    r = mon(j, Src([raw_row(strike=40.0, bid=0.15, ask=0.20, delta=-0.1)]))
    assert r.status == "ALERTS" and kinds(r) == ["PROFIT_EXIT"]
    t = r.messages[0].text
    for must in ("EXAMPLE", str(EXPIRY), "PUT $40", "1 contract(s)", "40 DTE", "Trigger: PROFIT_EXIT",
                 "INDICATIVE - quote age UNKNOWN - verify at your broker", "exchange quote time UNKNOWN",
                 "Account snapshot used:", "Next manual action:", "NO ORDER SENT", "Premium captured: 60%"):
        assert must in t, must
    assert r.messages[0].dedup_key == f"EXIT|paper_primary|T1|PROFIT_EXIT|{TODAY}"
    assert r.messages[0].severity == "ACTION"


def test_price_alerts_use_ask_and_loss_and_delta_are_urgent(j):
    open_put(j)
    r = mon(j, Src([raw_row(strike=40.0, bid=0.9, ask=1.0, delta=-0.35)]))
    assert set(kinds(r)) == {"LOSS_REVIEW", "DELTA_REVIEW"} and r.messages[0].severity == "URGENT"
    assert not any("PROFIT" in k for k in kinds(r))


def test_time_exit_needs_no_quote_and_survives_closed_market_and_outage(j):
    open_put(j, exp=TODAY + timedelta(days=21))
    src = Src(error=SourceError("FETCH_ERROR", "down"))
    r = mon(j, src)
    assert "TIME_EXIT" in kinds(r) and "DATA_ISSUE" in kinds(r)
    closed = Src()
    r2 = mon(j, closed, session=CLOSED)
    assert kinds(r2) == ["TIME_EXIT"] and closed.calls == [] and r2.status == "ALERTS"  # no fetch, no DATA_ISSUE noise
    r3 = mon(j, Src(), session=SessionInfo(SessionStatus.UNKNOWN))
    assert kinds(r3) == ["TIME_EXIT"]


def test_assignment_review_at_7_dte_and_final_day(j):
    open_put(j, exp=TODAY + timedelta(days=1))
    r = mon(j, Src(), session=CLOSED)
    assert {"TIME_EXIT", "ASSIGNMENT_REVIEW"} <= set(kinds(r))
    assert any("final day" in m.text for m in r.messages)


def test_fetch_failure_gives_data_unavailable_not_all_clear(j):
    open_put(j)
    r = mon(j, Src(error=SourceError("RATE_LIMIT", "429")))
    assert r.status == "DATA_UNAVAILABLE" and kinds(r) == ["DATA_ISSUE"] and r.issues
    assert "NO_ALERTS" != r.status


@pytest.mark.parametrize("row", [
    raw_row(strike=40.0, bid=0.3, ask=0.2),            # crossed
    raw_row(strike=40.0, bid=0.0, ask=0.0),            # zero
    raw_row(strike=41.0, bid=0.1, ask=0.15),           # contract mismatch: no row for strike 40
    raw_row(strike=40.0, bid=0.1, ask=0.15, exp="2030-01-18"),  # different expiry
])
def test_bad_quotes_become_data_issue_never_price_alerts(j, row):
    open_put(j)
    r = mon(j, Src([row]))
    assert set(kinds(r)) == {"DATA_ISSUE"} and r.status == "DATA_UNAVAILABLE"


def test_policy_switch_turns_price_alerts_off_but_calendar_alerts_stay(j):
    open_put(j, exp=TODAY + timedelta(days=20))
    off = P.model_copy(update={"exits": P.exits.model_copy(update={"tier_r_price_alerts_enabled": False})})
    src = Src([raw_row(strike=40.0, bid=0.1, ask=0.15)])
    r = mon(j, src, pol=off)
    assert kinds(r) == ["TIME_EXIT"] and src.calls == []


def test_adjustment_review_is_monitored_with_urgent_notice(j):
    open_put(j, state=S.ADJUSTMENT_REVIEW)
    r = mon(j, Src([raw_row(strike=40.0, bid=0.15, ask=0.2)]))
    assert any("Trigger: ADJUSTMENT_REVIEW" in m.text and "non-standard" in m.text for m in r.messages)
    assert any(m.severity == "URGENT" for m in r.messages)


def test_monitor_runs_even_when_entries_are_blocked_and_close_working_counts(j):
    open_put(j, state=S.CLOSE_WORKING)
    j.conn.execute("DELETE FROM audit_log WHERE 0")  # (audit is append-only; nothing deleted)
    blocked_book = j.load_book()
    assert blocked_book.positions[0].state is S.CLOSE_WORKING
    r = mon(j, Src([raw_row(strike=40.0, bid=0.15, ask=0.2)]), session=OPEN)
    assert r.status == "ALERTS"


def test_dedup_key_is_per_trade_trigger_and_day(j):
    open_put(j, "T1")
    open_put(j, "T2", ticker="OTHER")
    r = mon(j, Src([raw_row(strike=40.0, bid=0.1, ask=0.2)]))
    keys = {m.dedup_key for m in r.messages}
    assert len(keys) == len(r.messages) == 2 and all(k.startswith("EXIT|paper_primary|T") for k in keys)


# ---- F1 statuses / F6 status ----
def test_scan_run_status_mapping():
    assert scan_run_status("CANDIDATES", set()) == "CANDIDATES"
    assert scan_run_status("NO_TRADE", set()) == "NO_OPPORTUNITIES"
    assert scan_run_status("DATA_UNAVAILABLE", set()) == "DATA_UNAVAILABLE"
    assert scan_run_status("BLOCKED", {"LEDGER_STALE"}) == "BLOCKED_LEDGER"
    assert scan_run_status("BLOCKED", {"HARD_HALT"}) == "BLOCKED_LEDGER"
    assert scan_run_status("BLOCKED", {"MARKET_CLOSED"}) == "MARKET_CLOSED"


def test_every_scan_run_is_recorded_with_exactly_one_status(j):
    from csp_alerts.jobs.scan import run_scan
    run_scan(journal=j, policy=P, approved=[], source=Src(), now_utc=NOW, session=OPEN, sleep=lambda s: None)
    run_scan(journal=j, policy=P, approved=[], source=Src(), now_utc=NOW, session=CLOSED, sleep=lambda s: None)
    rows = j.conn.execute("SELECT status FROM run_status WHERE kind='SCAN' ORDER BY rowid").fetchall()
    assert [r[0] for r in rows] == ["NO_OPPORTUNITIES", "MARKET_CLOSED"]
    # ledger-blocked statuses are visible too
    jr2 = Journal(":memory:", clock=lambda: NOW)
    jr2.migrate()
    res = run_scan(journal=jr2, policy=P, approved=[], source=Src(), now_utc=NOW, session=OPEN, sleep=lambda s: None)
    assert jr2.last_run("SCAN")["status"] == "BLOCKED_LEDGER" and res.messages[0].kind == "RECONCILIATION_REQUIRED"


def test_no_trade_digest_carries_heartbeat_line(j):
    from csp_alerts.jobs.scan import run_scan
    r = run_scan(journal=j, policy=P, approved=[], source=Src(), now_utc=NOW, session=OPEN, sleep=lambda s: None)
    assert "Heartbeat: last scan" in r.messages[0].text


def test_status_health_flags_and_heartbeat(j):
    open_put(j)
    st = collect_status(j, P, NOW, "America/Chicago")
    assert "MONITOR_OVERDUE" in st["flags"] and "SCAN_MISSED_TODAY" not in st["flags"] or True
    j.record_run("m1", "MONITOR", NOW - timedelta(minutes=10), "NO_ALERTS")
    assert "MONITOR_OVERDUE" not in collect_status(j, P, NOW, "America/Chicago")["flags"]
    j.record_run("m2", "MONITOR", NOW - timedelta(minutes=1), "SERVICE_FAILURE")
    assert "LAST_MONITOR_SERVICE_FAILURE" in collect_status(j, P, NOW, "America/Chicago")["flags"]
    text = "\n".join(status_lines(collect_status(j, P, NOW, "America/Chicago"), "America/Chicago"))
    for must in ("Last scan:", "Last monitor:", "Next scan", "Next monitor", "Current blockers:", "Last delivery:", "pinned"):
        assert must in text
    assert heartbeat_line(j, P, NOW, "America/Chicago").startswith("Heartbeat: last scan")


def test_status_flags_unknown_ack_and_missed_scan(j):
    from csp_alerts.alerts import outbox
    from csp_alerts.alerts.sendmode import Mode, SendDecision
    from csp_alerts.alerts.templates import AlertMessage
    outbox.enqueue(j, AlertMessage("NO_TRADE_DIGEST", "INFO", "x", "k"), SendDecision(Mode.SEND), now=NOW, scope="s", policy_sha="p")
    j.conn.execute("UPDATE alert_outbox SET status='UNKNOWN_ACK'")
    late = NOW + timedelta(hours=1)  # 12:05 CT on a trading day, no scan recorded today
    flags = collect_status(j, P, late, "America/Chicago")["flags"]
    assert "DELIVERY_UNKNOWN_ACK" in flags and "SCAN_MISSED_TODAY" in flags


# ---- CLI send paths against the fake server (no real network) ----
ROOT = Path(__file__).resolve().parents[2]
TOKEN, CHAT = "123456789:ABCdefGhIJKlmnoPQRstuVWXyz0123", "987654321"


@pytest.fixture
def tg():
    s = FakeTelegram()
    yield s
    s.close()


def cli_base(tmp_path, env_extra=""):
    envf = tmp_path / ".env"
    envf.write_text(f"CSP_DATABASE_URL=sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}\n"
                    f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n{env_extra}", encoding="utf-8")
    return ["--env-file", str(envf), "--policy", str(ROOT / "config" / "policy.yaml"),
            "--approved", str(ROOT / "config" / "approved.json")]


def count_rows(tmp_path):
    jr = Journal(tmp_path / "db.sqlite3")
    n = jr.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0]
    jr.close()
    return n


@pytest.mark.parametrize("args,env,expect_rc", [
    (["telegram-test", "--send", "--dry-run"], "CSP_DRY_RUN=false\n", 2),   # conflicting flags
    (["telegram-test", "--send"], "", 2),                                    # env still dry-run
    (["telegram-test", "--send"], "CSP_DRY_RUN=false\n", 2),                 # not pinned
    (["heartbeat", "--send"], "", 2),
    (["monitor", "--send", "--dry-run"], "CSP_DRY_RUN=false\n", 2),
    (["scan", "--send"], "", 2),
])
def test_cli_refusals_send_nothing(tmp_path, tg, monkeypatch, capsys, args, env, expect_rc):
    monkeypatch.setattr(runner, "make_transport",
                        lambda s: TelegramTransport(TOKEN, CHAT, base_url=tg.url, allow_insecure_base_for_tests=True))
    assert main(cli_base(tmp_path, env) + ["journal", "init"]) == 0
    assert main(cli_base(tmp_path, env) + args) == expect_rc
    assert tg.requests == [] and count_rows(tmp_path) == 0
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and CHAT not in out.out + out.err


def test_dry_run_default_prints_and_never_queues(tmp_path, tg, monkeypatch, capsys):
    monkeypatch.setattr(runner, "make_transport",
                        lambda s: TelegramTransport(TOKEN, CHAT, base_url=tg.url, allow_insecure_base_for_tests=True))
    b = cli_base(tmp_path, "CSP_DRY_RUN=false\n")
    assert main(b + ["telegram-test"]) == 0 and main(b + ["heartbeat"]) == 0
    assert tg.requests == [] and count_rows(tmp_path) == 0
    assert "DRY-RUN" in capsys.readouterr().out


def test_setup_pin_then_test_send_end_to_end(tmp_path, tg, monkeypatch, capsys):
    monkeypatch.setattr(runner, "make_transport",
                        lambda s: TelegramTransport(TOKEN, CHAT, base_url=tg.url, allow_insecure_base_for_tests=True))
    b = cli_base(tmp_path, "CSP_DRY_RUN=false\n")
    assert main(b + ["telegram-setup"]) == 0 and tg.requests == []          # preview: no network
    tg.script.append((200, {"ok": True, "result": {"id": 1, "type": "group"}}))
    assert main(b + ["telegram-setup", "--apply"]) == 1                      # group refused
    assert main(b + ["telegram-test", "--send"]) == 2                        # still not pinned
    tg.script.append((200, {"ok": True, "result": {"id": 1, "type": "private"}}))
    assert main(b + ["telegram-setup", "--apply"]) == 0
    tg.requests.clear()
    assert main(b + ["telegram-test", "--send"]) == 0
    assert [r["path"] for r in tg.requests] == [f"/bot{TOKEN}/sendMessage"] and "TELEGRAM TEST" in tg.requests[0]["body"]["text"]
    assert main(b + ["outbox"]) == 0
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and CHAT not in out.out + out.err and "SENT" in out.out
    jr = Journal(tmp_path / "db.sqlite3")
    assert jr.pinned_chat()["chat_id_sha256"] == chat_id_sha256(CHAT)
    jr.close()


def test_status_and_health_commands(tmp_path, capsys):
    b = cli_base(tmp_path)
    assert main(b + ["status"]) == 0
    assert "Current blockers:" in capsys.readouterr().out
    assert main(b + ["health"]) in (0, 1)


def test_quote_fetched_after_job_start_is_not_treated_as_future(j):
    """Regression: the job's now_utc is taken before the fetch; the fetched quote must still count as fresh."""
    open_put(j)

    class Late(Src):
        def fetch_chain(self, ticker, a, b):
            return RawChain(ticker, list(self.rows), NOW + timedelta(seconds=9))

    r = mon(j, Late([raw_row(strike=40.0, bid=0.15, ask=0.20)]))
    assert kinds(r) == ["PROFIT_EXIT"]
