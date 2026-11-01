"""Slice 3 acceptance: send-mode matrix, telegram-setup pinning, CLI delivery paths (in-process, frozen clock, loopback fake).

My own derivation of when a Telegram message may leave the machine (spec 9, 14; AT-12, AT-15; MVP IT-014):
  SEND  <=>  --send given  AND  --dry-run NOT given  AND  CSP_DRY_RUN is false  AND  token+chat configured
             AND  the chat id hash equals the pinned PRIVATE chat.
Every other combination prints only (exit 0 when no --send was requested, exit 2 with an explanation when --send was refused).
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, TODAY
from s2_helpers import FakeSource, raw_row
from s3_helpers import CHAT, CHAT_OTHER, TOKEN, FakeTelegram, chain_rows_for, ledger, run_cli, write_approved

from csp_alerts.alerts.sendmode import Mode, resolve_send_mode
from csp_alerts.alerts.telegram import chat_id_sha256

ENV_SEND = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT, "CSP_DRY_RUN": "false"}


@pytest.fixture()
def fake():
    f = FakeTelegram()
    yield f
    f.close()


def db(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "j.sqlite3")
    c.row_factory = sqlite3.Row
    return c


def n(tmp_path, table):
    c = db(tmp_path)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def setup_pinned(monkeypatch, tmp_path, fake, env=None):
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "init"])
    assert code == 0, out + err
    code, out, err = run_cli(monkeypatch, tmp_path, ["telegram-setup", "--apply"], env=env or ENV_SEND, transport=fake.transport())
    assert code == 0, out + err


# ====================================================================== pure truth table
@pytest.mark.parametrize("env_dry,send,dry,conf,pinned", list(itertools.product([True, False], repeat=5)))
def test_send_mode_truth_table_all_32_combinations(env_dry, send, dry, conf, pinned):
    d = resolve_send_mode(env_dry_run=env_dry, flag_send=send, flag_dry_run=dry, telegram_configured=conf, chat_pinned_ok=pinned)
    should_send = send and not dry and not env_dry and conf and pinned
    assert (d.mode is Mode.SEND) is should_send
    if send and not should_send:
        assert d.error, "a refused --send must explain itself"
    if not send:
        assert d.mode is Mode.DRY_RUN and d.error is None or (send and dry)


# ====================================================================== CLI-level matrix on telegram-test (counts real requests)
@pytest.mark.parametrize("env_dry,flags,pin,configured,expect_sends,expect_code", [
    ("false", ["--send"], True, True, 1, 0),
    ("true", ["--send"], True, True, 0, 2),                  # env switch blocks
    (None, ["--send"], True, True, 0, 2),                    # CSP_DRY_RUN unset defaults to dry-run
    ("false", [], True, True, 0, 0),                         # no flag: print only
    ("false", ["--dry-run"], True, True, 0, 0),
    ("false", ["--send", "--dry-run"], True, True, 0, 2),    # conflicting flags never send
    ("false", ["--send"], False, True, 0, 2),                # not pinned
    ("false", ["--send"], True, False, 0, 2),                # no token
    ("true", [], True, True, 0, 0),
])
def test_cli_send_matrix_telegram_test(monkeypatch, tmp_path, fake, env_dry, flags, pin, configured, expect_sends, expect_code):
    if pin:
        setup_pinned(monkeypatch, tmp_path, fake)
    else:
        run_cli(monkeypatch, tmp_path, ["journal", "init"])
    fake.requests.clear()
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT} if configured else {}
    if env_dry is not None:
        env["CSP_DRY_RUN"] = env_dry
    code, out, err = run_cli(monkeypatch, tmp_path, ["telegram-test", *flags], env=env, transport=fake.transport())
    assert (code, len(fake.sends)) == (expect_code, expect_sends), out + err
    assert TOKEN not in out + err and CHAT not in out + err
    assert n(tmp_path, "alert_outbox") == expect_sends
    if expect_sends:
        assert "CSP TELEGRAM TEST" in fake.sends[0]["body"]["text"] and "NO ORDER SENT" in fake.sends[0]["body"]["text"]


@pytest.mark.parametrize("cmd", ["heartbeat", "reconcile-reminder", "scan", "monitor"])
def test_every_send_capable_command_refuses_send_without_the_env_switch(monkeypatch, tmp_path, fake, cmd):
    setup_pinned(monkeypatch, tmp_path, fake)
    fake.requests.clear()
    env = {**ENV_SEND, "CSP_DRY_RUN": "true"}
    code, out, err = run_cli(monkeypatch, tmp_path, [cmd, "--send"], env=env, transport=fake.transport(),
                             source=FakeSource({}))
    assert code == 2 and fake.requests == [] and "CSP_DRY_RUN" in out and n(tmp_path, "alert_outbox") == 0
    code, out, err = run_cli(monkeypatch, tmp_path, [cmd, "--send", "--dry-run"], env=ENV_SEND, transport=fake.transport(),
                             source=FakeSource({}))
    assert code == 2 and fake.requests == [] and "conflicting" in out


def test_chat_id_change_after_pinning_blocks_sends(monkeypatch, tmp_path, fake):
    setup_pinned(monkeypatch, tmp_path, fake)
    fake.requests.clear()
    code, out, _ = run_cli(monkeypatch, tmp_path, ["telegram-test", "--send"], env={**ENV_SEND, "TELEGRAM_CHAT_ID": CHAT_OTHER},
                           transport=fake.transport(chat=CHAT_OTHER))
    assert code == 2 and fake.requests == [] and "pinned" in out


# ====================================================================== telegram-setup pinning
def test_setup_preview_makes_no_request_and_pins_nothing(monkeypatch, tmp_path, fake):
    run_cli(monkeypatch, tmp_path, ["journal", "init"])
    code, out, err = run_cli(monkeypatch, tmp_path, ["telegram-setup"], env=ENV_SEND, transport=fake.transport())
    assert code == 0 and "PREVIEW" in out and fake.requests == []
    c = db(tmp_path)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='TELEGRAM_CHAT_PINNED'").fetchone()[0] == 0


def test_setup_pins_private_chat_by_hash_only(monkeypatch, tmp_path, fake):
    setup_pinned(monkeypatch, tmp_path, fake)
    assert [r["method"] for r in fake.requests] == ["getChat"]           # one-time verification, nothing sent
    c = db(tmp_path)
    row = c.execute("SELECT after_json FROM audit_log WHERE event_type='TELEGRAM_CHAT_PINNED'").fetchone()
    pin = json.loads(row[0])
    assert pin == {"chat_id_sha256": chat_id_sha256(CHAT), "chat_type": "private"}
    blob = "\n".join(c.iterdump())
    assert CHAT not in blob and TOKEN not in blob


@pytest.mark.parametrize("kind", ["group", "supergroup", "channel", "weird"])
def test_setup_rejects_non_private_chats(monkeypatch, tmp_path, fake, kind):
    fake.chat_type = kind
    run_cli(monkeypatch, tmp_path, ["journal", "init"])
    code, out, err = run_cli(monkeypatch, tmp_path, ["telegram-setup", "--apply"], env=ENV_SEND, transport=fake.transport())
    assert code == 1 and "private" in out
    assert n(tmp_path, "audit_log") >= 1
    c = db(tmp_path)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='TELEGRAM_CHAT_PINNED'").fetchone()[0] == 0
    # and a later --send is still refused
    code, out, _ = run_cli(monkeypatch, tmp_path, ["telegram-test", "--send"], env=ENV_SEND, transport=fake.transport())
    assert code == 2 and fake.sends == []


def test_setup_api_error_is_redacted_and_pins_nothing(monkeypatch, tmp_path, fake):
    fake.script = [(401, {"ok": False, "description": f"Unauthorized {TOKEN} chat {CHAT}"})]
    run_cli(monkeypatch, tmp_path, ["journal", "init"])
    code, out, err = run_cli(monkeypatch, tmp_path, ["telegram-setup", "--apply"], env=ENV_SEND, transport=fake.transport())
    assert code == 1 and TOKEN not in out + err and CHAT not in out + err


def test_setup_without_credentials_fails_cleanly_and_never_prints_a_token(monkeypatch, tmp_path, fake):
    run_cli(monkeypatch, tmp_path, ["journal", "init"])
    code, out, err = run_cli(monkeypatch, tmp_path, ["telegram-setup", "--apply"], env={}, transport=fake.transport())
    assert code == 2 and fake.requests == []


def test_placeholder_credentials_count_as_unconfigured(monkeypatch, tmp_path, fake):
    run_cli(monkeypatch, tmp_path, ["journal", "init"])
    code, out, _ = run_cli(monkeypatch, tmp_path, ["telegram-setup", "--apply"],
                           env={"TELEGRAM_BOT_TOKEN": "REPLACE_LOCALLY", "TELEGRAM_CHAT_ID": "REPLACE_LOCALLY"}, transport=fake.transport())
    assert code == 2 and fake.requests == []


# ====================================================================== dry-run never leaves sendable rows; real delivery path
def prepared(monkeypatch, tmp_path, fake, positions=None, rows=None):
    """Reconciled DB at tmp_path/j.sqlite3 (frozen ledger), whitelist file, pinned chat."""
    j, clock = ledger(positions or [], path=tmp_path / "j.sqlite3")
    j.record_policy(__import__("acc_helpers").policy())
    j.close()
    run_cli(monkeypatch, tmp_path, ["journal", "init"])
    setup_pinned(monkeypatch, tmp_path, fake)
    fake.requests.clear()
    ap = write_approved(tmp_path / "approved.json")
    return ap


def test_dry_run_scan_monitor_heartbeat_reminder_leave_zero_sendable_rows_even_when_sending_is_fully_enabled(monkeypatch, tmp_path, fake):
    ap = prepared(monkeypatch, tmp_path, fake, positions=[dict(trade_id="P1", ticker="AAA", expiration=TODAY + timedelta(days=10))])
    src = FakeSource({"AAA": [raw_row()]})
    for cmd in (["scan"], ["scan", "--dry-run"], ["monitor"], ["heartbeat"], ["reconcile-reminder"], ["monitor", "--dry-run"]):
        code, out, err = run_cli(monkeypatch, tmp_path, cmd, env=ENV_SEND, transport=fake.transport(), source=src, approved=ap)
        assert code in (0, 1), out + err
        assert "DRY-RUN: printed only, nothing sent" in out or "no reminder needed" in out or "not a trading day" in out, (cmd, out)
    assert fake.sends == [] and n(tmp_path, "alert_outbox") == 0
    c = db(tmp_path)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='NO_TRADE_DIGEST_SENT'").fetchone()[0] == 0


def test_scan_send_delivers_one_candidate_once_and_dedups_a_rerun(monkeypatch, tmp_path, fake):
    ap = prepared(monkeypatch, tmp_path, fake)
    src = FakeSource({"AAA": [raw_row()]})
    code, out, err = run_cli(monkeypatch, tmp_path, ["scan", "--send"], env=ENV_SEND, transport=fake.transport(), source=src, approved=ap)
    assert code == 0, out + err
    assert len(fake.sends) == 1 and fake.sends[0]["body"]["parse_mode"] == "HTML"
    t = fake.sends[0]["body"]["text"]
    assert "CSP RESEARCH CANDIDATE" in t and "NO ORDER SENT" in t and TOKEN not in t and CHAT not in t
    assert "sent=1" in out
    code, out, _ = run_cli(monkeypatch, tmp_path, ["scan", "--send"], env=ENV_SEND, transport=fake.transport(), source=src, approved=ap)
    assert len(fake.sends) == 1, "same contract the same day must not be delivered twice"
    c = db(tmp_path)
    assert [r[0] for r in c.execute("SELECT status FROM alert_outbox")] == ["SENT"]


def test_delivery_failure_gives_nonzero_exit_but_never_a_traceback(monkeypatch, tmp_path, fake):
    ap = prepared(monkeypatch, tmp_path, fake)
    fake.script = [(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"})]
    code, out, err = run_cli(monkeypatch, tmp_path, ["heartbeat", "--send"], env=ENV_SEND, transport=fake.transport(), approved=ap)
    assert code == 1 and "failed=1" in out and "Traceback" not in err and TOKEN not in out + err


def test_unknown_ack_gives_nonzero_exit_and_is_not_retried_by_the_next_run(monkeypatch, tmp_path, fake):
    ap = prepared(monkeypatch, tmp_path, fake)
    fake.script = ["hang"]
    code, out, err = run_cli(monkeypatch, tmp_path, ["heartbeat", "--send"], env=ENV_SEND, transport=fake.transport(timeout=0.4), approved=ap)
    assert code == 1 and "unknown_ack=1" in out
    code, out, _ = run_cli(monkeypatch, tmp_path, ["heartbeat", "--send"], env=ENV_SEND, transport=fake.transport(), approved=ap)
    assert len(fake.sends) == 1                                          # still just the one possibly-delivered attempt


def test_blocked_scan_status_message_is_sent_with_its_reason(monkeypatch, tmp_path, fake):
    ap = prepared(monkeypatch, tmp_path, fake)
    code, out, err = run_cli(monkeypatch, tmp_path, ["scan", "--send"], env=ENV_SEND, transport=fake.transport(),
                             source=FakeSource({}), approved=ap, now=NOW + timedelta(hours=40))        # snapshot is now stale
    assert code == 0 and len(fake.sends) == 1
    t = fake.sends[0]["body"]["text"]
    assert "LEDGER_STALE" in t and "NO ORDER SENT" in t


def test_service_failure_is_a_visible_run_status_and_message(monkeypatch, tmp_path, fake):
    import csp_alerts.jobs.scan as scanmod
    ap = prepared(monkeypatch, tmp_path, fake)

    def boom(*a, **k):
        raise RuntimeError(f"kaboom {TOKEN}")
    monkeypatch.setattr(scanmod, "normalize_rows", boom)
    code, out, err = run_cli(monkeypatch, tmp_path, ["scan", "--send"], env=ENV_SEND, transport=fake.transport(),
                             source=FakeSource({"AAA": [raw_row()]}), approved=ap)
    assert code == 1 and TOKEN not in out + err
    c = db(tmp_path)
    assert [r[0] for r in c.execute("SELECT status FROM run_status WHERE kind='SCAN'")] == ["SERVICE_FAILURE"]
    assert len(fake.sends) == 1 and "SERVICE FAILURE" in fake.sends[0]["body"]["text"] and TOKEN not in fake.sends[0]["body"]["text"]


# ====================================================================== scheduled-for lateness (wake never replays entries)
@pytest.mark.parametrize("now,runs", [
    (datetime(2026, 10, 30, 15, 5, 2, tzinfo=timezone.utc), True),         # 10:05:02 CDT
    (datetime(2026, 10, 30, 15, 20, 0, tzinfo=timezone.utc), True),        # exactly 15:00 late
    (datetime(2026, 10, 30, 15, 20, 1, tzinfo=timezone.utc), False),       # 15:01 late -> skipped
    (datetime(2026, 10, 30, 15, 3, 0, tzinfo=timezone.utc), True),         # 2:00 early still ok
    (datetime(2026, 10, 30, 15, 2, 59, tzinfo=timezone.utc), False),       # >2 min early
    (datetime(2026, 10, 30, 19, 0, 0, tzinfo=timezone.utc), False),        # woke at 14:00 CT: 10:05 run is long gone
])
def test_scheduled_scan_runs_only_inside_its_window(monkeypatch, tmp_path, fake, now, runs):
    j, _ = ledger(path=tmp_path / "j.sqlite3", now=now)
    j.close()
    ap = write_approved(tmp_path / "approved.json")
    src = FakeSource({"AAA": [raw_row(now)]}, now=now)
    code, out, err = run_cli(monkeypatch, tmp_path, ["scan", "--scheduled-for", "10:05", "--dry-run"], source=src, approved=ap, now=now)
    assert code == 0, out + err
    c = db(tmp_path)
    st = [r[0] for r in c.execute("SELECT status FROM run_status WHERE kind='SCAN'")]
    if runs:
        assert src.calls and st != ["SKIPPED_LATE"]
    else:
        assert src.calls == [] and st == ["SKIPPED_LATE"] and "SKIPPED" in out and "never replayed" in out


def test_scheduled_for_garbage_fails_cleanly(monkeypatch, tmp_path):
    for bad in ("abc", "25:99", ""):
        code, out, err = run_cli(monkeypatch, tmp_path, ["scan", "--scheduled-for", bad, "--dry-run"], source=FakeSource({}))
        assert code in (0, 1, 2) and "Traceback" not in err
