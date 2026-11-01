"""P0-01: .env.example per spec 9.2, DRY_RUN default, broker-access startup validation."""
from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

from csp_alerts.settings import StartupError, load_settings, settings_from_mapping

ROOT = Path(__file__).resolve().parents[2]

SPEC_KEYS = {
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CSP_ENV", "CSP_DRY_RUN", "CSP_TZ",
    "CSP_DATABASE_URL", "CSP_STOCKPILE_ROOT", "CSP_DATA_PROVIDER",
    "CSP_DISABLE_BROKER_ACCESS", "CSP_AI_ENABLED", "CSP_LOG_LEVEL",
}
# Documented additions outside spec 9.2: the local read-only dashboard (placeholders only, no real value).
DASHBOARD_KEYS = {"CSP_DASHBOARD_PASSWORD_HASH", "CSP_DASHBOARD_PORT"}


def test_env_example_matches_spec_and_has_no_secrets():
    vals = dotenv_values(ROOT / ".env.example")
    assert set(vals) == SPEC_KEYS | DASHBOARD_KEYS
    assert vals["CSP_DASHBOARD_PASSWORD_HASH"] == "REPLACE_LOCALLY"
    assert vals["TELEGRAM_BOT_TOKEN"] == "REPLACE_LOCALLY"
    assert vals["TELEGRAM_CHAT_ID"] == "REPLACE_LOCALLY"
    assert vals["CSP_DRY_RUN"] == "true"
    assert vals["CSP_DISABLE_BROKER_ACCESS"] == "true"
    assert vals["CSP_AI_ENABLED"] == "false"


def test_env_example_loads_and_placeholders_are_not_secrets():
    s = load_settings(ROOT / ".env.example", environ={})
    assert s.dry_run is True
    assert not s.telegram_configured


def test_dry_run_defaults_true_with_empty_env():
    s = settings_from_mapping({})
    assert s.dry_run is True and s.ai_enabled is False and s.disable_broker_access is True


@pytest.mark.parametrize("val", ["false", "False", "0", "no"])
def test_broker_access_false_is_startup_error(val):
    with pytest.raises(StartupError) as e:
        settings_from_mapping({"CSP_DISABLE_BROKER_ACCESS": val})
    assert "CSP_DISABLE_BROKER_ACCESS must be true" in str(e.value)


def test_broker_access_garbage_is_error():
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_DISABLE_BROKER_ACCESS": "maybe"})


def test_dry_run_can_be_disabled_explicitly_and_garbage_rejected():
    assert settings_from_mapping({"CSP_DRY_RUN": "false"}).dry_run is False
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_DRY_RUN": "sometimes"})


def test_broker_credential_env_vars_rejected():
    with pytest.raises(StartupError) as e:
        settings_from_mapping({"SCHWAB_APP_KEY": "supersecretvalue"})
    assert "SCHWAB_APP_KEY" in str(e.value)
    assert "supersecretvalue" not in str(e.value)


def test_broker_data_provider_rejected():
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_DATA_PROVIDER": "schwab"})


def test_secrets_never_in_repr_or_errors():
    s = settings_from_mapping({"TELEGRAM_BOT_TOKEN": "123456:SECRETTOKEN", "TELEGRAM_CHAT_ID": "987654321"})
    assert s.telegram_configured
    assert "SECRETTOKEN" not in repr(s) and "987654321" not in repr(s)
    assert "SECRETTOKEN" not in str(s.telegram_bot_token)
    assert s.telegram_bot_token.reveal() == "123456:SECRETTOKEN"
    with pytest.raises(StartupError) as e:
        settings_from_mapping({"TELEGRAM_BOT_TOKEN": "123456:SECRETTOKEN", "CSP_DISABLE_BROKER_ACCESS": "false"})
    assert "SECRETTOKEN" not in str(e.value)


def test_database_url_parsing():
    s = settings_from_mapping({"CSP_DATABASE_URL": "sqlite:///./data/x.sqlite3"})
    assert s.database_path == Path("./data/x.sqlite3")
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_DATABASE_URL": "postgres://u:p@h/db"})


def test_invalid_timezone_and_env():
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_TZ": "Mars/Base"})
    with pytest.raises(StartupError):
        settings_from_mapping({"CSP_ENV": "prod"})


@pytest.mark.parametrize("name", ["ETRADE_KEY", "FUTU_PWD", "WEBULL_X", "TOS_USER", "TDA_ID", "IBKR_ACCT", "IB_GATEWAY",
                                  "MOOMOO_OPEND", "ALPACA_KEY", "TRADIER_T", "TASTYTRADE_U", "ROBINHOOD_P", "SCHWAB_APP"])
def test_broker_prefixes_rejected(name):
    with pytest.raises(StartupError):
        settings_from_mapping({name: "x"})
    settings_from_mapping({name: ""})  # empty value is not a credential


def test_ordinary_windows_env_not_rejected_and_credential_names_only_warn():
    from csp_alerts.settings import env_warnings
    env = {"PATH": "x", "USERPROFILE": "c:/u", "APPDATA": "a", "PYTHONPATH": "p", "VIRTUAL_ENV": "v", "ProgramFiles": "pf",
           "OPENAI_API_KEY": "k", "MY_SECRET": "s", "CSP_API_THING": "ok", "TELEGRAM_BOT_TOKEN": "t",
           "AWS_ACCOUNT_ID": "1", "EMPTY_TOKEN": ""}
    settings_from_mapping(env)  # no error
    assert env_warnings(env) == ["AWS_ACCOUNT_ID", "MY_SECRET", "OPENAI_API_KEY"]
    assert "SCHWAB_KEY" not in env_warnings({"SCHWAB_KEY": "x"})  # broker prefix is an error instead


def test_example_whitelist_guard_is_content_based(tmp_path):
    import json

    from csp_alerts.policy.approved import load_approved
    from csp_alerts.policy.config import PolicyError
    rec = {"ticker": "ABC", "asset_type": "ETF", "sector": "s", "correlation_bucket": "b", "max_buy_strike": "50",
           "approved_through": "2026-12-31", "events_reviewed_on": "2026-10-30", "events_cleared_through": "2026-12-31",
           "ownership_reason": "real diligence", "approved_by": "owner"}
    f = tmp_path / "approved.json"  # innocuous filename
    for bad in ({**rec, "synthetic": True}, {**rec, "ticker": "EXAMPLE"}, {**rec, "ownership_reason": "Fictional example only"}):
        f.write_text(json.dumps({"approved_underlyings": [bad]}))
        with pytest.raises(PolicyError):
            load_approved(f)
    f.write_text(json.dumps({"synthetic": True, "approved_underlyings": []}))
    with pytest.raises(PolicyError):
        load_approved(f)
    f.write_text(json.dumps({"approved_underlyings": [rec]}))
    assert len(load_approved(f)) == 1
