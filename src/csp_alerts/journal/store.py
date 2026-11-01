"""SQLite canonical journal (spec 5): WAL, transactions, migrations, append-only audit log.

All writes are owner-initiated (CLI / CSV import). Nothing here talks to a broker.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from csp_alerts.journal.models import (
    COLLATERAL_STATES,
    AccountSnapshot,
    BookState,
    PositionState,
    PutPosition,
    StockLot,
    from_iso,
    norm_label,
    to_iso,
)
from csp_alerts.journal.state_machine import ALLOWED_FIELDS, TransitionError, validate_transition
from csp_alerts.money import dec, fmt
from csp_alerts.policy.approved import ApprovedUnderlying, asset_type_map, resolve_asset_type
from csp_alerts.policy.config import Policy

# Audit events that change what the owner must re-confirm (spec: reconcile after every trade).
LEDGER_EVENT_TYPES = (
    "POSITION_CREATED", "POSITION_TRANSITION", "STOCK_LOT_ADDED", "POSITION_IMPORTED", "CASH_FLOW",
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class _AssetTypes:
    """Per-ticker asset type through the single resolver (journal table, approvals file, UNKNOWN)."""
    def __init__(self, journal_types: dict[str, str], file_types: dict[str, str]):
        self.j, self.f = journal_types, file_types

    def get(self, ticker: str) -> str:
        return resolve_asset_type(ticker, self.j, self.f)[0]


class JournalError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewPosition:
    trade_id: str
    ticker: str
    expiration: date
    strike: Decimal
    multiplier: int
    contracts: int
    sector: str
    correlation_bucket: str
    account_scope: str = "paper_primary"
    paper: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sector", norm_label(self.sector))
        object.__setattr__(self, "correlation_bucket", norm_label(self.correlation_bucket))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _d(v: str | None) -> Decimal | None:
    return None if v is None else dec(v)


def _ts(v: str | None) -> datetime | None:
    return None if v is None else from_iso(v)


class Journal:
    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = _now):
        self.path = str(path)
        self.clock = clock
        self.last_migration_backup: Path | None = None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if self.path != ":memory:":
            mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise JournalError(f"could not enable WAL (got {mode})")

    # ---- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic write unit. Re-entrant: inside an already open transaction it becomes a SAVEPOINT, so several domain
        operations (create + transition + audit) can be wrapped in ONE outer transaction (used by the web recording path)
        and a failure anywhere rolls the whole unit back."""
        if self.conn.in_transaction:
            self._sp = getattr(self, "_sp", 0) + 1
            name = f"sp{self._sp}"
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute(f"ROLLBACK TO {name}")
                self.conn.execute(f"RELEASE {name}")
                raise
            else:
                self.conn.execute(f"RELEASE {name}")
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def migrate(self) -> list[str]:
        """Apply pending migrations in order, each atomically. Returns applied versions."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        done = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        files = sorted(
            (p for p in MIGRATIONS_DIR.iterdir()
             if _MIGRATION_RE.match(p.name)),
            key=lambda p: p.name,
        )
        applied: list[str] = []
        pending = [p for p in files if p.name[:4] not in done]
        if pending and done and self.path != ":memory:":
            self._backup_before_migrate(sorted(done)[-1], pending[-1].name[:4])
        for p in files:
            version = p.name[:4]
            if version in done:
                continue
            script = (
                "BEGIN IMMEDIATE;\n" + p.read_text(encoding="utf-8") + "\n"
                f"INSERT INTO schema_migrations(version, applied_at_utc) VALUES ('{version}', '{to_iso(self.clock())}');\n"
                "COMMIT;"
            )
            try:
                self.conn.executescript(script)
            except sqlite3.Error as exc:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise JournalError(f"migration {p.name} failed: {exc}") from exc
            applied.append(version)
        return applied

    def _backup_before_migrate(self, from_version: str, to_version: str) -> Path:
        """Consistent backup (SQLite online backup API, integrity-checked) of an EXISTING journal before any schema
        upgrade. A fresh journal (no applied migration yet) has nothing to protect and is not backed up. If the backup
        cannot be made or fails its integrity check the migration is refused and the journal is left untouched."""
        dest = Path(self.path).resolve().parent / "backups" / (
            f"pre-migrate-{from_version}-to-{to_version}-{self.clock():%Y%m%d-%H%M%S}.sqlite3")
        try:
            self.backup(dest)
            chk = sqlite3.connect(str(dest))
            try:
                ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            finally:
                chk.close()
        except (sqlite3.Error, OSError) as exc:
            raise JournalError(f"refusing to migrate: pre-migration backup failed ({type(exc).__name__})") from exc
        if not ok:
            raise JournalError("refusing to migrate: pre-migration backup failed its integrity check")
        self.last_migration_backup = dest
        return dest

    def schema_version(self) -> str | None:
        try:
            row = self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.Error:
            return None
        return row[0]

    def integrity_ok(self) -> bool:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def backup(self, dest: Path | str) -> None:
        """Consistent online backup via the SQLite backup API."""
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    # ---- audit -----------------------------------------------------------
    def _audit(self, event_type: str, actor: str, before: Any, after: Any, source: str) -> str:
        eid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO audit_log(event_id, created_at_utc, event_type, actor, before_json, after_json, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                eid, to_iso(self.clock()), event_type, actor,
                None if before is None else json.dumps(before, sort_keys=True, default=str),
                None if after is None else json.dumps(after, sort_keys=True, default=str),
                source,
            ),
        )
        return eid

    def audit_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM audit_log" + (" WHERE event_type=?" if event_type else "") + " ORDER BY rowid"
        rows = self.conn.execute(q, (event_type,) if event_type else ()).fetchall()
        return [dict(r) for r in rows]

    # ---- policy / approvals ---------------------------------------------
    def record_policy(self, policy: Policy, *, actor: str = "owner", source: str = "cli") -> bool:
        """Persist a policy revision (idempotent per sha256). Returns True if newly recorded."""
        with self.tx():
            exists = self.conn.execute(
                "SELECT 1 FROM policy_versions WHERE policy_id=? AND sha256=?", (policy.policy_id, policy.sha256)
            ).fetchone()
            if exists:
                return False
            self.conn.execute(
                "INSERT INTO policy_versions VALUES (?,?,?,?,?)",
                (policy.policy_id, to_iso(self.clock()), policy.sha256, policy.approved_by, policy.canonical_json()),
            )
            self._audit("POLICY_RECORDED", actor, None, {"policy_id": policy.policy_id, "sha256": policy.sha256}, source)
            return True

    def upsert_approved(self, a: ApprovedUnderlying, *, actor: str = "owner", source: str = "cli") -> None:
        if a.synthetic:
            raise JournalError("synthetic/example approvals cannot be recorded")
        with self.tx():
            before = self.conn.execute("SELECT * FROM approved_underlyings WHERE ticker=?", (a.ticker,)).fetchone()
            self.conn.execute(
                "INSERT OR REPLACE INTO approved_underlyings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (a.ticker, a.asset_type, a.sector, a.correlation_bucket, fmt(a.max_buy_strike),
                 a.approved_through.isoformat(), a.events_reviewed_on.isoformat(),
                 a.events_cleared_through.isoformat(), a.ownership_reason, a.approved_by, to_iso(self.clock())),
            )
            self._audit("APPROVAL_UPSERT", actor, dict(before) if before else None, a.model_dump(mode="json"), source)

    def list_approved(self) -> list[ApprovedUnderlying]:
        rows = self.conn.execute("SELECT * FROM approved_underlyings ORDER BY ticker").fetchall()
        return [
            ApprovedUnderlying(
                ticker=r["ticker"], asset_type=r["asset_type"], sector=r["sector"],
                correlation_bucket=r["correlation_bucket"], max_buy_strike=r["max_buy_strike"],
                approved_through=r["approved_through"], events_reviewed_on=r["events_reviewed_on"],
                events_cleared_through=r["events_cleared_through"], ownership_reason=r["ownership_reason"],
                approved_by=r["approved_by"],
            )
            for r in rows
        ]

    # ticker -> asset type read from the approvals FILE (read-only fallback; set by load_book(approved)). Never written anywhere.
    approval_file_types: dict[str, str] = {}

    def journal_asset_types(self) -> dict[str, str]:
        return {r["ticker"]: r["asset_type"] for r in self.conn.execute("SELECT ticker, asset_type FROM approved_underlyings")}

    def _asset_types(self) -> "_AssetTypes":
        return _AssetTypes(self.journal_asset_types(), self.approval_file_types)

    # ---- positions -------------------------------------------------------
    def _row_to_position(self, r: sqlite3.Row, asset_types: "_AssetTypes") -> PutPosition:
        return PutPosition(
            trade_id=r["trade_id"], account_scope=r["account_scope"], paper=bool(r["paper"]),
            ticker=r["ticker"], expiration=date.fromisoformat(r["expiration"]), strike=dec(r["strike"]),
            multiplier=r["multiplier"], contracts=r["contracts"], sector=r["sector"],
            correlation_bucket=r["correlation_bucket"], state=PositionState(r["state"]),
            # journal table, else approvals file, else UNKNOWN (counted as an individual company: conservative for the name cap).
            asset_type=asset_types.get(r["ticker"]),
            opened_at_utc=_ts(r["opened_at_utc"]), actual_open_credit_per_share=_d(r["actual_open_credit_per_share"]),
            actual_close_debit_per_share=_d(r["actual_close_debit_per_share"]), fees_usd=dec(r["fees_usd"]),
            closed_at_utc=_ts(r["closed_at_utc"]), parent_event_id=r["parent_event_id"],
            last_reconciled_at_utc=_ts(r["last_reconciled_at_utc"]), notes=r["notes"],
        )

    def get_position(self, trade_id: str) -> PutPosition:
        r = self.conn.execute("SELECT * FROM option_positions WHERE trade_id=?", (trade_id,)).fetchone()
        if r is None:
            raise JournalError(f"unknown trade_id {trade_id!r}")
        return self._row_to_position(r, self._asset_types())

    def list_positions(self, states: set[PositionState] | None = None) -> list[PutPosition]:
        at = self._asset_types()
        rows = self.conn.execute("SELECT * FROM option_positions ORDER BY expiration, trade_id").fetchall()
        out = [self._row_to_position(r, at) for r in rows]
        return [p for p in out if states is None or p.state in states]

    @staticmethod
    def _validate_new(n: NewPosition) -> None:
        if not n.trade_id.strip() or not n.ticker.strip():
            raise JournalError("trade_id and ticker are required")
        if not isinstance(n.strike, Decimal) or not n.strike.is_finite() or n.strike <= 0:
            raise JournalError("strike must be a positive finite Decimal")
        if n.multiplier <= 0 or n.contracts <= 0:
            raise JournalError("multiplier and contracts must be positive")
        if not n.sector.strip() or not n.correlation_bucket.strip():
            raise JournalError("sector and correlation_bucket are required")
        if not n.paper and n.account_scope.startswith("paper"):
            raise JournalError("paper=False is not allowed in a paper scope (initial delivery is paper-only)")

    def _insert_position(self, n: NewPosition, state: PositionState, extra: dict[str, Any] | None = None) -> None:
        extra = extra or {}
        self.conn.execute(
            "INSERT INTO option_positions(trade_id, account_scope, paper, ticker, option_right, expiration, strike, "
            "multiplier, contracts, sector, correlation_bucket, state, opened_at_utc, actual_open_credit_per_share, "
            "actual_close_debit_per_share, fees_usd, closed_at_utc, parent_event_id, last_reconciled_at_utc, notes) "
            "VALUES (?,?,?,?,'P',?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                n.trade_id, n.account_scope, int(n.paper), n.ticker, n.expiration.isoformat(), fmt(n.strike),
                n.multiplier, n.contracts, n.sector, n.correlation_bucket, state.value,
                extra.get("opened_at_utc"), extra.get("actual_open_credit_per_share"),
                extra.get("actual_close_debit_per_share"), extra.get("fees_usd", "0"), extra.get("closed_at_utc"),
                to_iso(self.clock()), n.notes,
            ),
        )

    def create_draft(self, n: NewPosition, *, actor: str = "owner", source: str = "cli") -> None:
        """DRAFT reserves no collateral and is not permission to trade."""
        self._validate_new(n)
        with self.tx():
            try:
                self._insert_position(n, PositionState.DRAFT)
            except sqlite3.IntegrityError as exc:
                raise JournalError(f"cannot create {n.trade_id!r}: {exc}") from exc
            self._audit("POSITION_DRAFT_CREATED", actor, None, {"trade_id": n.trade_id, "state": "DRAFT"}, source)

    def transition(
        self, trade_id: str, to_state: PositionState, *, actor: str = "owner", source: str = "cli", **fields: Any
    ) -> PutPosition:
        """Apply an owner-reported state change atomically; raises TransitionError on any rule violation."""
        bad = set(fields) - ALLOWED_FIELDS
        if bad:
            raise TransitionError(f"unknown fields: {sorted(bad)}")
        with self.tx():
            pos = self.get_position(trade_id)
            lot = self.conn.execute(
                "SELECT shares, cost_basis_reference_usd FROM stock_lots WHERE linked_trade_id=?", (trade_id,)
            ).fetchone()
            validate_transition(
                pos, to_state, fields,
                linked_lot_shares=_d(lot["shares"]) if lot else None,
                linked_lot_cost_basis=_d(lot["cost_basis_reference_usd"]) if lot else None,
            )
            sets: dict[str, Any] = {"state": to_state.value}
            for k in ("opened_at_utc", "closed_at_utc"):
                if k in fields:
                    sets[k] = to_iso(fields[k])
            for k in ("actual_open_credit_per_share", "actual_close_debit_per_share", "fees_usd"):
                if k in fields:
                    sets[k] = fmt(fields[k])
            if "notes" in fields:
                sets["notes"] = str(fields["notes"])
            self.conn.execute(
                f"UPDATE option_positions SET {', '.join(f'{c}=?' for c in sets)} WHERE trade_id=?",
                (*sets.values(), trade_id),
            )
            self._audit(
                "POSITION_TRANSITION", actor,
                {"trade_id": trade_id, "state": pos.state.value},
                {"trade_id": trade_id, "state": to_state.value, **{k: v for k, v in sets.items() if k != "state"}},
                source,
            )
        return self.get_position(trade_id)

    # ---- stock lots ------------------------------------------------------
    def add_stock_lot(self, lot: StockLot, *, actor: str = "owner", source: str = "cli") -> None:
        if not all(isinstance(v, Decimal) and v.is_finite() for v in (lot.shares, lot.reference_price)):
            raise JournalError("shares and reference_price must be finite Decimals")
        if lot.shares <= 0 or lot.reference_price <= 0:
            raise JournalError("shares and reference_price must be positive")
        if lot.reference_asof_utc > self.clock() + timedelta(minutes=5):
            raise JournalError("stock mark is dated in the future")
        if not lot.sector.strip() or not lot.correlation_bucket.strip() or not lot.acquisition_kind.strip():
            raise JournalError("sector, correlation_bucket and acquisition_kind are required")
        with self.tx():
            if lot.linked_trade_id is not None:
                pos = self.get_position(lot.linked_trade_id)
                if pos.state is not PositionState.ASSIGNED_PENDING_RECONCILIATION:
                    raise JournalError("a lot can only be linked to a put in ASSIGNED_PENDING_RECONCILIATION")
                if pos.ticker != lot.ticker:
                    raise JournalError("linked lot ticker differs from the put's ticker")
            try:
                self.conn.execute(
                    "INSERT INTO stock_lots VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (lot.lot_id, lot.ticker, fmt(lot.shares), fmt(lot.reference_price), to_iso(lot.reference_asof_utc),
                     None if lot.cost_basis_reference_usd is None else fmt(lot.cost_basis_reference_usd),
                     lot.acquisition_kind, lot.linked_trade_id, lot.sector, lot.correlation_bucket),
                )
            except sqlite3.IntegrityError as exc:
                raise JournalError(f"cannot add lot {lot.lot_id!r}: {exc}") from exc
            self._audit("STOCK_LOT_ADDED", actor, None, {"lot_id": lot.lot_id, "ticker": lot.ticker,
                                                         "shares": fmt(lot.shares), "linked_trade_id": lot.linked_trade_id}, source)

    def mark_stock_lot(self, lot_id: str, price: Decimal, asof_utc: datetime, *, actor: str = "owner", source: str = "cli") -> None:
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise JournalError("price must be a positive finite Decimal")
        if asof_utc > self.clock() + timedelta(minutes=5):
            raise JournalError("stock mark is dated in the future")
        with self.tx():
            r = self.conn.execute("SELECT * FROM stock_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if r is None:
                raise JournalError(f"unknown lot {lot_id!r}")
            self.conn.execute("UPDATE stock_lots SET reference_price=?, reference_asof_utc=? WHERE lot_id=?",
                              (fmt(price), to_iso(asof_utc), lot_id))
            self._audit("STOCK_LOT_MARKED", actor, {"reference_price": r["reference_price"]},
                        {"reference_price": fmt(price), "asof": to_iso(asof_utc)}, source)

    def list_stock_lots(self) -> list[StockLot]:
        at = self._asset_types()
        out = []
        for r in self.conn.execute("SELECT * FROM stock_lots ORDER BY lot_id"):
            out.append(StockLot(
                lot_id=r["lot_id"], ticker=r["ticker"], shares=dec(r["shares"]), reference_price=dec(r["reference_price"]),
                reference_asof_utc=from_iso(r["reference_asof_utc"]), sector=r["sector"],
                correlation_bucket=r["correlation_bucket"], acquisition_kind=r["acquisition_kind"],
                asset_type=at.get(r["ticker"]), cost_basis_reference_usd=_d(r["cost_basis_reference_usd"]),
                linked_trade_id=r["linked_trade_id"],
            ))
        return out

    # ---- snapshots / risk reset -----------------------------------------
    def add_snapshot(self, snap: AccountSnapshot, *, actor: str = "owner", source: str = "cli") -> None:
        for name, v in (("equity", snap.equity_usd), ("free cash", snap.free_unencumbered_cash_usd)):
            if not isinstance(v, Decimal) or not v.is_finite():
                raise JournalError(f"{name} must be a finite Decimal")
        if snap.equity_usd <= 0:
            raise JournalError("equity must be positive")
        if snap.free_unencumbered_cash_usd < 0:
            raise JournalError("free cash must be >= 0")
        if snap.asof_utc > self.clock() + timedelta(minutes=5):
            raise JournalError("snapshot is dated in the future")
        with self.tx():
            try:
                self.conn.execute(
                    "INSERT INTO account_snapshots VALUES (?,?,?,?,?,?)",
                    (snap.snapshot_id, to_iso(snap.asof_utc), fmt(snap.equity_usd), fmt(snap.free_unencumbered_cash_usd),
                     int(snap.manual_confirmed), snap.reconciliation_version),
                )
            except sqlite3.IntegrityError as exc:
                raise JournalError(f"cannot add snapshot: {exc}") from exc
            self._audit("SNAPSHOT_ADDED", actor, None, {"snapshot_id": snap.snapshot_id, "asof": to_iso(snap.asof_utc),
                                                        "confirmed": snap.manual_confirmed}, source)

    def list_snapshots(self) -> list[AccountSnapshot]:
        seqs: dict[str, int] = {}
        for r in self.conn.execute("SELECT rowid, after_json FROM audit_log WHERE event_type='SNAPSHOT_ADDED' ORDER BY rowid"):
            seqs[json.loads(r[1]).get("snapshot_id", "")] = r[0]
        rows = [
            AccountSnapshot(r["snapshot_id"], from_iso(r["asof_utc"]), dec(r["equity_usd"]),
                            dec(r["free_unencumbered_cash_usd"]), bool(r["manual_confirmed"]), r["reconciliation_version"],
                            seqs.get(r["snapshot_id"], 0))
            for r in self.conn.execute("SELECT * FROM account_snapshots")
        ]
        return sorted(rows, key=lambda s: (s.asof_utc, s.seq))

    def record_risk_reset(self, note: str, *, actor: str = "owner", source: str = "cli") -> datetime:
        """Owner's explicit post-review reset of the 8% hard halt. Sets the drawdown baseline to the
        latest manually confirmed snapshot; earlier equity peaks stop counting."""
        if not note.strip():
            raise JournalError("a review note is required for a risk reset")
        with self.tx():
            confirmed = [s for s in self.list_snapshots() if s.manual_confirmed]
            if not confirmed:
                raise JournalError("a manually confirmed snapshot is required before a risk reset")
            base = confirmed[-1].asof_utc
            self._audit("RISK_RESET", actor, None, {"baseline_asof_utc": to_iso(base), "note": note}, source)
            return base

    # ---- market observations / evaluations / data issues ---------------
    def record_observations(self, rows: list[tuple[str, dict[str, Any], str]]) -> None:
        """rows: (observation_id, spec-6.1 dict, raw_sha256). Provenance only; never used as ledger truth."""
        with self.tx():
            for oid, d, sha in rows:
                self.conn.execute(
                    "INSERT INTO market_observations VALUES (?,?,?,?,?,?,?,?)",
                    (oid, d["contract_key"], d["retrieved_at_utc"] or to_iso(self.clock()), d["quote_at_utc"],
                     d["provider"], d["data_tier"], sha, json.dumps(d, sort_keys=True)),
                )

    def record_evaluations(self, run_id: str, rows: list[tuple[str | None, dict[str, Any]]]) -> None:
        with self.tx():
            for oid, d in rows:
                self.conn.execute(
                    "INSERT INTO entry_evaluations VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), run_id, oid, d["candidate_key"], d["decision"],
                     json.dumps(d["reason_codes"]), json.dumps(d, sort_keys=True), d["evaluated_at_utc"]),
                )

    def record_data_issue(self, ticker: str, kind: str, message: str, *, run_id: str = "", source: str = "scan") -> None:
        with self.tx():
            self._audit("DATA_ISSUE", "system", None, {"ticker": ticker, "kind": kind, "message": message[:300],
                                                       "run_id": run_id}, source)

    def mark_digest_sent(self, local_date: date, *, source: str = "scan") -> None:
        """Called only by a real send path (a later slice); dry-run never marks."""
        with self.tx():
            self._audit("NO_TRADE_DIGEST_SENT", "system", None, {"date": local_date.isoformat()}, source)

    def digest_sent_on(self, local_date: date) -> bool:
        rows = self.conn.execute("SELECT after_json FROM audit_log WHERE event_type='NO_TRADE_DIGEST_SENT'").fetchall()
        return any(json.loads(r[0]).get("date") == local_date.isoformat() for r in rows)

    def sync_approved(self, approved: list[ApprovedUnderlying], *, actor: str = "owner", source: str = "cli") -> None:
        """Make the DB whitelist equal the owner's file: upsert listed names, delete (audited) the rest."""
        keep = {a.ticker for a in approved}
        existing = {x.ticker: x for x in self.list_approved()}
        for a in approved:
            if existing.get(a.ticker) != a.model_copy(update={"standard_deliverable_confirmed_on": None}):
                self.upsert_approved(a, actor=actor, source=source)
        with self.tx():
            for r in self.conn.execute("SELECT * FROM approved_underlyings").fetchall():
                if r["ticker"] not in keep:
                    self.conn.execute("DELETE FROM approved_underlyings WHERE ticker=?", (r["ticker"],))
                    self._audit("APPROVAL_REMOVED", actor, dict(r), None, source)

    # ---- manual commands, cash flows, atomic assignment ------------------
    def audit_manual(self, command: str, actor: str, reason: str, args: dict[str, Any], source: str = "cli") -> None:
        with self.tx():
            self._audit("MANUAL_COMMAND", actor, None, {"command": command, "reason": reason, "args": args}, source)

    def audit_event(self, event_type: str, actor: str, after: Any, source: str, before: Any = None) -> None:
        """Append one audit record inside the caller's transaction (web recording path: idempotency + breach flags)."""
        with self.tx():
            self._audit(event_type, actor, before, after, source)

    def audit_find(self, event_type: str, key: str, value: str) -> dict[str, Any] | None:
        """Newest audit record of `event_type` whose after_json[key] == value (e.g. an idempotency key), or None."""
        for r in self.conn.execute("SELECT after_json FROM audit_log WHERE event_type=? ORDER BY rowid DESC", (event_type,)):
            d = json.loads(r[0]) if r[0] else {}
            if d.get(key) == value:
                return d
        return None

    def record_cash_flow(self, amount: Decimal, effective: date, note: str, *, actor: str = "owner", source: str = "cli") -> None:
        """IT-008: external deposit(+)/withdrawal(-) from platform history. Shifts the drawdown peak (see drawdown.py);
        counts as a ledger change, so a new confirmed snapshot is required afterwards."""
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount == 0:
            raise JournalError("cash flow amount must be a non-zero finite Decimal (+deposit / -withdrawal)")
        if not note.strip():
            raise JournalError("a note is required (where the flow appears in platform history)")
        if effective > self.clock().date() + timedelta(days=1):
            raise JournalError("cash flow date is in the future")
        with self.tx():
            self._audit("CASH_FLOW", actor, None, {"amount": fmt(amount), "effective_date": effective.isoformat(), "note": note}, source)

    def list_cash_flows(self) -> tuple[tuple[datetime, Decimal, int], ...]:
        out = []
        # A flow applies from the moment it was RECORDED: snapshots taken earlier keep the level they had, so a
        # flow can never retroactively clear or downgrade a latched level (IT-008). The audit rowid is the third item:
        # it orders a flow against a snapshot recorded in the SAME second (SR-06).
        for r in self.conn.execute("SELECT created_at_utc, after_json, rowid FROM audit_log WHERE event_type='CASH_FLOW' ORDER BY rowid"):
            out.append((from_iso(r[0]), dec(json.loads(r[1])["amount"]), r[2]))
        return tuple(out)

    def complete_assignment(self, trade_id: str, lot: StockLot, *, fees_usd: Decimal = Decimal("0"),
                            actor: str = "owner", source: str = "cli") -> PutPosition:
        """F4 step 2, ATOMIC: record shares received + cash paid and move ASSIGNED_PENDING_RECONCILIATION ->
        ASSIGNED_RECONCILED in one transaction (no moment where collateral and shares are both counted or both missing)."""
        for name, v in (("shares", lot.shares), ("reference_price", lot.reference_price), ("fees", fees_usd)):
            if not isinstance(v, Decimal) or not v.is_finite() or v < 0 or (name != "fees" and v == 0):
                raise JournalError(f"{name} must be a finite positive Decimal")
        if lot.cost_basis_reference_usd is None or lot.cost_basis_reference_usd <= 0:
            raise JournalError("cash paid for the shares (cost_basis_reference_usd) is required")
        if lot.reference_asof_utc > self.clock() + timedelta(minutes=5):  # SR-08, same guard as add_stock_lot
            raise JournalError("stock mark is dated in the future")
        with self.tx():
            pos = self.get_position(trade_id)
            if pos.state is not PositionState.ASSIGNED_PENDING_RECONCILIATION:
                raise TransitionError(f"{trade_id} is {pos.state.value}; step 1 (record the assignment) must come first")
            if lot.ticker != pos.ticker or lot.shares != pos.contracts * pos.multiplier:
                raise JournalError("lot ticker/shares must match the assigned put (contracts x multiplier)")
            try:
                self.conn.execute(
                    "INSERT INTO stock_lots VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (lot.lot_id, lot.ticker, fmt(lot.shares), fmt(lot.reference_price), to_iso(lot.reference_asof_utc),
                     fmt(lot.cost_basis_reference_usd), "ASSIGNMENT", trade_id, lot.sector, lot.correlation_bucket))
            except sqlite3.IntegrityError as exc:
                raise JournalError(f"cannot record lot: {exc}") from exc
            self.conn.execute("UPDATE option_positions SET state=?, fees_usd=? WHERE trade_id=?",
                              (PositionState.ASSIGNED_RECONCILED.value, fmt(pos.fees_usd + fees_usd), trade_id))
            self._audit("STOCK_LOT_ADDED", actor, None, {"lot_id": lot.lot_id, "ticker": lot.ticker, "shares": fmt(lot.shares),
                                                        "linked_trade_id": trade_id}, source)
            self._audit("POSITION_TRANSITION", actor, {"trade_id": trade_id, "state": pos.state.value},
                        {"trade_id": trade_id, "state": "ASSIGNED_RECONCILED", "fees_usd": fmt(pos.fees_usd + fees_usd)}, source)
        return self.get_position(trade_id)

    # ---- run status (F1/F6) and Telegram pin -------------------------
    def record_run(self, run_id: str, kind: str, started_at: datetime, status: str,
                   reasons: list[str] | None = None, summary: dict[str, Any] | None = None) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT OR REPLACE INTO run_status VALUES (?,?,?,?,?,?)",
                (run_id, kind, to_iso(started_at), status, json.dumps(sorted(reasons or [])),
                 json.dumps(summary or {}, sort_keys=True, default=str)))

    def last_run(self, kind: str, statuses: set[str] | None = None) -> dict[str, Any] | None:
        rows = self.conn.execute(
            "SELECT * FROM run_status WHERE kind=? ORDER BY started_at_utc DESC, rowid DESC", (kind,)).fetchall()
        for r in rows:
            if statuses is None or r["status"] in statuses:
                d = dict(r)
                d["started_at"] = from_iso(d["started_at_utc"])
                d["reasons"] = json.loads(d["reasons_json"])
                return d
        return None

    def pin_chat(self, chat_id_sha256: str, chat_type: str, *, actor: str = "owner", source: str = "cli") -> None:
        with self.tx():
            self._audit("TELEGRAM_CHAT_PINNED", actor, None, {"chat_id_sha256": chat_id_sha256, "chat_type": chat_type}, source)

    def pinned_chat(self) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT after_json FROM audit_log WHERE event_type='TELEGRAM_CHAT_PINNED' ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(r[0]) if r else None

    # ---- book ------------------------------------------------------------
    def last_ledger_change(self) -> datetime | None:
        q = ",".join("?" * len(LEDGER_EVENT_TYPES))
        r = self.conn.execute(f"SELECT MAX(created_at_utc) FROM audit_log WHERE event_type IN ({q})", LEDGER_EVENT_TYPES).fetchone()
        return _ts(r[0])

    def ledger_seqs(self) -> tuple[int, int]:
        """(latest ledger-event rowid, latest ledger-or-snapshot rowid). Monotonic, so it changes on every ledger event
        even when timestamps tie within one second (SR-07)."""
        q = ",".join("?" * len(LEDGER_EVENT_TYPES))
        a = self.conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM audit_log WHERE event_type IN ({q})", LEDGER_EVENT_TYPES).fetchone()[0]
        b = self.conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM audit_log WHERE event_type IN ({q},'SNAPSHOT_ADDED')",
                              LEDGER_EVENT_TYPES).fetchone()[0]
        return a, b

    def confirm_baseline(self, equity_usd: Decimal, *, note: str = "", actor: str = "owner", source: str = "cli") -> None:
        """Owner-confirmed starting equity (seeds the drawdown peak). Audited; recorded once.
        Later re-baselining must go through record_risk_reset."""
        if not isinstance(equity_usd, Decimal) or not equity_usd.is_finite() or equity_usd <= 0:
            raise JournalError("baseline equity must be a positive finite Decimal")
        with self.tx():
            if self.conn.execute("SELECT 1 FROM audit_log WHERE event_type='BASELINE_CONFIRMED'").fetchone():
                raise JournalError("a baseline is already confirmed; use record_risk_reset to re-baseline")
            self._audit("BASELINE_CONFIRMED", actor, None,
                        {"equity_usd": fmt(equity_usd), "confirmed_at_utc": to_iso(self.clock()), "note": note}, source)

    def baseline(self) -> tuple[Decimal, datetime] | None:
        r = self.conn.execute("SELECT after_json FROM audit_log WHERE event_type='BASELINE_CONFIRMED' ORDER BY rowid LIMIT 1").fetchone()
        if not r:
            return None
        d = json.loads(r[0])
        return dec(d["equity_usd"]), from_iso(d["confirmed_at_utc"])

    def last_risk_reset_baseline(self) -> datetime | None:
        r = self.conn.execute("SELECT after_json FROM audit_log WHERE event_type='RISK_RESET' ORDER BY rowid DESC LIMIT 1").fetchone()
        return from_iso(json.loads(r[0])["baseline_asof_utc"]) if r else None

    def load_book(self, approved: "list[ApprovedUnderlying] | None" = None) -> BookState:
        """`approved`: the approvals file contents, used read-only as the fallback for the asset type of a ticker the journal's
        approved_underlyings table does not have yet (see policy.approved.resolve_asset_type)."""
        if approved is not None:
            self.approval_file_types = asset_type_map(approved)
        seq_change, revision = self.ledger_seqs()
        return BookState(
            positions=tuple(self.list_positions()),
            stock_lots=tuple(self.list_stock_lots()),
            snapshots=tuple(self.list_snapshots()),
            last_ledger_change_utc=self.last_ledger_change(),
            last_risk_reset_utc=self.last_risk_reset_baseline(),
            cash_flows=self.list_cash_flows(),
            baseline_equity_usd=(self.baseline() or (None, None))[0],
            baseline_confirmed_at_utc=(self.baseline() or (None, None))[1],
            last_ledger_change_seq=seq_change,
            ledger_revision=revision,
            policy_breaches=self.policy_breaches(),
        )

    def label_rows(self, ticker: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """(positions, lots) of `ticker` whose sector/correlation labels the exposure rules read: puts that reserve collateral or
        await assignment reconciliation, and every stock lot. Each item: id, sector, bucket (as stored)."""
        states = sorted(s.value for s in COLLATERAL_STATES | {PositionState.ASSIGNED_PENDING_RECONCILIATION})
        q = ",".join("?" * len(states))
        pos = [{"id": r["trade_id"], "sector": r["sector"], "bucket": r["correlation_bucket"]} for r in self.conn.execute(
            f"SELECT trade_id, sector, correlation_bucket FROM option_positions WHERE ticker=? AND state IN ({q}) ORDER BY trade_id",
            (ticker, *states))]
        lots = [{"id": r["lot_id"], "sector": r["sector"], "bucket": r["correlation_bucket"]} for r in self.conn.execute(
            "SELECT lot_id, sector, correlation_bucket FROM stock_lots WHERE ticker=? ORDER BY lot_id", (ticker,))]
        return pos, lots

    def correct_labels(self, ticker: str, sector: str, bucket: str, *, actor: str, source: str, reason: str, idempotency_key: str) -> int:
        """Owner-confirmed correction of the sector / correlation-bucket labels on the rows the exposure rules read (see
        `label_rows`). Writes the labels on those rows and ONE audit event LABEL_CORRECTED holding every previous value, in the
        caller's transaction. Not a ledger event: commitments are unchanged, so no new check-in is demanded. Returns rows changed."""
        sector, bucket = norm_label(sector), norm_label(bucket)
        if not sector or not bucket:
            raise JournalError("sector and correlation_bucket are required")
        with self.tx():
            pos, lots = self.label_rows(ticker)
            if not pos and not lots:
                raise JournalError("no held rows carry labels for that ticker")
            states = sorted(s.value for s in COLLATERAL_STATES | {PositionState.ASSIGNED_PENDING_RECONCILIATION})
            q = ",".join("?" * len(states))
            self.conn.execute(f"UPDATE option_positions SET sector=?, correlation_bucket=? WHERE ticker=? AND state IN ({q})",
                              (sector, bucket, ticker, *states))
            self.conn.execute("UPDATE stock_lots SET sector=?, correlation_bucket=? WHERE ticker=?", (sector, bucket, ticker))
            self._audit("LABEL_CORRECTED", actor, {"ticker": ticker, "positions": pos, "lots": lots},
                        {"ticker": ticker, "sector": sector, "correlation_bucket": bucket, "reason": reason,
                         "idempotency_key": idempotency_key}, source)
            return len(pos) + len(lots)

    def policy_breaches(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """(trade_id, codes) for every owner-reported trade that was recorded while breaching a policy rule (web recording
        path, audit event POLICY_BREACH_RECORDED) and has not been cleared since (POLICY_BREACH_CLEARED, written only when the
        breach no longer reproduces). The latest event per trade wins, in journal order."""
        out: dict[str, tuple[str, ...]] = {}
        for r in self.conn.execute("SELECT event_type, after_json FROM audit_log WHERE event_type IN "
                                   "('POLICY_BREACH_RECORDED','POLICY_BREACH_CLEARED') ORDER BY rowid"):
            d = json.loads(r[1])
            if r[0] == "POLICY_BREACH_CLEARED":
                out.pop(str(d.get("trade_id")), None)
            else:
                out[str(d.get("trade_id"))] = tuple(str(c) for c in d.get("codes", ()))
        return tuple(out.items())
