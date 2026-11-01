"""F1/F6 status, heartbeat and health. Read-only: reports what the local journal knows.

The service cannot announce its own outage: a missed run is detected locally by `health` (and by the absence of
the daily heartbeat in Telegram). Next-run times come from the policy schedule; they do NOT prove that Windows
Task Scheduler tasks are registered or that the PC is awake.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from csp_alerts.journal.queries import delivery_summary
from csp_alerts.journal.models import PositionState, from_iso
from csp_alerts.journal.reconcile import exchange_date
from csp_alerts.journal.store import Journal
from csp_alerts.market import calendar as mcal
from csp_alerts.market.models import SessionStatus
from csp_alerts.policy.config import Policy
from csp_alerts.policy.entry import book_entry_blocks
from csp_alerts.policy.portfolio import compute_exposure

SCAN_OK = {"CANDIDATES", "NO_OPPORTUNITIES", "BLOCKED_LEDGER", "MARKET_CLOSED"}  # a run that completed (even if blocked)
MONITOR_OK = {"ALERTS", "NO_ALERTS", "NO_POSITIONS"}


def scan_run_status(scan_status: str, reasons: set[str]) -> str:
    """Map the scan's internal status to the owner-visible F1 vocabulary."""
    if scan_status == "CANDIDATES":
        return "CANDIDATES"
    if scan_status == "NO_TRADE":
        return "NO_OPPORTUNITIES"
    if scan_status == "DATA_UNAVAILABLE":
        return "DATA_UNAVAILABLE"
    if scan_status == "BLOCKED":
        return "MARKET_CLOSED" if reasons <= {"MARKET_CLOSED", "UNKNOWN_SESSION"} else "BLOCKED_LEDGER"
    return "SERVICE_FAILURE"


def _next_local(now_utc: datetime, hhmm: list[str], tz: str, need_trading_day: bool = True) -> datetime | None:
    zone = ZoneInfo(tz)
    local = now_utc.astimezone(zone)
    for add in range(0, 10):
        d = local.date() + timedelta(days=add)
        if need_trading_day and mcal.is_trading_day(d) is not True:
            continue
        for t in sorted(hhmm):
            h, m = map(int, t.split(":"))
            cand = datetime.combine(d, time(h, m), tzinfo=zone)
            if cand > now_utc:
                return cand
    return None


def next_runs(now_utc: datetime, policy: Policy, tz: str) -> dict[str, datetime | None]:
    scheduler_tz = policy.market.scheduler_timezone
    scans = _next_local(now_utc, policy.market.entry_scans_local, scheduler_tz)
    iv = policy.market.exit_monitor_interval_minutes
    session = mcal.session_info(now_utc)
    if session.status is SessionStatus.OPEN:
        # next boundary on the quarter-hour grid, measured in local minutes
        local = now_utc.astimezone(ZoneInfo(scheduler_tz))
        nxt = local.replace(second=0, microsecond=0) + timedelta(minutes=iv - local.minute % iv)
        monitor = nxt
    else:
        monitor = _next_local(now_utc, ["08:30"], scheduler_tz)
    return {"scan": scans, "monitor": monitor}


def collect_status(journal: Journal, policy: Policy, now_utc: datetime, tz: str) -> dict[str, Any]:
    book = journal.load_book()
    codes, dd = book_entry_blocks(book, policy, now_utc)
    session = mcal.session_info(now_utc)
    blockers = sorted(c.value for c in codes)
    if session.status is not SessionStatus.OPEN:
        blockers.append("MARKET_CLOSED" if session.status is SessionStatus.CLOSED else "UNKNOWN_SESSION")
    last_scan = journal.last_run("SCAN", SCAN_OK)
    last_scan_any = journal.last_run("SCAN")
    last_mon = journal.last_run("MONITOR", MONITOR_OK)
    last_mon_any = journal.last_run("MONITOR")
    nxt = next_runs(now_utc, policy, tz)
    d = delivery_summary(journal)
    flags: list[str] = []
    today = exchange_date(now_utc)
    iv = policy.market.exit_monitor_interval_minutes
    if session.status is SessionStatus.OPEN:
        if last_mon is None or now_utc - last_mon["started_at"] > timedelta(minutes=2 * iv + 5):
            if any(p.state.value in ("OPEN", "CLOSE_WORKING", "ADJUSTMENT_REVIEW") for p in book.positions):
                flags.append("MONITOR_OVERDUE")
    first_scan = policy.market.entry_scans_local[0]
    zone = ZoneInfo(policy.market.scheduler_timezone)
    h, m = map(int, first_scan.split(":"))
    if mcal.is_trading_day(today) is True and now_utc > datetime.combine(
            now_utc.astimezone(zone).date(), time(h, m), tzinfo=zone) + timedelta(minutes=45):
        if last_scan is None or exchange_date(last_scan["started_at"]) != today:
            flags.append("SCAN_MISSED_TODAY")
    exp = compute_exposure(book)
    unresolved = [f"{p.trade_id} {p.ticker} {p.state.value}" for p in book.positions
                  if p.state in (PositionState.ASSIGNED_PENDING_RECONCILIATION, PositionState.PENDING, PositionState.WORKING)]
    if any(p.state is PositionState.ASSIGNED_PENDING_RECONCILIATION for p in book.positions):
        flags.append("ASSIGNMENT_PENDING_RECONCILIATION")
    for bad in ("UNKNOWN_ACK", "FAILED_FINAL"):
        if d["counts"].get(bad):
            flags.append(f"DELIVERY_{bad}")
    for r in (last_scan_any, last_mon_any):
        if r and r["status"] == "SERVICE_FAILURE":
            flags.append(f"LAST_{r['kind']}_SERVICE_FAILURE")
    return {"now_utc": now_utc, "blockers": blockers, "drawdown_level": dd.level.value, "last_scan": last_scan_any,
            "last_good_scan": last_scan, "last_monitor": last_mon_any, "last_good_monitor": last_mon, "next": nxt,
            "exposure": {"put_collateral_usd": exp.put_collateral_usd, "owned_stock_usd": exp.owned_stock_usd,
                         "assigned_pending_usd": exp.assigned_pending_usd, "aggregate_usd": exp.aggregate_equity_risk_usd},
            "unresolved": unresolved,
            "delivery": d, "flags": sorted(set(flags)), "pinned": journal.pinned_chat() is not None}


def _fmt(dt: datetime | None, tz: str) -> str:
    return "never" if dt is None else dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %I:%M %p %Z")


def _exposure_line(e: dict[str, Any] | None) -> str:
    if not e:
        return "Exposure: UNKNOWN"
    return (f"Exposure: put collateral ${e['put_collateral_usd']:,.2f} + owned stock ${e['owned_stock_usd']:,.2f} + "
            f"assignment pending ${e['assigned_pending_usd']:,.2f} = ${e['aggregate_usd']:,.2f}")


def status_lines(st: dict[str, Any], tz: str) -> list[str]:
    def _txt(r):
        return "never" if r is None else f"{r['status']} at {_fmt(r['started_at'], tz)}"

    dv = st["delivery"]
    last = dv["last"]
    return [
        f"Last scan: {_txt(st['last_scan'])}",
        f"Last monitor: {_txt(st['last_monitor'])}",
        f"Next scan (per policy schedule): {_fmt(st['next']['scan'], tz)}",
        f"Next monitor (per policy schedule): {_fmt(st['next']['monitor'], tz)}",
        _exposure_line(st.get("exposure")),
        "Unresolved positions: " + ("; ".join(st.get("unresolved") or []) or "none"),
        "Current blockers: " + (", ".join(st["blockers"]) or "none"),
        "Last delivery: " + ("none yet" if last is None else f"{last['class']} {last['status']} at {last['created_at_utc']}"),
        "Outbox counts: " + (", ".join(f"{k}={v}" for k, v in sorted(dv["counts"].items())) or "empty"),
        "Telegram chat pinned: " + ("yes" if st["pinned"] else "NO"),
        "Health flags: " + (", ".join(st["flags"]) or "none"),
    ]


def heartbeat_line(journal: Journal, policy: Policy, now_utc: datetime, tz: str) -> str:
    st = collect_status(journal, policy, now_utc, tz)
    scan, mon = st["last_scan"], st["last_monitor"]
    return (f"Heartbeat: last scan {'never' if scan is None else scan['status'] + ' ' + _fmt(scan['started_at'], tz)}; "
            f"last monitor {'never' if mon is None else mon['status'] + ' ' + _fmt(mon['started_at'], tz)}; "
            f"blockers: {', '.join(st['blockers']) or 'none'}; flags: {', '.join(st['flags']) or 'none'}")
