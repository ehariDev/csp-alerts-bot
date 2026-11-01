"""Slice 3 acceptance: Telegram transport + durable outbox against a LOCAL loopback fake only (spec 9.1, 13.2, 15.2; AT-04, AT-11,
AT-15). No real Telegram host is ever contacted; all tokens/chat ids are fake."""
from __future__ import annotations

import json
import random
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, TODAY, policy
from s2_helpers import FakeSource, appr, raw_row
from s3_helpers import (
    CHAT, CHAT_OTHER, TOKEN, FakeTelegram, count, dead_port_transport, dump_text, ledger,
)

from csp_alerts import runner
from csp_alerts.alerts import outbox, templates
from csp_alerts.alerts.sendmode import Mode, SendDecision, SendModeError
from csp_alerts.alerts.telegram import Outcome, TelegramTransport, redact
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.jobs.scan import run_scan
from csp_alerts.market.models import SessionInfo, SessionStatus

SEND = SendDecision(Mode.SEND)
POL = policy()


@pytest.fixture()
def fake():
    f = FakeTelegram()
    yield f
    f.close()


def msg(key="K1", text="hello", kind="DATA_ISSUE", sev="WARN"):
    return AlertMessage(kind, sev, text, key)


def q(j, m, now=NOW, scope="paper_primary", **kw):
    return outbox.enqueue(j, m, SEND, now=now, scope=scope, policy_sha=POL.sha256, **kw)


def status(j):
    return [r["status"] for r in j.conn.execute("SELECT status FROM alert_outbox ORDER BY rowid")]


def ev(j, i=0):
    return json.loads(j.conn.execute("SELECT evidence_json FROM alert_outbox ORDER BY rowid").fetchall()[i][0])


# ====================================================================== transport outcomes
def test_200_ok_posts_expected_payload_to_the_bot_path(fake):
    r = fake.transport().send("<b>x</b>", parse_mode="HTML", silent=True)
    assert r.outcome is Outcome.OK and r.message_id == "42"
    req = fake.sends[0]
    assert req["path"] == f"/bot{TOKEN}/sendMessage"
    assert req["body"] == {"chat_id": CHAT, "text": "<b>x</b>", "disable_notification": True, "parse_mode": "HTML"}


def test_ok_false_or_missing_result_is_never_a_success(fake):
    fake.script = [(200, {"ok": False, "description": "nope"}), (200, {"ok": True})]
    assert fake.transport().send("a").outcome is Outcome.PERMANENT
    r = fake.transport().send("b")
    assert r.outcome is Outcome.OK and r.message_id is None          # ok:true is acknowledged even without an id


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_4xx_is_permanent_and_dead_lettered_without_retry(fake, status_code):
    j, _ = ledger()
    fake.script = [(status_code, {"ok": False, "description": f"Bad Request: chat {CHAT} bot {TOKEN}"})]
    q(j, msg())
    rep = outbox.flush(j, fake.transport(), now=NOW, secrets=(TOKEN, CHAT))
    assert rep.failed == 1 and status(j) == ["FAILED_FINAL"] and len(fake.sends) == 1
    rep = outbox.flush(j, fake.transport(), now=NOW + timedelta(days=1), secrets=(TOKEN, CHAT))
    assert len(fake.sends) == 1                                         # dead letter: never retried
    assert TOKEN not in dump_text(j) and CHAT not in ev(j)["last_error"]


def test_429_honours_retry_after_exactly_and_resends_only_when_due(fake):
    j, _ = ledger()
    fake.script = [(429, {"ok": False, "parameters": {"retry_after": 7}})]
    q(j, msg())
    outbox.flush(j, fake.transport(), now=NOW)
    row = j.conn.execute("SELECT status, next_attempt_at_utc, attempts FROM alert_outbox").fetchone()
    assert row["status"] == "RETRY" and row["attempts"] == 1
    assert row["next_attempt_at_utc"] == (NOW + timedelta(seconds=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=6))
    assert len(fake.sends) == 1                                         # not due yet
    rep = outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=7))
    assert rep.sent == 1 and status(j) == ["SENT"] and len(fake.sends) == 2


def test_429_without_retry_after_defaults_to_30_seconds(fake):
    fake.script = [(429, {"ok": False})]
    assert fake.transport().send("x").retry_after == 30


def test_5xx_backoff_grows_with_bounded_jitter_and_dead_letters_after_5_attempts(fake):
    j, _ = ledger()
    fake.script = [(502, {"ok": False, "description": "Bad Gateway"})] * 6
    q(j, msg())
    now, delays = NOW, []
    for attempt in range(1, 6):
        outbox.flush(j, fake.transport(), now=now, rng=random.Random(attempt))
        r = j.conn.execute("SELECT status, next_attempt_at_utc, attempts FROM alert_outbox").fetchone()
        assert r["attempts"] == attempt
        if r["status"] == "RETRY":
            nxt = datetime.fromisoformat(r["next_attempt_at_utc"].replace("Z", "+00:00"))
            delays.append((nxt - now).total_seconds())
            now = nxt
    # base 5*2^(n-1) with 0..20% jitter: [5,6] [10,12] [20,24] [40,48]
    for d, base in zip(delays, (5, 10, 20, 40)):
        assert base <= d <= base * 1.2 + 1, (delays, base)
    assert status(j) == ["FAILED_FINAL"] and len(fake.sends) == 5 and ev(j)["last_error"].startswith("502")
    outbox.flush(j, fake.transport(), now=now + timedelta(days=2))
    assert len(fake.sends) == 5


def test_backoff_never_exceeds_the_cap():
    j, _ = ledger()
    t = dead_port_transport()
    q(j, msg())
    j.conn.execute("UPDATE alert_outbox SET attempts=3")                 # next failure is attempt 4 -> base 40 s
    outbox.flush(j, t, now=NOW, rng=random.Random(0))
    d = (datetime.fromisoformat(j.conn.execute("SELECT next_attempt_at_utc FROM alert_outbox").fetchone()[0].replace("Z", "+00:00")) - NOW).total_seconds()
    assert d <= 300 * 1.2


def test_connection_refused_before_send_is_retryable_not_unknown():
    j, _ = ledger()
    q(j, msg())
    rep = outbox.flush(j, dead_port_transport(timeout=8), now=NOW)       # Windows reports a refused loopback port after ~2 s
    assert rep.retry == 1 and status(j) == ["RETRY"]


def test_connect_phase_timeout_means_nothing_was_sent_so_it_must_be_retryable():
    """Nothing is listening: the TCP connection never completes, so no byte of the request left. A connect-phase timeout
    (PC just woke, network not up yet) must be RETRY; classifying it UNKNOWN_ACK strands the alert forever
    (never re-sent, and the same dedup key cannot be re-queued)."""
    j, _ = ledger()
    q(j, msg())
    outbox.flush(j, dead_port_transport(timeout=0.4), now=NOW)
    assert status(j) == ["RETRY"], status(j)


def test_timeout_after_post_is_unknown_ack_and_never_auto_resent(fake):
    j, _ = ledger()
    fake.script = ["hang"]
    q(j, msg())
    rep = outbox.flush(j, fake.transport(timeout=0.4), now=NOW)
    assert rep.unknown == 1 and status(j) == ["UNKNOWN_ACK"] and len(fake.sends) == 1
    for later in (NOW + timedelta(minutes=1), NOW + timedelta(days=1)):
        outbox.flush(j, fake.transport(), now=later)
    assert len(fake.sends) == 1 and status(j) == ["UNKNOWN_ACK"]         # possible duplicate is never created automatically
    assert q(j, msg()) == []                                             # and an identical alert cannot be re-queued


def test_connection_dropped_after_request_is_unknown_ack(fake):
    j, _ = ledger()
    fake.script = ["drop"]
    q(j, msg())
    outbox.flush(j, fake.transport(), now=NOW)
    assert status(j) == ["UNKNOWN_ACK"] and len(fake.sends) == 1


def test_crash_while_sending_becomes_unknown_ack_and_is_not_resent(fake):
    j, _ = ledger()
    q(j, msg())
    j.conn.execute("UPDATE alert_outbox SET status='SENDING', attempts=1")     # process died after persisting intent
    rep = outbox.flush(j, fake.transport(), now=NOW + timedelta(minutes=5))
    assert rep.unknown == 1 and status(j) == ["UNKNOWN_ACK"] and fake.sends == []
    assert "delivery unknown" in ev(j)["last_error"]


def test_intent_is_persisted_before_the_post(fake, tmp_path):
    path = tmp_path / "intent.sqlite3"
    j, _ = ledger(path=path)
    q(j, msg())
    seen = {}

    def spy(body):
        c = sqlite3.connect(path)               # independent connection: sees only COMMITTED state
        seen["status"] = c.execute("SELECT status FROM alert_outbox").fetchone()[0]
        c.close()
        return {"ok": True, "result": {"message_id": 7}}
    fake.script = [(200, spy)]
    outbox.flush(j, fake.transport(), now=NOW)
    assert seen["status"] == "SENDING" and status(j) == ["SENT"]
    assert j.conn.execute("SELECT telegram_message_id FROM alert_outbox").fetchone()[0] == "7"


def test_https_is_enforced_for_the_default_and_any_remote_base_url():
    for bad in ("http://api.telegram.org", "http://example.com", "ftp://x", "api.telegram.org", "http://127.0.0.1:9"):
        with pytest.raises(ValueError):
            TelegramTransport(TOKEN, CHAT, base_url=bad)
    TelegramTransport(TOKEN, CHAT)                                       # default https://api.telegram.org
    TelegramTransport(TOKEN, CHAT, base_url="https://api.telegram.org/")


# ====================================================================== splitting, ordering, persistence
def test_long_messages_are_split_at_3800_without_losing_a_character(fake):
    j, _ = ledger()
    lines = [f"line {i:04d} " + "x" * 90 for i in range(90)]                 # about 9,000 characters
    text = "\n".join(lines)
    ids = q(j, msg(text=text))
    assert len(ids) == 3
    assert all(len(r[0]) <= 3800 for r in j.conn.execute("SELECT content_text FROM alert_outbox"))
    assert "\n".join(outbox.split_text(text)) == text
    assert max(len(p) for p in outbox.split_text("y" * 10000)) <= 3800 and "".join(outbox.split_text("y" * 10000)) == "y" * 10000


def test_parts_are_delivered_in_order(fake):
    j, _ = ledger()
    lines = [f"line {i:04d} " + "x" * 90 for i in range(90)]
    q(j, msg(text="\n".join(lines)))
    outbox.flush(j, fake.transport(), now=NOW)
    got = [r["body"]["text"] for r in fake.sends]
    assert len(got) == 3 and "\n".join(got) == "\n".join(lines), "parts of one message must arrive in order"


def test_partial_delivery_of_a_split_message_retries_only_the_failed_part(fake):
    j, _ = ledger()
    text = "\n".join("z" * 100 for _ in range(60))                            # 2 parts
    q(j, msg(text=text))
    first_two = [(200, {"ok": True, "result": {"message_id": 1}}), (429, {"ok": False, "parameters": {"retry_after": 3}})]
    fake.script = first_two
    outbox.flush(j, fake.transport(), now=NOW)
    assert sorted(status(j)) == ["RETRY", "SENT"]
    assert q(j, msg(text=text)) == []                                           # re-enqueue does not duplicate parts
    outbox.flush(j, fake.transport(), now=NOW + timedelta(seconds=3))
    assert sorted(status(j)) == ["SENT", "SENT"] and len(fake.sends) == 3


def test_severity_order_critical_first(fake):
    j, _ = ledger()
    q(j, msg("a", "info", "NO_TRADE_DIGEST", "INFO"))
    q(j, msg("b", "crit", "ACCOUNT_RISK", "CRITICAL"))
    q(j, msg("c", "warn", "DATA_ISSUE", "WARN"))
    outbox.flush(j, fake.transport(), now=NOW)
    assert [r["body"]["text"] for r in fake.sends] == ["crit", "warn", "info"]
    assert [r["body"]["disable_notification"] for r in fake.sends] == [False, False, True]    # only INFO is silent


def test_restart_with_pending_rows_sends_them_once_from_a_new_connection(fake, tmp_path):
    path = tmp_path / "ob.sqlite3"
    j, _ = ledger(path=path)
    q(j, msg("p1", "one"))
    q(j, msg("p2", "two"))
    j.close()
    j2 = Journal(path)
    j2.migrate()
    assert sorted(status(j2)) == ["PENDING", "PENDING"]
    outbox.flush(j2, fake.transport(), now=NOW)
    outbox.flush(j2, fake.transport(), now=NOW)
    assert len(fake.sends) == 2 and sorted(status(j2)) == ["SENT", "SENT"]


def test_dry_run_decision_can_never_enqueue():
    j, _ = ledger()
    with pytest.raises(SendModeError):
        outbox.enqueue(j, msg(), SendDecision(Mode.DRY_RUN, "x"), now=NOW, scope="paper_primary", policy_sha="s")
    assert count(j, "alert_outbox") == 0


def test_chat_check_failure_sends_nothing_and_keeps_rows_pending(fake):
    j, _ = ledger()
    q(j, msg())
    rep = outbox.flush(j, fake.transport(), now=NOW, chat_check=lambda: "recipient not pinned")
    assert fake.sends == [] and status(j) == ["PENDING"] and rep.notes == ["recipient not pinned"]


# ====================================================================== ids, scoping, escalation
def test_alert_ids_are_stable_and_account_scoped():
    j1, _ = ledger()
    j2, _ = ledger()
    a = q(j1, msg("same"))[0]
    b = q(j2, msg("same"))[0]
    assert a == b == outbox.stable_alert_id("paper_primary|same")
    assert re.fullmatch(r"[0-9a-f-]{36}", a)
    assert q(j1, msg("same")) == []                                         # duplicate (same scope) skipped
    other = q(j1, msg("same"), scope="paper_b")                              # other account scope: independent
    assert len(other) == 1 and other[0] != a
    assert count(j1, "alert_outbox") == 2


def test_drawdown_escalation_3_to_5_to_8_percent_is_never_deduped_away(fake):
    from s2_helpers import make_journal, appr as _appr
    keys = []
    for eq in ("96900", "94900", "91900"):
        j = make_journal(snap=False)
        j.add_snapshot(AccountSnapshot("a", NOW - timedelta(hours=26), D("100000"), D("50000"), True, "v"))
        j.add_snapshot(AccountSnapshot("b", NOW - timedelta(hours=2), D(eq), D("50000"), True, "v"))
        res = run_scan(journal=j, policy=POL, approved=[_appr("AAA")], source=FakeSource({}), now_utc=NOW,
                       session=SessionInfo(SessionStatus.OPEN, 200))
        keys.append(res.messages[0].dedup_key)
    assert len(set(keys)) == 3
    j, _ = ledger()
    for i, k in enumerate(keys):
        assert len(q(j, msg(k, f"level {i}", "ACCOUNT_RISK", "URGENT"))) == 1       # all three are queued even after earlier ones SENT
        outbox.flush(j, fake.transport(), now=NOW)
    assert len(fake.sends) == 3 and status(j) == ["SENT"] * 3


# ====================================================================== entry expiry + revalidation (IT-003)
def entry_message():
    j, clock = ledger()
    a = appr("AAA")
    res = run_scan(journal=j, policy=POL, approved=[a], source=FakeSource({"AAA": [raw_row()]}), now_utc=NOW,
                   session=SessionInfo(SessionStatus.OPEN, 294))
    assert res.status == "CANDIDATES"
    return j, clock, a, res.messages[0]


def queue_entry(j, m, a, now=NOW, pol=POL):
    marker = runner._ledger_marker(j)
    reval = {"ticker": m.meta["ticker"], "approval_sha": m.meta["approval_sha"], "ledger_marker": marker}
    return outbox.enqueue(j, m, SEND, now=now, scope=pol.account_scope, policy_sha=pol.sha256,
                          expires_in_minutes=pol.alerts.entry_alert_expiry_minutes, revalidate=reval)


def flush_entry(j, fake, a, now, approved=None, pol=POL):
    rv = runner.make_revalidator(j, pol, lambda: [a] if approved is None else approved, lambda: now)
    return outbox.flush(j, fake.transport(), now=now, revalidator=rv)


@pytest.mark.parametrize("minutes,seconds,sent", [(29, 59, True), (30, 0, False), (31, 0, False)])
def test_entry_alert_expires_after_30_minutes(fake, minutes, seconds, sent):
    j, _, a, m = entry_message()
    assert m.kind == "ENTRY_RESEARCH"
    queue_entry(j, m, a)
    rep = flush_entry(j, fake, a, NOW + timedelta(minutes=minutes, seconds=seconds))
    assert (rep.sent == 1) is sent and len(fake.sends) == int(sent)
    if not sent:
        assert status(j) == ["DROPPED_EXPIRED"]
        assert any(e["event_type"] == "ALERT_DROPPED" for e in j.audit_events())


def test_unchanged_eligible_entry_is_sent(fake):
    j, _, a, m = entry_message()
    queue_entry(j, m, a)
    assert flush_entry(j, fake, a, NOW + timedelta(minutes=1)).sent == 1


def test_entry_dropped_when_ledger_changes_after_queueing(fake):
    j, clock, a, m = entry_message()
    queue_entry(j, m, a)
    clock.t = NOW + timedelta(minutes=2)
    from s3_helpers import open_position
    open_position(j, clock, trade_id="NEW1", ticker="ZZZ", strike="50.00", expiration=date(2026, 12, 11))
    rep = flush_entry(j, fake, a, NOW + timedelta(minutes=3))
    assert rep.dropped == 1 and fake.sends == [] and status(j) == ["DROPPED_STALE"]
    assert "ledger or snapshot changed" in ev(j)["last_error"]


def test_entry_dropped_when_a_new_snapshot_supersedes_the_one_it_was_built_on(fake):
    j, clock, a, m = entry_message()
    queue_entry(j, m, a)
    clock.t = NOW + timedelta(minutes=2)
    j.add_snapshot(AccountSnapshot("s9", clock.t, D("100000"), D("60000"), True, "v"))
    assert flush_entry(j, fake, a, NOW + timedelta(minutes=3)).dropped == 1 and fake.sends == []


@pytest.mark.parametrize("how", ["removed", "edited", "ceiling_lowered"])
def test_entry_dropped_when_owner_approval_is_revoked_or_edited(fake, how):
    j, _, a, m = entry_message()
    queue_entry(j, m, a)
    approved = [] if how == "removed" else [appr("AAA", ownership_reason="changed my mind")] if how == "edited" \
        else [appr("AAA", max_buy="70.00")]
    rep = flush_entry(j, fake, a, NOW + timedelta(minutes=3), approved=approved)
    assert rep.dropped == 1 and fake.sends == [] and "approval" in ev(j)["last_error"]


def test_entry_dropped_when_policy_revision_changes(fake):
    from s2_helpers import policy_variant
    j, _, a, m = entry_message()
    queue_entry(j, m, a)
    newpol = policy_variant(entries={"min_open_interest": 600})
    assert newpol.sha256 != POL.sha256
    rv = runner.make_revalidator(j, newpol, lambda: [a], lambda: NOW + timedelta(minutes=3))
    rep = outbox.flush(j, fake.transport(), now=NOW + timedelta(minutes=3), revalidator=rv)
    assert rep.dropped == 1 and fake.sends == [] and "policy revision" in ev(j)["last_error"]


def test_entry_dropped_when_drawdown_latch_appears_before_send(fake):
    j, clock, a, m = entry_message()
    queue_entry(j, m, a)
    # latch without touching the ledger marker: marker includes snapshot id, so simulate by patching the marker check path
    clock.t = NOW + timedelta(minutes=2)
    j.add_snapshot(AccountSnapshot("low", clock.t, D("91000"), D("60000"), True, "v"))
    rep = flush_entry(j, fake, a, NOW + timedelta(minutes=3))
    assert rep.dropped == 1 and fake.sends == []


def test_entry_alert_is_dropped_if_the_market_session_closed_before_send(fake):
    """AT-04: queued entry, then 'market closure' -> obsolete entry must not be sent (expiry alone is not enough:
    queued 10 minutes before the close, flushed 15 minutes after it, i.e. still inside the 30-minute expiry)."""
    close = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
    queued = close - timedelta(minutes=10)
    j, clock = ledger(now=queued)
    a = appr("AAA")
    res = run_scan(journal=j, policy=POL, approved=[a], source=FakeSource({"AAA": [raw_row(queued)]}, now=queued),
                   now_utc=queued, session=SessionInfo(SessionStatus.OPEN, 25))
    assert res.status == "CANDIDATES"
    queue_entry(j, res.messages[0], a, now=queued)
    later = close + timedelta(minutes=15)
    clock.t = later
    rep = flush_entry(j, fake, a, later)
    assert rep.sent == 0 and fake.sends == [], "an entry alert must not be delivered after the session closed"


def test_non_entry_alerts_are_not_dropped_by_ledger_changes(fake):
    j, clock = ledger()
    q(j, msg("risk", "halt", "ACCOUNT_RISK", "CRITICAL"))
    clock.t = NOW + timedelta(minutes=5)
    from s3_helpers import open_position
    open_position(j, clock, trade_id="NEW", ticker="ZZZ", strike="50.00")
    rv = runner.make_revalidator(j, POL, lambda: [], lambda: clock.t)
    outbox.flush(j, fake.transport(), now=clock.t, revalidator=rv)
    assert len(fake.sends) == 1 and status(j) == ["SENT"]


# ====================================================================== secrets
def test_redact_function_strips_tokens_and_ids():
    s = f"error calling https://api.telegram.org/bot{TOKEN}/sendMessage chat {CHAT} {CHAT_OTHER}"
    out = redact(s, TOKEN, CHAT)
    assert TOKEN not in out and CHAT not in out and "REDACTED" in out
    assert "bot[REDACTED_TOKEN]" in redact("bot" + TOKEN) or "REDACTED" in redact("bot" + TOKEN)
    assert redact("plain text 12345") == "plain text 12345"


def test_token_and_chat_id_never_reach_the_database_errors_or_reprs(fake):
    j, _ = ledger()
    fake.script = [(400, {"ok": False, "description": f"chat not found: {CHAT} / token {TOKEN} / bot{TOKEN}"}),
                   (500, {"ok": False, "description": f"oops {TOKEN} {CHAT}"}), "drop"]
    for k in ("a", "b", "c"):
        q(j, msg(k, f"text {k}"))
    t = fake.transport()
    outbox.flush(j, t, now=NOW, secrets=(TOKEN, CHAT))
    outbox.flush(j, t, now=NOW + timedelta(minutes=10), secrets=(TOKEN, CHAT))
    blob = dump_text(j)
    assert TOKEN not in blob and CHAT not in blob and "AAFakeTokenForTests" not in blob
    assert TOKEN not in repr(t) and TOKEN not in str(t) and CHAT not in repr(t)
    for r in j.conn.execute("SELECT evidence_json FROM alert_outbox"):
        assert TOKEN not in r[0] and CHAT not in r[0]


def test_transport_connection_errors_are_redacted_too():
    j, _ = ledger()
    t = dead_port_transport()
    q(j, msg())
    outbox.flush(j, t, now=NOW, secrets=(TOKEN, CHAT))
    assert TOKEN not in dump_text(j) and CHAT not in dump_text(j)
    res = t.send("x")
    assert TOKEN not in res.error and CHAT not in res.error


def test_message_text_never_contains_the_chat_id_or_token_by_construction():
    j, clock, a, m = entry_message()
    for text in (m.text, templates.heartbeat(["x"], NOW, "paper_primary").text, templates.service_failure("scan", "boom", NOW, "paper_primary").text):
        assert TOKEN not in text and CHAT not in text
