"""Entry research scan (spec 7.1/7.2). Advisory only: returns messages; this module never sends anything.

Order (fail closed at each step): session -> ledger/drawdown latch -> whitelist -> per-ticker fetch ->
normalize -> evaluate EVERY put -> persist -> (AI stage no-op) -> rank passing -> select max N (one per
underlying) with revalidation against a hypothetical book that already holds earlier picks.

Ranking (spec 7.2; ordering indicator only, never a return forecast), lexicographic:
  1. liquidity quality: spread/mid ascending            (tighter market first)
  2. bid yield per calendar day = 100 * bid / (strike * DTE) descending
  3. IV excess (pp) descending (None sorts last; never invented)
  4. open interest descending
All components are exposed on the Candidate. Premium never overrides a gate.
"""
from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from csp_alerts.alerts import templates
from csp_alerts.journal.models import BookState, PositionState, PutPosition
from csp_alerts.journal.reconcile import exchange_date
from csp_alerts.jobs.runmeta import run_context
from csp_alerts.journal.store import Journal
from csp_alerts.market import calendar as mcal
from csp_alerts.market import failures
from csp_alerts.market.models import MarketObservation, SessionInfo, SessionStatus
from csp_alerts.market.normalize import normalize_rows
from csp_alerts.market.stockpile import ChainSource, SourceError
from csp_alerts.policy.approved import ApprovedUnderlying
from csp_alerts.policy.config import Policy
from csp_alerts.policy.entry import GateDecision, book_entry_blocks, evaluate_entry
from csp_alerts.policy.portfolio import compute_exposure
from csp_alerts.policy.reasons import Reason


@dataclass(frozen=True)
class RankComponents:
    spread_fraction_of_mid: Decimal
    bid_yield_per_calendar_day_pct: Decimal
    iv_excess_pp: Decimal | None
    open_interest: int
    daily_volume: int

    def sort_key(self):
        iv = self.iv_excess_pp
        return (self.spread_fraction_of_mid, -self.bid_yield_per_calendar_day_pct,
                (1, Decimal(0)) if iv is None else (0, -iv), -self.open_interest)


@dataclass(frozen=True)
class Candidate:
    obs: MarketObservation
    approval: ApprovedUnderlying
    decision: GateDecision
    observation_id: str
    components: RankComponents
    snapshot_asof_utc: datetime | None = None
    put_collateral_before_usd: Decimal | None = None


@dataclass
class ScanResult:
    run_id: str
    started_at_utc: datetime
    status: str  # BLOCKED | NO_TRADE | DATA_UNAVAILABLE | CANDIDATES
    reason_counts: Counter = field(default_factory=Counter)
    candidates: list[Candidate] = field(default_factory=list)
    messages: list[templates.AlertMessage] = field(default_factory=list)
    data_issues: list[tuple[str, str, str]] = field(default_factory=list)  # (ticker, kind, message)
    ai_status: str = "DISABLED"
    contracts_evaluated: int = 0
    tickers_scanned: int = 0
    excluded_after_revalidation: int = 0
    notes: list[str] = field(default_factory=list)
    coverage: list[tuple[str, str, str]] = field(default_factory=list)  # (ticker, OK|UNAVAILABLE, detail)


def deliverable_multiplier(a: ApprovedUnderlying, policy: Policy, today: date) -> int | None:
    """100 only if the owner attested a standard deliverable recently; Yahoo data carries no multiplier."""
    d = a.standard_deliverable_confirmed_on
    if d is None:
        return None
    age = (today - d).days
    return 100 if 0 <= age <= policy.entries.manual_event_review_max_age_calendar_days else None


def _session_block(session: SessionInfo, policy: Policy) -> list[Reason]:
    if session.status is SessionStatus.UNKNOWN or (session.status is SessionStatus.OPEN and session.minutes_to_close is None):
        return [Reason.UNKNOWN_SESSION]
    if session.status is SessionStatus.CLOSED:
        return [Reason.MARKET_CLOSED]
    if session.minutes_to_close < policy.market.no_entry_in_final_minutes_of_session:
        return [Reason.MARKET_CLOSED]
    return []


def _components(o: MarketObservation, dte: int) -> RankComponents:
    mid = (o.bid + o.ask) / 2
    return RankComponents(
        spread_fraction_of_mid=(o.ask - o.bid) / mid,
        bid_yield_per_calendar_day_pct=Decimal(100) * o.bid / (o.strike * dte),
        iv_excess_pp=o.iv_excess_pp, open_interest=o.open_interest, daily_volume=o.daily_volume)


def _with_hypothetical(book: BookState, c: Candidate, n: int, scope: str) -> BookState:
    """Book as if the owner had entered candidate `c` (PENDING reserves collateral and consumes free cash)."""
    o, a = c.obs, c.approval
    hypo = PutPosition(
        trade_id=f"HYPO-{n}", account_scope=scope, paper=True, ticker=o.underlying, expiration=o.expiration,
        strike=o.strike, multiplier=o.multiplier, contracts=1, sector=a.sector, correlation_bucket=a.correlation_bucket,
        state=PositionState.PENDING, asset_type=a.asset_type)
    latest = book.latest_snapshot
    snaps = tuple(
        replace(s, free_unencumbered_cash_usd=s.free_unencumbered_cash_usd - o.strike * o.multiplier)
        if latest is not None and s.snapshot_id == latest.snapshot_id else s for s in book.snapshots)
    return replace(book, positions=book.positions + (hypo,), snapshots=snaps)


def _run_scan(
    *, journal: Journal, policy: Policy, approved: list[ApprovedUnderlying], source: ChainSource, now_utc: datetime,
    session: SessionInfo | None = None, sleep: Callable[[float], None] = time.sleep, tz: str = "America/Chicago",
    persist: bool = True,
) -> ScanResult:
    run_id = str(uuid.uuid4())
    res = ScanResult(run_id=run_id, started_at_utc=now_utc, status="NO_TRADE")
    today = exchange_date(now_utc)

    # 1. calendar
    session = session if session is not None else mcal.session_info(now_utc)
    block = _session_block(session, policy)
    # 2. ledger / drawdown latch. First re-check recorded policy-breach flags against the current book/approvals/labels/policy
    # (audited POLICY_BREACH_CLEARED when one no longer reproduces); a failure here leaves the flags in place (fail closed).
    if persist:
        try:
            from csp_alerts.journal.webtrade import sweep_breaches
            sweep_breaches(journal, policy, approved, now_utc, reason="re-checked before scan", actor="system:scan", source="scan")
        except Exception:  # noqa: BLE001
            pass
    book = journal.load_book()
    book_codes, _dd = book_entry_blocks(book, policy, now_utc)
    block += sorted(book_codes, key=lambda r: r.value)
    if block:
        res.status = "BLOCKED"
        res.reason_counts.update(r.value for r in block)
        ledger = {Reason.LEDGER_STALE, Reason.LEDGER_UNRECONCILED}
        if Reason.HARD_HALT in block:
            kind, sev = "ACCOUNT_RISK", "CRITICAL"          # spec 13.3: 8% halt is CRITICAL
        elif Reason.DRAWDOWN_PAUSE in block:
            kind, sev = "ACCOUNT_RISK", "URGENT"            # 3%/5% levels
        elif ledger & set(block):
            kind, sev = "RECONCILIATION_REQUIRED", "WARN"
        else:
            kind, sev = "NO_TRADE_DIGEST", "INFO"
        level = _dd.level.value if kind == "ACCOUNT_RISK" else None  # distinct key per level: escalation is never deduped away
        res.messages.append(templates.blocked_scan([r.value for r in block], now_utc, kind, sev, tz, level))
        return res

    # 3. whitelist
    if not approved:
        res.reason_counts["EMPTY_WHITELIST"] += 1
        return _finish_no_trade(res, journal, now_utc, tz, policy)

    # 4-6. fetch, normalize, evaluate every put
    snap_asof = book.latest_snapshot.asof_utc if book.latest_snapshot else None
    coll_before = compute_exposure(book).put_collateral_usd
    passing: list[Candidate] = []
    obs_rows: list[tuple[str, dict, str]] = []
    eval_rows: list[tuple[str | None, dict]] = []
    for a in approved:
        res.tickers_scanned += 1
        mult = deliverable_multiplier(a, policy, today)
        raw = _fetch_with_retries(source, a.ticker, policy, res, journal, sleep, run_id)
        if raw is None:
            continue
        if not raw.rows:
            _issue(res, journal, a.ticker, "EMPTY_CHAIN", "provider returned no put rows in the DTE window", run_id)
            continue
        try:
            earnings = source.fetch_earnings_dates(a.ticker)
        except Exception:  # noqa: BLE001
            earnings = None
        norm = normalize_rows(a.ticker, raw.rows, raw.fetched_at_utc, today_exchange=today, earnings_dates=earnings,
                              multiplier=mult, rows_seen_upstream=raw.rows_seen_upstream)
        if norm.degraded or not norm.rows:
            _issue(res, journal, a.ticker, "DEGRADED_CHAIN", "chain is mostly zeroed/unusable (throttled?)", run_id)
            continue
        res.coverage.append((a.ticker, "OK", f"{len(norm.rows)} puts"))
        for row in norm.rows:
            d = evaluate_entry(row.observation, a, book, policy, now_utc, session)
            res.contracts_evaluated += 1
            res.reason_counts.update(r.value for r in d.reason_codes)
            obs_rows.append((row.observation_id, row.observation.to_dict(), row.raw_sha256))
            eval_rows.append((row.observation_id, d.to_dict()))
            if d.passed:
                o = row.observation
                dte = (o.expiration - today).days
                passing.append(Candidate(o, a, d, row.observation_id, _components(o, dte), snap_asof, coll_before))
    if persist:
        journal.record_observations(obs_rows)
        journal.record_evaluations(run_id, eval_rows)

    # 7. AI stage (disabled = no-op; enabled but no reviewer implemented = fail closed)
    if policy.ai.enabled:
        res.ai_status = "UNAVAILABLE"
        res.reason_counts[Reason.AI_UNAVAILABLE.value] += len(passing)
        passing = []
    else:
        res.ai_status = "DISABLED"

    # 8. rank passing, select with revalidation
    # final tie-breakers make the order independent of whitelist/provider ordering (reproducible)
    passing.sort(key=lambda c: (*c.components.sort_key(), c.obs.underlying, c.obs.expiration, c.obs.strike))
    chosen: list[Candidate] = []
    hypo_book = book
    for c in passing:
        if len(chosen) >= policy.entries.max_alternative_alerts_per_scan:
            break
        if any(x.obs.underlying == c.obs.underlying for x in chosen):
            continue  # one alternative per underlying
        if chosen:  # revalidate against the book that already includes earlier picks (spec 4.5)
            d2 = evaluate_entry(c.obs, c.approval, hypo_book, policy, now_utc, session)
            if not d2.passed:
                res.excluded_after_revalidation += 1
                res.reason_counts.update(f"STACKED_{r.value}" for r in d2.reason_codes)
                continue
            # keep the stand-alone decision (evaluated against the real book) for display: each alert is an
            # alternative and its exposure figures must not silently include earlier picks
        chosen.append(c)
        hypo_book = _with_hypothetical(hypo_book, c, len(chosen), policy.account_scope)
    res.candidates = chosen

    if chosen:
        res.status = "CANDIDATES"
        res.messages += [templates.entry_alert(c, policy, tz) for c in chosen]
    elif res.data_issues:
        res.status = "DATA_UNAVAILABLE"
    if res.data_issues:
        if not chosen and any(st == "OK" for _, st, _ in res.coverage):
            # partial failure: keep the healthy symbols' reason counts visible, clearly labelled PARTIAL
            res.messages.append(templates.no_trade_digest(
                "DATA UNAVAILABLE FOR SOME SYMBOLS", res.reason_counts, now_utc, evaluated=res.contracts_evaluated,
                tickers=res.tickers_scanned, tz=tz, coverage=res.coverage, partial=True))
        res.messages.append(templates.data_issue(res.data_issues, now_utc, tz, res.coverage))
    if not chosen and not res.data_issues:
        return _finish_no_trade(res, journal, now_utc, tz, policy)
    return res


def _finish_no_trade(res: ScanResult, journal: Journal, now_utc: datetime, tz: str, policy: Policy | None = None) -> ScanResult:
    res.status = "NO_TRADE"
    from csp_alerts.jobs.status import heartbeat_line
    hb = None
    if policy is not None:
        try:
            hb = heartbeat_line(journal, policy, now_utc, tz)
        except Exception:  # noqa: BLE001 - the heartbeat line is a nicety; never break the digest
            hb = None
    if journal.digest_sent_on(exchange_date(now_utc)):
        res.notes.append("NO_TRADE digest already sent today; suppressed")
    else:
        res.messages.append(templates.no_trade_digest("NO TRADE", res.reason_counts, now_utc,
                                                      evaluated=res.contracts_evaluated, tickers=res.tickers_scanned, tz=tz,
                                                      heartbeat=hb, coverage=res.coverage))
    return res


def _issue(res: ScanResult, journal: Journal, ticker: str, kind: str, msg: str, run_id: str) -> None:
    res.data_issues.append((ticker, kind, msg))
    res.coverage.append((ticker, "UNAVAILABLE", kind))
    journal.record_data_issue(ticker, kind, msg, run_id=run_id)


def _fetch_with_retries(source, ticker, policy, res, journal, sleep, run_id):
    attempts = 1 + policy.market.provider_retries
    last: SourceError | None = None
    for i in range(attempts):
        try:
            return source.fetch_chain(ticker, policy.entries.min_dte_calendar, policy.entries.max_dte_calendar)
        except SourceError as exc:
            last = exc
            if exc.kind in ("RATE_LIMIT", "STOCKPILE_MISSING", "STOCKPILE_IMPORT"):
                break  # retrying would not help (or would hammer a throttled provider)
        except Exception as exc:  # noqa: BLE001
            last = SourceError.from_exception(exc)   # canned code only; a hostile __str__ cannot escape
        if i + 1 < attempts:
            sleep(2 ** i)
    # canned code + canned message only: raw provider/exception text never reaches the audit log, coverage or Telegram
    _issue(res, journal, ticker, failures.safe_kind(getattr(last, "kind", None)), failures.stored_for(last), run_id)
    return None


def run_scan(**kw) -> ScanResult:
    """Run the scan and record its F1 run status (exactly one owner-visible status per run)."""
    from csp_alerts.jobs.status import scan_run_status

    res = _run_scan(**kw)
    if kw.get("persist", True):
        kw["journal"].record_run(
            res.run_id, "SCAN", res.started_at_utc, scan_run_status(res.status, set(res.reason_counts)),
            reasons=[k for k in res.reason_counts if k.isupper() and not k.startswith("STACKED_")][:20],
            summary={"tickers": res.tickers_scanned, "evaluated": res.contracts_evaluated,
                     "candidates": len(res.candidates), "data_issues": len(res.data_issues),
                     "excluded_after_revalidation": res.excluded_after_revalidation, "ai_status": res.ai_status,
                     "coverage": [{"ticker": t, "status": st, "detail": d} for t, st, d in res.coverage],
                     **run_context(kw["journal"], kw["policy"])})
    return res
