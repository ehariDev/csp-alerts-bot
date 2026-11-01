"""Manual ledger state machine (spec 5.2). Pure; the store enforces it inside a transaction.

Every transition is an owner-reported fact. Nothing here is driven by market data or by time
(e.g. DTE reaching 0 never closes or expires a position).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.models import PutPosition

TRANSITIONS: dict[S, frozenset[S]] = {
    S.DRAFT: frozenset({S.PENDING, S.CANCELED}),
    S.PENDING: frozenset({S.WORKING, S.OPEN, S.CANCELED}),
    S.WORKING: frozenset({S.OPEN, S.CANCELED}),
    S.OPEN: frozenset({S.CLOSE_WORKING, S.EXPIRED, S.ASSIGNED_PENDING_RECONCILIATION, S.ADJUSTMENT_REVIEW}),
    # CLOSE_WORKING -> OPEN: owner cancelled the closing order (not in spec diagram; needed to be usable).
    S.CLOSE_WORKING: frozenset({S.CLOSED, S.OPEN, S.EXPIRED, S.ASSIGNED_PENDING_RECONCILIATION, S.ADJUSTMENT_REVIEW}),
    S.ADJUSTMENT_REVIEW: frozenset({S.OPEN, S.CLOSE_WORKING, S.CLOSED, S.EXPIRED, S.ASSIGNED_PENDING_RECONCILIATION}),
    S.ASSIGNED_PENDING_RECONCILIATION: frozenset({S.ASSIGNED_RECONCILED}),
    S.CLOSED: frozenset(),
    S.CANCELED: frozenset(),
    S.EXPIRED: frozenset(),
    S.ASSIGNED_RECONCILED: frozenset(),
}

# Fields a caller may set while transitioning.
ALLOWED_FIELDS = frozenset({
    "opened_at_utc", "actual_open_credit_per_share", "actual_close_debit_per_share",
    "fees_usd", "closed_at_utc", "notes", "fill_contracts", "confirmed",
})


class TransitionError(ValueError):
    pass


def validate_transition(
    pos: PutPosition,
    to_state: S,
    fields: Mapping[str, Any],
    *,
    linked_lot_shares: Decimal | None = None,
    linked_lot_cost_basis: Decimal | None = None,
) -> None:
    """Raise TransitionError unless the transition and its required owner-supplied facts are valid."""
    unknown = set(fields) - ALLOWED_FIELDS
    if unknown:
        raise TransitionError(f"unknown fields: {sorted(unknown)}")
    if to_state not in TRANSITIONS[pos.state]:
        raise TransitionError(f"illegal transition {pos.state.value} -> {to_state.value}")

    for name in ("actual_open_credit_per_share", "actual_close_debit_per_share", "fees_usd"):
        v = fields.get(name)
        if v is not None and (not isinstance(v, Decimal) or not v.is_finite()):
            raise TransitionError(f"{name} must be a finite Decimal (floats/NaN/Infinity rejected)")

    confirmed = fields.get("confirmed") is True

    if to_state is S.OPEN:
        credit = fields.get("actual_open_credit_per_share", pos.actual_open_credit_per_share)
        opened = fields.get("opened_at_utc", pos.opened_at_utc)
        if pos.state in (S.PENDING, S.WORKING):  # a fill is being reported
            if "fill_contracts" not in fields:
                raise TransitionError("OPEN requires explicit fill_contracts (actual filled quantity)")
            if fields["fill_contracts"] != pos.contracts:
                raise TransitionError("partial fills are not supported in v1: fill_contracts must equal contracts")
            if "actual_open_credit_per_share" not in fields or "opened_at_utc" not in fields:
                raise TransitionError("OPEN requires actual_open_credit_per_share and opened_at_utc")
        if credit is None or not isinstance(credit, Decimal) or credit <= 0:
            raise TransitionError("OPEN requires a positive actual opening credit per share")
        if not isinstance(opened, datetime):
            raise TransitionError("OPEN requires opened_at_utc")
    elif to_state is S.CLOSED:
        debit = fields.get("actual_close_debit_per_share")
        if not isinstance(debit, Decimal) or debit < 0:
            raise TransitionError("CLOSED requires actual_close_debit_per_share >= 0 (confirmed closing fill)")
        if not isinstance(fields.get("closed_at_utc"), datetime):
            raise TransitionError("CLOSED requires closed_at_utc")
        if "fees_usd" not in fields:
            raise TransitionError("CLOSED requires fees_usd (use 0 explicitly if none)")
    elif to_state is S.EXPIRED:
        if not confirmed:
            raise TransitionError("EXPIRED requires owner confirmation (confirmed=True); never assumed from DTE")
        ts = fields.get("closed_at_utc")
        if not isinstance(ts, datetime):
            raise TransitionError("EXPIRED requires closed_at_utc")
        if ts.date() < pos.expiration:
            raise TransitionError("cannot record expiry before the expiration date")
    elif to_state is S.CANCELED:
        if pos.state in (S.PENDING, S.WORKING) and not confirmed:
            raise TransitionError("CANCELED requires owner-verified cancellation (confirmed=True)")
    elif to_state is S.ASSIGNED_RECONCILED:
        if not confirmed:
            raise TransitionError("ASSIGNED_RECONCILED requires owner confirmation (confirmed=True)")
        if linked_lot_shares is None:
            raise TransitionError("record the received shares as a stock lot linked to this trade first")
        if linked_lot_shares != pos.contracts * pos.multiplier:
            raise TransitionError("linked stock lot shares must equal contracts x multiplier")
        if linked_lot_cost_basis is None:
            raise TransitionError("linked stock lot must record the actual purchase cash (cost_basis_reference_usd)")

    for name in ("actual_open_credit_per_share", "actual_close_debit_per_share", "fees_usd"):
        v = fields.get(name)
        if v is not None and (not isinstance(v, Decimal) or not v.is_finite() or v < 0):
            raise TransitionError(f"{name} must be a non-negative finite Decimal")


def is_expired_unconfirmed(pos: PutPosition, today: date) -> bool:
    """OPEN-like position past its expiration date with no owner confirmation (reconcile flag only)."""
    return pos.state in (S.OPEN, S.CLOSE_WORKING) and pos.expiration < today
