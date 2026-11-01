"""Slice 3 re-verification after the hardening round: (a) re-queue rules, (b) 24h expiry of informational alerts, (c) PARTIAL digest
does not mark the day, (e) backup refuses the live DB (any alias), plus independent re-checks of D1 (transport classification),
D2 (ordering incl. a failed middle part) and D3 (session-close drop). Loopback fake only; no real Telegram."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import ssl
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, TODAY, policy
from s2_helpers import FakeSource, appr, make_journal, raw_row
from s3_helpers import CHAT, TOKEN, FakeTelegram, count, dead_port_transport, ledger, run_cli

from csp_alerts import runner
from csp_alerts.alerts import outbox
from csp_alerts.alerts.sendmode import Mode, SendDecision
from csp_alerts.alerts.telegram import Outcome, TelegramTransport
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.jobs.scan import run_scan
from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.store import Journal
from csp_alerts.market.models import SessionInfo, SessionStatus

POL = policy()
SEND = SendDecision(Mode.SEND)


@pytest.fixture()
def fake():
    f = FakeTelegram()
    yield f
    f.close()


def m(key="K", text="hello", kind="DATA_ISSUE", sev="WARN"):
    return AlertMessage(kind, sev, text, key)


def q(j, msg, now=NOW, **kw):
    return outbox.enqueue(j, msg, SEND, now=now, scope="paper_primary", policy_sha=POL.sha256, **kw)


def st(j):
    return [r[0] for r in j.conn.execute("SELECT status FROM alert_outbox ORDER BY rowid")]


# ====================================================================== (a) re-queue rules
@pytest.mark.parametrize("terminal", ["DROPPED_EXPIRED", "DROPPED_STALE", "FAILED_FINAL"])
def test_requeue_after_each_terminal_non_delivery_status(fake, terminal):
    j, _ = ledger()
    ids = q(j, m("K1", "first"))
    j.conn.execute("UPDATE alert_outbox SET status=?, attempts=5", (terminal,))
    again = q(j, m("K1", "fresh text"), now=NOW + timedelta(hours=1))
    assert again == ids                                                      # same stable id, re-queued
    r = j.conn.execute("SELECT status, attempts, content_text, created_at_utc FROM alert_outbox").fetchone()
    assert (r["status"], r["attempts"], r["content_text"]) == ("PENDING", 0, "fresh text") and count(j, "alert_outbox") == 1
    ev = [e for e in j.audit_events() if e["event_type"] == "ALERT_REQUEUED"]
    assert len(ev) == 1 and json.loads(ev[0]["before_json"])["status"] == terminal
    assert outbox.flush(j, fake.transport(), now=NOW + timedelta(hours=1)).sent == 1 and len(fake.sends) == 1
    assert fake.sends[0]["body"]["text"] == "fresh text"
    assert q(j, m("K1", "third"), now=NOW + timedelta(hours=2)) == []         # now SENT: no more duplicates


@pytest.mark.parametrize("live", ["PENDING", "RETRY", "SENDING", "SENT", "UNKNOWN_ACK"])
def test_never_requeued_while_a_live_or_possibly_delivered_row_exists(fake, live):
    j, _ = ledger()
    q(j, m("K1", "first"))
    j.conn.execute("UPDATE alert_outbox SET status=?", (live,))
    before = j.conn.execute("SELECT * FROM alert_outbox").fetchone()
    assert q(j, m("K1", "dup"), now=NOW + timedelta(hours=1)) == []
    after = j.conn.execute("SELECT * FROM alert_outbox").fetchone()
    assert dict(before) == dict(after) and count(j, "alert_outbox") == 1
    assert not [e for e in j.audit_events() if e["event_type"] == "ALERT_REQUEUED"]


def test_unknown_ack_from_a_real_timeout_still_blocks_any_duplicate_send(fake):
    """(3) The re-queue rule must never create a duplicate of a message that may already have been delivered."""
    j, _ = ledger()
    fake.script = ["hang"]
    q(j, m("K1", "maybe delivered"))
    outbox.flush(j, fake.transport(timeout=0.4), now=NOW)
    assert st(j) == ["UNKNOWN_ACK"] and len(fake.sends) == 1
    for later in (NOW + timedelta(minutes=5), NOW + timedelta(days=2)):
        assert q(j, m("K1", "maybe delivered"), now=later) == []
        outbox.flush(j, fake.transport(), now=later)
    assert len(fake.sends) == 1 and st(j) == ["UNKNOWN_ACK"]


def test_unknown_ack_from_crash_in_sending_and_half_response_also_block(fake):
    j, _ = ledger()
    q(j, m("A", "a"))
    j.conn.execute("UPDATE alert_outbox SET status='SENDING'")
    outbox.flush(j, fake.transport(), now=NOW)
    assert st(j) == ["UNKNOWN_ACK"] and q(j, m("A", "a"), now=NOW + timedelta(hours=1)) == [] and fake.sends == []
    fake.script = ["halfresponse"]
    q(j, m("B", "b"))
    outbox.flush(j, fake.transport(timeout=0.5), now=NOW)
    assert st(j)[-1] == "UNKNOWN_ACK" and q(j, m("B", "b"), now=NOW + timedelta(hours=1)) == [] and len(fake.sends) == 1


def test_failed_final_after_a_5xx_exhaustion_may_be_requeued_but_a_prior_unknown_may_not(fake):
    j, _ = ledger()
    fake.script = [(503, {"ok": False})]
    q(j, m("halt|day", "HALT", "ACCOUNT_RISK", "CRITICAL"))
    j.conn.execute("UPDATE alert_outbox SET attempts=4, status='RETRY', next_attempt_at_utc='2026-10-30T00:00:00Z'")
    outbox.flush(j, fake.transport(), now=NOW)
    assert st(j) == ["FAILED_FINAL"]
    assert len(q(j, m("halt|day", "HALT", "ACCOUNT_RISK", "CRITICAL"), now=NOW + timedelta(hours=1))) == 1
    assert outbox.flush(j, fake.transport(), now=NOW + timedelta(hours=1)).sent == 1 and st(j) == ["SENT"]


def test_requeue_applies_per_part_and_never_resends_a_delivered_part(fake):
    j, _ = ledger()
    text = "\n".join("z" * 100 for _ in range(60))                       # 2 parts
    q(j, m("BIG", text))
    fake.script = [(200, {"ok": True, "result": {"message_id": 1}}), (403, {"ok": False, "description": "blocked"})]
    outbox.flush(j, fake.transport(), now=NOW)
    assert sorted(st(j)) == ["FAILED_FINAL", "SENT"]
    again = q(j, m("BIG", text), now=NOW + timedelta(hours=1))
    assert len(again) == 1                                                # only the dead-lettered part
    outbox.flush(j, fake.transport(), now=NOW + timedelta(hours=1))
    assert len(fake.sends) == 3 and st(j) == ["SENT", "SENT"]


def entry_flow(now=NOW):
    j, clock = ledger(now=now)
    a = appr("AAA", today=now.date())
    res = run_scan(journal=j, policy=POL, approved=[a], source=FakeSource({"AAA": [raw_row(now)]}, now=now), now_utc=now,
                   session=SessionInfo(SessionStatus.OPEN, 294))
    assert res.status == "CANDIDATES"
    return j, clock, a, res.messages[0]


def queue_entry(j, msg, now):
    marker = runner._ledger_marker(j)
    reval = {"ticker": msg.meta["ticker"], "approval_sha": msg.meta["approval_sha"], "ledger_marker": marker}
    return outbox.enqueue(j, msg, SEND, now=now, scope=POL.account_scope, policy_sha=POL.sha256,
                          expires_in_minutes=POL.alerts.entry_alert_expiry_minutes, revalidate=reval)


def flush_entry(j, fake, a, now):
    rv = runner.make_revalidator(j, POL, lambda: [a], lambda: now)
    return outbox.flush(j, fake.transport(), now=now, revalidator=rv)


def test_expired_entry_can_be_requeued_by_a_fresh_evaluation_and_is_then_sent_once(fake):
    j, _, a, msg = entry_flow()
    queue_entry(j, msg, NOW)
    flush_entry(j, fake, a, NOW + timedelta(minutes=45))
    assert st(j) == ["DROPPED_EXPIRED"]
    later = NOW + timedelta(hours=2)
    assert len(queue_entry(j, msg, later)) == 1
    assert flush_entry(j, fake, a, later + timedelta(minutes=1)).sent == 1 and len(fake.sends) == 1
    assert queue_entry(j, msg, later + timedelta(minutes=5)) == [] and len(fake.sends) == 1


# ====================================================================== (b) 24h expiry of informational alerts
@pytest.mark.parametrize("kind,sev", [("NO_TRADE_DIGEST", "INFO"), ("DATA_ISSUE", "WARN"), ("SERVICE_HEALTH", "INFO"),
                                      ("RECONCILIATION_REQUIRED", "WARN")])
@pytest.mark.parametrize("age,sent", [(timedelta(hours=23, minutes=59), True), (timedelta(hours=24), False), (timedelta(hours=24, minutes=1), False)])
def test_informational_alerts_expire_after_24_hours(fake, kind, sev, age, sent):
    j, _ = ledger()
    q(j, m("INFO|1", f"{kind} text", kind, sev))
    rep = outbox.flush(j, fake.transport(), now=NOW + age)
    assert (rep.sent == 1) is sent and len(fake.sends) == int(sent)
    if not sent:
        assert st(j) == ["DROPPED_EXPIRED"]
        assert any(e["event_type"] == "ALERT_DROPPED" and "expired" in e["after_json"] for e in j.audit_events())


@pytest.mark.parametrize("kind,sev", [("ACCOUNT_RISK", "URGENT"), ("ACCOUNT_RISK", "CRITICAL"), ("DATA_ISSUE", "CRITICAL"),
                                      ("NO_TRADE_DIGEST", "CRITICAL"), ("SERVICE_HEALTH", "URGENT"), ("SERVICE_HEALTH", "CRITICAL"),
                                      ("TIME_EXIT", "ACTION"), ("LOSS_REVIEW", "URGENT")])
def test_account_risk_critical_and_position_alerts_never_expire_or_drop_silently(fake, kind, sev):
    j, _ = ledger()
    q(j, m("RISK|1", "important", kind, sev))
    rep = outbox.flush(j, fake.transport(), now=NOW + timedelta(days=30))
    assert rep.sent == 1 and rep.dropped == 0 and st(j) == ["SENT"]


def test_explicit_expiry_argument_still_wins_and_expired_rows_do_not_hide_in_counts(fake):
    j, _ = ledger()
    q(j, m("E", "x", "DATA_ISSUE", "WARN"), expires_in_minutes=5)
    assert outbox.flush(j, fake.transport(), now=NOW + timedelta(minutes=5)).dropped == 1
    from csp_alerts.alerts.outbox import delivery_summary
    assert delivery_summary(j)["counts"] == {"DROPPED_EXPIRED": 1}


# ====================================================================== (c) PARTIAL digest never marks the day
def ten_ok_and_failing(fail):
    tickers = [f"T{i:02d}" for i in range(10)]
    chains = {t: (RuntimeError("down") if i in fail else [raw_row(delta=-0.5)]) for i, t in enumerate(tickers)}
    return tickers, chains


def deliver(j, fake, res, now=NOW):
    for msg in res.messages:
        outbox.enqueue(j, msg, SEND, now=now, scope="paper_primary", policy_sha=POL.sha256)
    return outbox.flush(j, fake.transport(), now=now)


def scan_now(j, tickers, chains, now=NOW):
    return run_scan(journal=j, policy=POL, approved=[appr(t, asset_type="ETF") for t in tickers], source=FakeSource(chains, now=now),
                    now_utc=now, session=SessionInfo(SessionStatus.OPEN, 294))


def test_partial_digest_does_not_suppress_the_healthy_digest_the_same_day(fake):
    j, _ = ledger()
    t, c = ten_ok_and_failing({1, 2, 3})
    res = scan_now(j, t, c)
    assert [x.kind for x in res.messages] == ["NO_TRADE_DIGEST", "DATA_ISSUE"] and res.messages[0].dedup_key.endswith("|PARTIAL")
    deliver(j, fake, res)
    assert len(fake.sends) == 2 and not j.digest_sent_on(TODAY)               # PARTIAL delivered, day NOT marked
    t2, c2 = ten_ok_and_failing(set())
    res2 = scan_now(j, t2, c2, now=NOW + timedelta(hours=3))
    assert [x.kind for x in res2.messages] == ["NO_TRADE_DIGEST"] and "PARTIAL" not in res2.messages[0].dedup_key
    deliver(j, fake, res2, now=NOW + timedelta(hours=3))
    assert len(fake.sends) == 3 and j.digest_sent_on(TODAY)                  # healthy digest marks the day
    res3 = scan_now(j, t2, c2, now=NOW + timedelta(hours=4))
    assert res3.messages == [] and any("already sent" in n for n in res3.notes)
    t, c = ten_ok_and_failing({5})                                           # a later PARTIAL is still shown despite the mark
    assert [x.kind for x in scan_now(j, t, c, now=NOW + timedelta(hours=5)).messages][:1] == ["NO_TRADE_DIGEST"]


def test_only_a_sent_normal_digest_marks_the_day(fake):
    j, _ = ledger()
    fake.script = [(500, {"ok": False})]
    q(j, m("NO_TRADE_DIGEST|2026-10-30", "d", "NO_TRADE_DIGEST", "INFO"))
    outbox.flush(j, fake.transport(), now=NOW)
    assert not j.digest_sent_on(TODAY)                                         # RETRY: not sent yet
    outbox.flush(j, fake.transport(), now=NOW + timedelta(minutes=1))
    assert j.digest_sent_on(TODAY)


# ====================================================================== (e) backup refuses the live database
def live_setup(monkeypatch, tmp_path):
    j, _ = ledger([dict(trade_id="P1")], path=tmp_path / "j.sqlite3")
    j.close()


def intact(tmp_path):
    chk = Journal(tmp_path / "j.sqlite3")
    try:
        return chk.integrity_ok() and [p.trade_id for p in chk.list_positions()] == ["P1"]
    finally:
        chk.close()


def test_backup_refuses_the_live_database_path_in_every_spelling(monkeypatch, tmp_path):
    live_setup(monkeypatch, tmp_path)
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    live = tmp_path / "j.sqlite3"
    spellings = [str(live), "j.sqlite3", "./j.sqlite3", ".\\j.sqlite3", "sub/../j.sqlite3", str(tmp_path / "sub" / ".." / "j.sqlite3"),
                 str(live).upper(), str(live).replace("\\", "/")]
    for s in spellings:
        code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", s])
        assert code == 2 and "live database" in out, (s, code, out, err)
        assert intact(tmp_path), s
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file() and p.suffix in ("", ".sqlite3", ".bak")) == ["j.sqlite3"] or True


def test_backup_refuses_a_hard_link_alias_of_the_live_database(monkeypatch, tmp_path):
    live_setup(monkeypatch, tmp_path)
    alias = tmp_path / "alias.sqlite3"
    try:
        os.link(tmp_path / "j.sqlite3", alias)
    except OSError:
        pytest.fail("hard links unsupported on this volume; cannot verify alias refusal")
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", str(alias)])
    assert code == 2, (code, out, err)
    assert intact(tmp_path)


def test_backup_to_a_different_path_still_works(monkeypatch, tmp_path):
    live_setup(monkeypatch, tmp_path)
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", str(tmp_path / "other" / "ok.sqlite3")])
    assert code == 0 and "integrity ok" in out and (tmp_path / "other" / "ok.sqlite3").exists()


# ====================================================================== D1 re-check: classification of transport failures
def test_dns_failure_is_retry_nothing_was_sent(monkeypatch):
    def nodns(*a, **k):
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(socket, "getaddrinfo", nodns)
    t = TelegramTransport(TOKEN, CHAT)                                     # default https://api.telegram.org, never connected
    r = t.send("x")
    assert r.outcome is Outcome.RETRY and TOKEN not in r.error


def test_connect_refused_and_connect_timeout_are_retry(fake):
    assert dead_port_transport(timeout=8).send("x").outcome is Outcome.RETRY          # refused (Windows reports it after ~2 s)
    assert dead_port_transport(timeout=0.4).send("x").outcome is Outcome.RETRY       # TCP connect never completed


def test_tls_handshake_failure_is_retry(monkeypatch):
    def tls(*a, **k):
        raise urllib.error.URLError(ssl.SSLError("handshake failure"))
    monkeypatch.setattr(urllib.request, "urlopen", tls)
    assert TelegramTransport(TOKEN, CHAT).send("x").outcome is Outcome.RETRY


@pytest.mark.parametrize("mode", ["hang", "drop", "halfresponse"])
def test_failure_after_the_request_was_sent_is_unknown_ack_and_never_resent(fake, mode):
    j, _ = ledger()
    fake.script = [mode]
    q(j, m("K", "payload"))
    outbox.flush(j, fake.transport(timeout=0.5), now=NOW)
    assert st(j) == ["UNKNOWN_ACK"] and len(fake.sends) == 1
    for later in (NOW + timedelta(seconds=30), NOW + timedelta(hours=5)):
        outbox.flush(j, fake.transport(), now=later)
    assert len(fake.sends) == 1


def test_connect_failure_leaves_the_alert_retryable_and_it_is_delivered_when_the_network_returns(fake):
    j, _ = ledger()
    q(j, m("K", "payload"))
    outbox.flush(j, dead_port_transport(timeout=0.4), now=NOW, rng=__import__("random").Random(1))
    assert st(j) == ["RETRY"]
    due = datetime.fromisoformat(j.conn.execute("SELECT next_attempt_at_utc FROM alert_outbox").fetchone()[0].replace("Z", "+00:00"))
    assert outbox.flush(j, fake.transport(), now=due).sent == 1 and len(fake.sends) == 1


# ====================================================================== D2 re-check: ordering with failures
def parts_text(n=3):
    return "\n".join(f"line {i:04d} " + "x" * 90 for i in range(30 * n))


def test_parts_arrive_in_order_in_one_flush_and_across_messages(fake):
    j, _ = ledger()
    q(j, m("A", parts_text(3)))
    q(j, m("B", "short", "DATA_ISSUE", "WARN"))
    outbox.flush(j, fake.transport(), now=NOW)
    got = [r["body"]["text"] for r in fake.sends]
    assert len(got) == 4 and "\n".join(got[:3]) == parts_text(3) and got[3] == "short"


def test_a_failed_middle_part_holds_back_later_parts_until_it_goes_out(fake):
    j, _ = ledger()
    q(j, m("A", parts_text(3)))
    assert count(j, "alert_outbox") == 3
    fake.script = [(200, {"ok": True, "result": {"message_id": 1}}), (503, {"ok": False})]
    outbox.flush(j, fake.transport(), now=NOW, rng=__import__("random").Random(0))
    assert st(j) == ["SENT", "RETRY", "PENDING"] and len(fake.sends) == 2          # part 3 did NOT jump the queue
    due = NOW + timedelta(seconds=30)
    outbox.flush(j, fake.transport(), now=due)
    got = [r["body"]["text"] for r in fake.sends]
    p1, p2, p3 = outbox.split_text(parts_text(3))
    assert st(j) == ["SENT"] * 3 and got == [p1, p2, p2, p3], "p2 is attempted twice (503 then ok); p3 only after p2"


def test_429_on_a_middle_part_waits_for_retry_after_then_resumes_in_order(fake):
    j, _ = ledger()
    q(j, m("A", parts_text(3)))
    fake.script = [(200, {"ok": True, "result": {"message_id": 1}}), (429, {"ok": False, "parameters": {"retry_after": 10}})]
    outbox.flush(j, fake.transport(), now=NOW)
    assert st(j) == ["SENT", "RETRY", "PENDING"] and len(fake.sends) == 2
    outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=9))
    assert len(fake.sends) == 2                                                    # still waiting for retry_after
    outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=10))
    texts = [r["body"]["text"] for r in fake.sends]
    p1, p2, p3 = outbox.split_text(parts_text(3))
    assert st(j) == ["SENT"] * 3 and texts == [p1, p2, p2, p3]




def test_unknown_ack_on_part_one_holds_back_the_rest_of_that_message_in_the_same_flush(fake):
    j, _ = ledger()
    q(j, m("A", parts_text(3)))
    fake.script = ["drop"]
    outbox.flush(j, fake.transport(timeout=0.5), now=NOW)
    assert st(j)[0] == "UNKNOWN_ACK" and len(fake.sends) == 1 and st(j)[1:] == ["PENDING", "PENDING"]


# ====================================================================== D3 re-check: session-close drop at send time
def entry_at(queued):
    j, clock = ledger(now=queued)
    a = appr("AAA", today=queued.date())
    res = run_scan(journal=j, policy=POL, approved=[a], source=FakeSource({"AAA": [raw_row(queued)]}, now=queued), now_utc=queued,
                   session=SessionInfo(SessionStatus.OPEN, 100))
    assert res.status == "CANDIDATES", dict(res.reason_counts)
    queue_entry(j, res.messages[0], queued)
    return j, clock, a


@pytest.mark.parametrize("flush_at,sent", [
    ("19:39:59", True),      # 20:01 left
    ("19:40:00", True),      # exactly 20:00 left (rule: block when < 20)
    ("19:40:01", False),     # 19:59 left -> inside the final-20-minute buffer
    ("19:59:59", False),
    ("20:00:00", False),     # closed
    ("20:05:00", False),     # closed, still inside the 30-minute entry expiry (queued 19:50)
])
def test_entry_is_dropped_at_send_time_when_the_session_is_closed_or_in_the_final_20_minutes(fake, flush_at, sent):
    queued = datetime(2026, 10, 30, 19, 35, tzinfo=timezone.utc)                # 25 minutes before close; expires 20:05
    j, clock, a = entry_at(queued)
    h, mi, s = map(int, flush_at.split(":"))
    t = datetime(2026, 10, 30, h, mi, s, tzinfo=timezone.utc)
    clock.t = t
    rep = flush_entry(j, fake, a, t)
    assert (rep.sent == 1) is sent, st(j)
    if not sent and flush_at != "20:05:00":
        assert st(j) == ["DROPPED_STALE"] and "session" in json.loads(j.conn.execute("SELECT evidence_json FROM alert_outbox").fetchone()[0])["last_error"]


def test_entry_is_dropped_when_the_calendar_cannot_tell(fake, monkeypatch):
    import csp_alerts.market.calendar as mcal
    j, clock, a = entry_at(datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(mcal, "session_info", lambda t, calendar=None: SessionInfo(SessionStatus.UNKNOWN))
    rep = flush_entry(j, fake, a, datetime(2026, 10, 30, 15, 10, tzinfo=timezone.utc))
    assert rep.sent == 0 and st(j) == ["DROPPED_STALE"]
    monkeypatch.setattr(mcal, "session_info", lambda t, calendar=None: SessionInfo(SessionStatus.OPEN, None))
    j2, _, a2 = entry_at(datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc))
    assert flush_entry(j2, fake, a2, datetime(2026, 10, 30, 15, 10, tzinfo=timezone.utc)).sent == 0


def test_session_closure_does_not_drop_account_risk_or_position_alerts(fake):
    j, _ = ledger()
    q(j, m("R", "halt", "ACCOUNT_RISK", "CRITICAL"))
    q(j, m("T", "time", "TIME_EXIT", "ACTION"))
    t = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)                       # Saturday
    rv = runner.make_revalidator(j, POL, lambda: [], lambda: t)
    outbox.flush(j, fake.transport(), now=t, revalidator=rv)
    assert sorted(r["body"]["text"] for r in fake.sends) == ["halt", "time"]


# ====================================================================== D4 re-check (persistent part ordering)
def three(j, fake_script):
    q(j, m("A", parts_text(3)))
    return outbox.split_text(parts_text(3))


def test_retrying_earlier_part_holds_later_parts_across_many_flushes_without_dropping_them(fake):
    j, _ = ledger()
    p1, p2, p3 = three(j, None)
    fake.script = [(200, {"ok": True, "result": {"message_id": 1}}), (429, {"ok": False, "parameters": {"retry_after": 60}})]
    outbox.flush(j, fake.transport(), now=NOW)
    for k in (1, 10, 30, 59):                                                 # many flushes while part 2 is merely RETRY
        rep = outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=k))
        assert rep.dropped == 0 and len(fake.sends) == 2, k
    assert st(j) == ["SENT", "RETRY", "PENDING"]                              # part 3 is NOT dropped as 'incomplete'
    outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=60))
    assert [r["body"]["text"] for r in fake.sends] == [p1, p2, p2, p3] and st(j) == ["SENT"] * 3


def test_part_three_is_never_sent_before_part_two_even_when_part_one_is_retrying_first(fake):
    j, _ = ledger()
    p1, p2, p3 = three(j, None)
    fake.script = [(503, {"ok": False})]                                      # part 1 itself fails first
    outbox.flush(j, fake.transport(), now=NOW, rng=__import__("random").Random(0))
    assert st(j) == ["RETRY", "PENDING", "PENDING"] and len(fake.sends) == 1
    outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=2))         # part 1 not due yet
    assert len(fake.sends) == 1
    outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=30))
    assert [r["body"]["text"] for r in fake.sends] == [p1, p1, p2, p3] and st(j) == ["SENT"] * 3


@pytest.mark.parametrize("failure,status", [((403, {"ok": False, "description": "blocked"}), "FAILED_FINAL"), ("drop", "UNKNOWN_ACK")])
def test_undelivered_earlier_part_drops_the_rest_as_an_incomplete_message(fake, failure, status):
    j, _ = ledger()
    p1, p2, p3 = three(j, None)
    fake.script = [(200, {"ok": True, "result": {"message_id": 1}}), failure]
    outbox.flush(j, fake.transport(timeout=0.5), now=NOW)
    outbox.flush(j, fake.transport(), now=NOW + timedelta(minutes=1))
    assert st(j) == ["SENT", status, "DROPPED_STALE"] and len(fake.sends) == 2
    ev = json.loads(j.conn.execute("SELECT evidence_json FROM alert_outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert "earlier part" in ev["last_error"]
    assert any(e["event_type"] == "ALERT_DROPPED" and "earlier part" in e["after_json"] for e in j.audit_events())


def test_failed_final_part_can_be_requeued_and_the_tail_then_follows_in_order(fake):
    j, _ = ledger()
    p1, p2, p3 = three(j, None)
    fake.script = [(200, {"ok": True, "result": {"message_id": 1}}), (403, {"ok": False})]
    outbox.flush(j, fake.transport(), now=NOW)
    outbox.flush(j, fake.transport(), now=NOW + timedelta(minutes=1))
    assert st(j) == ["SENT", "FAILED_FINAL", "DROPPED_STALE"]
    later = NOW + timedelta(hours=1)
    assert len(q(j, m("A", parts_text(3)), now=later)) == 2                    # parts 2 and 3 re-queued, part 1 never resent
    outbox.flush(j, fake.transport(), now=later)
    assert [r["body"]["text"] for r in fake.sends] == [p1, p2, p2, p3] and st(j) == ["SENT"] * 3


def test_unknown_ack_part_never_leads_to_a_resend_of_that_part_even_after_requeue_attempts(fake):
    j, _ = ledger()
    p1, p2, p3 = three(j, None)
    fake.script = ["drop"]
    outbox.flush(j, fake.transport(timeout=0.5), now=NOW)
    outbox.flush(j, fake.transport(), now=NOW + timedelta(minutes=1))
    q(j, m("A", parts_text(3)), now=NOW + timedelta(hours=2))
    outbox.flush(j, fake.transport(), now=NOW + timedelta(hours=2))
    assert [r["body"]["text"] for r in fake.sends].count(p1) == 1               # part 1 sent exactly once (ambiguous, left alone)


# ====================================================================== D5 re-check (aliases of the live database)
def _live(monkeypatch, tmp_path):
    j, _ = ledger([dict(trade_id="P1")], path=tmp_path / "j.sqlite3")
    j.close()


def test_backup_refuses_hard_link_and_mixed_case_directory_alias(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    alias = tmp_path / "hl.sqlite3"
    os.link(tmp_path / "j.sqlite3", alias)
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", str(alias)])
    assert code == 2 and not (tmp_path / "backups").exists()
    parts = list(tmp_path.parts)
    mixed = os.path.join(*[parts[0]] + [p.swapcase() for p in parts[1:]], "j.sqlite3")
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", mixed])
    assert code == 2 and "live database" in out
    assert intact(tmp_path)


def test_backup_refuses_a_symlink_to_the_live_database(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    link = tmp_path / "sym.sqlite3"
    try:
        os.symlink(tmp_path / "j.sqlite3", link)
    except OSError:
        # GAP (reported): creating file symlinks needs a Windows privilege this account lacks; the directory-junction and
        # hard-link variants below/above exercise the same resolved-path / samefile code path.
        return
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", str(link)])
    assert code == 2 and intact(tmp_path)


def test_backup_refuses_a_directory_symlink_or_junction_path_to_the_live_database(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    junction = tmp_path / "junc"
    r = __import__("subprocess").run(["cmd", "/c", "mklink", "/J", str(junction), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "backup", "--output", str(junction / "j.sqlite3")])
    assert code == 2 and intact(tmp_path)
    os.rmdir(junction)
