"""Slice 2 acceptance: Stockpile adapter, normalizer, IT-004 standard-contract rule, exchange calendar.
Oracles: spec 3.1-3.3, 6.1, 12.1, 13.1; MVP_SCOPE IT-004; INDEPENDENT_ACCEPTANCE AT-05, AT-06, AT-10, AT-16, AT-17.
Synthetic data, frozen clock, no network."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, REPO, TODAY, codes, policy, run
from s2_helpers import EXP35, FakeSource, appr, ms, raw_row

from csp_alerts.jobs.scan import deliverable_multiplier
from csp_alerts.market import calendar as mcal
from csp_alerts.market.models import SessionStatus, TIER_R
from csp_alerts.market.normalize import normalize_rows, raw_sha256, standard_symbol
from csp_alerts.market.stockpile import SourceError, StockpileSource

POL = policy()


def norm(rows, ticker="AAA", mult=100, earnings=None, today=TODAY, now=NOW):
    return normalize_rows(ticker, rows, now, today_exchange=today, earnings_dates=earnings, multiplier=mult)


def gate(row, ticker="AAA", mult=100, **appr_kw):
    n = norm([row], ticker=ticker, mult=mult)
    assert len(n.rows) == 1, n.rejected
    return run(o=n.rows[0].observation, a=appr(ticker, **appr_kw))


# ====================================================================== normalizer: Yahoo-style lies
def test_base_row_maps_to_spec_6_1_observation():
    n = norm([raw_row()])
    o = n.rows[0].observation
    assert (o.underlying, o.option_right, o.expiration, o.multiplier) == ("AAA", "P", EXP35, 100)
    assert o.strike == D("75") and o.spot == D("80") and o.bid == D("0.5") and o.ask == D("0.54")
    assert o.delta == D("-0.13") and o.open_interest == 1000 and o.daily_volume == 100
    assert o.quote_at_utc is None and o.data_tier == TIER_R and o.provider == "YAHOO_VIA_STOCKPILE"
    assert o.retrieved_at_utc == NOW
    assert o.last_option_trade_at_utc == NOW - timedelta(minutes=25)     # ms epoch -> UTC, exact
    assert o.iv_excess_pp is None                                         # puts-only fetch: never invented
    d = o.to_dict()
    assert d["quote_at_utc"] is None and d["data_tier"] == "INDICATIVE_UNTIMESTAMPED"
    assert d["contract_key"] == "AAA|2026-12-04|P|75.00|100"


def test_last_trade_ms_is_never_promoted_to_a_quote_timestamp():
    o = norm([raw_row()]).rows[0].observation
    assert o.quote_at_utc is None and o.last_option_trade_at_utc is not None
    assert gate(raw_row()).data_tier == TIER_R
    assert gate(raw_row()).decision.value == "PASS_RESEARCH_ONLY"


@pytest.mark.parametrize("over", [
    dict(bid=0.0, ask=0.0, mid=0.52, last=0.52),          # no two-sided quote; upstream falls back mid=last
    dict(bid=0.0, ask=0.05, mid=0.025, last=0.52),        # bid 0, ask>0
    dict(bid=0.5, ask=0.0, mid=0.25, last=0.5),           # ask 0, bid>0
])
def test_zero_bid_or_ask_stays_zero_and_blocks_regardless_of_mid_or_last(over):
    row = raw_row(**over)
    o = norm([row]).rows[0].observation
    assert o.bid == D(str(over["bid"])) and o.ask == D(str(over["ask"]))      # 0 stays 0
    d = gate(row)
    assert "BAD_BID_ASK" in codes(d) and not d.passed


def test_upstream_mid_and_last_do_not_influence_the_observation_or_gate():
    a = norm([raw_row(mid=0.52, last=0.52)]).rows[0].observation
    b = norm([raw_row(mid=9.99, last=0.01)]).rows[0].observation
    assert a == b
    # spread gate uses real bid/ask: bid .50/ask .62 (spread .12) must block even if upstream mid says .52
    d = gate(raw_row(bid=0.5, ask=0.62, mid=0.52))
    assert "SPREAD_TOO_WIDE" in codes(d)


@pytest.mark.parametrize("key,val", [
    ("open_interest", None), ("open_interest", float("nan")), ("open_interest", -5), ("open_interest", 500.5),
    ("open_interest", "1,000"), ("open_interest", "n/a"),
    ("volume", None), ("volume", float("nan")), ("volume", -1), ("volume", True),
])
def test_missing_or_garbage_oi_volume_is_unknown_not_zero_and_blocks(key, val):
    row = raw_row(**{key: val})
    o = norm([row]).rows[0].observation
    assert (o.open_interest if key == "open_interest" else o.daily_volume) is None
    d = gate(row)
    assert "MISSING_MARKET_DATA" in codes(d) and not d.passed


def test_key_absent_oi_volume_unknown_and_zero_is_blocked_as_insufficient():
    row = raw_row()
    del row["open_interest"], row["volume"]
    o = norm([row]).rows[0].observation
    assert o.open_interest is None and o.daily_volume is None
    d0 = gate(raw_row(open_interest=0, volume=0))
    assert {"INSUFFICIENT_OI", "INSUFFICIENT_VOLUME"} <= codes(d0)


@pytest.mark.parametrize("typ,kept,calls,rejected", [
    ("put", 1, 0, 0), (" Put ", 1, 0, 0), ("PUT", 1, 0, 0), ("call", 0, 1, 0), ("CALL", 0, 1, 0),
    ("P", 0, 0, 1), ("C", 0, 0, 1), ("", 0, 0, 1), (None, 0, 0, 1), ("straddle", 0, 0, 1),
])
def test_option_type_put_vs_P(typ, kept, calls, rejected):
    n = norm([raw_row(type=typ)])
    assert (len(n.rows), n.calls_ignored, n.rejected.get("UNKNOWN_OPTION_TYPE", 0)) == (kept, calls, rejected)


def test_missing_type_key_rejected_not_assumed_put():
    row = raw_row()
    del row["type"]
    assert norm([row]).rows == []


@pytest.mark.parametrize("dte_claim,exp_dte,ok", [(40, 34, False), (20, 35, True), (35, 46, False), (0, 45, True)])
def test_upstream_dte_is_ignored_gate_recomputes_from_expiration(dte_claim, exp_dte, ok):
    row = raw_row(dte=dte_claim, expiration=(TODAY + timedelta(days=exp_dte)).isoformat())
    d = gate(row)
    assert ("DTE_OUT_OF_RANGE" not in codes(d)) is ok, codes(d)
    assert d.metrics["dte"] == exp_dte


@pytest.mark.parametrize("bad,reason", [
    (dict(strike="abc"), "BAD_STRIKE"), (dict(strike=-5.0), "BAD_STRIKE"), (dict(strike=0), "BAD_STRIKE"),
    (dict(strike=float("nan")), "BAD_STRIKE"), (dict(strike=float("inf")), "BAD_STRIKE"), (dict(strike=None), "BAD_STRIKE"),
    (dict(strike=[1]), "BAD_STRIKE"),
    (dict(expiration="garbage"), "BAD_EXPIRATION"), (dict(expiration=None), "BAD_EXPIRATION"),
    (dict(expiration="2026-02-30"), "BAD_EXPIRATION"), (dict(expiration=""), "BAD_EXPIRATION"),
])
def test_malformed_rows_rejected_cleanly(bad, reason):
    n = norm([raw_row(**bad), raw_row(strike=70.0)])
    assert n.rejected.get(reason) == 1 and len(n.rows) == 1       # the good row still flows


def test_expiration_with_time_suffix_and_date_objects_accepted():
    assert len(norm([raw_row(expiration="2026-12-04T00:00:00")]).rows) == 1
    assert len(norm([raw_row(expiration=EXP35)]).rows) == 1


@pytest.mark.parametrize("val", [None, float("nan"), 0, -1.0, True, "abc"])
def test_bad_last_trade_ms_is_unknown_and_blocks(val):
    row = raw_row(last_trade_ms=val)
    assert norm([row]).rows[0].observation.last_option_trade_at_utc is None
    assert not gate(row).passed


def test_future_last_trade_blocks():
    assert not gate(raw_row(last_trade_ms=ms(NOW + timedelta(minutes=10)))).passed


def test_last_trade_exactly_1_5_days_ok_older_blocks():
    assert gate(raw_row(last_trade_ms=ms(NOW - timedelta(seconds=129600)))).passed
    assert "STALE_LAST_TRADE" in codes(gate(raw_row(last_trade_ms=ms(NOW - timedelta(seconds=129601)))))


def test_float_money_becomes_exact_decimal_and_boundary_values_hold():
    # float 0.2 -> Decimal("0.2") (shortest repr), so bid==min_bid passes and spread .02 / mid .21 = 9.5% passes
    o = norm([raw_row(bid=0.2, ask=0.22)]).rows[0].observation
    assert o.bid == D("0.2") and isinstance(o.bid, D)
    assert gate(raw_row(bid=0.2, ask=0.22)).passed
    assert not gate(raw_row(bid=0.19, ask=0.21)).passed
    # delta exactly -0.15 / -0.10 from floats
    assert gate(raw_row(delta=-0.15)).passed and gate(raw_row(delta=-0.1)).passed
    assert not gate(raw_row(delta=-0.151)).passed and not gate(raw_row(delta=-0.099)).passed


def test_raw_sha256_provenance():
    r = raw_row()
    a, b = norm([r]).rows[0], norm([dict(reversed(list(r.items())))]).rows[0]
    assert re.fullmatch(r"[0-9a-f]{64}", a.raw_sha256) and a.raw_sha256 == b.raw_sha256 == raw_sha256(r)
    assert norm([raw_row(bid=0.51)]).rows[0].raw_sha256 != a.raw_sha256
    assert norm([raw_row(open_interest=float("nan"))]).rows[0].raw_sha256            # NaN still hashable
    ids = {x.observation_id for x in norm([raw_row(strike=float(s)) for s in range(60, 70)]).rows}
    assert len(ids) == 10


def test_earnings_window_boundaries_and_unknown():
    # window = [today, expiration] inclusive; nearest-future-date only; empty/None = UNKNOWN (never 'clear')
    def ew(dates):
        return norm([raw_row()], earnings=dates).rows[0].observation.earnings_in_window
    assert ew(None) is None and ew([]) is None
    assert ew([EXP35]) is True and ew([TODAY]) is True
    assert ew([EXP35 + timedelta(days=1)]) is False and ew([TODAY - timedelta(days=1)]) is False
    assert ew([TODAY + timedelta(days=10)]) is True


@pytest.mark.parametrize("zeroed,total,degraded", [
    (8, 10, False), (9, 10, True), (10, 10, True),          # 10-row minimum: 80% no, 90% yes
    (16, 20, False), (17, 20, True),                          # exactly 85% (17/20) degrades
    (9, 9, True), (1, 1, True), (2, 2, True), (0, 1, False), (0, 9, False), (1, 9, False),   # slice 3: any chain size
])
def test_degraded_chain_threshold_85_percent(zeroed, total, degraded):
    rows = [raw_row(strike=float(40 + i), bid=0.0, ask=0.0) if i < zeroed else raw_row(strike=float(40 + i))
            for i in range(total)]
    assert norm(rows).degraded is degraded


@pytest.mark.parametrize("n", [1, 3, 9, 25])
def test_every_bid_zero_degrades_at_any_size_even_when_asks_are_positive(n):
    rows = [raw_row(strike=float(40 + i), bid=0.0, ask=0.05) for i in range(n)]
    assert norm(rows).degraded is True


def test_a_single_positive_bid_among_zero_bids_is_not_the_every_bid_zero_case():
    rows = [raw_row(strike=float(40 + i), bid=0.0, ask=0.05) for i in range(4)] + [raw_row(strike=60.0)]
    n = norm(rows)
    assert n.degraded is False and len(n.rows) == 5


def test_calls_do_not_count_toward_degradation_denominator():
    rows = [raw_row(type="call") for _ in range(30)] + [raw_row(strike=float(40 + i)) for i in range(10)]
    n = norm(rows)
    assert n.calls_ignored == 30 and len(n.rows) == 10 and not n.degraded


# ====================================================================== IT-004 standard-contract rule
@pytest.mark.parametrize("sym,ticker,expected", [
    (None, "AAA", None), ("", "AAA", None), ("   ", "AAA", None),
    ("AAA261204P00075000", "AAA", True), ("aaa261204p00075000", "AAA", True), ("BRKB261204P00075000", "BRK.B", True),
    ("AAA1261204P00075000", "AAA", False),     # adjusted root: digit suffix
    ("AAA2261204P00075000", "AAA", False),
    ("BBB261204P00075000", "AAA", False),       # root mismatch
    ("AAAA261204P00075000", "AAA", False),
    ("garbage", "AAA", False), ("AAA261204P0007500", "AAA", False),
])
def test_standard_symbol_rule(sym, ticker, expected):
    assert standard_symbol(sym, ticker) is expected


@pytest.mark.parametrize("key", ["contractSymbol", "contract_symbol"])
@pytest.mark.parametrize("sym", ["AAA1261204P00075000", "BBB261204P00075000", "garbage"])
def test_nonstandard_symbol_blocks_even_with_owner_confirmation(key, sym):
    row = raw_row(**{key: sym})
    n = norm([row])
    assert n.rejected.get("NONSTANDARD_SYMBOL") == 1
    d = run(o=n.rows[0].observation, a=appr())          # approval confirmed standard
    assert not d.passed and "NONSTANDARD_CONTRACT" in codes(d)


def test_plain_symbol_plus_owner_confirmation_passes():
    d = gate(raw_row(contractSymbol="AAA261204P00075000"))
    assert d.passed, codes(d)


@pytest.mark.parametrize("sym,why", [
    ("AAA261204C00075000", "call right on a put row"),
    ("AAA261211P00075000", "different expiration"),
    ("AAA261204P00070000", "different strike"),
])
def test_symbol_that_contradicts_the_row_identity_must_block(sym, why):
    """Spec 13.1: wrong/missing multiplier, expiration or option right -> fail closed."""
    d = gate(raw_row(contractSymbol=sym))
    assert not d.passed, why


def test_no_owner_confirmation_blocks_unknown_deliverable_as_nonstandard():
    n = norm([raw_row()], mult=None)
    d = run(o=n.rows[0].observation, a=appr(confirmed=False))
    assert not d.passed and "NONSTANDARD_CONTRACT" in codes(d)


@pytest.mark.parametrize("delta_days,expected", [(0, 100), (7, 100), (8, None), (-1, None), (365, None)])
def test_owner_standard_deliverable_confirmation_freshness(delta_days, expected):
    a = appr(confirmed=False, standard_deliverable_confirmed_on=(TODAY - timedelta(days=delta_days)).isoformat())
    assert deliverable_multiplier(a, POL, TODAY) == expected


def test_unconfirmed_approval_yields_no_multiplier_and_never_defaults_to_100():
    assert deliverable_multiplier(appr(confirmed=False), POL, TODAY) is None


# ====================================================================== adapter (ChainSource implementation)
def stockpile_src():
    return StockpileSource(REPO / "third_party" / "stockpile")


def test_pinned_checkout_is_exact_sha():
    head = (REPO / "third_party" / "stockpile" / ".git" / "HEAD")
    assert head.is_file()
    assert head.read_text().strip() == "34bf6cf8101e54620ea1e2bda940f0a3aa88e791"


def test_adapter_calls_chain_puts_only_provider_yahoo_with_margin(monkeypatch):
    import pandas as pd
    seen = {}

    def fake_chain(ticker, **kw):
        seen.update(kw, ticker=ticker)
        return pd.DataFrame([raw_row()])

    s = stockpile_src()
    s._fetch_chain, s._fetch_earnings = fake_chain, lambda t: []
    rc = s.fetch_chain("AAA", 35, 45)
    assert seen["opt_type"] == "puts" and seen["provider"] == "yahoo" and seen["ticker"] == "AAA"
    assert (seen["min_dte"], seen["max_dte"]) == (33, 47)        # fetch wider; the gate decides exactly
    assert rc.rows[0]["strike"] == 75.0 and rc.ticker == "AAA" and rc.fetched_at_utc.tzinfo is not None
    assert (datetime.now(timezone.utc) - rc.fetched_at_utc) < timedelta(seconds=30)


def test_adapter_never_passes_broker_provider_or_configs(monkeypatch):
    import pandas as pd
    seen = {}
    s = stockpile_src()
    s._fetch_chain = lambda t, **kw: (seen.update(kw), pd.DataFrame())[1]
    s._fetch_earnings = lambda t: []
    s.fetch_chain("AAA", 35, 45)
    assert seen["provider"] == "yahoo" and not {"schwab_config", "moomoo_config"} & set(seen)


def test_adapter_empty_dataframe_gives_empty_rows_and_errors_are_classified():
    import pandas as pd
    s = stockpile_src()
    s._fetch_earnings = lambda t: []
    s._fetch_chain = lambda t, **kw: pd.DataFrame()
    assert s.fetch_chain("AAA", 35, 45).rows == []
    s._fetch_chain = lambda t, **kw: None
    assert s.fetch_chain("AAA", 35, 45).rows == []

    class YFRateLimitError(Exception):
        pass

    def rl(t, **kw):
        raise YFRateLimitError("Too Many Requests. Rate limited. Try after a while.")

    def bad(t, **kw):
        raise ConnectionError("x" * 1000 + " TELEGRAM_BOT_TOKEN=123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs")
    s._fetch_chain = rl
    with pytest.raises(SourceError) as e1:
        s.fetch_chain("AAA", 35, 45)
    assert e1.value.kind == "RATE_LIMIT"
    s._fetch_chain = bad
    with pytest.raises(SourceError) as e2:
        s.fetch_chain("AAA", 35, 45)
    assert e2.value.kind == "FETCH_ERROR" and len(str(e2.value)) < 300


def test_adapter_earnings_unknown_on_failure_empty_and_nonsense():
    import pandas as pd
    s = stockpile_src()
    s._fetch_chain = lambda *a, **k: None
    s._fetch_earnings = lambda t: (_ for _ in ()).throw(RuntimeError("boom"))
    assert s.fetch_earnings_dates("AAA") is None
    s._fetch_earnings = lambda t: []
    assert s.fetch_earnings_dates("AAA") is None
    s._fetch_earnings = lambda t: None
    assert s.fetch_earnings_dates("AAA") is None
    s._fetch_earnings = lambda t: ["garbage", 5]
    assert s.fetch_earnings_dates("AAA") is None
    s._fetch_earnings = lambda t: [pd.Timestamp("2026-12-01"), date(2026, 12, 2), datetime(2026, 12, 3, 12)]
    assert s.fetch_earnings_dates("AAA") == [date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3)]


def test_missing_checkout_is_a_clean_source_error(tmp_path):
    with pytest.raises(SourceError) as e:
        StockpileSource(tmp_path / "nope").fetch_chain("AAA", 35, 45)
    assert e.value.kind == "STOCKPILE_MISSING"


def test_adapter_load_in_clean_process_loads_no_broker_or_ui_module_and_no_network():
    """AT-16: import + initialize the adapter against the pinned checkout with sockets blocked; no schwab/moomoo/
    streamlit/trade modules may load, sys.path must be appended (not prepended), only chain+earnings upstream."""
    code = r"""
import socket, sys, json
def boom(*a, **k): raise RuntimeError("NETWORK ATTEMPT")
socket.socket.connect = boom; socket.create_connection = boom; socket.getaddrinfo = boom
before0 = sys.path[0]
from csp_alerts.market.stockpile import StockpileSource
from csp_alerts import boundary
pre = sorted(m for m in sys.modules if m.startswith(("options_scanner", "stocks_shared")))
s = StockpileSource(sys.argv[1]); s._load()
bad = boundary.loaded_forbidden_modules()
extra = [m for m in sys.modules if m.split('.')[0] in ('schwab','moomoo','futu','streamlit','requests_oauthlib','ib_insync')]
ups = sorted(m for m in sys.modules if m.startswith(("options_scanner", "stocks_shared")))
print(json.dumps({"pre": pre, "bad": bad, "extra": extra, "ups": ups, "path0_same": sys.path[0] == before0,
                  "tail": sys.path[-2:]}))
"""
    r = subprocess.run([sys.executable, "-I", "-c", code, str(REPO / "third_party" / "stockpile")], capture_output=True,
                       text=True, env={**{k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP")},
                                       "PYTHONPATH": str(REPO / "src")}, cwd=str(REPO))
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["pre"] == [], "importing the adapter module must not load any upstream module"
    assert out["bad"] == [] and out["extra"] == [], out
    assert any(m == "options_scanner.chain" for m in out["ups"]) and any(m == "options_scanner.earnings" for m in out["ups"])
    forbidden = ("trade_actions", "confirm_gate", "trades_store", "schwab_chain", "moomoo_chain", "tabs", "schwab_live")
    assert not [m for m in out["ups"] if any(f in m for f in forbidden)], out["ups"]
    assert out["path0_same"] is True


# ====================================================================== exchange calendar (XNYS)
def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@pytest.mark.parametrize("t,status,minutes,early", [
    # regular day Fri 2026-10-30, EDT: 09:30-16:00 ET = 13:30Z-20:00Z
    ("2026-10-30T13:29:59Z", "CLOSED", None, False),
    ("2026-10-30T13:30:00Z", "OPEN", 390, False),
    ("2026-10-30T19:39:59Z", "OPEN", 20, False),     # 20:01 remaining -> 20
    ("2026-10-30T19:40:00Z", "OPEN", 20, False),     # exactly 20:00 remaining
    ("2026-10-30T19:40:01Z", "OPEN", 19, False),     # 19:59 remaining
    ("2026-10-30T19:59:59Z", "OPEN", 0, False),
    ("2026-10-30T20:00:00Z", "CLOSED", None, False),
    # DST: first Monday after fall-back (EST, open 14:30Z) and after spring-forward (EDT, open 13:30Z)
    ("2026-11-02T14:29:59Z", "CLOSED", None, False), ("2026-11-02T14:30:00Z", "OPEN", 390, False),
    ("2026-03-09T13:29:59Z", "CLOSED", None, False), ("2026-03-09T13:30:00Z", "OPEN", 390, False),
    # DST-change weekend itself: closed
    ("2026-11-01T15:00:00Z", "CLOSED", None, False), ("2026-03-08T15:00:00Z", "CLOSED", None, False),
    # weekend, holidays (Thanksgiving, Christmas, Good Friday, observed Independence Day)
    ("2026-10-31T15:00:00Z", "CLOSED", None, False), ("2026-11-26T15:00:00Z", "CLOSED", None, False),
    ("2026-12-25T15:00:00Z", "CLOSED", None, False), ("2026-04-03T15:00:00Z", "CLOSED", None, False),
    ("2026-07-03T15:00:00Z", "CLOSED", None, False),
    # early closes: day after Thanksgiving and Christmas Eve close 13:00 ET (18:00Z both, EST)
    ("2026-11-27T17:39:59Z", "OPEN", 20, True), ("2026-11-27T17:40:00Z", "OPEN", 20, True),
    ("2026-11-27T17:40:01Z", "OPEN", 19, True), ("2026-11-27T18:00:00Z", "CLOSED", None, False),
    ("2026-12-24T17:45:00Z", "OPEN", 15, True),
])
def test_session_matrix(t, status, minutes, early):
    d = mcal.session_detail(utc(t))
    assert d.info.status.value == status and d.info.minutes_to_close == minutes and d.early_close is early, d


@pytest.mark.parametrize("t,allowed", [
    ("2026-10-30T19:39:59Z", True), ("2026-10-30T19:40:00Z", True), ("2026-10-30T19:40:01Z", False),
    ("2026-11-27T17:40:00Z", True), ("2026-11-27T17:40:01Z", False),
])
def test_cutoff_is_block_when_remaining_below_20_minutes(t, allowed):
    from csp_alerts.jobs.scan import _session_block
    assert (_session_block(mcal.session_info(utc(t)), POL) == []) is allowed


def test_calendar_failures_fail_closed_to_unknown():
    assert mcal.session_info(datetime(2026, 10, 30, 15, 0)).status is SessionStatus.UNKNOWN          # naive
    assert mcal.session_info(utc("2031-06-03T15:00:00Z")).status is SessionStatus.UNKNOWN            # outside bounds
    assert mcal.session_info(utc("1990-06-04T15:00:00Z")).status is SessionStatus.UNKNOWN

    class Broken:
        first_minute = property(lambda s: (_ for _ in ()).throw(RuntimeError("no dataset")))

    d = mcal.session_detail(NOW, calendar=Broken())
    assert d.info.status is SessionStatus.UNKNOWN and "unavailable" in d.reason


def test_missing_calendar_library_is_unknown(monkeypatch):
    monkeypatch.setattr(mcal, "_CAL", None)
    import builtins
    real = builtins.__import__

    def deny(name, *a, **k):
        if name.startswith("exchange_calendars"):
            raise ImportError("not installed")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", deny)
    assert mcal.session_info(NOW).status is SessionStatus.UNKNOWN
    monkeypatch.setattr(mcal, "_CAL", None)


def test_is_trading_day():
    assert mcal.is_trading_day(date(2026, 10, 30)) is True and mcal.is_trading_day(date(2026, 11, 27)) is True
    for d in (date(2026, 10, 31), date(2026, 11, 26), date(2026, 12, 25), date(2026, 7, 3), date(2026, 4, 3)):
        assert mcal.is_trading_day(d) is False, d
    assert mcal.is_trading_day(date(2031, 1, 2)) is None


def test_calendar_session_date_is_exchange_local():
    # 2026-11-02T04:30Z is Sunday night in New York; closed. 2026-11-02T14:30Z is Monday's open.
    assert mcal.session_detail(utc("2026-11-02T14:30:00Z")).session_date == date(2026, 11, 2)
