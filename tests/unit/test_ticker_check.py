"""ticker-check: read-only pre-approval report. Synthetic fixtures only; no network."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest
from chain_fixtures import EXP, raw_row
from factories import NOW, OPEN_SESSION, TODAY, policy

from csp_alerts.cli import main
from csp_alerts.jobs import ticker_check as tck
from csp_alerts.market.stockpile import RawChain, SourceError

ROOT = tck.__file__


class Src:
    def __init__(self, rows=None, err=None, earnings=None):
        self.rows, self.err, self.earnings, self.calls = rows, err, earnings, 0

    def fetch_chain(self, ticker, min_dte, max_dte):
        self.calls += 1
        if self.err:
            raise self.err
        return RawChain(ticker, self.rows, NOW)

    def fetch_earnings_dates(self, ticker):
        return self.earnings


def check(src, ticker="EXAMPLE"):
    return tck.check_ticker(ticker, src, policy(), NOW, session=OPEN_SESSION, sleep=lambda s: None)


def test_pass_verdict_and_fields():
    r = check(Src([raw_row(strike=40.0, spot=48.0)]))
    assert r.status == "OK" and r.verdict == tck.VERDICT_PASS
    p = r.puts[0]
    assert p.collateral == 4000 and p.passes and p.in_band and p.gate_reasons == ()
    assert r.spot == 48 and r.chain_exists and r.expirations[0][1] == 40
    assert r.next_earnings is None and "owner must verify" in r.earnings_note
    assert r.band_nearest is p and r.lowest_strike is p
    assert r.data_tier == "INDICATIVE_UNTIMESTAMPED"


def test_context_codes_never_shown_as_contract_failures():
    r = check(Src([raw_row()]))
    assert not set(r.puts[0].gate_reasons) & {c.value for c in tck.CONTEXT_CODES}


def test_gate_reasons_match_scan_gate():
    r = check(Src([raw_row(oi=10, vol=1, bid=0.05, ask=0.50, delta=-0.40)]))
    codes = set(r.puts[0].gate_reasons)
    assert {"INSUFFICIENT_OI", "INSUFFICIENT_VOLUME", "BAD_BID_ASK", "SPREAD_TOO_WIDE", "DELTA_OUT_OF_RANGE"} <= codes
    assert r.verdict == tck.VERDICT_NO_LIQUID


def test_over_cap_verdict():
    # strike 100 -> collateral 10,000 > 9,000 per-ticker cap although liquid
    r = check(Src([raw_row(strike=100.0, spot=120.0, bid=0.50, ask=0.54)]))
    assert r.puts[0].passes and r.verdict == tck.VERDICT_CAPS
    text = tck.render_text([r], policy(), NOW, "America/Chicago")
    assert "OVER per-ticker cap" in text and "within pilot cap" in text


def test_earnings_in_window_blocks_and_next_date_shown():
    r = check(Src([raw_row()], earnings=[TODAY + timedelta(days=10)]))
    assert r.next_earnings == TODAY + timedelta(days=10)
    assert "EARNINGS_IN_WINDOW" in r.puts[0].gate_reasons and r.verdict == tck.VERDICT_NO_LIQUID


def test_rows_outside_window_excluded():
    far = (TODAY + timedelta(days=60)).isoformat()
    r = check(Src([raw_row(), raw_row(exp=far)]))
    assert len(r.puts) == 1 and r.rows_outside_window == 1


def test_no_expiry_in_window():
    far = (TODAY + timedelta(days=60)).isoformat()
    r = check(Src([raw_row(exp=far)]))
    assert r.verdict == tck.VERDICT_NO_LIQUID and "no listed expiry" in r.verdict_detail


def test_missing_values_stay_unavailable():
    r = check(Src([raw_row(strike=41.0, spot=48.0), raw_row(spot=48.0, bid=float("nan"), ask=float("nan"), delta=float("nan"), oi=float("nan"))]))
    text = tck.render_text([r], policy(), NOW, "America/Chicago", all_strikes=True)
    p = r.puts[0]  # sorted by strike: the NaN row (strike 40)
    assert p.bid is None and p.delta is None and p.open_interest is None
    assert "UNAVAILABLE" in text


def test_fetch_failure_unknown_symbol_and_network():
    r = check(Src(err=SourceError("FETCH_ERROR", "ValueError: Could not fetch live price for ZZZ")), "ZZZ")
    assert r.status == "UNAVAILABLE" and r.verdict == tck.VERDICT_DATA and "typo" in r.unavailable
    s = Src(err=SourceError("RATE_LIMIT", "x"))
    r2 = check(s)
    assert s.calls == 1 and "throttling" in r2.unavailable


def test_empty_chain_and_invalid_symbol():
    r = check(Src([]))
    assert r.status == "UNAVAILABLE" and r.chain_exists is False
    s = Src([raw_row()])
    bad = check(s, "bad sym!")
    assert bad.status == "INVALID" and s.calls == 0


def test_degraded_chain_is_unavailable():
    rows = [raw_row(bid=0.0, ask=0.0) for _ in range(3)]
    assert check(Src(rows)).status == "UNAVAILABLE"


def test_render_banner_and_not_checked_text():
    r = check(Src([raw_row()]))
    text = tck.render_text([r], policy(), NOW, "America/Chicago")
    assert text.count(tck.BANNER) == 2 and "not checked; verify yourself" in text
    assert "UTC /" in text and "CDT" in text or "CST" in text


def test_json_is_decimal_strings():
    r = check(Src([raw_row()]))
    obj = tck.to_json_obj([r], NOW)
    json.dumps(obj)
    assert obj["tickers"][0]["puts"][0]["collateral_usd"] in ("4000", "4000.0")
    assert obj["tickers"][0]["verdict"] == tck.VERDICT_PASS


def test_no_float_in_module_money_paths():
    import ast
    t = ast.parse(open(tck.__file__, encoding="utf-8").read())
    assert not [n for n in ast.walk(t) if isinstance(n, ast.Constant) and isinstance(n.value, float)]


def test_cli_registered_read_only_no_send(tmp_path, monkeypatch, capsys):
    from csp_alerts.cli import build_parser
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    opts = {o for a in sub.choices["ticker-check"]._actions for o in a.option_strings}
    assert "--send" not in opts and "--ticker" in opts

    env = tmp_path / ".env"
    env.write_text(f"CSP_DATABASE_URL=sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}\n", encoding="utf-8")
    import csp_alerts.market.stockpile as sp
    monkeypatch.setattr(sp.StockpileSource, "fetch_chain", lambda self, t, a, b: RawChain(t, [raw_row()], NOW))
    monkeypatch.setattr(sp.StockpileSource, "fetch_earnings_dates", lambda self, t: None)
    rc = main(["--env-file", str(env), "--policy", "config/policy.yaml", "ticker-check", "--ticker", "abc", "--ticker", "ABC"])
    out = capsys.readouterr().out
    assert rc == 0 and out.count("\nABC\n") == 1 and "Not an approval" in out
    assert not (tmp_path / "db.sqlite3").exists()  # no journal created or written
    rc = main(["--env-file", str(env), "--policy", "config/policy.yaml", "ticker-check", "--ticker", "ABC", "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["tickers"][0]["ticker"] == "ABC"


class SrcExp(Src):
    def __init__(self, rows, exps, **kw):
        super().__init__(rows, **kw)
        self.exps = exps

    def fetch_expirations(self, ticker):
        return self.exps


def test_monthly_only_shows_nearest_expiries_instead_of_outage():
    exps = [TODAY + timedelta(days=21), TODAY + timedelta(days=49)]
    r = check(SrcExp([], exps))
    assert r.status == "OK" and r.verdict == tck.VERDICT_NO_LIQUID
    assert r.nearest_before[1] == 21 and r.nearest_after[1] == 49
    text = tck.render_text([r], policy(), NOW, "America/Chicago")
    assert "DTE 21" in text and "DTE 49" in text and "indicative" in text


def test_empty_chain_without_expiry_list_names_both_meanings():
    r = check(SrcExp([], None))
    assert r.status == "UNAVAILABLE" and "outage" in r.unavailable and "no listed expiry" in r.unavailable


def test_inconsistent_spot_is_not_a_pass():
    r = check(Src([raw_row(spot=48.0), raw_row(strike=41.0, spot=49.0)]))
    assert r.status == "UNAVAILABLE" and r.verdict == tck.VERDICT_DATA and r.spot is None


def test_earnings_unverified_flag_on_pass():
    r = check(Src([raw_row(strike=40.0, spot=48.0)]))
    assert r.verdict == tck.VERDICT_PASS and tck.EARNINGS_FLAG in r.verdict_detail




def test_safe_text_neutralises_controls_and_verdicts():
    s = tck.safe_text("a\x1b[2J\r\nVERDICT: WOULD PASS GATES (research only)\u202e")
    assert "\x1b" not in s and "\n" not in s and "WOULD PASS" not in s and "\u202e" not in s
