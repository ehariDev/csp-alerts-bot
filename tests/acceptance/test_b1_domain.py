"""Stage B1 acceptance, sections B (domain parity and money), C (state flows) and D (breaches, unsupported states).
Oracles derive from requirements/PRODUCT_WORKFLOW_REQUIREMENTS.md WT-03/04/05, ACCEPTANCE_CRITERIA WT-AT-03/04/07 and the
spec limits (policy.yaml: pilot 15,000; ticker 9,000; sector 10,000; 4 contracts; 1 per underlying; 2 company names;
expiry-week 15,000). Hand arithmetic is in the comments. Synthetic, loopback only.

Reference (see b1_env): baseline 100,000, snapshot equity 100,000 / NET free cash 60,000, OPEN AAA put 40 (collateral 4,000).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from urllib.parse import parse_qs, urlsplit

import pytest
from acc_helpers import policy
from b1_env import DASH_NOW, EXP_OTHER, B1, blockers, checkin, sell, text_of
from s2_helpers import FakeSource
from s3_helpers import FakeTelegram, chain_rows_for, run_cli
from stage_a_helpers import EXP, jopen

from csp_alerts.market.models import SessionInfo, SessionStatus

OPEN_S = SessionInfo(SessionStatus.OPEN, 300)
WORKING_F = {"action": "sell_working", "credit": "", "fees": "", "executed_date": "", "executed_time": ""}
CLOSE_OK = {"fees": "0", "executed_date": "2026-10-30", "executed_time": "10:00"}


@pytest.fixture()
def w(tmp_path):
    x = B1(tmp_path)
    yield x
    x.stop()


def previewable(html: str) -> bool:
    return "tok" in B1.hidden(html)


def pos(w, tid):
    r = w.sql("SELECT state, strike, actual_open_credit_per_share, actual_close_debit_per_share, fees_usd, opened_at_utc, "
              "closed_at_utc, sector, correlation_bucket, contracts, multiplier FROM option_positions WHERE trade_id=?", (tid,))
    return r[0] if r else None


def n_rows(w):
    return tuple(w.sql(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("option_positions", "audit_log", "account_snapshots"))


def tid_of(loc):
    return parse_qs(urlsplit(loc).query)["trade"][0]


def working(sym="BBB", strike="30"):
    return {**sell(sym, strike), **WORKING_F}


def monitor(w, chains):
    from csp_alerts.jobs.monitor import run_monitor
    j = jopen(w.db, DASH_NOW)
    try:
        return run_monitor(journal=j, policy=policy(), source=FakeSource(chains, now=DASH_NOW), now_utc=DASH_NOW, session=OPEN_S)
    finally:
        j.close()


# ======================================================================================== B: numbers by hand
def test_preview_matches_hand_computed_collateral_credit_fees_net_and_capacity(w):
    """BBB 30 put: collateral = 30 x 100 x 1 = 3,000. credit 0.45 x 100 = 45.00; fees 0.65; net = 45.00 - 0.65 = 44.35.
    Book now: puts 4,000 (1 contract, AAA). After: pilot 7,000 (room 15,000-7,000 = 8,000); normal ceiling 7,000 (room 18,000);
    ticker BBB 0+3,000 (room 6,000); sector S2 0+3,000 (room 7,000); expiry-week 4,000+3,000 (room 8,000);
    contracts 1+1 (room 2); BBB contracts 0+1 (room 0); names 1+1 (room 0); free cash 60,000-3,000 = 57,000."""
    before = n_rows(w)
    st, _, html = w.preview(sell("BBB", "30"))
    assert st == 200 and previewable(html) and n_rows(w) == before
    t = text_of(html)
    assert "Collateral reserved strike x multiplier x contracts = $3,000.00" in t
    assert "$0.45/share = $45.00 for 1 contract; fees $0.65; net premium after fees about $44.35" in t
    expect = [("Pilot put collateral", "$15,000.00", "$4,000.00", "$3,000.00", "$7,000.00", "$8,000.00"),
              ("Put collateral vs normal ceiling", "$25,000.00", "$4,000.00", "$3,000.00", "$7,000.00", "$18,000.00"),
              ("Ticker BBB (puts + shares)", "$9,000.00", "$0.00", "$3,000.00", "$3,000.00", "$6,000.00"),
              ("Sector (puts + shares)", "$10,000.00", "$0.00", "$3,000.00", "$3,000.00", "$7,000.00"),
              ("Expiration-week put collateral", "$15,000.00", "$4,000.00", "$3,000.00", "$7,000.00", "$8,000.00"),
              ("Active short-put contracts", "4", "1", "1", "2", "2"),
              ("Contracts on BBB", "1", "0", "1", "1", "0"),
              ("Individual-company names (puts + shares)", "2", "1", "1", "2", "0"),
              ("Free cash (latest NET snapshot)", "$60,000.00", "$0.00", "$3,000.00", "$3,000.00", "$57,000.00")]
    for row in expect:
        assert " ".join(row) in t, row


def _cli(mp, d, *args, apply=True, now=DASH_NOW):
    a = list(args) + (["--apply", "--reason", "parity"] if apply and args[1] != "confirm-baseline" else [])
    code, out, err = run_cli(mp, d, a, now=now, frozen_journal=True)
    assert code == 0, (args, out, err)
    return out


def test_web_record_equals_cli_add_trade_plus_fill_row_for_row(monkeypatch, tmp_path):
    """Identical inputs -> identical option_positions row and identical POSITION_* audit payloads; only actor/source/notes differ.
    CLI side: journal add-trade + journal fill, same trade id, fill time = the UTC instant of 09:30 CDT on 2026-10-30 = 14:30Z."""
    wd, cd = tmp_path / "w", tmp_path / "c"
    wd.mkdir(), cd.mkdir()
    x = B1(wd)
    try:
        _, loc, _ = x.do(sell("BBB", "30"))
        tid = tid_of(loc)
        wrow = x.sql("SELECT * FROM option_positions WHERE trade_id=?", (tid,))[0]
        cols = [r[1] for r in x.sql("PRAGMA table_info(option_positions)")]
        q = "SELECT event_type, after_json, actor, source FROM audit_log WHERE event_type LIKE 'POSITION%' AND after_json LIKE ? ORDER BY rowid"
        waud = x.sql(q, (f"%{tid}%",))
    finally:
        x.stop()
    _cli(monkeypatch, cd, "journal", "init", apply=False)
    _cli(monkeypatch, cd, "journal", "confirm-baseline", "--equity", "100000")
    _cli(monkeypatch, cd, "journal", "add-trade", "--trade-id", tid, "--ticker", "BBB", "--expiration", "2026-12-04", "--strike", "30",
         "--sector", "s2", "--bucket", "b2")
    _cli(monkeypatch, cd, "journal", "fill", "--trade-id", tid, "--credit", "0.45", "--fees", "0.65", "--time", "2026-10-30T14:30:00Z")
    c = sqlite3.connect(cd / "j.sqlite3")
    crow = c.execute("SELECT * FROM option_positions WHERE trade_id=?", (tid,)).fetchone()
    caud = c.execute(q, (f"%{tid}%",)).fetchall()
    c.close()
    diff = {k: (a, b) for k, a, b in zip(cols, wrow, crow) if a != b}
    assert set(diff) <= {"notes"}, diff
    assert [(a, b) for a, b, _, _ in waud] == [(a, b) for a, b, _, _ in caud]
    assert {r[2] for r in waud} == {"web:owner"} and {r[3] for r in waud} == {"web"}
    assert {r[2] for r in caud} == {"owner"} and {r[3] for r in caud} == {"cli"}


# ======================================================================================== B: money and field boundaries
CREDITS = [  # (raw, accepted). Accepted: digits[.1-4 digits], > 0, <= strike 30
    ("0.45", True), (" 0.45 ", True), ("0.01", True), ("0.4500", True), ("30", True), ("30.0001", False), ("29.9999", True),
    ("0", False), ("0.0", False), ("-0.45", False), ("+0.45", False), ("NaN", False), ("nan", False), ("Infinity", False),
    ("1e-1", False), ("0x10", False), ("0,45", False), ("0.45001", False), ("0.450001", False), ("", False), (".45", False),
    ("0. 45", False), ("9999999", False), ("99999999999999999999", False), ("0.45\n0.46", False)]


@pytest.mark.parametrize("raw,ok", CREDITS, ids=[repr(c[0])[:20] for c in CREDITS])
def test_credit_boundaries(w, raw, ok):
    before = n_rows(w)
    st, _, html = w.preview(sell("BBB", "30", credit=raw))
    assert st == 200 and previewable(html) is ok, text_of(html)[-500:]
    assert n_rows(w) == before                                   # a preview never writes


FEES = [("0", True), ("0.00", True), ("", False), ("0.65", True), (" 0.65 ", True), ("99999", True), ("99999.9999", True), ("100000", False),
        ("-1", False), ("-0.0", False), ("NaN", False), ("Infinity", False), ("0,65", False), ("0.00001", False), ("1e2", False),
        ("0.65.1", False)]


@pytest.mark.parametrize("raw,ok", FEES, ids=[repr(c[0])[:20] for c in FEES])
def test_fee_boundaries(w, raw, ok):
    _, _, html = w.preview(sell("BBB", "30", fees=raw))
    assert previewable(html) is ok, text_of(html)[-400:]


def test_exact_decimal_storage(w):
    _, loc, _ = w.do(sell("BBB", "30", credit="0.1", fees="0.30"))
    r = pos(w, tid_of(loc))
    assert D(r[2]) == D("0.1") and D(r[4]) == D("0.30")
    assert not re.search(r"[eE]|0000001|9999999", r[2] + r[4])     # no float artefacts


def test_stored_fill_is_the_owner_number_never_the_bid_and_form_has_no_defaults(w):
    """The stored bid is 0.50. The owner reports 0.45 -> stored 0.45; the form shows the bid only as a labelled reference."""
    form = w.get("/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled")[2]
    assert "Reference only (NOT your fill)" in text_of(form) and "stored bid" in text_of(form)
    for fld in ("credit", "executed_date", "executed_time"):
        tags = re.findall(rf'<input[^>]*name="{fld}"[^>]*>', form)
        assert tags and all('value="' not in i or 'value=""' in i for i in tags), fld
    _, loc, _ = w.do(sell("BBB", "30", credit="0.45"))
    assert D(pos(w, tid_of(loc))[2]) == D("0.45")


def test_fees_field_has_no_default(w):
    form = w.get("/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled")[2]
    tags = re.findall(r'<input[^>]*name="fees"[^>]*>', form)
    assert tags and all('value="' not in i or 'value=""' in i for i in tags), tags


def test_unicode_digits_are_not_accepted_as_numbers(w):
    assert not previewable(w.preview(sell("BBB", "30", credit="٠.45"))[2])
    assert not previewable(w.preview(sell("BBB", "30", fees="٠.65"))[2])


def test_contract_must_exist_in_stored_chain_and_cannot_be_typed(w):
    for strike in ("31", "29.99", "100"):
        assert not previewable(w.preview(sell("BBB", strike))[2])
    assert not previewable(w.preview(sell("BBB", "30", expiry="2026-12-11"))[2])
    assert not previewable(w.preview(sell("QQQ", "30"))[2])


# ======================================================================================== B: timestamps
def test_future_execution_refused_with_five_minute_tolerance(w):
    """Dashboard clock = 2026-10-30T15:05:32Z = 10:05:32 CDT. +5 min = 10:10:32. 10:10 CDT (15:10Z, 4m28s ahead) allowed;
    10:11 CDT (15:11Z, 5m28s ahead) refused; 10:06 allowed."""
    for hhmm, ok in (("10:06", True), ("10:10", True), ("10:11", False), ("23:59", False)):
        assert previewable(w.preview(sell("BBB", "30", executed_time=hhmm))[2]) is ok, hhmm
    html = w.preview(sell("BBB", "30", executed_date="2026-10-31", executed_time="00:00"))[2]
    assert not previewable(html) and "future" in text_of(html)


@pytest.mark.parametrize("d,t", [("2026-10-30", ""), ("", "09:30"), ("2026-10-30T09:30", "09:30"), ("2026-13-01", "09:30"),
                                 ("2026-02-30", "09:30"), ("2026-10-30", "9:30"), ("2026-10-30", "24:00"), ("2026-10-30", "09:60"),
                                 ("2026-10-30", "09:30:00"), ("30/10/2026", "09:30"), ("2026-10-30", "09:30Z")])
def test_naive_or_malformed_timestamps_refused(w, d, t):
    assert not previewable(w.preview(sell("BBB", "30", executed_date=d, executed_time=t))[2])


def test_central_time_is_converted_to_utc_including_across_dst(tmp_path):
    """Chicago: CDT = UTC-5 until Sun 2026-11-01 02:00 local, then CST = UTC-6.
    2026-10-31 09:30 CDT -> 14:30Z; 2026-11-01 09:30 CST -> 15:30Z. (Dashboard clock = Tue 2026-11-03T20:00Z.)"""
    x = B1(tmp_path, now=datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc))
    try:
        for sym, strike, d, utc in (("AAA", "35", "2026-10-31", "2026-10-31T14:30:00Z"), ("EEE", "35", "2026-11-01", "2026-11-01T15:30:00Z")):
            _, loc, _ = x.do(sell(sym, strike, executed_date=d, executed_time="09:30"), acks=["ack_breach"])
            assert x.result_code(loc) == "recorded", loc
            assert pos(x, tid_of(loc))[5] == utc
    finally:
        x.stop()


def test_pre_market_execution_time_behaviour_is_documented(w):
    """OBSERVATION (spec silent on pre-open fills): 08:00 CDT = 09:00 EDT is before the 09:30 ET open."""
    html = w.preview(sell("BBB", "30", executed_time="08:00"))[2]
    print("PRE-MARKET fill time accepted:", previewable(html))


def test_expiry_in_past_or_unstored_refused_future_stored_ok(w):
    for e in ("2026-10-29", "2025-01-01", "2026-10-30", "2099-01-01"):
        assert not previewable(w.preview(sell("BBB", "30", expiry=e))[2]), e
    assert previewable(w.preview(sell("BBB", "30", expiry="2026-12-04"))[2])


@pytest.mark.parametrize("q,ok", [("1", True), (" 1 ", True), ("", True), ("2", False), ("0", False), ("-1", False), ("1.0", False),
                                  ("01", False), ("10", False), ("one", False)])
def test_quantity_must_be_one_with_cli_message(w, q, ok):
    html = w.preview(sell("BBB", "30", contracts=q))[2]
    assert previewable(html) is ok
    if not ok:
        t = text_of(html)
        assert "exactly 1 contract" in t and "documented CLI procedure" in t


def test_account_scope_is_paper_only_and_identity_fields_cannot_be_injected(w):
    _, loc, _ = w.do(sell("BBB", "30"))
    assert w.sql("SELECT account_scope, paper, multiplier FROM option_positions WHERE trade_id=?", (tid_of(loc),)) == [("paper_primary", 1, 100)]
    n = n_rows(w)
    w.preview(sell("BBB", "35", account_scope="live_primary", paper="0", multiplier="10"))
    assert n_rows(w) == n


# ======================================================================================== C: state flows
def test_sell_filled_becomes_open_and_monitor_evaluates_it_without_any_cli_step(w):
    """credit 0.45; ask 0.18 -> (0.45 - 0.18) / 0.45 = 0.27 / 0.45 = 0.60 exactly = profit threshold -> PROFIT_EXIT."""
    _, loc, _ = w.do(sell("BBB", "30"))
    tid = tid_of(loc)
    assert pos(w, tid)[0] == "OPEN"
    res = monitor(w, {"BBB": chain_rows_for(30.0, EXP, 0.15, 0.18, now=DASH_NOW), "AAA": chain_rows_for(40.0, EXP, 0.4, 0.5, now=DASH_NOW)})
    assert res.positions_checked == 2
    assert any(p.trade_id == tid and s.trigger.value == "PROFIT_EXIT" for p, s in res.signals)
    assert any("BBB" in m.text and "NO ORDER SENT" in m.text for m in res.messages if m.kind == "PROFIT_EXIT")


def test_sell_working_reserves_collateral_then_mark_filled_opens(w):
    _, loc, _ = w.do(working())
    tid = tid_of(loc)
    r = pos(w, tid)
    assert r[0] == "WORKING" and r[2] is None                    # no premium recorded for an unfilled order
    assert tid in text_of(w.get("/positions?group=reserved")[2])
    # reserved collateral counts in capacity: CCC 35 (3,500): pilot used now 4,000 + 3,000 = 7,000 -> after 10,500 -> room 4,500
    assert "Pilot put collateral $15,000.00 $7,000.00 $3,500.00 $10,500.00 $4,500.00" in text_of(w.preview(sell("CCC", "35"))[2])
    assert monitor(w, {"AAA": chain_rows_for(40.0, EXP, 0.4, 0.5, now=DASH_NOW)}).positions_checked == 1   # WORKING is not monitored as open
    _, loc2, _ = w.do({"action": "mark_filled", "trade_id": tid, "credit": "0.52", "fees": "0.70", "executed_date": "2026-10-30",
                       "executed_time": "09:45"})
    assert w.result_code(loc2) == "recorded"
    r = pos(w, tid)
    assert r[0] == "OPEN" and D(r[2]) == D("0.52") and D(r[4]) == D("0.70") and r[5] == "2026-10-30T14:45:00Z"


def test_close_order_then_closing_fill_closes_with_debit_and_fees(w):
    """OPN: credit 0.50, open fees 0.65. Close debit 0.20, close fees 0.65 -> total fees 1.30;
    P&L = (0.50 - 0.20) x 100 x 1 - 0.65 - 0.65 = 30.00 - 1.30 = 28.70."""
    _, loc, _ = w.do({"action": "close_order", "trade_id": "OPN"})
    assert w.result_code(loc) == "recorded" and pos(w, "OPN")[0] == "CLOSE_WORKING"
    assert "OPN" in text_of(w.get("/positions?group=live")[2])                       # still shown as live/reserved
    assert monitor(w, {"AAA": chain_rows_for(40.0, EXP, 0.4, 0.5, now=DASH_NOW)}).positions_checked == 1   # still monitored
    f = {"action": "close_filled", "trade_id": "OPN", "debit": "0.20", "fees": "0.65", "executed_date": "2026-10-30", "executed_time": "10:00"}
    assert "28.70" in text_of(w.preview(f)[2])
    w.do(f)
    r = pos(w, "OPN")
    assert r[0] == "CLOSED" and D(r[3]) == D("0.20") and D(r[4]) == D("1.30") and r[6] == "2026-10-30T15:00:00Z"
    assert "Pilot put collateral $15,000.00 $0.00 $3,000.00" in text_of(w.preview(sell("BBB", "30"))[2])   # collateral released


def test_close_directly_from_open_records_both_transitions(w):
    w.do({"action": "close_filled", "trade_id": "OPN", "debit": "0.20", **CLOSE_OK})
    assert pos(w, "OPN")[0] == "CLOSED"
    seq = [r[0] for r in w.sql("SELECT after_json FROM audit_log WHERE event_type='POSITION_TRANSITION' AND after_json LIKE '%\"OPN\"%' ORDER BY rowid")]
    assert any('"CLOSE_WORKING"' in x for x in seq) and any('"CLOSED"' in x for x in seq)


def test_cancel_open_order_requires_ack_and_releases_collateral(w):
    _, loc, _ = w.do(working())
    tid = tid_of(loc)
    html = w.preview({"action": "cancel_open_order", "trade_id": tid})[2]
    assert "ack_verified" in html
    w.post("/trade/confirm", w.confirm_fields(html, acks=[]))
    assert pos(w, tid)[0] == "WORKING"
    w.do({"action": "cancel_open_order", "trade_id": tid})
    assert pos(w, tid)[0] == "CANCELED"
    assert "Pilot put collateral $15,000.00 $4,000.00" in text_of(w.preview(sell("BBB", "30"))[2])


def test_cancel_close_order_returns_to_open(w):
    w.do({"action": "close_order", "trade_id": "OPN"})
    _, loc, _ = w.do({"action": "cancel_close_order", "trade_id": "OPN"})
    assert w.result_code(loc) == "recorded" and pos(w, "OPN")[0] == "OPEN"


def test_expire_date_rules_before_and_after_expiration(tmp_path):
    """Put expiring 2026-11-20. Clock 2026-10-30: a record dated on/after the expiry is in the future -> refused (and 'today' is
    before the expiration -> ledger refuses). Clock 2026-11-23: 2026-11-19 (before expiration) refused; 2026-11-20 accepted
    with the 'no late exercise' ack; without the ack nothing is written."""
    ex = [dict(tid="EXPR", ticker="CCC", strike="25", exp=EXP_OTHER, sector="S2", bucket="B3")]
    x = B1(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, extras=ex)
    try:
        assert not previewable(x.preview({"action": "expire", "trade_id": "EXPR"})[2])
        html = x.preview({"action": "expire", "trade_id": "EXPR", "expiry_date": "2026-11-20"})[2]
        assert not previewable(html) and "future" in text_of(html)
    finally:
        x.stop()
    x = B1(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, extras=ex, now=datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc))
    try:
        assert not previewable(x.preview({"action": "expire", "trade_id": "EXPR", "expiry_date": "2026-11-19"})[2])
        html = x.preview({"action": "expire", "trade_id": "EXPR", "expiry_date": "2026-11-20"})[2]
        assert previewable(html) and "ack_expired" in html
        loc = x.post("/trade/confirm", x.confirm_fields(html, acks=[]))[1]["location"]
        assert x.result_code(loc) == "ack_required" and pos(x, "EXPR")[0] == "OPEN"
        _, loc, _ = x.do({"action": "expire", "trade_id": "EXPR", "expiry_date": "2026-11-20"})
        assert pos(x, "EXPR")[0] == "EXPIRED"
    finally:
        x.stop()


@pytest.mark.parametrize("state,actions", [
    ("OPEN", ["mark_filled", "cancel_open_order", "cancel_close_order"]),
    ("WORKING", ["close_order", "close_filled", "cancel_close_order", "expire"]),
    ("CLOSE_WORKING", ["mark_filled", "close_order", "cancel_open_order"]),
])
def test_illegal_transitions_are_rejected(tmp_path, state, actions):
    x = B1(tmp_path, extras=[dict(tid="X1", ticker="CCC", strike="25", exp=EXP_OTHER, state=state, sector="S2", bucket="B3")])
    try:
        base = {"trade_id": "X1", "credit": "0.4", "debit": "0.1", "fees": "0", "executed_date": "2026-10-30", "executed_time": "09:30"}
        for a in actions:
            before = n_rows(x)
            assert not previewable(x.preview({**base, "action": a})[2]), a
            assert n_rows(x) == before
        assert pos(x, "X1")[0] == state
    finally:
        x.stop()


def test_closed_is_terminal(w):
    w.do({"action": "close_filled", "trade_id": "OPN", "debit": "0.2", **CLOSE_OK})
    for a in ("close_order", "close_filled", "mark_filled", "cancel_open_order", "cancel_close_order", "expire"):
        html = w.preview({"action": a, "trade_id": "OPN", "credit": "0.4", "debit": "0.1", "fees": "0",
                          "executed_date": "2026-10-30", "executed_time": "09:30"})[2]
        assert not previewable(html), a


def test_closed_position_queued_advice_is_dropped_at_send_but_open_control_still_sends(w):
    """Queue PROFIT_EXIT for OPN and for a new BBB put; close only OPN on the web; flush through a fake Telegram: the OPN advice is
    dropped (never sent), the BBB advice (a valid OPEN control) is sent. Profit rule: BBB credit 0.50, ask 0.10 -> 80% >= 60%."""
    from csp_alerts import runner
    from csp_alerts.alerts import outbox
    from csp_alerts.alerts.sendmode import Mode, SendDecision
    from csp_alerts.policy.approved import load_approved
    from s3_helpers import CHAT, TOKEN
    _, loc, _ = w.do(sell("BBB", "30", credit="0.50"))
    bbb = tid_of(loc)
    pol = policy()
    res = monitor(w, {"AAA": chain_rows_for(40.0, EXP, 0.05, 0.10, now=DASH_NOW), "BBB": chain_rows_for(30.0, EXP, 0.05, 0.10, now=DASH_NOW)})
    kinds = {(m.meta or {}).get("trade_id"): m for m in res.messages if m.kind == "PROFIT_EXIT"}
    assert {"OPN", bbb} <= set(kinds), [(m.kind, m.meta) for m in res.messages]
    j = jopen(w.db, DASH_NOW)
    for t_id, m in kinds.items():
        reval = {"trade_id": t_id, "trigger": m.meta.get("trigger"), "state": m.meta.get("state"), "fingerprint": m.meta.get("fingerprint"),
                 "evidence_at_utc": DASH_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}
        outbox.enqueue(j, m, SendDecision(Mode.SEND), now=DASH_NOW, scope=pol.account_scope, policy_sha=pol.sha256, revalidate=reval)
    j.close()
    w.do({"action": "close_filled", "trade_id": "OPN", "debit": "0.10", **CLOSE_OK})
    ft = FakeTelegram()
    try:
        j = jopen(w.db, DASH_NOW)
        rep = outbox.flush(j, ft.transport(), now=DASH_NOW, revalidator=runner.make_revalidator(j, pol, lambda: load_approved(w.appr), lambda: DASH_NOW),
                           chat_check=lambda: None, secrets=(TOKEN, CHAT))
        sent_text = " ".join(r["body"].get("text", "") for r in ft.sends)
        j.close()
    finally:
        ft.close()
    assert rep.dropped >= 1 and rep.sent >= 1
    assert "AAA" not in sent_text and "BBB" in sent_text, sent_text


def test_entries_blocked_after_record_until_new_checkin_and_page_says_so(w):
    assert "CHANGE_AFTER_SNAPSHOT" not in blockers(w)
    _, loc, _ = w.do(sell("BBB", "30"))
    assert "CHANGE_AFTER_SNAPSHOT" in blockers(w)
    t = text_of(w.get(f"/trade/result?code=recorded&trade={tid_of(loc)}&a=sell_filled")[2])
    assert "RECONCILIATION REQUIRED" in t and "LEDGER_UNRECONCILED" in t and "check-in" in t.lower()
    checkin(w, "100000", "93000")                               # NET cash: 100,000 - (4,000 + 3,000) = 93,000
    assert "CHANGE_AFTER_SNAPSHOT" not in blockers(w)


# ======================================================================================== F: check-in cash rule
def test_checkin_cash_rule_matches_cli(w, monkeypatch, tmp_path):
    """committed = 4,000 (OPN). cash + 4,000 <= equity: equity 100,000 -> cash <= 96,000 (boundary). 96,000.01 -> CASH_INCONSISTENT."""
    for cash, ok in (("96000", True), ("96000.01", False), ("96000.001", False), ("0", True), ("-1", False)):
        html = w.preview({"action": "checkin", "equity": "100000", "free_cash": cash, "nlots": "0"}, "/checkin/preview")[2]
        assert previewable(html) is ok, cash
        if cash == "96000.01":
            assert "CASH_INCONSISTENT" in text_of(html)
    cd = tmp_path / "cli"
    cd.mkdir()
    _cli(monkeypatch, cd, "journal", "init", apply=False)
    _cli(monkeypatch, cd, "journal", "confirm-baseline", "--equity", "100000")
    _cli(monkeypatch, cd, "journal", "add-trade", "--trade-id", "P1", "--ticker", "AAA", "--expiration", "2026-12-04", "--strike", "40",
         "--sector", "s", "--bucket", "b")
    _cli(monkeypatch, cd, "journal", "fill", "--trade-id", "P1", "--credit", "0.5", "--time", "2026-10-30T14:00:00Z")
    code, out, err = run_cli(monkeypatch, cd, ["daily-check-in", "--equity", "100000", "--free-cash", "96000.01", "--ack-reconciled",
                                               "--apply", "--reason", "x"], now=DASH_NOW, frozen_journal=True)
    assert "CASH_INCONSISTENT" in out + err and code != 0
    code, out, err = run_cli(monkeypatch, cd, ["daily-check-in", "--equity", "100000", "--free-cash", "96000", "--ack-reconciled",
                                               "--apply", "--reason", "x"], now=DASH_NOW, frozen_journal=True)
    assert code == 0


def test_checkin_lists_existing_records_requires_ack_and_has_no_baseline_controls(w):
    t = text_of(w.get("/checkin")[2])
    assert "OPN" in t and "AAA" in t and "4,000" in t
    assert "baseline" in t.lower() and "CLI" in t
    for path in ("/checkin/baseline", "/checkin/cash-flow", "/checkin/reset", "/baseline", "/cash-flow", "/risk-reset"):
        assert w.get(path)[0] == 404
    for pth in ("/baseline", "/cash-flow", "/risk-reset", "/approve"):
        assert w.post(pth, {"equity": "1"})[0] in (404, 405)
    html = w.preview({"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"}, "/checkin/preview")[2]
    before = n_rows(w)
    loc = w.post("/checkin/confirm", w.confirm_fields(html, acks=[]))[1]["location"]
    assert w.result_code(loc) == "ack_required" and n_rows(w) == before


# ======================================================================================== D: breaches and unsupported states
BIG = dict(tid="BIG", ticker="EEE", strike="90", exp=EXP_OTHER, sector="S3", bucket="B4")
NOT_PILOT = {"PILOT_COLLATERAL_LIMIT"}
BREACH_CASES = [
    # id, extras, sell fields, codes that MUST be flagged, codes that must NOT be flagged.
    # (B2-1 / WT-05: asset type comes from the ONE resolver journal > approvals file > UNKNOWN. EEE is an approved ETF so it is NOT an
    # individual company; unknown labels would count conservatively as a company. The old exact-limit row moved to its own test below.)
    ("pilot", [BIG], sell("BBB", "30"), {"PILOT_COLLATERAL_LIMIT"}, {"SECTOR_LIMIT", "EXPIRY_WEEK_LIMIT", "SINGLE_TICKER_LIMIT"}),   # 4,000+9,000+3,000 = 16,000 > 15,000
    ("pilot_one_over", [BIG], sell("BBB", "21"), {"PILOT_COLLATERAL_LIMIT"}, set()),            # 13,000+2,100 = 15,100 > 15,000
    ("sector", [dict(tid="S2X", ticker="CCC", strike="80", exp=EXP_OTHER, sector="S2", bucket="B3")], sell("BBB", "30"),
     {"SECTOR_LIMIT"}, NOT_PILOT),                                                              # S2: 8,000+3,000 = 11,000 > 10,000; pilot 4,000+8,000+3,000 = 15,000 ok
    ("expiry_week", [dict(tid="W1", ticker="EEE", strike="90", exp=EXP, sector="S3", bucket="B4")], sell("BBB", "21"),
     {"EXPIRY_WEEK_LIMIT"}, set()),                                                             # week: 4,000+9,000+2,100 = 15,100
    ("one_per_underlying", [], sell("AAA", "30"), {"CONTRACT_COUNT_LIMIT"}, set()),            # AAA already has 1 contract
    ("company_names", [dict(tid="N1", ticker="CCC", strike="5", exp=EXP_OTHER, sector="S2", bucket="B3")], sell("BBB", "30"),
     {"INDIVIDUAL_NAME_LIMIT"}, set()),                                                         # AAA, CCC + BBB = 3 names > 2
    ("four_contracts", [dict(tid=f"C{i}", ticker=f"C{i}X", strike="5", exp=EXP_OTHER, sector=f"s{i}", bucket=f"b{i}") for i in range(4)],
     sell("BBB", "30"), {"CONTRACT_COUNT_LIMIT"}, set()),                                       # 1 + 4 + 1 = 6 > 4
    ("unapproved", [], sell("ZZZ", "30", sector="SX", bucket="BX"), {"NOT_APPROVED"}, {"LABELS_UNKNOWN"}),
    ("unknown_labels", [], sell("ZZZ", "30"), {"LABELS_UNKNOWN", "NOT_APPROVED"}, set()),
]


def test_pilot_collateral_exactly_at_limit_with_etf_not_counted_as_company_flags_nothing(tmp_path):
    """Replaces the old parametrised row 'pilot_exact_limit_ok' (B2-1, WT-05: ETFs are not individual companies).
    Book: OPN AAA 40 = 4,000 (S1) + BIG EEE 90 = 9,000 (ETF, S3) + new BBB 20 = 2,000 (S2): 4,000+9,000+2,000 = 15,000 = pilot
    limit (allowed, '<='). Company names: AAA, BBB = 2 <= max 2 (EEE is an approved ETF, not counted). Sector S1/S3/S2 each below
    limits, one contract per ticker, expiries differ for BIG. Therefore NO breach: no BREACHES line, records without an ack.
    Previously this row flagged INDIVIDUAL_NAME_LIMIT only because the file-approved ETF was mis-counted as a 3rd company."""
    x = B1(tmp_path, extras=[BIG])
    try:
        fields = sell("BBB", "20")
        html = x.preview(fields)[2]
        assert previewable(html), text_of(html)[-600:]
        t = text_of(html)
        assert "BREACHES policy" not in t and "ack_breach" not in html, t[-600:]
        _, loc, _ = x.do(fields)
        assert x.result_code(loc) == "recorded"
        assert x.sql("SELECT COUNT(*) FROM audit_log WHERE event_type='POLICY_BREACH_RECORDED'")[0][0] == 0
        # one dollar over (strike 21 -> 2,100): 15,100 > 15,000 still flags ONLY the pilot cap (names stay 2)
    finally:
        x.stop()


@pytest.mark.parametrize("name,extras,fields,codes,never", BREACH_CASES, ids=[c[0] for c in BREACH_CASES])
def test_owner_reported_breach_is_previewed_recorded_flagged_and_blocks_entries(tmp_path, name, extras, fields, codes, never):
    x = B1(tmp_path, extras=extras)
    try:
        html = x.preview(fields)[2]
        assert previewable(html), text_of(html)[-600:]
        t = text_of(html)
        m = re.search(r"BREACHES policy \(([A-Z_, ]+)\)", t)
        got = set(m.group(1).replace(" ", "").split(",")) if m else set()
        assert codes <= got and not (never & got), (got, codes, never)
        for c in codes:
            assert c in t                                       # every failing rule is listed on the preview
        assert got, "this scenario must flag something"
        assert "ack_breach" in html
        before = n_rows(x)
        loc = x.post("/trade/confirm", x.confirm_fields(html, acks=[]))[1]["location"]
        assert x.result_code(loc) == "ack_required" and n_rows(x) == before        # needs the explicit acknowledgement
        _, loc, _ = x.do(fields)
        assert x.result_code(loc) == "recorded"                 # reality is recorded, not rejected
        tid = tid_of(loc)
        assert pos(x, tid)[0] == "OPEN"
        ev = x.sql("SELECT after_json FROM audit_log WHERE event_type='POLICY_BREACH_RECORDED'")
        assert len(ev) == 1 and all(c in ev[0][0] for c in codes)
        checkin(x)                                              # blocked even after a fresh, consistent check-in
        assert "POLICY_BREACH_UNRESOLVED" in blockers(x)
        pt = text_of(x.get("/positions")[2])
        assert tid in pt and "BREACH" in pt.upper()
        x.do({"action": "close_filled", "trade_id": tid, "debit": "0.10", **CLOSE_OK})
        checkin(x)
        assert "POLICY_BREACH_UNRESOLVED" not in blockers(x)    # clears when it stops reserving collateral
    finally:
        x.stop()


def test_working_order_that_breaches_is_flagged_and_cancel_clears(tmp_path):
    x = B1(tmp_path, extras=[BIG])
    try:
        _, loc, _ = x.do(working())
        tid = tid_of(loc)
        assert pos(x, tid)[0] == "WORKING"
        assert len(x.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_RECORDED'")) == 1
        checkin(x)
        assert "POLICY_BREACH_UNRESOLVED" in blockers(x)
        x.do({"action": "cancel_open_order", "trade_id": tid})
        checkin(x)
        assert "POLICY_BREACH_UNRESOLVED" not in blockers(x)
    finally:
        x.stop()


def test_mark_filled_of_a_flagged_working_order_keeps_the_flag(tmp_path):
    x = B1(tmp_path, extras=[BIG])
    try:
        _, loc, _ = x.do(working())
        tid = tid_of(loc)
        x.do({"action": "mark_filled", "trade_id": tid, "credit": "0.45", "fees": "0", "executed_date": "2026-10-30", "executed_time": "09:45"})
        assert pos(x, tid)[0] == "OPEN"
        checkin(x)
        assert "POLICY_BREACH_UNRESOLVED" in blockers(x)
    finally:
        x.stop()


@pytest.mark.parametrize("state,contracts,block", [("ASSIGNED_PENDING_RECONCILIATION", 1, "ASSIGNMENT_PENDING"),
                                                   ("ADJUSTMENT_REVIEW", 1, "ADJUSTMENT_REVIEW_PENDING"),
                                                   ("OPEN", 2, "MULTI_CONTRACT_POSITION")])
def test_unsupported_states_show_cli_message_no_form_remain_visible_and_block(tmp_path, state, contracts, block):
    x = B1(tmp_path, extras=[dict(tid="U1", ticker="CCC", strike="25", exp=EXP_OTHER, state=state, contracts=contracts, sector="S2", bucket="B3")])
    try:
        assert "U1" in text_of(x.get("/positions?group=all")[2])
        for slug in ("close-order", "close-fill", "cancel", "cancel-close", "expire", "mark-filled"):
            html = x.get(f"/trade/{slug}/U1")[2]
            assert "documented CLI procedure" in text_of(html), slug
            assert "/trade/preview" not in html and 'name="credit"' not in html and 'name="debit"' not in html, slug
        for a in ("close_order", "close_filled", "cancel_open_order", "cancel_close_order", "expire", "mark_filled"):
            before = n_rows(x)
            html = x.preview({"action": a, "trade_id": "U1", "debit": "0.1", "credit": "0.4", "fees": "0",
                              "executed_date": "2026-10-30", "executed_time": "09:30"})[2]
            assert not previewable(html) and n_rows(x) == before
        assert pos(x, "U1")[0] == state
        assert block in blockers(x)
    finally:
        x.stop()


# ======================================================================================== D: judgement of the open decisions (observations)
def test_contract_level_codes_are_information_only_not_flagged_not_recorded(tmp_path):
    """Open decision (1). A stored quote with delta -0.40 (limit 0.15) breaks a contract gate. Observed: shown as information, no
    acknowledgement, no POLICY_BREACH_RECORDED, entries NOT blocked after a check-in, and the code is not persisted anywhere
    (neither audit nor positions page) -> the off-strategy trade becomes invisible after the preview. See report recommendation."""
    x = B1(tmp_path, chain_over={"CCC": {"delta": -0.40}})
    try:
        html = x.preview(sell("CCC", "35"))[2]
        t = text_of(html)
        assert "tok" in B1.hidden(html) and "ack_breach" not in html
        assert re.search(r"contract.level|information", t, re.I), t[-1500:]
        _, loc, _ = x.do(sell("CCC", "35"))
        assert x.result_code(loc) == "recorded"
        assert x.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_RECORDED'") == []
        stored = " ".join(r[0] or "" for r in x.sql("SELECT after_json FROM audit_log WHERE after_json LIKE '%CCC%'"))
        persisted = bool(re.search(r"DELTA|DTE_|SPREAD", stored))
        checkin(x)
        assert "POLICY_BREACH_UNRESOLVED" not in blockers(x)
        print("contract-level codes persisted in audit:", persisted)
    finally:
        x.stop()


def test_policy_breach_flag_survives_later_owner_approval_current_behaviour(tmp_path):
    """Open decision (2). Observed: after the owner approves the previously unapproved ticker (with labels), the position stays
    flagged and entries stay blocked until it stops reserving collateral; there is no review action. Safe (fail closed) but
    over-blocking; see report recommendation."""
    from b1_env import APPROVED, write_approved_custom
    x = B1(tmp_path)
    try:
        _, loc, _ = x.do(sell("ZZZ", "30"))
        checkin(x)
        assert "POLICY_BREACH_UNRESOLVED" in blockers(x)
        write_approved_custom(x.appr, {**APPROVED, "ZZZ": ("STOCK", "S5", "B5", "100.00")})
        checkin(x)
        assert "POLICY_BREACH_UNRESOLVED" in blockers(x)
    finally:
        x.stop()


def _add_lot(x, ticker="BBB", shares="100", price="60.00"):
    from csp_alerts.journal.models import StockLot
    from stage_a_helpers import jopen
    from acc_helpers import NOW
    j = jopen(x.db, NOW - timedelta(hours=1))
    j.add_stock_lot(StockLot("LOT1", ticker, D(shares), D(price), NOW - timedelta(hours=1), "sec_l", "bk_l", "OWNED", "STOCK", None, None))
    j.close()


@pytest.mark.parametrize("strike,flagged", [("30", False), ("35", True)])
def test_per_ticker_cap_counts_owned_shares_at_the_9000_boundary(tmp_path, strike, flagged):
    """Shares 100 x 60.00 = 6,000 + put strike x 100: strike 30 -> 9,000 = limit (not a breach); strike 35 -> 9,500 > 9,000 (breach)."""
    x = B1(tmp_path)
    try:
        _add_lot(x)
        html = x.preview(sell("BBB", strike))[2]
        t = text_of(html)
        assert "tok" in B1.hidden(html)
        assert ("SINGLE_TICKER_LIMIT" in (re.search(r"BREACHES policy \(([A-Z_, ]+)\)", t) or [None, ""])[1]) is flagged, t[-1200:]
        if strike == "30":
            assert "Ticker BBB (puts + shares) $9,000.00 $6,000.00 $3,000.00 $9,000.00 $0.00 within" in t
        else:
            assert "Ticker BBB (puts + shares) $9,000.00 $6,000.00 $3,500.00 $9,500.00 -$500.00" in t
    finally:
        x.stop()
