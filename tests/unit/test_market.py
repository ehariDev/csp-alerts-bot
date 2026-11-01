"""P0-05: calendar, normalization, Stockpile adapter (no network, synthetic rows only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from chain_fixtures import EXP, LAST_TRADE_MS, raw_row
from factories import NOW, TODAY

from csp_alerts.market import calendar as mcal
from csp_alerts.market.models import SessionStatus
from csp_alerts.market.normalize import earnings_in_window, normalize_rows, raw_sha256, to_count, to_decimal
from csp_alerts.market.stockpile import SourceError, StockpileSource

ROOT = Path(__file__).resolve().parents[2]
STOCKPILE = ROOT / "third_party" / "stockpile"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# ---- calendar ----
@pytest.mark.parametrize("when,status", [
    (utc(2026, 10, 30, 15, 5), SessionStatus.OPEN),        # Friday 11:05 ET
    (utc(2026, 10, 31, 15, 5), SessionStatus.CLOSED),      # Saturday
    (utc(2026, 11, 26, 16, 0), SessionStatus.CLOSED),      # Thanksgiving
    (utc(2026, 12, 25, 16, 0), SessionStatus.CLOSED),      # Christmas
    (utc(2026, 7, 3, 16, 0), SessionStatus.CLOSED),        # Independence Day observed
    (utc(2026, 10, 30, 11, 0), SessionStatus.CLOSED),      # pre-market 07:00 ET
    (utc(2026, 10, 30, 21, 0), SessionStatus.CLOSED),      # after close
])
def test_open_closed(when, status):
    assert mcal.session_info(when).status is status


def test_minutes_to_close_regular_day_edt():
    info = mcal.session_info(utc(2026, 10, 30, 19, 41))  # 15:41 EDT, close 16:00 -> 19 min
    assert info.status is SessionStatus.OPEN and info.minutes_to_close == 19
    assert mcal.session_info(utc(2026, 10, 30, 19, 40)).minutes_to_close == 20


def test_early_close_day_after_thanksgiving():
    d = mcal.session_detail(utc(2026, 11, 27, 17, 30))  # 12:30 EST; closes 13:00 ET = 18:00 UTC
    assert d.early_close and d.info.minutes_to_close == 30
    assert mcal.session_info(utc(2026, 11, 27, 18, 30)).status is SessionStatus.CLOSED
    assert mcal.session_detail(utc(2026, 11, 30, 17, 0)).early_close is False
    d2 = mcal.session_detail(utc(2026, 12, 24, 17, 0))  # Christmas Eve early close
    assert d2.early_close and d2.info.minutes_to_close == 60


def test_dst_open_boundaries_use_exchange_local_time_not_fixed_utc():
    # EST (2026-03-06): 09:30 ET = 14:30 UTC.  EDT (2026-03-09, after spring forward): 09:30 ET = 13:30 UTC.
    assert mcal.session_info(utc(2026, 3, 6, 14, 29)).status is SessionStatus.CLOSED
    assert mcal.session_info(utc(2026, 3, 6, 14, 30)).status is SessionStatus.OPEN
    assert mcal.session_info(utc(2026, 3, 9, 13, 29)).status is SessionStatus.CLOSED
    assert mcal.session_info(utc(2026, 3, 9, 13, 30)).status is SessionStatus.OPEN
    # fall back (2026-11-02 is EST again)
    assert mcal.session_info(utc(2026, 11, 2, 14, 29)).status is SessionStatus.CLOSED
    assert mcal.session_info(utc(2026, 11, 2, 14, 30)).status is SessionStatus.OPEN


def test_unknown_when_calendar_cannot_answer():
    assert mcal.session_info(datetime(2026, 10, 30, 15, 5)).status is SessionStatus.UNKNOWN       # naive
    assert mcal.session_info(utc(2060, 1, 5, 15, 0)).status is SessionStatus.UNKNOWN              # beyond bounds
    assert mcal.session_info(utc(1990, 1, 5, 15, 0)).status is SessionStatus.UNKNOWN

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("no dataset")

    assert mcal.session_info(NOW, calendar=Broken()).status is SessionStatus.UNKNOWN
    assert mcal.is_trading_day(date(2026, 11, 26)) is False and mcal.is_trading_day(date(2026, 11, 27)) is True


# ---- normalization ----
def norm(rows, **kw):
    kw.setdefault("today_exchange", TODAY)
    kw.setdefault("earnings_dates", None)
    kw.setdefault("multiplier", 100)
    return normalize_rows("EXAMPLE", rows, NOW, **kw)


def test_numeric_helpers():
    assert to_decimal(0.1) == D("0.1") and to_decimal("0.50") == D("0.50")
    for bad in (None, float("nan"), float("inf"), "x", True):
        assert to_decimal(bad) is None
    assert to_count(1000.0) == 1000 and to_count(5.5) is None and to_count(-1) is None and to_count(float("nan")) is None


def test_basic_put_row_maps_to_spec_6_1_observation():
    n = norm([raw_row()])
    o = n.rows[0].observation
    assert (o.underlying, o.option_right, o.multiplier) == ("EXAMPLE", "P", 100)
    assert o.strike == D("40.0") and o.bid == D("0.5") and o.ask == D("0.54") and o.delta == D("-0.13")
    assert o.open_interest == 1000 and o.daily_volume == 100 and o.expiration.isoformat() == EXP
    assert o.quote_at_utc is None and o.data_tier == "INDICATIVE_UNTIMESTAMPED" and o.provider == "YAHOO_VIA_STOCKPILE"
    assert o.retrieved_at_utc == NOW and o.iv_excess_pp is None
    j = o.to_dict()
    assert j["quote_at_utc"] is None and j["event_check"] == "UNKNOWN" and j["contract_key"].startswith("EXAMPLE|")
    from csp_alerts.market.models import MarketObservation
    assert MarketObservation.from_dict(j).to_dict() == j


def test_last_trade_ms_is_last_print_never_quote_time():
    o = norm([raw_row()]).rows[0].observation
    assert o.last_option_trade_at_utc == NOW - timedelta(hours=1) and o.quote_at_utc is None
    assert norm([raw_row(last_trade_ms=0.0)]).rows[0].observation.last_option_trade_at_utc is None
    assert norm([raw_row(last_trade_ms=float("nan"))]).rows[0].observation.last_option_trade_at_utc is None


def test_upstream_mid_and_last_fallback_are_never_used_as_quote():
    r = raw_row(bid=0.0, ask=0.0, mid=0.52, last=0.52)   # upstream fallback: mid taken from last trade
    o = norm([r]).rows[0].observation
    assert o.bid == 0 and o.ask == 0                      # 0 stays 0 -> gate says BAD_BID_ASK
    nan_row = raw_row(bid=float("nan"), ask=0.54)
    assert norm([nan_row]).rows[0].observation.bid is None


def test_calls_and_garbage_rows_are_counted_not_used():
    n = norm([raw_row(), raw_row(side="call"), raw_row(side="weird"), raw_row(strike=0.0), raw_row(exp="garbage")])
    assert len(n.rows) == 1 and n.calls_ignored == 1
    assert n.rejected == {"UNKNOWN_OPTION_TYPE": 1, "BAD_STRIKE": 1, "BAD_EXPIRATION": 1}
    assert n.rows_in == 5 and n.rows_seen_upstream is None


def test_upstream_dte_ignored_and_multiplier_not_assumed():
    r = raw_row(dte=1)  # host-date DTE is irrelevant; the gate recomputes from expiration
    o = norm([r], multiplier=None).rows[0].observation
    assert o.multiplier is None and o.expiration.isoformat() == EXP


def test_degraded_chain_detected():
    rows = [raw_row(strike=30.0 + i, bid=0.0, ask=0.0) for i in range(10)]
    assert norm(rows).degraded
    rows = [raw_row(strike=30.0 + i, bid=0.0, ask=0.0) for i in range(8)] + [raw_row(strike=50.0), raw_row(strike=51.0)]
    assert not norm(rows).degraded  # 80% < 85%
    # small chains are judged too: unusable quotes are DATA_UNAVAILABLE, never a "healthy" no-trade
    assert norm([raw_row(bid=0.0, ask=0.0)]).degraded
    assert norm([raw_row(strike=40.0, bid=0.0, ask=0.05), raw_row(strike=41.0, bid=0.0, ask=0.06)]).degraded   # every bid zero
    assert not norm([raw_row(strike=40.0, bid=0.0, ask=0.05), raw_row(strike=41.0)]).degraded                  # one usable row
    assert not norm([raw_row()]).degraded


def test_earnings_mapping_unknown_is_never_clear():
    e = TODAY + timedelta(days=40)
    assert earnings_in_window(None, e, TODAY) is None and earnings_in_window([], e, TODAY) is None
    assert earnings_in_window([TODAY + timedelta(days=10)], e, TODAY) is True
    assert earnings_in_window([e], e, TODAY) is True
    assert earnings_in_window([e + timedelta(days=1)], e, TODAY) is False
    assert norm([raw_row()], earnings_dates=[TODAY + timedelta(days=10)]).rows[0].observation.earnings_in_window is True
    assert norm([raw_row()]).rows[0].observation.earnings_in_window is None


def test_raw_hash_is_stable_and_sensitive():
    a, b = raw_row(), raw_row()
    assert raw_sha256(a) == raw_sha256(b) and len(raw_sha256(a)) == 64
    assert raw_sha256(a) != raw_sha256(raw_row(bid=0.51))
    n = norm([a])
    assert n.rows[0].raw_sha256 == raw_sha256(a)


def test_static_synthetic_fixture_loads():
    data = json.loads((ROOT / "tests" / "fixtures" / "synthetic_chains" / "puts_synthetic.json").read_text())
    assert "SYNTHETIC" in data["_comment"]
    n = norm(data["rows"])
    assert len(n.rows) == 3 and n.calls_ignored == 1


# ---- adapter ----
def test_missing_checkout_is_a_clean_source_error(tmp_path):
    with pytest.raises(SourceError) as e:
        StockpileSource(tmp_path).fetch_chain("EXAMPLE", 35, 45)
    assert e.value.kind == "STOCKPILE_MISSING"


class _FakeDF:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def _wired(fn_chain, fn_earn=lambda t: []):
    s = StockpileSource(ROOT)
    s._fetch_chain, s._fetch_earnings = fn_chain, fn_earn
    return s


def test_adapter_forces_yahoo_puts_and_widens_window_for_host_date_skew():
    seen = {}

    def fake(ticker, **kw):
        seen.update(kw, ticker=ticker)
        return _FakeDF([raw_row()])

    raw = _wired(fake).fetch_chain("EXAMPLE", 35, 45)
    assert seen["provider"] == "yahoo" and seen["opt_type"] == "puts"
    assert (seen["min_dte"], seen["max_dte"]) == (33, 47)
    assert len(raw.rows) == 1 and raw.rows_seen_upstream is None and raw.fetched_at_utc.tzinfo is not None


def test_adapter_classifies_errors_and_empty_chains():
    class RateLimitError(Exception):
        pass

    def boom(ticker, **kw):
        raise RateLimitError("429")

    with pytest.raises(SourceError) as e:
        _wired(boom).fetch_chain("X", 35, 45)
    assert e.value.kind == "RATE_LIMIT"

    def boom2(ticker, **kw):
        raise ValueError("no price")

    with pytest.raises(SourceError) as e:
        _wired(boom2).fetch_chain("X", 35, 45)
    assert e.value.kind == "FETCH_ERROR"
    assert _wired(lambda t, **kw: _FakeDF([])).fetch_chain("X", 35, 45).rows == []


def test_earnings_adapter_returns_none_when_unknown():
    import pandas as pd
    assert _wired(lambda *a, **k: None, lambda t: []).fetch_earnings_dates("X") is None

    def fail(t):
        raise RuntimeError("x")

    assert _wired(lambda *a, **k: None, fail).fetch_earnings_dates("X") is None
    got = _wired(lambda *a, **k: None, lambda t: [pd.Timestamp("2026-11-20"), date(2026, 12, 1)]).fetch_earnings_dates("X")
    assert got == [date(2026, 11, 20), date(2026, 12, 1)]


@pytest.mark.skipif(not (STOCKPILE / "options-scanner").is_dir(), reason="pinned Stockpile checkout not present")
def test_loading_real_adapter_never_loads_broker_or_presentation_modules():
    code = (
        "import sys\n"
        "from csp_alerts.market.stockpile import StockpileSource\n"
        "from csp_alerts import boundary\n"
        f"s = StockpileSource(r'{STOCKPILE}')\n"
        "s._load()\n"
        "assert callable(s._fetch_chain) and callable(s._fetch_earnings)\n"
        "bad = boundary.loaded_forbidden_modules()\n"
        "extra = [m for m in sys.modules if m.split('.')[0] in ('schwab','moomoo','streamlit','futu','ib_insync')]\n"
        "assert not bad and not extra, (bad, extra)\n"
        "print('CLEAN', sorted(m for m in sys.modules if m.startswith(('options_scanner','stocks_shared'))))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=120)
    # -I ignores PYTHONPATH, so pass src explicitly through sys.path in the child
    if "No module named 'csp_alerts'" in r.stderr:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0 and "CLEAN" in r.stdout, r.stderr[-800:]


def test_it004_symbol_rule():
    from csp_alerts.market.normalize import standard_symbol as ss
    assert ss("EXAMPLE261209P00040000", "EXAMPLE") is True
    assert ss(None, "EXAMPLE") is None and ss("", "EXAMPLE") is None
    assert ss("EXAMPLE1261209P00040000", "EXAMPLE") is False   # adjusted root digit suffix
    assert ss("OTHER261209P00040000", "EXAMPLE") is False       # root differs from ticker
    assert ss("garbage", "EXAMPLE") is False
    assert ss("BRKB261209P00400000", "BRK.B") is True
    n = norm([raw_row(contractSymbol="EXAMPLE1261209P00040000"), raw_row(contractSymbol="EXAMPLE261209P00040000")])
    assert [r.observation.multiplier for r in n.rows] == [None, 100] and n.rejected == {"NONSTANDARD_SYMBOL": 1}
