"""Acceptance: startup validation (spec 9.2), policy config (spec 10), whitelist (spec 0.5, 4.3), no-broker boundary
(spec 3.1, 20), secret hygiene (spec 9.1, 13.2). No network. Synthetic tokens only."""
from __future__ import annotations

import ast
import copy
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from acc_helpers import POLICY_PATH, REPO

from csp_alerts.policy.approved import load_approved
from csp_alerts.policy.config import PolicyError, load_policy, parse_policy, policy_json_schema
from csp_alerts.settings import Secret, StartupError, settings_from_mapping

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs"
FAKE_CHAT = "987654321"
GOOD = {"CSP_DISABLE_BROKER_ACCESS": "true", "TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT}


# ---------------------------------------------------------------- settings
def test_defaults_are_safe():
    s = settings_from_mapping({})
    assert s.dry_run is True and s.disable_broker_access is True and s.ai_enabled is False and s.env == "paper"
    assert s.telegram_configured is False


def test_env_example_parses_and_placeholders_mean_unconfigured():
    from dotenv import dotenv_values
    vals = {k: v for k, v in dotenv_values(REPO / ".env.example").items() if v is not None}
    assert vals["CSP_DISABLE_BROKER_ACCESS"] == "true" and vals["CSP_DRY_RUN"] == "true" and vals["CSP_AI_ENABLED"] == "false"
    s = settings_from_mapping(vals)
    assert not s.telegram_configured


@pytest.mark.parametrize("v", ["false", "FALSE", "False", "0", "no", " false ", "off", "disabled", "garbage", "2", "n"])
def test_broker_flag_false_or_unknown_is_startup_error(v):
    with pytest.raises(StartupError) as ei:
        settings_from_mapping({"CSP_DISABLE_BROKER_ACCESS": v})
    assert "CSP_DISABLE_BROKER_ACCESS" in str(ei.value)


@pytest.mark.parametrize("v", [None, "", "true", "TRUE", "1", "yes", " true "])
def test_broker_flag_true_forms_accepted(v):
    env = {} if v is None else {"CSP_DISABLE_BROKER_ACCESS": v}
    assert settings_from_mapping(env).disable_broker_access is True


@pytest.mark.parametrize("name", [
    "SCHWAB_APP_KEY", "schwab_app_secret", "MOOMOO_PWD", "IBKR_USER", "IB_GATEWAY_PORT", "ALPACA_API_KEY",
    "TRADIER_TOKEN", "TASTYTRADE_PASSWORD", "ROBINHOOD_USER",
])
def test_broker_credential_env_vars_are_startup_errors(name):
    with pytest.raises(StartupError) as ei:
        settings_from_mapping({name: "x"})
    assert "broker" in str(ei.value).lower()


def test_multiple_problems_listed_together():
    with pytest.raises(StartupError) as ei:
        settings_from_mapping({"CSP_DISABLE_BROKER_ACCESS": "false", "CSP_ENV": "live", "CSP_DATA_PROVIDER": "schwab",
                               "CSP_LOG_LEVEL": "LOUD", "CSP_TZ": "Mars/Base", "CSP_DATABASE_URL": "postgres://x"})
    assert len(ei.value.problems) >= 5


@pytest.mark.parametrize("k,v", [("CSP_DATA_PROVIDER", "schwab"), ("CSP_DATA_PROVIDER", "moomoo"),
                                 ("CSP_ENV", "live"), ("CSP_ENV", "prod"), ("CSP_DRY_RUN", "maybe"),
                                 ("CSP_AI_ENABLED", "perhaps")])
def test_invalid_values_rejected(k, v):
    with pytest.raises(StartupError):
        settings_from_mapping({k: v})


def test_ai_default_disabled_and_dry_run_default_true():
    assert settings_from_mapping({"CSP_DRY_RUN": ""}).dry_run is True


def test_secret_never_in_repr_str_format_or_errors():
    s = settings_from_mapping(GOOD)
    assert s.telegram_configured
    for blob in (repr(s), str(s), repr(s.telegram_bot_token), str(s.telegram_chat_id), f"{s.telegram_bot_token}",
                 format(s.telegram_bot_token, ""), "%s" % s.telegram_bot_token, repr([s]), repr({"k": s})):
        assert FAKE_TOKEN not in blob and FAKE_CHAT not in blob
    assert s.telegram_bot_token.reveal() == FAKE_TOKEN
    with pytest.raises(StartupError) as ei:
        settings_from_mapping({**GOOD, "CSP_DISABLE_BROKER_ACCESS": "false", "CSP_ENV": FAKE_TOKEN})
    assert FAKE_TOKEN not in str(ei.value) and FAKE_TOKEN not in repr(ei.value) and FAKE_CHAT not in str(ei.value)
    assert not any(FAKE_TOKEN in p for p in ei.value.problems)


def test_secret_not_serializable_by_accident():
    import pickle
    s = Secret(FAKE_TOKEN)
    assert not hasattr(s, "__dict__")
    with pytest.raises(Exception):
        json.dumps(s)
    del pickle


def test_bad_values_with_secret_substrings_not_echoed():
    # a token pasted into the wrong variable must not be echoed back in the error message
    with pytest.raises(StartupError) as ei:
        settings_from_mapping({"CSP_DATA_PROVIDER": FAKE_TOKEN, "CSP_LOG_LEVEL": FAKE_TOKEN, "CSP_TZ": FAKE_TOKEN,
                               "CSP_DATABASE_URL": FAKE_TOKEN, "CSP_DRY_RUN": FAKE_TOKEN})
    assert FAKE_TOKEN not in str(ei.value)


# ---------------------------------------------------------------- policy
def raw():
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def test_shipped_policy_matches_spec_10_values():
    p = load_policy(POLICY_PATH)
    assert p.pilot.enabled and str(p.pilot.max_deployed_put_collateral_usd) == "15000.00"
    pf = p.portfolio
    assert (pf.normal_new_entry_exposure_ceiling_usd, pf.emergency_absolute_exposure_ceiling_usd,
            pf.max_underlying_exposure_usd, pf.max_sector_exposure_usd, pf.max_expiry_week_collateral_usd) == tuple(
        __import__("decimal").Decimal(x) for x in ("25000", "30000", "9000", "10000", "15000"))
    assert (pf.max_active_short_put_contracts, pf.max_contracts_per_underlying, pf.max_individual_company_names) == (4, 1, 2)
    e = p.entries
    assert (e.min_dte_calendar, e.max_dte_calendar, e.min_open_interest, e.min_daily_volume) == (35, 45, 500, 50)
    assert (str(e.min_abs_delta), str(e.max_abs_delta), str(e.min_bid_usd_per_share),
            str(e.max_absolute_spread_usd_per_share), str(e.max_fractional_spread_of_mid)) == ("0.10", "0.15", "0.20", "0.05", "0.10")
    assert (str(p.exits.profit_captured_fraction), str(p.exits.loss_buyback_ask_multiple), str(p.exits.delta_review_abs),
            p.exits.close_review_at_dte_calendar, p.exits.assignment_warning_dte) == ("0.60", "2.00", "0.30", 21, 7)
    assert (str(p.drawdown.pause_new_entries_at), str(p.drawdown.reduce_exposure_review_at), str(p.drawdown.hard_halt_at)) == ("0.03", "0.05", "0.08")
    assert p.ai.enabled is False and p.alerts.default_dry_run is True


def test_shipped_policy_equals_spec_yaml_block():
    """Spec section 10 YAML block parsed independently must equal config/policy.yaml semantically."""
    spec = (REPO / "requirements" / "PUT_SELLER_TELEGRAM_SPEC.md").read_text(encoding="utf-8")
    block = spec.split("```yaml", 1)[1].split("```", 1)[0]
    spec_cfg, ours = yaml.safe_load(block), raw()
    # every spec value is unchanged, except the policy revision and exactly the two documented MVP additions
    # (IT-003 entry-alert expiry, IT-005 Tier R price-alert switch).
    added = {("alerts", "entry_alert_expiry_minutes"), ("exits", "tier_r_price_alerts_enabled")}
    for sec, val in spec_cfg.items():
        if isinstance(val, dict):
            extra = {k for k in ours[sec] if k not in val}
            assert extra <= {k for s_, k in added if s_ == sec}, (sec, extra)
            assert {k: v for k, v in ours[sec].items() if k in val} == val, sec
        elif sec != "policy_version":
            assert ours[sec] == val, sec


def test_json_schema_file_in_sync_with_model():
    on_disk = json.loads((REPO / "config" / "policy.schema.json").read_text(encoding="utf-8"))
    assert on_disk == policy_json_schema()


def test_policy_hash_is_stable_and_changes_with_any_edit():
    a, b = load_policy(POLICY_PATH), load_policy(POLICY_PATH)
    assert a.sha256 == b.sha256 and re.fullmatch(r"[0-9a-f]{64}", a.sha256)
    d = raw(); d["entries"]["min_open_interest"] = 501
    assert parse_policy(d).sha256 != a.sha256


def mutate(path, value):
    d = copy.deepcopy(raw())
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[k]
    if value is KeyError:
        del cur[keys[-1]]
    else:
        cur[keys[-1]] = value
    return d


@pytest.mark.parametrize("path,value", [
    # hard safety flags cannot be loosened
    ("portfolio.allow_margin", True), ("portfolio.allow_naked_options", True),
    ("portfolio.treat_working_and_pending_as_committed", False), ("portfolio.block_on_unknown_cash_or_positions", False),
    ("broker_connections", "ENABLED"), ("trade_execution", "AUTO"), ("option_right", "CALL"), ("approved_by", "ai"),
    ("alerts.default_dry_run", False), ("alerts.no_inbound_trade_commands", False),
    ("alerts.telegram_private_chat_only", False),
    ("ai.can_override_risk_engine", True), ("ai.can_approve_new_underlyings", True), ("ai.can_send_orders", True),
    ("exits.use_buyback_ask_not_mid", False), ("exits.calendar_alerts_even_when_market_closed", False),
    ("exits.stale_quote_suppresses_price_based_alerts", False),
    ("entries.require_positive_bid_and_ask", False), ("entries.block_unverified_events", False),
    ("entries.block_earnings_through_expiration", False), ("entries.require_standard_100_share_deliverable", False),
    ("entries.require_strike_below_spot", False), ("entries.require_negative_put_delta", False),
    ("entries.require_strike_at_or_below_owner_buy_limit", False),
    ("drawdown.hard_halt_requires_manual_reset", False), ("manual_data.fail_closed_for_entries", False),
    ("market.disable_cached_last_price_as_quote", False), ("pilot.require_manual_reconciliation_daily", False),
    ("market.provider", "schwab"), ("market.session_calendar", "24x7"),
    # floats where Decimal strings are required
    ("entries.min_abs_delta", 0.1), ("portfolio.max_underlying_exposure_usd", 9000.0), ("exits.profit_captured_fraction", 0.6),
    ("pilot.max_deployed_put_collateral_usd", 15000.5),
    # nonsense / non-finite
    ("entries.min_abs_delta", "NaN"), ("portfolio.max_underlying_exposure_usd", "Infinity"),
    ("portfolio.max_underlying_exposure_usd", "-1"), ("portfolio.max_underlying_exposure_usd", "0"),
    ("entries.max_abs_delta", "1.5"), ("entries.max_abs_delta", "0.05"), ("entries.min_dte_calendar", 50),
    ("entries.min_dte_calendar", -1), ("portfolio.max_active_short_put_contracts", 0),
    ("portfolio.max_active_short_put_contracts", True), ("portfolio.max_active_short_put_contracts", 4.0),
    ("portfolio.max_individual_company_names", "2"),
    ("drawdown.pause_new_entries_at", "0.06"), ("drawdown.hard_halt_at", "0.04"), ("drawdown.pause_new_entries_at", "0"),
    ("pilot.max_deployed_put_collateral_usd", "26000.00"),     # pilot > normal
    ("portfolio.normal_new_entry_exposure_ceiling_usd", "31000.00"),  # normal > emergency
    ("portfolio.max_underlying_exposure_usd", "11000.00"),     # ticker > sector
    ("portfolio.max_contracts_per_underlying", 5),
    ("policy_version", "1.0"), ("policy_id", ""), ("market.scheduler_timezone", "Mars/Base"),
    ("market.entry_scans_local", ["25:99"]),
    ("entries.rank_basis", "iv_excess"),
    # missing keys
    ("entries.min_open_interest", KeyError), ("drawdown", KeyError), ("pilot.enabled", KeyError),
])
def test_invalid_or_unsafe_policy_rejected(path, value):
    with pytest.raises(PolicyError):
        parse_policy(mutate(path, value))


def test_unknown_keys_rejected_at_every_level():
    for path in ("", "entries.", "portfolio.", "exits.", "ai.", "alerts.", "market.", "pilot.", "drawdown.", "manual_data."):
        d = raw()
        cur = d
        for k in [x for x in path.split(".") if x]:
            cur = cur[k]
        cur["surprise_flag"] = True
        with pytest.raises(PolicyError):
            parse_policy(d)


def test_policy_file_errors_are_policy_errors(tmp_path):
    for name, content in (("a.yaml", "- a\n- b\n"), ("b.yaml", "{unclosed"), ("c.yaml", ""), ("d.yaml", "just text")):
        f = tmp_path / name
        f.write_text(content, encoding="utf-8")
        with pytest.raises(PolicyError):
            load_policy(f)
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "missing.yaml")


def test_unquoted_integer_money_is_exact_not_float():
    d = raw(); d["portfolio"]["max_underlying_exposure_usd"] = 9000
    assert parse_policy(d).portfolio.max_underlying_exposure_usd == 9000


def test_policy_has_no_float_fields():
    p = load_policy(POLICY_PATH)

    def walk(o):
        if isinstance(o, float):
            raise AssertionError("float in policy")
        if isinstance(o, dict):
            [walk(v) for v in o.values()]
        elif isinstance(o, (list, tuple)):
            [walk(v) for v in o]
    walk(p.model_dump())


# ---------------------------------------------------------------- whitelist
def test_shipped_whitelist_is_empty():
    assert json.loads((REPO / "config" / "approved.json").read_text(encoding="utf-8")) == {"approved_underlyings": []}
    assert load_approved(REPO / "config" / "approved.json") == []


def test_example_file_cannot_be_loaded_as_live(tmp_path):
    with pytest.raises(PolicyError):
        load_approved(REPO / "config" / "approved.example.json")
    with pytest.raises(PolicyError):
        load_approved(tmp_path / "approved.example.json")


def test_example_content_is_synthetic_marked():
    d = json.loads((REPO / "config" / "approved.example.json").read_text(encoding="utf-8"))
    assert [a["ticker"] for a in d["approved_underlyings"]] == ["EXAMPLE"]
    assert "ictional" in d["approved_underlyings"][0]["ownership_reason"]


@pytest.mark.parametrize("mut", [
    {"max_buy_strike": 75.0}, {"max_buy_strike": "0"}, {"max_buy_strike": "-5"}, {"max_buy_strike": "NaN"},
    {"ticker": "lower"}, {"ticker": ""}, {"asset_type": "CRYPTO"}, {"approved_by": "ai"}, {"approved_by": "agent"},
    {"approved_through": "12/31/2026"}, {"approved_through": 20261231}, {"events_reviewed_on": "2026-13-01"},
    {"sector": ""}, {"correlation_bucket": ""}, {"ownership_reason": ""}, {"surprise": 1},
])
def test_whitelist_entry_validation(tmp_path, mut):
    rec = json.loads((REPO / "config" / "approved.example.json").read_text(encoding="utf-8"))["approved_underlyings"][0]
    # strip the synthetic markers first so the rejection is caused by `mut`, not by the content-based example guard
    rec.pop("synthetic", None)   # example records carry a per-record synthetic flag
    rec.update({"ticker": "ABCD", "ownership_reason": "long-term owner thesis"})
    f = tmp_path / "approved.json"
    f.write_text(json.dumps({"approved_underlyings": [rec]}), encoding="utf-8")
    assert [x.ticker for x in load_approved(f)] == ["ABCD"]          # positive control: clean record loads
    rec.update(mut)
    f.write_text(json.dumps({"approved_underlyings": [rec]}), encoding="utf-8")
    with pytest.raises(PolicyError):
        load_approved(f)


def test_whitelist_duplicate_ticker_and_missing_field(tmp_path):
    rec = json.loads((REPO / "config" / "approved.example.json").read_text(encoding="utf-8"))["approved_underlyings"][0]
    rec.pop("synthetic", None)   # example records carry a per-record synthetic flag
    rec.update({"ticker": "ABCD", "ownership_reason": "long-term owner thesis"})
    f = tmp_path / "approved.json"
    f.write_text(json.dumps({"approved_underlyings": [rec, rec]}), encoding="utf-8")
    with pytest.raises(PolicyError):
        load_approved(f)
    rec2 = dict(rec); del rec2["events_cleared_through"]
    f.write_text(json.dumps({"approved_underlyings": [rec2]}), encoding="utf-8")
    with pytest.raises(PolicyError):
        load_approved(f)
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_approved(f)


# ---------------------------------------------------------------- no-broker boundary / advisory-only guarantee
SRC = REPO / "src" / "csp_alerts"
ORDER_NAME = re.compile(
    r"(place|submit|cancel|modify|replace|amend|send|create|execute|route|transmit)_?(an_?)?(option_?)?(order|trade)s?"
    r"|buy_to_(close|open)|sell_to_(open|close)|broker_sync|live_trade|roll_position|auto_trade|place_trade", re.I)
BROKER_IMPORT = re.compile(r"^(schwab|moomoo|futu|streamlit|ib_insync|ibapi|ib_async|alpaca|tda|robin_stocks|tastytrade|tradier|"
                           r"options_scanner|stocks_shared|stockpile|requests|httpx|urllib3?|aiohttp|telegram|websocket)")


def py_files():
    return sorted(SRC.rglob("*.py"))


def test_no_order_placement_identifiers_anywhere_in_src():
    hits = []
    for f in py_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            names = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(n.name)
            elif isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
            elif isinstance(n, ast.arg):
                names.append(n.arg)
            elif isinstance(n, ast.keyword) and n.arg:
                names.append(n.arg)
            for nm in names:
                if ORDER_NAME.search(nm) and nm not in ("FORBIDDEN_CALLABLE_NAMES", "can_send_orders"):  # can_send_orders: Literal[False] policy flag
                    hits.append((f.name, nm))
    assert hits == [], hits


def test_no_order_strings_or_endpoints_in_src_text():
    bad = re.compile(r"/orders|/trade|placeOrder|place_order|order[_ ]ticket|api\.schwabapi|api\.tdameritrade|"
                     r"interactivebrokers|alpaca\.markets|tradier\.com|webull", re.I)
    for f in py_files():
        if f.name == "boundary.py":
            continue  # the denylist itself
        text = f.read_text(encoding="utf-8")
        assert not bad.search(text), f.name


# Slice 2: the ONLY file allowed to touch the pinned upstream, and only these two modules, only lazily (inside a
# function body, so merely importing csp_alerts never loads Stockpile).
STOCKPILE_ALLOWED = {"options_scanner.chain", "options_scanner.earnings"}
# Slice 3: the ONLY file allowed to open a network connection, via urllib, and only to the Telegram Bot API.
TELEGRAM_ALLOWED = {"urllib.request", "urllib.error"}


def test_no_broker_or_network_imports_in_src():
    offenders = []
    stockpile_imports: list[tuple[str, bool]] = []   # (module, at_module_level)
    telegram_imports: list[str] = []
    for f in py_files():
        if f.name == "boundary.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        top_level = {id(n) for n in tree.body}
        # collect nodes that are lexically inside a function (lazy imports)
        in_func = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(fn):
                    in_func.add(id(sub))
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods = [n.module]
            elif isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("__import__", "import_module"):
                offenders.append((f.name, "dynamic import"))
            elif isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "import_module":
                offenders.append((f.name, "dynamic import"))
            for m in mods:
                if f.name == "stockpile.py" and f.parent.name == "market" and m in STOCKPILE_ALLOWED:
                    stockpile_imports.append((m, id(n) not in in_func))
                    continue
                if f.name == "telegram.py" and f.parent.name == "alerts" and m in TELEGRAM_ALLOWED:
                    telegram_imports.append(m)       # the ONLY network client in the project
                    continue
                if BROKER_IMPORT.match(m) or m in ("socket", "ssl", "http.client", "ftplib", "smtplib", "subprocess", "os.system"):
                    offenders.append((f.name, m))
    assert offenders == [], offenders
    # exactly the two permitted upstream modules, both imported lazily
    assert {m for m, _ in stockpile_imports} == STOCKPILE_ALLOWED, stockpile_imports
    assert not any(top for _, top in stockpile_imports), "upstream imported at module level"
    assert set(telegram_imports) == TELEGRAM_ALLOWED, telegram_imports      # urllib used by alerts/telegram.py and nowhere else
    # no other URL literal anywhere in src: the only endpoint is https://api.telegram.org (configurable base is a ctor arg)
    urls = []
    for f in py_files():
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.search(r"https?://", n.value):
                urls.append((f.name, n.value))
    assert sorted(urls) == sorted([("telegram.py", "https://api.telegram.org"), ("telegram.py", "https://")] + [
        u for u in urls if u[0] == "telegram.py" and u[1].startswith("http://127.0.0.1")]), urls


def test_only_stockpile_adapter_touches_sys_path_or_environ_credentials():
    for f in py_files():
        text = f.read_text(encoding="utf-8")
        if "sys.path" in text:
            assert f.name == "stockpile.py" and f.parent.name == "market", f
            assert "sys.path.insert" not in text and "sys.path[0" not in text, "adapter must append, never prepend"


def test_no_eval_exec_or_shell_in_src():
    for f in py_files():
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Call):
                fn = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                assert fn not in ("eval", "exec", "system", "popen", "Popen", "run", "check_output"), (f.name, fn)


def test_dependencies_have_no_broker_or_streamlit():
    import tomllib
    meta = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(meta["project"]["dependencies"] + [d for g in meta.get("dependency-groups", {}).values() for d in g
                                                          if isinstance(d, str)]).lower()
    for bad in ("schwab", "moomoo", "futu", "streamlit", "ib-insync", "ibapi", "alpaca", "tda", "robin", "tastytrade",
                "tradier", "stockpile", "options-scanner", "requests", "httpx"):
        assert bad not in deps, bad


def test_importing_everything_loads_no_forbidden_module_and_opens_no_socket():
    code = r"""
import socket, sys, importlib, pkgutil
def boom(*a, **k): raise RuntimeError("NETWORK ATTEMPT")
socket.socket.connect = boom; socket.create_connection = boom; socket.getaddrinfo = boom
import csp_alerts
for m in pkgutil.walk_packages(csp_alerts.__path__, "csp_alerts."):
    importlib.import_module(m.name)
from csp_alerts import boundary
bad = boundary.loaded_forbidden_modules()
extra = [m for m in sys.modules if m.split('.')[0] in ('requests','httpx','urllib3','aiohttp','telegram','schwab','moomoo','streamlit')]
print("BAD", bad, extra)
sys.exit(1 if (bad or extra) else 0)
"""
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(REPO / "src")}, cwd=str(REPO))
    assert r.returncode == 0, r.stdout + r.stderr


def test_cli_has_no_trade_or_send_commands():
    from csp_alerts.cli import build_parser
    p = build_parser()
    sub = next(a for a in p._actions if a.dest == "command")
    names = set(sub.choices)
    assert names == {"doctor", "policy-check", "approved-list", "reconcile-check", "account-check", "journal",
                     "scan", "alert-preview", "calendar-check", "monitor", "status", "health", "heartbeat",
                     "telegram-setup", "telegram-test", "outbox", "daily-check-in", "reconcile-reminder",
                     "ticker-check",  # read-only pre-approval data report; no --send (see test_ticker_check_acceptance.py)
                     # Stage A (deliberate pin change): watchlist (tracking list; NOT approval) and market-refresh (market-data
                     # tables only). Neither may send or place orders: asserted below via SEND_CAPABLE and test_stage_a_*.
                     "watchlist", "market-refresh"}
    for n in names:
        assert not re.search(r"place|close|roll|sync|trade-|order|execute|submit|(^|-)live($|-)", n), n
    jsub = next(a for a in sub.choices["journal"]._actions if a.dest == "journal_command")
    assert set(jsub.choices) == {"init", "import-csv", "export-csv", "confirm-baseline", "backup", "snapshot", "approve",
                                 "add-trade", "fill", "cancel", "close", "expire", "assign", "reset", "record-cash-flow"}
    # nothing may be spelled place/live/broker-sync anywhere, at any depth
    every = []
    for n, sp in sub.choices.items():
        every.append((n, sp))
        for a in sp._actions:
            if getattr(a, "choices", None) and isinstance(a.choices, dict):
                every += [(f"{n} {k}", v) for k, v in a.choices.items()]
    for label, sp in every:
        opts = {o for a in sp._actions for o in a.option_strings}
        assert not [o for o in opts if re.search(r"place|(^|-)live($|-)|broker|sync|order|execute|submit|roll", o)], (label, opts)
    # --send exists on exactly the commands that may deliver Telegram messages (and nowhere else)
    SEND_CAPABLE = {"scan", "monitor", "heartbeat", "telegram-test", "reconcile-reminder"}
    with_send = {n for n, sp in every if "--send" in {o for a in sp._actions for o in a.option_strings}}
    assert with_send == SEND_CAPABLE, with_send
    # global flags that would trade/send are rejected
    for flag in ("--live-trade", "--place", "--close", "--roll", "--broker-sync", "--send"):
        with pytest.raises(SystemExit):
            p.parse_args([flag, "doctor"])
    for name in sorted(names - SEND_CAPABLE - {"journal"}):
        for flag in ("--send", "--live", "--place", "--live-trade", "--broker-sync"):
            with pytest.raises(SystemExit):
                p.parse_args([name, flag] + (["--fixture", "x"] if name == "alert-preview" else []))
    for jc in set(jsub.choices):
        for flag in ("--send", "--live-trade", "--place", "--broker-sync"):
            with pytest.raises(SystemExit):
                p.parse_args(["journal", jc, flag])
    # --send and --dry-run both exist on send-capable commands; combining is handled at runtime (tested separately)
    assert p.parse_args(["scan", "--send"]).send is True and p.parse_args(["scan"]).send is False


def code_tokens(path) -> str:
    """Identifiers, attribute names and non-docstring string constants of a module (docstrings/comments excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body and                 isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            doc_ids.add(id(n.body[0].value))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            out.append(n.value)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            out.append(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out += [a.name for a in n.names] + ([n.module] if isinstance(n, ast.ImportFrom) and n.module else [])
    return " ".join(out)


def test_telegram_code_is_confined_to_alerts_and_runner_and_has_no_inbound_path():
    """Slice 3 restatement: Telegram transport lives only in alerts/telegram.py (+ runner.py/cli.py glue); outbound
    sendMessage plus the one-time setup getChat; no polling loop, webhook, server socket or command parsing anywhere."""
    for f in py_files():
        t = f.read_text(encoding="utf-8")
        if "api.telegram.org" in t or re.search(r"\burllib\b", t):
            assert f.name == "telegram.py" and f.parent.name == "alerts", f
        if f.name != "boundary.py":
            for bad in ("getUpdates", "setWebhook", "deleteWebhook", "webhook", "http.server", "socketserver", "HTTPServer",
                        "serve_forever", "socket.socket", "start_server", "flask", "fastapi", "aiohttp"):
                assert bad.lower() not in code_tokens(f).lower(), (f.name, bad)
        assert not re.search(r"(message|text|update)\w*\.startswith\(.{0,3}/", t), f.name     # no inbound command parsing
    tg = (SRC / "alerts" / "telegram.py").read_text(encoding="utf-8")
    methods = set(re.findall(r'self\._post\(\s*"([A-Za-z]+)"', tg))
    assert methods == {"sendMessage", "getChat"}, methods
    callers = [f.name for f in py_files() if "get_chat_type" in f.read_text(encoding="utf-8") and f.name != "telegram.py"]
    assert callers == ["cli.py"]
    cli_text = (SRC / "cli.py").read_text(encoding="utf-8")
    assert cli_text.count("get_chat_type") == 1
    setup_body = cli_text.split("def cmd_telegram_setup", 1)[1].split("\ndef ", 1)[0]
    assert "get_chat_type" in setup_body and "args.apply" in setup_body.split("get_chat_type")[0]   # only after --apply


def test_gitignore_covers_secrets_and_data():
    gi = (REPO / ".gitignore").read_text(encoding="utf-8").split()
    for need in (".env", "data/", "third_party/", "*.sqlite3"):
        assert need in gi, need


def test_repo_contains_no_real_looking_telegram_token_or_secret():
    """Scan every text file in the repo for a bot-token shape. ONLY the owner's repo-root .env is exempt, and only
    because it is git-ignored (secrets live there by design, CLAUDE.md); if .env were not ignored it is scanned too.
    Reports file paths only: never the matched text (a real token may be present in the exempt file)."""
    pat = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
    skip = {".venv", "third_party", "__pycache__", ".pytest_cache", ".git"}
    gi = (REPO / ".gitignore").read_text(encoding="utf-8").split()
    env_ignored = ".env" in gi
    hits = []
    for f in REPO.rglob("*"):
        if f.is_dir() or any(part in skip for part in f.parts) or f.suffix in (".pyc", ".sqlite3"):
            continue
        if f.name == "test_settings_policy_boundary.py":
            continue
        if env_ignored and f == REPO / ".env":
            continue
        try:
            t = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if pat.search(t):
            hits.append(str(f.relative_to(REPO)))
    assert hits == [], f"token-shaped secret found in: {hits}"


def test_decimal_only_no_float_in_policy_journal_market():
    offenders = []
    for sub in ("policy", "journal", "market"):
        for f in (SRC / sub).rglob("*.py"):
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Constant) and isinstance(n.value, float) and not (f.name == "store.py" and n.value == 5.0):  # sqlite connect timeout seconds, not money
                    offenders.append((f.name, n.value))
                if isinstance(n, ast.Name) and n.id == "float" and isinstance(n.ctx, ast.Load):
                    offenders.append((f.name, "float()"))
    assert offenders == [], offenders


# ---------------------------------------------------------------- CLI end to end (subprocess, no network)
def cli(args, tmp_path, env_extra=None, env_file_text=None):
    env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME")}
    env["PYTHONPATH"] = str(REPO / "src")
    env["CSP_DATABASE_URL"] = "sqlite:///" + (tmp_path / "j.sqlite3").as_posix()
    env.update(env_extra or {})
    ef = tmp_path / "t.env"
    ef.write_text(env_file_text or "", encoding="utf-8")
    cmd = [sys.executable, "-I", "-m", "csp_alerts.cli", "--env-file", str(ef), "--policy", str(POLICY_PATH),
           "--approved", str(REPO / "config" / "approved.json"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(tmp_path))


def test_cli_doctor_ok_and_never_prints_secrets(tmp_path):
    r = cli(["doctor"], tmp_path, {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT})
    assert r.returncode == 0, r.stdout + r.stderr
    assert FAKE_TOKEN not in r.stdout + r.stderr and FAKE_CHAT not in r.stdout + r.stderr
    assert "0 approved" in r.stdout and "telegram_configured=True" in r.stdout


def test_cli_doctor_fails_on_broker_flag_false_without_leaking(tmp_path):
    r = cli(["doctor"], tmp_path, {"CSP_DISABLE_BROKER_ACCESS": "false", "TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
    assert r.returncode != 0 and "CSP_DISABLE_BROKER_ACCESS" in r.stdout + r.stderr
    assert FAKE_TOKEN not in r.stdout + r.stderr


def test_cli_broker_env_blocks_every_command(tmp_path):
    for cmd in (["doctor"], ["policy-check"], ["approved-list"], ["journal", "init"], ["reconcile-check"], ["account-check"]):
        r = cli(cmd, tmp_path, {"SCHWAB_APP_KEY": "x"})
        if cmd[0] in ("policy-check", "approved-list"):
            continue  # these never load settings (no broker interaction); not required to fail
        assert r.returncode != 0, cmd


def test_cli_env_file_token_never_echoed(tmp_path):
    r = cli(["doctor"], tmp_path, env_file_text=f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nCSP_DISABLE_BROKER_ACCESS=false\n")
    assert r.returncode != 0 and FAKE_TOKEN not in r.stdout + r.stderr


def test_cli_policy_check_and_bad_policy(tmp_path):
    r = cli(["policy-check"], tmp_path)
    assert r.returncode == 0 and "pilot_enabled=True" in r.stdout and "15000" in r.stdout
    d = raw(); d["portfolio"]["allow_margin"] = True
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(d), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH")}
    env["PYTHONPATH"] = str(REPO / "src")
    r2 = subprocess.run([sys.executable, "-I", "-m", "csp_alerts.cli", "--policy", str(bad), "policy-check"],
                        capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r2.returncode != 0 and "allow_margin" in r2.stderr + r2.stdout


def test_cli_approved_list_empty_and_example_rejected(tmp_path):
    r = cli(["approved-list"], tmp_path)
    assert r.returncode == 0 and "EMPTY" in r.stdout
    env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH")}
    env["PYTHONPATH"] = str(REPO / "src")
    r2 = subprocess.run([sys.executable, "-I", "-m", "csp_alerts.cli", "--approved",
                         str(REPO / "config" / "approved.example.json"), "approved-list"],
                        capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r2.returncode != 0


def test_cli_fresh_install_blocks_entries_and_dry_run_import_writes_nothing(tmp_path):
    assert cli(["journal", "init"], tmp_path).returncode == 0
    r = cli(["reconcile-check"], tmp_path)
    assert r.returncode == 1 and "NO_SNAPSHOT" in r.stdout            # fresh install: entries blocked (spec 15.3)
    csvf = tmp_path / "in.csv"
    from csp_alerts.journal.import_csv import COLUMNS
    csvf.write_text(",".join(COLUMNS) + "\nC1,paper_primary,1,AAA,2026-12-04,75.00,100,1,tech,b1,PENDING,,,,0,,\n"
                    "C2,paper_primary,1,AAA,2026-12-04,-75.00,100,1,tech,b1,PENDING,,,,0,,\n", encoding="utf-8")
    r = cli(["journal", "import-csv", str(csvf), "--dry-run"], tmp_path)
    assert "accepted=1 quarantined=1" in r.stdout and r.returncode == 1
    out = tmp_path / "out.csv"
    r = cli(["journal", "export-csv", "--output", str(out)], tmp_path)
    assert r.returncode == 0 and "exported 0" in r.stdout
    r = cli(["journal", "import-csv", str(csvf)], tmp_path)
    assert "accepted=1 quarantined=1" in r.stdout and (tmp_path / "in.quarantine.csv").exists()
    r = cli(["journal", "export-csv", "--output", str(out)], tmp_path)
    assert "exported 1" in r.stdout
    r = cli(["account-check"], tmp_path)
    assert json.loads(r.stdout)["drawdown_level"] == "UNKNOWN"
    assert json.loads(r.stdout)["put_collateral_usd"] == "7500.00"      # PENDING import reserves collateral


def test_cli_bad_csv_header_fails_cleanly(tmp_path):
    cli(["journal", "init"], tmp_path)
    f = tmp_path / "bad.csv"
    f.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    r = cli(["journal", "import-csv", str(f), "--dry-run"], tmp_path)
    assert r.returncode == 2 and "header" in r.stdout
