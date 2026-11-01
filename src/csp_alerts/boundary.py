"""No-broker boundary (spec 3.1 / 20). Single source of truth for forbidden modules.

The static audit lives in tests/unit/test_boundary.py; `assert_no_broker_modules_loaded`
is the runtime counterpart used by `doctor`.
"""
from __future__ import annotations

import sys

# Module-name prefixes that must never be imported (statically or dynamically).
FORBIDDEN_MODULE_PREFIXES: tuple[str, ...] = (
    "options_scanner.trade_actions",
    "options_scanner.confirm_gate",
    "options_scanner.tabs.trades",
    "options_scanner.tabs.rolls",
    "options_scanner.trades_store",
    "options_scanner.schwab_chain",
    "options_scanner.moomoo_chain",
    "options_scanner.tabs",  # Streamlit presentation modules
    "stocks_shared.schwab_live",
    "schwab",
    "moomoo",
    "futu",
    "streamlit",
    "ib_insync",
    "ibapi",
    "ib_async",
    "alpaca",
    "alpaca_trade_api",
    "tda",
    "robin_stocks",
    "tastytrade",
    "tradier",
)

# Broker env-var name prefixes: any non-empty variable starting with one of these is a startup error.
# Deliberately prefix-scoped so ordinary Windows/CI environments are not rejected.
BROKER_ENV_PREFIXES: tuple[str, ...] = (
    "SCHWAB", "IBKR", "IB_", "MOOMOO", "ALPACA", "TRADIER", "TASTYTRADE", "ROBINHOOD",
    "ETRADE_", "FUTU_", "WEBULL_", "TOS_", "TDA_",
)

# Distribution names that must not appear in our dependency list.
FORBIDDEN_DISTRIBUTIONS: tuple[str, ...] = (
    "schwab-py", "schwab", "moomoo-api", "futu-api", "streamlit", "ib-insync",
    "ibapi", "ib-async", "alpaca-py", "alpaca-trade-api", "tda-api",
    "robin-stocks", "tastytrade", "tradier",
)

# Function/attribute names that would indicate an order path in our own code.
FORBIDDEN_CALLABLE_NAMES: frozenset[str] = frozenset({
    "place_order", "submit_order", "cancel_order", "modify_order", "replace_order",
    "send_order", "create_order", "place_trade", "execute_trade", "buy_to_close",
    "sell_to_open", "broker_sync",
})


def is_forbidden_module(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES)


def loaded_forbidden_modules() -> list[str]:
    return sorted(m for m in sys.modules if is_forbidden_module(m))


class BrokerBoundaryError(RuntimeError):
    pass


def assert_no_broker_modules_loaded() -> None:
    bad = loaded_forbidden_modules()
    if bad:
        raise BrokerBoundaryError(f"forbidden broker/presentation modules loaded: {bad}")
