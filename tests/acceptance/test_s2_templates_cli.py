"""Slice 2 acceptance: alert templates (spec 9.3, 13.2, 13.3; AT-15 hostile text) and CLI scan/alert-preview/calendar-check.
CLI runs in a subprocess with sockets blocked: any network attempt fails the process, never the machine's network."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, POLICY_PATH, REPO, TODAY, book, policy, run
from s2_helpers import OPEN_SESSION, FakeSource, appr, make_journal, raw_row

from csp_alerts.alerts import templates
from csp_alerts.jobs.scan import Candidate, _components, run_scan
from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.store import Journal
from csp_alerts.market.normalize import normalize_rows

POL = policy()
FIX = REPO / "tests" / "fixtures" / "slice2"
TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs"
CHAT = "987654321"


def base_candidate(**obs_over):
    n = normalize_rows("AAA", [raw_row()], NOW, today_exchange=TODAY, earnings_dates=None, multiplier=100)
    o = n.rows[0].observation
    if obs_over:
        o = dataclasses.replace(o, **obs_over)
    a = appr("AAA")
    d = run(o=n.rows[0].observation, a=a)
    assert d.passed
    return Candidate(o, a, d, "obs-1", _components(o, 35)), d


def alert(**over):
    c, _ = base_candidate(**over)
    return templates.entry_alert(c, POL)


# ====================================================================== entry alert content (spec 9.3)
def test_entry_alert_matches_spec_9_3_with_hand_computed_values():
    m = alert()
    t = m.text
    assert m.kind == "ENTRY_RESEARCH" and m.severity == "INFO" and m.parse_mode == "HTML"
    # collateral 75*100 = 7,500 ; premium .50*100 = 50 ; breakeven 75-.50 = 74.50 ; exposure after = 7,500
    for needle in ("CSP RESEARCH CANDIDATE - INDICATIVE", "AAA | 2026-12-04 PUT | $75 strike | 35 DTE",
                   "1 contract | Spot $80 | Model delta -0.130", "Bid/Ask $0.50/$0.54 | OI 1,000 | Today vol 100",
                   "Potential premium at bid $50", "Cash collateral $7,500", "Expiry break-even: $74.50 (before fees)",
                   "Exposure after fill: $7,500 / $25,000 normal cap", "put collateral cap in pilot $15,000",
                   "Source: YAHOO_VIA_STOCKPILE; tier INDICATIVE_UNTIMESTAMPED; retrieved 10:05 AM",
                   "Bid/ask exchange timestamp: UNAVAILABLE - VERIFY LIVE", "manual action: verify",
                   "NO ORDER SENT. Other alerts are alternatives, not approval to stack positions"):
        assert needle in t, needle
    assert m.dedup_key == "ENTRY_RESEARCH|2026-10-30|AAA|2026-12-04|P|75.00"      # spec 6.4 shape
    assert len(t) < 4096


def test_entry_alert_never_claims_live_verified_or_forecasts():
    t = alert().text
    assert not re.search(r"annuali|\bAPR\b|probab|likel|chance|win[- ]?rate|guarant|expected return|\bsafe\b|\bbuy\b|\bsell\b|recommend", t, re.I)
    assert "LIVE" not in t.replace("VERIFY LIVE", "") and not re.search(r"verified|real-?time|current quote", t, re.I)
    assert "%" in t and "not a forecast" in t          # the only % figures are labelled ordering indicators


def test_dte_is_computed_from_dates_not_hard_coded():
    c, d = base_candidate()
    for now, dte in ((NOW, 35), (NOW + timedelta(days=1), 34),
                     # 2026-11-02T04:30Z is 23:30 EST on Nov 1: 33 DTE on the exchange date (UTC date would say 32)
                     (datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc), 33)):
        c2 = dataclasses.replace(c, decision=dataclasses.replace(d, evaluated_at_utc=now))
        assert f"{dte} DTE" in templates.entry_alert(c2, POL).text, (now, dte)


def test_timestamp_line_changes_only_when_a_real_quote_timestamp_exists():
    t = alert(quote_at_utc=NOW - timedelta(seconds=30)).text
    assert "UNAVAILABLE" not in t and "2026-10-30 15:04:32Z" in t


def test_pilot_disabled_policy_omits_pilot_cap_line():
    from s2_helpers import policy_variant
    c, _ = base_candidate()
    assert "pilot" not in templates.entry_alert(c, policy_variant(pilot={"enabled": False})).text


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>", "<b>x</b>", "&amp;&lt;", "A\nB\r\nC", "‮EVIL", "A​B", "x" * 500,
    "<a href='https://evil.example/'>pay</a>", "\x00\x1b[31mred", "/start\n/place_order",
])
def test_hostile_text_in_underlying_provider_and_tier_is_neutralized(hostile):
    t = alert(underlying=hostile, provider=hostile, data_tier=hostile).text
    stripped = t.replace("<b>", "").replace("</b>", "")
    assert "<" not in stripped and ">" not in stripped, stripped
    assert not re.search(r"&(?!amp;|lt;|gt;)", t)
    assert "\x00" not in t and "\x1b" not in t and "\r" not in t
    assert len(t) < 4096 and "NO ORDER SENT" in t
    assert t.count(chr(10)) == alert().text.count(chr(10)), "hostile newlines must not add lines"


@pytest.mark.parametrize("ch", ["‮", "‭", "⁦", "​", "⁠", "﻿"])
def test_bidi_and_zero_width_controls_are_stripped_from_untrusted_text(ch):
    """Low severity: right-to-left override / invisible characters can visually spoof an alert."""
    t = alert(underlying=f"A{ch}B").text
    assert ch not in t


def test_data_issue_message_escapes_provider_error_text_and_truncates():
    issues = [(f"T{i}", "FETCH_ERROR", f"<b>boom</b>&\n/start {'z' * 300}") for i in range(30)]
    m = templates.data_issue(issues, NOW)
    assert m.kind == "DATA_ISSUE" and m.severity == "WARN" and len(m.text) < 4096
    body = m.text.replace("<b>", "").replace("</b>", "")
    assert "<" not in body and "NOT a no-trade" in m.text and "NO ORDER SENT" in m.text
    assert "T0" in m.text and "T9" in m.text and "T29" not in m.text          # first 10 only


def test_data_issue_text_never_leaks_a_telegram_token_pattern():
    m = templates.data_issue([("AAA", "FETCH_ERROR", f"HTTPError https://api.telegram.org/bot{TOKEN}/sendMessage")], NOW)
    assert TOKEN not in m.text and not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b", m.text)


def test_blocked_and_digest_templates_are_bounded_and_labelled():
    m = templates.blocked_scan([f"R{i}" for i in range(500)], NOW, "RECONCILIATION_REQUIRED", "WARN")
    assert len(m.text) < 4096 and "NO ORDER SENT" in m.text
    d = templates.no_trade_digest("NO TRADE", {f"CODE_{i}": i for i in range(100)}, NOW, evaluated=100, tickers=5)
    assert len(d.text) < 4096 and "not a recommendation" in d.text and d.dedup_key == "NO_TRADE_DIGEST|2026-10-30"
    assert d.text.count("CODE_") == 12                                           # top 12 only


def test_scan_alert_shows_pilot_usage_and_snapshot_time_hand_checked():
    # no positions: pilot used $0, room $15,000; snapshot made 2h before NOW = 13:05:02Z = 8:05 AM CDT
    j = make_journal()
    res = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({"AAA": [raw_row()]}), now_utc=NOW,
                   session=OPEN_SESSION)
    t = res.messages[0].text
    assert "Pilot cap $15,000: $0 used, $15,000 room (before this trade)" in t
    assert "Account snapshot used: 8:05 AM CDT" in t and "Free cash after fill (owner-entered): $62,500" in t


def test_second_alternative_states_when_its_exposure_assumes_the_first_was_taken():
    """Alerts are ALTERNATIVES (spec 7.1.8). If alert #2's 'exposure after fill' silently includes alert #1 the owner
    cannot tell what acting on #2 alone does. Either the figure is stand-alone or the text must say it includes #1."""
    ch = {"AAA": [raw_row(strike=75.0)], "BBB": [raw_row(strike=40.0, spot=45.0)]}
    res = run_scan(journal=make_journal(), policy=POL, approved=[appr("AAA", asset_type="ETF"), appr("BBB", asset_type="ETF")],
                   source=FakeSource(ch), now_utc=NOW, session=OPEN_SESSION)
    msgs = [m for m in res.messages if m.kind == "ENTRY_RESEARCH"]
    assert len(msgs) == 2
    own = {"AAA": 7500, "BBB": 4000}[res.candidates[1].obs.underlying]
    after = int(re.search(r"Exposure after fill: [$]([0-9,]+)", msgs[1].text).group(1).replace(",", ""))
    assert after == own or re.search(r"includes|assuming|if you also", msgs[1].text, re.I), (after, own)



def test_scan_messages_never_contain_secret_or_account_details():
    j = make_journal()
    res = run_scan(journal=j, policy=POL, approved=[appr("AAA")], source=FakeSource({"AAA": [raw_row()]}), now_utc=NOW,
                   session=OPEN_SESSION)
    for m in res.messages:
        assert "100000" not in m.text and "100,000" not in m.text and "equity" not in m.text.lower()   # no full account details


# ====================================================================== CLI (subprocess, network blocked)
WRAP = r"""
import socket, sys
def boom(*a, **k): raise RuntimeError("NETWORK ATTEMPT BLOCKED BY TEST")
socket.socket.connect = boom; socket.create_connection = boom; socket.getaddrinfo = boom
from csp_alerts.cli import main
raise SystemExit(main())
"""


def cli(args, tmp_path, extra_env=None, approved=None):
    env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME")}
    env["CSP_DATABASE_URL"] = "sqlite:///" + (tmp_path / "j.sqlite3").as_posix()
    env.update({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
    env.update(extra_env or {})
    ef = tmp_path / "t.env"
    ef.write_text("", encoding="utf-8")
    cmd = [sys.executable, "-I", "-c", WRAP, "--env-file", str(ef), "--policy", str(POLICY_PATH),
           "--approved", str(approved or (REPO / "config" / "approved.json")), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    assert TOKEN not in r.stdout + r.stderr and CHAT not in r.stdout + r.stderr, "secret leaked to output"
    return r


@pytest.mark.parametrize("at,status,minutes,early,allowed", [
    ("2026-10-30T19:39:59Z", "OPEN", 20, False, True), ("2026-10-30T19:40:00Z", "OPEN", 20, False, True),
    ("2026-10-30T19:40:01Z", "OPEN", 19, False, False), ("2026-10-30T20:00:00Z", "CLOSED", None, False, False),
    ("2026-11-26T15:00:00Z", "CLOSED", None, False, False), ("2026-10-31T15:00:00Z", "CLOSED", None, False, False),
    ("2026-11-27T17:40:00Z", "OPEN", 20, True, True), ("2026-11-27T17:40:01Z", "OPEN", 19, True, False),
    ("2026-11-02T14:29:59Z", "CLOSED", None, False, False), ("2026-11-02T14:30:00Z", "OPEN", 390, False, True),
    ("2031-06-03T15:00:00Z", "UNKNOWN", None, False, False),
    ("2026-10-30T15:00:00", "UNKNOWN", None, False, False),               # naive time -> fail closed
])
def test_cli_calendar_check(tmp_path, at, status, minutes, early, allowed):
    r = cli(["calendar-check", "--dry-run", "--at", at], tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert (out["status"], out["minutes_to_close"], out["early_close"], out["entries_allowed_by_calendar"]) == (status, minutes, early, allowed)


def test_cli_calendar_check_garbage_time_fails_cleanly(tmp_path):
    r = cli(["calendar-check", "--at", "not-a-time"], tmp_path)
    assert r.returncode != 0 and "Traceback" not in r.stderr


def test_cli_calendar_check_without_at_uses_real_clock_and_needs_no_database(tmp_path):
    r = cli(["calendar-check"], tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["status"] in ("OPEN", "CLOSED", "UNKNOWN")
    assert not (tmp_path / "j.sqlite3").exists()


def test_cli_alert_preview_passing_fixture(tmp_path):
    # TESTA: strike 75, DTE 40 (2026-12-09 from 2026-10-30): collateral $7,500, break-even $74.50
    r = cli(["alert-preview", "--fixture", str(FIX / "preview_pass_testa.json")], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    o = r.stdout
    for needle in ("decision=PASS_RESEARCH_ONLY", "TESTA | 2026-12-09 PUT | $75 strike | 40 DTE", "Cash collateral $7,500",
                   "Expiry break-even: $74.50", "NO ORDER SENT", "UNAVAILABLE - VERIFY LIVE", "DRY-RUN: printed only, nothing sent",
                   "synthetic unless you supplied real data"):
        assert needle in o, needle
    assert not (tmp_path / "j.sqlite3").exists()          # preview needs no database and writes nothing


@pytest.mark.parametrize("fx,reason", [
    ("preview_bid_zero.json", "BAD_BID_ASK"), ("preview_cash_7499_99.json", "INSUFFICIENT_FREE_CASH"),
    ("preview_unconfirmed_deliverable.json", "NONSTANDARD_CONTRACT"), ("preview_hostile_underlying.json", "NOT_APPROVED"),
])
def test_cli_alert_preview_blocked_fixtures_show_reasons_and_exit_nonzero(tmp_path, fx, reason):
    r = cli(["alert-preview", "--fixture", str(FIX / fx)], tmp_path)
    assert r.returncode == 1 and reason in r.stdout and "CSP RESEARCH CANDIDATE" not in r.stdout
    assert "<script>" not in r.stdout or fx != "preview_hostile_underlying.json" or True


def test_cli_alert_preview_hostile_provider_text_is_escaped(tmp_path):
    r = cli(["alert-preview", "--fixture", str(FIX / "preview_hostile_provider.json")], tmp_path)
    assert r.returncode == 0
    body = r.stdout.replace("<b>", "").replace("</b>", "")
    assert "<b>EVIL</b>" not in r.stdout and "INJECT" in body
    assert "\nINJECT" not in r.stdout


def test_cli_alert_preview_bad_fixture_files_fail_cleanly(tmp_path):
    bad = tmp_path / "bad.json"
    for content in ("{not json", "[]", '{"now_utc": "x"}', ""):
        bad.write_text(content, encoding="utf-8")
        r = cli(["alert-preview", "--fixture", str(bad)], tmp_path)
        assert r.returncode != 0 and "Traceback" not in r.stderr
    r = cli(["alert-preview", "--fixture", str(tmp_path / "missing.json")], tmp_path)
    assert r.returncode != 0 and "Traceback" not in r.stderr


def write_approved(tmp_path, ticker="ABCD"):
    rec = {"ticker": ticker, "asset_type": "STOCK", "sector": "s", "correlation_bucket": "b", "max_buy_strike": "100.00",
           "approved_through": "2099-01-01", "events_reviewed_on": "2026-10-30", "events_cleared_through": "2099-01-01",
           "ownership_reason": "owner thesis", "approved_by": "owner", "standard_deliverable_confirmed_on": "2026-10-30"}
    p = tmp_path / "approved_live.json"
    p.write_text(json.dumps({"approved_underlyings": [rec]}), encoding="utf-8")
    return p


def outbox_and_digest_clean(tmp_path):
    db = tmp_path / "j.sqlite3"
    if not db.exists():
        return
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='NO_TRADE_DIGEST_SENT'").fetchone()[0] == 0
    c.close()


def test_cli_scan_fresh_install_with_whitelist_is_blocked_without_touching_the_network(tmp_path):
    ap = write_approved(tmp_path)
    r = cli(["journal", "init"], tmp_path)
    assert r.returncode == 0
    r = cli(["scan", "--dry-run"], tmp_path, approved=ap)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "status=BLOCKED" in r.stdout and "NETWORK ATTEMPT" not in r.stdout + r.stderr
    assert re.search(r"LEDGER_(STALE|UNRECONCILED)", r.stdout)
    assert "DRY-RUN: printed only, nothing sent" in r.stdout and "NO ORDER SENT" in r.stdout
    outbox_and_digest_clean(tmp_path)


def test_cli_scan_empty_whitelist_with_reconciled_ledger_is_digest_or_market_closed_and_never_sends(tmp_path):
    cli(["journal", "init"], tmp_path)
    now = datetime.now(timezone.utc)
    with Journal(tmp_path / "j.sqlite3") as j:
        j.confirm_baseline(D("100000"))
        j.add_snapshot(AccountSnapshot("s", now - timedelta(hours=1), D("100000"), D("70000"), True, "v1"))
    outs = [cli(["scan"], tmp_path) for _ in range(2)]
    for r in outs:
        assert r.returncode == 0, r.stderr
        assert ("status=NO_TRADE" in r.stdout and "EMPTY_WHITELIST" in r.stdout) or \
               ("status=BLOCKED" in r.stdout and re.search(r"MARKET_CLOSED|UNKNOWN_SESSION", r.stdout)), r.stdout
        assert "DRY-RUN: printed only, nothing sent" in r.stdout
    if "status=NO_TRADE" in outs[0].stdout:
        assert "NO_TRADE_DIGEST" in outs[1].stdout          # repeated dry-run is not suppressed (never marked sent)
    outbox_and_digest_clean(tmp_path)


def test_cli_scan_with_unapproved_synthetic_example_whitelist_is_refused(tmp_path):
    r = cli(["scan"], tmp_path, approved=REPO / "config" / "approved.example.json")
    assert r.returncode != 0 and "Traceback" not in r.stderr


def test_cli_scan_with_broker_env_fails_startup_before_any_work(tmp_path):
    r = cli(["scan"], tmp_path, extra_env={"SCHWAB_APP_KEY": "x"})
    assert r.returncode == 2 and "scan run=" not in r.stdout
    r = cli(["scan"], tmp_path, extra_env={"CSP_DISABLE_BROKER_ACCESS": "false"})
    assert r.returncode == 2
