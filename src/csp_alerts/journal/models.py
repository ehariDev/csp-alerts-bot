"""Journal domain types (spec 5). Pure data; no I/O. Decimal for all finance values."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum


class PositionState(StrEnum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    WORKING = "WORKING"
    OPEN = "OPEN"
    CLOSE_WORKING = "CLOSE_WORKING"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    ASSIGNED_PENDING_RECONCILIATION = "ASSIGNED_PENDING_RECONCILIATION"
    ASSIGNED_RECONCILED = "ASSIGNED_RECONCILED"
    ADJUSTMENT_REVIEW = "ADJUSTMENT_REVIEW"


# States that reserve full strike collateral (spec 5.2).
# ADJUSTMENT_REVIEW is added conservatively: the contract is still live until the owner resolves it.
COLLATERAL_STATES = frozenset({
    PositionState.PENDING, PositionState.WORKING, PositionState.OPEN,
    PositionState.CLOSE_WORKING, PositionState.ADJUSTMENT_REVIEW,
})
# States that freeze new entries until the owner finishes manual reconciliation.
LEDGER_BLOCKING_STATES = frozenset({
    PositionState.ASSIGNED_PENDING_RECONCILIATION, PositionState.ADJUSTMENT_REVIEW,
})
# Positions that exit monitoring must watch (ADJUSTMENT_REVIEW included: may be non-standard, verify manually).
MONITORED_STATES = frozenset({PositionState.OPEN, PositionState.CLOSE_WORKING, PositionState.ADJUSTMENT_REVIEW})
TERMINAL_STATES = frozenset({
    PositionState.CLOSED, PositionState.CANCELED, PositionState.EXPIRED, PositionState.ASSIGNED_RECONCILED,
})


def norm_label(s: str) -> str:
    """Owner-typed sector/bucket labels compare case- and whitespace-insensitively."""
    return s.strip().casefold()


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; use tz-aware UTC")
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class PutPosition:
    trade_id: str
    account_scope: str
    paper: bool
    ticker: str
    expiration: date
    strike: Decimal
    multiplier: int
    contracts: int
    sector: str
    correlation_bucket: str
    state: PositionState
    asset_type: str = "UNKNOWN"  # resolved by policy.approved.resolve_asset_type; not a DB column of option_positions
    opened_at_utc: datetime | None = None
    actual_open_credit_per_share: Decimal | None = None
    actual_close_debit_per_share: Decimal | None = None
    fees_usd: Decimal = Decimal("0")
    closed_at_utc: datetime | None = None
    parent_event_id: str | None = None
    last_reconciled_at_utc: datetime | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sector", norm_label(self.sector))
        object.__setattr__(self, "correlation_bucket", norm_label(self.correlation_bucket))

    @property
    def collateral_usd(self) -> Decimal:
        return self.strike * self.multiplier * self.contracts

    @property
    def contract_key(self) -> str:
        return f"{self.ticker}|{self.expiration.isoformat()}|P|{self.strike:.2f}|{self.multiplier}"

    @property
    def reserves_collateral(self) -> bool:
        return self.state in COLLATERAL_STATES


@dataclass(frozen=True)
class StockLot:
    lot_id: str
    ticker: str
    shares: Decimal
    reference_price: Decimal
    reference_asof_utc: datetime
    sector: str
    correlation_bucket: str
    acquisition_kind: str
    asset_type: str = "UNKNOWN"
    cost_basis_reference_usd: Decimal | None = None
    linked_trade_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sector", norm_label(self.sector))
        object.__setattr__(self, "correlation_bucket", norm_label(self.correlation_bucket))

    @property
    def exposure_usd(self) -> Decimal:
        return self.shares * self.reference_price


@dataclass(frozen=True)
class AccountSnapshot:
    snapshot_id: str
    asof_utc: datetime
    equity_usd: Decimal
    free_unencumbered_cash_usd: Decimal
    manual_confirmed: bool
    reconciliation_version: str
    # Journal event order (audit rowid of SNAPSHOT_ADDED); 0 = unknown. Breaks same-second timestamp ties (SR-06/07).
    seq: int = field(default=0, compare=False)


@dataclass(frozen=True)
class BookState:
    """Everything the risk engine needs from the manual ledger, as of `loaded_at_utc`."""
    positions: tuple[PutPosition, ...] = ()
    stock_lots: tuple[StockLot, ...] = ()
    snapshots: tuple[AccountSnapshot, ...] = ()
    last_ledger_change_utc: datetime | None = None
    last_risk_reset_utc: datetime | None = None
    baseline_equity_usd: Decimal | None = None  # owner-confirmed starting equity (audited event)
    baseline_confirmed_at_utc: datetime | None = None
    # (recorded_at_utc, +deposit/-withdrawal[, journal_seq]), IT-008. The optional 3rd item is the audit rowid and
    # orders a flow against snapshots recorded in the same second.
    cash_flows: tuple[tuple, ...] = ()
    last_ledger_change_seq: int = 0  # audit rowid of the latest ledger event (0 = unknown)
    ledger_revision: int = 0  # highest audit rowid among ledger events + snapshots; changes on EVERY ledger event
    # Owner-reported trades recorded while breaching a policy rule: (trade_id, reason codes). Flag only; the position keeps
    # its true state. While such a position still reserves collateral, reconcile blocks new entries (POLICY_BREACH_UNRESOLVED).
    policy_breaches: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def latest_snapshot(self) -> AccountSnapshot | None:
        return max(self.snapshots, key=lambda s: (s.asof_utc, s.seq), default=None)


@dataclass(frozen=True)
class ReconcileIssue:
    code: str
    reason_code: str  # entry-gate reason code this maps to (LEDGER_STALE | LEDGER_UNRECONCILED)
    severity: str  # BLOCK | WARN
    message: str
    ref: str = ""


@dataclass(frozen=True)
class ReconcileReport:
    issues: tuple[ReconcileIssue, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> bool:
        return any(i.severity == "BLOCK" for i in self.issues)
