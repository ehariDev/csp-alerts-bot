"""Contract-level gate result for BROWSING (research only). Reuses `evaluate_entry` unchanged; adds no gate logic.

The call is exactly what `ticker-check` does: no approval, an empty book. Codes that are not properties of the contract
or cannot be known before owner approval (CONTEXT_CODES: not approved, ledger, drawdown, market session) are separated
out; a contract "passes the contract gates (research only)" when no other reason code remains. Approval, event
clearance, free cash, sector/correlation/expiry-week/ticker capacity, ledger, drawdown and the session are NOT evaluated
here, so this marker is never an entry alert and never a recommendation. The Yahoo path carries no deliverable data, so
the contract is evaluated as a standard 100-share put (a labelled assumption, as in ticker-check).
"""
from __future__ import annotations

from datetime import datetime

from csp_alerts.journal.models import BookState
from csp_alerts.market.models import MarketObservation, SessionInfo
from csp_alerts.policy.config import Policy
from csp_alerts.policy.entry import evaluate_entry
from csp_alerts.policy.reasons import Reason

CONTEXT_CODES = frozenset({
    Reason.NOT_APPROVED, Reason.LEDGER_STALE, Reason.LEDGER_UNRECONCILED, Reason.DRAWDOWN_PAUSE, Reason.HARD_HALT,
    Reason.MARKET_CLOSED, Reason.UNKNOWN_SESSION})

NOT_EVALUATED = ("owner approval (max buy strike, event clearance), free cash, sector/correlation/expiry-week/ticker capacity, "
                 "ledger state, drawdown, market session, earnings")


def observation_from_row(symbol: str, r: dict) -> MarketObservation:
    """MarketObservation (standard 100-share assumption, as in the browse marker) from a stored `md_option_latest` row.
    Raises on an unreadable stored value; callers treat that as 'not evaluated', never as a pass."""
    from datetime import date

    from csp_alerts.journal.models import from_iso
    from csp_alerts.money import dec

    stale = bool(r.get("retained_unusable"))      # newer failed/absent attempt: quote fields are history, never current

    def d(k):
        return dec(r[k]) if (r.get(k) is not None and not (stale and k in ("spot", "bid", "ask", "delta"))) else None
    return MarketObservation(
        underlying=symbol, expiration=date.fromisoformat(r["expiration"]), strike=dec(r["strike"]), multiplier=100,
        option_right="P", spot=d("spot"), bid=d("bid"), ask=d("ask"), delta=d("delta"), open_interest=r["open_interest"],
        daily_volume=r["volume"], last_option_trade_at_utc=from_iso(r["last_trade_at_utc"]) if r["last_trade_at_utc"] else None,
        retrieved_at_utc=from_iso(r["retrieved_at_utc"]))


def contract_gate_codes(o: MarketObservation, policy: Policy, now_utc: datetime, session: SessionInfo) -> tuple[str, ...]:
    d = evaluate_entry(o, None, BookState(), policy, now_utc, session)
    return tuple(r.value for r in d.reason_codes if r not in CONTEXT_CODES)
