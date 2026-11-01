"""Slice 3A: send-mode matrix, Telegram transport (fake server), outbox, revalidation, pinning."""
from __future__ import annotations

import itertools
import json
import random
from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import NOW, TODAY, approval, policy, snap
from fake_telegram import FakeTelegram

from csp_alerts import runner
from csp_alerts.alerts import outbox
from csp_alerts.alerts.sendmode import Mode, SendDecision, SendModeError, resolve_send_mode
from csp_alerts.alerts.telegram import Outcome, TelegramTransport, chat_id_sha256, redact
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.journal.store import Journal
from csp_alerts.settings import settings_from_mapping

P = policy()
TOKEN = "123456789:ABCdefGhIJKlmnoPQRstuVWXyz0123"
CHAT = "987654321"
SEND = SendDecision(Mode.SEND)


@pytest.fixture
def tg():
    s = FakeTelegram()
    yield s
    s.close()


def transport(tg, timeout=5.0):
    return TelegramTransport(TOKEN, CHAT, base_url=tg.url, timeout=timeout, allow_insecure_base_for_tests=True)


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "d.sqlite3", clock=lambda: NOW)
    jr.migrate()
    yield jr
    jr.close()


def msg(key="K1", kind="NO_TRADE_DIGEST", sev="INFO", text="hello", meta=None):
    return AlertMessage(kind, sev, text, key, meta=meta)


def enq(j, m=None, **kw):
    return outbox.enqueue(j, m or msg(), SEND, now=NOW, scope="paper_primary", policy_sha=P.sha256, **kw)


def flush(j, tg, now=NOW, **kw):
    return outbox.flush(j, transport(tg), now=now, secrets=(TOKEN, CHAT), rng=random.Random(1), **kw)


def status_of(j):
    return [r["status"] for r in j.conn.execute("SELECT status FROM alert_outbox ORDER BY rowid")]


# ---- send-mode matrix: no conflicting combination can send ----
@pytest.mark.parametrize("env_dry,send,dry,conf,pinned", list(itertools.product([True, False], repeat=5)))
def test_send_mode_matrix(env_dry, send, dry, conf, pinned):
    d = resolve_send_mode(env_dry_run=env_dry, flag_send=send, flag_dry_run=dry, telegram_configured=conf, chat_pinned_ok=pinned)
    expect_send = send and not dry and not env_dry and conf and pinned
    assert d.can_send is expect_send
    if send and not expect_send:
        assert d.error  # a refused send is always explained (CLI exits 2)
    if not send:
        assert d.mode is Mode.DRY_RUN and d.error is None


def test_dry_run_never_writes_sendable_rows(j):
    with pytest.raises(SendModeError):
        outbox.enqueue(j, msg(), SendDecision(Mode.DRY_RUN), now=NOW, scope="s", policy_sha="x")
    assert j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0


# ---- transport ----
def test_transport_ok_payload_and_path(tg):
    r = transport(tg).send("<b>hi</b>", silent=True)
    assert r.outcome is Outcome.OK and r.message_id == "42"
    req = tg.requests[0]
    assert req["path"] == f"/bot{TOKEN}/sendMessage"
    assert req["body"] == {"chat_id": CHAT, "text": "<b>hi</b>", "disable_notification": True, "parse_mode": "HTML"}


@pytest.mark.parametrize("status,body,outcome", [
    (429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}, Outcome.RETRY_AFTER),
    (500, {"ok": False, "description": "boom"}, Outcome.RETRY),
    (502, {"ok": False}, Outcome.RETRY),
    (400, {"ok": False, "description": "bad request"}, Outcome.PERMANENT),
    (401, {"ok": False, "description": "Unauthorized"}, Outcome.PERMANENT),
    (403, {"ok": False, "description": "bot was blocked"}, Outcome.PERMANENT),
    (200, {"ok": False, "description": "weird"}, Outcome.PERMANENT),
])
def test_transport_classification(tg, status, body, outcome):
    tg.script.append((status, body))
    r = transport(tg).send("x")
    assert r.outcome is outcome
    if outcome is Outcome.RETRY_AFTER:
        assert r.retry_after == 7


def test_transport_timeout_is_unknown_and_refused_connection_is_retry(tg):
    tg.script.append("sleep")
    assert transport(tg, timeout=0.3).send("x").outcome is Outcome.UNKNOWN
    import socket
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()  # nothing listens here now: connection refused
    dead = TelegramTransport(TOKEN, CHAT, base_url=f"http://127.0.0.1:{port}", timeout=3, allow_insecure_base_for_tests=True)
    assert dead.send("x").outcome is Outcome.RETRY


def test_transport_requires_https_and_redacts_secrets(tg):
    with pytest.raises(ValueError):
        TelegramTransport(TOKEN, CHAT, base_url="http://example.com")
    with pytest.raises(ValueError):
        TelegramTransport(TOKEN, CHAT, base_url="http://127.0.0.1:9")  # loopback only with the explicit test flag
    tg.script.append((400, {"ok": False, "description": f"chat {CHAT} bad token {TOKEN}"}))
    r = transport(tg).send("x")
    assert TOKEN not in r.error and CHAT not in r.error
    assert TOKEN not in redact(f"https://api.telegram.org/bot{TOKEN}/sendMessage")


def test_get_chat_type(tg):
    tg.script.append((200, {"ok": True, "result": {"id": 1, "type": "group"}}))
    assert transport(tg).get_chat_type() == ("group", "")
    tg.script.append((400, {"ok": False, "description": "chat not found"}))
    kind, err = transport(tg).get_chat_type()
    assert kind is None and "chat not found" in err


# ---- outbox ----
def test_enqueue_dedup_and_stable_ids(j):
    ids = enq(j)
    assert len(ids) == 1 and enq(j) == []                     # same key: skipped
    assert ids[0] == outbox.stable_alert_id("paper_primary|K1")
    assert enq(j, msg(key="K2"))                               # different key: queued
    other_scope = outbox.enqueue(j, msg(), SEND, now=NOW, scope="paper_other", policy_sha="x")
    assert other_scope                                         # account-scoped keys never collide across scopes


def test_long_messages_are_split_into_ordered_parts(j):
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(120))
    ids = enq(j, msg(text=text))
    rows = j.conn.execute("SELECT content_text, dedup_key FROM alert_outbox ORDER BY rowid").fetchall()
    assert len(ids) == len(rows) >= 3 and all(len(r["content_text"]) <= 4096 for r in rows)
    assert "".join(r["content_text"] for r in rows).replace("\n", "") == text.replace("\n", "")
    assert rows[0]["dedup_key"].endswith(f"#1/{len(rows)}")
    assert len(outbox.split_text("a" * 9000)) == 3


def test_flush_ok_marks_sent_persists_before_send_and_silences_info(j, tg):
    enq(j)
    seen = {}
    t = transport(tg)
    orig = t.send

    def spy(text, **kw):
        seen["status_during_send"] = status_of(j)[0]
        seen.update(kw)
        return orig(text, **kw)

    t.send = spy
    rep = outbox.flush(j, t, now=NOW, rng=random.Random(1))
    assert rep.sent == 1 and status_of(j) == ["SENT"] and seen["status_during_send"] == "SENDING" and seen["silent"] is True
    assert j.conn.execute("SELECT telegram_message_id FROM alert_outbox").fetchone()[0] == "42"
    assert flush(j, tg).sent == 0 and len(tg.requests) == 1   # never resent


def test_429_waits_retry_after_then_succeeds(j, tg):
    enq(j)
    tg.script.append((429, {"ok": False, "parameters": {"retry_after": 10}}))
    assert flush(j, tg).retry == 1 and status_of(j) == ["RETRY"]
    assert flush(j, tg, now=NOW + timedelta(seconds=9)).sent == 0 and len(tg.requests) == 1
    assert flush(j, tg, now=NOW + timedelta(seconds=10)).sent == 1 and status_of(j) == ["SENT"]


def test_5xx_backoff_then_final_failure(j, tg):
    enq(j)
    now = NOW
    for i in range(5):
        tg.script.append((500, {"ok": False, "description": "x"}))
    for i in range(5):
        flush(j, tg, now=now)
        now += timedelta(seconds=400)
    assert status_of(j) == ["FAILED_FINAL"] and len(tg.requests) == 5
    assert j.conn.execute("SELECT attempts FROM alert_outbox").fetchone()[0] == 5


def test_permanent_4xx_dead_letters_without_retry(j, tg):
    enq(j)
    tg.script.append((403, {"ok": False, "description": f"blocked {CHAT}"}))
    rep = flush(j, tg)
    assert rep.failed == 1 and status_of(j) == ["FAILED_FINAL"]
    ev = j.conn.execute("SELECT evidence_json FROM alert_outbox").fetchone()[0]
    assert CHAT not in ev and TOKEN not in ev
    flush(j, tg, now=NOW + timedelta(hours=1))
    assert len(tg.requests) == 1


def test_timeout_after_post_is_unknown_ack_and_never_auto_resent(j, tg):
    enq(j)
    tg.script.append("sleep")
    t = transport(tg, timeout=0.3)
    rep = outbox.flush(j, t, now=NOW, rng=random.Random(1))
    assert rep.unknown == 1 and status_of(j) == ["UNKNOWN_ACK"]
    flush(j, tg, now=NOW + timedelta(hours=1))
    assert len(tg.requests) == 1  # only the original (timed-out) attempt


def test_crash_recovery_marks_sending_rows_unknown(j, tg):
    enq(j)
    j.conn.execute("UPDATE alert_outbox SET status='SENDING'")
    rep = flush(j, tg)
    assert rep.unknown == 1 and status_of(j) == ["UNKNOWN_ACK"] and tg.requests == []


def test_expiry_drops_entry_alerts(j, tg):
    enq(j, msg(kind="ENTRY_RESEARCH"), expires_in_minutes=30)
    assert flush(j, tg, now=NOW + timedelta(minutes=30)).dropped == 1
    assert status_of(j) == ["DROPPED_EXPIRED"] and tg.requests == []
    assert j.audit_events("ALERT_DROPPED")


def test_revalidator_can_drop_and_chat_check_blocks_everything(j, tg):
    enq(j, msg(kind="ENTRY_RESEARCH"))
    assert flush(j, tg, revalidator=lambda ev, kind: "ledger changed").dropped == 1 and status_of(j) == ["DROPPED_STALE"]
    enq(j, msg(key="K9"))
    rep = flush(j, tg, chat_check=lambda: "recipient not pinned")
    assert rep.sent == 0 and rep.notes == ["recipient not pinned"] and tg.requests == []


def test_severity_ordering(j, tg):
    enq(j, msg(key="a", sev="INFO", text="info"))
    enq(j, msg(key="b", sev="URGENT", text="urgent"))
    enq(j, msg(key="c", sev="ACTION", text="action"))
    flush(j, tg)
    assert [r["body"]["text"] for r in tg.requests] == ["urgent", "action", "info"]


# ---- runner: pinning and IT-003 revalidation ----
def settings(**extra):
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT, "CSP_DRY_RUN": "false"}
    env.update(extra)
    return settings_from_mapping(env)


def test_pinning_requires_private_chat_matching_id(j):
    s = settings()
    assert not runner.pinned_ok(j, s)
    j.pin_chat(chat_id_sha256(CHAT), "group")
    assert not runner.pinned_ok(j, s)
    j.pin_chat(chat_id_sha256("111"), "private")
    assert not runner.pinned_ok(j, s)                         # pinned to a different chat
    j.pin_chat(chat_id_sha256(CHAT), "private")
    assert runner.pinned_ok(j, s)
    assert runner.decide(j, s, flag_send=True, flag_dry_run=False).can_send
    assert not runner.decide(j, settings(CSP_DRY_RUN="true"), flag_send=True, flag_dry_run=False).can_send
    assert not runner.decide(j, s, flag_send=True, flag_dry_run=True).can_send
    assert "token" not in json.dumps(j.audit_events("TELEGRAM_CHAT_PINNED")).lower().replace("telegram", "")
    assert CHAT not in json.dumps(j.audit_events("TELEGRAM_CHAT_PINNED"))


def _entry_setup(j, tg):
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(equity="100000", cash="60000", asof=NOW - timedelta(hours=1)))
    a = approval(ticker="AAA", asset_type="ETF", sector="s1", correlation_bucket="b1",
                 standard_deliverable_confirmed_on=TODAY, events_cleared_through=TODAY + timedelta(days=400))
    j.pin_chat(chat_id_sha256(CHAT), "private")
    m = msg(kind="ENTRY_RESEARCH", key="ENTRY|AAA", meta={"ticker": "AAA", "approval_sha": runner.approval_sha(a)})
    return a, m


def run_dispatch(j, tg, a, m, pol=P, approved=None, clock=lambda: NOW):
    return runner.dispatch(j, [m], SEND, settings(), pol, NOW, transport(tg),
                           lambda: [a] if approved is None else approved, clock)


def test_entry_alert_sent_when_still_eligible(j, tg):
    a, m = _entry_setup(j, tg)
    rep = run_dispatch(j, tg, a, m)
    assert rep.sent == 1 and rep.dropped == 0


def test_entry_alert_dropped_when_state_changes_before_send(j, tg):
    a, m = _entry_setup(j, tg)
    # ledger changed between queue and send is simulated by queueing, mutating, then flushing
    marker_msgs = [m]
    from csp_alerts.journal.store import NewPosition
    runner_marker = runner._ledger_marker(j)
    outbox.enqueue(j, m, SEND, now=NOW, scope="paper_primary", policy_sha=P.sha256, expires_in_minutes=30,
                   revalidate={"ticker": "AAA", "approval_sha": runner.approval_sha(a), "ledger_marker": runner_marker})
    j.create_draft(NewPosition("T1", "ZZZ", TODAY + timedelta(days=40), D("50"), 100, 1, "o", "o"))
    from csp_alerts.journal.models import PositionState
    j.transition("T1", PositionState.PENDING)
    chk = runner.make_revalidator(j, P, lambda: [a], lambda: NOW)
    assert "ledger" in chk(json.loads(j.conn.execute("SELECT evidence_json FROM alert_outbox").fetchone()[0]), "ENTRY_RESEARCH")
    assert marker_msgs


def test_revalidator_checks_policy_approval_and_blockers(j, tg):
    a, m = _entry_setup(j, tg)
    marker = runner._ledger_marker(j)
    ev = {"policy_sha": P.sha256, "revalidate": {"ticker": "AAA", "approval_sha": runner.approval_sha(a), "ledger_marker": marker}}
    chk = runner.make_revalidator(j, P, lambda: [a], lambda: NOW)
    assert chk(ev, "ENTRY_RESEARCH") is None and chk(ev, "NO_TRADE_DIGEST") is None
    assert "policy" in runner.make_revalidator(j, P, lambda: [a], lambda: NOW)({**ev, "policy_sha": "old"}, "ENTRY_RESEARCH")
    assert "approval" in chk({**ev, "revalidate": {**ev["revalidate"], "approval_sha": "x"}}, "ENTRY_RESEARCH")
    assert "approval" in runner.make_revalidator(j, P, lambda: [], lambda: NOW)(ev, "ENTRY_RESEARCH")   # approval removed
    late = runner.make_revalidator(j, P, lambda: [a], lambda: NOW + timedelta(hours=40))                  # snapshot now stale
    assert "blocked" in late(ev, "ENTRY_RESEARCH")
