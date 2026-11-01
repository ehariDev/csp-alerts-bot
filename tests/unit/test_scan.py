"""P0-06: entry scan, ranking, revalidation, digest, templates, dry-run (synthetic fixtures; no network)."""
from __future__ import annotations

import socket
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from chain_fixtures import EXP, raw_row
from factories import NOW, TODAY, approval, policy, snap

from csp_alerts.alerts import templates
from csp_alerts.jobs.scan import run_scan
from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import RawChain, SourceError

P = policy()
OPEN = SessionInfo(SessionStatus.OPEN, 300)


class FakeSource:
    def __init__(self, chains=None, errors=None, earnings=None):
        self.chains, self.errors, self.earn = chains or {}, errors or {}, earnings
        self.calls = []

    def fetch_chain(self, ticker, min_dte, max_dte):
        self.calls.append(ticker)
        e = self.errors.get(ticker)
        if e is not None:
            raise e
        return RawChain(ticker, list(self.chains.get(ticker, [])), NOW)

    def fetch_earnings_dates(self, ticker):
        return self.earn


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "s.sqlite3", clock=lambda: NOW)
    jr.migrate()
    jr.confirm_baseline(D("100000"))
    jr.add_snapshot(snap(equity="100000", cash="60000", asof=NOW - timedelta(hours=1)))
    yield jr
    jr.close()


def appr(t, sector=None, bucket=None, typ="ETF", **kw):
    base = dict(ticker=t, asset_type=typ, sector=sector or f"sec_{t}", correlation_bucket=bucket or f"b_{t}",
                max_buy_strike=D("100"), standard_deliverable_confirmed_on=TODAY,
                events_cleared_through=TODAY + timedelta(days=400))
    base.update(kw)
    return approval(**base)


def scan(j, approved, src, session=OPEN, pol=P, **kw):
    return run_scan(journal=j, policy=pol, approved=approved, source=src, now_utc=NOW, session=session,
                    sleep=lambda s: None, **kw)


def count(j, table):
    return j.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---- fail-closed pre-gates ----
def test_empty_whitelist_gives_no_trade_digest_and_no_fetch(j):
    src = FakeSource()
    r = scan(j, [], src)
    assert r.status == "NO_TRADE" and r.reason_counts["EMPTY_WHITELIST"] == 1 and src.calls == []
    assert r.messages[0].kind == "NO_TRADE_DIGEST" and "EMPTY_WHITELIST" in r.messages[0].text


@pytest.mark.parametrize("session,code", [
    (SessionInfo(SessionStatus.CLOSED), "MARKET_CLOSED"), (SessionInfo(SessionStatus.UNKNOWN), "UNKNOWN_SESSION"),
    (SessionInfo(SessionStatus.OPEN, 19), "MARKET_CLOSED"),
])
def test_session_blocks_before_any_fetch(j, session, code):
    src = FakeSource({"AAA": [raw_row()]})
    r = scan(j, [appr("AAA")], src, session=session)
    assert r.status == "BLOCKED" and code in r.reason_counts and src.calls == [] and not r.candidates


def test_real_calendar_is_used_when_no_session_injected(j):
    src = FakeSource()
    r = run_scan(journal=j, policy=P, approved=[appr("AAA")], source=src,
                 now_utc=datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc))
    assert r.status == "BLOCKED" and "MARKET_CLOSED" in r.reason_counts and src.calls == []


def test_ledger_and_drawdown_latch_block_before_fetch(tmp_path):
    jr = Journal(tmp_path / "x.sqlite3", clock=lambda: NOW)
    jr.migrate()
    src = FakeSource({"AAA": [raw_row()]})
    r = scan(jr, [appr("AAA")], src)  # nothing recorded: no baseline, no snapshot
    assert r.status == "BLOCKED" and r.messages[0].kind == "RECONCILIATION_REQUIRED" and src.calls == []
    jr.confirm_baseline(D("100000"))
    jr.add_snapshot(snap(equity="90000", cash="50000", asof=NOW - timedelta(hours=1)))
    r = scan(jr, [appr("AAA")], src)
    assert r.status == "BLOCKED" and "HARD_HALT" in r.reason_counts
    assert r.messages[0].kind == "ACCOUNT_RISK" and src.calls == []
    jr.close()


# ---- data failures are never NO_TRADE ----
def test_fetch_error_is_data_unavailable_with_retries_and_issue_record(j):
    sleeps = []
    src = FakeSource(errors={"AAA": SourceError("FETCH_ERROR", "boom")})
    r = run_scan(journal=j, policy=P, approved=[appr("AAA")], source=src, now_utc=NOW, session=OPEN, sleep=sleeps.append)
    assert r.status == "DATA_UNAVAILABLE" and r.messages[0].kind == "DATA_ISSUE"
    assert src.calls == ["AAA"] * 3 and sleeps == [1, 2]  # 1 + provider_retries(2)
    assert not any(m.kind == "NO_TRADE_DIGEST" for m in r.messages) and not r.candidates
    assert len(j.audit_events("DATA_ISSUE")) == 1


def test_rate_limit_is_not_retried(j):
    src = FakeSource(errors={"AAA": SourceError("RATE_LIMIT", "429")})
    r = scan(j, [appr("AAA")], src)
    assert src.calls == ["AAA"] and r.status == "DATA_UNAVAILABLE" and r.data_issues[0][1] == "RATE_LIMIT"


def test_unexpected_exception_type_is_contained(j):
    r = scan(j, [appr("AAA")], FakeSource(errors={"AAA": RuntimeError("weird")}))
    from csp_alerts.market import failures as F
    assert r.status == "DATA_UNAVAILABLE" and r.data_issues[0][2] == F.stored(F.UNKNOWN_ERROR)   # canned; no raw text or class name


def test_empty_and_degraded_chains_are_data_issues_not_no_trade(j):
    r = scan(j, [appr("AAA")], FakeSource({"AAA": []}))
    assert r.status == "DATA_UNAVAILABLE" and r.data_issues[0][1] == "EMPTY_CHAIN"
    zeroed = [raw_row(strike=30.0 + i, bid=0.0, ask=0.0) for i in range(12)]
    r = scan(j, [appr("AAA")], FakeSource({"AAA": zeroed}))
    assert r.status == "DATA_UNAVAILABLE" and r.data_issues[0][1] == "DEGRADED_CHAIN" and not r.candidates


def test_one_ticker_failing_does_not_hide_other_candidates(j):
    src = FakeSource({"BBB": [raw_row()]}, errors={"AAA": SourceError("FETCH_ERROR", "x")})
    r = scan(j, [appr("AAA"), appr("BBB")], src)
    assert r.status == "CANDIDATES" and [m.kind for m in r.messages] == ["ENTRY_RESEARCH", "DATA_ISSUE"]


# ---- evaluation and candidates ----
def test_passing_put_becomes_candidate_and_everything_is_persisted(j):
    r = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row(), raw_row(strike=39.0, bid=0.0, ask=0.05)]}))
    assert r.status == "CANDIDATES" and len(r.candidates) == 1 and r.contracts_evaluated == 2
    c = r.candidates[0]
    assert c.decision.decision.value == "PASS_RESEARCH_ONLY" and c.obs.quote_at_utc is None
    assert r.reason_counts["BAD_BID_ASK"] == 1 and r.ai_status == "DISABLED"
    assert count(j, "market_observations") == 2 and count(j, "entry_evaluations") == 2
    row = j.conn.execute("SELECT * FROM market_observations").fetchone()
    assert len(row["raw_sha256"]) == 64 and row["quote_at_utc"] is None and row["tier"] == "INDICATIVE_UNTIMESTAMPED"
    assert j.conn.execute("SELECT COUNT(*) FROM entry_evaluations WHERE decision='BLOCKED'").fetchone()[0] == 1


def test_unconfirmed_deliverable_blocks_everything(j):
    a = appr("AAA", standard_deliverable_confirmed_on=None)
    r = scan(j, [a], FakeSource({"AAA": [raw_row()]}))
    assert not r.candidates and r.reason_counts["NONSTANDARD_CONTRACT"] == 1 and r.status == "NO_TRADE"
    stale = appr("AAA", standard_deliverable_confirmed_on=TODAY - timedelta(days=8))
    assert not scan(j, [stale], FakeSource({"AAA": [raw_row()]})).candidates


def test_unknown_earnings_from_provider_still_needs_owner_clearance(j):
    a = appr("AAA", events_cleared_through=TODAY + timedelta(days=10))
    r = scan(j, [a], FakeSource({"AAA": [raw_row()]}))
    assert not r.candidates and r.reason_counts["EVENT_UNVERIFIED"] == 1
    r2 = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row()]}, earnings=[TODAY + timedelta(days=5)]))
    assert not r2.candidates and r2.reason_counts["EARNINGS_IN_WINDOW"] == 1


def test_ranking_liquidity_first_then_bid_yield_and_components_exposed(j):
    rows = {
        "AAA": [raw_row(strike=40.0, bid=0.60, ask=0.64)],   # spread 6.5% of mid, higher yield
        "BBB": [raw_row(strike=40.0, bid=0.50, ask=0.52)],   # spread 3.9% -> better liquidity
        "CCC": [raw_row(strike=20.0, bid=0.50, ask=0.52)],   # same spread as BBB, higher yield (cheaper strike)
    }
    r = scan(j, [appr(t) for t in rows], FakeSource(rows))
    assert [c.obs.underlying for c in r.candidates] == ["CCC", "BBB", "AAA"]
    comp = r.candidates[0].components
    assert comp.bid_yield_per_calendar_day_pct == D(100) * D("0.5") / (D("20.0") * 40)
    assert comp.spread_fraction_of_mid == D("0.02") / D("0.51") and comp.open_interest == 1000


def test_max_three_alternatives_and_one_per_underlying(j):
    rows = {t: [raw_row(strike=20.0, bid=0.50, ask=0.52), raw_row(strike=21.0, bid=0.50, ask=0.52)]
            for t in ("AAA", "BBB", "CCC", "DDD")}
    r = scan(j, [appr(t) for t in rows], FakeSource(rows), pol=policy(enabled=False))
    assert len(r.candidates) == 3 and len({c.obs.underlying for c in r.candidates}) == 3
    assert len(r.messages) == 3 and all(m.kind == "ENTRY_RESEARCH" for m in r.messages)


def test_alternatives_are_revalidated_against_book_with_earlier_picks(j):
    # Same sector: 7,500 + 7,500 > 10,000 sector cap, so only the first survives although each passes alone.
    rows = {"AAA": [raw_row(strike=75.0, bid=0.50, ask=0.52)], "BBB": [raw_row(strike=75.0, bid=0.60, ask=0.64)]}
    ap = [appr("AAA", sector="tech", bucket="x1"), appr("BBB", sector="tech", bucket="x2")]
    r = scan(j, ap, FakeSource(rows), pol=policy(enabled=False))
    assert [c.obs.underlying for c in r.candidates] == ["AAA"] and r.excluded_after_revalidation == 1
    assert r.reason_counts["STACKED_SECTOR_LIMIT"] == 1
    # individual-company cap: two STOCK names already held hypothetically, so the third is dropped
    rows3 = {t: [raw_row(strike=20.0, bid=0.50, ask=0.52)] for t in ("AAA", "BBB", "CCC")}
    r3 = scan(j, [appr(t, typ="STOCK") for t in rows3], FakeSource(rows3), pol=policy(enabled=False))
    assert len(r3.candidates) == 2 and r3.reason_counts["STACKED_INDIVIDUAL_NAME_LIMIT"] == 1


def test_pilot_cap_applies_across_alternatives(j):
    rows = {t: [raw_row(strike=75.0, bid=0.50, ask=0.52)] for t in ("AAA", "BBB", "CCC")}
    r = scan(j, [appr(t) for t in rows], FakeSource(rows))  # pilot 15,000: two 7,500 puts fit, the third does not
    assert len(r.candidates) == 2 and r.reason_counts["STACKED_PILOT_COLLATERAL_LIMIT"] == 1


def test_existing_pending_position_counts_against_new_candidates(j):
    j.create_draft(NewPosition("T1", "ZZZ", TODAY + timedelta(days=40), D("100"), 100, 1, "other", "other2"))
    j.transition("T1", S.PENDING)  # reserves 10,000
    j.add_snapshot(snap(equity="100000", cash="50000", asof=NOW, sid="s2"))
    r = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row(strike=75.0, bid=0.50, ask=0.52)]}))
    assert not r.candidates and r.reason_counts["PILOT_COLLATERAL_LIMIT"] == 1  # 10,000 + 7,500 > 15,000


def test_ai_enabled_without_reviewer_fails_closed(j):
    pol = P.model_copy(update={"ai": P.ai.model_copy(update={"enabled": True})})
    r = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row()]}), pol=pol)
    assert not r.candidates and r.ai_status == "UNAVAILABLE" and r.reason_counts["AI_UNAVAILABLE"] == 1


def test_no_trade_digest_once_per_day_and_dry_run_never_marks(j):
    src = FakeSource({"AAA": [raw_row(delta=-0.5)]})
    r1 = scan(j, [appr("AAA")], src)
    assert r1.status == "NO_TRADE" and r1.messages[0].kind == "NO_TRADE_DIGEST"
    assert "DELTA_OUT_OF_RANGE" in r1.messages[0].text
    r2 = scan(j, [appr("AAA")], src)
    assert r2.messages[0].kind == "NO_TRADE_DIGEST"  # a dry run never records "sent"
    j.mark_digest_sent(date(2026, 10, 30))
    r3 = scan(j, [appr("AAA")], src)
    assert r3.status == "NO_TRADE" and r3.messages == [] and "suppressed" in r3.notes[0]


def test_scan_makes_no_network_calls(j, monkeypatch):
    def deny(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    assert scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row()]})).status == "CANDIDATES"


# ---- templates ----
def test_entry_alert_content_and_constraints(j):
    r = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row(strike=75.0)]}))
    m = r.messages[0]
    t = m.text
    for must in ("INDICATIVE", "NO ORDER SENT", "Bid/ask exchange timestamp: UNAVAILABLE - VERIFY LIVE",
                 "YAHOO_VIA_STOCKPILE", "INDICATIVE_UNTIMESTAMPED", "retrieved", "$7,500",
                 "Exposure after fill: $7,500 / $25,000", "40 DTE", "break-even: $74.50", "alternatives, not approval"):
        assert must in t, must
    low = t.lower()
    for banned in ("annualized", "ann.", "probability", "% otm", "chance of", "guarantee"):
        assert banned not in low
    assert len(t) < 4096 and m.dedup_key == f"ENTRY_RESEARCH|{TODAY}|AAA|{EXP}|P|75.00"
    assert "36 DTE" not in t  # DTE is computed from dates


def test_dte_in_alert_follows_the_dates_not_a_constant(j):
    far = (TODAY + timedelta(days=36)).isoformat()
    r = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row(exp=far)]}))
    assert "36 DTE" in r.messages[0].text


def test_hostile_text_is_escaped_and_control_chars_removed():
    assert templates.esc("<b>x</b>&\n\x07evil") == "&lt;b&gt;x&lt;/b&gt;&amp; evil"
    assert len(templates.esc("a" * 500)) == 120
    msg = templates.data_issue([("<script>", "K\nEY", "<a href=x>click</a>")], NOW)
    assert "<script>" not in msg.text and "<a href" not in msg.text and "&lt;script&gt;" in msg.text
    d = templates.no_trade_digest("X", {"<i>bad</i>": 2}, NOW)
    assert "<i>" not in d.text


def test_templates_truncate_long_messages():
    big = templates.data_issue([(f"T{i}", "K", "m" * 100) for i in range(500)], NOW)
    assert len(big.text) <= 4096


def test_small_all_zero_chain_is_data_unavailable_not_a_healthy_no_trade(j):
    r = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row(strike=40.0, bid=0.0, ask=0.05), raw_row(strike=41.0, bid=0.0, ask=0.06)]}))
    assert r.status == "DATA_UNAVAILABLE" and r.data_issues[0][1] == "DEGRADED_CHAIN"
    assert not any(m.kind == "NO_TRADE_DIGEST" for m in r.messages) and len(j.audit_events("DATA_ISSUE")) == 1


def test_partial_failure_keeps_healthy_symbols_reason_counts_in_a_partial_digest(j):
    src = FakeSource({"AAA": [raw_row(delta=-0.5)]}, errors={"BBB": SourceError("FETCH_ERROR", "x")})
    r = scan(j, [appr("AAA"), appr("BBB")], src)
    assert r.status == "DATA_UNAVAILABLE" and not r.candidates
    kinds = [m.kind for m in r.messages]
    assert kinds == ["NO_TRADE_DIGEST", "DATA_ISSUE"]
    digest = r.messages[0]
    assert "PARTIAL SCAN" in digest.text and "DELTA_OUT_OF_RANGE" in digest.text          # healthy ticker's counts kept
    assert "Coverage: 1 of 2" in digest.text and "BBB UNAVAILABLE" in digest.text
    assert digest.dedup_key.endswith("|PARTIAL") and digest.severity == "WARN"
    assert "PARTIAL" in r.messages[1].text
    # when every symbol fails there is no digest, only the data-issue message
    r2 = scan(j, [appr("BBB")], FakeSource(errors={"BBB": SourceError("FETCH_ERROR", "x")}))
    assert [m.kind for m in r2.messages] == ["DATA_ISSUE"]
