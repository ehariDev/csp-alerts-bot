"""Slice 2 acceptance: jobs/scan.py (spec 7, 13.1; AT-14, AT-17; MVP_SCOPE IT-002/IT-004/IT-013).
Hand-computed oracles are in comments. Frozen clock NOW=2026-10-30T15:05:02Z (DTE 35 = 2026-12-04). No network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, TODAY, policy
from s2_helpers import OPEN_SESSION, Boom, FakeSource, appr, count, make_journal, policy_variant, raw_row, ms

from csp_alerts.journal.models import AccountSnapshot, PositionState as S
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.jobs.scan import run_scan
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import SourceError

POL = policy()
EXP_W49 = date(2026, 12, 4)     # Fri, ISO week 49 (35 DTE)
EXP_W50 = date(2026, 12, 11)    # Fri, ISO week 50 (42 DTE)


def scan(j, approved, src, pol=POL, now=NOW, session=OPEN_SESSION, sleeps=None):
    return run_scan(journal=j, policy=pol, approved=approved, source=src, now_utc=now, session=session,
                    sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def kinds(res):
    return [m.kind for m in res.messages]


def order(res):
    return [c.obs.underlying for c in res.candidates]


def etf(t, **kw):
    return appr(t, asset_type="ETF", **kw)


# ====================================================================== happy path + provenance
def test_single_candidate_end_to_end_with_spec_numbers():
    j = make_journal()
    src = FakeSource({"AAA": [raw_row()]}, {"AAA": [date(2027, 3, 1)]})
    res = scan(j, [appr("AAA")], src)
    assert res.status == "CANDIDATES" and order(res) == ["AAA"] and res.contracts_evaluated == 1
    assert kinds(res) == ["ENTRY_RESEARCH"]
    assert src.calls == [("AAA", 35, 45)] and src.earn_calls == ["AAA"]
    d = res.candidates[0].decision
    assert d.additional_collateral_usd == D("7500") and d.manual_free_cash_after_usd == D("62500")   # 70,000 - 7,500
    assert d.exposure_after_usd == D("7500")
    assert d.data_tier == "INDICATIVE_UNTIMESTAMPED" and d.requires_broker_manual_quote_check
    # provenance persisted; dry-run never creates anything sendable
    assert count(j, "market_observations") == 1 and count(j, "entry_evaluations") == 1
    row = j.conn.execute("SELECT * FROM market_observations").fetchone()
    assert row["quote_at_utc"] is None and row["tier"] == "INDICATIVE_UNTIMESTAMPED" and len(row["raw_sha256"]) == 64
    assert count(j, "alert_outbox") == 0
    assert j.audit_events("NO_TRADE_DIGEST_SENT") == []


def test_unknown_provider_earnings_still_needs_owner_clearance_and_passes_with_it():
    j = make_journal()
    res = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row()]}, {"AAA": None}))
    assert res.status == "CANDIDATES"
    # owner clearance that stops before expiration blocks even though provider is silent
    res2 = scan(make_journal(), [appr("AAA", events_cleared_through=TODAY.isoformat())], FakeSource({"AAA": [raw_row()]}))
    assert res2.status == "NO_TRADE" and res2.reason_counts["EVENT_UNVERIFIED"] == 1


def test_earnings_in_window_blocks_and_earnings_fetch_exception_is_unknown():
    j = make_journal()
    r = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row()]}, {"AAA": [EXP_W49]}))
    assert r.status == "NO_TRADE" and r.reason_counts["EARNINGS_IN_WINDOW"] == 1
    r = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": [raw_row()]}, {"AAA": RuntimeError("yahoo down")}))
    assert r.status == "CANDIDATES"          # unknown (not 'no earnings'); owner clearance is the evidence


# ====================================================================== fail-closed ordering (nothing fetched)
def book_journal(kind):
    now = NOW
    if kind == "stale":
        return make_journal(snap=False, baseline="100000"), "LEDGER_STALE", "RECONCILIATION_REQUIRED"
    j = make_journal(baseline=None if kind == "nobaseline" else "100000", snap=kind not in ("stale",))
    if kind == "unconfirmed":
        j = make_journal(snap=False)
        j.add_snapshot(AccountSnapshot("u", now - timedelta(hours=1), D("100000"), D("70000"), False, "v1"))
        return j, "LEDGER_UNRECONCILED", "RECONCILIATION_REQUIRED"
    if kind == "nobaseline":
        return j, "LEDGER_UNRECONCILED", "RECONCILIATION_REQUIRED"
    if kind == "cash":
        j = make_journal(cash="100001")
        return j, "LEDGER_UNRECONCILED", "RECONCILIATION_REQUIRED"
    if kind == "oldsnap":
        j = make_journal(snap=False)
        j.add_snapshot(AccountSnapshot("o", now - timedelta(hours=30, seconds=1), D("100000"), D("70000"), True, "v1"))
        return j, "LEDGER_STALE", "RECONCILIATION_REQUIRED"
    if kind in ("pause", "reduce", "halt"):
        eq = {"pause": "96900", "reduce": "94900", "halt": "91900"}[kind]
        j = make_journal(snap=False)
        j.add_snapshot(AccountSnapshot("a", now - timedelta(hours=26), D("100000"), D("50000"), True, "v1"))
        j.add_snapshot(AccountSnapshot("b", now - timedelta(hours=2), D(eq), D("50000"), True, "v1"))
        return j, ("HARD_HALT" if kind == "halt" else "DRAWDOWN_PAUSE"), "ACCOUNT_RISK"
    if kind == "halt_rebound":
        j = make_journal(snap=False)
        for sid, h, eq in (("a", 72, "100000"), ("b", 48, "91000"), ("c", 2, "99900")):
            j.add_snapshot(AccountSnapshot(sid, now - timedelta(hours=h), D(eq), D("50000"), True, "v1"))
        return j, "HARD_HALT", "ACCOUNT_RISK"
    if kind == "scope":
        j = make_journal(snap=False)
        j.create_draft(NewPosition("X1", "ZZZ", EXP_W49, D("50"), 100, 1, "s", "b", account_scope="paper_b"))
        j.add_snapshot(AccountSnapshot("s", now, D("100000"), D("70000"), True, "v1"))
        return j, "LEDGER_UNRECONCILED", "RECONCILIATION_REQUIRED"
    if kind == "assignment":
        c = [NOW - timedelta(hours=5)]
        j = Journal(":memory:", clock=lambda: c[0])
        j.migrate()
        j.confirm_baseline(D("100000"))
        j.create_draft(NewPosition("A1", "ZZZ", EXP_W49, D("50"), 100, 1, "s", "b"))
        j.transition("A1", S.PENDING)
        j.transition("A1", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.5"), opened_at_utc=c[0])
        j.transition("A1", S.ASSIGNED_PENDING_RECONCILIATION)
        c[0] = NOW
        j.add_snapshot(AccountSnapshot("s", NOW - timedelta(hours=1), D("100000"), D("70000"), True, "v1"))
        return j, "LEDGER_UNRECONCILED", "RECONCILIATION_REQUIRED"
    if kind == "change_after":
        c = [NOW - timedelta(hours=3)]
        j = Journal(":memory:", clock=lambda: c[0])
        j.migrate()
        j.confirm_baseline(D("100000"))
        j.add_snapshot(AccountSnapshot("s", NOW - timedelta(hours=3), D("100000"), D("70000"), True, "v1"))
        c[0] = NOW - timedelta(hours=1)
        j.create_draft(NewPosition("A1", "ZZZ", EXP_W49, D("50"), 100, 1, "s", "b"))
        j.transition("A1", S.PENDING)
        c[0] = NOW
        return j, "LEDGER_UNRECONCILED", "RECONCILIATION_REQUIRED"
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["stale", "oldsnap", "nobaseline", "unconfirmed", "cash", "pause", "reduce", "halt",
                                  "halt_rebound", "scope", "assignment", "change_after"])
def test_ledger_or_drawdown_problem_sends_nothing_shows_reason_and_never_fetches(kind):
    j, reason, msg_kind = book_journal(kind)
    res = scan(j, [appr("AAA"), appr("BBB")], Boom())
    assert res.status == "BLOCKED" and res.candidates == [] and res.contracts_evaluated == 0
    assert res.reason_counts[reason] >= 1
    assert kinds(res) == [msg_kind]
    assert reason in res.messages[0].text and "NO ORDER SENT" in res.messages[0].text
    assert count(j, "entry_evaluations") == 0 and count(j, "alert_outbox") == 0


@pytest.mark.parametrize("session,reason", [
    (SessionInfo(SessionStatus.CLOSED), "MARKET_CLOSED"), (SessionInfo(SessionStatus.UNKNOWN), "UNKNOWN_SESSION"),
    (SessionInfo(SessionStatus.OPEN, None), "UNKNOWN_SESSION"), (SessionInfo(SessionStatus.OPEN, 19), "MARKET_CLOSED"),
    (SessionInfo(SessionStatus.OPEN, 0), "MARKET_CLOSED"),
])
def test_session_problem_blocks_before_any_fetch_even_with_empty_or_full_whitelist(session, reason):
    for approved in ([], [appr("AAA")]):
        res = scan(make_journal(), approved, Boom(), session=session)
        assert res.status == "BLOCKED" and res.reason_counts[reason] == 1 and len(res.messages) == 1


def test_session_exactly_20_minutes_proceeds():
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": [raw_row()]}), session=SessionInfo(SessionStatus.OPEN, 20))
    assert res.status == "CANDIDATES"


def test_blocked_by_both_session_and_ledger_lists_both_reasons():
    j, _, _ = book_journal("stale")
    res = scan(j, [appr("AAA")], Boom(), session=SessionInfo(SessionStatus.CLOSED))
    assert {"MARKET_CLOSED", "LEDGER_STALE"} <= set(res.reason_counts)


def test_halt_alert_severity_is_critical_per_spec_13_3():
    """Spec 13.3: CRITICAL for the 8% hard halt, URGENT for 5%. Implementation uses URGENT for both."""
    j, _, _ = book_journal("halt")
    assert scan(j, [appr("AAA")], Boom()).messages[0].severity == "CRITICAL"
    j, _, _ = book_journal("reduce")
    assert scan(j, [appr("AAA")], Boom()).messages[0].severity == "URGENT"


def test_drawdown_levels_have_distinct_dedup_keys_so_escalation_is_never_suppressed():
    """Spec 8.2: 'Escalation to 5% or 8% creates a new alert immediately, even after a 3% acknowledgment.'"""
    keys = {}
    for k in ("pause", "reduce", "halt"):
        j, _, _ = book_journal(k)
        keys[k] = scan(j, [appr("AAA")], Boom()).messages[0].dedup_key
    assert len(set(keys.values())) == 3, keys


# ====================================================================== empty whitelist / digest rules
def test_empty_whitelist_gives_useful_digest_without_fetching():
    j = make_journal()
    res = scan(j, [], Boom())
    assert res.status == "NO_TRADE" and kinds(res) == ["NO_TRADE_DIGEST"]
    assert res.reason_counts["EMPTY_WHITELIST"] == 1 and "EMPTY_WHITELIST" in res.messages[0].text
    assert "NO ORDER SENT" in res.messages[0].text and res.messages[0].dedup_key == "NO_TRADE_DIGEST|2026-10-30"


def test_dry_run_never_marks_digest_sent_and_never_writes_outbox():
    j = make_journal()
    for _ in range(3):
        res = scan(j, [], Boom())
        assert kinds(res) == ["NO_TRADE_DIGEST"]              # not suppressed: dry-run never marked it sent
    assert j.audit_events("NO_TRADE_DIGEST_SENT") == [] and count(j, "alert_outbox") == 0
    j.mark_digest_sent(TODAY - timedelta(days=1))
    assert kinds(scan(j, [], Boom())) == ["NO_TRADE_DIGEST"]  # yesterday's mark does not suppress today
    j.mark_digest_sent(TODAY)                                  # what a REAL send path would record
    res = scan(j, [], Boom())
    assert res.messages == [] and any("already sent" in n for n in res.notes) and res.status == "NO_TRADE"


def test_digest_suppression_applies_to_no_candidate_scans_but_never_to_data_issues():
    j = make_journal()
    bad = [raw_row(delta=-0.5)]
    j.mark_digest_sent(TODAY)
    r = scan(j, [appr("AAA")], FakeSource({"AAA": bad}))
    assert r.status == "NO_TRADE" and r.messages == [] and r.reason_counts["DELTA_OUT_OF_RANGE"] == 1
    r = scan(j, [appr("AAA")], FakeSource({"AAA": RuntimeError("down")}))
    assert r.status == "DATA_UNAVAILABLE" and kinds(r) == ["DATA_ISSUE"]


def test_no_candidate_digest_reports_reason_counts_hand_checked():
    # 3 contracts: delta -0.5 (DELTA), OI 10 (OI), bid 0 (BAD_BID_ASK); one reason each (volume etc fine)
    rows = [raw_row(strike=70.0, delta=-0.5), raw_row(strike=65.0, open_interest=10), raw_row(strike=60.0, bid=0.0, ask=0.0)]
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": rows}))
    assert res.status == "NO_TRADE" and res.contracts_evaluated == 3
    assert (res.reason_counts["DELTA_OUT_OF_RANGE"], res.reason_counts["INSUFFICIENT_OI"], res.reason_counts["BAD_BID_ASK"]) == (1, 1, 1)
    t = res.messages[0].text
    assert "contracts evaluated: 3" in t and "scanned: 1" in t and "DELTA_OUT_OF_RANGE: 1" in t


# ====================================================================== data failures are never NO_TRADE
def ten(fail: set[int], good_rows=None):
    tickers = [f"T{i:02d}" for i in range(10)]
    chains = {t: (RuntimeError("yahoo 500") if i in fail else (good_rows or [raw_row(delta=-0.5)])) for i, t in enumerate(tickers)}
    return tickers, chains


def test_three_of_ten_failing_is_not_a_healthy_no_trade():
    tickers, chains = ten({1, 4, 8})
    j = make_journal()
    j.mark_digest_sent(TODAY)                      # even if a normal digest was already sent today
    res = scan(j, [etf(t) for t in tickers], FakeSource(chains))
    assert res.status == "DATA_UNAVAILABLE" and res.tickers_scanned == 10 and res.contracts_evaluated == 7
    assert {t for t, _, _ in res.data_issues} == {"T01", "T04", "T08"}
    assert kinds(res) == ["NO_TRADE_DIGEST", "DATA_ISSUE"]
    digest, issue = res.messages
    # the digest is explicitly PARTIAL (never the clean 'NO-TRADE DIGEST'), coverage 7 of 10, healthy symbols' reasons shown
    assert digest.severity == "WARN" and digest.dedup_key == "NO_TRADE_DIGEST|2026-10-30|PARTIAL"
    assert "PARTIAL SCAN" in digest.text and "NO-TRADE DIGEST" not in digest.text
    assert "Coverage: 7 of 10 approved symbols scanned" in digest.text and "do not read this as a clean no-trade" in digest.text
    assert "DELTA_OUT_OF_RANGE: 7" in digest.text and "contracts evaluated: 7" in digest.text      # 7 healthy symbols, one bad row each
    for t in ("T01", "T04", "T08"):
        assert t in digest.text and t in issue.text and f"{t} UNAVAILABLE" in digest.text
    assert "NOT a no-trade" in issue.text and "Coverage: 7 of 10" in issue.text
    assert len(j.audit_events("DATA_ISSUE")) == 3


def test_all_healthy_no_candidate_scan_keeps_the_clean_digest_with_full_coverage():
    tickers, chains = ten(set())
    res = scan(make_journal(), [etf(t) for t in tickers], FakeSource(chains))
    assert res.status == "NO_TRADE" and kinds(res) == ["NO_TRADE_DIGEST"]
    t = res.messages[0].text
    assert "NO-TRADE DIGEST" in t and "PARTIAL" not in t and "Coverage: 10 of 10" in t
    assert res.messages[0].dedup_key == "NO_TRADE_DIGEST|2026-10-30"


def test_partial_failure_with_a_candidate_reports_both():
    tickers, chains = ten({0, 1, 2}, good_rows=[raw_row(strike=float(20 + 0), spot=25.0)])
    res = scan(make_journal(), [etf(t) for t in tickers], FakeSource(chains))
    assert res.status == "CANDIDATES" and len(res.candidates) == 3          # 7 healthy ETFs, max 3 alerts
    assert kinds(res).count("ENTRY_RESEARCH") == 3 and kinds(res)[-1] == "DATA_ISSUE"
    assert {t for t, _, _ in res.data_issues} == {"T00", "T01", "T02"}


def test_all_failing_is_data_unavailable_and_produces_nothing_stale():
    tickers, chains = ten(set(range(10)))
    res = scan(make_journal(), [etf(t) for t in tickers], FakeSource(chains))
    assert res.status == "DATA_UNAVAILABLE" and res.candidates == [] and res.contracts_evaluated == 0
    assert kinds(res) == ["DATA_ISSUE"] and len(res.data_issues) == 10


def test_retry_policy_rate_limit_not_retried_transient_retried_with_backoff():
    sleeps: list = []
    src = FakeSource({"AAA": SourceError("RATE_LIMIT", "Too Many Requests")})
    res = scan(make_journal(), [appr("AAA")], src, sleeps=sleeps)
    assert len(src.calls) == 1 and sleeps == [] and res.data_issues[0][1] == "RATE_LIMIT"
    sleeps.clear()
    src = FakeSource({"AAA": RuntimeError("boom")})
    res = scan(make_journal(), [appr("AAA")], src, sleeps=sleeps)
    assert len(src.calls) == 3 and sleeps == [1, 2] and res.data_issues[0][1] == "FETCH_ERROR"   # provider_retries=2
    for kind in ("STOCKPILE_MISSING", "STOCKPILE_IMPORT"):
        src = FakeSource({"AAA": SourceError(kind, "x")})
        scan(make_journal(), [appr("AAA")], src)
        assert len(src.calls) == 1


def test_transient_failure_then_success_yields_candidate():
    state = {"n": 0}

    class Flaky(FakeSource):
        def fetch_chain(self, t, a, b):
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionError("reset")
            return super().fetch_chain(t, a, b)
    res = scan(make_journal(), [appr("AAA")], Flaky({"AAA": [raw_row()]}))
    assert res.status == "CANDIDATES" and res.data_issues == []


def test_empty_chain_is_a_data_issue_never_no_trade():
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": []}))
    assert res.status == "DATA_UNAVAILABLE" and res.data_issues[0][1] == "EMPTY_CHAIN" and kinds(res) == ["DATA_ISSUE"]


@pytest.mark.parametrize("rows", [
    [raw_row(type="call") for _ in range(5)],                       # calls only
    [raw_row(type="P") for _ in range(5)],                          # unrecognised type token
    [{"garbage": 1}, {"strike": "x"}],                              # junk rows
])
def test_unusable_chain_is_degraded_data_issue(rows):
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": rows}))
    assert res.status == "DATA_UNAVAILABLE" and res.data_issues[0][1] == "DEGRADED_CHAIN"


@pytest.mark.parametrize("zeroed,status", [(16, "CANDIDATES"), (17, "DATA_UNAVAILABLE")])
def test_mostly_zeroed_chain_boundary_at_85_percent_sends_no_stale_candidates(zeroed, status):
    # 20 rows; strikes 40..59; unzeroed rows are otherwise valid (spot 80 > strike)
    rows = [raw_row(strike=float(40 + i), bid=0.0, ask=0.0) if i < zeroed else raw_row(strike=float(40 + i))
            for i in range(20)]
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": rows}))
    assert res.status == status
    if status == "DATA_UNAVAILABLE":
        assert res.candidates == [] and res.data_issues[0][1] == "DEGRADED_CHAIN" and "ENTRY_RESEARCH" not in kinds(res)


def test_hostile_garbage_in_rows_never_crashes_a_scan():
    rows = [raw_row(), raw_row(strike={"a": 1}), raw_row(bid="abc"), raw_row(delta=object()), raw_row(expiration=12345),
            raw_row(open_interest=[1]), raw_row(volume={}), raw_row(spot="NaN"), raw_row(last_trade_ms="x")]
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": rows}))
    assert res.status in ("CANDIDATES", "NO_TRADE", "DATA_UNAVAILABLE")
    assert order(res) in ([], ["AAA"])


# ====================================================================== exchange-date DTE (host/UTC date vs New York)
@pytest.mark.parametrize("now,exp,expect_candidate", [
    # 2026-11-02T04:30Z = 23:30 EST Sun Nov 1 -> exchange date Nov 1. Dec 6 = 35 DTE (ok); UTC date would give 34.
    (datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc), date(2026, 12, 6), True),
    (datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc), date(2026, 12, 5), False),     # 34 on the exchange date
    # 2026-11-01T03:30Z = 23:30 EDT Sat Oct 31 -> exchange date Oct 31. Dec 5 = 35 DTE; UTC date (Nov 1) says 34.
    (datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc), date(2026, 12, 5), True),
    (datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc), date(2026, 12, 4), False),     # 34
])
def test_scan_dte_uses_new_york_date_not_host_or_utc_date(now, exp, expect_candidate):
    from csp_alerts.journal.reconcile import exchange_date
    today = exchange_date(now)
    j = make_journal(now)
    row = raw_row(now, expiration=exp.isoformat(), dte=35)
    res = scan(j, [appr("AAA", today=today)], FakeSource({"AAA": [row]}, now=now), now=now)
    assert (res.status == "CANDIDATES") is expect_candidate, dict(res.reason_counts)


# ====================================================================== real calendar integrated with scan (AT-17 cutoff)
def clock_scan(t: datetime, today: date, expiry: date):
    j = make_journal(t)
    src = FakeSource({"AAA": [raw_row(t, expiration=expiry.isoformat())]}, now=t)
    return scan(j, [appr("AAA", today=today)], src, now=t, session=None)


@pytest.mark.parametrize("t,today,expiry,blocked", [
    ("2026-10-30T19:39:59Z", date(2026, 10, 30), date(2026, 12, 4), False),     # 20:01 left
    ("2026-10-30T19:40:00Z", date(2026, 10, 30), date(2026, 12, 4), False),     # 20:00 left (not < 20)
    ("2026-10-30T19:40:01Z", date(2026, 10, 30), date(2026, 12, 4), True),      # 19:59 left
    ("2026-10-30T13:29:59Z", date(2026, 10, 30), date(2026, 12, 4), True),      # before open
    ("2026-10-30T13:30:00Z", date(2026, 10, 30), date(2026, 12, 4), False),     # first minute
    ("2026-10-30T20:00:00Z", date(2026, 10, 30), date(2026, 12, 4), True),      # at close
    ("2026-11-26T15:00:00Z", date(2026, 11, 26), date(2027, 1, 5), True),       # Thanksgiving
    ("2026-10-31T15:00:00Z", date(2026, 10, 31), date(2026, 12, 5), True),      # Saturday
    ("2026-11-27T17:40:00Z", date(2026, 11, 27), date(2027, 1, 1), False),      # early close, 20:00 left
    ("2026-11-27T17:40:01Z", date(2026, 11, 27), date(2027, 1, 1), True),       # early close, 19:59 left
    ("2026-11-02T14:30:00Z", date(2026, 11, 2), date(2026, 12, 7), False),      # first open after fall-back
    ("2026-11-02T14:29:59Z", date(2026, 11, 2), date(2026, 12, 7), True),
    ("2026-03-09T13:30:00Z", date(2026, 3, 9), date(2026, 4, 13), False),       # first open after spring-forward
])
def test_scan_with_real_calendar_cutoffs(t, today, expiry, blocked):
    # expiry dates: Nov 27 + 35 = Jan 1 2027; Mar 9 + 35 = Apr 13; Nov 2 + 35 = Dec 7; Oct 31 + 35 = Dec 5
    now = datetime.fromisoformat(t.replace("Z", "+00:00"))
    res = clock_scan(now, today, expiry)
    if blocked:
        assert res.status == "BLOCKED" and {"MARKET_CLOSED"} & set(res.reason_counts), dict(res.reason_counts)
    else:
        assert res.status == "CANDIDATES", dict(res.reason_counts)


def test_scan_with_broken_or_out_of_range_calendar_is_unknown_session_and_no_fetch():
    t = datetime(2031, 6, 3, 15, 0, tzinfo=timezone.utc)
    j = make_journal(t)
    res = scan(j, [appr("AAA", today=date(2031, 6, 3))], Boom(), now=t, session=None)
    assert res.status == "BLOCKED" and res.reason_counts["UNKNOWN_SESSION"] == 1


# ====================================================================== ranking and selection
def rows_for(*specs):
    """specs: (ticker, strike, spot, bid, ask, oi) -> {ticker: [row]}"""
    return {t: [raw_row(strike=float(k), spot=float(s), bid=b, ask=a, open_interest=oi)] for t, k, s, b, a, oi in specs}


def test_ranking_is_liquidity_first_then_bid_yield():
    # A: strike 75 .50/.54  spread .04 mid .52 -> 0.07692 ; yield 100*.5/(75*35)=0.019048
    # B: strike 20 .50/.55  spread .05 mid .525 -> 0.09524 ; yield 100*.5/(20*35)=0.071429 (HIGHER yield, worse liquidity)
    #    collateral C 4,000 + A 7,500 + B 2,000 = 13,500 <= 15,000 pilot cap, so all three can be alternatives
    # C: strike 40 .50/.54  spread .04 mid .52 -> 0.07692 (ties A on liquidity) ; yield 0.035714 -> beats A
    ch = rows_for(("AAA", 75, 80, 0.5, 0.54, 1000), ("BBB", 20, 25, 0.5, 0.55, 1000), ("CCC", 40, 45, 0.5, 0.54, 1000))
    res = scan(make_journal(), [etf("AAA"), etf("BBB"), etf("CCC")], FakeSource(ch))
    assert order(res) == ["CCC", "AAA", "BBB"]
    c = {x.obs.underlying: x.components for x in res.candidates}
    assert abs(c["AAA"].spread_fraction_of_mid - D("0.076923")) < D("0.000001")
    assert abs(c["BBB"].bid_yield_per_calendar_day_pct - D("0.071429")) < D("0.000001")
    assert abs(c["AAA"].bid_yield_per_calendar_day_pct - D("0.019048")) < D("0.000001")


def test_open_interest_breaks_a_liquidity_and_yield_tie():
    ch = rows_for(("AAA", 40, 45, 0.5, 0.54, 600), ("BBB", 40, 45, 0.5, 0.54, 2000))
    assert order(scan(make_journal(), [etf("AAA"), etf("BBB")], FakeSource(ch))) == ["BBB", "AAA"]


def test_full_ties_are_permutation_invariant_and_deterministic():
    """Equal on every ranking component: the choice must not depend on whitelist order (reproducibility)."""
    ch = rows_for(*[(t, 40, 45, 0.5, 0.54, 1000) for t in ("DDD", "AAA", "CCC", "BBB")])
    a = scan(make_journal(), [etf(t) for t in ("DDD", "AAA", "CCC", "BBB")], FakeSource(ch))
    b = scan(make_journal(), [etf(t) for t in ("BBB", "CCC", "AAA", "DDD")], FakeSource(ch))
    c = scan(make_journal(), [etf(t) for t in ("DDD", "AAA", "CCC", "BBB")], FakeSource(ch))
    assert order(a) == order(c)                    # same input -> same output
    assert order(a) == order(b), (order(a), order(b))   # permutation invariance


def test_same_input_gives_identical_message_text():
    ch = rows_for(("AAA", 75, 80, 0.5, 0.54, 1000), ("BBB", 40, 45, 0.5, 0.55, 1000))
    a = scan(make_journal(), [etf("AAA"), etf("BBB")], FakeSource(ch))
    b = scan(make_journal(), [etf("AAA"), etf("BBB")], FakeSource(ch))
    assert [m.text for m in a.messages] == [m.text for m in b.messages]
    assert [m.dedup_key for m in a.messages] == [m.dedup_key for m in b.messages]


def test_max_three_alerts_per_scan():
    ch = rows_for(*[(t, 20, 25, 0.5, 0.54, 1000 + i) for i, t in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE"))])
    res = scan(make_journal(), [etf(t) for t in ("AAA", "BBB", "CCC", "DDD", "EEE")], FakeSource(ch))
    assert len(res.candidates) == 3 and kinds(res) == ["ENTRY_RESEARCH"] * 3 and res.contracts_evaluated == 5
    assert order(res) == ["EEE", "DDD", "CCC"]                     # OI descending breaks the tie


def test_max_alerts_follows_policy_value():
    pol = policy_variant(entries={"max_alternative_alerts_per_scan": 1})
    ch = rows_for(*[(t, 20, 25, 0.5, 0.54, 1000) for t in ("AAA", "BBB", "CCC")])
    assert len(scan(make_journal(), [etf(t) for t in ("AAA", "BBB", "CCC")], FakeSource(ch), pol=pol).candidates) == 1


def test_one_alert_per_underlying_best_ranked_wins():
    # AAA: s75 .50/.54 (7.69%), s70 .50/.53 (.03/.515 = 5.83%), s65 .50/.54 (7.69%). All pass individually.
    rows = [raw_row(strike=75.0), raw_row(strike=70.0, ask=0.53), raw_row(strike=65.0)]
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": rows}))
    assert res.contracts_evaluated == 3 and len(res.candidates) == 1 and res.candidates[0].obs.strike == D("70.0")


def test_stacking_sector_cap():
    # BBB strike 50 (5000) outranks AAA strike 60 (6000) on yield (tie on liquidity); together 11,000 > 10,000 sector cap
    ch = rows_for(("AAA", 60, 65, 0.5, 0.54, 1000), ("BBB", 50, 55, 0.5, 0.54, 1000))
    res = scan(make_journal(), [etf("AAA", sector="tech", bucket="b1"), etf("BBB", sector="tech", bucket="b2")], FakeSource(ch))
    assert order(res) == ["BBB"] and res.excluded_after_revalidation == 1
    assert res.reason_counts["STACKED_SECTOR_LIMIT"] == 1 and res.reason_counts["SECTOR_LIMIT"] == 0


def test_stacking_correlation_bucket_cap():
    ch = rows_for(("AAA", 60, 65, 0.5, 0.54, 1000), ("BBB", 50, 55, 0.5, 0.54, 1000))
    res = scan(make_journal(), [etf("AAA", sector="s1", bucket="bx"), etf("BBB", sector="s2", bucket="bx")], FakeSource(ch))
    assert order(res) == ["BBB"] and res.reason_counts["STACKED_CORRELATED_LIMIT"] == 1


@pytest.mark.parametrize("k1,k2,both", [(70, 80, True), (75, 80, False), (80, 80, False)])
def test_stacking_pilot_collateral_cap_boundary(k1, k2, both):
    # same liquidity; lower strike ranks first. 7,000+8,000 = 15,000 == cap -> both; 7,500+8,000 = 15,500 -> second blocked.
    ch = rows_for(("AAA", k1, k1 + 5, 0.5, 0.54, 1000), ("BBB", k2, k2 + 5, 0.5, 0.54, 1000))
    if k1 == k2:      # equal strike: deterministic tie would be needed; use OI to rank AAA first
        ch["AAA"][0]["open_interest"] = 5000
    res = scan(make_journal(), [etf("AAA"), etf("BBB")], FakeSource(ch))
    assert (len(res.candidates) == 2) is both, dict(res.reason_counts)
    if not both:
        assert res.reason_counts["STACKED_PILOT_COLLATERAL_LIMIT"] == 1


def test_stacking_16000_never_passes_under_default_pilot_even_in_different_weeks():
    ch = {"AAA": [raw_row(strike=80.0, spot=85.0)], "BBB": [raw_row(strike=80.0, spot=85.0, expiration=EXP_W50.isoformat(), open_interest=900)]}
    res = scan(make_journal(), [etf("AAA"), etf("BBB")], FakeSource(ch))
    assert len(res.candidates) == 1 and res.reason_counts["STACKED_PILOT_COLLATERAL_LIMIT"] == 1


def test_stacking_expiry_week_cap_isolated_with_pilot_disabled():
    pol = policy_variant(pilot={"enabled": False})
    same_week = {"AAA": [raw_row(strike=80.0, spot=85.0)], "BBB": [raw_row(strike=80.0, spot=85.0, open_interest=900)]}
    res = scan(make_journal(), [etf("AAA"), etf("BBB")], FakeSource(same_week), pol=pol)
    assert len(res.candidates) == 1 and res.reason_counts["STACKED_EXPIRY_WEEK_LIMIT"] == 1      # 16,000 > 15,000 in week 49
    diff_week = {"AAA": same_week["AAA"], "BBB": [raw_row(strike=80.0, spot=85.0, expiration=EXP_W50.isoformat(), open_interest=900)]}
    res = scan(make_journal(), [etf("AAA"), etf("BBB")], FakeSource(diff_week), pol=pol)
    assert len(res.candidates) == 2                                                               # 8,000 per week, 16,000 < 25,000


def test_stacking_net_free_cash_is_consumed_by_earlier_picks():
    # cash 10,000: AAA (strike 75 -> 7,500) first on yield? make AAA rank first by OI tie-break at equal strike? use strikes:
    # BBB strike 40 (4,000) has higher yield -> ranks first; then AAA 7,500: 10,000-4,000 = 6,000 < 7,500 -> excluded.
    ch = rows_for(("AAA", 75, 80, 0.5, 0.54, 1000), ("BBB", 40, 45, 0.5, 0.54, 1000))
    res = scan(make_journal(cash="10000"), [etf("AAA"), etf("BBB")], FakeSource(ch))
    assert order(res) == ["BBB"] and res.reason_counts["STACKED_INSUFFICIENT_FREE_CASH"] == 1


def test_stacking_company_name_cap_but_etfs_unlimited():
    ch = rows_for(*[(t, 20, 25, 0.5, 0.54, 1000 - i) for i, t in enumerate(("AAA", "BBB", "CCC"))])
    res = scan(make_journal(), [appr(t) for t in ("AAA", "BBB", "CCC")], FakeSource(ch))     # STOCKs: max 2 names
    assert len(res.candidates) == 2 and res.reason_counts["STACKED_INDIVIDUAL_NAME_LIMIT"] == 1
    res = scan(make_journal(), [etf(t) for t in ("AAA", "BBB", "CCC")], FakeSource(ch))
    assert len(res.candidates) == 3


def test_stacking_excluded_middle_candidate_does_not_stop_later_ones():
    # P (70, ranks 1st), Q (85: 7,000+8,500 = 15,500 > 15,000 -> excluded), R (30, wider spread -> ranks last; 10,000 ok)
    ch = rows_for(("PPP", 70, 75, 0.5, 0.54, 1000), ("QQQ", 85, 90, 0.5, 0.54, 1000), ("RRR", 30, 35, 0.2, 0.22, 1000))
    res = scan(make_journal(), [etf("PPP"), etf("QQQ"), etf("RRR")], FakeSource(ch))
    assert order(res) == ["PPP", "RRR"] and res.excluded_after_revalidation == 1


def test_stacking_contract_count_cap_with_existing_book():
    c = [NOW - timedelta(hours=6)]
    j = Journal(":memory:", clock=lambda: c[0])
    j.migrate()
    j.confirm_baseline(D("100000"))
    for i in range(2):                                   # two existing OPEN ETF puts, 1,000 each
        tid = f"E{i}"
        j.create_draft(NewPosition(tid, f"EX{i}", EXP_W50, D("10"), 100, 1, f"se{i}", f"be{i}"))
        j.transition(tid, S.PENDING)
        j.transition(tid, S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.5"), opened_at_utc=c[0])
    c[0] = NOW - timedelta(hours=2)
    j.add_snapshot(AccountSnapshot("s", c[0], D("100000"), D("60000"), True, "v1"))
    c[0] = NOW
    ch = rows_for(*[(t, 20, 25, 0.5, 0.54, 1000 - i) for i, t in enumerate(("AAA", "BBB", "CCC"))])
    res = scan(j, [etf(t) for t in ("AAA", "BBB", "CCC")], FakeSource(ch))
    assert len(res.candidates) == 2 and res.reason_counts["STACKED_CONTRACT_COUNT_LIMIT"] == 1       # 2 existing + 2 = 4 max


def test_existing_same_ticker_put_blocks_scan_candidate():
    c = [NOW - timedelta(hours=6)]
    j = Journal(":memory:", clock=lambda: c[0])
    j.migrate()
    j.confirm_baseline(D("100000"))
    j.create_draft(NewPosition("E0", "AAA", EXP_W50, D("10"), 100, 1, "x", "y"))
    j.transition("E0", S.PENDING)
    c[0] = NOW - timedelta(hours=2)
    j.add_snapshot(AccountSnapshot("s", c[0], D("100000"), D("60000"), True, "v1"))
    c[0] = NOW
    res = scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row()]}))
    assert res.status == "NO_TRADE" and res.reason_counts["CONTRACT_COUNT_LIMIT"] == 1


def test_ai_enabled_without_reviewer_fails_closed():
    pol = policy_variant(ai={"enabled": True})
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": [raw_row()]}), pol=pol)
    assert res.ai_status == "UNAVAILABLE" and res.candidates == [] and res.reason_counts["AI_UNAVAILABLE"] == 1
    assert "ENTRY_RESEARCH" not in kinds(res)


def test_scan_leaves_ledger_reconciled_and_repeatable():
    j = make_journal()
    for _ in range(2):
        assert scan(j, [appr("AAA")], FakeSource({"AAA": [raw_row()]})).status == "CANDIDATES"
    assert not reconcile(j.load_book(), POL, NOW).blocking and j.load_book().last_ledger_change_utc is None
    assert count(j, "market_observations") == 2


def test_unconfirmed_deliverable_blocks_scan_candidates_as_nonstandard():
    res = scan(make_journal(), [appr("AAA", confirmed=False)], FakeSource({"AAA": [raw_row()]}))
    assert res.status == "NO_TRADE" and res.reason_counts["NONSTANDARD_CONTRACT"] == 1
    old = appr("AAA", confirmed=False, standard_deliverable_confirmed_on=(TODAY - timedelta(days=8)).isoformat())
    assert scan(make_journal(), [old], FakeSource({"AAA": [raw_row()]})).status == "NO_TRADE"
    ok = appr("AAA", confirmed=False, standard_deliverable_confirmed_on=(TODAY - timedelta(days=7)).isoformat())
    assert scan(make_journal(), [ok], FakeSource({"AAA": [raw_row()]})).status == "CANDIDATES"


def test_adjusted_symbol_blocks_scan_even_with_confirmation():
    res = scan(make_journal(), [appr("AAA")], FakeSource({"AAA": [raw_row(contractSymbol="AAA1261204P00075000")]}))
    assert res.status == "NO_TRADE" and res.reason_counts["NONSTANDARD_CONTRACT"] == 1
