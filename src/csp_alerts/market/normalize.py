"""Normalize raw Stockpile chain rows into canonical MarketObservations (spec 3.2, 6.1, 13.1).

Principles
  * Unknown stays unknown: NaN/None/non-numeric -> None. Never infer a missing value.
  * Upstream `mid` and `last` are IGNORED (upstream falls back mid=last when there is no two-sided quote).
    Bid and ask are taken independently; 0 stays 0 and is rejected by the entry gate (BAD_BID_ASK).
    Upstream also turns missing volume/OI into 0, so 0 is treated as "unverified", which also blocks.
  * `type` is 'put'/'call' upstream; only puts are kept. Calls are counted and dropped.
  * DTE is NOT taken from upstream (host-date based); the entry gate recomputes it from `expiration`
    in America/New_York.
  * `last_trade_ms` (epoch ms of the option's last trade print) -> last_option_trade_at_utc. It is never a
    quote timestamp: quote_at_utc stays None and data_tier stays INDICATIVE_UNTIMESTAMPED.
  * IT-004 (MVP): if a contract symbol is present it must be a plain OCC symbol (no adjusted-root digit);
    otherwise multiplier=None -> NONSTANDARD_CONTRACT. Stockpile rows normally carry NO symbol, so the owner's
    `standard_deliverable_confirmed_on` on the approval is then the only evidence (documented gap).
  * Multiplier is unknown for the Yahoo path; the caller supplies 100 only when the owner-approved record
    confirms a standard deliverable, otherwise None (blocks entry).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from csp_alerts.market.models import TIER_R, MarketObservation

PROVIDER = "YAHOO_VIA_STOCKPILE"
DEGRADED_ZERO_QUOTE_FRACTION = Decimal("0.85")  # spec 13.1: "85% zeroed quotes"


def to_decimal(v: Any) -> Decimal | None:
    """Exact Decimal from the printed (shortest round-trip) form of a number; None if missing/NaN/inf."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def to_count(v: Any) -> int | None:
    d = to_decimal(v)
    if d is None or d != d.to_integral_value() or d < 0:
        return None
    return int(d)


def raw_sha256(row: dict[str, Any]) -> str:
    """Provenance hash of the raw upstream row (stable key order; non-JSON types via str)."""
    blob = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _last_trade(ms: Any) -> datetime | None:
    d = to_decimal(ms)
    if d is None or d <= 0:
        return None
    return datetime.fromtimestamp(int(d) / 1000, tz=timezone.utc)


def earnings_in_window(dates: list[date] | None, expiration: date, today: date) -> bool | None:
    """None = UNKNOWN (empty/failed fetch is never 'clear'). Upstream returns only the nearest future date."""
    if not dates:
        return None
    return any(today <= d <= expiration for d in dates)


_OCC_TAIL = re.compile(r"^(?P<root>.+?)(?P<date>\d{6})(?P<cp>[PC])(?P<strike>\d{8})$")


def standard_symbol(symbol: str | None, ticker: str, expiration: date | None = None,
                    strike: Decimal | None = None) -> bool | None:
    """IT-004 minimal rule. None = no symbol available (cannot disprove; owner confirmation is then the only
    evidence). False = malformed, or the OCC root carries an adjusted-root digit suffix (e.g. 'AAPL1') or
    differs from the ticker. True = plain OCC symbol whose root equals the ticker."""
    if symbol is None or not str(symbol).strip():
        return None
    m = _OCC_TAIL.match(str(symbol).strip().upper())
    if not m:
        return False
    root = m.group("root")
    if not (root.isalpha() and root == re.sub(r"[.\-/]", "", ticker.upper())) or m.group("cp") != "P":
        return False
    d = m.group("date")
    try:
        sym_exp = date(2000 + int(d[:2]), int(d[2:4]), int(d[4:6]))
    except ValueError:
        return False
    if expiration is not None and sym_exp != expiration:
        return False  # symbol contradicts the row's expiration
    if strike is not None and Decimal(int(m.group("strike"))) / 1000 != strike:
        return False  # symbol contradicts the row's strike
    return True


@dataclass
class NormalizedRow:
    observation_id: str
    observation: MarketObservation
    raw_sha256: str
    raw: dict[str, Any]


@dataclass
class NormalizedChain:
    ticker: str
    rows: list[NormalizedRow] = field(default_factory=list)
    rows_in: int = 0
    rows_seen_upstream: int | None = None  # pre-filter count is not exposed by Stockpile
    calls_ignored: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    degraded: bool = False

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def normalize_rows(
    ticker: str,
    raw_rows: list[dict[str, Any]],
    retrieved_at_utc: datetime,
    *,
    today_exchange: date,
    earnings_dates: list[date] | None,
    multiplier: int | None,
    rows_seen_upstream: int | None = None,
    new_id=lambda: str(uuid.uuid4()),
) -> NormalizedChain:
    out = NormalizedChain(ticker=ticker, rows_in=len(raw_rows), rows_seen_upstream=rows_seen_upstream)
    zeroed = 0
    for raw in raw_rows:
        side = str(raw.get("type", "")).strip().lower()
        if side == "call":
            out.calls_ignored += 1
            continue
        if side != "put":
            out.reject("UNKNOWN_OPTION_TYPE")
            continue
        strike = to_decimal(raw.get("strike"))
        if strike is None or strike <= 0:
            out.reject("BAD_STRIKE")
            continue
        try:
            expiration = date.fromisoformat(str(raw.get("expiration"))[:10])
        except ValueError:
            out.reject("BAD_EXPIRATION")
            continue
        sym_ok = standard_symbol(raw.get("contractSymbol") or raw.get("contract_symbol"), ticker, expiration, strike)
        if sym_ok is False:
            out.reject("NONSTANDARD_SYMBOL")  # kept (gate blocks via multiplier=None) but counted
        bid, ask = to_decimal(raw.get("bid")), to_decimal(raw.get("ask"))
        if (bid is None or bid <= 0) and (ask is None or ask <= 0):
            zeroed += 1
        obs = MarketObservation(
            underlying=ticker, expiration=expiration, strike=strike, multiplier=None if sym_ok is False else multiplier, option_right="P",
            spot=to_decimal(raw.get("spot")), bid=bid, ask=ask, delta=to_decimal(raw.get("delta")),
            implied_volatility=to_decimal(raw.get("iv")), open_interest=to_count(raw.get("open_interest")),
            daily_volume=to_count(raw.get("volume")), last_option_trade_at_utc=_last_trade(raw.get("last_trade_ms")),
            quote_at_utc=None, retrieved_at_utc=retrieved_at_utc, provider=PROVIDER, data_tier=TIER_R,
            earnings_in_window=earnings_in_window(earnings_dates, expiration, today_exchange),
            iv_excess_pp=None,  # puts-only fetch: no high-confidence surface fit (spec 3.1); never invented
        )
        out.rows.append(NormalizedRow(new_id(), obs, raw_sha256(raw), raw))
    kept = len(out.rows)
    # Unusable quotes at ANY chain size: >= 85% of rows with no bid and no ask, or every bid missing/zero.
    # (A tiny chain of zeroed quotes must be DATA_UNAVAILABLE, never a "healthy" no-trade with BAD_BID_ASK counts.)
    no_bid = sum(1 for r in out.rows if r.observation.bid is None or r.observation.bid <= 0)
    if kept >= 1 and (Decimal(zeroed) / Decimal(kept) >= DEGRADED_ZERO_QUOTE_FRACTION or no_bid == kept):
        out.degraded = True
    return out
