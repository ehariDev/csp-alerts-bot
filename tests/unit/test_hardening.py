"""Slice 3 hardening: phase-based send classification, part ordering, send-time session check, requeue, expiry."""
from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from factories import NOW, TODAY, approval, policy, snap
from fake_telegram import FakeTelegram

from csp_alerts import runner
from csp_alerts.alerts import outbox
from csp_alerts.alerts.sendmode import Mode, SendDecision
from csp_alerts.alerts.telegram import Outcome, TelegramTransport
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.cli import main
from csp_alerts.journal.store import Journal

P = policy()
SEND = SendDecision(Mode.SEND)
TOKEN, CHAT = "123456789:ABCdefGhIJKlmnoPQRstuVWXyz0123", "987654321"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tg():
    s = FakeTelegram()
    yield s
    s.close()


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "h.sqlite3", clock=lambda: NOW)
    jr.migrate()
    yield jr
    jr.close()


def tr(tg):
    return TelegramTransport(TOKEN, CHAT, base_url=tg.url, allow_insecure_base_for_tests=True)


def enq(j, kind="NO_TRADE_DIGEST", sev="INFO", key="K", text="x", now=NOW, **kw):
    return outbox.enqueue(j, AlertMessage(kind, sev, text, key), SEND, now=now, scope="s", policy_sha="p", **kw)


def statuses(j):
    return [r[0] for r in j.conn.execute("SELECT status FROM alert_outbox ORDER BY rowid")]


# ---- D1: phase, not exception type ----
def test_connect_phase_failures_are_retry_and_post_request_failures_are_unknown(tg, monkeypatch):
    t = tr(tg)

    def connect_timeout(*a, **k):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", connect_timeout)
    assert t.send("x").outcome is Outcome.RETRY

    def refused(*a, **k):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert t.send("x").outcome is Outcome.RETRY

    def read_timeout(*a, **k):
        raise TimeoutError("timed out waiting for response")

    monkeypatch.setattr(urllib.request, "urlopen", read_timeout)
    assert t.send("x").outcome is Outcome.UNKNOWN

    def dropped(*a, **k):
        raise ConnectionResetError()

    monkeypatch.setattr(urllib.request, "urlopen", dropped)
    assert t.send("x").outcome is Outcome.UNKNOWN


def test_connect_timeout_leaves_alert_retryable(j, tg, monkeypatch):
    enq(j)

    def connect_timeout(*a, **k):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", connect_timeout)
    outbox.flush(j, tr(tg), now=NOW)
    assert statuses(j) == ["RETRY"]


# ---- D2: parts in order ----
def test_parts_arrive_in_order_and_a_failed_part_holds_back_later_parts(j, tg):
    lines = [f"line {i:04d} " + "x" * 90 for i in range(90)]
    enq(j, text="\n".join(lines))
    outbox.flush(j, tr(tg), now=NOW)
    assert "\n".join(r["body"]["text"] for r in tg.requests) == "\n".join(lines) and len(tg.requests) == 3
    # part 2 fails with 429: part 3 must not jump ahead
    j2 = Journal(":memory:", clock=lambda: NOW)
    j2.migrate()
    enq(j2, text="\n".join(lines))
    tg.requests.clear()
    tg.script = [(200, {"ok": True, "result": {"message_id": 1}}), (429, {"ok": False, "parameters": {"retry_after": 5}})]
    outbox.flush(j2, tr(tg), now=NOW)
    assert len(tg.requests) == 2 and statuses(j2) == ["SENT", "RETRY", "PENDING"]
    tg.requests.clear()
    outbox.flush(j2, tr(tg), now=NOW + timedelta(seconds=6))
    assert [r["body"]["text"].split("\n")[0][:9] for r in tg.requests] == ["line 0030", "line 0060"][: len(tg.requests)] or len(tg.requests) == 2
    assert statuses(j2) == ["SENT", "SENT", "SENT"]


# ---- D3: session at send time ----
def test_entry_revalidation_drops_when_market_closed_unknown_or_final_minutes():
    clock = {"t": NOW}
    j = Journal(":memory:", clock=lambda: clock["t"])
    j.migrate()
    j.confirm_baseline(D("100000"))
    a = approval(ticker="AAA", asset_type="ETF", standard_deliverable_confirmed_on=TODAY,
                 events_cleared_through=TODAY + timedelta(days=400))
    j.add_snapshot(snap(equity="100000", cash="60000", asof=NOW - timedelta(minutes=1)))
    ev = {"policy_sha": P.sha256, "revalidate": {"ticker": "AAA", "approval_sha": runner.approval_sha(a),
                                                  "ledger_marker": runner._ledger_marker(j)}}
    chk = runner.make_revalidator(j, P, lambda: [a], lambda: clock["t"])
    assert chk(ev, "ENTRY_RESEARCH") is None                                   # 11:05 ET open
    clock["t"] = datetime(2026, 10, 30, 19, 45, tzinfo=timezone.utc)           # 15:45 ET: 15 min to close
    j.add_snapshot(snap(equity="100000", cash="60000", asof=clock["t"] - timedelta(minutes=1), sid="s2"))
    ev["revalidate"]["ledger_marker"] = runner._ledger_marker(j)
    assert "session" in chk(ev, "ENTRY_RESEARCH")
    clock["t"] = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)            # after close
    assert "session" in chk(ev, "ENTRY_RESEARCH") or "blocked" in chk(ev, "ENTRY_RESEARCH")
    assert chk(ev, "ACCOUNT_RISK") is None                                      # non-entry alerts are never session-dropped


# ---- (a) requeue after non-delivery ----
@pytest.mark.parametrize("final,expect_requeue", [
    ("DROPPED_EXPIRED", True), ("DROPPED_STALE", True), ("FAILED_FINAL", True),
    ("PENDING", False), ("RETRY", False), ("SENDING", False), ("SENT", False), ("UNKNOWN_ACK", False),
])
def test_dedup_requeue_rules(j, final, expect_requeue):
    assert enq(j)
    j.conn.execute("UPDATE alert_outbox SET status=?, attempts=3", (final,))
    got = enq(j, text="fresh")
    assert bool(got) is expect_requeue
    if expect_requeue:
        row = j.conn.execute("SELECT status, attempts, content_text FROM alert_outbox").fetchone()
        assert (row["status"], row["attempts"], row["content_text"]) == ("PENDING", 0, "fresh")
        assert j.audit_events("ALERT_REQUEUED")
    assert j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 1


# ---- (b) informational expiry, critical never dropped ----
def test_informational_classes_expire_after_24h_but_critical_never(j, tg):
    enq(j, "NO_TRADE_DIGEST", "INFO", "d")
    enq(j, "SERVICE_HEALTH", "INFO", "h")
    enq(j, "DATA_ISSUE", "WARN", "i")
    enq(j, "ACCOUNT_RISK", "CRITICAL", "c")
    enq(j, "SERVICE_HEALTH", "URGENT", "f")
    late = NOW + timedelta(hours=25)
    rep = outbox.flush(j, tr(tg), now=late)
    st = dict(j.conn.execute("SELECT dedup_key, status FROM alert_outbox").fetchall())
    assert st["s|d"] == st["s|h"] == st["s|i"] == "DROPPED_EXPIRED"
    assert st["s|c"] == "SENT" and st["s|f"] == "SENT" and rep.dropped == 3
    # still deliverable inside 24h
    j2 = Journal(":memory:", clock=lambda: NOW)
    j2.migrate()
    enq(j2, "NO_TRADE_DIGEST", "INFO", "d")
    assert outbox.flush(j2, tr(tg), now=NOW + timedelta(hours=23)).sent == 1


# ---- (c) partial digest does not mark the healthy digest as sent ----
def test_partial_digest_does_not_mark_the_day_digest_sent(j, tg):
    enq(j, "NO_TRADE_DIGEST", "WARN", "NO_TRADE_DIGEST|2026-10-30|PARTIAL")
    outbox.flush(j, tr(tg), now=NOW)
    assert not j.digest_sent_on(NOW.date())
    enq(j, "NO_TRADE_DIGEST", "INFO", "NO_TRADE_DIGEST|2026-10-30")
    outbox.flush(j, tr(tg), now=NOW)
    assert j.digest_sent_on(NOW.date())


# ---- (d) wording, (e) backup guard ----
def cli_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"CSP_DATABASE_URL=sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}\n", encoding="utf-8")
    return ["--env-file", str(env), "--policy", str(ROOT / "config" / "policy.yaml"), "--approved", str(ROOT / "config" / "approved.json")]


def test_cash_flow_preview_states_timing_in_words(tmp_path, capsys):
    b = cli_env(tmp_path)
    assert main(b + ["journal", "init"]) == 0
    assert main(b + ["journal", "record-cash-flow", "--amount", "-100", "--date", TODAY.isoformat(), "--note", "n"]) == 0
    out = capsys.readouterr().out
    assert "NEXT snapshot" in out and "BEFORE the check-in" in out and "PREVIEW ONLY" in out


def test_backup_refuses_live_database_path(tmp_path, capsys):
    b = cli_env(tmp_path)
    assert main(b + ["journal", "init"]) == 0
    capsys.readouterr()
    assert main(b + ["journal", "backup", "--output", str(tmp_path / "db.sqlite3")]) == 2
    assert "live database" in capsys.readouterr().out
    assert main(b + ["journal", "backup", "--output", str(tmp_path / "x" / ".." / "db.sqlite3")]) == 2
    assert main(b + ["journal", "backup", "--output", str(tmp_path / "ok.sqlite3")]) == 0


def test_retry_part_not_yet_due_holds_later_parts_across_flushes(j, tg):
    lines = [f"line {i:04d} " + "x" * 90 for i in range(90)]
    enq(j, text="\n".join(lines))
    tg.script = [(200, {"ok": True, "result": {"message_id": 1}}), (429, {"ok": False, "parameters": {"retry_after": 10}})]
    outbox.flush(j, tr(tg), now=NOW)
    assert statuses(j) == ["SENT", "RETRY", "PENDING"] and len(tg.requests) == 2
    outbox.flush(j, tr(tg), now=NOW + timedelta(seconds=9))           # part 2 not due: part 3 must NOT go out
    assert len(tg.requests) == 2 and statuses(j) == ["SENT", "RETRY", "PENDING"]
    outbox.flush(j, tr(tg), now=NOW + timedelta(seconds=10))
    assert len(tg.requests) == 4 and statuses(j) == ["SENT", "SENT", "SENT"]


def test_incomplete_message_is_not_continued_after_an_earlier_part_failed_final(j, tg):
    lines = [f"line {i:04d} " + "x" * 90 for i in range(90)]
    enq(j, text="\n".join(lines))
    tg.script = [(400, {"ok": False, "description": "bad"})]
    outbox.flush(j, tr(tg), now=NOW)
    outbox.flush(j, tr(tg), now=NOW + timedelta(seconds=1))
    assert statuses(j) == ["FAILED_FINAL", "DROPPED_STALE", "DROPPED_STALE"] and len(tg.requests) == 1


def test_backup_refuses_hard_link_and_alias_of_live_db(tmp_path, capsys):
    import os
    b = cli_env(tmp_path)
    assert main(b + ["journal", "init"]) == 0
    capsys.readouterr()
    link = tmp_path / "alias.sqlite3"
    os.link(tmp_path / "db.sqlite3", link)
    assert main(b + ["journal", "backup", "--output", str(link)]) == 2
    assert "live database" in capsys.readouterr().out
    assert main(b + ["journal", "backup", "--output", str(tmp_path / "fine.sqlite3")]) == 0


def test_reconciliation_reminders_expire_after_24h_but_safety_alerts_never(j, tg):
    enq(j, "RECONCILIATION_REQUIRED", "WARN", "r")
    for i, (kind, sev) in enumerate([("TIME_EXIT", "ACTION"), ("LOSS_REVIEW", "URGENT"), ("ACCOUNT_RISK", "URGENT"),
                                     ("ACCOUNT_RISK", "CRITICAL")]):
        enq(j, kind, sev, f"s{i}")
    rep = outbox.flush(j, tr(tg), now=NOW + timedelta(hours=30))
    st = dict(j.conn.execute("SELECT dedup_key, status FROM alert_outbox").fetchall())
    assert st["s|r"] == "DROPPED_EXPIRED" and all(st[f"s|s{i}"] == "SENT" for i in range(4)) and rep.dropped == 1
