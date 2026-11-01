"""Stage A: watchlist, persisted observations, market-refresh job, migration, web write module. All data synthetic."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from chain_fixtures import EXP, raw_row
from factories import NOW, TODAY, policy, put, snap
from market_fixtures import FakeProvider, dump_non_market, make_journal

from csp_alerts.jobs.market_refresh import run_market_refresh
from csp_alerts.journal import store, watchlist, webwrites
from csp_alerts.journal.models import PositionState as S, StockLot
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.market import freshness as fr
from csp_alerts.market import observations as obs
from csp_alerts.market.models import SessionInfo, SessionStatus

OPEN = SessionInfo(SessionStatus.OPEN, 300)
CLOSED = SessionInfo(SessionStatus.CLOSED, None)
POL = policy()


def refresh(j, src, now=NOW, session=OPEN, **kw):
    return run_market_refresh(journal=j, policy=POL, source=src, now_utc=now, session=session, sleep=lambda s: None,
                              pause_seconds=0, **kw)


def add(j, *syms):
    for s in syms:
        watchlist.add_symbol(j, s, actor="owner", source="test")


# ---------------------------------------------------------------- watchlist
def test_add_remove_idempotent_audited_and_not_a_ledger_event(tmp_path):
    j = make_journal(tmp_path)
    j.confirm_baseline(D("100000"))
    seqs = j.ledger_seqs()
    assert watchlist.add_symbol(j, " spym ", actor="owner", source="cli") == ("SPYM", True)
    assert watchlist.add_symbol(j, "SPYM", actor="owner", source="cli") == ("SPYM", False)      # double submit: no second row/audit
    assert [e["event_type"] for e in j.audit_events() if e["event_type"].startswith("WATCHLIST")] == ["WATCHLIST_ADDED"]
    assert watchlist.remove_symbol(j, "spym", actor="owner", source="cli") == ("SPYM", True)
    assert watchlist.remove_symbol(j, "SPYM", actor="owner", source="cli") == ("SPYM", False)
    assert [e["event_type"] for e in j.audit_events() if e["event_type"].startswith("WATCHLIST")] == ["WATCHLIST_ADDED", "WATCHLIST_REMOVED"]
    assert j.ledger_seqs()[0] == seqs[0] and j.last_ledger_change() is None      # never forces a new snapshot / reconcile


@pytest.mark.parametrize("bad", ["", " ", "1ABC", "A B", "ABCDEFGHIJK", "<script>", "SPYM;DROP", "A\x00B", "‮SPY", "ａｂｃ", "-X", "a" * 100,
                                 None, 5, b"SPY"])
def test_hostile_or_invalid_symbols_rejected_without_echo(tmp_path, bad):
    j = make_journal(tmp_path)
    with pytest.raises(watchlist.WatchlistError) as e:
        watchlist.add_symbol(j, bad, actor="owner", source="test")
    assert e.value.code == "invalid" and (not isinstance(bad, str) or not bad.strip() or bad.strip() not in str(e.value) or len(bad.strip()) < 2)
    assert j.conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


def test_watchlist_is_bounded(tmp_path):
    j = make_journal(tmp_path)
    for i in range(fr.MAX_WATCHLIST):
        watchlist.add_symbol(j, f"S{i}", actor="o", source="t")
    with pytest.raises(watchlist.WatchlistError) as e:
        watchlist.add_symbol(j, "EXTRA", actor="o", source="t")
    assert e.value.code == "full"


def _open_position(j, ticker="HLD", state_to=S.OPEN):
    j.create_draft(NewPosition("T-" + ticker, ticker, TODAY + timedelta(days=40), D("40"), 100, 1, "sec_x", "b_x"))
    j.transition("T-" + ticker, S.PENDING)
    if state_to is S.OPEN:
        j.transition("T-" + ticker, S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.50"), opened_at_utc=NOW)


def test_tracking_reason_watchlist_holding_both_and_holding_survives_removal(tmp_path):
    j = make_journal(tmp_path)
    add(j, "WLO", "HLD")
    _open_position(j, "HLD")
    j.add_stock_lot(StockLot("L-1", "STK", D("100"), D("10"), NOW, "sec_s", "b_s", "PURCHASE", asset_type="ETF"))
    r = {t.symbol: t.reason for t in watchlist.tracked(j)}
    assert r == {"WLO": "watchlist", "HLD": "both", "STK": "active holding"}
    watchlist.remove_symbol(j, "HLD", actor="o", source="t")
    r = {t.symbol: t.reason for t in watchlist.tracked(j)}
    assert r["HLD"] == "active holding"                                   # removed from the list, still monitored
    # and the refresh job still fetches it, even though no approval exists anywhere
    src = FakeProvider()
    for s in ("WLO", "HLD", "STK"):
        src.set(s)
    res = refresh(j, src)
    assert sorted(o.symbol for o in res.outcomes if o.attempted) == ["HLD", "STK", "WLO"]


def test_closed_position_is_no_longer_a_holding(tmp_path):
    j = make_journal(tmp_path)
    _open_position(j, "HLD")
    j.transition("T-HLD", S.CLOSE_WORKING)
    assert "HLD" in watchlist.held_symbols(j)                              # CLOSE_WORKING is still monitored
    j.transition("T-HLD", S.CLOSED, actual_close_debit_per_share=D("0.10"), closed_at_utc=NOW, fees_usd=D("0"))
    assert "HLD" not in watchlist.held_symbols(j)


# ---------------------------------------------------------------- refresh job: persistence and truthfulness
def test_observation_a_then_b_propagates_and_history_is_bounded(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", spot=48.0, bid=0.50, ask=0.54)
    r = refresh(j, src)
    assert r.status == "OK" and r.outcomes[0].price == "48"
    st = obs.latest_stock(j.conn, "AAA")
    assert (st["price"], st["price_type"], st["provider"], st["tier"], st["market_time_utc"], st["retrieved_at_utc"], st["source"]) == (
        "48", "LAST", "YAHOO_VIA_STOCKPILE", "INDICATIVE_UNTIMESTAMPED", None, "2026-10-30T15:05:00Z", "REFRESH")
    o = obs.option_for_contract(j.conn, "AAA", TODAY + timedelta(days=40), D("40"))
    assert (o["bid"], o["ask"], o["occ_symbol"], o["occ_source"], o["option_right"]) == ("0.5", "0.54", "AAA261209P00040000", "DERIVED", "P")
    # observation B, five minutes later
    src.set("AAA", spot=49.5, bid=0.60, ask=0.66)
    src.fetched_at = NOW + timedelta(minutes=5)
    refresh(j, src, now=NOW + timedelta(minutes=5))
    st = obs.latest_stock(j.conn, "AAA")
    o = obs.option_for_contract(j.conn, "AAA", TODAY + timedelta(days=40), D("40"))
    assert st["price"] == "49.5" and st["retrieved_at_utc"] == "2026-10-30T15:10:00Z"
    assert (o["bid"], o["ask"], o["retrieved_at_utc"]) == ("0.6", "0.66", "2026-10-30T15:10:00Z")
    assert j.conn.execute("SELECT COUNT(*) FROM md_stock_history").fetchone()[0] == 2
    assert j.conn.execute("SELECT COUNT(*) FROM md_option_history WHERE strike='40'").fetchone()[0] == 2


def test_binary_float_noise_from_the_provider_is_not_stored_as_precision(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", rows=[raw_row(strike=40.0, spot=60.13999938964844, bid=0.30000001, ask=0.5, delta=-0.44506781023026554, iv=0.327155166015625)])
    refresh(j, src)
    assert obs.latest_stock(j.conn, "AAA")["price"] == "60.14"
    o = obs.latest_options(j.conn, "AAA")[0]
    assert (o["bid"], o["spot"], o["delta"], o["implied_volatility"]) == ("0.3", "60.14", "-0.445068", "0.327155")


def test_unchanged_quote_updates_retrieval_time_but_adds_no_history_row(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA")
    refresh(j, src)
    src.fetched_at = NOW + timedelta(minutes=5)
    refresh(j, src, now=NOW + timedelta(minutes=5))
    assert j.conn.execute("SELECT COUNT(*) FROM md_option_history WHERE strike='40'").fetchone()[0] == 1
    assert obs.option_for_contract(j.conn, "AAA", TODAY + timedelta(days=40), D("40"))["retrieved_at_utc"] == "2026-10-30T15:10:00Z"


def test_outage_keeps_prior_rows_with_original_timestamps_and_never_writes_nulls(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", spot=48.0)
    refresh(j, src)
    before = (obs.latest_stock(j.conn, "AAA"), obs.latest_options(j.conn, "AAA"))
    src.fail("AAA", "FETCH_ERROR", "connection reset by peer")
    later = NOW + timedelta(hours=1)
    r = refresh(j, src, now=later)
    assert r.status == "ERROR"
    assert (obs.latest_stock(j.conn, "AAA"), obs.latest_options(j.conn, "AAA")) == before          # untouched, ORIGINAL times
    ss = obs.get_status(j.conn, "AAA")
    assert ss["status"] == "ERROR" and ss["last_success_at_utc"] == "2026-10-30T15:05:00Z" and ss["last_attempt_at_utc"] == "2026-10-30T16:05:00Z"
    assert ss["last_error"].startswith("NETWORK_ERROR: ") and "connection reset" not in ss["last_error"] and ss["consecutive_failures"] == 1


def test_unusable_requote_does_not_overwrite_a_good_quote(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", strikes=(40.0, 42.0), bid=0.50, ask=0.54)
    refresh(j, src)
    src.set("AAA", rows=[raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0, bid=0.70, ask=0.75)])
    src.fetched_at = NOW + timedelta(minutes=5)
    r = refresh(j, src, now=NOW + timedelta(minutes=5))
    o40 = obs.option_for_contract(j.conn, "AAA", TODAY + timedelta(days=40), D("40"))
    o42 = obs.option_for_contract(j.conn, "AAA", TODAY + timedelta(days=40), D("42"))
    assert (o40["bid"], o40["retrieved_at_utc"]) == ("0.5", "2026-10-30T15:05:00Z")                # retained, original time
    assert (o42["bid"], o42["retrieved_at_utc"]) == ("0.7", "2026-10-30T15:10:00Z")
    assert r.outcomes[0].contracts_retained == 1


def test_rate_limit_status_backoff_and_deferral_then_recovery(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA", "BBB")
    src = FakeProvider()
    src.fail("AAA", "RATE_LIMIT", "Yahoo rate limit hit")
    src.set("BBB")
    r = refresh(j, src)
    assert r.status == "RATE_LIMITED"
    by = {o.symbol: o for o in r.outcomes}
    assert by["AAA"].status == "RATE_LIMITED" and by["BBB"].attempted is False and "rate limit" in by["BBB"].skipped
    assert [c[0] for c in src.calls] == ["AAA"]                                                    # no hammering after a 429
    ss = obs.get_status(j.conn, "AAA")
    assert ss["status"] == "RATE_LIMITED" and ss["next_attempt_after_utc"] == "2026-10-30T15:20:00Z"
    # inside the backoff: even a forced run does not call the provider for AAA
    src.calls.clear()
    r2 = refresh(j, src, now=NOW + timedelta(minutes=6), force=True)
    assert "AAA" not in [c[0] for c in src.calls] and {o.symbol: o.skipped for o in r2.outcomes}["AAA"] == "backoff"
    # after the backoff the provider recovers
    src.set("AAA")
    src.fetched_at = NOW + timedelta(minutes=16)
    r3 = refresh(j, src, now=NOW + timedelta(minutes=16))
    assert {o.symbol: o.status for o in r3.outcomes}["AAA"] == "OK"
    ss = obs.get_status(j.conn, "AAA")
    assert ss["status"] == "OK" and ss["consecutive_failures"] == 0 and ss["next_attempt_after_utc"] is None


def test_backoff_doubles_and_is_capped():
    assert [fr.backoff_seconds("RATE_LIMITED", n) for n in (1, 2, 3, 4, 9)] == [900, 1800, 3600, 3600, 3600]
    assert [fr.backoff_seconds("ERROR", n) for n in (1, 2, 3, 4, 9)] == [300, 600, 1200, 1800, 1800]


def test_absent_market_timestamp_is_null_never_the_retrieval_time(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA")
    refresh(j, src)
    st = obs.latest_stock(j.conn, "AAA")
    assert st["market_time_utc"] is None and st["retrieved_at_utc"] == "2026-10-30T15:05:00Z"
    assert all(o["quote_time_utc"] is None for o in obs.latest_options(j.conn, "AAA"))
    # a provider that does supply a market time keeps it, distinct from the retrieval time
    src.set("AAA", spot=48.0, spot_asof_utc="2026-10-30T15:04:30Z")
    src.fetched_at = NOW + timedelta(minutes=5)
    refresh(j, src, now=NOW + timedelta(minutes=5))
    st = obs.latest_stock(j.conn, "AAA")
    assert st["market_time_utc"] == "2026-10-30T15:04:30Z" and st["retrieved_at_utc"] == "2026-10-30T15:10:00Z"


def test_market_closed_is_labelled_and_still_stored(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA")
    r = refresh(j, src, session=CLOSED)
    assert r.outcomes[0].status == "MARKET_CLOSED" and r.session == "CLOSED"
    assert obs.latest_stock(j.conn, "AAA")["price"] == "48"
    assert obs.get_status(j.conn, "AAA")["session_at_attempt"] == "CLOSED"


def test_empty_chain_is_partial_not_ok_and_invents_nothing(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.script["AAA"] = []
    r = refresh(j, src)
    assert r.outcomes[0].status == "PARTIAL" and obs.latest_stock(j.conn, "AAA") is None
    assert obs.get_status(j.conn, "AAA")["last_success_at_utc"] is None


def test_inconsistent_stock_prices_store_no_price(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", rows=[raw_row(strike=40.0, spot=48.0), raw_row(strike=42.0, spot=47.0)])
    r = refresh(j, src)
    assert obs.latest_stock(j.conn, "AAA") is None and r.outcomes[0].status == "PARTIAL"
    assert obs.latest_options(j.conn, "AAA")                                   # option rows are still stored; stock is separate


def test_refresh_writes_only_market_data_tables(tmp_path):
    j = make_journal(tmp_path)
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(asof=NOW - timedelta(hours=1)))
    _open_position(j, "HLD")
    add(j, "AAA")
    before = dump_non_market(j)
    src = FakeProvider()
    src.set("AAA")
    src.set("HLD", strikes=(40.0,))
    r = refresh(j, src, force=True)
    assert r.status == "OK"
    assert dump_non_market(j) == before                       # ledger, snapshots, audit, outbox, runs, approvals: byte-for-byte rows
    assert j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0


def test_dry_run_persists_nothing_at_all(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA")
    before_all = {t: j.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                  ("md_stock_latest", "md_option_latest", "md_symbol_status", "md_refresh_runs", "md_refresh_requests")}
    r = refresh(j, src, persist=False)
    assert r.outcomes[0].price == "48" and r.status == "OK"
    assert {t: j.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before_all} == before_all


def test_cadence_open_vs_closed_and_no_duplicate_calls(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA")
    refresh(j, src)
    assert len(src.calls) == 1
    refresh(j, src, now=NOW + timedelta(minutes=1))                              # task fired again a minute later: nothing due
    refresh(j, src, now=NOW + timedelta(minutes=4))
    assert len(src.calls) == 1
    r = refresh(j, src, now=NOW + timedelta(minutes=5))
    assert len(src.calls) == 2 and r.outcomes[0].attempted
    # market closed: hourly
    refresh(j, src, now=NOW + timedelta(minutes=30), session=CLOSED)
    assert len(src.calls) == 2
    refresh(j, src, now=NOW + timedelta(minutes=66), session=CLOSED)
    assert len(src.calls) == 3
    idle = lambda: j.conn.execute("SELECT COUNT(*) FROM md_refresh_runs WHERE status='IDLE'").fetchone()[0]  # noqa: E731
    assert idle() == 0                                                         # idle 5-minute ticks are not logged
    refresh(j, src, now=NOW + timedelta(minutes=67), session=CLOSED)           # idle, but a run was recorded a minute ago
    assert idle() == 0
    refresh(j, src, now=NOW + timedelta(minutes=123), session=CLOSED)          # 57 min after the last record, nothing due yet
    assert idle() == 1                                                         # hourly heartbeat so the page can show the job is alive


def test_is_due_rules():
    t0 = NOW
    sec = lambda n: t0 + timedelta(seconds=n)  # noqa: E731
    assert fr.is_due(now=t0, last_attempt=None, next_attempt_after=None, session=SessionStatus.OPEN) == (True, "never attempted")
    assert fr.is_due(now=sec(30), last_attempt=t0, next_attempt_after=None, session=SessionStatus.OPEN, force=True)[0] is False   # min gap
    assert fr.is_due(now=sec(61), last_attempt=t0, next_attempt_after=None, session=SessionStatus.OPEN, force=True)[0] is True
    assert fr.is_due(now=sec(260), last_attempt=t0, next_attempt_after=None, session=SessionStatus.OPEN)[0] is False
    assert fr.is_due(now=sec(271), last_attempt=t0, next_attempt_after=None, session=SessionStatus.OPEN)[0] is True    # 30 s jitter grace
    assert fr.is_due(now=sec(600), last_attempt=t0, next_attempt_after=sec(900), session=SessionStatus.OPEN, force=True)[0] is False
    assert fr.is_due(now=sec(3000), last_attempt=t0, next_attempt_after=None, session=SessionStatus.CLOSED)[0] is False


def test_per_run_symbol_cap_serves_longest_waiting_first(tmp_path, monkeypatch):
    from csp_alerts.jobs import market_refresh as mr
    monkeypatch.setattr(mr, "MAX_SYMBOLS_PER_RUN", 2)
    j = make_journal(tmp_path)
    add(j, "AAA", "BBB", "CCC")
    src = FakeProvider()
    for s in ("AAA", "BBB", "CCC"):
        src.set(s)
    r = refresh(j, src)
    assert len(src.calls) == 2 and sum(1 for o in r.outcomes if o.skipped == "per-run symbol cap") == 1
    src.calls.clear()
    src.fetched_at = NOW + timedelta(minutes=5)
    refresh(j, src, now=NOW + timedelta(minutes=5))
    assert "CCC" in [c[0] for c in src.calls]                              # the one skipped earlier goes first


def test_held_contract_widens_the_browse_window_but_not_any_gate(tmp_path):
    j = make_journal(tmp_path)
    _open_position(j, "HLD")                                               # expires in 40 days, inside the default browse window
    j.create_draft(NewPosition("T-FAR", "FAR", TODAY + timedelta(days=100), D("40"), 100, 1, "sec_f", "b_f"))
    j.transition("T-FAR", S.PENDING)
    src = FakeProvider()
    src.set("HLD")
    src.set("FAR", exp=(TODAY + timedelta(days=100)).isoformat())
    refresh(j, src)
    win = {c[0]: (c[1], c[2]) for c in src.calls}
    assert win["HLD"] == (fr.BROWSE_MIN_DTE, fr.BROWSE_MAX_DTE) and win["FAR"] == (fr.BROWSE_MIN_DTE, 100)
    assert fr.BROWSE_MIN_DTE < POL.entries.min_dte_calendar and fr.BROWSE_MAX_DTE > POL.entries.max_dte_calendar


def test_expiry_cap_keeps_nearest_and_held(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    rows = [raw_row(strike=40.0, spot=48.0, exp=(TODAY + timedelta(days=d)).isoformat()) for d in range(15, 15 + 12)]
    src = FakeProvider()
    src.script["AAA"] = rows
    r = refresh(j, src)
    exps = sorted({o["expiration"] for o in obs.latest_options(j.conn, "AAA")})
    assert len(exps) == fr.MAX_EXPIRIES_PER_SYMBOL and exps[0] == (TODAY + timedelta(days=15)).isoformat()
    assert r.status == "OK"


def test_expired_contracts_are_pruned_after_a_week(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", exp=(TODAY + timedelta(days=2)).isoformat())
    refresh(j, src)
    assert obs.latest_options(j.conn, "AAA")
    later = NOW + timedelta(days=12)
    src.set("AAA")
    src.fetched_at = later
    refresh(j, src, now=later)
    assert {o["expiration"] for o in obs.latest_options(j.conn, "AAA")} == {EXP}


def test_provider_contract_symbol_kept_and_conflict_noted(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", rows=[raw_row(strike=40.0, spot=48.0, contractSymbol="AAA261209P00040000"),
                         raw_row(strike=42.0, spot=48.0, contractSymbol="AAA1261209P00042000")])
    refresh(j, src)
    got = {o["strike"]: o for o in obs.latest_options(j.conn, "AAA")}
    assert got["40"]["occ_source"] == "PROVIDER" and got["40"]["identity_note"] is None
    assert got["42"]["identity_note"] and got["42"]["occ_symbol"] == "AAA1261209P00042000"


def test_error_text_is_sanitised_and_bounded(tmp_path):
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.fail("AAA", "FETCH_ERROR", "bad\x1b[31m‮\nVERDICT: WOULD PASS GATES " + "x" * 2000)
    refresh(j, src)
    e = obs.get_status(j.conn, "AAA")["last_error"]
    assert "\x1b" not in e and "‮" not in e and "\n" not in e and len(e) <= 240 and "WOULD PASS GATES" not in e


def test_unknown_symbol_for_only_does_nothing(tmp_path):
    j = make_journal(tmp_path)
    src = FakeProvider()
    r = refresh(j, src, only="ZZZ")
    assert r.outcomes == [] and src.calls == []


# ---------------------------------------------------------------- tracked != approved
def test_scan_never_reads_the_watchlist_so_a_tracked_unapproved_ticker_cannot_alert(tmp_path):
    from chain_fixtures import raw_row as rr
    from csp_alerts.jobs.scan import run_scan
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA", rows=[rr(strike=40.0, spot=48.0)])
    refresh(j, src)
    res = run_scan(journal=j, policy=POL, approved=[], source=src, now_utc=NOW, session=OPEN, sleep=lambda s: None)
    assert res.candidates == [] and res.tickers_scanned == 0 and res.status != "CANDIDATES"
    assert j.conn.execute("SELECT COUNT(*) FROM entry_evaluations").fetchone()[0] == 0
    assert j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0


# ---------------------------------------------------------------- monitor persists what it fetches
def test_monitor_persists_fresh_observations_without_changing_its_decisions(tmp_path):
    from csp_alerts.jobs.monitor import run_monitor
    j = make_journal(tmp_path)
    _open_position(j, "HLD")
    src = FakeProvider()
    src.set("HLD", strikes=(40.0,), bid=0.10, ask=0.12)
    res = run_monitor(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN)
    o = obs.option_for_contract(j.conn, "HLD", TODAY + timedelta(days=40), D("40"))
    assert o and (o["bid"], o["ask"], o["source"], o["retrieved_at_utc"]) == ("0.1", "0.12", "MONITOR", "2026-10-30T15:05:00Z")
    assert obs.latest_stock(j.conn, "HLD")["source"] == "MONITOR"
    assert obs.get_status(j.conn, "HLD")["last_success_at_utc"] == "2026-10-30T15:05:00Z"
    # same result as before this change: ask 0.12 vs credit 0.50 => profit exit still fires from the in-memory quote
    assert [s.trigger.value for _, s in res.signals] == ["PROFIT_EXIT"]
    assert j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0


def test_monitor_storage_failure_never_breaks_monitoring(tmp_path, monkeypatch):
    from csp_alerts.jobs.monitor import run_monitor
    j = make_journal(tmp_path)
    _open_position(j, "HLD")
    src = FakeProvider()
    src.set("HLD", strikes=(40.0,), bid=0.10, ask=0.12)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(obs, "store_chain", boom)
    res = run_monitor(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN)
    assert [s.trigger.value for _, s in res.signals] == ["PROFIT_EXIT"] and any("not persisted" in n for n in res.notes)


# ---------------------------------------------------------------- refresh requests
def test_request_is_rate_limited_deduplicated_and_picked_up_by_the_job(tmp_path):
    clock = {"t": NOW}
    j = Journal(tmp_path / "r.sqlite3", clock=lambda: clock["t"])
    j.migrate()
    add(j, "AAA")
    tr = {"AAA"}
    assert obs.request_refresh(j, "ALL", "web:owner", tr)[0] == "queued"
    assert obs.request_refresh(j, "ALL", "web:owner", tr)[0] == "pending"           # duplicate submit
    assert obs.request_refresh(j, "AAA", "web:owner", tr)[0] == "pending"           # ALL already covers it
    assert obs.request_refresh(j, "ZZZ", "web:owner", tr)[0] == "unknown_symbol"
    assert [e["event_type"] for e in j.audit_events() if e["event_type"] == "MARKET_REFRESH_REQUESTED"] == ["MARKET_REFRESH_REQUESTED"]
    src = FakeProvider()
    src.set("AAA")
    # a regular run at the same instant is "never attempted" anyway; make AAA recently fetched so only the request forces it
    refresh(j, src, now=NOW + timedelta(minutes=2))
    req = obs.recent_requests(j.conn)[0]
    assert req["status"] == "DONE" and req["picked_up_at_utc"] and req["run_id"]
    clock["t"] = NOW + timedelta(seconds=30)
    assert obs.request_refresh(j, "ALL", "web:owner", tr)[0] == "rate"             # < 60 s since the last one
    clock["t"] = NOW + timedelta(minutes=3)
    assert obs.request_refresh(j, "ALL", "web:owner", tr)[0] == "queued"
    clock["t"] = NOW + timedelta(minutes=30)                                       # never picked up -> expires
    obs.claim_requests(j, "x", clock["t"])
    assert obs.recent_requests(j.conn)[0]["status"] == "EXPIRED"


def test_request_forces_a_symbol_but_never_overrides_backoff_or_min_gap(tmp_path):
    clock = {"t": NOW}
    j = Journal(tmp_path / "r.sqlite3", clock=lambda: clock["t"])
    j.migrate()
    add(j, "AAA")
    src = FakeProvider()
    src.set("AAA")
    refresh(j, src)
    clock["t"] = NOW + timedelta(minutes=2)
    obs.request_refresh(j, "AAA", "web:owner", {"AAA"})
    src.calls.clear()
    r = refresh(j, src, now=NOW + timedelta(minutes=2))
    assert r.trigger == "REQUEST" and len(src.calls) == 1                         # 2 min < 5 min interval, forced by the request
    clock["t"] = NOW + timedelta(minutes=2, seconds=20)
    obs.request_refresh(j, "AAA", "web:owner", {"AAA"})
    src.calls.clear()
    clock["t"] = NOW + timedelta(minutes=10)
    refresh(j, src, now=NOW + timedelta(minutes=2, seconds=30))                    # 30 s after the last fetch: minimum gap holds
    assert src.calls == []


# ---------------------------------------------------------------- web write module
def test_web_writes_validate_dedupe_and_are_audited(tmp_path):
    j = make_journal(tmp_path)
    db = tmp_path / "m.sqlite3"
    j.close()
    clk = lambda: NOW  # noqa: E731
    assert webwrites.add_watchlist_symbol(db, "tqqq", clock=clk) == webwrites.WriteResult("added", "TQQQ")
    assert webwrites.add_watchlist_symbol(db, "TQQQ", clock=clk).code == "already"
    for bad in ("", "<b>x</b>", "A" * 40, "1", None):
        assert webwrites.add_watchlist_symbol(db, bad, clock=clk).code == "invalid"
    assert webwrites.remove_watchlist_symbol(db, "TQQQ", clock=clk).code == "removed"
    assert webwrites.remove_watchlist_symbol(db, "TQQQ", clock=clk).code == "not_tracked"
    webwrites.add_watchlist_symbol(db, "AIQ", clock=clk)
    assert webwrites.request_market_refresh(db, "ALL", clock=clk).code == "queued"
    assert webwrites.request_market_refresh(db, "ALL", clock=clk).code == "pending"
    assert webwrites.request_market_refresh(db, "bad symbol!", clock=clk).code == "invalid"
    j = Journal(db)
    ev = [(e["event_type"], e["actor"], e["source"]) for e in j.audit_events() if e["event_type"].startswith(("WATCHLIST", "MARKET_REFRESH"))]
    assert ev == [("WATCHLIST_ADDED", "web:owner", "web"), ("WATCHLIST_REMOVED", "web:owner", "web"), ("WATCHLIST_ADDED", "web:owner", "web"),
                  ("MARKET_REFRESH_REQUESTED", "web:owner", "web")]


def test_web_writes_never_create_or_migrate_a_journal(tmp_path):
    missing = tmp_path / "none.sqlite3"
    assert webwrites.add_watchlist_symbol(missing, "SPYM").code == "unavailable" and not missing.exists()
    old = tmp_path / "old.sqlite3"
    j = Journal(old)
    j.migrate()
    j.conn.executescript("DROP TABLE watchlist;")
    j.close()
    assert webwrites.add_watchlist_symbol(old, "SPYM").code == "unavailable"


def test_web_write_module_has_no_network_or_trading_surface():
    import ast
    from pathlib import Path
    src = Path(webwrites.__file__).read_text(encoding="utf-8")
    mods = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(ast.parse(src))
            if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [None])}
    assert not [m for m in mods if m and m.split(".")[0] in {"socket", "urllib", "http", "subprocess", "requests"}]
    assert not [m for m in mods if m and ("telegram" in m or "stockpile" in m or "outbox" in m)]


# ---------------------------------------------------------------- migration with backup
def _copy_migrations(dst, upto):
    dst.mkdir()
    for p in store.MIGRATIONS_DIR.iterdir():
        if p.suffix == ".sql" and p.name[:4] <= upto:
            shutil.copy(p, dst / p.name)


def test_upgrade_of_an_existing_journal_takes_a_verified_backup_and_keeps_every_row(tmp_path, monkeypatch):
    old_dir = tmp_path / "mig_old"
    _copy_migrations(old_dir, "0004")
    monkeypatch.setattr(store, "MIGRATIONS_DIR", old_dir)
    db = tmp_path / "data" / "j.sqlite3"
    j = Journal(db, clock=lambda: NOW)
    assert j.migrate() == ["0001", "0002", "0003", "0004"] and j.last_migration_backup is None     # fresh journal: nothing to protect
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap(asof=NOW - timedelta(hours=1)))
    _open_position(j, "HLD")
    j.record_run("r1", "SCAN", NOW, "NO_OPPORTUNITIES", [], {})
    before = dump_non_market(j)
    j.close()
    monkeypatch.undo()
    j = Journal(db, clock=lambda: NOW)
    assert j.schema_version() == "0004"
    assert j.migrate() == ["0005"]
    assert j.schema_version() == "0005" and j.integrity_ok()
    bk = j.last_migration_backup
    assert bk is not None and bk.is_file() and bk.parent.name == "backups" and bk.name.startswith("pre-migrate-0004-to-0005-")
    after = dump_non_market(j)
    assert after.pop("schema_migrations")[-1][0] == "0005" and before.pop("schema_migrations")[-1][0] == "0004"
    assert after == before                                                 # every ledger / audit / run row preserved
    b = sqlite3.connect(bk)
    assert b.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == "0004"        # the backup is the pre-upgrade state
    assert b.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0] == 1
    assert b.execute("SELECT name FROM sqlite_master WHERE name='watchlist'").fetchone() is None
    b.close()
    assert j.migrate() == [] and len(list((db.parent / "backups").glob("pre-migrate-*"))) == 1      # idempotent: no second backup
    # drawdown / reconciliation / delivery state untouched by the upgrade
    assert j.baseline() is not None and len(j.list_snapshots()) == 1 and j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0


def test_migration_refused_when_the_backup_fails(tmp_path, monkeypatch):
    old_dir = tmp_path / "mig_old"
    _copy_migrations(old_dir, "0004")
    monkeypatch.setattr(store, "MIGRATIONS_DIR", old_dir)
    db = tmp_path / "j.sqlite3"
    j = Journal(db)
    j.migrate()
    j.close()
    monkeypatch.undo()
    j = Journal(db)

    def boom(self, dest):
        raise OSError("disk full")
    monkeypatch.setattr(Journal, "backup", boom)
    with pytest.raises(store.JournalError, match="refusing to migrate"):
        j.migrate()
    assert j.schema_version() == "0004"


# ---------------------------------------------------------------- CLI: watchlist and market-refresh
def _cli_env(tmp_path, monkeypatch):
    db = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("CSP_DATABASE_URL", "sqlite:///" + db.as_posix())
    monkeypatch.setenv("CSP_DRY_RUN", "true")
    return db, str(tmp_path / "none.env")


def test_cli_watchlist_preview_by_default_apply_writes_and_audits(tmp_path, monkeypatch, capsys):
    from csp_alerts.cli import main
    db, env = _cli_env(tmp_path, monkeypatch)
    assert main(["--env-file", env, "watchlist", "add", "spym", "TQQQ"]) == 0
    out = capsys.readouterr().out
    assert "PREVIEW SPYM" in out and "PREVIEW ONLY" in out
    j = Journal(db)
    assert j.conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    j.close()
    assert main(["--env-file", env, "watchlist", "add", "spym", "TQQQ", "--apply", "--reason", "owner choice"]) == 0
    assert main(["--env-file", env, "watchlist", "add", "SPYM", "--apply"]) == 0
    assert "already on" in capsys.readouterr().out
    j = Journal(db)
    assert sorted(s for s, _, _ in watchlist.watchlist_symbols(j)) == ["SPYM", "TQQQ"]
    assert [e["event_type"] for e in j.audit_events() if e["event_type"] == "WATCHLIST_ADDED"] == ["WATCHLIST_ADDED"] * 2
    j.close()
    assert main(["--env-file", env, "watchlist", "list"]) == 0
    out = capsys.readouterr().out
    assert "NOT approved for entry alerts" in out and "no price stored" in out and "SPYM" in out
    assert main(["--env-file", env, "watchlist", "remove", "SPYM"]) == 0
    assert "PREVIEW SPYM" in capsys.readouterr().out
    assert main(["--env-file", env, "watchlist", "remove", "SPYM", "--apply"]) == 0
    assert main(["--env-file", env, "watchlist", "add", "bad symbol!", "--apply"]) == 2
    j = Journal(db)
    assert [s for s, _, _ in watchlist.watchlist_symbols(j)] == ["TQQQ"]


def test_cli_market_refresh_runs_with_a_fake_provider_and_writes_only_market_tables(tmp_path, monkeypatch, capsys):
    from csp_alerts import cli
    from csp_alerts.market import stockpile
    db, env = _cli_env(tmp_path, monkeypatch)
    assert cli.main(["--env-file", env, "journal", "init"]) == 0
    assert cli.main(["--env-file", env, "watchlist", "add", "AAA", "--apply"]) == 0
    fake = FakeProvider()
    fake.set("AAA")
    monkeypatch.setattr(stockpile, "StockpileSource", lambda root: fake)
    j = Journal(db)
    before = dump_non_market(j)
    j.close()
    assert cli.main(["--env-file", env, "market-refresh", "--dry-run", "--force"]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "Yahoo indicative data" in out and "AAA" in out
    j = Journal(db)
    assert j.conn.execute("SELECT COUNT(*) FROM md_stock_latest").fetchone()[0] == 0
    j.close()
    assert cli.main(["--env-file", env, "market-refresh", "--force"]) == 0
    assert "persisted to market-data tables only" in capsys.readouterr().out
    j = Journal(db)
    assert obs.latest_stock(j.conn, "AAA")["price"] == "48" and j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0
    after = dump_non_market(j)
    assert after == before                                              # the CLI job moved nothing outside market-data tables
    j.close()
    fake.fail("AAA", "RATE_LIMIT", "429")
    assert cli.main(["--env-file", env, "market-refresh", "--force", "--ticker", "AAA"]) in (0, 1)   # inside the 60 s gap: skipped
    assert cli.main(["--env-file", env, "market-refresh", "--ticker", "ZZZ"]) == 0
    assert "not tracked" in capsys.readouterr().out


def test_cli_market_refresh_second_instance_exits_3_and_does_nothing(tmp_path, monkeypatch, capsys):
    from csp_alerts import cli
    from csp_alerts.lock import single_instance
    from csp_alerts.market import stockpile
    db, env = _cli_env(tmp_path, monkeypatch)
    assert cli.main(["--env-file", env, "journal", "init"]) == 0
    fake = FakeProvider()
    monkeypatch.setattr(stockpile, "StockpileSource", lambda root: fake)
    with single_instance(db.parent / "locks" / "market-refresh.lock"):
        assert cli.main(["--env-file", env, "market-refresh", "--force"]) == 3
    assert "SKIPPED" in capsys.readouterr().err and fake.calls == []
    # the shared scan/monitor lock is NOT used: a slow refresh can never make the monitor skip
    with single_instance(db.parent / "locks" / "market-refresh.lock"):
        assert cli.main(["--env-file", env, "monitor", "--dry-run"]) == 0


def test_cli_surface_has_no_send_on_new_commands():
    from csp_alerts.cli import build_parser
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    for name in ("watchlist", "market-refresh"):
        every = [sub.choices[name]] + [v for a in sub.choices[name]._actions if isinstance(getattr(a, "choices", None), dict) for v in a.choices.values()]
        for sp in every:
            opts = {o for a in sp._actions for o in a.option_strings}
            assert "--send" not in opts


def test_symbol_normalizers_reject_case_folding_lookalikes():
    from csp_alerts.jobs import ticker_check as tck
    from csp_alerts.journal import watchlist as wl
    for raw in ("ſpy", "ıbm"):
        assert tck.normalize_symbol(raw) is None
        with pytest.raises(wl.WatchlistError):
            wl.normalize_symbol(raw)
    assert wl.normalize_symbol(" spy ") == "SPY"


def test_feed_label_is_state_aware():
    from csp_alerts.market import freshness as fr
    from csp_alerts.market.models import SessionStatus
    assert fr.feed_label([("RETRIEVED",)], SessionStatus.OPEN).startswith("Auto-refreshing page")
    assert "failing" in fr.feed_label([("STALE",)], SessionStatus.OPEN)
    assert "market closed" in fr.feed_label([("MARKET-CLOSED",)], SessionStatus.CLOSED)
