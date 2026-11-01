"""Owner-approved underlying whitelist records (spec 4.3 / 6.2).

The whitelist ships EMPTY (config/approved.json). Synthetic examples live only in
config/approved.example.json, which the loader refuses to treat as a live whitelist.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

from csp_alerts.policy.config import PolicyError, _strict_decimal


class ApprovedUnderlying(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")
    asset_type: Literal["STOCK", "ETF"]
    sector: str = Field(min_length=1)
    correlation_bucket: str = Field(min_length=1)
    max_buy_strike: Annotated[Decimal, BeforeValidator(_strict_decimal), Field(gt=0)]
    approved_through: date
    events_reviewed_on: date
    events_cleared_through: date
    ownership_reason: str = Field(min_length=1)
    approved_by: Literal["owner"]
    # Owner attests the option deliverable is standard 100 shares (Yahoo data has no multiplier). Required for entries.
    standard_deliverable_confirmed_on: date | None = None
    synthetic: bool = False  # True marks example/fixture data; the live loader rejects it

    @field_validator("sector", "correlation_bucket", mode="after")
    @classmethod
    def _norm_labels(cls, v: str) -> str:
        v = v.strip().casefold()
        if not v:
            raise ValueError("label must not be blank")
        return v

    @field_validator("approved_through", "events_reviewed_on", "events_cleared_through", "standard_deliverable_confirmed_on", mode="before")
    @classmethod
    def _iso_dates_only(cls, v: Any) -> Any:
        if v is None or isinstance(v, date):
            return v
        if isinstance(v, str):
            return date.fromisoformat(v)  # strict YYYY-MM-DD
        raise ValueError("date must be an ISO YYYY-MM-DD string")


_SYNTHETIC_MARKERS = ("synthetic", "fictional", "example only", "placeholder")


def looks_synthetic(a: "ApprovedUnderlying") -> bool:
    """Content-based check (not filename-based): explicit flag, the EXAMPLE ticker, or marker words."""
    reason = a.ownership_reason.lower()
    return a.synthetic or a.ticker == "EXAMPLE" or any(m in reason for m in _SYNTHETIC_MARKERS)


class ApprovedList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    synthetic: bool = False
    approved_underlyings: list[ApprovedUnderlying]

    @field_validator("approved_underlyings")
    @classmethod
    def _unique(cls, v: list[ApprovedUnderlying]) -> list[ApprovedUnderlying]:
        tickers = [a.ticker for a in v]
        if len(set(tickers)) != len(tickers):
            raise ValueError("duplicate ticker in approved list")
        return v


def load_approved(path: Path | str) -> list[ApprovedUnderlying]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        parsed = ApprovedList.model_validate(data)
        if parsed.synthetic:
            raise PolicyError("approved list is flagged synthetic/example and cannot be the live whitelist")
        bad = [a.ticker for a in parsed.approved_underlyings if looks_synthetic(a)]
        if bad:
            raise PolicyError(f"synthetic/example entries cannot be live approvals: {bad}")
        return list(parsed.approved_underlyings)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read approved list {p}: {exc}") from exc
    except PolicyError:
        raise
    except (ValidationError, ValueError) as exc:
        raise PolicyError(f"invalid approved list {p}: {exc}") from exc


ASSET_UNKNOWN = "UNKNOWN"
ASSET_UNKNOWN_LABEL = "UNKNOWN (not in approvals)"
SRC_JOURNAL = "approval record (synced into the journal)"
SRC_FILE = "approvals file (not yet synced into the journal; read-only)"
SRC_NONE = "not in approvals (counted as an individual company)"


def asset_type_map(approved: "list[ApprovedUnderlying] | None") -> dict[str, str]:
    """ticker -> asset type from an approvals list (None, unreadable, gives an empty map: nothing is assumed)."""
    return {a.ticker: a.asset_type for a in (approved or [])}


def resolve_asset_type(ticker: str, journal_types: "dict[str, str] | None", file_types: "dict[str, str] | None") -> tuple[str, str]:
    """The ONE asset-type resolution used by the book, previews, portfolio, capacity display and reconcile.
    Order: the journal's approved_underlyings table, then the approvals file (read-only), else ASSET_UNKNOWN (never a silent
    STOCK). Returns (asset_type, source). Only ETF is exempt from the company-name cap; STOCK and UNKNOWN both count."""
    t = (journal_types or {}).get(ticker)
    if t:
        return t, SRC_JOURNAL
    t = (file_types or {}).get(ticker)
    if t:
        return t, SRC_FILE
    return ASSET_UNKNOWN, SRC_NONE


def asset_type_label(asset_type: str) -> str:
    return ASSET_UNKNOWN_LABEL if asset_type == ASSET_UNKNOWN else asset_type
