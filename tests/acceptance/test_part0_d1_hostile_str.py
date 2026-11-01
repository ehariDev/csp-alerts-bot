"""Part 0 / D-1: provider exceptions with a hostile __str__ (raises / returns non-str) and a hostile __repr__ never crash
market-refresh, ticker-check, scan or monitor; later symbols are still processed; no raw exception text (a distinctive canary
token) appears in stored rows (whole SQLite dump), outbox/Telegram bodies, run summaries, CLI stdout/stderr, or the
STOCKPILE_MISSING message (which must not print the checkout path either). Synthetic data, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from acc_helpers import NOW, POLICY_PATH, policy
from s2_helpers import FakeSource, raw_row
from s3_helpers import chain_rows_for, dump_text, ledger, run_cli, write_approved
from stage_a_helpers import Prov, add_wl, jopen, md_dump, refresh
from stage_a_env import plain_db

from csp_alerts.market.stockpile import SourceError, StockpileSource
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.policy.approved import load_approved

CANARY = "ZQXJ-CANARY-7731"
OPEN_S = SessionInfo(SessionStatus.OPEN, 300)


class StrRaises(Exception):
    def __str__(self):
        raise RuntimeError(CANARY)

    def __repr__(self):
        raise RuntimeError(CANARY)


class StrNonStr(Exception):
    def __str__(self):
        return 12345  # type: ignore[return-value]

    def __repr__(self):
        return 777  # type: ignore[return-value]


class StrCanaryOk(Exception):
    """__str__ is fine but carries the canary; still must never be stored."""
    def __str__(self):
        return "connection reset " + CANARY


HOSTILE = [StrRaises, StrNonStr, StrCanaryOk]
IDS = ["str-raises", "str-nonstr", "str-has-canary"]


@pytest.mark.parametrize("cls", HOSTILE, ids=IDS)
def test_market_refresh_survives_and_continues(tmp_path, cls):
    db = plain_db(tmp_path)
    add_wl(db, "AAA", "BBB")
    p = Prov(NOW)
    p.script["AAA"] = cls(CANARY)
    p.set("BBB")
    res = refresh(db, p, NOW)
    assert next(o for o in res.outcomes if o.symbol == "BBB").attempted
    j = jopen(db)
    assert CANARY not in dump_text(j) and CANARY not in json.dumps(md_dump(db))
    j.close()
    assert CANARY not in repr(res.outcomes) + repr(res.notes)


@pytest.mark.parametrize("cls", HOSTILE, ids=IDS)
def test_scan_survives_continues_and_leaks_nothing(tmp_path, cls):
    j, _ = ledger()
    appr = load_approved(write_approved(tmp_path / "a.json", ("AAA", "BBB")))
    src = FakeSource({"AAA": cls(CANARY), "BBB": [raw_row(spot=80.0)]})
    from csp_alerts.jobs.scan import run_scan
    res = run_scan(journal=j, policy=policy(), approved=appr, source=src, now_utc=NOW, session=OPEN_S, sleep=lambda s: None)
    assert any(c[0] == "BBB" for c in src.calls), "later symbol must still be fetched"
    blob = "\n".join(m.text for m in res.messages) + repr(res.coverage) + repr(res.data_issues) + dump_text(j)
    assert CANARY not in blob
    assert any(t == "AAA" for t, *_ in res.coverage)         # the failed symbol is reported as unavailable, not dropped


@pytest.mark.parametrize("cls", HOSTILE, ids=IDS)
def test_monitor_survives_continues_and_leaks_nothing(cls):
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00"), dict(trade_id="P2", ticker="BBB", strike="75.00")])
    from datetime import date
    from csp_alerts.jobs.monitor import run_monitor
    src = FakeSource({"AAA": cls(CANARY), "BBB": chain_rows_for(75.0, date(2026, 12, 4), 0.10, 0.15)})
    res = run_monitor(journal=j, policy=policy(), source=src, now_utc=NOW, session=OPEN_S)
    assert [c[0] for c in src.calls].count("BBB") >= 1, "later position must still be evaluated"
    assert any(t.trade_id == "P2" for t, _ in [(p, s) for p, s in res.signals]), "BBB profit exit must fire (ask 0.15 <= 0.20)"
    blob = "\n".join(m.text for m in res.messages) + repr(res.issues) + repr(res.notes) + dump_text(j)
    assert CANARY not in blob


@pytest.mark.parametrize("cls", HOSTILE, ids=IDS)
def test_ticker_check_survives(cls):
    from csp_alerts.jobs.ticker_check import check_ticker, render_text, to_json_obj

    class Src:
        def fetch_chain(self, t, lo, hi):
            raise cls(CANARY)

        def fetch_earnings_dates(self, t):
            return None
    rep = check_ticker("ZZZ", Src(), policy(), NOW, sleep=lambda s: None)
    assert rep.status == "UNAVAILABLE"
    assert CANARY not in render_text([rep], policy(), NOW, "America/Chicago") + json.dumps(to_json_obj([rep], NOW))


@pytest.mark.parametrize("cls", HOSTILE, ids=IDS)
def test_cli_scan_and_monitor_stdout_stderr_and_run_status(monkeypatch, tmp_path, cls):
    db = tmp_path / "j.sqlite3"
    j = jopen(db, NOW)
    j.migrate()
    j.close()
    for cmd in (["scan", "--dry-run"], ["monitor", "--dry-run"], ["market-refresh"], ["ticker-check", "--ticker", "ZZZ"]):
        src = FakeSource({"ZZZ": cls(CANARY), "AAA": cls(CANARY)})
        code, out, err = run_cli(monkeypatch, tmp_path, cmd, now=NOW, source=src)
        assert CANARY not in out + err, (cmd, out, err)
        assert "Traceback" not in out + err, (cmd, err)
    j = jopen(db, NOW)
    assert CANARY not in dump_text(j)
    j.close()


def test_stockpile_missing_message_is_canned_and_prints_no_path(tmp_path):
    secret_dir = tmp_path / "hidden-dir-QQ91"
    src = StockpileSource(secret_dir)
    with pytest.raises(SourceError) as ei:
        src.fetch_chain("AAA", 35, 45)
    assert "hidden-dir-QQ91" not in str(ei.value) and str(tmp_path) not in str(ei.value)
    with pytest.raises(SourceError) as ei2:
        src.fetch_earnings_dates("AAA")
    assert str(tmp_path) not in str(ei2.value)
    assert "STOCKPILE_MISSING" in str(ei.value) or getattr(ei.value, "code", None)
