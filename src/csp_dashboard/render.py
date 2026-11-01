"""Server-rendered HTML. Safe by construction: every dynamic value is escaped unless it is an explicit `Safe`
fragment built by `el()`. No JavaScript, no external assets (one same-origin stylesheet), no charts."""
from __future__ import annotations

import html
import json
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from csp_alerts.journal.models import from_iso
from csp_alerts.market.freshness import feed_label

UNAVAILABLE = "unavailable"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class Safe(str):
    """Already-escaped HTML."""


def clean(s: str) -> str:
    """Strip control/format characters (bidi overrides, zero width, ANSI) from untrusted text; keep \\n and \\t."""
    return "".join(c for c in s if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf"))


def clean_value(v: Any, limit: int = 120) -> str:
    """Untrusted text reflected into an input value: ALL control, format, surrogate, private-use, unassigned and line/paragraph
    separator characters dropped (including newline and tab), then length limited."""
    return "".join(c for c in str(v) if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))[:limit]


def esc(v: Any) -> Safe:
    if isinstance(v, Safe):
        return v
    return Safe(html.escape(clean(str(v)), quote=True))


def el(tag: str, *children: Any, **attrs: Any) -> Safe:
    a = "".join(f' {k.rstrip("_").replace("_", "-")}="{esc(v)}"' for k, v in attrs.items() if v is not None and v is not False)
    return Safe(f"<{tag}{a}>{''.join(esc(c) for c in children)}</{tag}>")


def join(items: list[Any], sep: str = "") -> Safe:
    return Safe(sep.join(esc(i) for i in items))


# ---------------------------------------------------------------- value formatting
def na() -> Safe:
    return el("span", UNAVAILABLE, class_="na")


def dt(v: Any, tz: str) -> Safe:
    """Local (Central) time with zone, plus the UTC evidence. None -> unavailable (never a default time)."""
    if v is None or v == "":
        return na()
    if isinstance(v, str):
        if not _ISO.match(v):
            return esc(v)
        v = from_iso(v)
    if not isinstance(v, datetime):
        return esc(v)
    loc = v.astimezone(ZoneInfo(tz))
    return Safe(f"{esc(loc.strftime('%Y-%m-%d %H:%M %Z'))} " + el("span", f"({v.strftime('%Y-%m-%dT%H:%M:%SZ')})", class_="utc"))


def d_(v: Any) -> Safe:
    return na() if v is None else esc(v.isoformat() if isinstance(v, date) else v)


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = v if isinstance(v, Decimal) else Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return x if x.is_finite() else None


def usd(v: Any) -> Safe:
    x = _dec(v)
    if x is None:
        return na()
    sign = "-" if x < 0 else ""
    x = abs(x)
    q = x.quantize(Decimal("0.01"))
    return esc(f"{sign}${q:,.2f}" if q == x else f"{sign}${x:,f}")


def px(v: Any) -> Safe:
    """Exact stored price/greek string."""
    x = _dec(v)
    return na() if x is None else esc(format(x, "f"))


def pct(v: Any) -> Safe:
    x = _dec(v)
    return na() if x is None else esc(f"{(x * 100):.2f}%")


def val(v: Any) -> Safe:
    return na() if v is None or v == "" else esc(v)


def yn(v: bool | None) -> Safe:
    return na() if v is None else el("span", "yes" if v else "NO", class_="ok" if v else "blk")


def badge(text: str, level: str = "info") -> Safe:
    return el("span", text, class_=f"badge {level}")


SEV = {"BLOCK": "blk", "WARN": "warn", "INFO": "info", "CRITICAL": "blk", "URGENT": "blk", "ACTION": "warn"}


def link(path: str, text: Any, **params: Any) -> Safe:
    qs = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return el("a", text, href=path + (f"?{qs}" if qs else ""))


def table(headers: list[str], rows: list[list[Any]], empty: str = "No rows.") -> Safe:
    if not rows:
        return el("p", empty, class_="muted")
    head = el("tr", *[el("th", h, scope="col") for h in headers])
    body = [el("tr", *[el("td", c) for c in r]) for r in rows]
    return el("table", el("thead", head), el("tbody", *body))


def kv(pairs: list[tuple[str, Any]]) -> Safe:
    return el("table", el("tbody", *[el("tr", el("th", k, scope="row"), el("td", v)) for k, v in pairs]), class_="kv")


def pager(path: str, data: dict, **keep: Any) -> Safe:
    parts = []
    if data.get("newer"):
        parts.append(link(path, "Newer", after=data["newer"], **keep))
    if data.get("older"):
        parts.append(link(path, "Older", before=data["older"], **keep))
    if not parts:
        return Safe("")
    return el("p", join(parts, " | "), class_="pager")


# ---------------------------------------------------------------- layout
NAV = [("/", "Overview"), ("/watchlist", "Watchlist"), ("/candidates", "Candidates"), ("/positions", "Positions"), ("/portfolio", "Portfolio"), ("/checkin", "Check-in"), ("/alerts", "Alerts"),
       ("/activity", "Activity"), ("/runs", "Runs"), ("/health", "Health")]


def meta_block(m: dict[str, Any], tz: str, refresh_path: str) -> Safe:
    ob = m.get("observed") or {}
    return el("section", el("h2", "Data and page timing"), kv([
        ("Page refreshed (not data time)", dt(m["rendered_at_utc"], tz)),
        ("Newest account snapshot", dt(ob.get("latest_snapshot_asof_utc"), tz)),
        ("Last scan started", dt(ob.get("last_scan_started_utc"), tz)),
        ("Last monitor started", dt(ob.get("last_monitor_started_utc"), tz)),
        ("Last journal event", dt(ob.get("last_audit_event_utc"), tz)),
        ("Ledger revision", val(m.get("ledger_revision"))), ("Journal event revision", val(m.get("audit_revision"))),
        ("Policy", Safe(f"{esc(m.get('policy_id') or UNAVAILABLE)} v{esc(m.get('policy_version') or UNAVAILABLE)} sha256 "
                        f"{esc((m.get('policy_sha256') or UNAVAILABLE)[:16])}")),
        ("Policy recorded in journal", yn(m.get("policy_recorded_in_journal"))),
        ("Journal schema", Safe(f"{esc(m.get('schema_version') or UNAVAILABLE)} (software expects {esc(m['schema_expected'])})")),
        ("Code identity", val(m.get("code_revision"))),
    ]), el("p", link(refresh_path, "Refresh (re-reads stored state only; starts no scan, sends nothing)"), class_="refresh"),
        class_="meta")


def attention_block(a: dict[str, Any]) -> Safe:
    if not a:
        return Safe("")
    blockers = [i for i in a["items"]]
    head = el("h2", "Current blockers and next manual steps")
    if not blockers:
        return el("section", head, el("p", "No blockers recorded.", class_="ok"), class_="attention")
    lis = [el("li", badge(i["severity"], SEV.get(i["severity"], "info")), " ", el("code", i["code"]), " ", i["text"]) for i in blockers]
    steps = [el("li", s) for s in a["steps"]]
    checkin_codes = {"NO_SNAPSHOT", "SNAPSHOT_STALE", "SNAPSHOT_UNCONFIRMED", "DAILY_CHECKIN_MISSING", "CHANGE_AFTER_SNAPSHOT", "CASH_INCONSISTENT"}
    web = (el("p", "You can also do this check-in on the web: ", link("/checkin", "account check-in"), class_="muted")
           if any(i["code"] in checkin_codes for i in blockers) else Safe(""))
    return el("section", head, el("ul", *lis),
              el("h3", "Next manual steps (run in your terminal; this page cannot do them)"),
              el("ol", *steps) if steps else el("p", "None required.", class_="muted"), web, class_="attention")


def layout(title: str, path: str, m: dict[str, Any], tz: str, body: Safe, attention: dict | None = None,
           refresh: str | None = None, auto_refresh: tuple[int, str] | None = None) -> str:
    nav = join([el("a", n, href=h, class_="cur" if h == path or (h != "/" and path.startswith(h + "/")) or (h == "/watchlist" and path.startswith("/market/")) else None)
                for h, n in NAV], " ")
    auto = Safe("") if auto_refresh is None else Safe(f'<meta http-equiv="refresh" content="{int(auto_refresh[0])};url={esc(auto_refresh[1])}">')
    doc = el("html", el("head", Safe('<meta charset="utf-8">'), auto, el("title", f"{title} - CSP dashboard"),
                        Safe('<meta name="viewport" content="width=device-width, initial-scale=1">'),
                        Safe('<link rel="stylesheet" href="/static/app.css">')),
             el("body",
                el("header", el("strong", "CSP alerts - local dashboard"),
                   el("span", " advisory only: no orders, no broker; shows stored records (writes: watchlist, refresh requests and the trades/check-ins you preview and confirm)", class_="muted"),
                   el("nav", nav),
                   el("form", el("button", "Log out", type="submit"), method="post", action="/logout", class_="logout")),
                el("main", el("h1", title), attention_block(attention or {}), body,
                   meta_block(m, tz, refresh or path)),
                el("footer", "Telegram acknowledgment does not prove you read or acted. Exposure figures are an exposure "
                             "budget, not maximum possible loss. Quotes are indicative.")))
    return "<!doctype html>" + doc


def login_page(error: str | None = None) -> str:
    doc = el("html", el("head", Safe('<meta charset="utf-8">'), el("title", "Sign in - CSP dashboard"),
                        Safe('<link rel="stylesheet" href="/static/app.css">')),
             el("body", el("main", el("h1", "CSP dashboard sign-in"),
                           el("p", error, class_="blk") if error else Safe(""),
                           el("form", el("label", "Password ", Safe('<input type="password" name="password" autocomplete="current-password" '
                                                                     'autofocus required maxlength="256">')),
                              el("button", "Sign in", type="submit"), method="post", action="/login"),
                           el("p", "Local use only (127.0.0.1). Writes: watchlist, refresh requests and the trades/check-ins you confirm.", class_="muted"))))
    return "<!doctype html>" + doc


def message_page(title: str, text: str) -> str:
    return "<!doctype html>" + el("html", el("head", Safe('<meta charset="utf-8">'), el("title", title),
                                              Safe('<link rel="stylesheet" href="/static/app.css">')),
                                  el("body", el("main", el("h1", title), el("p", text), el("p", link("/", "Home")))))


# ---------------------------------------------------------------- views
def _snap(s: dict | None, tz: str) -> Safe:
    if s is None:
        return na()
    age = s["age_hours"]
    return Safe(f"{usd(s['equity_usd'])} equity, {usd(s['free_cash_usd'])} net free cash, as of {dt(s['asof_utc'], tz)}, "
                f"{esc(f'{age:.1f}')} h old, {'confirmed' if s['confirmed'] else 'NOT confirmed'}")


def page_overview(d: dict, m: dict, tz: str) -> str:
    if "account" not in d:
        return layout("Overview", "/", m, tz, el("p", "Overview data is unavailable (see blockers above).", class_="muted"), d["attention"])
    ac, dd, ex = d["account"], d["drawdown"], d["exposure"]
    parts = [
        el("section", el("h2", "Account"), kv([
            ("Account scope", ac["scope"]), ("Paper-only bookkeeping", yn(ac["paper"])), ("Active profile", ac["profile"]),
            ("Policy", f"{ac['policy_id']} v{ac['policy_version']}"),
            ("Confirmed starting equity", Safe(f"{usd(ac['baseline_equity_usd'])} confirmed {dt(ac['baseline_confirmed_at_utc'], tz)}")),
            ("Latest confirmed snapshot", _snap(d["latest_confirmed_snapshot"], tz)),
            ("Latest snapshot (any)", _snap(d["latest_snapshot"], tz)),
        ])),
        el("section", el("h2", "Drawdown"), kv([
            ("Level (latched until manual reset)", Safe(f"{badge(dd['level'], 'blk' if dd['latched'] else 'ok')} latched: {'yes' if dd['latched'] else 'no'}")),
            ("Level from latest equity alone", dd["current_level"]),
            ("Recorded peak equity", usd(dd["peak_equity_usd"])), ("Latest confirmed equity", usd(dd["latest_equity_usd"])),
            ("Current drawdown", pct(dd["drawdown_fraction"])), ("As of", dt(dd["as_of_utc"], tz)),
            ("Thresholds pause / review / halt", Safe(f"{pct(dd['thresholds']['pause'])} / {pct(dd['thresholds']['review'])} / {pct(dd['thresholds']['halt'])}")),
        ])),
        el("section", el("h2", "Exposure (budget, not maximum possible loss)"), kv([
            ("Put collateral (pending, working, open, close-working, adjustment)", usd(ex["put_collateral_usd"])),
            ("Owned stock reference value", usd(ex["owned_stock_usd"])),
            ("Assignment pending (obligation not yet reconciled)", usd(ex["assigned_pending_usd"])),
            ("Aggregate exposure budget", usd(ex["aggregate_usd"])), ("Active put contracts", ex["active_contracts"]),
        ])),
    ]
    rows = []
    for r in d["limits"]:
        cnt = r.get("count")
        f = (lambda x: val(x)) if cnt else usd
        rows.append([r["name"] + (" (BINDING)" if r["binding"] else ""), f(r["used"]), f(r["limit"]),
                     na() if r["room"] is None else f(r["room"]),
                     badge("BREACHED", "blk") if r.get("breached") else ""])
    parts.append(el("section", el("h2", "Limits (room = limit - used)"), table(["Limit", "Used", "Limit", "Room", ""], rows)))
    iss = d["reconciliation"]["issues"]
    parts.append(el("section", el("h2", "Reconciliation"),
                    el("p", badge("BLOCKING", "blk") if d["reconciliation"]["blocking"] else badge("clean", "ok")),
                    table(["Code", "Severity", "Message", "Ref"], [[i["code"], i["severity"], i["message"], i["ref"]] for i in iss],
                          "No reconciliation issues.")))
    parts.append(el("section", el("h2", "Orders and obligations that are not plain open positions"), table(
        ["Trade", "Ticker", "State", "Expiry", "DTE", "Strike", "Collateral"],
        [[link(f"/positions/{o['trade_id']}", o["trade_id"]), o["ticker"], o["state"], d_(o["expiration"]), o["dte"],
          px(o["strike"]), usd(o["collateral_usd"])] for o in d["obligations"]],
        "None pending, working, close-working, under review or awaiting assignment reconciliation.")))
    parts.append(el("section", el("h2", "Stock lots"), table(
        ["Lot", "Ticker", "Shares", "Mark", "Mark time", "Reference value", "Linked put"],
        [[l["lot_id"], l["ticker"], px(l["shares"]), px(l["reference_price"]), dt(l["mark_asof_utc"], tz),
          usd(l["exposure_usd"]), val(l["linked_trade_id"])] for l in d["stock_lots"]], "No stock lots recorded.")))
    return layout("Overview", "/", m, tz, join(parts), d["attention"])


def _state_banner(states: list[dict]) -> Safe:
    if not states:
        return Safe("")
    return el("section", el("h2", "Why the list looks like this"), *[
        el("div", badge(s["title"], SEV.get(s["severity"], "info")), " ", s["text"], class_=f"state state-{s['code']}",
           id=f"state-{s['code']}") for s in states])


def _cand_table(rows: list[dict], tz: str) -> Safe:
    return table(["Contract", "Decision", "Failed gates", "Evaluated", "Bid", "Ask", "Model delta", "DTE", "OI", "Vol",
                  "Collateral", "Tier"],
                 [[link(f"/candidates/{r['evaluation_id']}", r["contract_key"]), r["decision"],
                   ", ".join(r["reason_codes"]) or "none", dt(r["evaluated_at_utc"], tz), px(r["bid"]), px(r["ask"]),
                   px(r["delta"]), val(r["dte"]), val(r["open_interest"]), val(r["daily_volume"]), usd(r["collateral_usd"]),
                   val(r["data_tier"])] for r in rows], "No evaluations match this filter.")


def page_candidates(d: dict, m: dict, tz: str, qs: dict) -> str:
    if "states" not in d:
        return layout("Candidates and reasons", "/candidates", m, tz, el("p", "Unavailable (see blockers).", class_="muted"), d["attention"])
    sel, c = d["selected_run"], d["counts"]
    parts = [_state_banner(d["states"])]
    ls = d["latest_scan"]
    parts.append(el("section", el("h2", "Evaluation run shown"), kv([
        ("Latest scan run", Safe(f"{esc(ls['status'])} at {dt(ls['started_at_utc'], tz)} {link('/runs/' + ls['run_id'], 'details')}") if ls else na()),
        ("Run shown", val(sel["run_id"])), ("Decision time", dt(sel["evaluated_at_utc"], tz)),
        ("Policy version at decision", val((d["rows"][0]["policy_version"]) if d["rows"] else None)),
        ("Freshness", badge("STALE: " + sel["stale_reason"], "warn") if sel["stale"] else badge("current", "ok")),
    ]), el("p", "Passing means it cleared every deterministic gate for research only. It is not approval to trade. "
                "A stale decision is history: do not act on it.", class_="muted")))
    tabs = [("pass", f"Passing research alternatives ({c['passing']})"), ("stale", f"Stale decisions ({c['stale']})"),
            ("rejected", f"Rejected evaluations ({c['rejected']})"), ("data", f"Contract data unavailable ({c['data_unavailable']})")]
    f = d["filters"]
    parts.append(el("p", join([link("/candidates", t, bucket=b, run=f["run"], ticker=f["ticker"]) if b != f["bucket"]
                               else el("strong", t) for b, t in tabs], " | "), class_="tabs"))
    parts.append(el("form", el("label", "Ticker ", Safe(f'<input name="ticker" maxlength="10" value="{esc(f["ticker"] or "")}">')),
                    Safe(f'<input type="hidden" name="bucket" value="{esc(f["bucket"])}">'),
                    Safe(f'<input type="hidden" name="run" value="{esc(f["run"] or "")}">'),
                    el("button", "Filter", type="submit"), method="get", action="/candidates"))
    parts.append(_cand_table(d["rows"], tz))
    parts.append(pager("/candidates", d, bucket=f["bucket"], ticker=f["ticker"], run=f["run"]))
    ne = d["not_evaluated"]
    cov = d["coverage"]
    parts.append(el("section", el("h2", "Symbols not evaluated because data failed"), table(
        ["Ticker", "Problem", "Detail", "Recorded"], [[i["ticker"], i["kind"], i["message"], dt(i["at_utc"], tz)] for i in ne],
        "No data failures recorded for this run.")))
    if cov:
        parts.append(el("section", el("h2", "Coverage"), kv([
            ("Source", cov["source"]), ("Symbols with usable data", ", ".join(cov["ok"]) if cov.get("ok") is not None else na()),
            ("Symbols unavailable", ", ".join(f"{u['ticker']} ({u['detail']})" for u in cov["unavailable"]) or "none"),
            ("Approved symbols with no record in this run", ", ".join(d.get("no_record_this_run") or []) or "none")])))
    parts.append(el("section", el("h2", "Failed-gate counts for this run"), table(
        ["Reason", "Contracts"], [[r["reason"], r["count"]] for r in d["reason_histogram"]], "No rejection reasons recorded.")))
    return layout("Candidates and reasons", "/candidates", m, tz, join(parts), d["attention"])


def page_candidate_detail(d: dict, m: dict, tz: str) -> str:
    r = d["row"]
    parts = [
        el("section", el("h2", "Decision"), kv([
            ("Contract", r["contract_key"]), ("Decision", r["decision"]), ("Class", r["bucket"]),
            ("Failed gates", ", ".join(r["reason_codes"]) or "none"), ("Evaluated", dt(r["evaluated_at_utc"], tz)),
            ("Policy version", val(r["policy_version"])), ("Data tier", val(r["data_tier"])),
            ("Run", link("/runs/" + r["run_id"], r["run_id"])), ("Cautions", "; ".join(r["cautions"]) or "none")])),
        el("section", el("h2", "Quote and contract (indicative; verify at your broker)"), kv([
            ("Spot", px(r["spot"])), ("Strike", px(r["strike"])), ("Expiration", val(r["expiration"])), ("DTE (from dates)", val(r["dte"])),
            ("Bid / ask", Safe(f"{px(r['bid'])} / {px(r['ask'])}")), ("Spread / mid", pct(r["spread_fraction_of_mid"])),
            (f"Delta ({r['delta_source'] or 'source unavailable'}; model, not a probability)", px(r["delta"])),
            ("Open interest / volume", Safe(f"{val(r['open_interest'])} / {val(r['daily_volume'])}")),
            ("Premium at bid (USD, 1 contract)", usd(r["premium_at_bid_usd"])), ("Collateral", usd(r["collateral_usd"])),
            ("Exposure before / after", Safe(f"{usd(r['exposure_before_usd'])} / {usd(r['exposure_after_usd'])}")),
            ("Quote retrieved", dt(r["retrieved_at_utc"], tz)), ("Exchange quote time", dt(r["quote_at_utc"], tz)),
            ("Event check (provider)", val(r["event_check"]))])),
    ]
    a = d["approval"]
    parts.append(el("section", el("h2", "Ownership and event-clearance context"),
                    kv([(k, v if v is not None else UNAVAILABLE) for k, v in a.items()]) if a else
                    el("p", "This ticker is not in the current approval file.", class_="blk")))
    parts.append(el("section", el("h2", "Stored decision record"), el("pre", json.dumps(d["decision"], indent=2, sort_keys=True))))
    return layout("Evaluation detail", "/candidates", m, tz, join(parts), d["attention"])


def _opt_quote_cell(p: dict, tz: str) -> Safe:
    """Stored bid/ask of THIS contract with its observation time and source. Never a stock price; never the current time."""
    ev = (p.get("price_check") or {}).get("evidence")
    if not ev:
        return na()
    return Safe(f"{quote_px(ev['bid'])} / {quote_px(ev['ask'])}<br><span class='muted'>observed {dt(ev['retrieved_at_utc'], tz)}; "
                f"{esc(ev.get('source') or 'stored')}</span>")


def page_positions(d: dict, m: dict, tz: str) -> str:
    if "rows" not in d:
        return layout("Positions", "/positions", m, tz, el("p", "Unavailable (see blockers).", class_="muted"), d["attention"])
    f = d["filters"]
    tabs = [(g, g) for g in ("live", "active", "reserved", "review", "history", "draft", "all")]
    parts = [el("p", join([link("/positions", t, group=g, ticker=f["ticker"]) if g != f["group"] else el("strong", t)
                           for g, t in tabs], " | "), class_="tabs"),
             el("form", el("label", "Ticker ", Safe(f'<input name="ticker" maxlength="10" value="{esc(f["ticker"] or "")}">')),
                Safe(f'<input type="hidden" name="group" value="{esc(f["group"])}">'),
                el("button", "Filter", type="submit"), method="get", action="/positions")]
    rows = []
    for p in d["rows"]:
        w = ", ".join(x["trigger"] for x in p["warnings"]) or "none"
        pl = p["pnl"]
        rows.append([link(f"/positions/{p['trade_id']}", p["trade_id"]), p["ticker"], p["state"], d_(p["expiration"]),
                     val(p["dte"]) if p["group"] != "history" else na(), px(p["strike"]), usd(p["collateral_usd"]) if p["reserves_collateral"] else "none",
                     px(p["open_credit_per_share"]), usd(p["fees_usd"]), w,
                     usd(pl["net_of_fees_usd"]) if pl else na(), _opt_quote_cell(p, tz)])
    parts.append(table(["Trade", "Ticker", "State", "Expiry", "DTE", "Strike", "Collateral", "Recorded credit/sh", "Fees",
                        "Triggered warnings", "Indicative net P&L", "Option bid / ask (observed)"], rows, "No positions match."))
    parts.append(pager("/positions", d, group=f["group"], ticker=f["ticker"]))
    parts.append(el("p", "P&L is shown only when a fresh two-sided stored quote exists; otherwise it is unavailable, never zero.",
                    class_="muted"))
    return layout("Positions", "/positions", m, tz, join(parts), d["attention"])


def _position_actions_section(p: dict) -> Safe:
    parts: list[Any] = [el("h2", "Record what happened (manual; this page places nothing)")]
    if p.get("breach_codes"):
        parts.append(el("p", Safe("Flagged: this reported execution breached policy (" + ", ".join(f"<code>{esc(x)}</code>" for x in p["breach_codes"])
                                  + "). New entries stay blocked while it reserves collateral and the breach still reproduces; it is re-checked "
                                  "automatically and cleared (audited) when it no longer does."), class_="blk"))
    if p.get("recorded_info_codes"):
        parts.append(el("p", Safe("Recorded with off-strategy contract characteristics (information): "
                                  + ", ".join(f"<code>{esc(x)}</code>" for x in p["recorded_info_codes"])
                                  + ". Shown for your records; they never blocked recording."), class_="muted"))
    if p["actions"]:
        parts.append(el("ul", *[el("li", link(a["path"], a["label"])) for a in p["actions"]]))
    elif p["state"] in ("ASSIGNED_PENDING_RECONCILIATION", "ADJUSTMENT_REVIEW") or p["contracts"] != 1:
        parts.append(el("p", "Use the documented CLI procedure for this state (RUNBOOK.md: assignment / adjustments / multi-contract). "
                             "The obligation stays visible and new entries stay blocked until it is resolved.", class_="blk"))
    else:
        parts.append(el("p", "No further recording applies to this state.", class_="muted"))
    return el("section", *parts)


def page_position_detail(d: dict, m: dict, tz: str) -> str:
    p = d["position"]
    pc = p["price_check"]
    ev = pc.get("evidence")
    parts = [
        el("section", el("h2", "Contract and recorded fills"), kv([
            ("Trade", p["trade_id"]), ("State", p["state"]), ("Ticker", p["ticker"]), ("Expiration / DTE", Safe(f"{d_(p['expiration'])} / {val(p['dte'])}")),
            ("Strike x multiplier x contracts", Safe(f"{px(p['strike'])} x {esc(p['multiplier'])} x {esc(p['contracts'])}")),
            ("Collateral reserved", usd(p["collateral_usd"]) if p["reserves_collateral"] else "none (does not reserve collateral)"),
            ("Recorded opening credit per share", px(p["open_credit_per_share"])), ("Opened", dt(p["opened_at_utc"], tz)),
            ("Recorded closing debit per share", px(p["close_debit_per_share"])), ("Closed", dt(p["closed_at_utc"], tz)),
            ("Fees (USD)", usd(p["fees_usd"])), ("Account scope / paper", Safe(f"{esc(p['account_scope'])} / {'yes' if p['paper'] else 'NO'}")),
            ("Last reconciled", dt(p["last_reconciled_at_utc"], tz)), ("Notes (untrusted text)", p["notes"] or "none")])),
        _position_actions_section(p),
        el("section", el("h2", "Triggered warnings"),
           table(["Trigger", "Severity", "Detail"], [[w["trigger"], w["severity"], json.dumps(w["detail"], default=str, sort_keys=True)]
                                                    for w in p["warnings"]], "No warnings triggered by stored data."),
           el("p", p.get("advice_note") or "", class_="muted")),
        el("section", el("h2", "Indicative price check"), kv([
            ("Status", pc["status"]), ("Reason", val(pc.get("reason"))),
            ("Stored quote retrieved", dt(ev["retrieved_at_utc"], tz) if ev else na()),
            ("Stored bid / ask", Safe(f"{quote_px(ev['bid'])} / {quote_px(ev['ask'])}") if ev else na()),
            ("Observation source", val(ev.get("source")) if ev else na()),
            ("Observed contract id", Safe(f"<code>{esc(ev['contract'])}</code> ({esc(str(ev.get('occ_source') or '').lower())} id)") if ev and ev.get("contract") else na()),
            ("Provider quote time", dt(ev.get("quote_time_utc"), tz) if ev and ev.get("quote_time_utc") else el("span", "unknown (the provider supplied none)", class_="na")),
            ("Indicative gross P&L", usd(p["pnl"]["gross_usd"]) if p["pnl"] else na()),
            ("Indicative net of fees", usd(p["pnl"]["net_of_fees_usd"]) if p["pnl"] else na())])),
    ]
    if d["lots"]:
        parts.append(el("section", el("h2", "Linked assignment lots"), table(
            ["Lot", "Shares", "Mark", "Mark time", "Cash paid (cost basis)"],
            [[l["lot_id"], px(l["shares"]), px(l["reference_price"]), dt(l["mark_asof_utc"], tz), usd(l["cost_basis_reference_usd"])]
             for l in d["lots"]])))
    parts.append(thresholds_block(p.get("thresholds")))
    parts.append(el("section", el("h2", "Alerts that referenced this position"), table(
        ["Alert", "Type", "Severity", "Delivery state", "Created"],
        [[link("/alerts/" + a["alert_id"], a["alert_id"][:8]), a["class"], a["severity"], a["delivery_state"], dt(a["created_at_utc"], tz)]
         for a in d["alerts"]], "No queued alerts reference this position."), el("p", link("/alerts", "All alerts and their delivery state"))))
    parts.append(el("section", el("h2", "Audit records"), table(
        ["Time", "Event", "Actor", "Summary"],
        [[dt(a["created_at_utc"], tz), link("/activity/" + a["event_id"], a["event_type"]), a["actor"], a["summary"]] for a in d["audit"]],
        "No audit records reference this position."), pager(f"/positions/{p['trade_id']}", d)))
    return layout(f"Position {p['trade_id']}", "/positions", m, tz, join(parts), d["attention"])


def page_alerts(d: dict, m: dict, tz: str) -> str:
    if "rows" not in d:
        return layout("Alerts", "/alerts", m, tz, el("p", "Unavailable.", class_="muted"), d["attention"])
    f = d["filters"]
    parts = [el("p", "These are advisory messages only; this page cannot resend a message or place, change or cancel anything. Delivery states: queued, retry "
                     "scheduled, acknowledged by Telegram API, unknown acknowledgment (never auto-resent), failed, dropped. "
                     "Telegram acknowledgment does not prove you read or acted.", class_="muted"),
             el("p", "Outbox counts: " + (", ".join(f"{k}={v}" for k, v in sorted(d["status_counts"].items())) or "empty"))]
    if d["total_rows"] == 0:
        parts.append(el("p", "No alerts are stored. Dry-run (the default) prints previews and never stores them; "
                             "only messages queued for real sending appear here.", class_="muted"))
    parts.append(el("form", el("label", "Status ", Safe(f'<input name="status" maxlength="20" value="{esc(f["status"] or "")}">')),
                    el("label", " Severity ", Safe(f'<input name="severity" maxlength="10" value="{esc(f["severity"] or "")}">')),
                    el("label", " Type ", Safe(f'<input name="type" maxlength="30" value="{esc(f["class"] or "")}">')),
                    el("button", "Filter", type="submit"), method="get", action="/alerts"))

    def source(a: dict) -> Safe:
        bits = []
        if a["trade_id"]:
            bits.append(link("/positions/" + a["trade_id"], "position " + a["trade_id"]))
        if a["ticker"]:
            bits.append(esc("ticker " + a["ticker"]))
        if a["decision_ref"]:
            bits.append(el("span", "decision ref " + ", ".join(f"{k}={str(v)[:12]}" for k, v in sorted(a["decision_ref"].items())), class_="muted"))
        return join(bits, "<br>") if bits else na()
    parts.append(table(["Alert ID (stable)", "Advisory type", "Severity", "Source", "Trigger", "Generated", "Delivery state", "Attempts"],
                       [[link("/alerts/" + a["alert_id"], a["alert_id"]), Safe(f"{esc(a['type_text'])}<br><span class='muted'>{esc(a['class'])}</span>"),
                         a["severity"], source(a), val(a["trigger"]), dt(a["created_at_utc"], tz),
                         Safe(f"{esc(a['delivery_state'])}" + (f"<br><span class='muted'>next attempt {dt(a['next_attempt_at_utc'], tz)}</span>"
                                                               if a["next_attempt_at_utc"] and a["status"] in ("PENDING", "RETRY") else "")
                              + (f"<br><span class='muted'>reason: {esc(a['last_error'])}</span>" if a["last_error"] and a["status"].startswith("DROPPED")
                                 or a["status"] == "FAILED_FINAL" and a["last_error"] else "")),
                         a["attempts"]] for a in d["rows"]], "No alerts match."))
    parts.append(pager("/alerts", d, status=f["status"], severity=f["severity"], type=f["class"]))
    parts.append(thresholds_block(d.get("thresholds")))
    return layout("Alerts", "/alerts", m, tz, join(parts), d["attention"])


def page_alert_detail(d: dict, m: dict, tz: str) -> str:
    a = d["alert"]
    parts = [el("section", kv([
        ("Alert ID (stable)", a["alert_id"]), ("Type", a["class"]), ("Severity", a["severity"]), ("Delivery state", a["delivery_state"]),
        ("Raw status", a["status"]), ("Generated", dt(a["created_at_utc"], tz)), ("Next attempt", dt(a["next_attempt_at_utc"], tz)),
        ("Attempts", a["attempts"]), ("Telegram message id", val(a["telegram_message_id"])),
        ("Position", link("/positions/" + a["trade_id"], a["trade_id"]) if a["trade_id"] else na()), ("Trigger", val(a["trigger"])),
        ("Ticker", val(a["ticker"])), ("Part", Safe(f"{val(a['part'])} of {val(a['parts'])}")), ("Expires", dt(a["expires_at_utc"], tz)),
        ("Policy sha256 at queue time", val(a["policy_sha256"])), ("Last delivery reason", val(a["last_error"])),
        ("Source scan/monitor run", "not recorded in the outbox"), ("Dedup key", d["dedup_key"])])),
        el("section", el("h2", "Message text (untrusted rendering as plain text)"), el("pre", d["content_text"])),
        el("section", el("h2", "Evidence"), el("pre", json.dumps(d["evidence"], indent=2, sort_keys=True, default=str)))]
    return layout("Alert detail", "/alerts", m, tz, join(parts), d["attention"])


def page_activity(d: dict, m: dict, tz: str) -> str:
    if "rows" not in d:
        return layout("Activity", "/activity", m, tz, el("p", "Unavailable.", class_="muted"), d["attention"])
    f = d["filters"]
    parts = [el("p", "Append-only audit log (newest first). Market changes are not fills; alerts are not trades.", class_="muted"),
             el("p", join([link("/activity", t, event_type=t) for t in d["event_types"]], " | "), class_="tabs"),
             table(["Time", "Event", "Actor", "Source", "Summary"],
                   [[dt(r["created_at_utc"], tz), link("/activity/" + r["event_id"], r["event_type"]), r["actor"], r["source"], r["summary"]]
                    for r in d["rows"]], "No audit events."),
             pager("/activity", d, event_type=f["event_type"])]
    return layout("Activity", "/activity", m, tz, join(parts), d["attention"])


def page_activity_detail(d: dict, m: dict, tz: str) -> str:
    e = d["event"]
    parts = [el("section", kv([("Event ID", e["event_id"]), ("Time", dt(e["created_at_utc"], tz)), ("Type", e["event_type"]),
                               ("Actor", e["actor"]), ("Source", e["source"]), ("Journal sequence", e["rid"])])),
             el("section", el("h2", "Before"), el("pre", json.dumps(d["before"], indent=2, sort_keys=True))),
             el("section", el("h2", "After"), el("pre", json.dumps(d["after"], indent=2, sort_keys=True)))]
    return layout("Audit event", "/activity", m, tz, join(parts), d["attention"])


def _run_cell(r: dict | None, tz: str) -> Safe:
    if r is None:
        return el("span", "never recorded", class_="na")
    return Safe(f"{esc(r['status'])} at {dt(r['started_at_utc'], tz)} {link('/runs/' + r['run_id'], 'details')}")


def page_runs(d: dict, m: dict, tz: str) -> str:
    if "rows" not in d:
        return layout("Run history", "/runs", m, tz, el("p", "Unavailable.", class_="muted"), d["attention"])
    f = d["filters"]
    parts = [el("p", join([link("/runs", k, kind=k) for k in ("SCAN", "MONITOR", "HEARTBEAT")] + [link("/runs", "all")], " | "), class_="tabs"),
             table(["Started", "Kind", "Status", "Reasons", "Policy", "Ledger rev.", "Code"],
                   [[link("/runs/" + r["run_id"], dt(r["started_at_utc"], tz)), r["kind"], r["status"], ", ".join(r["reasons"]) or "none",
                     (r["policy_sha256"] or "")[:12] or "not recorded", val(r["ledger_revision"]) if r["recorded_identity"] else "not recorded",
                     r["code_revision"] or "not recorded"] for r in d["rows"]], "No runs recorded."),
             pager("/runs", d, kind=f["kind"])]
    return layout("Run history", "/runs", m, tz, join(parts), d["attention"])


def page_run_detail(d: dict, m: dict, tz: str) -> str:
    r = d["run"]
    cov = d["coverage"]
    rec = "not recorded (run predates run identity)"
    parts = [el("section", kv([
        ("Run ID", r["run_id"]), ("Kind", r["kind"]), ("Status", r["status"]), ("Started", dt(r["started_at_utc"], tz)),
        ("Structured reasons", ", ".join(r["reasons"]) or "none"), ("Scope", r["scope"] or rec),
        ("Policy", Safe(f"{esc(r['policy_version'] or rec)} sha256 {esc(r['policy_sha256'] or rec)}")),
        ("Ledger revision seen", val(r["ledger_revision"]) if r["recorded_identity"] else rec), ("Code identity", r["code_revision"] or rec),
        ("Stored evaluations", Safe(f"{esc(d['evaluations'])} " + (link("/candidates", "view", run=r["run_id"], bucket="rejected")
                                                                    if d["evaluations"] else Safe(""))))])),
        el("section", el("h2", "Coverage"), kv([("Source", cov["source"]), ("Usable", ", ".join(cov["ok"]) if cov.get("ok") is not None else na()),
                                              ("Unavailable", ", ".join(f"{u['ticker']} ({u['detail']})" for u in cov["unavailable"]) or "none")])),
        el("section", el("h2", "Summary record"), el("pre", json.dumps(r["summary"], indent=2, sort_keys=True, default=str))),
        el("section", el("h2", "Data issues"), table(["Ticker", "Kind", "Message"], [[i["ticker"], i["kind"], i["message"]] for i in d["issues"]],
                                                    "None recorded."))]
    return layout("Run detail", "/runs", m, tz, join(parts), d["attention"])


def page_health(d: dict, m: dict, tz: str) -> str:
    if "runs" not in d:
        return layout("Health and reconciliation", "/health", m, tz, el("p", "Unavailable (see blockers).", class_="muted"), d["attention"])
    r, nr, dv = d["runs"], d["next_runs"], d["delivery"] or {"counts": {}, "last": None}
    last = dv["last"]
    cov = d["coverage_last_scan"]
    parts = [
        el("section", el("h2", "Runs"), kv([
            ("Last scan attempted", _run_cell(r["last_scan_attempt"], tz)), ("Last scan completed", _run_cell(r["last_scan_completed"], tz)),
            ("Last monitor attempted", _run_cell(r["last_monitor_attempt"], tz)), ("Last monitor completed", _run_cell(r["last_monitor_completed"], tz)),
            ("Health flags", ", ".join(d["flags"]) or "none"),
            ("Provider coverage, last scan", (", ".join(cov["ok"]) + " usable; " if cov.get("ok") else "") +
             (", ".join(f"{u['ticker']} unavailable ({u['detail']})" for u in cov["unavailable"]) or "no failures") if cov else na()),
            ("Last monitor fetch issues", ", ".join(d["monitor_fetch_issues"]) if d["monitor_fetch_issues"] else
             ("none recorded" if d["monitor_fetch_issues"] is not None else na()))]),
           el("p", link("/runs", "Full run history"))),
        el("section", el("h2", "Next runs (policy-derived estimate, NOT proof a task is installed)"), kv([
            ("Next scan", dt(nr["scan"], tz)), ("Next monitor", dt(nr["monitor"], tz)), ("Basis", nr["basis"])])),
        el("section", el("h2", "Delivery"), kv([
            ("Last outbox row", Safe(f"{esc(last['class'])} {esc(OUTBOX_STATE(last['status']))} at {dt(last['created_at_utc'], tz)}") if last else "none yet"),
            ("Outbox counts", ", ".join(f"{k}={v}" for k, v in sorted(dv["counts"].items())) or "empty"),
            ("Private chat pinned", yn(d["telegram_chat_pinned"])), ("Receipt in your Telegram", "unverified by this dashboard")]),
           el("p", link("/alerts", "Alerts and delivery states"))),
        el("section", el("h2", "Required manual records"), table(["Item", "Current?", "Detail"],
                                                              [[x["item"], yn(x["ok"]), dt(x.get("detail"), tz) if x.get("detail") else ""] for x in d["manual_records"]]),
           table(["Approved ticker", "Event review date", "Within allowed age?"],
                 [[a["ticker"], d_(a["events_reviewed_on"]), yn(a["ok"])] for a in d["approval_reviews"]], "No approved tickers.")),
        el("section", el("h2", "Not verified here"), el("ul", *[el("li", u) for u in d["unverified"]])),
        el("section", el("h2", "Operating limits"), el("ul", *[el("li", u) for u in d["limits"]])),
    ]
    return layout("Health and reconciliation", "/health", m, tz, join(parts), d["attention"])


def OUTBOX_STATE(s: str) -> str:
    from csp_dashboard.views import OUTBOX_LABEL
    return OUTBOX_LABEL.get(s, s)


# ---------------------------------------------------------------- Stage A: watchlist and market pages
AUTO_REFRESH_SECONDS = 60
MSG_TEXT = {
    "added": ("ok", "Added to the watchlist. Prices appear after the next market-refresh run."),
    "already": ("info", "Already on the watchlist; nothing changed."),
    "removed": ("ok", "Removed from the watchlist. Stored prices are kept; an active holding stays tracked and monitored."),
    "not_tracked": ("info", "That symbol was not on the watchlist; nothing changed."),
    "invalid": ("blk", "Rejected: not a valid ticker symbol (letters, digits, '.' or '-'; first character a letter; at most 10 characters)."),
    "full": ("blk", "Rejected: the watchlist is full (provider rate limits). Remove a symbol first."),
    "queued": ("ok", "Refresh requested. The next scheduled market-refresh run picks it up (within about 5 minutes); this page itself fetches nothing."),
    "pending": ("info", "A refresh request is already waiting for the next market-refresh run."),
    "rate": ("warn", "Refresh requests are rate limited (one per minute, ten per hour). Try again shortly."),
    "unknown_symbol": ("blk", "Refresh not requested: that symbol is not tracked."),
    "unavailable": ("blk", "The journal is missing or has not been upgraded for the watchlist (run `csp journal init`; a backup is taken first)."),
    "bad_request": ("blk", "Bad request."),
    "csrf": ("blk", "The form expired or was not issued by this page. Nothing was changed; reload the page and try again."),
}
STATE_LEVEL = {"UNAVAILABLE": "blk", "STALE": "warn", "MARKET-CLOSED": "info", "SESSION-UNKNOWN": "warn", "UNTIMESTAMPED": "info",
               "DELAYED": "warn", "RETRIEVED": "ok"}
STATUS_LEVEL = {"OK": "ok", "MARKET_CLOSED": "info", "PARTIAL": "warn", "RATE_LIMITED": "warn", "ERROR": "blk", "NEVER_FETCHED": "info"}


def age_text(sec: int | None) -> str:
    if sec is None:
        return UNAVAILABLE
    if sec < 90:
        return f"{sec} s ago"
    if sec < 5400:
        return f"{sec // 60} min ago"
    if sec < 172800:
        return f"{sec // 3600} h ago"
    return f"{sec // 86400} d ago"


def state_chips(labels: list[str]) -> Safe:
    return join([badge(x, STATE_LEVEL.get(x, "info")) for x in labels], " ")


def quote_px(v: Any) -> Safe:
    """Bid/ask: a stored 0 is the provider's 'no quote' (Yahoo conflates missing and zero), shown as such, never as a price."""
    x = _dec(v)
    if x is None:
        return na()
    if x <= 0:
        return el("span", "no quote (0)", class_="na")
    return esc(format(x, "f"))


def csrf_field(token: str | None) -> Safe:
    return Safe(f'<input type="hidden" name="csrf" value="{esc(token or "")}">')


def flash(msg: str | None) -> Safe:
    if not msg or msg not in MSG_TEXT:
        return Safe("")
    lvl, text = MSG_TEXT[msg]
    return el("p", badge(msg.upper(), lvl), " ", text, class_="state")


def feed_banner(label: str) -> Safe:
    return el("section", el("p", el("strong", label)),
              el("p", "A backend job (market-refresh) retrieves Yahoo data through Stockpile about every 5 minutes while the US "
                      "market is open and hourly otherwise. This page only re-reads what is stored and reloads itself every "
                      f"{AUTO_REFRESH_SECONDS} seconds; reloading never makes old evidence current. Retrieval time is not exchange "
                      "time: Yahoo gives no quote timestamp here, so quote age is unknown. Not a live exchange feed; verify at your broker.",
                   class_="muted"), class_="state")


def approval_badge(a: dict) -> Safe:
    lvl = {"APPROVED": "ok", "EXPIRED": "warn"}.get(a["state"], "blk")
    return Safe(f"{badge(a['state'].replace('_', ' '), lvl)} {esc(a['text'])}")


def _stock_cells(b: dict, tz: str) -> list[Any]:
    s = b["stock"]
    if s is None:
        return [na(), na(), na(), na()]
    return [Safe(f"{esc(s['price'])} <span class='muted'>({esc(s['price_type'])}, {esc(s['provider'])}, {esc(s['tier'])})</span>"),
            dt(s["market_time_utc"], tz) if s["market_time_utc"] else el("span", "unknown (provider gives none)", class_="na"),
            dt(s["retrieved_at_utc"], tz), esc(age_text(s["age_seconds"]))]


def page_watchlist(d: dict, m: dict, tz: str) -> str:
    csrf = m.get("_csrf")
    head = [feed_banner(d.get("feed_label") or feed_label([])), flash(d.get("message"))]
    if "rows" not in d:
        return layout("Watchlist and market data", "/watchlist", m, tz, join(head + [el("p", d.get("unavailable") or "Unavailable.", class_="blk")]),
                      d["attention"])
    lim, cad = d["limits"], d["cadence"]
    rows = []
    for b in d["rows"]:
        st = b["status"]
        rm = (el("form", csrf_field(csrf), Safe(f'<input type="hidden" name="symbol" value="{esc(b["symbol"])}">'),
                el("button", "Remove", type="submit"), method="post", action="/watchlist/remove")
              if b["reason_tracked"] in ("watchlist", "both") else el("span", "held: always tracked", class_="muted"))
        rows.append([link(f"/market/{b['symbol']}", b["symbol"]), b["reason_tracked"], approval_badge(b["approval"]),
                     *_stock_cells(b, tz),
                     Safe(f"{state_chips(b['state']['labels'])}<br><span class='muted'>{esc(b['state']['note'])}</span>"),
                     Safe(f"{badge(st['status'], STATUS_LEVEL.get(st['status'], 'info'))}"
                          + (f" <span class='muted'>session then: {esc(st['session_at_attempt'])}</span>" if st["session_at_attempt"] else "")),
                     dt(st["last_attempt_utc"], tz), dt(st["last_success_utc"], tz),
                     Safe(f"{dt(st['next_update_estimate_utc'], tz)}<br><span class='muted'>estimate, not a guarantee"
                          + (f"; backing off until {esc(st['backoff_until_utc'].strftime('%H:%M:%SZ'))}" if st["backoff_until_utc"] else "") + "</span>"),
                     st["last_error"] or el("span", "none", class_="muted"), b["contracts_stored"], rm])
    parts = head + [
        el("section", el("h2", "Tracked symbols"),
           el("p", f"Watchlist {lim['on_watchlist']} of {lim['max_watchlist']}. Market session now: {d['session']}. Tracking is not approval: "
                   "a tracked but unapproved symbol shows market data only and can never produce an entry alert. Active holdings are "
                   "always tracked and monitored, even if removed here or if their approval expires.", class_="muted"),
           table(["Symbol", "Why tracked", "Entry approval", "Stock price", "Provider market time", "Retrieved", "Age (retrieval)",
                  "Data state", "Last refresh status", "Last attempt", "Last success", "Next update (estimate)", "Provider status",
                  "Contracts stored", ""], rows, "Nothing is tracked yet. Add a symbol below; nothing is preselected.")),
        el("section", el("h2", "Add a symbol"),
           el("form", csrf_field(csrf), el("label", "Ticker ", Safe('<input name="symbol" maxlength="12" autocomplete="off" required>')),
              el("button", "Add to watchlist", type="submit"), method="post", action="/watchlist/add"),
           el("p", "Letters, digits, '.' or '-' (first character a letter, at most 10). The symbol is validated here; whether the provider "
                   "knows it shows up as a fetch error after the next refresh.", class_="muted")),
        el("section", el("h2", "Refresh now"),
           el("form", csrf_field(csrf), Safe('<input type="hidden" name="scope" value="ALL">'),
              el("button", "Request a refresh of all tracked symbols", type="submit"), method="post", action="/market/refresh-request"),
           el("p", "This writes a request that the next scheduled market-refresh run picks up (about every 5 minutes). The web page itself "
                   "never contacts the provider. Requests are limited to one per minute and ten per hour.", class_="muted"),
           table(["Requested", "By", "Scope", "Status", "Picked up", "Run"],
                 [[dt(r["requested_at_utc"], tz), r["requested_by"], r["scope"], r["status"], dt(r["picked_up_at_utc"], tz), (r["run_id"] or "")[:8] or na()]
                  for r in d["requests"]], "No refresh requests yet.")),
        el("section", el("h2", "Recent market-refresh runs"),
           table(["Started", "Trigger", "Session", "Status", "Symbols attempted"],
                 [[dt(r["started_at_utc"], tz), r["trigger"], r["session_status"], r["status"], ", ".join(json.loads(r["symbols_json"])) or "none due"]
                  for r in d["refresh_runs"]],
                 "No market-refresh run has been recorded. Run `csp market-refresh` or register the scheduled task (RUNBOOK section 9).")),
    ]
    return layout("Watchlist and market data", "/watchlist", m, tz, join(parts), d["attention"], auto_refresh=(AUTO_REFRESH_SECONDS, "/watchlist"))


def page_market(d: dict, m: dict, tz: str, qs: dict | None = None) -> str:
    sym = d["symbol"]
    csrf = m.get("_csrf")
    path = f"/market/{sym}"
    if "block" not in d:
        return layout(f"{sym} market data", path, m, tz, el("p", d.get("unavailable") or "Unavailable.", class_="blk"), d["attention"])
    b, f = d["block"], d["filters"]
    st = b["status"]
    parts: list[Any] = [feed_banner(d["feed_label"]), flash(d.get("message"))]
    if b["approval"]["state"] != "APPROVED":
        extra = "" if b["approval"]["state"] == "NOT_APPROVED" else b["approval"]["text"] + ". "
        parts.append(el("div", el("strong", "Not approved for entry alerts. "), extra + "This ticker shows market data only; it "
                                                                   "can never produce an entry alert until you approve it yourself (nothing here approves anything).",
                        class_="attention"))
    s = b["stock"]
    parts.append(el("section", el("h2", "Stock price (separate from option quotes)"), kv([
        ("Price", Safe(f"{esc(s['price'])} ({esc(s['price_type'])})") if s else na()),
        ("Price type note", s["price_type_note"] if s else na()),
        ("Provider / tier / written by", Safe(f"{esc(s['provider'])} / {esc(s['tier'])} / {esc(s['source'].lower())}") if s else na()),
        ("Provider market time", dt(s["market_time_utc"], tz) if s and s["market_time_utc"] else el("span", "unknown (the provider supplied none)", class_="na")),
        ("Retrieved", Safe(f"{dt(s['retrieved_at_utc'], tz)} ({esc(age_text(s['age_seconds']))})") if s else na()),
        ("Data state", Safe(f"{state_chips(b['state']['labels'])} <span class='muted'>{esc(b['state']['note'])}</span>")),
        ("Why tracked", b["reason_tracked"]),
        ("Last refresh status", Safe(f"{badge(st['status'], STATUS_LEVEL.get(st['status'], 'info'))} attempt {dt(st['last_attempt_utc'], tz)}; "
                                     f"success {dt(st['last_success_utc'], tz)}")),
        ("Provider status", st["last_error"] or "none"),
        ("Next update (estimate)", Safe(f"{dt(st['next_update_estimate_utc'], tz)} <span class='muted'>{esc(st['next_update_basis'])}</span>")),
    ]), el("form", csrf_field(csrf), Safe(f'<input type="hidden" name="scope" value="{esc(sym)}">'),
           Safe('<input type="hidden" name="back" value="market">'),
           el("button", f"Request a refresh of {sym}", type="submit"), method="post", action="/market/refresh-request")))
    exp_opts = [Safe('<option value="">all expiries</option>')] + [
        el("option", f"{e['expiration']} (DTE {e['dte']}, {e['contracts']} puts)", value=e["expiration"], selected=(e["expiration"] == f["expiry"]) or None)
        for e in d["expiries"]]
    parts.append(el("section", el("h2", "Put chain (latest stored quote per exact contract)"),
                    el("form", el("label", "Expiry ", el("select", *exp_opts, name="expiry")), " ",
                       el("label", "Strike from ", Safe(f'<input name="strike_min" size="8" maxlength="12" value="{esc(f["strike_min"] or "")}">')), " ",
                       el("label", "to ", Safe(f'<input name="strike_max" size="8" maxlength="12" value="{esc(f["strike_max"] or "")}">')), " ",
                       el("label", "Show ", el("select", el("option", "all puts (browse)", value="all", selected=(f["show"] != "pass") or None),
                                                el("option", "only those passing contract gates", value="pass", selected=(f["show"] == "pass") or None), name="show")),
                       el("button", "Filter", type="submit"), method="get", action=path),
                    el("p", d["gate_note"], class_="muted")))
    if not d["rows"]:
        parts.append(el("p", "No stored put contracts match." if d["expiries"] else
                        "No put contracts are stored for this symbol yet (the stock price above is independent of them).", class_="muted"))
    elif d["passing_in_view"] == 0:
        parts.append(el("p", d["no_pass_note"], class_="state"))
    parts.append(_entry_context_block(d.get("entry_context"), sym))
    if d.get("entry_context") is not None and d["rows"] and not d["all_gates_in_view"]:
        parts.append(el("p", "No contract in this view passes every gate (see the rule list on each row). Prices below are unaffected.", class_="state"))
    body = []
    for idx, r in enumerate(d["rows"]):
        g = r["gate"]
        mark = (badge("passes contract gates (research only)", "ok") if g["marker"] == "PASS" else
                badge("browsable", "info") if g["marker"] == "BROWSE" else badge("gates not evaluated", "warn"))
        body.append([Safe(f"<code>{esc(r['occ_symbol'])}</code> <span class='muted'>({esc(r['occ_source'].lower())} id)</span>"
                          + (f" {badge('identity conflict', 'blk')}" if r["identity_note"] else "")),
                     d_(r["expiration"]), r["dte"], px(r["strike"]), quote_px(r["bid"]), quote_px(r["ask"]), val(r["volume"]),
                     val(r["open_interest"]), px(r["delta"]),
                     Safe(f"{px(r['last'])}"), dt(r["last_trade_at_utc"], tz),
                     Safe(f"{dt(r['retrieved_at_utc'], tz)}<br>{esc(age_text(r['age_seconds']))} {state_chips(r['labels'])}"),
                     Safe(f"{esc(r['provider'])}<br>{esc(r['tier'])}<br>{esc(r['source'].lower())}"),
                     Safe(f"{mark}<br><span class='muted'>{esc(', '.join(g['codes']) if g['codes'] else g['text'])}</span>"),
                     _review_cell(r, sym, idx < CAPACITY_DETAIL_ROWS),
                     Safe(f"{link('/trade/sell', 'Record sold put', symbol=sym, expiry=r['expiration'].isoformat(), strike=r['strike'], kind='filled')}"
                          f"<br>{link('/trade/sell', 'I placed an order, not filled yet', symbol=sym, expiry=r['expiration'].isoformat(), strike=r['strike'], kind='working')}")])
    if body:
        parts.append(table(["Contract (OCC)", "Expiry", "DTE", "Strike", "Bid", "Ask", "Volume", "Open interest", "Delta (model, indicative)",
                            "Last print (not a quote)", "Last trade time", "Quote observed", "Provenance", "Contract-gate marker",
                            "All entry gates, exposure and capacity",
                            "Record a trade YOU made (manual; places nothing)"], body))
        if d["rows_matching"] > d["rows_shown_cap"]:
            parts.append(el("p", f"Showing the first {d['rows_shown_cap']} of {d['rows_matching']} matching puts; narrow the filter.", class_="muted"))
    if len(d["rows"]) > CAPACITY_DETAIL_ROWS:
        parts.append(el("p", f"The per-rule capacity table is expanded for the first {CAPACITY_DETAIL_ROWS} rows; every row still shows its verdict, "
                             "failing rules and exposure summary. Narrow the filter (expiry, strikes) to expand the others.", class_="muted"))
    parts.append(el("p", d["trade_recording_note"], class_="muted"))
    return layout(f"{sym} market data", path, m, tz, join(parts), d["attention"], auto_refresh=(AUTO_REFRESH_SECONDS, path))


CSS = """
body{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:0;color:#1d1d1f;background:#fafafa}
header{background:#14213d;color:#fff;padding:8px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header nav a{color:#cfe3ff;margin-right:10px;text-decoration:none}header nav a.cur{color:#fff;font-weight:700;text-decoration:underline}
.logout{margin-left:auto}main{padding:12px 16px;max-width:1280px}footer{padding:12px 16px;color:#555;font-size:12px}
h1{font-size:20px;margin:8px 0}h2{font-size:15px;margin:14px 0 6px;border-bottom:1px solid #ddd}h3{font-size:13px;margin:8px 0 4px}
table{border-collapse:collapse;margin:6px 0;background:#fff}th,td{border:1px solid #ddd;padding:3px 8px;text-align:left;vertical-align:top}
th{background:#f0f0f4}table.kv th{width:30%}
.attention{border:2px solid #b00020;background:#fff4f4;padding:6px 12px;margin:8px 0}
.blk{color:#b00020;font-weight:600}.warn{color:#8a5a00;font-weight:600}.ok{color:#0a6b2d;font-weight:600}.info{color:#1f4e8c}
.muted,.na,.utc{color:#666}.na{font-style:italic}.utc{font-size:12px}
.badge{border:1px solid currentColor;border-radius:3px;padding:0 5px;font-size:12px}
.state{margin:4px 0;padding:4px 8px;background:#fff;border-left:4px solid #888}
pre{background:#fff;border:1px solid #ddd;padding:8px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.tabs a,.pager a{margin:0 4px}.meta{margin-top:20px;border-top:2px solid #ccc}
input,button{font:inherit}
"""


# ---------------------------------------------------------------- Stage B1: recording forms (no JavaScript, no computation)
def _inp(name: str, value: Any = "", **attrs: Any) -> Safe:
    a = "".join(f' {k.replace("_", "-")}="{esc(v)}"' for k, v in attrs.items() if v is not None and v is not False)
    return Safe(f'<input name="{esc(name)}" value="{esc(clean_value(value if value is not None else ""))}"{a}>')


def _hidden(name: str, value: Any) -> Safe:
    return Safe(f'<input type="hidden" name="{esc(name)}" value="{esc(clean_value(value if value is not None else ""))}">')


def _errors(errs: list[str]) -> Safe:
    if not errs:
        return Safe("")
    return el("section", el("h2", "Please fix"), el("ul", *[el("li", e) for e in errs]), class_="attention")


def _how_note() -> Safe:
    return el("p", "This page only RECORDS a trade you already made at your broker. It places, changes or cancels nothing, and it "
                   "never uses a quoted price as your fill: you type the actual numbers. Step 1 previews (writes nothing); step 2 "
                   "confirms. Not available here (CLI only): baseline, cash flows, risk reset, approvals, assignment, partial "
                   "fills, adjustments and any quantity other than 1 contract.", class_="muted")


def _field(label: str, name: str, value: Any, hint: str = "", **attrs: Any) -> Safe:
    return el("p", el("label", label + " ", _inp(name, value, **attrs)), " ", el("span", hint, class_="muted") if hint else Safe(""))


def _no_journal(title: str, path: str, d: dict, m: dict, tz: str) -> str:
    return layout(title, path, m, tz, el("p", "This page needs the journal (run `csp journal init`); the dashboard never creates it.", class_="blk"),
                  d.get("attention"))


def page_trade_form(d: dict, m: dict, tz: str) -> str:
    if "action" not in d:
        return _no_journal("Record a trade", "/positions", d, m, tz)
    csrf = m.get("_csrf")
    title = d["title"]
    path = "/market" if d["kind"] == "new" else "/positions"
    if d.get("unavailable"):
        return layout(title, path, m, tz, join([_errors(d["errors"]), el("p", d["unavailable"], class_="blk"),
                                                el("p", link("/positions", "Back to positions"), " | ", link("/watchlist", "Back to watchlist"))]),
                      d["attention"])
    v = d["values"]
    parts: list[Any] = [_errors(d["errors"]), _how_note()]
    words = d.get("order_words")
    if words:
        parts.append(el("p", badge(words, "info"), " This is the order type you used at your broker. This page does not send it.", class_="state"))
    hidden = [csrf_field(csrf), _hidden("action", d["action"])]
    fields: list[Any] = []
    if d["kind"] == "new":
        c = d["context"]
        ref = c.get("reference_bid")
        parts.append(el("section", el("h2", "Exact contract (fixed; not editable)"), kv([
            ("Ticker", c["symbol"]), ("Contract id (OCC)", Safe(f"<code>{esc(c['occ_symbol'])}</code> ({esc(str(c['occ_source']).lower())} id)")),
            ("Right", "P (put)"), ("Strike", px(c["strike"])), ("Expiration / DTE", Safe(f"{d_(c['expiration'])} / {esc(c['dte'])}")),
            ("Multiplier / contracts", "100 / 1 (the MVP records exactly 1 contract)"), ("Account scope", Safe(f"{esc(c['scope'])} (paper)")),
            ("Entry approval", "on your approved list" if c["approved"] else "NOT on your approved list (the trade is still recorded as it "
                                                                              "happened, flagged and acknowledged)"),
            ("Reference only (NOT your fill)", Safe(f"stored bid {quote_px(ref)} observed {dt(c.get('reference_retrieved_at_utc'), tz)}"
                                                    + (f" <strong>NOT CURRENT:</strong> {esc(c['retained_unusable'])}" if c.get("retained_unusable") else ""))),
        ])))
        hidden += [_hidden("symbol", d["symbol"]), _hidden("expiry", d["expiry"]), _hidden("strike", d["strike"]), _hidden("contracts", "1")]
        if not c["approved"]:
            fields += [_field("Sector label", "sector", v.get("sector", ""), "your own label; blank = unknown (flagged)", maxlength=40),
                       _field("Correlation bucket", "bucket", v.get("bucket", ""), "your own label; blank = unknown (flagged)", maxlength=40)]
        else:
            parts.append(el("p", f"Labels from your approval: sector {c['sector']}, bucket {c['bucket']}.", class_="muted"))
    else:
        p = d["position"]
        parts.append(el("section", el("h2", "Position"), kv([
            ("Trade", p["trade_id"]), ("State", p["state"]), ("Contract", Safe(f"{esc(p['ticker'])} {d_(p['expiration'])} PUT {px(p['strike'])} x "
                                                                             f"{esc(p['contracts'])} (multiplier {esc(p['multiplier'])})")),
            ("Recorded credit/share", px(p["open_credit_per_share"])), ("Collateral", usd(p["collateral_usd"]) if p["reserves_collateral"] else "none")])))
        hidden.append(_hidden("trade_id", p["trade_id"]))
    act = d["action"]
    when = [_field("Execution date (YYYY-MM-DD)", "executed_date", v.get("executed_date", ""), f"today is {d['today']} ({tz}); required",
                   maxlength=10, size=11, required=True),
            _field("Execution time (HH:MM, 24-hour)", "executed_time", v.get("executed_time", ""), f"{tz}; required", maxlength=5, size=6, required=True)]
    if act in ("sell_filled", "mark_filled"):
        fields += [_field("ACTUAL credit per share received", "credit", v.get("credit", ""), "from your broker fill; never the quoted bid",
                          maxlength=12, size=10, required=True),
                   _field("Fees (USD)", "fees", v.get("fees", ""), "total commissions and fees for this fill; required (type 0 if there were none)", maxlength=12, size=8, required=True)] + when
    elif act == "close_filled":
        fields += [_field("ACTUAL closing debit per share paid", "debit", v.get("debit", ""), "from your broker fill", maxlength=12, size=10, required=True),
                   _field("Closing fees (USD)", "fees", v.get("fees", ""), "required (type 0 if there were none)", maxlength=12, size=8, required=True)] + when
    elif act == "expire":
        fields.append(_field("Expiry date (YYYY-MM-DD)", "expiry_date", v.get("expiry_date", ""), f"today is {d['today']}; required, cannot be in the future", maxlength=10, size=11, required=True))
    parts.append(el("section", el("h2", "Your numbers"),
                    el("form", *hidden, *fields,
                       el("button", "Preview (writes nothing)", type="submit"), method="post", action="/trade/preview")))
    parts.append(el("p", link("/positions", "Cancel and go back")))
    return layout(title, path, m, tz, join(parts), d["attention"])


def _cap_table(caps: list) -> Safe:
    rows = []
    for r in caps:
        def f(x: Any) -> Any:
            return na() if x is None else (usd(x) if r.unit == "USD" else esc(format(x, "f")))
        rows.append([r.label, f(r.limit), f(r.used_before), f(r.added), f(r.used_after), f(r.room_after),
                     badge("unknown", "warn") if r.limit is None else badge("BREACH", "blk") if r.breached else
                     badge("book already over; this adds none", "warn") if r.room_after is not None and r.room_after < 0 else badge("within", "ok")])
    return table(["Rule", "Limit", "Used now", "This trade adds", "Used after", "Room after", "Result"], rows)


def page_trade_preview(d: dict, m: dict, tz: str) -> str:
    csrf, plan = m.get("_csrf"), d["plan"]
    wt_words = d.get("order_words")
    parts: list[Any] = [el("p", badge("PREVIEW", "warn"), " Nothing has been written. Review, then confirm below.", class_="state"),
                        _how_note(), el("section", el("h2", "Exact change that will be recorded"), kv([
                            ("Action", plan.title), *([("Order type at your broker", wt_words)] if wt_words else []), ("Trade id", plan.trade_id or "(new, assigned on confirm)"),
                            ("State change", plan.state_change)]), *[el("p", x) for x in plan.lines])]
    c = plan.contract
    if c:
        parts.append(el("section", el("h2", "Contract"), kv([
            ("Ticker", c.get("symbol")), ("Contract id", Safe(f"<code>{esc(c['occ_symbol'])}</code>")) if c.get("occ_symbol") else ("Contract id", na()),
            ("Right / strike / expiry", Safe(f"P / {px(c.get('strike'))} / {d_(c.get('expiration'))}")),
            ("Multiplier / contracts", Safe(f"{esc(c.get('multiplier'))} / {esc(c.get('contracts'))}")),
            ("Sector / bucket", Safe(f"{esc(c.get('sector') or 'n/a')} / {esc(c.get('bucket') or 'n/a')}"))])))
    if plan.collateral_usd is not None:
        parts.append(el("section", el("h2", "Collateral reserved"), el("p", Safe(
            f"strike x multiplier x contracts = {usd(plan.collateral_usd)}. It stays reserved until the position is closed, canceled or expired "
            "(a closing order does not release it).")), _cap_table(plan.caps) if plan.caps else Safe("")))
    for w in plan.warnings:
        parts.append(el("p", w, class_="muted"))
    if plan.breach_codes:
        parts.append(el("section", el("h2", "This reported execution BREACHES policy"), el("ul", *[
            el("li", el("code", x), " ", d["reasons_text"].get(x, "")) for x in plan.breach_codes]),
            el("p", "It is NOT rejected: reality is recorded as it happened, flagged, shown on the Positions page and new entries stay "
                    "blocked while it reserves collateral. You must tick the acknowledgement below.", class_="blk"), class_="attention"))
    if plan.info_codes:
        parts.append(el("p", Safe("Contract-level entry-rule results at the stored quote (information, not a ledger breach; they do NOT block "
                                  "recording and are saved with the record): "
                                  + ", ".join(f"<code>{esc(x)}</code>" for x in plan.info_codes)), class_="muted"))
    if plan.context_codes:
        parts.append(el("p", Safe("Account state right now (information): " + ", ".join(f"<code>{esc(x)}</code>" for x in plan.context_codes)),
                        class_="muted"))
    if plan.still_blocked:
        parts.append(el("p", Safe("After this snapshot these issues would remain blocking: " + ", ".join(f"<code>{esc(x)}</code>" for x in plan.still_blocked)),
                        class_="blk"))
    elif plan.action == "checkin":
        parts.append(el("p", "After this snapshot the ledger would have no blocking reconciliation issue (drawdown and other gates are separate).",
                        class_="ok"))
    if plan.commitment_changed:
        parts.append(el("p", "After confirming, new-entry alerts stay blocked until you record a new account snapshot on the "
                             "check-in page (reconcile now).", class_="muted"))
    form_children: list[Any] = [csrf_field(csrf), _hidden("tok", d["token"]), _hidden("rev", plan.ledger_revision),
                                _hidden("psha", plan.policy_sha)]
    form_children += [_hidden(k, v) for k, v in plan.fields.items()]
    form_children.append(el("p", el("label", "Reason (required, stored in the audit log) ",
                                    Safe('<input name="reason" maxlength="300" size="60" required autocomplete="off">'))))
    for ck in plan.checks:
        form_children.append(el("p", Safe(f'<label><input type="checkbox" name="{esc(ck.name)}" value="1" required> {esc(ck.text)}</label>')))
    form_children.append(el("button", "Confirm and record", type="submit"))
    parts.append(el("section", el("h2", "Confirm"), el("form", *form_children, method="post", action=d["confirm_path"]),
                    el("p", link(d["back"], "Change the numbers"), " | ", link("/positions", "Cancel"))))
    return layout(plan.title, "/positions", m, tz, join(parts), d["attention"])


def page_trade_result(d: dict, m: dict, tz: str) -> str:
    if "message" not in d:
        return _no_journal("Recording result", "/positions", d, m, tz)
    lvl = "ok" if d["ok"] else "blk"
    parts: list[Any] = [el("p", badge(d["code"].upper(), lvl), " ", d["message"], class_="state")]
    if d.get("position"):
        p = d["position"]
        parts.append(el("p", "Position ", link(f"/positions/{p['trade_id']}", p["trade_id"]), f" is now {p['state']}."))
    if d.get("breach_codes"):
        parts.append(el("p", Safe("Flagged as a policy breach: " + ", ".join(f"<code>{esc(x)}</code>" for x in d["breach_codes"])
                                  + ". New entries stay blocked while it reserves collateral."), class_="blk"))
    if d.get("f3"):
        parts.append(el("p", d["f3"], class_="attention"))
        parts.append(el("p", link("/checkin", "Reconcile now: account check-in")))
    if d["code"] in ("stale", "expired", "token"):
        parts.append(el("p", "Go back to the form and preview again; the ledger or session changed."))
    parts.append(el("p", link("/positions", "Positions"), " | ", link("/", "Overview")))
    return layout("Recording result", "/positions", m, tz, join(parts), d["attention"])


def page_checkin(d: dict, m: dict, tz: str) -> str:
    if "values" not in d:
        return _no_journal("Account check-in", "/checkin", d, m, tz)
    csrf = m.get("_csrf")
    if d.get("unavailable"):
        return layout("Account check-in", "/checkin", m, tz, join([el("p", d["unavailable"], class_="blk")]), d["attention"])
    v = d["values"]
    parts: list[Any] = [_errors(d["errors"]), _how_note(),
                        el("section", el("h2", "1. Review what the journal says (no retyping)"),
                           table(["Trade", "Ticker", "State", "Expiry", "DTE", "Strike", "Collateral", "Recorded credit/sh"],
                                 [[link(f"/positions/{p['trade_id']}", p["trade_id"]), p["ticker"], p["state"], d_(p["expiration"]), p["dte"],
                                   px(p["strike"]), usd(p["collateral_usd"]) if p["reserves_collateral"] else na(), px(p["credit"])]
                                  for p in d["positions"]], "No positions or orders on record."),
                           kv([("Expected put collateral reserved", usd(d["expected_collateral"])),
                               ("Last snapshot", _snap(d["snapshot"], tz))]),
                           *([el("ul", *[el("li", badge(i["severity"], SEV.get(i["severity"], "info")), " ", el("code", i["code"]), " ",
                                           i["message"], f" [{i['ref']}]" if i["ref"] else "") for i in d["issues"]])] if d["issues"] else []))]
    fields: list[Any] = [csrf_field(csrf), _hidden("action", "checkin"), _hidden("nlots", len(d["lots"])),
                         _field("Total account equity (USD)", "equity", v.get("equity", ""), "from your broker statement", maxlength=15, size=14, required=True),
                         _field("NET free cash for NEW puts (USD)", "free_cash", v.get("free_cash", ""),
                                "AFTER reserving collateral for the puts above (not gross cash)", maxlength=15, size=14, required=True)]
    for i, lot in enumerate(d["lots"]):
        fields += [_hidden(f"lot_{i}", lot["lot_id"]),
                   _field(f"Re-mark {lot['lot_id']} ({lot['ticker']}, {lot['shares']} sh; last {lot['price']} at {lot['asof']:%Y-%m-%d %H:%MZ})",
                          f"mark_{i}", v.get(f"mark_{i}", ""), "price per share; blank = leave unchanged", maxlength=12, size=10)]
    if d["lots_over_limit"]:
        parts.append(el("section", el("h2", "2. Confirm equity and cash"),
                        el("p", f"More than {d['max_lots']} stock lots: use the CLI check-in.", class_="blk"),
                        el("p", "Run `csp daily-check-in` in your terminal (RUNBOOK section 3). This page does not offer a partial check-in "
                                "because it could not show every lot.", class_="muted")))
        return layout("Account check-in", "/checkin", m, tz, join(parts), d["attention"])
    parts.append(el("section", el("h2", "2. Confirm equity and cash"),
                    el("form", *fields, el("button", "Preview (writes nothing)", type="submit"), method="post", action="/checkin/preview"),
                    el("p", "Same rules as `csp daily-check-in`: free cash + put collateral must not exceed equity (CASH_INCONSISTENT). The "
                            "snapshot is dated now. Baseline confirmation, cash-flow recording and risk resets stay CLI-only (restricted controls).",
                       class_="muted")))
    return layout("Account check-in", "/checkin", m, tz, join(parts), d["attention"])


# ---------------------------------------------------------------- Stage B2: portfolio, label correction, candidate capacity, alerts
def _m(unit: str, x: Any) -> Safe:
    """Value of a limit row in its own unit."""
    if x is None:
        return na()
    return usd(x) if unit == "USD" else esc(format(x, "f"))


def _limit_badge(breached: bool, exhausted: bool) -> Safe:
    return badge("OVER LIMIT", "blk") if breached else badge("NO ROOM", "warn") if exhausted else badge("within", "ok")


def _entries_table(entries: list[dict]) -> Safe:
    return table(["Limit", "Rule", "Used", "Limit", "Room", "Status", ""],
                 [[e["label"], e["reason"], _m(e["unit"], e["used"]), _m(e["unit"], e["limit"]), _m(e["unit"], e["room"]),
                   _limit_badge(e["breached"], e["exhausted"]), badge("BINDING (tightest in its unit)", "info") if e["binding"] else ""]
                  for e in entries])


def _groups_table(rows: list, title: str, keyname: str) -> Safe:
    return el("section", el("h3", title), table(
        [keyname, "Put collateral", "Owned stock", "Exposure", "Contracts", "Limit",
         "Room", "Status"],
        [[(g.key.upper() if g.dimension == "ticker" else g.key) + (" (UNKNOWN: flagged, never counted as diversified)" if g.unknown else ""), usd(g.put_collateral_usd),
          usd(g.owned_stock_usd), usd(g.exposure_usd), g.contracts, usd(g.limit), usd(g.room_usd), _limit_badge(g.breached, g.exhausted)]
         for g in rows], "Nothing held in this dimension."))


def thresholds_block(t: dict | None) -> Safe:
    if not t:
        return Safe("")
    return el("section", el("h2", "Active exit thresholds (provisional)"),
              el("p", f"Policy {t['policy_id']} version {t['policy_version']} (sha256 {t['policy_sha256'][:16]}). ", t["provisional_note"], class_="muted"),
              table(["Threshold", "Active value"], [[k, esc(str(v))] for k, v in t["rows"]]))


def page_portfolio(d: dict, m: dict, tz: str) -> str:
    if "counts" not in d:
        return layout("Portfolio", "/portfolio", m, tz, el("p", "Portfolio data is unavailable (see blockers above).", class_="muted"), d["attention"])
    c, ex, cash = d["counts"], d["exposure"], d["cash"]
    parts: list[Any] = [
        el("p", "Everything here is computed by the backend exposure rules and only displayed. Nothing on this page places, changes or "
                "cancels an order.", class_="muted"),
        el("section", el("h2", "Counts (positions, contracts and lifetime trades are different numbers)"), kv([
            ("Active positions (reserve collateral, incl. working orders)", c["active_positions"]),
            ("Active contracts", c["active_contracts"]),
            ("of which pending / working / closing-order reservations", c["working_reservations"]),
            ("Individual-company names (puts + shares; ETFs counted separately)",
             Safe(f"{esc(c['company_name_count'])}: {esc(', '.join(c['company_names']) or 'none')}")),
            ("Unresolved assignment obligations", c["unresolved_assignments"]),
            ("Lifetime recorded trades (any state, not drafts)", c["lifetime_trades"]),
            ("Trades by state", ", ".join(f"{k}={v}" for k, v in sorted(c["by_state"].items())) or "none")])),
        el("section", el("h2", "Exposure (budget, not maximum possible loss)"), kv([
            ("Put collateral (pending, working, open, close-working, adjustment)", usd(ex["put_collateral_usd"])),
            ("Owned stock reference value", usd(ex["owned_stock_usd"])),
            ("Unresolved assignment obligations (not yet a stock lot)", usd(ex["assigned_pending_usd"])),
            ("Aggregate exposure budget", usd(ex["aggregate_usd"])),
            ("Emergency ceiling (incident flag, never headroom)", Safe(f"{usd(ex['emergency_ceiling_usd'])} "
                                                                       + (str(badge("BREACHED", "blk")) if ex["emergency_breached"] else ""))),
            ("Cash (latest owner-entered NET free cash)",
             Safe(f"{usd(cash['free_cash_usd'])} free of {usd(cash['equity_usd'])} equity, as of {dt(cash['asof_utc'], tz)}, "
                  f"{'confirmed' if cash['confirmed'] else 'NOT confirmed'}") if cash else
             el("span", "unknown: no account snapshot (not treated as a pass)", class_="blk"))])),
    ]
    if d["unknown_tickers"]:
        parts.append(el("div", el("strong", "Unknown labels. "), "Holdings of " + ", ".join(d["unknown_tickers"]) + " have an UNKNOWN sector or "
                        "correlation bucket. Unknown is never counted as diversified, and any candidate whose concentration cannot be "
                        "assessed fails LABELS_UNKNOWN. Use the correction link in the holdings table.", class_="attention"))
    rows = []
    for h in d["holdings"]:
        flag = badge("UNKNOWN LABEL", "blk") if h["unknown"] else ""
        rows.append([link(h["ref_path"], h["ref"]) if h["ref_path"] else h["ref"], h["ticker"], h["state"], h["detail"],
                     usd(h["exposure_usd"]), h["counts_as"],
                     Safe(f"{esc(h['sector'].upper() if h['sector'] == 'unknown' else h['sector'])}"),
                     Safe(f"{esc(h['asset_type'])}<br><span class='muted'>{esc(h['asset_type_source'])}</span>"),
                     esc(h["bucket"].upper() if h["bucket"] == "unknown" else h["bucket"]), flag,
                     link(d["label_paths"][h["ticker"]], "correct labels")])
    parts.append(el("section", el("h2", "Holdings (every row shows ticker, sector, asset type and correlation bucket)"),
                    table(["Ref", "Ticker", "State", "Contract / shares", "Exposure", "Counted as", "Sector", "Asset type", "Correlation bucket", "",
                           "Labels"], rows, "No positions, working orders, obligations or stock lots.")))
    g = d["groups"]
    parts.append(el("section", el("h2", "Exposure by key and the limit that applies to it"),
                    _groups_table(g["ticker"], "Exposure by ticker", "Ticker"), _groups_table(g["sector"], "Exposure by sector", "Sector"),
                    _groups_table(g["bucket"], "Exposure by correlation bucket", "Correlation bucket"),
                    _groups_table(g["expiry_week"], "Collateral by expiration week", "ISO week")))
    parts.append(el("section", el("h2", "Binding limits (room = limit - used; limits apply together)"), _entries_table(d["entries"])))
    if d["mitigation"]:
        parts.append(el("section", el("h2", "What binds and what is left (rule-based; no advice)"), el("ul", *[el("li", x) for x in d["mitigation"]]),
                        class_="attention"))
    parts.append(el("section", el("h2", "Notes"), el("ul", *[el("li", n) for n in d["notes"]]),
                    el("p", f"Policy {d['policy']['policy_id']} version {d['policy']['policy_version']}. Limits are the configured policy values; "
                            "this page cannot change them.", class_="muted")))
    return layout("Portfolio", "/portfolio", m, tz, join(parts), d["attention"])


def page_label_form(d: dict, m: dict, tz: str) -> str:
    if "ticker" not in d:
        return _no_journal("Correct labels", "/portfolio", d, m, tz)
    csrf, t = m.get("_csrf"), d["ticker"]
    head = [_errors(d["errors"]), el("p", "Records the sector and correlation bucket YOU confirm for a ticker you hold. It changes the labels "
                                          "stored on that ticker's live puts and stock lots, nothing else: never approvals, event clearance, "
                                          "asset type or any order. Step 1 previews (writes nothing); step 2 confirms and is audited.",
                                     class_="muted")]
    if d.get("unavailable"):
        return layout(f"Correct labels for {t}", "/portfolio", m, tz, join(head + [el("p", d["unavailable"], class_="blk"),
                                                                                  el("p", link("/portfolio", "Back to the portfolio"))]),
                      d["attention"])
    v = {**d["prefill"], **{k: x for k, x in d["values"].items() if k in ("sector", "bucket")}}
    ap = d["approval_labels"]
    parts = head + [
        el("section", el("h2", "Labels stored now"), table(["Row", "Kind", "Sector", "Correlation bucket"],
                                                          [[r["id"], r["kind"], r["sector"], r["bucket"]] for r in d["current"]]),
           el("p", f"Asset type: {d['asset_type']} (source: {d['asset_type_source']}). Asset type: set by approval (CLI). ", d["asset_type_note"], class_="muted")),
    ]
    if ap:
        parts.append(el("p", f"{t} is on your approved list (sector '{ap[0]}', bucket '{ap[1]}'). The only correction accepted is the one that "
                             "makes the stored rows equal those approval labels; changing the approval itself is a CLI step.", class_="state"))
    parts.append(el("section", el("h2", "Labels you confirm"), el(
        "form", csrf_field(csrf), _hidden("ticker", t), _hidden("action", "label_correct"),
        _field("Sector", "sector", v.get("sector", ""), "your own label; letters, digits, spaces, _ & . -", maxlength=40, required=True),
        _field("Correlation bucket", "bucket", v.get("bucket", ""), "tickers that tend to move together share a bucket", maxlength=40, required=True),
        el("button", "Preview (writes nothing)", type="submit"), method="post", action="/portfolio/labels/preview")))
    parts.append(el("p", link("/portfolio", "Cancel and go back")))
    return layout(f"Correct labels for {t}", "/portfolio", m, tz, join(parts), d["attention"])


def page_label_preview(d: dict, m: dict, tz: str) -> str:
    csrf, plan = m.get("_csrf"), d["plan"]
    f = plan.fields
    parts: list[Any] = [
        el("p", badge("PREVIEW", "warn"), " Nothing has been written. Review, then confirm below.", class_="state"),
        el("section", el("h2", "Exact change that will be recorded"), kv([
            ("Ticker", plan.ticker), ("New sector", f["sector"]), ("New correlation bucket", f["bucket"]),
            ("Rows relabelled", ", ".join(f"{r['kind']} {r['id']} (now {r['sector']} / {r['bucket']})" for r in plan.current))]))]
    for n in plan.notes:
        parts.append(el("p", n, class_="muted"))
    parts.append(el("section", el("h2", "Exposure by label now"), table(
        ["Dimension", "Key", "Exposure", "Limit", "Room", "Status"],
        [[g.dimension, g.key, usd(g.exposure_usd), usd(g.limit), usd(g.room_usd), _limit_badge(g.breached, g.exhausted)] for g in plan.before])))
    parts.append(el("section", el("h2", "Exposure by label after this correction"), table(
        ["Dimension", "Key", "Exposure", "Limit", "Room", "Status"],
        [[g.dimension, g.key, usd(g.exposure_usd), usd(g.limit), usd(g.room_usd), _limit_badge(g.breached, g.exhausted)] for g in plan.after],
        "No holding remains under these labels.")))
    if plan.after_breached:
        parts.append(el("p", "After the correction these groups are over their limit: " + ", ".join(plan.after_breached) + ". Reality is recorded "
                             "as you confirm it; new puts that add to those groups will fail their gate until the exposure falls.", class_="blk"))
    kids: list[Any] = [csrf_field(csrf), _hidden("tok", d["token"]), _hidden("rev", plan.ledger_revision), _hidden("psha", plan.policy_sha)]
    kids += [_hidden(k, v) for k, v in plan.fields.items()]
    kids.append(el("p", el("label", "Reason (required, stored in the audit log) ", Safe('<input name="reason" maxlength="300" size="60" required autocomplete="off">'))))
    for ck in plan.checks:
        kids.append(el("p", Safe(f'<label><input type="checkbox" name="{esc(ck.name)}" value="1" required> {esc(ck.text)}</label>')))
    kids.append(el("button", "Confirm and record", type="submit"))
    parts.append(el("section", el("h2", "Confirm"), el("form", *kids, method="post", action="/portfolio/labels/confirm"),
                    el("p", link(f"/portfolio/labels/{plan.ticker}", "Change the labels"), " | ", link("/portfolio", "Cancel"))))
    return layout(f"Correct labels for {plan.ticker}", "/portfolio", m, tz, join(parts), d["attention"])


def page_label_result(d: dict, m: dict, tz: str) -> str:
    if "message" not in d:
        return _no_journal("Label correction result", "/portfolio", d, m, tz)
    parts: list[Any] = [el("p", badge(d["code"].upper(), "ok" if d["ok"] else "blk"), " ", d["message"], class_="state")]
    if d["code"] in ("stale", "expired", "token"):
        parts.append(el("p", "Go back to the portfolio and preview the correction again; the ledger, the labels or the session changed."))
    parts.append(el("p", link("/portfolio", "Portfolio"), " | ", link("/positions", "Positions")))
    return layout("Label correction result", "/portfolio", m, tz, join(parts), d["attention"])


CAPACITY_DETAIL_ROWS = 40          # per-rule capacity tables are expanded for the first rows only (page size); summaries are on every row


def _review_cell(r: dict, sym: str, detail: bool = True) -> Safe:
    rv = r.get("review")
    if rv is None:
        return na()
    if not rv["evaluated"]:
        head = badge("not evaluated (stale or unreadable data)", "warn")
    elif rv["all_pass"]:
        head = badge("passes all gates", "ok")
    elif "NOT_APPROVED" in rv["failing"]:
        head = badge("NOT APPROVED: no eligible marker", "blk")
    else:
        head = badge(f"fails {len(rv['failing'])} rule(s)", "blk")
    why = el("span", ", ".join(rv["failing"]), class_="muted") if rv["failing"] else Safe("")
    summary = Safe(f"{head}<br>{why}<br><span class='muted'>exposure {usd(rv['current_usd'])} + {usd(rv['added_usd'])} = {usd(rv['post_usd'])}; "
                   f"tightest room after: {usd(rv['remaining_usd'])} ({esc(rv['remaining_rule'] or UNAVAILABLE)})"
                   + (" EXHAUSTED" if rv["exhausted"] else "") + "</span>")
    if not detail:
        return summary
    return Safe(f"{summary}<details><summary>capacity by rule</summary>{_cap_table(rv['caps'])}</details>")


def _entry_context_block(ec: dict | None, sym: str) -> Safe:
    if ec is None:
        return Safe("")
    unk = ec["labels_unknown"]
    return el("section", el("h2", "Entry capacity for this ticker"), kv([
        ("Approval", ec["approval_state"].replace("_", " ")),
        ("Labels used (sector / bucket / asset type)", Safe(f"{esc(ec['sector'].upper() if ec['sector'] == 'unknown' else ec['sector'])} / "
                                                           f"{esc(ec['bucket'].upper() if ec['bucket'] == 'unknown' else ec['bucket'])} / {esc(ec['asset_type'])} "
                                                           f"<span class='muted'>(from {esc(ec['label_source'])})</span>")),
        ("Latest NET free cash", usd(ec["free_cash_usd"]) if ec["free_cash_usd"] is not None else el("span", "unknown", class_="blk"))]),
        el("p", "UNKNOWN labels: concentration cannot be assessed, so every contract below fails LABELS_UNKNOWN. ", link(ec["label_path"], "Correct labels")
           if unk and ec["label_path"] else "", class_="blk") if unk else Safe(""),
        el("p", "'Passes all gates' appears only when approval, event clearance, every contract rule, the ledger, drawdown, the session, every "
                "portfolio cap, free cash and the labels each pass independently. Prices stay visible whatever the capacity.", class_="muted"))
