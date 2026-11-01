"""P0-02: typed policy config, schema validation, empty whitelist."""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from csp_alerts.policy.approved import load_approved
from csp_alerts.policy.config import PolicyError, load_policy, parse_policy, policy_json_schema

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "config" / "policy.yaml"


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def test_shipped_policy_loads_with_spec_values():
    p = load_policy(POLICY)
    assert p.policy_version == "1.1.0"
    assert p.entries.min_dte_calendar == 35 and p.entries.max_dte_calendar == 45
    assert p.entries.min_abs_delta == Decimal("0.10") and p.entries.max_abs_delta == Decimal("0.15")
    assert p.portfolio.max_underlying_exposure_usd == Decimal("9000.00")
    assert p.portfolio.normal_new_entry_exposure_ceiling_usd == Decimal("25000.00")
    assert p.pilot.max_deployed_put_collateral_usd == Decimal("15000.00")
    assert p.exits.profit_captured_fraction == Decimal("0.60")
    assert p.put_collateral_cap_usd == Decimal("15000.00")
    assert all(isinstance(getattr(p.entries, f), (Decimal, int, bool, str))
               for f in type(p.entries).model_fields)


def test_policy_hash_stable_and_changes_with_content(raw):
    a = parse_policy(raw)
    assert a.sha256 == load_policy(POLICY).sha256
    raw2 = copy.deepcopy(raw)
    raw2["entries"]["min_open_interest"] = 400
    assert parse_policy(raw2).sha256 != a.sha256


def test_pilot_disabled_uses_normal_ceiling(raw):
    raw["pilot"]["enabled"] = False
    assert parse_policy(raw).put_collateral_cap_usd == Decimal("25000.00")


def test_unknown_top_level_and_nested_keys_rejected(raw):
    r = copy.deepcopy(raw)
    r["surprise"] = 1
    with pytest.raises(PolicyError, match="surprise"):
        parse_policy(r)
    r = copy.deepcopy(raw)
    r["entries"]["min_open_intrest"] = 5
    with pytest.raises(PolicyError, match="min_open_intrest"):
        parse_policy(r)


def test_missing_key_rejected(raw):
    del raw["drawdown"]["hard_halt_at"]
    with pytest.raises(PolicyError, match="hard_halt_at"):
        parse_policy(raw)


def test_yaml_floats_rejected_for_decimals(raw):
    raw["entries"]["min_abs_delta"] = 0.1
    with pytest.raises(PolicyError, match="min_abs_delta"):
        parse_policy(raw)


@pytest.mark.parametrize("path,value", [
    (("portfolio", "allow_margin"), True),
    (("portfolio", "allow_naked_options"), True),
    (("broker_connections",), "ENABLED"),
    (("trade_execution",), "AUTO"),
    (("ai", "can_override_risk_engine"), True),
    (("ai", "can_send_orders"), True),
    (("ai", "can_approve_new_underlyings"), True),
    (("alerts", "no_inbound_trade_commands"), False),
    (("alerts", "default_dry_run"), False),
    (("entries", "require_strike_below_spot"), False),
    (("entries", "block_earnings_through_expiration"), False),
    (("drawdown", "hard_halt_requires_manual_reset"), False),
    (("exits", "use_buyback_ask_not_mid"), False),
    (("option_right",), "CALL"),
    (("approved_by",), "ai"),
])
def test_hard_constraints_cannot_be_loosened(raw, path, value):
    d = raw
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value
    with pytest.raises(PolicyError):
        parse_policy(raw)


@pytest.mark.parametrize("mutate", [
    lambda r: r["entries"].update(min_dte_calendar=50),
    lambda r: r["entries"].update(min_abs_delta="0.20"),
    lambda r: r["pilot"].update(max_deployed_put_collateral_usd="26000.00"),
    lambda r: r["portfolio"].update(emergency_absolute_exposure_ceiling_usd="24000.00"),
    lambda r: r["portfolio"].update(max_underlying_exposure_usd="11000.00"),
    lambda r: r["drawdown"].update(pause_new_entries_at="0.06"),
    lambda r: r["drawdown"].update(hard_halt_at="1.5"),
    lambda r: r["entries"].update(min_bid_usd_per_share="-1"),
    lambda r: r["entries"].update(min_open_interest=-1),
    lambda r: r["entries"].update(min_open_interest=True),
    lambda r: r["entries"].update(min_open_interest="500"),
    lambda r: r["portfolio"].update(max_active_short_put_contracts=0),
    lambda r: r["market"].update(scheduler_timezone="Mars/Base"),
    lambda r: r["market"].update(entry_scans_local=["25:99"]),
    lambda r: r.update(policy_version="v1"),
    lambda r: r.update(account_reference_value_usd="NaN"),
    lambda r: r["entries"].update(min_abs_delta="not-a-number"),
])
def test_invalid_values_rejected(raw, mutate):
    mutate(raw)
    with pytest.raises(PolicyError):
        parse_policy(raw)


def test_non_mapping_and_bad_yaml_rejected(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("- a\n- b\n")
    with pytest.raises(PolicyError):
        load_policy(f)
    f.write_text("a: [unclosed\n")
    with pytest.raises(PolicyError):
        load_policy(f)
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "missing.yaml")


def test_checked_in_json_schema_is_current():
    schema = json.loads((ROOT / "config" / "policy.schema.json").read_text(encoding="utf-8"))
    assert schema == policy_json_schema()


def test_shipped_whitelist_is_empty():
    assert load_approved(ROOT / "config" / "approved.json") == []


def test_example_whitelist_is_synthetic_and_not_loadable_as_live():
    data = json.loads((ROOT / "config" / "approved.example.json").read_text(encoding="utf-8"))
    assert [a["ticker"] for a in data["approved_underlyings"]] == ["EXAMPLE"]
    with pytest.raises(PolicyError):
        load_approved(ROOT / "config" / "approved.example.json")


def test_approved_record_validation(tmp_path):
    good = {
        "ticker": "ABC", "asset_type": "ETF", "sector": "s", "correlation_bucket": "b",
        "max_buy_strike": "50.00", "approved_through": "2026-12-31",
        "events_reviewed_on": "2026-10-30", "events_cleared_through": "2026-12-31",
        "ownership_reason": "r", "approved_by": "owner",
    }
    f = tmp_path / "approved.json"

    def write(rec):
        f.write_text(json.dumps({"approved_underlyings": [rec]}))

    write(good)
    got = load_approved(f)
    assert got[0].max_buy_strike == Decimal("50.00")

    for bad in (
        {**good, "max_buy_strike": 50.0},
        {**good, "max_buy_strike": "0"},
        {**good, "ticker": "abc"},
        {**good, "asset_type": "OPTION"},
        {**good, "approved_by": "ai"},
        {**good, "approved_through": "12/31/2026"},
        {**good, "extra": 1},
    ):
        write(bad)
        with pytest.raises(PolicyError):
            load_approved(f)

    f.write_text(json.dumps({"approved_underlyings": [good, good]}))
    with pytest.raises(PolicyError, match="duplicate"):
        load_approved(f)
