"""Stage A acceptance: WT-01 watchlist (validation, limits, dedupe, persistence, tracked != approved, holdings stay tracked).
Synthetic; no network. Clock NOW = Fri 2026-10-30T15:05:02Z."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest
from acc_helpers import NOW
from s2_helpers import FakeSource, raw_row
from stage_a_env import approved_file, dash, mkt, plain_db, text_of
from stage_a_helpers import Prov, add_wl, csrf_of, hold, jopen, md_dump, refresh
from web_helpers import build_book

from csp_alerts.journal import watchlist as wl
from csp_alerts.journal import webwrites

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def symbols(db):
    c = sqlite3.connect(db)
    r = [x[0] for x in c.execute("SELECT symbol FROM watchlist ORDER BY 1")]
    c.close()
    return r


def audit_types(db):
    c = sqlite3.connect(db)
    r = [x[0] for x in c.execute("SELECT event_type FROM audit_log WHERE event_type LIKE 'WATCHLIST%' ORDER BY rowid")]
    c.close()
    return r


# ------------------------------------------------------------------------------------------ validation
@pytest.mark.parametrize("raw,stored", [("ZZZ", "ZZZ"), ("  zzz  ", "ZZZ"), ("brk.b", "BRK.B"), ("BRK-B", "BRK-B"), ("A", "A"),
                                        ("ABCDEFGHIJ", "ABCDEFGHIJ")])             # 10 chars is the maximum
def test_valid_symbols_are_accepted_and_canonicalised(tmp_path, raw, stored):
    db = plain_db(tmp_path)
    r = webwrites.add_watchlist_symbol(db, raw, clock=lambda: NOW)
    assert (r.code, r.symbol) == ("added", stored) and symbols(db) == [stored]


HOSTILE = [
    "", " ", "1ABC", "-AB", ".AB", "AB CD", "ABCDEFGHIJK",                                  # format / 11 chars
    "A" * 64, "A" * 5000, "SPY;DROP TABLE watchlist", "SPY'--", "SPY\" OR 1=1",
    "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "../../etc/passwd", "C:\\Windows", "SPY\x00",
    "SPY\x00ZZZ", "SPY\nQQQ", "SPY\rQQQ", "SPY\tQQQ", "SP\u200bY", "\u202eYPS",                 # NUL, newlines, zero-width, RTL override
    "\uff33\uff30\uff39",                                                                    # fullwidth SPY
    "\u0410APL",                                                                             # Cyrillic A
    "\u212aO",                                                                               # Kelvin sign
    "\u017fpy",                                                                              # long s: str.upper() -> "S"
    "\u0131bm",                                                                              # dotless i: str.upper() -> "I"
    "\u1d00AA", "\u2160", "\U0001f600", "SPY\u0301",
    None, 5, b"SPY", ["SPY"],
]


PARAMS = [pytest.param(h, id=repr(h)[:24]) for h in HOSTILE]


@pytest.mark.parametrize("raw", PARAMS)
def test_hostile_and_lookalike_symbols_are_rejected_and_nothing_is_written(tmp_path, raw):
    """Requirement WT-01: validate symbols. Expected: ASCII ticker grammar only. Anything non-ASCII (including characters
    that str.upper() would fold into ASCII) is rejected; nothing stored, nothing audited."""
    db = plain_db(tmp_path)
    before = hold(db)
    r = webwrites.add_watchlist_symbol(db, raw, clock=lambda: NOW)
    assert r.code == "invalid", (raw, r)
    assert symbols(db) == [] and hold(db) == before
    msg = r.message
    if isinstance(raw, str) and raw.strip():
        assert raw[:8] not in msg                                  # messages never echo raw input


def test_trailing_newline_only_is_trimmed_not_a_second_symbol(tmp_path):
    db = plain_db(tmp_path)
    assert webwrites.add_watchlist_symbol(db, "ZZZ\n", clock=lambda: NOW).code == "added"
    assert symbols(db) == ["ZZZ"]


def test_max_12_dedupe_and_idempotent_audit(tmp_path):
    db = plain_db(tmp_path)
    names = [f"S{chr(65 + i)}" for i in range(12)]
    for n in names:
        assert webwrites.add_watchlist_symbol(db, n, clock=lambda: NOW).code == "added"
    assert webwrites.add_watchlist_symbol(db, "SZ", clock=lambda: NOW).code == "full" and len(symbols(db)) == 12
    assert webwrites.add_watchlist_symbol(db, "sa", clock=lambda: NOW).code == "already"      # dedupe, case-insensitive, even when full
    assert audit_types(db) == ["WATCHLIST_ADDED"] * 12                                         # the duplicate left no second event
    assert webwrites.remove_watchlist_symbol(db, "SA", clock=lambda: NOW).code == "removed"
    assert webwrites.remove_watchlist_symbol(db, "SA", clock=lambda: NOW).code == "not_tracked"
    assert audit_types(db).count("WATCHLIST_REMOVED") == 1
    assert webwrites.add_watchlist_symbol(db, "SZ", clock=lambda: NOW).code == "added"        # room again


def test_web_write_never_creates_or_migrates_a_journal(tmp_path):
    missing = tmp_path / "nope.sqlite3"
    assert webwrites.add_watchlist_symbol(missing, "ZZZ").code == "unavailable" and not missing.exists()
    # a journal older than 0005
    db = plain_db(tmp_path)
    c = sqlite3.connect(db)
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if t.startswith("md_") or t == "watchlist":
            c.execute(f"DROP TABLE {t}")
    c.execute("DELETE FROM schema_migrations WHERE version='0005'")
    c.commit(); c.close()
    assert webwrites.add_watchlist_symbol(db, "ZZZ").code == "unavailable"
    c = sqlite3.connect(db)
    assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == "0004"
    c.close()


# ------------------------------------------------------------------------------------------ persistence across restart
def test_watchlist_persists_across_a_server_restart_and_is_audited(tmp_path):
    db = plain_db(tmp_path)
    ap = approved_file(tmp_path)
    d = dash(db, ap)
    try:
        html = d.req("GET", "/watchlist")[2]
        st, hd, _ = d.req("POST", "/watchlist/add", origin=d.origin, body=f"csrf={csrf_of(html)}&symbol=zzz".encode(), headers=FORM)
        assert (st, hd["location"]) == (303, "/watchlist?msg=added")
    finally:
        d.stop()
    d2 = dash(db, ap)                                              # "restart": a brand-new server/session on the same journal
    try:
        rows = d2.api("/api/watchlist")["data"]["rows"]
        assert [(r["symbol"], r["reason_tracked"]) for r in rows] == [("ZZZ", "watchlist")]
        assert rows[0]["status"]["status"] == "NEVER_FETCHED" and rows[0]["stock"] is None
    finally:
        d2.stop()
    assert audit_types(db) == ["WATCHLIST_ADDED"]


# ------------------------------------------------------------------------------------------ tracked != approved
def test_tracked_unapproved_ticker_shows_warning_and_can_never_produce_an_entry_candidate(monkeypatch, tmp_path):
    """ZZZ is tracked, fully fetched and has a juicy-looking put, but is NOT on the owner whitelist (config/approved.json
    stand-in contains only AAA). A real `scan --dry-run` over everything must never evaluate, queue or print ZZZ."""
    from s3_helpers import run_cli
    db = tmp_path / "j.sqlite3"
    ap = approved_file(tmp_path, ("AAA",), approved_through="2027-06-30")
    ap_bytes = ap.read_bytes()
    for args in (["journal", "init"], ["journal", "confirm-baseline", "--equity", "100000"],
                 ["daily-check-in", "--equity", "100000", "--free-cash", "100000", "--ack-reconciled", "--apply", "--reason", "t"]):
        c0, o0, e0 = run_cli(monkeypatch, tmp_path, args, now=NOW - timedelta(minutes=30), frozen_journal=True, approved=ap)
        assert c0 == 0, o0 + e0                                      # a CLEAN, reconciled ledger so that the scan really runs
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ", spot=61.25, strikes=(55.0,), bid=0.9, ask=0.95, delta=-0.12)
    refresh(db, p, NOW)
    d = dash(db, ap, now=NOW + timedelta(seconds=30))
    try:
        blk = mkt(d, "ZZZ")["block"]
        assert blk["stock"]["price"] == "61.25"
        ap_state = blk["approval"]["state"] if isinstance(blk["approval"], dict) else str(blk["approval"])
        assert "NOT" in ap_state.upper() and "NOT APPROVED" in ap_state.upper().replace("_", " ")
        t = text_of(d.req("GET", "/market/ZZZ")[2])
        assert "Not approved for entry alerts" in t and "can never produce an entry alert" in t
    finally:
        d.stop()
    before = hold(db)
    src = FakeSource({"AAA": [raw_row(strike=75.0)], "ZZZ": [raw_row(strike=55.0, spot=61.25, bid=0.9, ask=0.95, delta=-0.12)]})
    code, out, err = run_cli(monkeypatch, tmp_path, ["scan", "--dry-run"], now=NOW, source=src, approved=ap)
    assert any(c[0] == "AAA" for c in src.calls), (code, out, err)       # the scan did run for the approved symbol (not vacuous)
    assert all(c[0] != "ZZZ" for c in src.calls), src.calls          # the scan never even asks the provider about a tracked-only symbol
    assert "ZZZ" not in out + err
    dump = json.dumps(hold(db), default=str)
    for table in ("entry_evaluations", "alert_outbox", "market_observations"):
        assert "ZZZ" not in json.dumps(hold(db)[table], default=str), table
    assert ap.read_bytes() == ap_bytes
    assert "ZZZ" not in json.dumps({k: v for k, v in hold(db).items() if k == "approved_underlyings"})


def test_expired_approval_is_labelled_and_a_held_symbol_is_still_refreshed(tmp_path):
    db = tmp_path / "j.sqlite3"
    build_book(db).close()                                          # AAA is an OPEN holding
    ap = approved_file(tmp_path, ("AAA",), approved_through="2026-10-01")         # expired before NOW
    p = Prov(NOW)
    p.set("AAA", spot=80.0, strikes=(75.0,))
    res = refresh(db, p, NOW)
    assert any(o.symbol == "AAA" and o.attempted and o.reasons == "active holding" for o in res.outcomes)
    d = dash(db, ap, now=NOW + timedelta(seconds=30))
    try:
        blk = mkt(d, "AAA")["block"]
        a = blk["approval"]
        assert "EXPIRED" in (a["state"] if isinstance(a, dict) else str(a)).upper()
        assert blk["stock"]["price"] == "80" and blk["reason_tracked"] == "active holding"
    finally:
        d.stop()


# ------------------------------------------------------------------------------------------ reasons and holdings
def test_reason_labels_watchlist_holding_both_and_removal_never_stops_holdings(tmp_path):
    """build_book: OPEN AAA, WORKING BBB, CLOSE_WORKING CCC, ADJUSTMENT_REVIEW DDD, PENDING EEE, ASSIGNED_PENDING FFF,
    owned-stock lot GGG are HELD. CLOSED HHH and DRAFT III are not."""
    db = tmp_path / "j.sqlite3"
    build_book(db).close()
    add_wl(db, "ZZZ", "AAA")
    j = jopen(db, NOW)
    got = {t.symbol: t.reason for t in wl.tracked(j)}
    j.close()
    assert got == {"AAA": "both", "BBB": "active holding", "CCC": "active holding", "DDD": "active holding", "EEE": "active holding",
                   "FFF": "active holding", "GGG": "active holding", "ZZZ": "watchlist"}
    assert "HHH" not in got and "III" not in got
    assert webwrites.remove_watchlist_symbol(db, "AAA", clock=lambda: NOW).code == "removed"
    j = jopen(db, NOW)
    assert {t.symbol: t.reason for t in wl.tracked(j)}["AAA"] == "active holding"        # removal does not stop monitoring the holding
    j.close()
    p = Prov(NOW)
    for s in ("AAA", "ZZZ", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"):
        p.set(s, strikes=(75.0,))
    res = refresh(db, p, NOW)
    assert {o.symbol for o in res.outcomes if o.attempted} == {"AAA", "ZZZ", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"}
    assert "HHH" not in {c[0] for c in p.calls}


def test_closing_the_last_holding_returns_a_symbol_to_untracked_but_keeps_stored_data(tmp_path):
    from csp_alerts.journal.models import PositionState as S
    db = tmp_path / "j.sqlite3"
    j = build_book(db)
    j.close()
    p = Prov(NOW)
    p.set("AAA", spot=80.0, strikes=(75.0,))
    refresh(db, p, NOW)
    j = jopen(db, NOW)
    j.transition("OPN", S.CLOSE_WORKING)
    j.transition("OPN", S.CLOSED, actual_close_debit_per_share=__import__("decimal").Decimal("0.10"), closed_at_utc=NOW, fees_usd=__import__("decimal").Decimal("0.65"))
    assert "AAA" not in {t.symbol for t in wl.tracked(j)}
    j.close()
    d = dash(db, approved_file(tmp_path, ("AAA",)), now=NOW + timedelta(seconds=30))
    try:
        assert "AAA" not in [r["symbol"] for r in d.api("/api/watchlist")["data"]["rows"]]
        blk = mkt(d, "AAA")["block"]                                 # stored data remains viewable, labelled honestly
        assert blk["stock"]["price"] == "80" and "no longer tracked" in blk["reason_tracked"]
    finally:
        d.stop()


def test_removing_a_watchlist_symbol_keeps_its_stored_history(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "ZZZ")
    p = Prov(NOW)
    p.set("ZZZ")
    refresh(db, p, NOW)
    snap = md_dump(db)
    assert webwrites.remove_watchlist_symbol(db, "ZZZ", clock=lambda: NOW).code == "removed"
    after = md_dump(db)
    assert after["md_stock_latest"] == snap["md_stock_latest"] and after["md_option_latest"] == snap["md_option_latest"]
    p.calls.clear()
    refresh(db, p, NOW + timedelta(hours=2))
    assert p.calls == []                                             # no longer fetched
