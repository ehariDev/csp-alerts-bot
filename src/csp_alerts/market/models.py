"""Canonical market observation (spec 6.1). Values are optional: absent means UNKNOWN, never inferred.

Nothing in this module fetches data. A later slice's adapter builds these from raw Stockpile rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

TIER_R = "INDICATIVE_UNTIMESTAMPED"
TIER_V = "VERIFIED_TIMESTAMPED"


class SessionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SessionInfo:
    """Exchange-session facts supplied by the (future) calendar module."""
    status: SessionStatus
    minutes_to_close: int | None = None


@dataclass(frozen=True)
class MarketObservation:
    underlying: str
    expiration: date | None
    strike: Decimal | None
    multiplier: int | None
    option_right: str | None = "P"
    spot: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    delta: Decimal | None = None
    delta_source: str = "STOCKPILE_BLACK_SCHOLES"
    implied_volatility: Decimal | None = None
    open_interest: int | None = None
    daily_volume: int | None = None
    last_option_trade_at_utc: datetime | None = None
    quote_at_utc: datetime | None = None  # None unless the source supplies a trustworthy quote timestamp
    retrieved_at_utc: datetime | None = None
    provider: str = "YAHOO_VIA_STOCKPILE"
    data_tier: str = TIER_R
    earnings_in_window: bool | None = None  # None = unknown (not "no earnings")
    iv_excess_pp: Decimal | None = None

    @property
    def contract_key(self) -> str:
        exp = self.expiration.isoformat() if self.expiration else "?"
        k = f"{self.strike:.2f}" if self.strike is not None and self.strike.is_finite() else "?"
        return f"{self.underlying}|{exp}|{self.option_right or '?'}|{k}|{self.multiplier if self.multiplier else '?'}"

    def to_dict(self) -> dict[str, Any]:
        """Spec 6.1 canonical JSON (Decimals as strings; unknown stays null, never inferred)."""
        def d(v: Decimal | None) -> str | None:
            return None if v is None else format(v, "f") if v.is_finite() else str(v)

        def t(v: datetime | None) -> str | None:
            return None if v is None else v.strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "contract_key": self.contract_key, "underlying": self.underlying, "option_right": self.option_right,
            "expiration": None if self.expiration is None else self.expiration.isoformat(),
            "strike": d(self.strike), "multiplier": self.multiplier, "spot": d(self.spot), "bid": d(self.bid),
            "ask": d(self.ask), "delta": d(self.delta), "delta_source": self.delta_source,
            "implied_volatility": d(self.implied_volatility), "open_interest": self.open_interest,
            "daily_volume": self.daily_volume, "last_option_trade_at_utc": t(self.last_option_trade_at_utc),
            "quote_at_utc": t(self.quote_at_utc), "retrieved_at_utc": t(self.retrieved_at_utc),
            "provider": self.provider, "data_tier": self.data_tier,
            "event_check": {True: "EARNINGS_IN_WINDOW", False: "NO_EARNINGS_BEFORE_EXPIRATION_PER_PROVIDER",
                            None: "UNKNOWN"}[self.earnings_in_window],
            "iv_excess_pp": d(self.iv_excess_pp),
        }

    @staticmethod
    def from_dict(x: dict[str, Any]) -> "MarketObservation":
        """Strict parse of a spec 6.1 JSON object (used by alert-preview fixtures)."""
        def dec_(v: Any) -> Decimal | None:
            return None if v is None else Decimal(str(v))

        def ts(v: Any) -> datetime | None:
            if v is None:
                return None
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

        ec = x.get("event_check", "UNKNOWN")
        return MarketObservation(
            underlying=x["underlying"], expiration=date.fromisoformat(x["expiration"]), strike=dec_(x.get("strike")),
            multiplier=x.get("multiplier"), option_right=x.get("option_right"), spot=dec_(x.get("spot")),
            bid=dec_(x.get("bid")), ask=dec_(x.get("ask")), delta=dec_(x.get("delta")),
            delta_source=x.get("delta_source", "STOCKPILE_BLACK_SCHOLES"),
            implied_volatility=dec_(x.get("implied_volatility")), open_interest=x.get("open_interest"),
            daily_volume=x.get("daily_volume"), last_option_trade_at_utc=ts(x.get("last_option_trade_at_utc")),
            quote_at_utc=ts(x.get("quote_at_utc")), retrieved_at_utc=ts(x.get("retrieved_at_utc")),
            provider=x.get("provider", "YAHOO_VIA_STOCKPILE"), data_tier=x.get("data_tier", TIER_R),
            earnings_in_window=True if ec == "EARNINGS_IN_WINDOW" else False if ec.startswith("NO_EARNINGS") else None,
            iv_excess_pp=dec_(x.get("iv_excess_pp")),
        )
