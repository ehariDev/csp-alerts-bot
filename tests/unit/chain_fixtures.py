"""SYNTHETIC Stockpile-format raw chain rows for tests. Not market data."""
from __future__ import annotations

import math
from datetime import timedelta

from factories import NOW, TODAY

EXP = (TODAY + timedelta(days=40)).isoformat()
LAST_TRADE_MS = float(int((NOW - timedelta(hours=1)).timestamp() * 1000))


def raw_row(strike=40.0, bid=0.50, ask=0.54, delta=-0.13, oi=1000, vol=100, exp=EXP, spot=None, side="put",
            last_trade_ms=LAST_TRADE_MS, iv=0.24, **over):
    spot = strike * 1.2 if spot is None else spot
    row = {
        "type": side, "strike": strike, "expiration": exp, "dte": 40, "spot": spot, "log_moneyness": math.log(strike / spot) if strike > 0 else 0.0,
        "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "last": 0.52, "iv": iv, "iv_fitted": iv, "iv_excess": 0.0,
        "signal_score": 0.0, "signal_kind": "IV+pp", "delta": delta, "gamma": 0.01, "theta": -0.01, "vega": 0.05,
        "ann_yield_pct": 1.0, "open_interest": oi, "volume": vol, "last_trade_days": 0.04,
        "last_trade_ms": last_trade_ms, "earnings_count": 0, "hv_20": float("nan"), "vr_ratio": float("nan"),
    }
    row.update(over)
    return row
