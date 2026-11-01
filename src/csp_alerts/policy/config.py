"""Typed policy configuration (spec section 10). Strict: unknown keys rejected, Decimal only.

Hard constraints (advisory-only, no margin, no broker, AI cannot override) are encoded as
Literal types so a config edit cannot loosen them. Budget numbers may be edited manually
but must stay internally consistent, and every change yields a new sha256 for the
`policy_versions` table.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from csp_alerts.money import MoneyError, dec


class PolicyError(ValueError):
    """Invalid or unsafe policy file."""


def _strict_decimal(v: Any) -> Decimal:
    # YAML floats (e.g. 0.10) are rejected: write decimals as quoted strings.
    try:
        return dec(v)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc


Money = Annotated[Decimal, BeforeValidator(_strict_decimal)]
PosMoney = Annotated[Decimal, BeforeValidator(_strict_decimal), Field(gt=0)]
Frac = Annotated[Decimal, BeforeValidator(_strict_decimal), Field(ge=0, le=1)]
NonNegInt = Annotated[int, Field(ge=0, strict=True)]
PosInt = Annotated[int, Field(gt=0, strict=True)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Pilot(_Strict):
    enabled: bool
    duration_calendar_days: PosInt
    max_deployed_put_collateral_usd: PosMoney
    require_manual_reconciliation_daily: Literal[True]


class Portfolio(_Strict):
    normal_min_deployed_collateral_usd: Money
    normal_new_entry_exposure_ceiling_usd: PosMoney
    emergency_absolute_exposure_ceiling_usd: PosMoney
    desired_uncommitted_cash_usd: Money
    max_underlying_exposure_usd: PosMoney
    max_sector_exposure_usd: PosMoney
    max_correlated_bucket_usd: PosMoney
    max_active_short_put_contracts: PosInt
    max_contracts_per_underlying: PosInt
    max_individual_company_names: PosInt
    max_expiry_week_collateral_usd: PosMoney
    allow_margin: Literal[False]
    allow_naked_options: Literal[False]
    treat_working_and_pending_as_committed: Literal[True]
    block_on_unknown_cash_or_positions: Literal[True]


class Entries(_Strict):
    one_contract_per_alert: bool
    min_dte_calendar: NonNegInt
    max_dte_calendar: NonNegInt
    min_abs_delta: Frac
    max_abs_delta: Frac
    require_negative_put_delta: Literal[True]
    require_strike_below_spot: Literal[True]
    require_strike_at_or_below_owner_buy_limit: Literal[True]
    min_open_interest: NonNegInt
    min_daily_volume: NonNegInt
    min_bid_usd_per_share: PosMoney
    max_absolute_spread_usd_per_share: Money
    max_fractional_spread_of_mid: Frac
    max_last_trade_age_calendar_days: PosMoney
    require_positive_bid_and_ask: Literal[True]
    require_standard_100_share_deliverable: Literal[True]
    block_unverified_events: Literal[True]
    block_earnings_through_expiration: Literal[True]
    manual_event_review_max_age_calendar_days: NonNegInt
    max_alternative_alerts_per_scan: PosInt
    # Label kept from the spec text; the implemented ordering is liquidity first (see jobs/scan.py docstring).
    rank_basis: Literal["bid_premium_per_collateral_after_all_gates"]

    @model_validator(mode="after")
    def _ranges(self) -> "Entries":
        if self.min_dte_calendar > self.max_dte_calendar:
            raise ValueError("min_dte_calendar > max_dte_calendar")
        if self.min_abs_delta > self.max_abs_delta:
            raise ValueError("min_abs_delta > max_abs_delta")
        return self


class ManualData(_Strict):
    max_account_snapshot_age_hours: PosInt
    max_stock_mark_age_hours: PosInt
    require_reconciliation_after_every_trade: Literal[True]
    require_daily_end_of_day_reconciliation: Literal[True]
    fail_closed_for_entries: Literal[True]


class Exits(_Strict):
    profit_captured_fraction: Frac
    close_review_at_dte_calendar: NonNegInt
    loss_buyback_ask_multiple: PosMoney
    delta_review_abs: Frac
    assignment_warning_dte: NonNegInt
    use_buyback_ask_not_mid: Literal[True]
    stale_quote_suppresses_price_based_alerts: Literal[True]
    calendar_alerts_even_when_market_closed: Literal[True]
    # IT-005 (owner decision, policy 1.1.0): Tier R (untimestamped Yahoo) price-review alerts; time/assignment always on.
    tier_r_price_alerts_enabled: bool = True


class Drawdown(_Strict):
    pause_new_entries_at: Frac
    reduce_exposure_review_at: Frac
    hard_halt_at: Frac
    hard_halt_requires_manual_reset: Literal[True]

    @model_validator(mode="after")
    def _order(self) -> "Drawdown":
        if not (0 < self.pause_new_entries_at < self.reduce_exposure_review_at < self.hard_halt_at):
            raise ValueError("drawdown thresholds must satisfy 0 < pause < reduce < halt")
        return self


class Market(_Strict):
    provider: Literal["yahoo_via_stockpile"]
    initial_quote_tier: Literal["INDICATIVE_UNTIMESTAMPED"]
    option_market: str
    session_calendar: Literal["XNYS"]
    calendar_timezone: str
    scheduler_timezone: str
    entry_scans_local: list[Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]]
    exit_monitor_interval_minutes: PosInt
    no_entry_in_final_minutes_of_session: NonNegInt
    provider_retries: NonNegInt
    quote_ttl_seconds_if_timestamped: PosInt
    disable_cached_last_price_as_quote: Literal[True]

    @model_validator(mode="after")
    def _tz(self) -> "Market":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        for tz in (self.calendar_timezone, self.scheduler_timezone):
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown IANA timezone {tz!r}") from exc
        return self


class Alerts(_Strict):
    telegram_private_chat_only: Literal[True]
    transport: Literal["sendMessage"]
    default_dry_run: Literal[True]
    no_inbound_trade_commands: Literal[True]
    dedup_entry_by_trading_date: bool
    dedup_exit_by_trade_trigger_date: bool
    critical_repeat_daily_until_resolved: bool
    no_trade_digest_once_per_session: bool
    entry_alert_expiry_minutes: PosInt = 30  # IT-003 (policy 1.1.0)


class AIPolicy(_Strict):
    enabled: bool
    mode: Literal["SOURCE_GROUNDED_VETO_ONLY"]
    on_error: Literal["MORE_RESEARCH"]
    can_override_risk_engine: Literal[False]
    can_approve_new_underlyings: Literal[False]
    can_send_orders: Literal[False]


class Policy(_Strict):
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    approved_by: Literal["owner"]
    asset_scope: list[Literal["STOCK", "ETF"]] = Field(min_length=1)
    option_right: Literal["PUT"]
    trade_execution: Literal["MANUAL_ONLY"]
    broker_connections: Literal["DISABLED"]
    account_scope: str = Field(min_length=1)
    account_reference_value_usd: PosMoney
    pilot: Pilot
    portfolio: Portfolio
    entries: Entries
    manual_data: ManualData
    exits: Exits
    drawdown: Drawdown
    market: Market
    alerts: Alerts
    ai: AIPolicy

    @model_validator(mode="after")
    def _consistency(self) -> "Policy":
        p = self.portfolio
        if not (self.pilot.max_deployed_put_collateral_usd <= p.normal_new_entry_exposure_ceiling_usd
                <= p.emergency_absolute_exposure_ceiling_usd):
            raise ValueError("require pilot cap <= normal ceiling <= emergency ceiling")
        if p.normal_min_deployed_collateral_usd > p.normal_new_entry_exposure_ceiling_usd:
            raise ValueError("normal_min_deployed_collateral_usd exceeds normal ceiling")
        if p.max_underlying_exposure_usd > p.max_sector_exposure_usd:
            raise ValueError("per-underlying cap exceeds sector cap")
        if p.max_contracts_per_underlying > p.max_active_short_put_contracts:
            raise ValueError("max_contracts_per_underlying exceeds max_active_short_put_contracts")
        return self

    # --- helpers ---
    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def put_collateral_cap_usd(self) -> Decimal:
        """Cap that applies to short-put collateral: pilot cap while pilot is enabled, else normal ceiling."""
        if self.pilot.enabled:
            return self.pilot.max_deployed_put_collateral_usd
        return self.portfolio.normal_new_entry_exposure_ceiling_usd


def parse_policy(data: Any) -> Policy:
    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        lines = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise PolicyError("invalid policy:\n  " + "\n  ".join(lines)) from exc


def load_policy(path: Path | str) -> Policy:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot read policy file {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy file must be a YAML mapping")
    return parse_policy(data)


def policy_json_schema() -> dict[str, Any]:
    return Policy.model_json_schema()
