"""Independent acceptance for `ticker-check` (read-only pre-approval data report; DECISIONS D11 items 105-112).

Oracles come from CLAUDE.md, the spec entry gates, config/policy.yaml and the task rules, NOT from the implementation:
  policy: DTE 35..45, |delta| 0.10..0.15 (put delta negative), OI >= 500, volume >= 50, bid >= 0.20,
          spread <= 0.05 and <= 10% of mid, last option print age <= 1.5 days, strike < spot,
          per-ticker collateral cap 9,000.00, pilot cap 15,000.00, collateral = strike * 100.
Frozen clock: NOW = 2026-10-30T15:05:02Z (11:05 EDT = 10:05 CDT, open session); TODAY (exchange) = 2026-10-30.
Synthetic data only; the chain source is a fake, nothing touches the network or Telegram.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from acc_helpers import NOW, REPO, TODAY, approval, book, policy
from acc_helpers import OPEN_SESSION as _OPEN
from s2_helpers import FakeSource, raw_row as _raw_row
from s3_helpers import CHAT, TOKEN, run_cli, write_approved

from csp_alerts.jobs import ticker_check as tck
from csp_alerts.market.normalize import normalize_rows
from csp_alerts.market.stockpile import SourceError
from csp_alerts.policy.entry import evaluate_entry

T = "AAA"
CONTEXT = {"NOT_APPROVED", "LEDGER_STALE", "LEDGER_UNRECONCILED", "DRAWDOWN_PAUSE", "HARD_HALT", "MARKET_CLOSED",
           "UNKNOWN_SESSION"}
NAN = float("nan")


_DEF = object()


def row(dte=35, strike=75.0, spot=_DEF, **over):
    """Stockpile-shaped put row. Default spot = 100.0 for every row (so rows agree) unless overridden."""
    exp = (TODAY + timedelta(days=dte)).isoformat()
    spot = 100.0 if spot is _DEF else spot
    return _raw_row(NOW, strike=strike, spot=spot, expiration=exp, dte=dte, **over)


def control(**over):
    """A clean passing neighbour (strike 74) so single-row degraded-chain logic never hides the row under test."""
    over.setdefault("strike", 74.0)
    return row(**over)


class Src:
    def __init__(self, rows, earnings=None, err=None, earn_err=None):
        self.rows, self.earn, self.err, self.earn_err, self.calls = rows, earnings, err, earn_err, []

    def fetch_chain(self, ticker, min_dte, max_dte):
        self.calls.append((ticker, min_dte, max_dte))
        if self.err:
            raise self.err
        from csp_alerts.market.stockpile import RawChain
        return RawChain(ticker, list(self.rows), NOW, None, [])

    def fetch_earnings_dates(self, ticker):
        if self.earn_err:
            raise self.earn_err
        return self.earn


def check(rows=None, ticker=T, pol=None, **kw):
    src = rows if isinstance(rows, Src) else Src(rows or [], **kw)
    return tck.check_ticker(ticker, src, pol or policy(), NOW, session=_OPEN, sleep=lambda s: None)


def line(rep, strike, dte=35):
    got = [p for p in rep.puts if p.strike == D(str(strike)) and p.dte == dte]
    assert len(got) == 1, [(p.strike, p.dte) for p in rep.puts]
    return got[0]


def text(rep, **kw):
    return tck.render_text([rep], policy(), NOW, "America/Chicago", **kw)


# ------------------------------------------------------------------------------ 2. hand-computed gate boundaries
# Each case: overrides for the strike-75 row -> exact expected contract-gate code set.
NS = {"MISSING_MARKET_DATA", "NONSTANDARD_CONTRACT"}
GATE_CASES = [
    # delta band [0.10, 0.15] on |delta|, put delta must be negative
    ("delta_-0.10_edge_pass", dict(delta=-0.10), set()),
    ("delta_-0.15_edge_pass", dict(delta=-0.15), set()),
    ("delta_-0.0999_below", dict(delta=-0.0999), {"DELTA_OUT_OF_RANGE"}),
    ("delta_-0.1501_above", dict(delta=-0.1501), {"DELTA_OUT_OF_RANGE"}),
    ("delta_positive", dict(delta=0.12), {"DELTA_OUT_OF_RANGE"}),
    ("delta_missing_nan", dict(delta=NAN), {"MISSING_MARKET_DATA"}),
    ("delta_missing_none", dict(delta=None), {"MISSING_MARKET_DATA"}),
    # open interest >= 500, volume >= 50
    ("oi_500_edge_pass", dict(open_interest=500), set()),
    ("oi_499_fail", dict(open_interest=499), {"INSUFFICIENT_OI"}),
    ("vol_50_edge_pass", dict(volume=50), set()),
    ("vol_49_fail", dict(volume=49), {"INSUFFICIENT_VOLUME"}),
    ("oi_missing", dict(open_interest=None), {"MISSING_MARKET_DATA"}),
    # bid >= 0.20
    ("bid_0.20_ask_0.22_pass", dict(bid=0.20, ask=0.22), set()),          # spread .02, mid .21, .02/.21 = 9.52% <= 10%
    ("bid_0.19_ask_0.21_fail", dict(bid=0.19, ask=0.21), {"BAD_BID_ASK"}),  # bid below min; spread .02 = 10% of mid .20 ok
    # abs spread <= 0.05 AND <= 10% of mid
    ("spread_0.05_mid_0.50_both_edges_pass", dict(bid=0.475, ask=0.525), set()),  # .05 == .05 and .05 == 10% * .50
    ("spread_0.06_fail", dict(bid=0.47, ask=0.53), {"SPREAD_TOO_WIDE"}),
    ("frac_edge_bid_.38_ask_.42_pass", dict(bid=0.38, ask=0.42), set()),     # spread .04, mid .40, 10% = .04 exactly
    ("frac_over_bid_.38_ask_.43_fail", dict(bid=0.38, ask=0.43), {"SPREAD_TOO_WIDE"}),  # .05 > 10% * .405 = .0405
    # zero / missing / crossed bid
    ("bid_zero", dict(bid=0.0, ask=0.50), {"BAD_BID_ASK"}),
    ("bid_nan", dict(bid=NAN, ask=0.50), {"MISSING_MARKET_DATA", "BAD_BID_ASK"}),
    ("ask_none", dict(bid=0.50, ask=None), {"MISSING_MARKET_DATA", "BAD_BID_ASK"}),
    ("crossed_bid_.60_ask_.50", dict(bid=0.60, ask=0.50), {"BAD_BID_ASK"}),
    # last option print age <= 1.5 days = 129600 s
    ("last_print_exactly_1.5d_pass", dict(last_trade_ms=float(int((NOW - timedelta(seconds=129600)).timestamp() * 1000))), set()),
    ("last_print_1.5d_plus_1s_fail", dict(last_trade_ms=float(int((NOW - timedelta(seconds=129601)).timestamp() * 1000))), {"STALE_LAST_TRADE"}),
    ("last_print_missing", dict(last_trade_ms=None), {"STALE_LAST_TRADE"}),
    # contract symbol (plain OCC root = ticker, date, P, strike*1000)
    # a contradicting symbol makes the multiplier unknown: scan gate reports MISSING_MARKET_DATA + NONSTANDARD_CONTRACT
    ("symbol_ok", dict(contractSymbol="AAA261204P00075000"), set()),
    ("symbol_strike_contradicts", dict(contractSymbol="AAA261204P00080000"), NS),
    ("symbol_expiry_contradicts", dict(contractSymbol="AAA261211P00075000"), NS),
    ("symbol_digit_suffix_root", dict(contractSymbol="AAA1261204P00075000"), NS),
    ("symbol_other_root", dict(contractSymbol="ZZZ261204P00075000"), NS),
    ("symbol_call_letter", dict(contractSymbol="AAA261204C00075000"), NS),
]


@pytest.mark.parametrize("name,over,expected", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_gate_boundaries_exact_codes(name, over, expected):
    rep = check([row(**over), control()])
    assert rep.status == "OK"
    p = line(rep, 75)
    assert set(p.gate_reasons) == expected, (name, p.gate_reasons)
    assert p.passes is (not expected)
    # never a context code leaking as a contract failure
    assert not set(p.gate_reasons) & CONTEXT


@pytest.mark.parametrize("name,over,expected", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_same_codes_as_scan_gate_for_same_put(name, over, expected):
    """ticker-check must report exactly what evaluate_entry (the scan's gate) says for the same put, once approved."""
    raw = row(**over)
    rep = check([raw, control()])
    p = line(rep, 75)
    norm = normalize_rows(T, [raw], NOW, today_exchange=TODAY, earnings_dates=None, multiplier=100)
    d = evaluate_entry(norm.rows[0].observation, approval(ticker=T), book(), policy(), NOW, _OPEN)
    scan = {c.value for c in d.reason_codes}
    assert set(p.gate_reasons) == scan, (set(p.gate_reasons), scan)


def test_dte_edges_35_45_listed_34_46_excluded_and_counted():
    rows = [row(dte=d, strike=70.0 + i) for i, d in enumerate((34, 35, 45, 46))]
    rep = check(rows)
    assert sorted(p.dte for p in rep.puts) == [35, 45]
    assert rep.rows_outside_window == 2
    assert all(p.passes for p in rep.puts)
    # DTE 35 -> 2026-12-04 (Oct 31 = 1, Nov 30 = 31, Dec 4 = 35); DTE 45 -> 2026-12-14
    assert {str(p.expiration) for p in rep.puts} == {"2026-12-04", "2026-12-14"}


def test_fetch_requests_policy_window_and_never_more():
    src = Src([row()])
    check(src)
    assert src.calls == [(T, 35, 45)]  # adapter widens by its own margin; the report re-filters at 35..45


# ------------------------------------------------------------------------------ collateral / cap verdicts
@pytest.mark.parametrize("strike,verdict", [
    (90.00, tck.VERDICT_PASS),       # 90.00 * 100 = 9,000.00 <= 9,000 cap
    (90.01, tck.VERDICT_CAPS),       # 90.01 * 100 = 9,001.00  > 9,000 cap
    (89.99, tck.VERDICT_PASS),       # 8,999.00
    (150.00, tck.VERDICT_CAPS),      # 15,000.00 > per-ticker 9,000 (the lower cap binds)
    (150.01, tck.VERDICT_CAPS),
])
def test_cap_verdicts_exact(strike, verdict):
    rep = check([row(strike=strike, spot=strike + 10)])
    assert rep.status == "OK" and rep.passing_count == 1
    assert rep.puts[0].collateral == D(str(strike)) * 100
    assert rep.verdict == verdict


def test_cap_display_per_ticker_and_pilot_edges():
    t150 = text(check([row(strike=150.00, spot=210.0)]))
    assert "collateral $15,000.00 [OVER per-ticker cap $9,000; within pilot cap $15,000]" in t150
    t15001 = text(check([row(strike=150.01, spot=210.0)]))
    assert "collateral $15,001.00 [OVER per-ticker cap $9,000; OVER pilot cap $15,000]" in t15001
    t90 = text(check([row(strike=90.00, spot=110.0)]))
    assert "collateral $9,000.00 [within per-ticker cap $9,000; within pilot cap $15,000]" in t90
    t9001 = text(check([row(strike=90.01, spot=110.0)]))
    assert "collateral $9,001.00 [OVER per-ticker cap $9,000; within pilot cap $15,000]" in t9001


def test_pilot_cap_binds_when_it_is_the_lower_cap():
    """If the pilot cap were lower than the per-ticker cap, the verdict must use the LOWER one."""
    from csp_alerts.policy.config import parse_policy
    data = policy().model_dump(mode="json")
    data["pilot"]["max_deployed_put_collateral_usd"] = "8000.00"
    try:
        pol = parse_policy(data)
    except Exception as exc:  # validation may forbid it; then the pilot-binds path is unreachable by config
        pytest.skip(f"policy validation rejects pilot < per-ticker cap: {type(exc).__name__}")
    rep = check([row(strike=80.00, spot=95.0)], pol=pol)  # collateral 8,000 -> pass at the edge
    assert rep.verdict == tck.VERDICT_PASS
    rep = check([row(strike=80.01, spot=95.0)], pol=pol)  # 8,001 > 8,000
    assert rep.verdict == tck.VERDICT_CAPS


def test_verdict_precedence():
    # all fail gates AND over cap -> NO LIQUID (gates first)
    assert check([row(strike=95.0, open_interest=1)]).verdict == tck.VERDICT_NO_LIQUID
    # one passing but over cap, one failing under cap -> CAPS
    assert check([row(strike=95.0), row(strike=50.0, open_interest=1)]).verdict == tck.VERDICT_CAPS
    # a passing under-cap put wins even when the others fail or are over cap
    assert check([row(strike=95.0), row(strike=50.0, open_interest=1), row(strike=60.0)]).verdict == tck.VERDICT_PASS
    # fetch failure -> DATA UNAVAILABLE
    r = check(Src([], err=SourceError("FETCH_ERROR", "boom")))
    assert r.status == "UNAVAILABLE" and r.verdict == tck.VERDICT_DATA
    # rows only outside the window -> no put listed -> NO LIQUID (never PASS)
    r = check([row(dte=34), row(dte=46, strike=70.0)])
    assert r.verdict in (tck.VERDICT_NO_LIQUID, tck.VERDICT_DATA) and r.puts == []


def test_hand_computed_spread_and_collateral_fields():
    rep = check([row(strike=75.0, bid=0.50, ask=0.54), control()])
    p = line(rep, 75)
    # spread = .54 - .50 = .04 ; mid = .52 ; .04/.52 = 7.6923...% ; collateral = 75 * 100 = 7,500
    assert p.spread == D("0.04") and p.collateral == D("7500")
    assert D("0.0769") < p.spread_pct_of_mid < D("0.0770")


# ------------------------------------------------------------------------------ 3. fail-closed
def test_missing_underlying_price_never_invented():
    rep = check([row(spot=None), control(spot=None)])
    assert rep.spot is None and "UNAVAILABLE" in rep.spot_note
    assert rep.verdict != tck.VERDICT_PASS
    assert "Underlying last price: UNAVAILABLE" in text(rep)
    assert all("MISSING_MARKET_DATA" in p.gate_reasons for p in rep.puts)


def test_nan_and_zero_underlying_price_not_shown_as_a_price():
    for bad in (NAN, 0.0, -5.0):
        rep = check([row(spot=bad), control(spot=bad)])
        assert rep.spot is None and rep.verdict != tck.VERDICT_PASS, bad


def test_inconsistent_spot_across_rows_is_unavailable():
    rep = check([row(spot=80.0), control(spot=81.0)])
    assert rep.spot is None
    # fail-closed: no single underlying price can be shown, so the report must not claim WOULD PASS
    assert rep.verdict != tck.VERDICT_PASS, "price UNAVAILABLE yet verdict WOULD PASS GATES"


def test_fetch_error_empty_unknown_symbol():
    for src in (Src([], err=RuntimeError("socket exploded")), Src([], err=SourceError("RATE_LIMIT", "429"))):
        r = check(src)
        assert r.status == "UNAVAILABLE" and r.verdict == tck.VERDICT_DATA and r.puts == [] and r.spot is None
    r = check(Src([], err=ValueError("Could not fetch live price for ZZZZ")))
    assert r.status == "UNAVAILABLE" and "typo" in r.unavailable.lower() or "spelling" in r.unavailable.lower()
    r = check([])
    assert r.status == "UNAVAILABLE" and r.verdict in (tck.VERDICT_DATA, tck.VERDICT_NO_LIQUID) and r.puts == []


def test_all_bids_zero_is_unavailable_not_pass():
    r = check([row(bid=0.0, ask=0.5), row(strike=70.0, bid=0.0, ask=0.4)])
    assert r.verdict != tck.VERDICT_PASS and not any(p.passes for p in r.puts)


def test_retry_is_bounded_by_policy_provider_retries():
    src = Src([], err=SourceError("FETCH_ERROR", "x"))
    check(src)
    assert len(src.calls) == 1 + policy().market.provider_retries


def test_earnings_unavailable_is_never_a_guess():
    for earn in (None, []):
        rep = check([row(), control()], earnings=earn)
        assert rep.next_earnings is None
        assert "Next earnings: UNAVAILABLE" in text(rep)
    rep = check(Src([row(), control()], earn_err=RuntimeError("x")))
    assert rep.next_earnings is None and "Next earnings: UNAVAILABLE" in text(rep)


@pytest.mark.parametrize("edate,blocked", [
    (TODAY, True),                              # today is inside [today, expiry]
    (TODAY + timedelta(days=35), True),         # exactly on the expiration date 2026-12-04
    (TODAY + timedelta(days=36), False),        # day after expiry
    (TODAY + timedelta(days=10), True),
    (TODAY - timedelta(days=1), None),          # past date only: not a future earnings date
])
def test_earnings_inside_dte_window(edate, blocked):
    rep = check([row(), control()], earnings=[edate])
    p = line(rep, 75)
    if blocked is True:
        assert "EARNINGS_IN_WINDOW" in p.gate_reasons and not p.passes
        assert f"Next earnings: {edate.isoformat()}" in text(rep)
    elif blocked is False:
        assert "EARNINGS_IN_WINDOW" not in p.gate_reasons
        assert f"Next earnings: {edate.isoformat()}" in text(rep)
    else:
        # past-only date: the scan gate would see "today <= d" false -> not in window; report must not print it as upcoming
        assert rep.next_earnings is None


def test_nonstandard_root_blocks_that_put_and_text_shows_code():
    rep = check([row(contractSymbol="AAA1261204P00075000"), control()])
    t = text(rep)
    assert "NONSTANDARD_CONTRACT" in t and line(rep, 75).passes is False


def test_labels_provenance_banner_and_standard_deliverable_assumption():
    out = text(check([row(), control()]), all_strikes=True)
    lines = out.splitlines()
    assert set(lines[0]) == {"="} and set(lines[2]) == {"="}
    for ln in (lines[1], lines[-1]):
        low = ln.lower()
        assert "indicative" in low and re.search(r"verify at (your )?broker", low) and "not an approval" in low, ln
    assert "YAHOO_VIA_STOCKPILE" in out and "INDICATIVE_UNTIMESTAMPED" in out
    # retrieval time: 15:05:02Z == 10:05:02 CDT on 2026-10-30
    assert "Retrieved: 2026-10-30 15:05:02 UTC / 2026-10-30 10:05:02 CDT" in out
    assert "100-share" in out and "standard" in out.lower()
    assert "NOT_APPROVED" not in out
    assert not re.search(r"real-?time|live (quote|data|price)|verified (data|quote)|PASS_VERIFIED|exchange[- ]timestamp(ed)? quote",
                         out, re.I), out
    assert not re.search(r"\b(live|verified)\b", out, re.I), "output claims live/verified"
    assert out.count("Indicative research data") == 2


def test_banner_present_top_and_bottom_even_when_everything_fails():
    out = text(check(Src([], err=RuntimeError("x"))))
    lines = out.splitlines()
    assert "not an approval" in lines[1].lower() and "not an approval" in lines[-1].lower()
    assert "VERDICT: DATA UNAVAILABLE" in out


def test_asset_type_and_leverage_not_guessed():
    out = text(check([row(), control()]))
    assert re.search(r"Asset type: UNAVAILABLE", out)
    assert re.search(r"Leveraged/inverse: not checked", out)


# ------------------------------------------------------------------------------ 4. output safety
def cli(monkeypatch, tmp_path, args, chains, earnings=None, env=None):
    src = FakeSource(chains, earnings or {}, NOW)
    return run_cli(monkeypatch, tmp_path, ["ticker-check", *args], source=src, env=env), src


CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]")


@pytest.mark.parametrize("bad", [
    "AAA;rm -rf /", "$(whoami)", "AAA`id`", "AAA|BBB", "../etc/passwd", "AAA\x1b[31mRED", "AAA\x07", "A" * 40, "",
    "AAA BBB", "'; DROP TABLE x;--", "AAA\nBBB", "‮AAA", "AAA&&calc", "<script>x</script>", "1AAA", "-AAA", "AAA\r\nX: y",
])
def test_hostile_ticker_rejected_without_fetch_and_without_raw_control_chars(monkeypatch, tmp_path, bad):
    (res, src) = cli(monkeypatch, tmp_path, ["--ticker", bad], {})
    code, out, err = res
    assert src.calls == [], "invalid symbol reached the data source"
    assert code != 0
    assert "VERDICT: DATA UNAVAILABLE" in out or code == 2
    assert not CTRL.search(out + err), repr(out)
    assert "\x1b" not in out and "‮" not in out


def test_hostile_ticker_cannot_forge_verdict_line(monkeypatch, tmp_path):
    forged = "AAA\n  VERDICT: WOULD PASS GATES (research only)"
    (code, out, err), src = cli(monkeypatch, tmp_path, ["--ticker", forged], {})
    assert code == 1 and src.calls == []
    assert "WOULD PASS GATES" not in out, out


def test_hostile_ticker_json_is_valid_and_marked_invalid(monkeypatch, tmp_path):
    (code, out, err), src = cli(monkeypatch, tmp_path, ["--ticker", "AAA\x1b[31m", "--json"], {})
    obj = json.loads(out)
    assert code == 1 and src.calls == [] and obj["tickers"][0]["status"] == "INVALID"
    assert obj["tickers"][0]["verdict"] == "DATA UNAVAILABLE"


def test_hostile_provider_error_text_is_sanitised(monkeypatch, tmp_path):
    evil = SourceError("FETCH_ERROR", "bad \x1b[2J\x1b]0;pwned\x07 line1\r\nVERDICT: WOULD PASS GATES (research only)")
    (code, out, err), _ = cli(monkeypatch, tmp_path, ["--ticker", T], {T: evil})
    assert code == 1
    assert not CTRL.search(out), repr(out)
    # an attacker-controlled message must not be able to forge a verdict line
    assert out.count("WOULD PASS GATES") == 0


def test_many_tickers_are_bounded(monkeypatch, tmp_path):
    syms = [f"T{chr(65 + i % 26)}{chr(65 + i // 26)}" for i in range(200)]
    chains = {s: [row()] for s in syms}
    (code, out, err), src = cli(monkeypatch, tmp_path, [a for s in syms for a in ("--ticker", s)], chains)
    assert len(src.calls) <= 20 or code != 0, f"{len(src.calls)} network fetches in one invocation"


def test_duplicates_and_case_deduped(monkeypatch, tmp_path):
    (code, out, err), src = cli(monkeypatch, tmp_path, ["--ticker", "aaa", "--ticker", " AAA ", "--ticker", "AAA"],
                                {T: [row(), control()]})
    assert code == 0 and [c[0] for c in src.calls] == [T]


def test_json_money_exact_decimal_strings_no_floats(monkeypatch, tmp_path):
    def no_float(s):
        raise AssertionError(f"JSON float literal {s}")
    chains = {T: [row(strike=90.01, spot=100.0, bid=0.38, ask=0.42), control()]}
    (code, out, err), _ = cli(monkeypatch, tmp_path, ["--ticker", T, "--json"], chains)
    assert code == 0
    obj = json.loads(out, parse_float=no_float, parse_constant=no_float)  # whole stdout is exactly one JSON doc
    t = obj["tickers"][0]
    assert t["verdict"] == tck.VERDICT_PASS  # control row 74 -> 7,400 passes
    p = next(x for x in t["puts"] if D(x["strike"]) == D("90.01"))
    for k in ("strike", "bid", "ask", "delta", "spread", "collateral_usd", "spread_pct_of_mid"):
        assert isinstance(p[k], str), (k, p[k])
    assert D(p["collateral_usd"]) == D("9001")          # 90.01 * 100
    assert D(p["spread"]) == D("0.04")                  # .42 - .38
    assert D(p["bid"]) == D("0.38") and D(p["ask"]) == D("0.42")
    assert D(p["spread_pct_of_mid"]) == D("10")         # .04 / .40 = 10%
    assert isinstance(p["open_interest"], int) and isinstance(t["underlying_last_price"], str)
    assert D(t["underlying_last_price"]) == D("100")
    assert t["cap_per_ticker_usd"] in ("9000.00", "9000") and D(t["cap_pilot_usd"]) == D("15000")
    assert t["provider"] == "YAHOO_VIA_STOCKPILE" and t["data_tier"] == "INDICATIVE_UNTIMESTAMPED"
    assert t["retrieved_at_utc"].startswith("2026-10-30T15:05:02")
    assert "approval" in obj["banner"].lower()


def test_json_unavailable_has_nulls_not_numbers(monkeypatch, tmp_path):
    (code, out, err), _ = cli(monkeypatch, tmp_path, ["--ticker", T, "--json"], {T: RuntimeError("down")})
    t = json.loads(out)["tickers"][0]
    assert code == 1 and t["status"] == "UNAVAILABLE" and t["underlying_last_price"] is None and t["puts"] == []
    assert t["verdict"] == "DATA UNAVAILABLE" and t["next_earnings"] is None


def test_exit_codes(monkeypatch, tmp_path):
    ok = [row(), control()]
    assert cli(monkeypatch, tmp_path, ["--ticker", T], {T: ok})[0][0] == 0
    (c, out, _), _ = cli(monkeypatch, tmp_path, ["--ticker", T, "--ticker", "BBB"], {T: ok, "BBB": RuntimeError("x")})
    assert c == 1 and "BBB" in out and "VERDICT: DATA UNAVAILABLE" in out
    assert cli(monkeypatch, tmp_path, ["--ticker", "bad sym"], {})[0][0] == 1
    # missing required --ticker is a usage error, never 0
    assert cli(monkeypatch, tmp_path, [], {})[0][0] == 2


def test_cli_text_end_to_end_banner_and_session_note(monkeypatch, tmp_path):
    (code, out, err), _ = cli(monkeypatch, tmp_path, ["--ticker", T], {T: [row(), control()]})
    assert code == 0 and "Market session now:" in out and out.rstrip().splitlines()[-1].lower().count("not an approval") == 1
    assert "VERDICT: WOULD PASS GATES (research only)" in out
    assert not CTRL.search(out)
    assert TOKEN not in out + err


# ------------------------------------------------------------------------------ 1. read-only guarantees
def _snapshot(d: Path):
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(d).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_size)
    return out


def test_no_journal_present_none_created_and_nothing_else_written(monkeypatch, tmp_path):
    before_cwd = _snapshot(REPO / "config")
    (code, out, err), _ = cli(monkeypatch, tmp_path, ["--ticker", T], {T: [row(), control()]})
    assert code == 0
    assert not (tmp_path / "j.sqlite3").exists()
    assert not list(tmp_path.glob("*.sqlite3*")) and not list(tmp_path.glob("*-wal")) and not list(tmp_path.glob("*-shm"))
    assert {p.name for p in tmp_path.iterdir()} == {"t.env", "approved.json"}  # created by the test helper only
    assert _snapshot(REPO / "config") == before_cwd  # shipped whitelist/policy untouched


def test_existing_journal_and_whitelist_byte_identical(monkeypatch, tmp_path):
    from s2_helpers import make_journal  # in-memory builder; copy to disk instead
    from acc_helpers import snap
    from csp_alerts.journal.store import Journal
    db = tmp_path / "j.sqlite3"
    j = Journal(db, clock=lambda: NOW)
    j.migrate()
    j.confirm_baseline(D("100000"))
    j.add_snapshot(snap())
    j.conn.commit()
    j.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    j.conn.close()
    appr_path = write_approved(tmp_path / "approved.json", tickers=("BBB",))
    tables = []
    ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    tables = [r[0] for r in ro.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before_counts = {t: ro.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    ro.close()
    before = _snapshot(tmp_path)
    src = FakeSource({T: [row(), control()]}, {}, NOW)
    code, out, err = run_cli(monkeypatch, tmp_path, ["ticker-check", "--ticker", T, "--ticker", "BBB"], source=src,
                             approved=appr_path,
                             env={"CSP_DRY_RUN": "false", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
    after = _snapshot(tmp_path)
    assert code == 1  # BBB not in FakeSource -> KeyError is a fetch failure (UNAVAILABLE), exit 1
    ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    after_counts = {t: ro.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    ro.close()
    assert after_counts == before_counts
    assert {k: v for k, v in after.items() if k != "t.env"} == {k: v for k, v in before.items() if k != "t.env"}, \
        "a file under the data dir changed"
    assert TOKEN not in out + err and CHAT not in out + err


def test_cannot_send_even_with_live_env_and_token(monkeypatch, tmp_path):
    import csp_alerts.runner as runner
    import csp_alerts.alerts.telegram as tg

    def boom(*a, **k):
        raise AssertionError("transport touched by ticker-check")
    monkeypatch.setattr(runner, "make_transport", boom)
    monkeypatch.setattr(tg.TelegramTransport, "__init__", boom)
    (code, out, err), _ = cli(monkeypatch, tmp_path, ["--ticker", T], {T: [row(), control()]},
                              env={"CSP_DRY_RUN": "false", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
    assert code == 0 and TOKEN not in out + err
    for flag in ("--send", "--live", "--approve", "--place", "--write", "--dry-run=false"):
        (c, o, e), _ = cli(monkeypatch, tmp_path, ["--ticker", T, flag], {T: [row()]})
        assert c == 2, flag


def test_module_and_handler_have_no_io_beyond_the_adapter():
    mod = REPO / "src" / "csp_alerts" / "jobs" / "ticker_check.py"
    tree = ast.parse(mod.read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
    banned = ("socket", "urllib", "http", "requests", "httpx", "yfinance", "sqlite3", "ssl", "subprocess",
              "csp_alerts.alerts", "csp_alerts.runner", "csp_alerts.journal.store", "csp_alerts.ledger_cli", "os", "shutil")
    assert not [i for i in imported if i.split(".")[0] in banned or any(i.startswith(b) for b in banned if "." in b)], imported
    src = mod.read_text(encoding="utf-8")
    code_only = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"\bopen\(|write_text|write_bytes|\.execute\(|INSERT|UPDATE\s|DELETE\s|\.commit\(|approve|enqueue|sendMessage",
                         re.sub(r'""".*?"""', "", code_only, flags=re.S))
    cli_src = (REPO / "src" / "csp_alerts" / "cli.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(cli_src).body if isinstance(n, ast.FunctionDef) and n.name == "cmd_ticker_check")
    fn.body = fn.body[1:]  # drop the docstring (it says "no journal, no outbox")
    body = ast.unparse(fn)
    assert not re.search(r"_journal\(|Journal|runner|outbox|transport|single_instance|approved|\.open\(|write", body), body


def test_advisory_only_no_order_placement_anywhere_in_src():
    pat = re.compile(r"(place|submit|create|cancel|modify|replace|send)_?order|buy_to_close|sell_to_open|\.place_trade|execute_trade",
                     re.I)
    hits = []
    for p in (REPO / "src").rglob("*.py"):
        if p.name == "boundary.py":
            continue  # the forbidden-name list itself
        for i, l in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            s = l.strip()
            if pat.search(l) and not s.startswith("#") and "can_send_orders" not in l:  # policy flag pinned to False
                hits.append(f"{p.relative_to(REPO)}:{i}: {s[:100]}")
    assert not hits, hits


def test_network_modules_not_used_by_new_code_only_known_files():
    allowed = {"alerts/telegram.py", "boundary.py"}  # telegram transport + dashboard are pre-existing, reviewed elsewhere
    pat = re.compile(r"^\s*(import|from)\s+(socket|urllib|http|requests|httpx|yfinance|aiohttp|ssl|ftplib|smtplib|websocket)\b", re.M)
    found = {p.relative_to(REPO / "src" / "csp_alerts").as_posix() for p in (REPO / "src" / "csp_alerts").rglob("*.py")
             if pat.search(p.read_text(encoding="utf-8"))}
    assert found <= allowed, found
    assert "jobs/ticker_check.py" not in found and "cli.py" not in found


def test_cli_surface_for_ticker_check():
    from csp_alerts.cli import build_parser
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    assert "ticker-check" in sub.choices
    opts = {o for a in sub.choices["ticker-check"]._actions for o in a.option_strings}
    assert opts == {"-h", "--help", "--ticker", "--all-strikes", "--json"}
    assert not re.search(r"place|close|roll|sync|trade|order|execute|submit|live|broker|send|approve", "ticker-check")
