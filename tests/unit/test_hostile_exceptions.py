"""D-1: an exception whose __str__ raises / returns a non-str, or that carries a secret, must cost ONE symbol only and
must never put raw exception text into stored rows, alerts, run summaries, service-failure messages or stdout."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
from chain_fixtures import raw_row
from factories import NOW, TODAY, approval, policy, snap

from csp_alerts.jobs.monitor import run_monitor
from csp_alerts.jobs.scan import run_scan
from csp_alerts.jobs.ticker_check import check_ticker
from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.market import failures as F
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import RawChain, SourceError

P = policy()
OPEN = SessionInfo(SessionStatus.OPEN, 300)
SECRET = "SECRETTOKEN987654"


class RaisingStr(Exception):
    def __str__(self):
        raise RuntimeError("boom " + SECRET)


class NonStrStr(Exception):
    def __str__(self):  # type: ignore[override]
        return 12345  # type: ignore[return-value]


class LeakyStr(Exception):
    def __str__(self):
        return f"ConnectionError: token={SECRET} https://u:pw@h/x"


class HostileKind(SourceError):
    def __init__(self):
        super().__init__("FETCH_ERROR", "x")
        self.kind = RaisingStr()      # a kind that is not even a string


HOSTILE = [RaisingStr, NonStrStr, LeakyStr, HostileKind]
IDS = ["str-raises", "str-nonstr", "leaky", "hostile-kind"]


class Src:
    def __init__(self, chains=None, errors=None):
        self.chains, self.errors = chains or {}, errors or {}

    def fetch_chain(self, ticker, a, b):
        if ticker in self.errors:
            raise self.errors[ticker]
        return RawChain(ticker, list(self.chains.get(ticker, [])), NOW)

    def fetch_earnings_dates(self, ticker):
        return None


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "h.sqlite3", clock=lambda: NOW)
    jr.migrate()
    jr.confirm_baseline(D("100000"))
    jr.add_snapshot(snap(equity="100000", cash="60000", asof=NOW - timedelta(hours=1)))
    yield jr
    jr.close()


def appr(t):
    return approval(ticker=t, asset_type="ETF", sector=f"sec_{t}", correlation_bucket=f"b_{t}", max_buy_strike=D("100"),
                    standard_deliverable_confirmed_on=TODAY, events_cleared_through=TODAY + timedelta(days=400))


def dump(j) -> str:
    rows = []
    for t in ("audit_log", "run_status", "alert_outbox", "market_observations"):
        rows += [dict(r) for r in j.conn.execute(f"SELECT * FROM {t}")]
    return json.dumps(rows, default=str)


@pytest.mark.parametrize("factory", HOSTILE, ids=IDS)
def test_scan_hostile_exception_costs_one_symbol_and_is_canned(j, factory):
    res = run_scan(journal=j, policy=P, approved=[appr("AAA"), appr("BBB")], now_utc=NOW, session=OPEN, sleep=lambda s: None,
                   source=Src({"BBB": [raw_row()]}, {"AAA": factory()}))
    cov = {t: st for t, st, _ in res.coverage}
    assert cov["AAA"] == "UNAVAILABLE" and cov["BBB"] == "OK"                 # the other symbol still ran
    assert res.status in ("CANDIDATES", "DATA_UNAVAILABLE")
    (ticker, kind, msg), = res.data_issues
    assert ticker == "AAA" and kind in F.KNOWN_KINDS
    assert msg in {F.stored(c) for c in F.CODES}
    blob = dump(j) + json.dumps([m.text for m in res.messages]) + json.dumps(res.coverage)
    assert SECRET not in blob and "12345" not in blob and "boom" not in blob and "https://" not in blob


def test_scan_source_error_with_raw_text_is_canned_too(j):
    err = SourceError("FETCH_ERROR", f"ConnectionError token={SECRET} C:\\Users\\me\\x.log")
    res = run_scan(journal=j, policy=P, approved=[appr("AAA")], now_utc=NOW, session=OPEN, sleep=lambda s: None,
                   source=Src(errors={"AAA": err}))
    assert res.data_issues[0][2] == F.stored(F.NETWORK_ERROR)
    assert SECRET not in dump(j) + json.dumps([m.text for m in res.messages])


@pytest.mark.parametrize("factory", HOSTILE, ids=IDS)
def test_ticker_check_hostile_exception_is_canned(factory):
    rep = check_ticker("ZZZ", Src(errors={"ZZZ": factory()}), P, NOW, sleep=lambda s: None)
    assert rep.status == "UNAVAILABLE" and rep.unavailable in {F.stored(c) for c in F.CODES}


def _put(j, tid, ticker):
    j.create_draft(NewPosition(tid, ticker, TODAY + timedelta(days=40), D("40"), 100, 1, "sec", "bkt"))
    j.transition(tid, S.PENDING)
    j.transition(tid, S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.50"), opened_at_utc=NOW - timedelta(days=3),
                 fees_usd=D("0.65"))


@pytest.mark.parametrize("factory", HOSTILE, ids=IDS)
def test_monitor_hostile_exception_costs_one_symbol_and_is_canned(j, factory):
    _put(j, "T1", "AAA")
    _put(j, "T2", "BBB")
    j.add_snapshot(snap(equity="100000", cash="60000", asof=NOW, sid="s-now"))
    res = run_monitor(journal=j, policy=P, now_utc=NOW, session=OPEN,
                      source=Src({"BBB": [raw_row(strike=40.0, bid=0.10, ask=0.12)]}, {"AAA": factory()}))
    assert res.positions_checked == 2
    assert res.issues and all(i.startswith("AAA: ") and i[5:] in {F.stored(c) for c in F.CODES} for i in res.issues)
    assert any(m for m in res.messages)                                         # BBB / AAA still produced advice or DATA_ISSUE
    blob = json.dumps(res.issues) + json.dumps([m.text for m in res.messages]) + json.dumps(res.notes)
    assert SECRET not in blob and "12345" not in blob and "boom" not in blob


def test_classify_and_stored_for_never_raise_and_never_copy():
    for factory in HOSTILE:
        e = factory()
        out = F.stored_for(e)
        assert out in {F.stored(c) for c in F.CODES} and SECRET not in out
    assert F.stored_for(None) == F.stored(F.UNKNOWN_ERROR)
    assert F.safe_kind(RaisingStr()) == "FETCH_ERROR" and F.safe_kind("x" * 5000) == "FETCH_ERROR"
    assert F.safe_kind("RATE_LIMIT") == "RATE_LIMIT"


def test_from_exception_carries_a_code_and_canned_message_only():
    e = SourceError.from_exception(LeakyStr())
    assert e.code == F.NETWORK_ERROR and SECRET not in str(e) and F.CANNED[F.NETWORK_ERROR] in str(e)
    assert F.classify(e.kind, e) == F.NETWORK_ERROR            # the code survives re-classification


def test_service_failure_message_summary_and_stdout_carry_a_fixed_code_only(monkeypatch, tmp_path, capsys):
    """A job-level crash whose exception text holds a secret: the queued URGENT message, the run_status summary and
    stdout/stderr carry the fixed code only (and a hostile __str__ does not break the handler)."""
    import csp_alerts.jobs.scan as scanmod
    from csp_alerts import cli
    from csp_alerts.cli import main
    from test_monitor_status import cli_base

    seen = {}
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(cli, "_deliver", lambda args, s, jn, pol, msgs, label: seen.setdefault("msgs", list(msgs)) and 0)
    assert main(cli_base(tmp_path) + ["journal", "init"]) == 0

    class FakeStockpile:
        def __init__(self, *a, **k):
            pass
    import csp_alerts.market.stockpile as sp
    monkeypatch.setattr(sp, "StockpileSource", FakeStockpile)
    for factory in (LeakyStr, RaisingStr):
        monkeypatch.setattr(scanmod, "_run_scan", lambda **kw: (_ for _ in ()).throw(factory()))
        seen.clear()
        assert main(cli_base(tmp_path) + ["scan", "--dry-run"]) == 1
        out = capsys.readouterr()
        assert SECRET not in out.out + out.err and "FAIL scan: SERVICE_ERROR" in out.err
        (msg,) = seen["msgs"]
        assert SECRET not in msg.text and F.service_failure_text() in msg.text and "ConnectionError" not in msg.text
    jr = Journal(tmp_path / "db.sqlite3")
    rows = [dict(r) for r in jr.conn.execute("SELECT * FROM run_status WHERE status='SERVICE_FAILURE'")]
    jr.close()
    assert rows and all(SECRET not in json.dumps(r) and json.loads(r["summary_json"])["error"] == F.SERVICE_ERROR for r in rows)


def test_last_resort_cli_handler_tolerates_hostile_str(capsys):
    from csp_alerts.cli import _safe_error_text
    assert _safe_error_text(RaisingStr()) == "RaisingStr" and _safe_error_text(NonStrStr()) == "NonStrStr"
    assert SECRET not in _safe_error_text(SourceError("FETCH_ERROR", "token=" + SECRET))
