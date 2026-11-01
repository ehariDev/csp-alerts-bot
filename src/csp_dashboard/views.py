"""Read/query layer (WD-018). Every function takes a ReadOnlyJournal inside ONE snapshot() transaction and returns plain
data (Decimals/datetimes kept exact; see `jsonable`). Nothing here writes, scans, sends, migrates or syncs.

Money math is NOT done here: exposure, drawdown, reconciliation, exit triggers and indicative P&L come from the
existing backend functions (csp_alerts.policy.*, csp_alerts.journal.reconcile). The only arithmetic is the limit
headroom `room = limit - used` (Decimal subtraction)."""
from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from csp_alerts.policy.alert_reasons import display_reason
from csp_alerts.journal import watchlist as _wl
from csp_alerts.journal import weblabels as wl
from csp_alerts.journal import webtrade as wt
from csp_alerts.journal.queries import delivery_summary
from csp_alerts.jobs.status import collect_status
from csp_alerts.journal.models import COLLATERAL_STATES, MONITORED_STATES, PositionState as S, PutPosition, from_iso, norm_label, to_iso
from csp_alerts.journal.reconcile import exchange_date, reconcile
from csp_alerts.journal.store import MIGRATIONS_DIR
from csp_alerts.market import calendar as mcal
from csp_alerts.market import freshness as _fr
from csp_alerts.market import observations as _obs
from csp_alerts.market.models import MarketObservation, SessionInfo, SessionStatus
from csp_alerts.money import dec, usd2
from csp_alerts.policy.approved import (ApprovedUnderlying, asset_type_label, asset_type_map, load_approved,
                                        resolve_asset_type)
from csp_alerts.policy.browse import NOT_EVALUATED, contract_gate_codes, observation_from_row
from csp_alerts.policy.config import Policy, load_policy
from csp_alerts.policy.drawdown import compute_drawdown
from csp_alerts.policy.entry import book_entry_blocks, evaluate_entry
from csp_alerts.policy.exits import OptionQuote, Trigger, estimated_pnl_usd, evaluate_exits
from csp_alerts.policy.portfolio import (
    UNKNOWN_LABEL, ProposedPut, capacity_rows, company_names, compute_exposure, exposure_groups, portfolio_limits)
from csp_alerts.version import code_identity

from csp_alerts.market import failures as _fail
from csp_dashboard.readonly import JournalMissing, ReadOnlyJournal

PAGE_SIZE = 50
PASS = ("PASS_RESEARCH_ONLY", "PASS_VERIFIED_DATA")
GROUPS: dict[str, tuple[S, ...]] = {
    "active": (S.OPEN, S.CLOSE_WORKING),
    "reserved": (S.PENDING, S.WORKING),
    "review": (S.ADJUSTMENT_REVIEW, S.ASSIGNED_PENDING_RECONCILIATION),
    "history": (S.CLOSED, S.CANCELED, S.EXPIRED, S.ASSIGNED_RECONCILED),
    "draft": (S.DRAFT,),
}
GROUPS["live"] = GROUPS["active"] + GROUPS["reserved"] + GROUPS["review"]
GROUPS["all"] = tuple(S)
OUTBOX_LABEL = {
    "PENDING": "queued", "SENDING": "sending", "RETRY": "retry scheduled", "SENT": "acknowledged by Telegram API",
    "UNKNOWN_ACK": "unknown acknowledgment (may or may not have arrived; never auto-resent)",
    "FAILED_FINAL": "failed (permanent)", "DROPPED_EXPIRED": "dropped (expired before send)",
    "DROPPED_STALE": "dropped (no longer valid at send time)",
}
CHECKIN = "Run `csp daily-check-in` (preview), then confirm it with --apply (RUNBOOK section 3)."
STEPS = {
    "NO_SNAPSHOT": CHECKIN, "SNAPSHOT_STALE": CHECKIN, "SNAPSHOT_UNCONFIRMED": CHECKIN, "DAILY_CHECKIN_MISSING": CHECKIN,
    "CHANGE_AFTER_SNAPSHOT": CHECKIN,
    "CASH_INCONSISTENT": "Re-run `csp daily-check-in` with NET free cash (already reduced by put collateral) (RUNBOOK section 3).",
    "SNAPSHOT_FUTURE": "Correct the snapshot date: re-run `csp daily-check-in` with the right time.",
    "NO_BASELINE": "Run `csp journal confirm-baseline --equity <starting equity>` once (RUNBOOK section 1).",
    "MIXED_ACCOUNT_SCOPE": "Correct the position record so every position uses the configured account scope.",
    "REAL_POSITION_IN_PAPER_SCOPE": "Correct the record: this version is paper-only.",
    "MULTI_CONTRACT_POSITION": "Only 1 contract per position is supported; resolve with your broker records, then correct the journal.",
    "STOCK_MARK_STALE": "Re-mark owned shares: `csp daily-check-in ... --mark LOT=PRICE` (RUNBOOK section 3).",
    "STOCK_MARK_FUTURE": "Correct the stock mark date via `csp daily-check-in --mark`.",
    "ASSIGNMENT_PENDING": "Finish assignment step 2 (`csp journal assign --shares ... --cost-basis-usd ...`) (RUNBOOK section 5).",
    "ADJUSTMENT_REVIEW_PENDING": "Verify the adjusted contract at your broker; resolve it in the journal.",
    "EXPIRY_UNCONFIRMED": "Record the outcome: `csp journal expire`, `close` or `assign` (RUNBOOK section 4).",
    "ASSIGNED_WITHOUT_LOT": "Complete assignment step 2 so a stock lot is linked (RUNBOOK section 5).",
    "OPEN_WITHOUT_FILL": "Record the fill: `csp journal fill --trade-id ... --credit ...`.",
    "DOUBLE_COUNT_RISK": "A stock lot is linked to a put that still reserves collateral; review the journal.",
    "POLICY_BREACH_UNRESOLVED": "No action button exists for this. It clears by itself when the recorded breach no longer reproduces (a cap is no longer exceeded, labels or approvals changed, or the position closes), re-checked after each record, scan and ledger command.",
    "MULTIPLE_ACTIVE_PUTS_SAME_UNDERLYING": "Review concentration; more than the allowed contracts are active on one underlying.",
}
LATEST_SCHEMA = max(p.name[:4] for p in MIGRATIONS_DIR.glob("0*.sql"))


def jsonable(v: Any) -> Any:
    """Exact JSON form: Decimal -> plain-notation string, datetime -> UTC ISO 'Z', date -> ISO, enums -> value."""
    if isinstance(v, Decimal):
        return format(v, "f") if v.is_finite() else str(v)
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, date):
        return v.isoformat()
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return jsonable(dataclasses.asdict(v))
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [jsonable(x) for x in v]
    if hasattr(v, "value") and not isinstance(v, (str, int)):
        return v.value
    return v


@dataclass
class Env:
    """Everything one request needs. `policy`/`approved` are None (with an error string) when unreadable."""
    rj: ReadOnlyJournal
    now: datetime
    tz: str
    policy: Policy | None
    policy_error: str | None
    approved: list[ApprovedUnderlying] | None
    approved_error: str | None
    _status: dict | None = field(default=None, repr=False)

    def status(self) -> dict | None:
        if self.policy is None:
            return None
        if self._status is None:
            self._status = collect_status(self.rj, self.policy, self.now, self.tz)
        return self._status

    def approval(self, ticker: str) -> ApprovedUnderlying | None:
        return next((a for a in (self.approved or []) if a.ticker == ticker), None)

    def asset_info(self, ticker: str) -> tuple[str, str]:
        """(asset type, source) through the single resolver also used by the book: journal table, approvals file, UNKNOWN."""
        return resolve_asset_type(ticker, self.rj.journal_asset_types(), asset_type_map(self.approved))


@dataclass
class Sources:
    db_path: Path
    policy_path: Path
    approved_path: Path
    tz: str
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _try(fn, default=None):
    try:
        return fn()
    except sqlite3.Error:
        return default


def meta(env: Env) -> dict[str, Any]:
    rj = env.rj
    schema = rj.schema_version()
    ident = code_identity()
    m: dict[str, Any] = {
        "rendered_at_utc": env.now, "display_timezone": env.tz, "schema_version": schema, "schema_expected": LATEST_SCHEMA,
        "code_revision": ident.label, "code_manifest_sha256": ident.manifest_sha256,
        "policy_sha256": env.policy.sha256 if env.policy else None,
        "policy_version": env.policy.policy_version if env.policy else None,
        "policy_id": env.policy.policy_id if env.policy else None,
        "ledger_revision": None, "audit_revision": None, "policy_recorded_in_journal": None, "observed": {},
    }
    if schema is None:
        return m
    m["ledger_revision"] = _try(lambda: rj.ledger_seqs()[1])
    m["audit_revision"] = _try(lambda: rj.conn.execute("SELECT COALESCE(MAX(rowid),0) FROM audit_log").fetchone()[0])
    if env.policy:
        m["policy_recorded_in_journal"] = _try(lambda: rj.conn.execute(
            "SELECT 1 FROM policy_versions WHERE policy_id=? AND sha256=?", (env.policy.policy_id, env.policy.sha256)).fetchone()
            is not None)
    snaps = _try(rj.list_snapshots, [])
    last_audit = _try(lambda: rj.conn.execute("SELECT MAX(created_at_utc) FROM audit_log").fetchone()[0])
    scan, mon = _try(lambda: rj.last_run("SCAN")), _try(lambda: rj.last_run("MONITOR"))
    m["observed"] = {
        "latest_snapshot_asof_utc": snaps[-1].asof_utc if snaps else None,
        "last_audit_event_utc": from_iso(last_audit) if last_audit else None,
        "last_scan_started_utc": scan["started_at"] if scan else None,
        "last_monitor_started_utc": mon["started_at"] if mon else None,
    }
    return m


# ---------------------------------------------------------------- shared: blockers + next manual steps
def attention(env: Env) -> dict[str, Any]:
    """Current blockers and manual next steps (rendered above everything else)."""
    items: list[dict[str, str]] = []
    steps: list[str] = []

    def add(code: str, text: str, step: str | None = None, severity: str = "BLOCK") -> None:
        items.append({"code": code, "text": text, "severity": severity})
        if step and step not in steps:
            steps.append(step)

    if env.policy is None:
        add("POLICY_INVALID", f"Policy file cannot be loaded: {env.policy_error}", "Fix config/policy.yaml; run `csp policy-check`.")
        return {"items": items, "steps": steps, "ok": False}
    schema = env.rj.schema_version()
    if schema is None:
        add("JOURNAL_NOT_INITIALISED", "The journal database has no schema.", "Run `csp journal init` (the dashboard never migrates).")
        return {"items": items, "steps": steps, "ok": False}
    if schema != LATEST_SCHEMA:
        add("SCHEMA_BEHIND", f"Journal schema {schema} differs from software schema {LATEST_SCHEMA}; some views may be incomplete.",
            "Run `csp journal init` after a backup (the dashboard never migrates).", "WARN")
    if env.approved is None:
        add("WHITELIST_UNREADABLE", f"Approval file cannot be read: {env.approved_error}", "Fix config/approved.json; run `csp approved-list`.")
    elif not env.approved:
        add("EMPTY_WHITELIST", "No owner-approved underlyings: no entry alerts can be produced (a valid safe state).",
            "Only after your own diligence: `csp journal approve ...` (RUNBOOK section 1).", "INFO")
    book = env.rj.load_book()
    report = reconcile(book, env.policy, env.now)
    for i in report.issues:
        add(i.code, f"{i.message}" + (f" [{i.ref}]" if i.ref else ""), STEPS.get(i.code), i.severity)
    codes, dd = book_entry_blocks(book, env.policy, env.now)
    if dd.latched:
        add("DRAWDOWN_LATCHED", f"Drawdown latch {dd.level.value} is active; entries stay blocked until a manual, reviewed reset.",
            "After reviewing positions and plan: `csp journal reset --reviewed --apply --reason ...` (RUNBOOK section 6).")
    st = env.status()
    for f in (st or {}).get("flags", []):
        if f in ("MONITOR_OVERDUE", "SCAN_MISSED_TODAY"):
            add(f, "A scheduled run is overdue or missed (PC asleep/offline, logged out, or task failed).",
                "Check the PC and the scheduled tasks (RUNBOOK section 9); run `csp health`.", "WARN")
        elif f.startswith("DELIVERY_") or f.endswith("SERVICE_FAILURE"):
            add(f, "Delivery or a job failed; see the health view.", "See RUNBOOK section 7 (outages).", "WARN")
    sess = mcal.session_info(env.now)
    if sess.status is not SessionStatus.OPEN:
        add("MARKET_CLOSED" if sess.status is SessionStatus.CLOSED else "UNKNOWN_SESSION",
            "US option market is closed (or the calendar is unavailable): no entry alerts are produced now.", None, "INFO")
    blocking = [i for i in items if i["severity"] == "BLOCK"]
    return {"items": items, "steps": steps, "ok": not blocking, "entries_blocked": bool(codes)}


# ---------------------------------------------------------------- overview (WD-010)
def _snap_view(s, now: datetime) -> dict[str, Any] | None:
    if s is None:
        return None
    return {"snapshot_id": s.snapshot_id, "asof_utc": s.asof_utc, "equity_usd": s.equity_usd,
            "free_cash_usd": s.free_unencumbered_cash_usd, "confirmed": s.manual_confirmed,
            "age_hours": Decimal((now - s.asof_utc) // timedelta(minutes=1)) / Decimal(60),
            "reconciliation_version": s.reconciliation_version}


def overview(env: Env) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "overview", "attention": attention(env)}
    pol = env.policy
    if pol is None or env.rj.schema_version() is None:
        return out
    book = env.rj.load_book()
    confirmed = [s for s in book.snapshots if s.manual_confirmed]
    ex = compute_exposure(book)
    dd = compute_drawdown(book.snapshots, pol, baseline_asof_utc=book.last_risk_reset_utc,
                          baseline_equity_usd=book.baseline_equity_usd, cash_flows=book.cash_flows)
    out["account"] = {"scope": pol.account_scope, "paper": all(p.paper for p in book.positions),
                      "profile": "pilot" if pol.pilot.enabled else "normal", "policy_id": pol.policy_id,
                      "policy_version": pol.policy_version, "baseline_equity_usd": book.baseline_equity_usd,
                      "baseline_confirmed_at_utc": book.baseline_confirmed_at_utc}
    out["latest_snapshot"] = _snap_view(book.latest_snapshot, env.now)
    out["latest_confirmed_snapshot"] = _snap_view(confirmed[-1] if confirmed else None, env.now)
    out["drawdown"] = {"level": dd.level.value, "current_level": dd.current_level.value, "latched": dd.latched,
                       "halt_latched": dd.halt_latched, "peak_equity_usd": dd.peak_equity_usd,
                       "latest_equity_usd": dd.latest_equity_usd, "drawdown_fraction": dd.drawdown,
                       "as_of_utc": dd.latest_asof_utc,
                       "thresholds": {"pause": pol.drawdown.pause_new_entries_at, "review": pol.drawdown.reduce_exposure_review_at,
                                      "halt": pol.drawdown.hard_halt_at}}
    out["exposure"] = {"put_collateral_usd": ex.put_collateral_usd, "owned_stock_usd": ex.owned_stock_usd,
                       "assigned_pending_usd": ex.assigned_pending_usd, "aggregate_usd": ex.aggregate_equity_risk_usd,
                       "active_contracts": ex.active_contracts,
                       "note": "Exposure budget (collateral + stock reference value), not maximum possible loss."}
    pf = pol.portfolio
    limits = []
    if pol.pilot.enabled:
        limits.append({"name": "Pilot put-collateral cap", "used": ex.put_collateral_usd,
                       "limit": pol.pilot.max_deployed_put_collateral_usd})
    limits.append({"name": "Normal ceiling: aggregate exposure", "used": ex.aggregate_equity_risk_usd,
                   "limit": pf.normal_new_entry_exposure_ceiling_usd})
    limits.append({"name": "Normal ceiling: put collateral", "used": ex.put_collateral_usd,
                   "limit": pf.normal_new_entry_exposure_ceiling_usd})
    for r in limits:
        r["room"] = r["limit"] - r["used"]
    tight = min(limits, key=lambda r: r["room"])
    for r in limits:
        r["binding"] = r is tight
    limits.append({"name": "Active short-put contracts", "used": ex.active_contracts, "limit": pf.max_active_short_put_contracts,
                   "room": pf.max_active_short_put_contracts - ex.active_contracts, "binding": False, "count": True})
    limits.append({"name": "Emergency ceiling (incident flag, never headroom)", "used": ex.aggregate_equity_risk_usd,
                   "limit": pf.emergency_absolute_exposure_ceiling_usd, "room": None, "binding": False,
                   "breached": ex.aggregate_equity_risk_usd > pf.emergency_absolute_exposure_ceiling_usd})
    out["limits"] = limits
    rep = reconcile(book, pol, env.now)
    out["reconciliation"] = {"blocking": rep.blocking, "issues": [
        {"code": i.code, "severity": i.severity, "message": i.message, "ref": i.ref} for i in rep.issues]}
    counts: dict[str, int] = {}
    for p in book.positions:
        counts[p.state.value] = counts.get(p.state.value, 0) + 1
    out["state_counts"] = counts
    out["obligations"] = [_pos_brief(p, env) for p in book.positions
                          if p.state in (S.PENDING, S.WORKING, S.CLOSE_WORKING, S.ADJUSTMENT_REVIEW,
                                         S.ASSIGNED_PENDING_RECONCILIATION)]
    out["stock_lots"] = [{"lot_id": l.lot_id, "ticker": l.ticker, "shares": l.shares, "reference_price": l.reference_price,
                          "mark_asof_utc": l.reference_asof_utc, "exposure_usd": l.exposure_usd,
                          "linked_trade_id": l.linked_trade_id} for l in book.stock_lots]
    return out


def _dte(p: PutPosition, env: Env) -> int:
    return (p.expiration - exchange_date(env.now)).days


def _pos_brief(p: PutPosition, env: Env) -> dict[str, Any]:
    return {"trade_id": p.trade_id, "ticker": p.ticker, "state": p.state.value, "expiration": p.expiration, "strike": p.strike,
            "contracts": p.contracts, "multiplier": p.multiplier, "collateral_usd": p.collateral_usd,
            "reserves_collateral": p.reserves_collateral, "dte": _dte(p, env)}


# ---------------------------------------------------------------- pagination
def _cursor(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() and 0 < int(v) < 2 ** 62 else None


def page(rj: ReadOnlyJournal, select: str, where: str, params: list, rid: str, before: str | None, after: str | None,
         limit: int = PAGE_SIZE) -> dict[str, Any]:
    """Keyset pagination on rowid: new rows arriving between page loads never shift or duplicate older records.
    `select` must expose the rowid as column `rid`. `before`/`after` are rowid cursors."""
    b, a = _cursor(before), _cursor(after)
    cond = where or "1=1"
    if a is not None:
        rows = rj.conn.execute(f"{select} WHERE {cond} AND {rid} > ? ORDER BY {rid} ASC LIMIT ?", (*params, a, limit + 1)).fetchall()
        more_newer = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        has_newer, has_older = more_newer, True
    else:
        extra, ps = (f" AND {rid} < ?", [*params, b]) if b is not None else ("", list(params))
        rows = rj.conn.execute(f"{select} WHERE {cond}{extra} ORDER BY {rid} DESC LIMIT ?", (*ps, limit + 1)).fetchall()
        has_older = len(rows) > limit
        rows = rows[:limit]
        has_newer = b is not None
    return {"rows": rows, "newer": rows[0]["rid"] if rows and has_newer else None,
            "older": rows[-1]["rid"] if rows and has_older else None}


def _j(s: str | None) -> Any:
    try:
        return json.loads(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- candidates (WD-011)
def _coverage(run: dict | None, issues: list[dict]) -> dict[str, Any]:
    """Per-symbol coverage of a scan run: recorded coverage when present, else derived from data-issue audit events."""
    summ = _j(run["summary_json"]) if run else None
    if summ and isinstance(summ.get("coverage"), list):
        cov = summ["coverage"]
        return {"source": "recorded", "ok": [c["ticker"] for c in cov if c["status"] == "OK"],
                "unavailable": [{"ticker": c["ticker"], "detail": c["detail"]} for c in cov if c["status"] != "OK"]}
    return {"source": "derived from data-issue events (older run record)",
            "ok": None, "unavailable": [{"ticker": i["ticker"], "detail": i["kind"]} for i in issues],
            "tickers_scanned": (summ or {}).get("tickers")}


def _run_issues(rj: ReadOnlyJournal, run_id: str) -> list[dict]:
    out = []
    for r in rj.conn.execute("SELECT created_at_utc, after_json FROM audit_log WHERE event_type='DATA_ISSUE' "
                             "AND json_extract(after_json,'$.run_id')=? ORDER BY rowid", (run_id,)):
        d = _j(r["after_json"]) or {}
        out.append({"at_utc": from_iso(r["created_at_utc"]), "ticker": d.get("ticker"), "kind": d.get("kind"),
                    "message": d.get("message")})
    return out


def pipeline_states(env: Env, latest: dict | None, evals: int, passed: int, cov: dict, summ: dict | None) -> list[dict]:
    """Distinct explanations for 'why is the candidate list like this' (never relax criteria to populate it)."""
    out: list[dict] = []

    def add(code, severity, title, text):
        out.append({"code": code, "severity": severity, "title": title, "text": text})

    if env.approved is not None and not env.approved:
        add("EMPTY_WHITELIST", "INFO", "Empty whitelist",
            "No underlying is owner-approved, so no contract is ever evaluated or alerted. This is the safe default, not an error.")
    if env.policy is not None:
        codes, _ = book_entry_blocks(env.rj.load_book(), env.policy, env.now)
        if codes:
            add("BLOCKED_LEDGER", "BLOCK", "Blocked ledger",
                "Entries are blocked by the manual ledger or drawdown latch: " + ", ".join(sorted(c.value for c in codes)) + ".")
    if latest is None:
        add("NEVER_SCANNED", "INFO", "No scan recorded", "No scan run is stored yet; nothing has been evaluated.")
        return out
    status = latest["status"]
    unavailable = cov.get("unavailable") or []
    if status == "DATA_UNAVAILABLE" or (summ or {}).get("data_issues"):
        ok = cov.get("ok")
        scanned = (summ or {}).get("tickers")
        none_ok = (ok is not None and not ok) or (ok is None and scanned and len(unavailable) >= scanned)
        if none_ok:
            add("PROVIDER_OUTAGE", "BLOCK", "Provider outage",
                "The data provider returned nothing usable for any scanned symbol. This is NOT a no-trade result; "
                "no candidates were evaluated.")
        else:
            add("PARTIAL_COVERAGE", "WARN", "Partial coverage",
                "Some symbols could not be evaluated because their data failed. Do not read the remaining list as a clean no-trade.")
    if status == "MARKET_CLOSED":
        add("MARKET_CLOSED", "INFO", "Market closed", "The last scan ran outside the entry session; nothing was evaluated.")
    if status == "BLOCKED_LEDGER" and not any(o["code"] == "BLOCKED_LEDGER" for o in out):
        add("BLOCKED_LEDGER", "BLOCK", "Blocked ledger (at last scan)", "The last scan was blocked by the ledger or drawdown latch.")
    if status == "NO_OPPORTUNITIES" and "EMPTY_WHITELIST" not in latest["reasons"] and not any(o["code"] == "EMPTY_WHITELIST" for o in out):
        add("NO_QUALIFYING_CONTRACTS", "INFO", "No qualifying contracts",
            "The last scan evaluated contracts and none passed every hard gate. Criteria are never relaxed to fill the list.")
    elif status in ("CANDIDATES",) and passed == 0:
        add("NO_QUALIFYING_CONTRACTS", "INFO", "No passing evaluation stored", "The scan reported candidates but none are stored.")
    if status == "SERVICE_FAILURE":
        add("SERVICE_FAILURE", "BLOCK", "Scan failed", "The last scan job crashed; nothing from it is trustworthy.")
    return out


def _cand_row(r: sqlite3.Row, env: Env, stale: bool) -> dict[str, Any]:
    d = _j(r["decision_json"]) or {}
    o = _j(r["quote_json"]) or {}
    ck = r["contract_key"]
    tk = ck.split("|")[0]
    bucket = "pass" if r["decision"] in PASS else "data" if r["decision"] == "DATA_UNAVAILABLE" else "rejected"
    met = d.get("metrics") or {}
    return {"evaluation_id": r["evaluation_id"], "rid": r["rid"], "run_id": r["run_id"], "contract_key": ck, "ticker": tk,
            "expiration": o.get("expiration"), "strike": o.get("strike"), "decision": r["decision"],
            "bucket": "stale" if (bucket == "pass" and stale) else bucket, "reason_codes": _j(r["reason_codes_json"]) or [],
            "evaluated_at_utc": from_iso(r["evaluated_at_utc"]), "policy_version": d.get("policy_version"),
            "bid": o.get("bid"), "ask": o.get("ask"), "delta": o.get("delta"), "delta_source": o.get("delta_source"),
            "spot": o.get("spot"), "dte": met.get("dte"), "open_interest": o.get("open_interest"),
            "daily_volume": o.get("daily_volume"), "spread_fraction_of_mid": met.get("spread_fraction_of_mid"),
            "collateral_usd": d.get("additional_collateral_usd"), "exposure_before_usd": d.get("exposure_before_usd"),
            "exposure_after_usd": d.get("exposure_after_usd"), "premium_at_bid_usd": met.get("premium_at_bid_usd"),
            "data_tier": d.get("data_tier") or o.get("data_tier"), "event_check": o.get("event_check"),
            "retrieved_at_utc": o.get("retrieved_at_utc"), "quote_at_utc": o.get("quote_at_utc"),
            "cautions": d.get("cautions") or [], "has_observation": bool(o)}


_CAND_SELECT = ("SELECT e.rowid AS rid, e.*, o.quote_json AS quote_json FROM entry_evaluations e "
                "LEFT JOIN market_observations o ON o.observation_id = e.observation_id")


def candidates(env: Env, run: str | None = None, bucket: str = "pass", ticker: str | None = None,
               before: str | None = None, after: str | None = None) -> dict[str, Any]:
    rj = env.rj
    out: dict[str, Any] = {"view": "candidates", "attention": attention(env), "filters": {"bucket": bucket, "ticker": ticker, "run": run}}
    if rj.schema_version() is None or env.policy is None:
        return out
    latest = rj.last_run("SCAN")
    last_eval = rj.conn.execute("SELECT run_id, evaluated_at_utc FROM entry_evaluations ORDER BY rowid DESC LIMIT 1").fetchone()
    sel_run = run or (last_eval["run_id"] if last_eval else None)
    run_row = None
    if sel_run:
        run_row = rj.conn.execute("SELECT * FROM run_status WHERE run_id=?", (sel_run,)).fetchone()
    summ = _j(run_row["summary_json"]) if run_row else None
    issues = _run_issues(rj, sel_run) if sel_run else []
    latest_summ = _j(latest["summary_json"]) if latest else None
    latest_issues = _run_issues(rj, latest["run_id"]) if latest else []
    cov_latest = _coverage(latest, latest_issues)
    counts = {"pass": 0, "data": 0, "rejected": 0}
    reasons: list[dict] = []
    ev_time = None
    if sel_run:
        for r in rj.conn.execute("SELECT CASE WHEN decision IN ('PASS_RESEARCH_ONLY','PASS_VERIFIED_DATA') THEN 'pass' "
                                 "WHEN decision='DATA_UNAVAILABLE' THEN 'data' ELSE 'rejected' END b, COUNT(*) n "
                                 "FROM entry_evaluations WHERE run_id=? GROUP BY b", (sel_run,)):
            counts[r["b"]] = r["n"]
        reasons = [{"reason": r["v"], "count": r["n"]} for r in rj.conn.execute(
            "SELECT j.value v, COUNT(*) n FROM entry_evaluations e, json_each(e.reason_codes_json) j WHERE e.run_id=? "
            "GROUP BY j.value ORDER BY n DESC, v LIMIT 20", (sel_run,))]
        t = rj.conn.execute("SELECT MAX(evaluated_at_utc) FROM entry_evaluations WHERE run_id=?", (sel_run,)).fetchone()[0]
        ev_time = from_iso(t) if t else None
    expiry = timedelta(minutes=env.policy.alerts.entry_alert_expiry_minutes)
    superseded = bool(latest and sel_run and latest["run_id"] != sel_run)
    stale = bool(sel_run and (superseded or ev_time is None or env.now - ev_time > expiry))
    out["states"] = pipeline_states(env, latest, sum(counts.values()), counts["pass"], cov_latest, latest_summ)
    out["latest_scan"] = _run_view(latest) if latest else None
    out["selected_run"] = {"run_id": sel_run, "record": _run_view(run_row) if run_row else None, "evaluated_at_utc": ev_time,
                           "stale": stale, "superseded_by_newer_scan": superseded,
                           "stale_reason": ("a newer scan exists" if superseded else
                                            f"older than the {env.policy.alerts.entry_alert_expiry_minutes}-minute entry-alert expiry") if stale else None}
    out["counts"] = {"passing": 0 if stale else counts["pass"], "stale": counts["pass"] if stale else 0,
                     "rejected": counts["rejected"], "data_unavailable": counts["data"]}
    out["reason_histogram"] = reasons
    cov = _coverage(run_row, issues) if sel_run else {}
    out["coverage"] = cov
    out["not_evaluated"] = issues
    if env.approved is not None and sel_run:
        seen = {r[0].split("|")[0] for r in rj.conn.execute(
            "SELECT DISTINCT contract_key FROM entry_evaluations WHERE run_id=?", (sel_run,))} | {i["ticker"] for i in issues}
        out["no_record_this_run"] = sorted(a.ticker for a in env.approved if a.ticker not in seen)
    where, params = "e.run_id = ?", [sel_run]
    if bucket == "pass":
        where += " AND e.decision IN ('PASS_RESEARCH_ONLY','PASS_VERIFIED_DATA')"
    elif bucket == "stale":
        where += " AND e.decision IN ('PASS_RESEARCH_ONLY','PASS_VERIFIED_DATA')" + ("" if stale else " AND 0")
    elif bucket == "rejected":
        where += " AND e.decision NOT IN ('PASS_RESEARCH_ONLY','PASS_VERIFIED_DATA','DATA_UNAVAILABLE')"
    elif bucket == "data":
        where += " AND e.decision = 'DATA_UNAVAILABLE'"
    if bucket == "pass" and stale:
        where += " AND 0"   # a stale/superseded passing decision is listed under "stale", never as a live alternative
    if ticker:
        where += " AND substr(e.contract_key, 1, instr(e.contract_key, '|') - 1) = ?"
        params.append(ticker)
    if sel_run:
        pg = page(rj, _CAND_SELECT, where, params, "e.rowid", before, after)
        out["rows"] = [_cand_row(r, env, stale) for r in pg["rows"]]
        out["newer"], out["older"] = pg["newer"], pg["older"]
    else:
        out["rows"], out["newer"], out["older"] = [], None, None
    return out


def candidate_detail(env: Env, evaluation_id: str) -> dict[str, Any] | None:
    rj = env.rj
    if rj.schema_version() is None:
        return None
    r = rj.conn.execute(_CAND_SELECT + " WHERE e.evaluation_id=?", (evaluation_id,)).fetchone()
    if r is None:
        return None
    latest = rj.last_run("SCAN")
    stale = bool(latest and latest["run_id"] != r["run_id"]) or (
        env.policy is not None and env.now - from_iso(r["evaluated_at_utc"]) > timedelta(minutes=env.policy.alerts.entry_alert_expiry_minutes))
    row = _cand_row(r, env, stale)
    ap = env.approval(row["ticker"])
    run_row = rj.conn.execute("SELECT * FROM run_status WHERE run_id=?", (r["run_id"],)).fetchone()
    return {"view": "candidate_detail", "attention": attention(env), "row": row,
            "decision": _j(r["decision_json"]), "observation": _j(r["quote_json"]),
            "approval": None if ap is None else {
                "ticker": ap.ticker, "asset_type": ap.asset_type, "sector": ap.sector, "correlation_bucket": ap.correlation_bucket,
                "max_buy_strike": ap.max_buy_strike, "approved_through": ap.approved_through,
                "events_reviewed_on": ap.events_reviewed_on, "events_cleared_through": ap.events_cleared_through,
                "standard_deliverable_confirmed_on": ap.standard_deliverable_confirmed_on, "ownership_reason": ap.ownership_reason,
                "note": "Current approval record (not necessarily the one in force at decision time)."},
            "run": _run_view(run_row) if run_row else None}


# ---------------------------------------------------------------- runs (WD-003 history)
def _run_view(r: sqlite3.Row | dict) -> dict[str, Any]:
    r = dict(r)
    s = r.get("summary") if "summary" in r else _j(r.get("summary_json")) or {}
    return {"run_id": r["run_id"], "kind": r["kind"], "started_at_utc": from_iso(r["started_at_utc"]), "status": r["status"],
            "reasons": _j(r["reasons_json"]) or [], "summary": s,
            "scope": s.get("scope"), "policy_sha256": s.get("policy_sha256"), "policy_version": s.get("policy_version"),
            "ledger_revision": s.get("ledger_revision"), "code_revision": s.get("code_revision"),
            "recorded_identity": "policy_sha256" in s}


def runs(env: Env, kind: str | None = None, before: str | None = None, after: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "runs", "attention": attention(env), "filters": {"kind": kind}}
    if env.rj.schema_version() is None:
        return out
    where, params = ("kind = ?", [kind]) if kind else ("", [])
    pg = page(env.rj, "SELECT rowid AS rid, * FROM run_status", where, params, "rowid", before, after)
    out["rows"] = [dict(_run_view(r), rid=r["rid"]) for r in pg["rows"]]
    out["newer"], out["older"] = pg["newer"], pg["older"]
    return out


def run_detail(env: Env, run_id: str) -> dict[str, Any] | None:
    r = env.rj.conn.execute("SELECT * FROM run_status WHERE run_id=?", (run_id,)).fetchone()
    if r is None:
        return None
    issues = _run_issues(env.rj, run_id)
    n = env.rj.conn.execute("SELECT COUNT(*) FROM entry_evaluations WHERE run_id=?", (run_id,)).fetchone()[0]
    return {"view": "run_detail", "attention": attention(env), "run": _run_view(r), "issues": issues,
            "coverage": _coverage(r, issues), "evaluations": n}


# ---------------------------------------------------------------- positions (WD-012)
def _latest_quote(rj: ReadOnlyJournal, p: PutPosition) -> tuple[OptionQuote | None, dict | None]:
    """Newest applicable stored observation of THIS exact contract (underlying, expiration, right P, strike): the market-data
    store (monitor / market-refresh) or the last entry-scan observation, whichever was RETRIEVED later (MD-AT-04). The
    contract identity is preserved and a stock price is never used as an option ask. The position's own multiplier is
    trusted (the Yahoo path carries none), as in the monitor."""
    cands: list[tuple[datetime, int, OptionQuote, dict]] = []
    row_ref = None
    try:
        row = _obs.option_for_contract(rj.conn, p.ticker, p.expiration, p.strike) if _obs.has_tables(rj.conn) else None
    except sqlite3.Error:
        row = None
    if row is not None:
        def dd(k):
            try:
                return dec(row[k]) if row.get(k) is not None else None
            except Exception:  # noqa: BLE001 - unreadable stored value is "unavailable", never zero
                return None
        rt = from_iso(row["retrieved_at_utc"])
        row_ref = row
        q = OptionQuote(contract_key=p.contract_key, retrieved_at_utc=rt, bid=dd("bid"), ask=dd("ask"), delta=dd("delta"), spot=dd("spot"))
        label = {"MONITOR": "monitor observation (market-data store)", "REFRESH": "market-refresh observation (market-data store)"}.get(
            row["source"], "market-data store")
        cands.append((rt, 1, q, {"retrieved_at_utc": rt, "bid": q.bid, "ask": q.ask, "delta": q.delta, "spot": q.spot, "source": label,
                                 "contract": row["occ_symbol"], "occ_source": row["occ_source"],
                                 "quote_time_utc": from_iso(row["quote_time_utc"]) if row["quote_time_utc"] else None}))
    r = rj.conn.execute("SELECT quote_json, retrieved_at_utc FROM market_observations WHERE contract_key=? "
                        "ORDER BY rowid DESC LIMIT 1", (p.contract_key,)).fetchone()
    if r is not None:
        o = _j(r["quote_json"]) or {}

        def d(k):
            try:
                return dec(o[k]) if o.get(k) is not None else None
            except Exception:  # noqa: BLE001
                return None
        rt = from_iso(r["retrieved_at_utc"])
        q = OptionQuote(contract_key=p.contract_key, retrieved_at_utc=rt, bid=d("bid"), ask=d("ask"), delta=d("delta"), spot=d("spot"))
        cands.append((rt, 0, q, {"retrieved_at_utc": rt, "bid": q.bid, "ask": q.ask, "delta": q.delta, "spot": q.spot,
                                 "source": "last stored entry-scan observation", "contract": None, "occ_source": None,
                                 "quote_time_utc": None}))
    if not cands:
        return None, None
    _, _, q, ev = max(cands, key=lambda c: (c[0], c[1]))
    # RV-20261031-02: a NEWER failed attempt for this exact contract makes the retained quote unusable for numeric advice. The
    # original timestamps and prices stay visible; evaluate_exits turns the identity_problem into DATA_ISSUE (calendar and
    # assignment signals are unaffected).
    reason = at = None
    try:
        if _obs.has_tables(rj.conn):
            from_store = ev.get("contract") is not None          # the market-data store row (not the entry-scan observation)
            fail = (_obs.latest_evidence(rj.conn, p.ticker).get("contracts") or {}).get(
                _obs.contract_key(p.expiration.isoformat(), _obs.canon_strike(p.strike)))
            if fail and q.retrieved_at_utc is not None and (
                    _obs.mark_active(fail, to_iso(q.retrieved_at_utc), row_ref["run_id"] if (row_ref is not None and from_store) else None)):
                reason, at = fail.get("reason") or "newest quote attempt was invalid", _obs.parse_at(fail["at"])
            # RV-20261031-03: a newer WHOLE-fetch failure (provider error, rate limit, empty chain) for the symbol also makes the
            # retained quote history. A newer valid write (refresh or monitor) moves last_success past the failed attempt.
            fm = _obs.latest_evidence(rj.conn, p.ticker).get("fetch")
            if reason is None and fm and q.retrieved_at_utc is not None and _obs.parse_at(fm["at"]) > (
                    _obs.row_precise_time(rj.conn, p.ticker, to_iso(q.retrieved_at_utc), row_ref["run_id"])
                    if (row_ref is not None and ev.get("contract") is not None) else q.retrieved_at_utc):
                reason, at = f"newest monitor fetch failed ({_fail.display(fm.get('reason')) if fm.get('reason') else 'no data'})", _obs.parse_at(fm["at"])
            st = _obs.get_status(rj.conn, p.ticker)
            if reason is None and st and st["last_attempt_at_utc"] and q.retrieved_at_utc is not None:
                att = from_iso(st["last_attempt_at_utc"])
                ok = from_iso(st["last_success_at_utc"]) if st["last_success_at_utc"] else None
                if (ok is None or att > ok) and att > q.retrieved_at_utc:
                    reason = ("newest refresh attempt failed (" + (_fail.display(st["last_error"]) if st["last_error"] else "no usable data")
                              + ")")
                    at = att
    except Exception:  # noqa: BLE001 - bookkeeping unreadable: do not invent currency; fall through to existing checks
        reason = None
    if reason is not None:
        q = dataclasses.replace(q, identity_problem=f"{reason} (retained quote from {q.retrieved_at_utc:%Y-%m-%d %H:%M}Z is not current)")
        ev = {**ev, "retained_unusable": True, "latest_failure_at_utc": at, "latest_failure_reason": reason}
    return q, ev


def position_view(env: Env, p: PutPosition, with_quote: bool = True) -> dict[str, Any]:
    pol = env.policy
    v = _pos_brief(p, env)
    v.update({"group": next((g for g in ("active", "reserved", "review", "history", "draft") if p.state in GROUPS[g]), "other"),
              "account_scope": p.account_scope, "paper": p.paper, "opened_at_utc": p.opened_at_utc,
              "open_credit_per_share": p.actual_open_credit_per_share, "close_debit_per_share": p.actual_close_debit_per_share,
              "closed_at_utc": p.closed_at_utc, "fees_usd": p.fees_usd, "notes": p.notes,
              "last_reconciled_at_utc": p.last_reconciled_at_utc, "sector": p.sector, "correlation_bucket": p.correlation_bucket,
              "warnings": [], "price_check": {"status": "not applicable"}, "pnl": None, "actions": position_actions(p),
              "breach_codes": list(dict(env.rj.policy_breaches() if env.rj.schema_version() else ()).get(p.trade_id, ()))})
    if v["breach_codes"] and p.state in _collateral_states():
        v["warnings"].append({"trigger": "POLICY_BREACH_RECORDED", "severity": "URGENT",
                              "detail": {"codes": v["breach_codes"], "meaning": "owner-reported trade breached policy; new entries "
                                                                               "blocked while it reserves collateral"}})
    if p.state is S.ASSIGNED_PENDING_RECONCILIATION:
        v["warnings"].append({"trigger": "ASSIGNMENT_PENDING_RECONCILIATION", "severity": "URGENT",
                              "detail": f"Obligation: {p.strike * p.multiplier * p.contracts} USD for {p.multiplier * p.contracts} shares, "
                                        "not yet reconciled."})
        v["advice_note"] = "Unresolved assignment: finish the manual reconciliation (RUNBOOK section 5)."
    elif p.state not in MONITORED_STATES:
        v["advice_note"] = ("Closed/terminal: no open-position advice." if p.state in GROUPS["history"]
                            else "Not an open position yet: exit monitoring does not apply.")
    if p.state in MONITORED_STATES and pol is not None:
        q, ev = _latest_quote(env.rj, p) if with_quote else (None, None)
        sigs = evaluate_exits(p, q, pol, env.now)
        for s in sigs:
            if s.trigger is not Trigger.DATA_ISSUE:
                v["warnings"].append({"trigger": s.trigger.value, "severity": s.severity, "detail": s.detail})
        problems = [x for s in sigs if s.trigger is Trigger.DATA_ISSUE for x in s.detail.get("problems", [])]
        if not pol.exits.tier_r_price_alerts_enabled:
            v["price_check"] = {"status": "unavailable", "reason": "price alerts are switched off in the policy", "evidence": ev}
        elif problems or q is None:
            v["price_check"] = {"status": "unavailable", "reason": "; ".join(problems) or "no stored quote", "evidence": ev}
        else:
            gross, net = estimated_pnl_usd(p, q.ask)
            v["price_check"] = {"status": "usable", "evidence": ev}
            v["pnl"] = {"gross_usd": gross, "net_of_fees_usd": net, "basis": "buy-to-close at the stored ask; INDICATIVE, verify at your broker"}
    return v


def positions(env: Env, group: str = "live", ticker: str | None = None, before: str | None = None,
              after: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "positions", "attention": attention(env), "filters": {"group": group, "ticker": ticker}}
    if env.rj.schema_version() is None or env.policy is None:
        return out
    states = [s.value for s in GROUPS[group]]
    where = f"state IN ({','.join('?' * len(states))})"
    params: list = list(states)
    if ticker:
        where += " AND ticker = ?"
        params.append(ticker)
    pg = page(env.rj, "SELECT rowid AS rid, trade_id FROM option_positions", where, params, "rowid", before, after)
    rows = []
    for r in pg["rows"]:
        v = position_view(env, env.rj.get_position(r["trade_id"]))
        v["rid"] = r["rid"]
        rows.append(v)
    out["rows"], out["newer"], out["older"] = rows, pg["newer"], pg["older"]
    return out


def position_detail(env: Env, trade_id: str, before: str | None = None, after: str | None = None) -> dict[str, Any] | None:
    rj = env.rj
    if rj.schema_version() is None or env.policy is None:
        return None
    try:
        p = rj.get_position(trade_id)
    except Exception:  # noqa: BLE001 - unknown id
        return None
    where = ("(json_extract(after_json,'$.trade_id') = ? OR json_extract(before_json,'$.trade_id') = ? "
             "OR json_extract(after_json,'$.linked_trade_id') = ? OR json_extract(after_json,'$.args.trade_id') = ?)")
    pg = page(rj, "SELECT rowid AS rid, * FROM audit_log", where, [trade_id] * 4, "rowid", before, after)
    alerts = [_alert_row(r) for r in rj.conn.execute(
        "SELECT rowid AS rid, * FROM alert_outbox WHERE json_extract(evidence_json,'$.revalidate.trade_id') = ? "
        "ORDER BY rowid DESC LIMIT 20", (trade_id,))]
    lots = [l for l in env.rj.list_stock_lots() if l.linked_trade_id == trade_id]
    pv = position_view(env, p)
    pv["recorded_info_codes"] = recorded_info_codes(rj, trade_id)
    pv["thresholds"] = thresholds(env)
    return {"view": "position_detail", "attention": attention(env), "position": pv,
            "lots": [{"lot_id": l.lot_id, "ticker": l.ticker, "shares": l.shares, "reference_price": l.reference_price,
                      "mark_asof_utc": l.reference_asof_utc, "cost_basis_reference_usd": l.cost_basis_reference_usd,
                      "acquisition_kind": l.acquisition_kind} for l in lots],
            "audit": [_audit_row(r) for r in pg["rows"]], "newer": pg["newer"], "older": pg["older"], "alerts": alerts}


def recorded_info_codes(rj: ReadOnlyJournal, trade_id: str) -> list[str]:
    """Contract-level codes (delta, DTE window, spread, ...) that were true at the stored quote when the owner recorded this
    trade. Information only: they never blocked recording. Read from the WEB_TRADE_RECORDED audit payload."""
    out: set[str] = set()
    for r in rj.conn.execute("SELECT after_json FROM audit_log WHERE event_type='WEB_TRADE_RECORDED' "
                             "AND json_extract(after_json,'$.trade_id') = ?", (trade_id,)):
        d = _j(r["after_json"]) or {}
        out.update(str(c) for c in (d.get("contract_info_codes") or []) if re.fullmatch(r"[A-Z_]{1,40}", str(c)))
    return sorted(out)


# ---------------------------------------------------------------- alerts and activity (WD-013)
# Stored outbox evidence is displayed through closed per-field patterns (an allowlist). A value that does not match is shown as
# "withheld", never parsed or cleaned, so a restored backup or hand-edited row cannot put free text on the page.
_HEX = re.compile(r"[0-9a-f]{8,64}\Z")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+\-]{1,20}Z?\Z")
_IDENT = re.compile(r"[A-Za-z0-9_\-]{1,60}\Z")
_TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")
_UPPER_SNAKE = re.compile(r"[A-Z][A-Z0-9_]{0,40}\Z")
# Closed sets of values the application writes into revalidate.trigger / revalidate.state (templates.py meta):
# exit Trigger names plus the assignment alert's own "ASSIGNED_PENDING"; and the position state enum.
_TRIGGER_NAMES = frozenset({t.value for t in Trigger} | {"ASSIGNED_PENDING"})
_STATE_NAMES = frozenset(s.value for s in S)
_MARKER = re.compile(r"[0-9T:.+\-Z]{0,40}\|[0-9A-Za-z\-]{0,40}\|[0-9]{1,12}\Z")
_FIELD_RX = {"policy_sha": _HEX, "policy_sha256": _HEX, "approval_sha": _HEX, "fingerprint": _HEX, "expires_at_utc": _ISO,
             "evidence_at_utc": _ISO, "trade_id": _IDENT, "scope": _IDENT, "ticker": _TICKER, "trigger": _UPPER_SNAKE,
             "state": _UPPER_SNAKE, "ledger_marker": _MARKER, "parse_mode": re.compile(r"(HTML|Markdown|MarkdownV2)\Z")}
FIELD_WITHHELD = "(older record, text withheld)"


def _safe_field(key: str, v: Any) -> Any:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    rx = _FIELD_RX.get(key)
    if rx is not None and isinstance(v, str) and len(v) <= 200 and rx.match(v):
        if key == "trigger" and v not in _TRIGGER_NAMES:
            return FIELD_WITHHELD
        if key == "state" and v not in _STATE_NAMES:
            return FIELD_WITHHELD
        return v
    return FIELD_WITHHELD


def safe_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("policy_sha", "parse_mode", "expires_at_utc", "part", "parts", "scope"):
        if k in ev:
            out[k] = _safe_field(k, ev[k])
    out["last_error"] = display_reason(ev.get("last_error"))
    rv = ev.get("revalidate")
    if isinstance(rv, dict):
        out["revalidate"] = {k: _safe_field(k, rv.get(k)) for k in ("trade_id", "ticker", "trigger", "approval_sha", "ledger_marker",
                                                                    "fingerprint", "state", "evidence_at_utc") if k in rv}
    omitted = sorted(set(ev) - set(out) - {"revalidate"})
    if omitted:
        out["other_keys_withheld"] = len(omitted)
    return out


def _alert_row(r: sqlite3.Row) -> dict[str, Any]:
    ev = _j(r["evidence_json"]) or {}
    rv = ev.get("revalidate") or {}
    return {"rid": r["rid"], "alert_id": r["alert_id"], "class": r["class"], "severity": r["severity"], "status": r["status"],
            "delivery_state": OUTBOX_LABEL.get(r["status"], r["status"]), "created_at_utc": from_iso(r["created_at_utc"]),
            "next_attempt_at_utc": from_iso(r["next_attempt_at_utc"]) if r["next_attempt_at_utc"] else None,
            "attempts": r["attempts"], "telegram_message_id": r["telegram_message_id"],
            "trade_id": _safe_field("trade_id", rv.get("trade_id")), "trigger": _safe_field("trigger", rv.get("trigger")),
            "ticker": _safe_field("ticker", rv.get("ticker")),
            "part": _safe_field("part", ev.get("part")), "parts": _safe_field("parts", ev.get("parts")),
            "expires_at_utc": _safe_field("expires_at_utc", ev.get("expires_at_utc")),
            "last_error": display_reason(ev.get("last_error")), "policy_sha256": _safe_field("policy_sha", ev.get("policy_sha")),
            "type_text": ALERT_TYPE.get(r["class"], r["class"]),
            "decision_ref": {k: _safe_field(k, rv.get(k)) for k in ("approval_sha", "ledger_marker", "fingerprint", "state") if rv.get(k) is not None},
            "dedup_key": r["dedup_key"]}


def alerts(env: Env, status: str | None = None, severity: str | None = None, klass: str | None = None,
           before: str | None = None, after: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "alerts", "attention": attention(env),
                           "filters": {"status": status, "severity": severity, "class": klass}}
    if env.rj.schema_version() is None:
        return out
    conds, params = [], []
    for col, val in (("status", status), ("severity", severity), ("class", klass)):
        if val:
            conds.append(f"{col} = ?")
            params.append(val)
    pg = page(env.rj, "SELECT rowid AS rid, * FROM alert_outbox", " AND ".join(conds), params, "rowid", before, after)
    out["rows"], out["newer"], out["older"] = [_alert_row(r) for r in pg["rows"]], pg["newer"], pg["older"]
    out["delivery"] = delivery_summary(env.rj)
    out["status_counts"] = out["delivery"]["counts"]
    out["total_rows"] = sum(out["status_counts"].values())
    out["thresholds"] = thresholds(env)
    return out


def alert_detail(env: Env, alert_id: str) -> dict[str, Any] | None:
    r = env.rj.conn.execute("SELECT rowid AS rid, * FROM alert_outbox WHERE alert_id=?", (alert_id,)).fetchone()
    if r is None:
        return None
    ev = _j(r["evidence_json"]) or {}
    return {"view": "alert_detail", "attention": attention(env), "alert": _alert_row(r), "content_text": r["content_text"],
            "dedup_key": r["dedup_key"], "evidence": safe_evidence(ev)}


def _summarize(js: str | None) -> str:
    d = _j(js)
    if not isinstance(d, dict):
        return ""
    return "; ".join(f"{k}={str(v)[:60]}" for k, v in list(d.items())[:6])


def _audit_row(r: sqlite3.Row) -> dict[str, Any]:
    return {"rid": r["rid"], "event_id": r["event_id"], "created_at_utc": from_iso(r["created_at_utc"]),
            "event_type": r["event_type"], "actor": r["actor"], "source": r["source"], "summary": _summarize(r["after_json"])}


def activity(env: Env, event_type: str | None = None, before: str | None = None, after: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "activity", "attention": attention(env), "filters": {"event_type": event_type}}
    if env.rj.schema_version() is None:
        return out
    where, params = ("event_type = ?", [event_type]) if event_type else ("", [])
    pg = page(env.rj, "SELECT rowid AS rid, * FROM audit_log", where, params, "rowid", before, after)
    out["rows"], out["newer"], out["older"] = [_audit_row(r) for r in pg["rows"]], pg["newer"], pg["older"]
    out["event_types"] = [r[0] for r in env.rj.conn.execute("SELECT DISTINCT event_type FROM audit_log ORDER BY 1")]
    return out


def activity_detail(env: Env, event_id: str) -> dict[str, Any] | None:
    r = env.rj.conn.execute("SELECT rowid AS rid, * FROM audit_log WHERE event_id=?", (event_id,)).fetchone()
    if r is None:
        return None
    return {"view": "activity_detail", "attention": attention(env), "event": _audit_row(r),
            "before": _j(r["before_json"]), "after": _j(r["after_json"])}


# ---------------------------------------------------------------- health and reconciliation (WD-014)
def health(env: Env) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "health", "attention": attention(env)}
    pol, rj = env.policy, env.rj
    if pol is None or rj.schema_version() is None:
        return out
    st = env.status() or {}

    def run_v(r):
        return None if r is None else _run_view(r)

    def raw(kind, ok=None):
        rows = rj.conn.execute("SELECT * FROM run_status WHERE kind=? ORDER BY started_at_utc DESC, rowid DESC", (kind,)).fetchall()
        for r in rows:
            if ok is None or r["status"] in ok:
                return r
        return None
    from csp_alerts.jobs.status import MONITOR_OK, SCAN_OK
    out["runs"] = {"last_scan_attempt": run_v(raw("SCAN")), "last_scan_completed": run_v(raw("SCAN", SCAN_OK)),
                   "last_monitor_attempt": run_v(raw("MONITOR")), "last_monitor_completed": run_v(raw("MONITOR", MONITOR_OK))}
    ls = raw("SCAN")
    out["coverage_last_scan"] = _coverage(ls, _run_issues(rj, ls["run_id"]) if ls else []) if ls else None
    mon = out["runs"]["last_monitor_attempt"]
    out["monitor_fetch_issues"] = (mon or {}).get("summary", {}).get("fetch_issues", None)
    out["flags"] = st.get("flags", [])
    out["blockers"] = st.get("blockers", [])
    out["delivery"] = st.get("delivery")
    out["telegram_chat_pinned"] = st.get("pinned")
    out["next_runs"] = {"scan": (st.get("next") or {}).get("scan"), "monitor": (st.get("next") or {}).get("monitor"),
                        "basis": "Estimated from the policy schedule only. This does NOT show that any Windows scheduled "
                                 "task is installed, enabled or that the PC will be awake."}
    book = rj.load_book()
    rep = reconcile(book, pol, env.now)
    codes = {i.code for i in rep.issues}
    snap = book.latest_snapshot
    today = exchange_date(env.now)
    maxage = pol.entries.manual_event_review_max_age_calendar_days
    out["manual_records"] = [
        {"item": "Starting-equity baseline confirmed", "ok": "NO_BASELINE" not in codes},
        {"item": "Latest snapshot present, fresh and confirmed",
         "ok": snap is not None and not codes & {"NO_SNAPSHOT", "SNAPSHOT_STALE", "SNAPSHOT_UNCONFIRMED", "SNAPSHOT_FUTURE"},
         "detail": None if snap is None else snap.asof_utc},
        {"item": "Daily/end-of-day check-in done", "ok": "DAILY_CHECKIN_MISSING" not in codes and snap is not None},
        {"item": "No trade change since the last snapshot", "ok": "CHANGE_AFTER_SNAPSHOT" not in codes},
        {"item": "Stock marks fresh", "ok": "STOCK_MARK_STALE" not in codes and "STOCK_MARK_FUTURE" not in codes},
    ]
    out["approval_reviews"] = [{"ticker": a.ticker, "events_reviewed_on": a.events_reviewed_on,
                                "ok": 0 <= (today - a.events_reviewed_on).days <= maxage,
                                "max_age_days": maxage} for a in (env.approved or [])]
    out["unverified"] = [
        "Windows scheduled tasks: not checked by this dashboard (it never queries Task Scheduler). Verify with RUNBOOK section 9.",
        "Actual Telegram receipt in your private chat: not verifiable here; an acknowledgment from the API is not proof you read or acted.",
        "Clean-install and live-data integration checks: separate owner-operated acceptance items.",
    ]
    out["limits"] = [
        "Alerts only run while the PC is powered on and you are logged in; a stopped or sleeping PC cannot announce its own outage.",
        "Silence is never 'all clear': look for the daily heartbeat in Telegram and the run times above.",
    ]
    return out


# ---------------------------------------------------------------- Stage A: watchlist and market data (WT-01, WT-02)

MSG_CODES = frozenset({"added", "already", "removed", "not_tracked", "invalid", "full", "queued", "pending", "rate",
                       "unknown_symbol", "unavailable", "bad_request", "csrf"})
BROWSE_ROW_CAP = 400
RESEARCH_ONLY_NOTE = ("Gate marker = every CONTRACT-level entry gate passes on the stored data (research only). Not evaluated here: "
                      + NOT_EVALUATED + ". It is not an entry alert and not a recommendation. Evaluated as a standard 100-share "
                      "put (Yahoo gives no deliverable data).")


def _stage_a_ready(env: Env) -> bool:
    return env.rj.schema_version() is not None and _obs.has_tables(env.rj.conn) and _wl.has_table(env.rj)


def _session_info(env: Env) -> SessionInfo:
    return mcal.session_detail(env.now).info


def _approval_state(env: Env, sym: str) -> dict[str, Any]:
    if env.approved is None:
        return {"state": "UNKNOWN", "text": "approval file unreadable: treated as NOT approved for entry alerts"}
    a = env.approval(sym)
    if a is None:
        return {"state": "NOT_APPROVED", "text": "not approved for entry alerts"}
    if a.approved_through < exchange_date(env.now):
        return {"state": "EXPIRED", "text": f"approval expired {a.approved_through.isoformat()}: no new entry alerts (an existing holding stays monitored)"}
    return {"state": "APPROVED", "text": "owner-approved whitelist entry (entry alerts still need every gate and a current reconciliation)"}


def _stock_failure(c, sym: str, stock_retrieved: datetime | None) -> dict[str, Any] | None:
    """Stock-specific latest-attempt failure (RV-20261031-01): active while it is newer than the retained stock observation."""
    f = _obs.latest_evidence(c, sym).get("stock")
    srow = c.execute("SELECT run_id FROM md_stock_latest WHERE symbol=?", (sym,)).fetchone()
    try:
        at = _obs.parse_at(f["at"]) if f else None
    except Exception:  # noqa: BLE001
        at = None
    if at is None or (stock_retrieved is not None and not _obs.mark_active(
            f, to_iso(stock_retrieved), srow["run_id"] if srow else None)):
        return None
    return {"at": at, "reason": str(f.get("reason") or "no valid underlying price")[:200]}


def _contract_failure(c, sym: str, r: dict) -> str | None:
    """Reason text when the newest attempt for this exact contract was invalid and this row is the older retained quote."""
    f = (_obs.latest_evidence(c, sym).get("contracts") or {}).get(_obs.contract_key(r["expiration"], r["strike"]))
    if not _obs.mark_active(f, r["retrieved_at_utc"], r.get("run_id")):
        return None
    return str(f.get("reason") or "newest quote attempt for this contract was invalid") + " (retained older quote, not current)"


def _symbol_block(env: Env, sym: str, reason: str, sess: SessionInfo) -> dict[str, Any]:
    c = env.rj.conn
    stock, st = _obs.latest_stock(c, sym), _obs.get_status(c, sym)
    retrieved = from_iso(stock["retrieved_at_utc"]) if stock else None
    mtime = from_iso(stock["market_time_utc"]) if stock and stock["market_time_utc"] else None
    last_att = from_iso(st["last_attempt_at_utc"]) if st and st["last_attempt_at_utc"] else None
    last_ok = from_iso(st["last_success_at_utc"]) if st and st["last_success_at_utc"] else None
    nxt = from_iso(st["next_attempt_after_utc"]) if st and st["next_attempt_after_utc"] else None
    failed = bool(last_att and (last_ok is None or last_att > last_ok))
    sfail = _stock_failure(c, sym, retrieved)
    failed = failed or sfail is not None or "fetch" in _obs.latest_evidence(c, sym)
    state = _fr.describe_state(now=env.now, session=sess.status, retrieved_at=retrieved, market_time=mtime,
                               status=st["status"] if st else None, attempt_failed=failed)
    n_opt = c.execute("SELECT COUNT(*) FROM md_option_latest WHERE symbol=?", (sym,)).fetchone()[0]
    note = state.note
    if sfail is not None:
        note = (f"latest stock evidence rejected at {sfail['at']:%Y-%m-%d %H:%M}Z ({sfail['reason']}); the stock price shown is the "
                "prior value with its original retrieval time and candidates are not evaluated; " + note)
    return {
        "symbol": sym, "stock_failure": sfail, "reason_tracked": reason, "approval": _approval_state(env, sym),
        "stock": None if not stock else {
            "price": stock["price"], "price_type": stock["price_type"], "price_type_note": _obs.PRICE_TYPE_NOTE,
            "provider": stock["provider"], "tier": stock["tier"], "source": stock["source"],
            "market_time_utc": mtime, "retrieved_at_utc": retrieved, "age_seconds": state.age_seconds},
        "state": {"labels": list(state.labels), "primary": state.primary, "note": note},
        "status": {"status": st["status"] if st else "NEVER_FETCHED", "last_attempt_utc": last_att, "last_success_utc": last_ok,
                   "last_error": _fail.display(st["last_error"]) if st and st["last_error"] else None, "consecutive_failures": st["consecutive_failures"] if st else 0,
                   "backoff_until_utc": nxt if nxt and nxt > env.now else None,
                   "session_at_attempt": st["session_at_attempt"] if st else None,
                   "next_update_estimate_utc": _fr.next_update_estimate(now=env.now, last_attempt=last_att, next_attempt_after=nxt,
                                                                       session=sess.status),
                   "next_update_basis": ("policy-derived estimate from the cadence (5 min while the market is open, 60 min otherwise, "
                                         "plus any backoff); it does not show that a Windows task exists")},
        "contracts_stored": n_opt,
    }


def watchlist(env: Env, msg: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "watchlist", "attention": attention(env), "feed_label": _fr.feed_label([]),
                           "message": msg if msg in MSG_CODES else None}
    if not _stage_a_ready(env):
        out["unavailable"] = ("The journal has not been upgraded for the watchlist and market data (schema 0005). "
                              "Run `csp journal init` in a terminal; it takes a verified backup first. The dashboard never migrates.")
        return out
    sess = _session_info(env)
    c = env.rj.conn
    rows = [_symbol_block(env, t.symbol, t.reason, sess) for t in _wl.tracked(env.rj)]
    out["feed_label"] = _fr.feed_label([b["state"]["labels"] for b in rows], sess.status)
    out.update({
        "rows": rows, "session": sess.status.value,
        "limits": {"max_watchlist": _fr.MAX_WATCHLIST, "on_watchlist": len(_wl.watchlist_symbols(env.rj))},
        "cadence": {"open_seconds": _fr.OPEN_INTERVAL_SECONDS, "closed_seconds": _fr.CLOSED_INTERVAL_SECONDS,
                    "stale_after_open_seconds": _fr.STALE_AFTER_OPEN_SECONDS},
        "requests": _obs.recent_requests(c, 5), "refresh_runs": _obs.last_runs(c, 3),
    })
    return out


def market_detail(env: Env, symbol: str, expiry: str | None = None, strike_min: str | None = None,
                  strike_max: str | None = None, show: str | None = None, msg: str | None = None) -> dict[str, Any] | None:
    if not _stage_a_ready(env):
        return {"view": "market", "attention": attention(env), "symbol": symbol,
                "feed_label": _fr.feed_label([]),
                "unavailable": "The journal has not been upgraded for market data (run `csp journal init`; a backup is taken first)."}
    trk = {t.symbol: t for t in _wl.tracked(env.rj)}
    stored = _obs.latest_stock(env.rj.conn, symbol) or _obs.latest_options(env.rj.conn, symbol)
    if symbol not in trk and not stored:
        return None
    sess = _session_info(env)
    block = _symbol_block(env, symbol, trk[symbol].reason if symbol in trk else "no longer tracked (stored data only)", sess)
    today = exchange_date(env.now)
    allrows = [r for r in _obs.latest_options(env.rj.conn, symbol) if date.fromisoformat(r["expiration"]) >= today]
    expiries = []
    for e in sorted({r["expiration"] for r in allrows}):
        expiries.append({"expiration": e, "dte": (date.fromisoformat(e) - today).days,
                         "contracts": sum(1 for r in allrows if r["expiration"] == e)})
    smin = dec(strike_min) if strike_min else None
    smax = dec(strike_max) if strike_max else None
    stale = block["state"]["primary"] in ("STALE", "UNAVAILABLE")
    ectx = entry_context(env, symbol, block["approval"])
    rows, n_pass, n_all = [], 0, 0
    for r in allrows:
        if expiry and r["expiration"] != expiry:
            continue
        k = dec(r["strike"])
        if (smin is not None and k < smin) or (smax is not None and k > smax):
            continue
        hold = _contract_failure(env.rj.conn, symbol, r)
        if hold is None and block["stock_failure"] is not None:
            hold = "latest stock evidence was rejected (conflicting or missing underlying price); candidates are not evaluated"
        row_stale = stale or hold is not None or block["stock_failure"] is not None
        gate = _gate_for(env, symbol, r, sess, row_stale, hold)
        if gate["marker"] == "PASS":
            n_pass += 1
        if show == "pass" and gate["marker"] != "PASS":
            continue
        row = _option_row(env, r, gate, sess)
        if hold and _contract_failure(env.rj.conn, symbol, r):
            row["retained_unusable"] = hold
        if ectx is not None:
            row["review"] = entry_review(env, ectx, symbol, r, sess, row_stale)
            n_all += 1 if row["review"]["all_pass"] else 0
        rows.append(row)
    total = len(rows)
    return {"view": "market", "attention": attention(env), "symbol": symbol, "block": block, "expiries": expiries,
            "filters": {"expiry": expiry, "strike_min": strike_min, "strike_max": strike_max, "show": show or "all"},
            "rows": rows[:BROWSE_ROW_CAP], "rows_matching": total, "rows_shown_cap": BROWSE_ROW_CAP,
            "passing_in_view": n_pass, "gate_note": RESEARCH_ONLY_NOTE, "all_gates_in_view": n_all,
            "entry_context": None if ectx is None else {
                "approval_state": ectx.approval_state, "asset_type": asset_type_label(ectx.asset_type), "sector": ectx.sector, "bucket": ectx.bucket,
                "labels_unknown": ectx.labels_unknown, "label_source": ectx.label_source, "free_cash_usd": ectx.cash,
                "label_path": f"/portfolio/labels/{symbol}"},
            "feed_label": _fr.feed_label([block["state"]["labels"]], sess.status),
            "message": msg if msg in MSG_CODES else None,
            "no_pass_note": ("No stored contract passes the contract-level gates right now; prices stay visible. Entry capacity, "
                             "approval and event checks are separate and are shown in the blockers above."),
            "trade_recording_note": ("'Record sold put' only records a trade YOU already executed at your broker; it places nothing and never "
                                     "uses a quoted price as your fill. You enter the actual credit and fees.")}


def _gate_for(env: Env, symbol: str, r: dict, sess: SessionInfo, stale: bool, hold: str | None = None) -> dict[str, Any]:
    if env.policy is None:
        return {"marker": "NOT_EVALUATED", "codes": [], "text": "policy unreadable"}
    if stale:
        return {"marker": "NOT_EVALUATED", "codes": [],
                "text": (hold or "stored data is stale or missing") + ": gates not evaluated"}
    try:
        def d(k):
            return dec(r[k]) if r.get(k) is not None else None
        mo = MarketObservation(
            underlying=symbol, expiration=date.fromisoformat(r["expiration"]), strike=dec(r["strike"]), multiplier=100,
            option_right="P", spot=d("spot"), bid=d("bid"), ask=d("ask"), delta=d("delta"), open_interest=r["open_interest"],
            daily_volume=r["volume"], last_option_trade_at_utc=from_iso(r["last_trade_at_utc"]) if r["last_trade_at_utc"] else None,
            retrieved_at_utc=from_iso(r["retrieved_at_utc"]))
        codes = contract_gate_codes(mo, env.policy, env.now, sess)
    except Exception:  # noqa: BLE001 - an unreadable stored value is "not evaluated", never a pass
        return {"marker": "NOT_EVALUATED", "codes": [], "text": "stored value unreadable"}
    return {"marker": "PASS" if not codes else "BROWSE", "codes": list(codes),
            "text": "passes contract gates (research only)" if not codes else "browsable only: " + ", ".join(codes)}


def _option_row(env: Env, r: dict, gate: dict, sess: SessionInfo) -> dict[str, Any]:
    exp = date.fromisoformat(r["expiration"])
    retrieved = from_iso(r["retrieved_at_utc"])
    mt = from_iso(r["quote_time_utc"]) if r["quote_time_utc"] else None
    st = _fr.describe_state(now=env.now, session=sess.status, retrieved_at=retrieved, market_time=mt, status=None)
    return {
        "occ_symbol": r["occ_symbol"], "occ_source": r["occ_source"], "identity_note": r["identity_note"], "right": r["option_right"],
        "expiration": exp, "dte": (exp - exchange_date(env.now)).days, "strike": r["strike"], "bid": r["bid"], "ask": r["ask"],
        "last": r["last"], "volume": r["volume"], "open_interest": r["open_interest"], "delta": r["delta"],
        "last_trade_at_utc": from_iso(r["last_trade_at_utc"]) if r["last_trade_at_utc"] else None,
        "quote_time_utc": mt, "retrieved_at_utc": retrieved, "age_seconds": st.age_seconds, "labels": list(st.labels),
        "provider": r["provider"], "tier": r["tier"], "source": r["source"], "gate": gate,
    }


# ---------------------------------------------------------------- Stage B1: recording forms (read side only)
# The browser computes nothing financial: every figure on these pages comes from csp_alerts.journal.webtrade (which reuses
# the backend rules). These functions only read.
ACT_SLUGS = {"mark-filled": wt.MARK_FILLED, "close-order": wt.CLOSE_ORDER, "close-fill": wt.CLOSE_FILLED, "cancel": wt.CANCEL_OPEN,
             "cancel-close": wt.CANCEL_CLOSE, "expire": wt.EXPIRE}
SLUG_OF = {v: k for k, v in ACT_SLUGS.items()}
RESULT_CODES = frozenset(wt.MESSAGES) | {"csrf", "bad_request"}
ACTION_LABEL = {wt.MARK_FILLED: "Mark filled", wt.CLOSE_ORDER: "I placed a closing order (not filled)",
                wt.CLOSE_FILLED: "Record the confirmed closing fill", wt.CANCEL_OPEN: "Order was canceled (unfilled)",
                wt.CANCEL_CLOSE: "Closing order was canceled", wt.EXPIRE: "Record expiry"}
STATE_ACTIONS: dict[S, tuple[str, ...]] = {
    S.WORKING: (wt.MARK_FILLED, wt.CANCEL_OPEN), S.PENDING: (wt.MARK_FILLED, wt.CANCEL_OPEN),
    S.OPEN: (wt.CLOSE_ORDER, wt.CLOSE_FILLED, wt.EXPIRE), S.CLOSE_WORKING: (wt.CLOSE_FILLED, wt.CANCEL_CLOSE, wt.EXPIRE),
}
REASON_TEXT = {
    "NORMAL_EXPOSURE_LIMIT": "Aggregate or put exposure above the normal ceiling.", "PILOT_COLLATERAL_LIMIT": "Put collateral above the pilot cap.",
    "SINGLE_TICKER_LIMIT": "Combined exposure to this ticker above its limit.", "SECTOR_LIMIT": "Sector exposure above its limit.",
    "CORRELATED_LIMIT": "Correlation-bucket exposure above its limit.", "EXPIRY_WEEK_LIMIT": "Collateral expiring in the same week above its limit.",
    "CONTRACT_COUNT_LIMIT": "Active contract count (or one-per-underlying) above its limit.",
    "INDIVIDUAL_NAME_LIMIT": "Too many individual-company names.", "INSUFFICIENT_FREE_CASH": "Latest NET free cash is below the collateral needed.",
    "NOT_APPROVED": "Ticker is not on your owner-approved list.", "APPROVAL_EXPIRED": "Your approval for this ticker has expired.",
    "VALUATION_TOO_HIGH": "Strike is above your approved maximum buy strike.", "EVENT_UNVERIFIED": "Event review is old or does not cover the expiration.",
    "EARNINGS_IN_WINDOW": "The provider shows earnings inside the window.",
    "LABELS_UNKNOWN": "Sector or correlation bucket is unknown, so concentration cannot be assessed.",
}


def _collateral_states():
    from csp_alerts.journal.models import COLLATERAL_STATES
    return COLLATERAL_STATES


def position_actions(p: PutPosition) -> list[dict[str, str]]:
    if p.contracts != 1 or p.multiplier != 100:
        return []
    return [{"label": ACTION_LABEL[a], "path": f"/trade/{SLUG_OF[a]}/{p.trade_id}"} for a in STATE_ACTIONS.get(p.state, ())]


def _trade_ready(env: Env) -> str | None:
    if env.policy is None:
        return "The policy file cannot be loaded, so nothing can be recorded here."
    if not _stage_a_ready(env) or env.rj.schema_version() != LATEST_SCHEMA:
        return "The journal has not been upgraded for web recording (run `csp journal init`; a backup is taken first)."
    return None


def _today_hint(env: Env) -> str:
    from zoneinfo import ZoneInfo
    return env.now.astimezone(ZoneInfo(env.tz)).strftime("%Y-%m-%d")


def trade_sell_form(env: Env, symbol: str, expiry: str | None, strike: str | None, kind: str | None,
                    values: dict | None = None, errors: list[str] | None = None) -> dict[str, Any]:
    action = wt.SELL_WORKING if kind == "working" else wt.SELL_FILLED
    out: dict[str, Any] = {"view": "trade_form", "attention": attention(env), "action": action, "title": wt.TITLES[action],
                           "values": values or {}, "errors": errors or [], "today": _today_hint(env), "kind": "new",
                           "order_words": wt.ORDER_WORDS.get(action)}
    bad = _trade_ready(env)
    if bad:
        out["unavailable"] = bad
        return out
    ctx = None
    try:
        if expiry and strike and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expiry) and re.fullmatch(r"[0-9]{1,7}(\.[0-9]{1,4})?", strike):
            ctx = wt.contract_context(env.rj.conn, symbol, date.fromisoformat(expiry), dec(strike))
    except (ValueError, sqlite3.Error):
        ctx = None
    if ctx is None:
        out["unavailable"] = ("That exact contract is not in the stored chain. Open it from a row on the market page "
                              "(after a refresh); the contract is never typed by hand.")
        return out
    ap = env.approval(symbol)
    ctx = {k: v for k, v in ctx.items() if k != "row"}
    ctx.update({"dte": (ctx["expiration"] - exchange_date(env.now)).days, "scope": env.policy.account_scope,
                "approved": ap is not None, "sector": ap.sector if ap else None, "bucket": ap.correlation_bucket if ap else None})
    out.update({"context": ctx, "symbol": symbol, "expiry": expiry, "strike": strike,
                "kind_arg": "working" if kind == "working" else "filled"})
    return out


def trade_position_form(env: Env, act: str, trade_id: str, values: dict | None = None, errors: list[str] | None = None) -> dict | None:
    action = ACT_SLUGS.get(act)
    if action is None:
        return None
    out: dict[str, Any] = {"view": "trade_form", "attention": attention(env), "action": action, "title": wt.TITLES[action],
                           "values": values or {}, "errors": errors or [], "today": _today_hint(env), "kind": "position",
                           "trade_id": trade_id, "slug": act, "order_words": wt.ORDER_WORDS.get(action)}
    bad = _trade_ready(env)
    if bad:
        out["unavailable"] = bad
        return out
    try:
        p = env.rj.get_position(trade_id)
    except Exception:  # noqa: BLE001 - unknown id
        return None
    out["position"] = position_view(env, p, with_quote=False)
    if p.contracts != 1 or p.multiplier != 100 or p.state in (S.ASSIGNED_PENDING_RECONCILIATION, S.ADJUSTMENT_REVIEW):
        out["unavailable"] = wt.UNSUPPORTED_STATE
    elif action not in STATE_ACTIONS.get(p.state, ()):
        out["unavailable"] = f"This action does not apply to a position in state {p.state.value}."
    return out


def checkin_form(env: Env, values: dict | None = None, errors: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "checkin", "attention": attention(env), "values": values or {}, "errors": errors or []}
    bad = _trade_ready(env)
    if bad:
        out["unavailable"] = bad
        return out
    book = env.rj.load_book()
    coll = _collateral_states()
    live = [p for p in book.positions if p.state in coll or p.state is S.ASSIGNED_PENDING_RECONCILIATION]
    committed = sum((p.collateral_usd for p in book.positions if p.state in coll), Decimal("0"))
    snap = book.latest_snapshot
    skip = ("SNAPSHOT_STALE", "DAILY_CHECKIN_MISSING", "SNAPSHOT_FUTURE", "CHANGE_AFTER_SNAPSHOT", "NO_SNAPSHOT", "SNAPSHOT_UNCONFIRMED")
    issues = [{"code": i.code, "severity": i.severity, "message": i.message, "ref": i.ref}
              for i in reconcile(book, env.policy, env.now).issues if i.code not in skip]
    out.update({
        "positions": [{**_pos_brief(p, env), "credit": p.actual_open_credit_per_share} for p in live],
        "lots": [{"lot_id": x.lot_id, "ticker": x.ticker, "shares": x.shares, "price": x.reference_price, "asof": x.reference_asof_utc}
                 for x in book.stock_lots][:wt.MAX_LOTS],
        "lots_over_limit": len(book.stock_lots) > wt.MAX_LOTS, "max_lots": wt.MAX_LOTS, "expected_collateral": committed,
        "snapshot": _snap_view(snap, env.now) if snap else None, "issues": issues, "today": _today_hint(env),
    })
    return out


def trade_preview(env: Env, plan: "wt.Plan", back: str) -> dict[str, Any]:
    return {"view": "trade_preview", "attention": attention(env), "plan": plan, "back": back, "reasons_text": REASON_TEXT,
            "order_words": wt.ORDER_WORDS.get(plan.action)}


def trade_result(env: Env, code: str, trade_id: str | None, action: str | None) -> dict[str, Any]:
    code = code if code in RESULT_CODES else "bad_request"
    csrf_text = "The form expired or was not issued by this page. Nothing was written; reload the page and try again."
    out: dict[str, Any] = {"view": "trade_result", "attention": attention(env), "code": code,
                           "message": wt.MESSAGES.get(code) or (csrf_text if code == "csrf" else "Bad request."),
                           "action": action if action in wt.ACTIONS else None, "ok": code in ("recorded", "duplicate")}
    if trade_id and code in ("recorded", "duplicate"):
        try:
            p = env.rj.get_position(trade_id)
        except Exception:  # noqa: BLE001
            p = None
        if p is not None:
            out["position"] = position_view(env, p, with_quote=False)
            out["breach_codes"] = list(dict(env.rj.policy_breaches()).get(trade_id, ()))
    out["f3"] = wt.F3_PROMPT if (out["ok"] and out["action"] not in (None, wt.CHECKIN)) else None
    return out


# ---------------------------------------------------------------- Stage B2: portfolio, label correction, candidate capacity, alerts (WT-04/05)
# The browser computes nothing financial. Every figure below comes from csp_alerts.policy.portfolio (compute_exposure,
# exposure_groups, portfolio_limits, capacity_rows) or evaluate_entry; this layer only selects, orders and words them.
ALERT_TYPE = {
    "ENTRY_RESEARCH": "Entry research (a candidate; not a trade instruction)", "PROFIT_EXIT": "Position: profit-capture review",
    "LOSS_REVIEW": "Position: loss review", "DELTA_REVIEW": "Position: delta review", "TIME_EXIT": "Position: time / expiry review",
    "ASSIGNMENT_REVIEW": "Position: assignment review", "DATA_ISSUE": "Data problem", "NO_TRADE_DIGEST": "No-trade digest",
    "ACCOUNT_RISK": "Account risk (drawdown)", "RECONCILIATION_REQUIRED": "Reconciliation required", "SERVICE_HEALTH": "Service health",
}
LABEL_RESULT_CODES = wl.RESULT_CODES


def thresholds(env: Env) -> dict[str, Any] | None:
    """Active exit thresholds and policy identity for display. PROVISIONAL policy values: the owner decides later (WT-04)."""
    pol = env.policy
    if pol is None:
        return None
    x = pol.exits
    return {"policy_id": pol.policy_id, "policy_version": pol.policy_version, "policy_sha256": pol.sha256,
            "provisional_note": ("These exit thresholds are provisional policy values, not an approved optimization. The owner decides the "
                                 "final profit, loss and delta thresholds later; nothing here retunes or disables them."),
            "rows": [("Profit review when this fraction of the credit is captured", x.profit_captured_fraction),
                     ("Close review at or below this many calendar days to expiry", x.close_review_at_dte_calendar),
                     ("Loss review when the buy-to-close ask reaches this multiple of the credit", x.loss_buyback_ask_multiple),
                     ("Delta review when |delta| reaches", x.delta_review_abs),
                     ("Assignment warning at or below this many calendar days to expiry", x.assignment_warning_dte),
                     ("Price-based review alerts on unstamped (Tier R) quotes", "on" if x.tier_r_price_alerts_enabled else "off")]}


def _usd(x: Decimal) -> str:
    return usd2(x)


def _entries(limits, groups) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in limits:
        out.append({"scope": "portfolio", "label": r.label, "unit": r.unit, "limit": r.limit, "used": r.used, "room": r.room,
                    "breached": r.breached, "exhausted": r.exhausted, "reason": r.reason.value, "binding": False})
    for g in groups:
        out.append({"scope": g.dimension, "label": f"{g.dimension.replace('_', ' ')} {g.key.upper() if g.dimension == 'ticker' else g.key}", "unit": "USD", "limit": g.limit,
                    "used": g.exposure_usd, "room": g.room_usd, "breached": g.breached, "exhausted": g.exhausted,
                    "reason": g.reason.value, "binding": False})
    for unit in ("USD", "contracts", "names"):
        same = [e for e in out if e["unit"] == unit]
        if same:
            min(same, key=lambda e: e["room"])["binding"] = True       # tightest limit in each unit
    return out


def mitigation_lines(entries: list[dict[str, Any]], groups) -> list[str]:
    """Rule-based explanation only: which independent limit binds and what capacity remains by bucket. It never suggests a
    trade, a hedge, an alternative sector or any movement of capital, and no model is involved."""
    hot = [e for e in entries if e["breached"] or e["exhausted"]]
    if not hot:
        return []

    def money(e, x):
        if e["unit"] == "USD":
            return _usd(x)
        unit = e["unit"][:-1] if x == 1 and e["unit"].endswith("s") else e["unit"]       # 1 contract, 2 contracts
        return f"{x:f} {unit}"
    lines = []
    for e in sorted(hot, key=lambda e: e["room"]):
        verdict = (f"over its limit by {money(e, -e['room'])}" if e["breached"] else "at its limit with no room left")
        lines.append(f"{e['label']}: used {money(e, e['used'])} of {money(e, e['limit'])}, {verdict}. Any new put that adds to it "
                     f"fails the {e['reason']} gate until this exposure falls (position closed, expired or canceled).")
    tightest = min(hot, key=lambda e: e["room"])
    lines.append(f"The independent limit that binds hardest is: {tightest['label']} (rule {tightest['reason']}). Limits apply together; "
                 "clearing one does not clear another.")
    for dim, word in (("bucket", "correlation bucket"), ("sector", "sector"), ("expiry_week", "expiration week")):
        rows = [g for g in groups if g.dimension == dim]
        if rows:
            lines.append(f"Remaining capacity by {word}: " + "; ".join(
                f"{g.key} {_usd(g.room_usd) if g.room_usd > 0 else 'none (' + _usd(g.room_usd) + ')'}" for g in rows)
                + ". A bucket or sector with no holding yet has the full limit available to it, subject to every other limit.")
    lines.append("This page recommends no trade, hedge, alternative sector or movement of capital; it only states the rules and the "
                 "remaining room.")
    return lines


def portfolio(env: Env) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "portfolio", "attention": attention(env)}
    pol = env.policy
    if pol is None or env.rj.schema_version() is None:
        return out
    book = env.rj.load_book()
    ex = compute_exposure(book)
    puts = [p for p in book.positions if p.reserves_collateral]
    groups = exposure_groups(book, pol)
    limits = portfolio_limits(book, pol)
    entries = _entries(limits, groups)
    names = company_names(book)
    snap = book.latest_snapshot
    by_state: dict[str, int] = {}
    for p in book.positions:
        by_state[p.state.value] = by_state.get(p.state.value, 0) + 1
    holdings = []
    for p in sorted(book.positions, key=lambda p: (p.ticker, p.expiration, p.trade_id)):
        if not (p.reserves_collateral or p.state is S.ASSIGNED_PENDING_RECONCILIATION):
            continue
        linked = any(l.linked_trade_id == p.trade_id for l in book.stock_lots)
        pending = p.state is S.ASSIGNED_PENDING_RECONCILIATION
        holdings.append({
            "kind": "put", "ref": p.trade_id, "ref_path": f"/positions/{p.trade_id}", "ticker": p.ticker, "state": p.state.value,
            "detail": f"expires {p.expiration.isoformat()} (DTE {_dte(p, env)}), strike {p.strike}, {p.contracts} x {p.multiplier}",
            "exposure_usd": p.collateral_usd,
            "counts_as": ("assignment obligation, counted through its linked stock lot" if pending and linked else
                          "unresolved assignment obligation (shares owed, not yet a lot)" if pending else
                          "put collateral" + (" (a closing order does not release it)" if p.state is S.CLOSE_WORKING else
                                              " (working order reservation)" if p.state in (S.WORKING, S.PENDING) else "")),
            "sector": p.sector, "bucket": p.correlation_bucket, "asset_type": asset_type_label(p.asset_type),
            "asset_type_source": env.asset_info(p.ticker)[1],
            "unknown": UNKNOWN_LABEL in (p.sector, p.correlation_bucket)})
    for l in sorted(book.stock_lots, key=lambda l: (l.ticker, l.lot_id)):
        holdings.append({
            "kind": "stock", "ref": l.lot_id, "ref_path": None, "ticker": l.ticker, "state": "owned shares",
            "detail": f"{l.shares} shares at reference {l.reference_price}", "exposure_usd": l.exposure_usd,
            "counts_as": "owned stock reference value" + (f" (from assignment of {l.linked_trade_id})" if l.linked_trade_id else ""),
            "sector": l.sector, "bucket": l.correlation_bucket, "asset_type": asset_type_label(l.asset_type),
            "asset_type_source": env.asset_info(l.ticker)[1],
            "unknown": UNKNOWN_LABEL in (l.sector, l.correlation_bucket)})
    unknown_tickers = sorted({h["ticker"] for h in holdings if h["unknown"]})
    out.update({
        "counts": {"active_positions": len(puts), "active_contracts": ex.active_contracts, "company_names": list(names),
                   "company_name_count": len(names),
                   "working_reservations": sum(1 for p in puts if p.state in (S.PENDING, S.WORKING, S.CLOSE_WORKING)),
                   "unresolved_assignments": sum(1 for p in book.positions if p.state is S.ASSIGNED_PENDING_RECONCILIATION),
                   "lifetime_trades": sum(1 for p in book.positions if p.state is not S.DRAFT), "by_state": by_state},
        "exposure": {"put_collateral_usd": ex.put_collateral_usd, "owned_stock_usd": ex.owned_stock_usd,
                     "assigned_pending_usd": ex.assigned_pending_usd, "aggregate_usd": ex.aggregate_equity_risk_usd,
                     "emergency_ceiling_usd": pol.portfolio.emergency_absolute_exposure_ceiling_usd,
                     "emergency_breached": ex.aggregate_equity_risk_usd > pol.portfolio.emergency_absolute_exposure_ceiling_usd},
        "cash": None if snap is None else {"free_cash_usd": snap.free_unencumbered_cash_usd, "equity_usd": snap.equity_usd,
                                           "asof_utc": snap.asof_utc, "confirmed": snap.manual_confirmed},
        "holdings": holdings, "unknown_tickers": unknown_tickers,
        "groups": {d: [g for g in groups if g.dimension == d] for d in ("ticker", "sector", "bucket", "expiry_week")},
        "limits": limits, "entries": entries, "mitigation": mitigation_lines(entries, groups),
        "label_paths": {t: f"/portfolio/labels/{t}" for t in sorted({h["ticker"] for h in holdings})},
        "notes": ["Exposure is a budget (collateral + stock reference value), not maximum possible loss.",
                  "A closing order does not release capacity: the put stays counted until it is recorded closed, canceled or expired.",
                  "Unknown sector or bucket labels are never counted as diversified; a candidate whose concentration cannot be assessed "
                  "is shown as failing LABELS_UNKNOWN."],
        "policy": {"policy_id": pol.policy_id, "policy_version": pol.policy_version},
    })
    return out


# ---------------------------------------------------------------- label correction (read side)
def label_form(env: Env, ticker: str, values: dict | None = None, errors: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"view": "label_form", "attention": attention(env), "ticker": ticker, "values": values or {},
                           "errors": errors or []}
    bad = _trade_ready(env)
    if bad:
        out["unavailable"] = bad
        return out
    if env.approved is None:
        out["unavailable"] = "Your approval file cannot be read, so label corrections are refused (nothing is assumed)."
        return out
    pos, lots = env.rj.label_rows(ticker)
    if not pos and not lots:
        out["unavailable"] = "No live put position or stock lot of that ticker carries labels to correct."
        return out
    ap = env.approval(ticker)
    rows = [{"kind": "put", **r} for r in pos] + [{"kind": "stock lot", **r} for r in lots]
    known_s = {r["sector"] for r in rows if r["sector"] != UNKNOWN_LABEL}
    known_b = {r["bucket"] for r in rows if r["bucket"] != UNKNOWN_LABEL}
    out.update({"current": rows, "approval_labels": None if ap is None else (ap.sector, ap.correlation_bucket),
                "asset_type": asset_type_label(env.asset_info(ticker)[0]),
                "asset_type_source": env.asset_info(ticker)[1],
                "prefill": {"sector": next(iter(known_s)) if len(known_s) == 1 else (ap.sector if ap else ""),
                            "bucket": next(iter(known_b)) if len(known_b) == 1 else (ap.correlation_bucket if ap else "")},
                "asset_type_note": wl.ASSET_TYPE_NOTE})
    return out


def label_preview(env: Env, plan: "wl.LabelPlan") -> dict[str, Any]:
    return {"view": "label_preview", "attention": attention(env), "plan": plan}


def label_result(env: Env, code: str, ticker: str | None) -> dict[str, Any]:
    code = code if code in LABEL_RESULT_CODES else "bad_request"
    return {"view": "label_result", "attention": attention(env), "code": code, "message": wl.MESSAGES[code],
            "ok": code in ("label_recorded", "duplicate"), "ticker": ticker}


# ---------------------------------------------------------------- candidate capacity on the market page
@dataclass
class EntryContext:
    """Everything about the book that is the same for every contract of one symbol (computed once per page)."""
    book: Any
    policy: Policy
    approval: ApprovedUnderlying | None
    approval_state: str
    asset_type: str
    sector: str
    bucket: str
    labels_unknown: bool
    label_source: str
    cash: Decimal | None


def entry_context(env: Env, symbol: str, approval_state: dict[str, Any]) -> EntryContext | None:
    if env.policy is None or env.rj.schema_version() is None:
        return None
    book = env.rj.load_book()
    ap = env.approval(symbol)
    held = [x for x in list(book.positions) + list(book.stock_lots) if x.ticker == symbol
            and (not isinstance(x, PutPosition) or x.reserves_collateral or x.state is S.ASSIGNED_PENDING_RECONCILIATION)]
    if ap is not None:
        sector, bucket, atype, src = ap.sector, ap.correlation_bucket, ap.asset_type, "your approval record"
    else:
        sector = next((x.sector for x in held if x.sector != UNKNOWN_LABEL), UNKNOWN_LABEL)
        bucket = next((x.correlation_bucket for x in held if x.correlation_bucket != UNKNOWN_LABEL), UNKNOWN_LABEL)
        atype = env.asset_info(symbol)[0]
        src = "labels on your existing holding of this ticker" if held else "none recorded: UNKNOWN"
    snap = book.latest_snapshot
    return EntryContext(book, env.policy, ap, approval_state["state"], atype, sector, bucket,
                        UNKNOWN_LABEL in (sector, bucket), src, snap.free_unencumbered_cash_usd if snap is not None else None)


def entry_review(env: Env, ctx: EntryContext, symbol: str, r: dict, sess: SessionInfo, stale: bool) -> dict[str, Any]:
    """Per candidate contract: current exposure, proposed addition, post-trade exposure, remaining capacity, and EVERY failing
    rule. `all_pass` is true only when the full entry gate (approval, event clearance, contract rules, ledger, drawdown, session,
    every portfolio cap, free cash) and the label check each pass independently. Prices are never hidden by any of this."""
    exp = date.fromisoformat(r["expiration"])
    proposed = ProposedPut(symbol, ctx.asset_type, ctx.sector, ctx.bucket, exp, dec(r["strike"]), 100, 1)
    caps = capacity_rows(ctx.book, proposed, ctx.policy, ctx.cash)
    failing: set[str] = {c.reason.value for c in caps if c.breached}
    evaluated = not stale
    if evaluated:
        try:
            failing |= {c.value for c in evaluate_entry(observation_from_row(symbol, r), ctx.approval, ctx.book, ctx.policy, env.now, sess,
                                                         contracts=1).reason_codes}
        except Exception:  # noqa: BLE001 - an unreadable stored value is "not evaluated", never a pass
            evaluated = False
    if ctx.approval is None:
        failing.add("NOT_APPROVED")
    if ctx.labels_unknown:
        failing.add(wt.LABELS_UNKNOWN)
    usd_rows = [c for c in caps if c.unit == "USD" and c.room_after is not None]
    tight = min(usd_rows, key=lambda c: c.room_after, default=None)
    agg = next((c for c in caps if c.key == "normal_aggregate"), None)
    ok_approval = ctx.approval is not None and ctx.approval_state == "APPROVED"
    return {"caps": caps, "failing": sorted(failing), "evaluated": evaluated,
            "all_pass": bool(evaluated and ok_approval and not failing),
            "current_usd": agg.used_before if agg else None, "added_usd": proposed.collateral_usd,
            "post_usd": agg.used_after if agg else None,
            "remaining_usd": tight.room_after if tight else None, "remaining_rule": tight.label if tight else None,
            "exhausted": any(c.room_after <= 0 for c in usd_rows)}
