"""Alert text templates (spec 9.3). Pure formatting; nothing here sends anything.

Output is Telegram-HTML-safe text: every dynamic string is control-char stripped and HTML-escaped, so it is
also readable as plain text. DTE is ALWAYS computed from dates at render time (never hard-coded). No annualized
yield and no probability-of-profit figures are shown; the bid-yield per calendar day is labelled an ordering
indicator only.
"""
from __future__ import annotations

import html
import unicodedata
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from csp_alerts.journal.reconcile import exchange_date

MAX_LEN = 4000  # Telegram sendMessage limit is 4096; keep headroom
_TOKEN_LIKE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{10,}")


@dataclass(frozen=True)
class AlertMessage:
    kind: str       # ENTRY_RESEARCH | NO_TRADE_DIGEST | DATA_ISSUE | ACCOUNT_RISK | RECONCILIATION_REQUIRED
    severity: str   # INFO | WARN | URGENT | CRITICAL
    text: str
    dedup_key: str
    parse_mode: str = "HTML"
    meta: dict | None = None  # e.g. ticker/approval hash for entry alerts (used by revalidation)


def esc(value: object, limit: int = 120) -> str:
    """Neutralize untrusted text: drop control characters/newlines, collapse spaces, HTML-escape, truncate."""
    s = _TOKEN_LIKE.sub("[REDACTED_TOKEN]", str(value))
    # drop control (Cc) and format (Cf: bidi overrides, zero-width, BOM) characters; newlines become spaces
    s = "".join(" " if ch in "\n\r\u2028\u2029" else ch for ch in s
                if ch in "\n\r\u2028\u2029" or unicodedata.category(ch) not in ("Cc", "Cf"))
    s = " ".join(s.split())[:limit]
    return html.escape(s, quote=False)


def usd(d: Decimal | None) -> str:
    if d is None:
        return "UNKNOWN"
    a = abs(d)
    cents = a.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # A negative amount that rounds to zero cents prints as plain "$0.00" (no negative zero).
    sign = "-" if d < 0 and cents != 0 else ""
    return f"{sign}${int(a):,}" if a == a.to_integral_value() else f"{sign}${a:,.2f}"


def px(d: Decimal | None) -> str:
    if d is None:
        return "UNKNOWN"
    return f"{d:.2f}" if d.as_tuple().exponent >= -2 else format(d, "f")


def _local_time(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return "UNKNOWN"
    t = dt.astimezone(ZoneInfo(tz))
    return t.strftime("%I:%M %p %Z").lstrip("0")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 20] + "\n...[truncated]"


def _cap_line(cand, policy) -> str:
    used = getattr(cand, "put_collateral_before_usd", None)
    cap = policy.put_collateral_cap_usd
    label = "Pilot cap" if policy.pilot.enabled else "Put collateral cap"
    if used is None:
        return f"{label} {usd(cap)}: usage UNKNOWN"
    return f"{label} {usd(cap)}: {usd(used)} used, {usd(cap - used)} room (before this trade)"


def entry_alert(cand, policy, tz: str = "America/Chicago") -> AlertMessage:
    """`cand` is a jobs.scan.Candidate (duck-typed: obs, approval, decision, components)."""
    o, a, d, c = cand.obs, cand.approval, cand.decision, cand.components
    now = d.evaluated_at_utc
    dte = (o.expiration - exchange_date(now)).days  # computed, never hard-coded
    mult = o.multiplier or 0
    premium = (o.bid or Decimal(0)) * mult
    collateral = d.additional_collateral_usd
    lines = [
        "<b>CSP RESEARCH CANDIDATE - INDICATIVE</b>",
        f"{esc(o.underlying, 12)} | {o.expiration.isoformat()} PUT | {usd(o.strike)} strike | {dte} DTE",
        f"1 contract | Spot {usd(o.spot)} | Model delta {o.delta:.3f}",
        f"Bid/Ask ${px(o.bid)}/${px(o.ask)} | OI {o.open_interest:,} | Today vol {o.daily_volume:,}",
        f"Potential premium at bid {usd(premium)} | Cash collateral {usd(collateral)}",
        f"Expiry break-even: {usd(o.strike - o.bid)} (before fees)",
        f"Exposure after fill: {usd(d.exposure_after_usd)} / {usd(policy.portfolio.normal_new_entry_exposure_ceiling_usd)} normal cap"
        + (f" | put collateral cap in pilot {usd(policy.pilot.max_deployed_put_collateral_usd)}" if policy.pilot.enabled else ""),
        f"Free cash after fill (owner-entered): {usd(d.manual_free_cash_after_usd)}",
        _cap_line(cand, policy),
        f"Account snapshot used: {_local_time(getattr(cand, 'snapshot_asof_utc', None), tz)}",
        f"Ordering indicators only (not a forecast): spread {c.spread_fraction_of_mid * 100:.1f}% of mid; "
        f"bid/collateral {c.bid_yield_per_calendar_day_pct:.3f}% per calendar day",
        f"Source: {esc(o.provider)}; tier {esc(o.data_tier)}; retrieved {_local_time(o.retrieved_at_utc, tz)}",
        "Bid/ask exchange timestamp: " + ("UNAVAILABLE - VERIFY LIVE" if o.quote_at_utc is None
                                          else o.quote_at_utc.strftime("%Y-%m-%d %H:%M:%SZ")),
        f"Approved acquisition ceiling: {usd(a.max_buy_strike)} | Event review: {a.events_reviewed_on.isoformat()}, "
        f"cleared through {a.events_cleared_through.isoformat()}",
        "Next manual action: verify the exact contract, earnings, quote and available cash at your broker; "
        "if you act, record the order and fill here, then run the check-in.",
        "NO ORDER SENT. Other alerts are alternatives, not approval to stack positions; "
        "capacity is rechecked after you record a fill.",
    ]
    key = f"ENTRY_RESEARCH|{exchange_date(now).isoformat()}|{o.underlying}|{o.expiration.isoformat()}|P|{o.strike:.2f}"
    from csp_alerts.runner import approval_sha
    return AlertMessage("ENTRY_RESEARCH", "INFO", _clip("\n".join(lines)), key,
                        meta={"ticker": o.underlying, "approval_sha": approval_sha(a)})


def coverage_line(coverage: list[tuple[str, str, str]] | None) -> str | None:
    """coverage = [(ticker, 'OK'|'UNAVAILABLE', detail)]. A partial scan must never read as a healthy no-trade."""
    if not coverage:
        return None
    ok = sum(1 for _, s, _ in coverage if s == 'OK')
    parts = "; ".join(f"{esc(t, 12)} {esc(s, 12)} {esc(d, 40)}".strip() for t, s, d in coverage[:12])
    flag = "" if ok == len(coverage) else " - PARTIAL: do not read this as a clean no-trade"
    return f"Coverage: {ok} of {len(coverage)} approved symbols scanned ({parts}){flag}"


def no_trade_digest(status: str, reason_counts: Mapping[str, int], now_utc: datetime, *, evaluated: int = 0,
                    tickers: int = 0, tz: str = "America/Chicago", heartbeat: str | None = None,
                    coverage: list[tuple[str, str, str]] | None = None, partial: bool = False) -> AlertMessage:
    day = exchange_date(now_utc).isoformat()
    top = sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    lines = [f"<b>CSP {'PARTIAL SCAN - NO CANDIDATE' if partial else 'NO-TRADE DIGEST'} - {esc(status)}</b>",
             f"Scan {_local_time(now_utc, tz)} | approved underlyings scanned: {tickers} | contracts evaluated: {evaluated}",
             "No candidate passed every hard gate. Reason counts (a contract can have several):"]
    lines += [f"  {esc(k, 40)}: {v}" for k, v in top] or ["  (none recorded)"]
    lines += ["This is the absence of an alert, not a recommendation."]
    cov = coverage_line(coverage)
    if cov:
        lines.append(cov)
    if heartbeat:
        lines.append(esc(heartbeat, 400))
    lines.append("NO ORDER SENT.")
    return AlertMessage("NO_TRADE_DIGEST", "WARN" if partial else "INFO", _clip("\n".join(lines)),
                        f"NO_TRADE_DIGEST|{day}" + ("|PARTIAL" if partial else ""))


def blocked_scan(reasons: list[str], now_utc: datetime, kind: str = "NO_TRADE_DIGEST", severity: str = "WARN",
                 tz: str = "America/Chicago", level: str | None = None) -> AlertMessage:
    day = exchange_date(now_utc).isoformat()
    lines = ["<b>CSP ENTRY SCAN BLOCKED</b>", f"Scan {_local_time(now_utc, tz)}: new-entry alerts are suspended.",
             "Reason codes: " + ", ".join(esc(r, 40) for r in reasons),
             "Existing-position monitoring is unaffected. Fix the manual record or wait for the market session.",
             "NO ORDER SENT."]
    return AlertMessage(kind, severity, _clip("\n".join(lines)), f"BLOCKED|{day}|{','.join(sorted(reasons))}" + (f"|{level}" if level else ""))


def data_issue(issues: list[tuple[str, str, str]], now_utc: datetime, tz: str = "America/Chicago",
               coverage: list[tuple[str, str, str]] | None = None) -> AlertMessage:
    day = exchange_date(now_utc).isoformat()
    lines = ["<b>CSP DATA ISSUE</b>", f"Scan {_local_time(now_utc, tz)}: market data unavailable; this is NOT a no-trade result."]
    lines += [f"  {esc(t, 12)}: {esc(k, 30)} - {esc(m, 100)}" for t, k, m in issues[:10]]
    cov = coverage_line(coverage)
    if cov:
        lines.append(cov)
    lines += ["Check the quote manually at your broker. No stale candidates were produced.", "NO ORDER SENT."]
    return AlertMessage("DATA_ISSUE", "WARN", _clip("\n".join(lines)), f"DATA_ISSUE|{day}|{','.join(sorted({t for t, _, _ in issues}))}")


TIER_R_LABEL = "INDICATIVE - quote age UNKNOWN - verify at your broker"

_EXIT_TITLES = {
    "PROFIT_EXIT": ("CSP PROFIT EXIT - MANUAL REVIEW", "ACTION"),
    "TIME_EXIT": ("CSP TIME EXIT - MANUAL REVIEW (21 DTE or less)", "ACTION"),
    "LOSS_REVIEW": ("CSP LOSS REVIEW - URGENT", "URGENT"),
    "DELTA_REVIEW": ("CSP DELTA REVIEW - URGENT", "URGENT"),
    "ASSIGNMENT_REVIEW": ("CSP ASSIGNMENT REVIEW - URGENT", "URGENT"),
    "ADJUSTMENT_REVIEW": ("CSP ADJUSTMENT REVIEW - URGENT", "URGENT"),
    "DATA_ISSUE": ("CSP DATA ISSUE (position)", "WARN"),
}
_EXIT_NEXT = {
    "PROFIT_EXIT": "Check the actual closing ask at your broker/simulator; if you buy to close, record the fill here.",
    "TIME_EXIT": "Close or reassess the position at your broker per your plan; record any fill here.",
    "LOSS_REVIEW": "Review the position now; an alert does not cap the loss. Record any action here.",
    "DELTA_REVIEW": "Verify delta independently and review downside/assignment risk; record any action here.",
    "ASSIGNMENT_REVIEW": "Check assignment/exercise status at your broker (early assignment is possible). If assigned, run: journal assign.",
    "ADJUSTMENT_REVIEW": "The contract may be non-standard (split/merger/adjusted deliverable). Verify every number at your broker.",
    "DATA_ISSUE": "Cannot estimate the exit price. Check the position and quote manually at your broker.",
}


def exit_alert(signal, pos, quote, policy, now_utc: datetime, snapshot_asof_utc: datetime | None, scope: str,
               tz: str = "America/Chicago") -> AlertMessage:
    """Exit/position alert. `signal` is a policy.exits.ExitSignal. DTE is computed from dates."""
    trig = signal.trigger.value
    title, _ = _EXIT_TITLES[trig]
    sev = signal.severity
    dte = (pos.expiration - exchange_date(now_utc)).days
    det = signal.detail
    lines = [
        f"<b>{esc(title)}</b>",
        f"Trade {esc(pos.trade_id, 40)} | {esc(pos.ticker, 12)} {pos.expiration.isoformat()} PUT {usd(pos.strike)} | "
        f"{pos.contracts} contract(s) | {dte} DTE | state {pos.state.value}",
        f"Trigger: {esc(trig)}",
    ]
    if "actual_open_credit" in det:
        lines.append(f"Actual opening credit ${px(det['actual_open_credit'])}/sh | indicative buyback ASK "
                     f"${px(det['buyback_ask'])}/sh | est gross P&L {usd(det['est_gross_pnl_usd'])} "
                     f"(net of recorded fees {usd(det['est_net_pnl_usd'])})")
    if "captured_fraction" in det:
        lines.append(f"Premium captured: {det['captured_fraction'] * 100:.0f}% (rule: 60% or more)")
    if "ask_multiple" in det:
        lines.append(f"Buyback ASK is {det['ask_multiple']:.2f}x the opening credit (rule: 2x or more)")
    if "delta" in det:
        lines.append(f"Model delta {det['delta']:.2f} (rule: abs 0.30 or more; {esc(det['delta_source'])})")
    if "problems" in det:
        lines.append("Problems: " + "; ".join(esc(x, 60) for x in det["problems"]))
    if "notice" in det:
        lines.append(esc(det["notice"], 200))
    if trig == "ASSIGNMENT_REVIEW":
        lines.append(f"DTE {dte}" + (" (final day)" if det.get("final_day") else "") +
                     ("; indicative spot is below the strike (in the money)" if det.get("itm_on_indicative_quote") else ""))
    if quote is not None and trig in ("PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW"):
        lines.append(f"Data: {TIER_R_LABEL}; retrieved {_local_time(quote.retrieved_at_utc, tz)}; exchange quote time UNKNOWN")
    elif trig in ("TIME_EXIT", "ASSIGNMENT_REVIEW"):
        lines.append("Data: calendar/ledger based; no quote needed")
    else:
        lines.append(f"Data: {TIER_R_LABEL}")
    lines.append(f"Account snapshot used: {_local_time(snapshot_asof_utc, tz)}")
    lines.append("Next manual action: " + _EXIT_NEXT[trig])
    lines.append("Advisory only: this does not cap losses or guarantee an exit price. NO ORDER SENT.")
    day = exchange_date(now_utc).isoformat()
    from csp_alerts.runner import position_fingerprint
    return AlertMessage(_exit_kind(trig), sev, _clip("\n".join(lines)), f"EXIT|{scope}|{pos.trade_id}|{trig}|{day}",
                        meta={"trade_id": pos.trade_id, "trigger": trig, "state": pos.state.value,
                              "fingerprint": position_fingerprint(pos)})


def unresolved_assignment_alert(pos, now_utc: datetime, scope: str, tz: str = "America/Chicago") -> AlertMessage:
    """Reminder for ASSIGNED_PENDING_RECONCILIATION (SR-03). Obligation = strike x multiplier x contracts (cash for the
    shares at the strike, before fees). Repeats once per exchange day until the owner completes `journal assign`."""
    day = exchange_date(now_utc).isoformat()
    shares = pos.multiplier * pos.contracts
    lines = [
        "<b>CSP ASSIGNMENT PENDING RECONCILIATION - URGENT</b>",
        f"Trade {esc(pos.trade_id, 40)} | {esc(pos.ticker, 12)} {pos.expiration.isoformat()} PUT {usd(pos.strike)} | "
        f"state {pos.state.value}",
        f"Unresolved obligation: {shares} shares at {usd(pos.strike)} = {usd(pos.collateral_usd)} (before fees). "
        "This exposure is NOT zero even though no stock lot is recorded yet.",
        "New-entry alerts stay blocked until you record the shares and cash paid.",
        "Next manual action: confirm the assignment at your broker, then run: journal assign (step 2: shares and cash).",
        "Advisory only. NO ORDER SENT.",
    ]
    from csp_alerts.runner import position_fingerprint
    return AlertMessage("ASSIGNMENT_REVIEW", "URGENT", _clip("\n".join(lines)),
                        f"UNRESOLVED|{scope}|{pos.trade_id}|{pos.state.value}|{day}",
                        meta={"trade_id": pos.trade_id, "trigger": "ASSIGNED_PENDING", "state": pos.state.value,
                              "fingerprint": position_fingerprint(pos)})


def _exit_kind(trig: str) -> str:
    if trig == "DATA_ISSUE":
        return "DATA_ISSUE"
    return {"LOSS_REVIEW": "LOSS_REVIEW", "DELTA_REVIEW": "DELTA_REVIEW", "PROFIT_EXIT": "PROFIT_EXIT",
            "TIME_EXIT": "TIME_EXIT"}.get(trig, "ASSIGNMENT_REVIEW")


def heartbeat(lines: list[str], now_utc: datetime, scope: str, tz: str = "America/Chicago") -> AlertMessage:
    day = exchange_date(now_utc).isoformat()
    body = ["<b>CSP SERVICE HEARTBEAT</b>", _local_time(now_utc, tz)] + [esc(x, 200) for x in lines] + [
        "A stopped or sleeping PC cannot announce its own outage: no heartbeat today means check the PC.",
        "NO ORDER SENT."]
    return AlertMessage("SERVICE_HEALTH", "INFO", _clip("\n".join(body)), f"HEARTBEAT|{scope}|{day}")


def service_failure(job: str, error: str, now_utc: datetime, scope: str, tz: str = "America/Chicago") -> AlertMessage:
    day = exchange_date(now_utc).isoformat()
    lines = ["<b>CSP SERVICE FAILURE</b>", f"{_local_time(now_utc, tz)}: job {esc(job, 20)} failed before finishing.",
             f"Error: {esc(error, 200)}", "Nothing from this run should be trusted. Check the PC and logs.",
             "NO ORDER SENT."]
    return AlertMessage("SERVICE_HEALTH", "URGENT", _clip("\n".join(lines)), f"SERVICE_FAILURE|{scope}|{job}|{day}")


def reconcile_reminder(issues: list[str], now_utc: datetime, scope: str, tz: str = "America/Chicago") -> AlertMessage:
    day = exchange_date(now_utc).isoformat()
    lines = ["<b>CSP RECONCILIATION REMINDER</b>", f"{_local_time(now_utc, tz)}: your manual record is not confirmed for today.",
             "Open items: " + (", ".join(esc(i, 40) for i in issues) or "daily check-in not done"),
             "Run: daily-check-in (review positions, enter equity and NET free cash, acknowledge).",
             "Until then new-entry alerts stay blocked; exit and data warnings continue. NO ORDER SENT."]
    return AlertMessage("RECONCILIATION_REQUIRED", "WARN", _clip("\n".join(lines)), f"RECONCILE_REMINDER|{scope}|{day}")
