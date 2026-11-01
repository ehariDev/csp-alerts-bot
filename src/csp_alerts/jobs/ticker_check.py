"""Read-only pre-approval ticker check (`ticker-check`). Advisory only; nothing is written or sent.

Purpose: before the owner runs `journal approve`, show the REAL data the scan would use for a symbol, with
provenance. Nothing here is invented or estimated: every missing value is printed as UNAVAILABLE.

Reuse (no duplicated gate logic):
  * fetching and earnings: market.stockpile.ChainSource (same adapter as the scan; sockets live only there)
  * normalization: market.normalize.normalize_rows
  * gates: policy.entry.evaluate_entry, called per put exactly as jobs.scan does, with approval=None and an empty
    book. Owner/context codes that cannot be known before approval or that are not properties of the contract
    (CONTEXT_CODES: NOT_APPROVED, ledger, drawdown, session) are separated from the contract gates and listed as
    "not evaluated". A contract "passes the contract gates" when no other reason code remains.

Assumption (printed on every report): Yahoo rows carry no deliverable information, so contracts are evaluated AS IF
the deliverable is a standard 100-share one (multiplier 100). The scan only does this after the owner attests it at
approval time. A contract symbol that disproves it (adjusted root) still yields NONSTANDARD_CONTRACT.

Formulas (USD):
  collateral(strike)            = strike * 100                      (one contract, cash-secured)
  per-ticker cap                = policy.portfolio.max_underlying_exposure_usd
  pilot cap                     = policy.put_collateral_cap_usd     (pilot.max_deployed_put_collateral_usd)
  fits caps                     = collateral <= min(per-ticker cap, pilot cap)   (empty book assumed; other caps,
                                  sector/correlation limits and free cash are NOT evaluated here)
  spread                        = ask - bid ; spread % of mid = spread / ((bid + ask) / 2)
  delta band                    = [policy.entries.min_abs_delta, policy.entries.max_abs_delta] on |delta|
  band-nearest contract         = in-band contract with the lowest collateral if any in-band, else the contract
                                  whose |delta| is closest to the band (distance = gap to the nearest band edge)
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from csp_alerts.journal.models import BookState
from csp_alerts.journal.reconcile import exchange_date
from csp_alerts.market import calendar as mcal
from csp_alerts.market import failures
from csp_alerts.market.models import TIER_R, SessionInfo
from csp_alerts.market.normalize import PROVIDER, normalize_rows, to_decimal
from csp_alerts.market.stockpile import ChainSource, SourceError
from csp_alerts.policy.config import Policy
from csp_alerts.policy.entry import evaluate_entry
from csp_alerts.policy.reasons import Reason

BANNER = ("Indicative research data, not a quote. Verify at your broker. "
          "Not an approval; this does not change the whitelist.")
ASSUMPTION = ("Evaluated as if the deliverable is a standard 100-share contract (Yahoo gives no deliverable data; "
              "you must confirm it yourself at approval).")
NOT_EVALUATED = ("owner approval (max buy strike, event clearance), free cash, sector/correlation/expiry-week caps, "
                 "ledger state, drawdown, market session")
LEVERAGE_NOTE = "not checked; verify yourself (leveraged/inverse products are not flagged by this data source)"

# Codes that are not contract properties or cannot be known before approval (shared with the dashboard's browse marker).
from csp_alerts.policy.browse import CONTEXT_CODES  # noqa: E402

VERDICT_DATA = "DATA UNAVAILABLE"
VERDICT_NO_LIQUID = "NO LIQUID CONTRACTS"
VERDICT_CAPS = "NOT ELIGIBLE UNDER CAPS"
VERDICT_PASS = "WOULD PASS GATES (research only)"

MAX_TICKERS = 20
EARNINGS_FLAG = "earnings unverified: owner must check"


def safe_text(value: object, limit: int = 300) -> str:
    """Untrusted text for display: drop control (Cc) and format (Cf: bidi, zero-width) characters, newlines become
    spaces, whitespace collapsed, truncated, and any verdict wording neutralised so echoed text can never forge a
    VERDICT line. Mirrors alerts.templates.esc without HTML escaping (console output)."""
    s = str(value)
    s = "".join(" " if ch in "\n\r\u2028\u2029" else ch for ch in s
                if ch in "\n\r\u2028\u2029" or unicodedata.category(ch) not in ("Cc", "Cf"))
    s = " ".join(s.split())[:limit]
    for v in (VERDICT_DATA, VERDICT_NO_LIQUID, VERDICT_CAPS, VERDICT_PASS, "WOULD PASS GATES", "VERDICT:"):
        s = re.sub(re.escape(v), "[verdict text removed]", s, flags=re.IGNORECASE)
    return s[:limit]   # replacement can lengthen the text, so the bound is applied last


_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


@dataclass(frozen=True)
class PutLine:
    expiration: date
    dte: int
    strike: Decimal
    bid: Decimal | None
    ask: Decimal | None
    delta: Decimal | None
    open_interest: int | None
    volume: int | None
    spread: Decimal | None
    spread_pct_of_mid: Decimal | None
    collateral: Decimal
    last_trade_at_utc: datetime | None
    gate_reasons: tuple[str, ...]      # contract-gate reason codes (context codes removed)
    passes: bool
    in_band: bool


@dataclass
class TickerReport:
    ticker: str
    status: str = "OK"                           # OK | UNAVAILABLE | INVALID
    unavailable: str = ""                        # reason when not OK
    provider: str = PROVIDER
    data_tier: str = TIER_R
    retrieved_at_utc: datetime | None = None
    spot: Decimal | None = None
    spot_note: str = "UNAVAILABLE"
    asset_type: str = "UNAVAILABLE (not provided by the data adapter; verify yourself)"
    leverage_note: str = LEVERAGE_NOTE
    next_earnings: date | None = None
    earnings_note: str = "UNAVAILABLE: owner must verify (never guessed)"
    chain_exists: bool | None = None
    expirations: list[tuple[date, int, int]] = field(default_factory=list)   # (expiration, dte, put count)
    rows_outside_window: int = 0
    puts: list[PutLine] = field(default_factory=list)
    lowest_strike: PutLine | None = None
    band_nearest: PutLine | None = None
    band_nearest_is_in_band: bool = False
    in_band_count: int = 0
    passing_count: int = 0
    cap_ticker_usd: Decimal | None = None
    cap_pilot_usd: Decimal | None = None
    verdict: str = VERDICT_DATA
    verdict_detail: str = ""
    notes: list[str] = field(default_factory=list)
    nearest_before: tuple[date, int] | None = None   # indicative listed expiry just before the window
    nearest_after: tuple[date, int] | None = None    # indicative listed expiry just after the window
    expirations_known: bool = False                  # the listed-expiry lookup succeeded
    in_window_expiry_listed: bool = False


def normalize_symbol(raw: str) -> str | None:
    s = raw.strip()
    if not s.isascii():          # reject BEFORE case folding: U+017F / U+0131 upper-case to ASCII S / I
        return None
    s = s.upper()
    return s if _SYMBOL.match(s) else None


def _fetch(source: ChainSource, ticker: str, policy: Policy, sleep: Callable[[float], None],
           window: tuple[int, int] | None = None):
    """Fetch with the policy's retry rule. `window` (min_dte, max_dte) defaults to the entry window; the market-refresh
    job passes a wider BROWSING window (display only; no gate uses it)."""
    attempts = 1 + policy.market.provider_retries
    lo, hi = window or (policy.entries.min_dte_calendar, policy.entries.max_dte_calendar)
    last: SourceError | None = None
    for i in range(attempts):
        try:
            return source.fetch_chain(ticker, lo, hi), None
        except SourceError as exc:
            last = exc
            if exc.kind in ("RATE_LIMIT", "STOCKPILE_MISSING", "STOCKPILE_IMPORT"):
                break
        except Exception as exc:  # noqa: BLE001
            last = SourceError.from_exception(exc)   # canned code only; a hostile __str__ cannot escape
        if i + 1 < attempts:
            sleep(2 ** i)
    return None, last


def _explain(exc: SourceError) -> str:
    """Canned text for the failure CODE only; the raw provider message is never shown (see market/failures.py)."""
    code = failures.classify(getattr(exc, "kind", None), exc)
    return f"{code}: {failures.CANNED[code]}"


def check_ticker(ticker: str, source: ChainSource, policy: Policy, now_utc: datetime, *,
                 session: SessionInfo | None = None, sleep: Callable[[float], None] = time.sleep) -> TickerReport:
    rep = TickerReport(ticker=ticker, cap_ticker_usd=policy.portfolio.max_underlying_exposure_usd,
                       cap_pilot_usd=policy.put_collateral_cap_usd)
    if normalize_symbol(ticker) != ticker:
        rep.ticker = safe_text(ticker, 24) or "(empty)"
        rep.status, rep.verdict = "INVALID", VERDICT_DATA
        rep.unavailable = ("not a valid ticker symbol format (letters, digits, '.' or '-', max 10); "
                           "the input was not sent to the provider")
        return rep
    e = policy.entries
    raw, err = _fetch(source, ticker, policy, sleep)
    if raw is None:
        rep.status, rep.unavailable = "UNAVAILABLE", _explain(err) if err else f"{failures.UNKNOWN_ERROR}: {failures.CANNED[failures.UNKNOWN_ERROR]}"
        return rep
    rep.retrieved_at_utc = raw.fetched_at_utc
    rep.chain_exists = bool(raw.rows)
    if not raw.rows:
        _expiry_context(rep, source, ticker, policy, now_utc)
        if rep.expirations_known and not rep.in_window_expiry_listed:
            rep.status, rep.verdict = "OK", VERDICT_NO_LIQUID
            rep.verdict_detail = _no_expiry_text(rep, policy)
            return rep
        rep.status = "UNAVAILABLE"
        rep.unavailable = ("no put rows returned for the fetch window. Two possible meanings, cannot tell which: "
                           "(a) provider outage/throttling/bad symbol, or (b) no listed expiry inside "
                           f"{policy.entries.min_dte_calendar}-{policy.entries.max_dte_calendar} DTE (monthly-only "
                           "products often have none). Nothing was estimated; retry later or check the broker")
        rep.verdict_detail = rep.unavailable
        return rep

    try:
        dates = source.fetch_earnings_dates(ticker)
    except Exception:  # noqa: BLE001
        dates = None
    today = exchange_date(now_utc)
    future = sorted(d for d in (dates or []) if d >= today)
    if future:
        rep.next_earnings = future[0]
        rep.earnings_note = "per provider (Yahoo via Stockpile); verify yourself before approving"
    norm = normalize_rows(ticker, raw.rows, raw.fetched_at_utc, today_exchange=today, earnings_dates=dates,
                          multiplier=100, rows_seen_upstream=raw.rows_seen_upstream)
    if norm.degraded or not norm.rows:
        rep.status = "UNAVAILABLE"
        rep.unavailable = "chain is mostly zeroed/unusable (provider throttled or no quote data); retry later"
        return rep

    spots = {o.observation.spot for o in norm.rows if o.observation.spot is not None and o.observation.spot > 0}
    if len(spots) == 1:
        rep.spot = next(iter(spots))
        rep.spot_note = ("Yahoo last/regular-market price (via Stockpile); no exchange "
                         f"timestamp, retrieved {raw.fetched_at_utc:%Y-%m-%d %H:%M:%S}Z")
    elif len(spots) > 1:
        rep.notes.append("provider returned inconsistent underlying prices across rows; spot shown as UNAVAILABLE")
        rep.spot_note = "UNAVAILABLE: inconsistent values across rows"
        rep.status = "UNAVAILABLE"
        rep.unavailable = "provider rows disagree on the underlying price; no single price can be shown (retry later)"
        return rep
    else:
        rep.spot_note = "UNAVAILABLE: provider rows carried no valid underlying price"
    if norm.rejected:
        rep.notes.append("rows flagged by normalization: " + ", ".join(f"{k}={v}" for k, v in sorted(norm.rejected.items())))

    session = session if session is not None else mcal.session_info(now_utc)
    empty = BookState()
    exp_counts: dict[date, int] = {}
    for row in norm.rows:
        o = row.observation
        dte = (o.expiration - today).days
        if not (e.min_dte_calendar <= dte <= e.max_dte_calendar):
            rep.rows_outside_window += 1
            continue
        exp_counts[o.expiration] = exp_counts.get(o.expiration, 0) + 1
        d = evaluate_entry(o, None, empty, policy, now_utc, session)
        codes = tuple(r.value for r in d.reason_codes if r not in CONTEXT_CODES)
        spread = None if o.bid is None or o.ask is None else o.ask - o.bid
        mid = None if o.bid is None or o.ask is None else (o.bid + o.ask) / 2
        in_band = (o.delta is not None and o.delta < 0 and e.min_abs_delta <= abs(o.delta) <= e.max_abs_delta)
        rep.puts.append(PutLine(
            o.expiration, dte, o.strike, o.bid, o.ask, o.delta, o.open_interest, o.daily_volume, spread,
            None if spread is None or not mid else spread / mid, o.strike * 100, o.last_option_trade_at_utc,
            codes, not codes, in_band))
    rep.expirations = sorted((x, (x - today).days, n) for x, n in exp_counts.items())
    rep.puts.sort(key=lambda p: (p.expiration, p.strike))
    if not rep.puts:
        _expiry_context(rep, source, ticker, policy, now_utc)
        rep.verdict = VERDICT_NO_LIQUID
        rep.verdict_detail = _no_expiry_text(rep, policy) + f" ({rep.rows_outside_window} put rows fetched just outside it)"
        return rep

    rep.lowest_strike = min(rep.puts, key=lambda p: (p.strike, p.expiration))
    band = [p for p in rep.puts if p.in_band]
    rep.in_band_count = len(band)
    if band:
        rep.band_nearest, rep.band_nearest_is_in_band = min(band, key=lambda p: (p.collateral, p.expiration)), True
    else:
        have = [p for p in rep.puts if p.delta is not None and p.delta < 0]
        if have:
            def dist(p: PutLine) -> Decimal:
                a = abs(p.delta)
                return e.min_abs_delta - a if a < e.min_abs_delta else a - e.max_abs_delta
            rep.band_nearest = min(have, key=lambda p: (dist(p), p.expiration, p.strike))
    rep.passing_count = sum(1 for p in rep.puts if p.passes)

    cap = min(rep.cap_ticker_usd, rep.cap_pilot_usd)
    fits = [p for p in rep.puts if p.passes and p.collateral <= cap]
    if rep.passing_count == 0:
        rep.verdict = VERDICT_NO_LIQUID
        rep.verdict_detail = "no put in the window passes the contract gates (see reason codes per contract)"
    elif not fits:
        rep.verdict = VERDICT_CAPS
        rep.verdict_detail = (f"{rep.passing_count} put(s) pass the contract gates but every one needs collateral "
                              f"above the lower cap (${cap:,.2f})")
    else:
        rep.verdict = VERDICT_PASS
        rep.verdict_detail = (f"{len(fits)} put(s) pass the contract gates and fit both caps. Owner approval, "
                              "free cash and the other portfolio limits are NOT evaluated here"
                              + ("" if rep.next_earnings else f"; {EARNINGS_FLAG}"))
    return rep


def _expiry_context(rep: TickerReport, source: ChainSource, ticker: str, policy: Policy, now_utc: datetime) -> None:
    """Indicative nearest listed expirations around the window, if the adapter can list them (optional, read-only)."""
    fn = getattr(source, "fetch_expirations", None)
    if fn is None:
        return
    try:
        exps = fn(ticker)
    except Exception:  # noqa: BLE001
        exps = None
    if not exps:
        return
    today = exchange_date(now_utc)
    e = policy.entries
    dtes = [(x, (x - today).days) for x in exps]
    rep.expirations_known = True
    rep.in_window_expiry_listed = any(e.min_dte_calendar <= d <= e.max_dte_calendar for _, d in dtes)
    before = [t for t in dtes if 0 <= t[1] < e.min_dte_calendar]
    after = [t for t in dtes if t[1] > e.max_dte_calendar]
    rep.nearest_before = max(before, key=lambda t: t[1]) if before else None
    rep.nearest_after = min(after, key=lambda t: t[1]) if after else None


def _no_expiry_text(rep: TickerReport, policy: Policy) -> str:
    e = policy.entries
    t = f"no listed expiry between {e.min_dte_calendar} and {e.max_dte_calendar} DTE today"
    if rep.expirations_known:
        b = f"{rep.nearest_before[0]} (DTE {rep.nearest_before[1]})" if rep.nearest_before else "none"
        a = f"{rep.nearest_after[0]} (DTE {rep.nearest_after[1]})" if rep.nearest_after else "none within 120 DTE"
        return f"{t}: nearest listed expiries are before {b} and after {a} (indicative; the entry window is not widened)"
    return (t + " among the rows fetched (the expiry list itself could not be read, so this may also be a provider "
            "gap; check your broker)")


# ---------------------------------------------------------------- rendering

def _m(v: Decimal | None) -> str:
    return "UNAVAILABLE" if v is None else f"{v:,.2f}"


def _d(v: Decimal | None) -> str:
    return "UNAVAILABLE" if v is None else f"{v:.3f}"


def _n(v: int | None) -> str:
    return "UNAVAILABLE" if v is None else str(v)


def _times(t: datetime | None, tz_name: str) -> str:
    if t is None:
        return "UNAVAILABLE"
    return f"{t.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC / {t.astimezone(ZoneInfo(tz_name)):%Y-%m-%d %H:%M:%S %Z}"


def _focus(rep: TickerReport, lo: Decimal, hi: Decimal) -> list[PutLine]:
    """Rows shown by default: per expiration the in-band puts plus the nearest put just below and just above the
    band (by |delta|), then the lowest listed strike and the band-nearest put."""
    shown: set[tuple[date, Decimal]] = set()
    by_exp: dict[date, list[PutLine]] = {}
    for p in rep.puts:
        by_exp.setdefault(p.expiration, []).append(p)
    for rows in by_exp.values():
        dl = [p for p in rows if p.delta is not None and p.delta < 0]
        below = [p for p in dl if abs(p.delta) < lo]
        above = [p for p in dl if abs(p.delta) > hi]
        pick = [p for p in dl if p.in_band]
        pick += sorted(below, key=lambda p: abs(p.delta))[-1:] + sorted(above, key=lambda p: abs(p.delta))[:1]
        shown.update((p.expiration, p.strike) for p in pick)
    for p in (rep.lowest_strike, rep.band_nearest):
        if p:
            shown.add((p.expiration, p.strike))
    return [p for p in rep.puts if (p.expiration, p.strike) in shown]


def render_text(reports: list[TickerReport], policy: Policy, now_utc: datetime, tz_name: str, *,
                all_strikes: bool = False, session_note: str = "") -> str:
    e = policy.entries
    L: list[str] = ["=" * 78, BANNER, "=" * 78,
                    f"Policy {policy.policy_id} v{policy.policy_version}: DTE {e.min_dte_calendar}-{e.max_dte_calendar}, "
                    f"|delta| {e.min_abs_delta}-{e.max_abs_delta}, OI>={e.min_open_interest}, vol>={e.min_daily_volume}, "
                    f"bid>={e.min_bid_usd_per_share}, spread<={e.max_absolute_spread_usd_per_share} and "
                    f"<={e.max_fractional_spread_of_mid * 100:.0f}% of mid",
                    ASSUMPTION, f"Not evaluated here: {NOT_EVALUATED}."]
    if session_note:
        L.append(session_note)
    for r in reports:
        L += ["", "-" * 78, f"{safe_text(r.ticker, 24)}"]
        if r.status != "OK":
            L += [f"  Provider: {r.provider}    Data tier: {r.data_tier}"]
            if r.retrieved_at_utc:
                L.append(f"  Retrieved: {_times(r.retrieved_at_utc, tz_name)}")
            if r.chain_exists is False:
                L.append("  Option chain: none returned for the DTE window (does not prove the symbol has no options at "
                         "other expirations)")
            L += [f"  STATUS: {r.status}: {safe_text(r.unavailable)}", f"  VERDICT: {VERDICT_DATA}"]
            continue
        L += [f"  Provider: {r.provider}    Data tier: {r.data_tier}",
              f"  Retrieved: {_times(r.retrieved_at_utc, tz_name)}",
              f"  Underlying last price: {_m(r.spot)}  (source: {r.spot_note})",
              f"  Asset type: {r.asset_type}",
              f"  Leveraged/inverse: {r.leverage_note}",
              f"  Next earnings: {r.next_earnings.isoformat()}  [{r.earnings_note}]" if r.next_earnings
              else f"  Next earnings: {r.earnings_note}",
              f"  Option chain exists: {'yes' if r.chain_exists else 'no'}"]
        if r.expirations:
            L.append("  Expirations in window: " + ", ".join(f"{x.isoformat()} (DTE {d}, {n} puts)" for x, d, n in r.expirations))
        else:
            L.append("  Expirations in window: none")
        for n in r.notes:
            L.append(f"  note: {safe_text(n)}")
        if r.puts:
            rows = r.puts if all_strikes else _focus(r, e.min_abs_delta, e.max_abs_delta)
            L.append(f"  Puts ({'all' if all_strikes else 'focus rows: in-band, neighbours, lowest strike; use --all-strikes'}; "
                     f"{len(r.puts)} in window):")
            L.append("    expiry     DTE strike    bid    ask  delta   OI  vol  spread  sprd%  collateral  gates")
            for p in rows:
                sp = "UNAVAIL" if p.spread is None else f"{p.spread:.2f}"
                spp = "UNAVAIL" if p.spread_pct_of_mid is None else f"{p.spread_pct_of_mid * 100:.1f}%"
                L.append(f"    {p.expiration.isoformat()} {p.dte:>3} {p.strike:>6.2f} {_m(p.bid):>6} {_m(p.ask):>6} "
                         f"{_d(p.delta):>6} {_n(p.open_interest):>4} {_n(p.volume):>4} {sp:>7} {spp:>6} "
                         f"{p.collateral:>10,.2f}  "
                         + ("PASS" if p.passes else "FAIL " + ",".join(p.gate_reasons))
                         + ("  [in band]" if p.in_band else ""))
                if p.last_trade_at_utc is None:
                    L.append("             last option trade: UNAVAILABLE")
        L.append(f"  Summary: {len(r.puts)} puts in window, {r.in_band_count} in the delta band, "
                 f"{r.passing_count} pass the contract gates.")
        if r.lowest_strike:
            ls = r.lowest_strike
            L.append(f"  Lowest listed strike {ls.strike:.2f} ({ls.expiration}): collateral ${ls.collateral:,.2f} "
                     f"{_vs(ls.collateral, r)}")
        if r.band_nearest:
            b = r.band_nearest
            kind = "in-band, lowest collateral" if r.band_nearest_is_in_band else "NO strike in band; closest to band"
            L.append(f"  Band-nearest strike {b.strike:.2f} ({b.expiration}, delta {_d(b.delta)}, {kind}): "
                     f"collateral ${b.collateral:,.2f} {_vs(b.collateral, r)}")
        else:
            L.append("  Band-nearest strike: UNAVAILABLE (no put with a usable delta)")
        if not r.puts and r.expirations_known:
            bf = r.nearest_before
            af = r.nearest_after
            L.append("  Nearest listed expiries outside the window (indicative): "
                     f"before {bf[0].isoformat() + ' (DTE %d)' % bf[1] if bf else 'none'}; "
                     f"after {af[0].isoformat() + ' (DTE %d)' % af[1] if af else 'none'}")
        L += [f"  VERDICT: {r.verdict}", f"    {r.verdict_detail}"]
    L += ["", "=" * 78, BANNER]
    return "\n".join(L)


def _vs(collateral: Decimal, r: TickerReport) -> str:
    t = "within" if collateral <= r.cap_ticker_usd else "OVER"
    p = "within" if collateral <= r.cap_pilot_usd else "OVER"
    return f"[{t} per-ticker cap ${r.cap_ticker_usd:,.0f}; {p} pilot cap ${r.cap_pilot_usd:,.0f}]"


def to_json_obj(reports: list[TickerReport], now_utc: datetime) -> dict:
    def s(v):
        if v is None:
            return None
        if isinstance(v, Decimal):
            return format(v, "f")
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v

    def put(p: PutLine) -> dict:
        return {"expiration": s(p.expiration), "dte": p.dte, "strike": s(p.strike), "bid": s(p.bid), "ask": s(p.ask),
                "delta": s(p.delta), "open_interest": p.open_interest, "volume": p.volume, "spread": s(p.spread),
                "spread_pct_of_mid": None if p.spread_pct_of_mid is None else format(p.spread_pct_of_mid * 100, "f"),
                "collateral_usd": s(p.collateral), "last_option_trade_at_utc": s(p.last_trade_at_utc),
                "gate_reasons": list(p.gate_reasons), "passes_contract_gates": p.passes, "in_delta_band": p.in_band}

    return {
        "banner": BANNER, "assumption": ASSUMPTION, "not_evaluated": NOT_EVALUATED, "generated_at_utc": s(now_utc),
        "tickers": [{
            "ticker": safe_text(r.ticker, 24), "status": r.status, "unavailable_reason": safe_text(r.unavailable) or None,
            "provider": r.provider, "data_tier": r.data_tier, "retrieved_at_utc": s(r.retrieved_at_utc),
            "underlying_last_price": s(r.spot), "underlying_price_source": r.spot_note,
            "asset_type": r.asset_type, "leveraged_inverse": r.leverage_note,
            "next_earnings": s(r.next_earnings), "earnings_note": r.earnings_note, "chain_exists": r.chain_exists,
            "expirations_in_window": [{"expiration": s(x), "dte": d, "puts": n} for x, d, n in r.expirations],
            "puts": [put(p) for p in r.puts],
            "lowest_strike": None if r.lowest_strike is None else put(r.lowest_strike),
            "band_nearest": None if r.band_nearest is None else put(r.band_nearest),
            "band_nearest_in_band": r.band_nearest_is_in_band,
            "cap_per_ticker_usd": s(r.cap_ticker_usd), "cap_pilot_usd": s(r.cap_pilot_usd),
            "verdict": r.verdict, "verdict_detail": r.verdict_detail, "notes": [safe_text(n) for n in r.notes],
            "nearest_expiry_before_window": None if r.nearest_before is None else {
                "expiration": s(r.nearest_before[0]), "dte": r.nearest_before[1]},
            "nearest_expiry_after_window": None if r.nearest_after is None else {
                "expiration": s(r.nearest_after[0]), "dte": r.nearest_after[1]},
        } for r in reports]}
