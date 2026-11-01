"""Ledger input rules shared by the owner CLI (`ledger_cli`) and the web recording path (`webtrade`).

Pure functions, no I/O, no wall clock. They exist so the web layer never re-implements a financial or timing rule:
both front ends call these and then the same `Journal` methods.

* cash consistency:   free_cash + sum(strike * multiplier * contracts over collateral-reserving puts) <= equity
* event time (expire/assign): 21:00Z of the exchange date, never later than the journal clock; a typed date after
  today's exchange date is refused outright (an event cannot be recorded in the future).
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

from csp_alerts.journal.models import COLLATERAL_STATES, PutPosition
from csp_alerts.journal.reconcile import exchange_date


class RuleError(ValueError):
    """A ledger input rule was violated (message is owner-facing and contains no untrusted text)."""


def committed_collateral(positions) -> Decimal:
    """sum(strike * multiplier * contracts) over PENDING/WORKING/OPEN/CLOSE_WORKING/ADJUSTMENT_REVIEW puts."""
    return sum((p.collateral_usd for p in positions if p.state in COLLATERAL_STATES), Decimal("0"))


def check_cash(committed: Decimal, equity: Decimal, cash: Decimal) -> None:
    """Raise RuleError (CASH_INCONSISTENT) unless 0 <= cash, equity > 0 and cash + committed <= equity."""
    if equity <= 0 or cash < 0:
        raise RuleError("equity must be positive and free cash must be >= 0")
    if cash + committed > equity:
        raise RuleError(
            f"CASH_INCONSISTENT: free cash {cash} + active put collateral {committed} exceeds equity {equity}. "
            "Enter NET free unencumbered cash (already reduced by collateral reserved for your puts).")


def event_time(now: datetime, on: date | None, *, refuse_future_date: bool = False) -> datetime:
    """21:00Z of `on` (default: today's exchange date), clamped to `now` (SR-08). With refuse_future_date a date after the
    current exchange date raises RuleError and nothing is stored (expiry)."""
    day = on if on is not None else exchange_date(now)
    when = datetime.combine(day, time(21, 0), tzinfo=timezone.utc)
    if refuse_future_date and when.date() > exchange_date(now):
        raise RuleError(f"--date {when.date()} is after today's exchange date {exchange_date(now)}; an event cannot be "
                        "recorded in the future. Run this on or after the expiration date.")
    return min(when, now)


def position_is_single_contract(p: PutPosition) -> bool:
    return p.contracts == 1 and p.multiplier == 100
