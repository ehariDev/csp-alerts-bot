"""Strict CSV import/export of manually recorded put positions (spec 5, 15.2).

Rules: header must match exactly; each row is validated independently; invalid or
contradictory rows go to quarantine (never silently corrected); valid rows are written in
one transaction, or not at all on --dry-run. Existing trade_ids are never overwritten.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from csp_alerts.journal.models import PositionState, from_iso, to_iso
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.money import MoneyError, dec, fmt

COLUMNS = (
    "trade_id", "account_scope", "paper", "ticker", "expiration", "strike", "multiplier", "contracts",
    "sector", "correlation_bucket", "state", "opened_at_utc", "actual_open_credit_per_share",
    "actual_close_debit_per_share", "fees_usd", "closed_at_utc", "notes",
)

_FILLED_STATES = {
    PositionState.OPEN, PositionState.CLOSE_WORKING, PositionState.CLOSED, PositionState.EXPIRED,
    PositionState.ASSIGNED_PENDING_RECONCILIATION, PositionState.ASSIGNED_RECONCILED,
    PositionState.ADJUSTMENT_REVIEW,
}


class CsvFormatError(ValueError):
    """The whole file is unusable (bad header, unreadable)."""


@dataclass
class QuarantinedRow:
    row_number: int
    raw: dict[str, str]
    errors: list[str]


@dataclass
class ImportReport:
    source: str
    dry_run: bool
    accepted: list[str] = field(default_factory=list)
    quarantined: list[QuarantinedRow] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.quarantined


def _opt_dec(raw: str, name: str, errors: list[str], *, allow_zero: bool = True) -> Decimal | None:
    raw = raw.strip()
    if raw == "":
        return None
    try:
        d = dec(raw)
    except MoneyError:
        errors.append(f"{name}: not a finite decimal")
        return None
    if d < 0 or (not allow_zero and d == 0):
        errors.append(f"{name}: must be {'> 0' if not allow_zero else '>= 0'}")
        return None
    return d


def _opt_ts(raw: str, name: str, errors: list[str]) -> datetime | None:
    raw = raw.strip()
    if raw == "":
        return None
    if not raw.endswith("Z"):
        errors.append(f"{name}: must be UTC ISO-8601 ending in Z")
        return None
    try:
        return from_iso(raw)
    except ValueError:
        errors.append(f"{name}: invalid timestamp")
        return None


def _int(raw: str, name: str, errors: list[str]) -> int | None:
    raw = raw.strip()
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        errors.append(f"{name}: must be a positive integer")
        return None
    return int(raw)


def parse_row(row: dict[str, str]) -> tuple[tuple[NewPosition, PositionState, dict[str, str]] | None, list[str]]:
    errors: list[str] = []
    for c in ("trade_id", "ticker", "sector", "correlation_bucket", "account_scope"):
        if not row[c].strip():
            errors.append(f"{c}: required")
    paper_raw = row["paper"].strip().lower()
    if paper_raw not in ("0", "1", "true", "false"):
        errors.append("paper: must be 0/1/true/false")
    try:
        state = PositionState(row["state"].strip())
    except ValueError:
        errors.append("state: unknown state")
        state = None  # type: ignore[assignment]
    try:
        expiration = date.fromisoformat(row["expiration"].strip())
    except ValueError:
        errors.append("expiration: must be YYYY-MM-DD")
        expiration = None  # type: ignore[assignment]
    strike = _opt_dec(row["strike"], "strike", errors, allow_zero=False)
    if strike is None and not any(e.startswith("strike") for e in errors):
        errors.append("strike: required")
    multiplier = _int(row["multiplier"], "multiplier", errors)
    contracts = _int(row["contracts"], "contracts", errors)
    opened = _opt_ts(row["opened_at_utc"], "opened_at_utc", errors)
    closed = _opt_ts(row["closed_at_utc"], "closed_at_utc", errors)
    credit = _opt_dec(row["actual_open_credit_per_share"], "actual_open_credit_per_share", errors)
    debit = _opt_dec(row["actual_close_debit_per_share"], "actual_close_debit_per_share", errors)
    fees = _opt_dec(row["fees_usd"], "fees_usd", errors)

    if state is not None and not errors:
        if state in _FILLED_STATES and (credit is None or credit == 0 or opened is None):
            errors.append(f"state {state.value} requires actual_open_credit_per_share > 0 and opened_at_utc")
        if state is PositionState.CLOSED and (debit is None or closed is None):
            errors.append("state CLOSED requires actual_close_debit_per_share and closed_at_utc")
        if state not in (PositionState.CLOSED,) and debit is not None:
            errors.append(f"state {state.value} must not have a closing debit")
        if state in (PositionState.EXPIRED,) and closed is None:
            errors.append("state EXPIRED requires closed_at_utc")
        if state in (PositionState.DRAFT, PositionState.PENDING, PositionState.WORKING, PositionState.CANCELED) and (
            credit is not None or opened is not None or closed is not None
        ):
            errors.append(f"state {state.value} must not carry fill data")
        if closed is not None and state not in (
            PositionState.CLOSED, PositionState.EXPIRED, PositionState.ASSIGNED_PENDING_RECONCILIATION,
            PositionState.ASSIGNED_RECONCILED, PositionState.CANCELED,
        ):
            errors.append(f"state {state.value} must not have closed_at_utc")
        if state is PositionState.EXPIRED and closed is not None and expiration is not None and closed.date() < expiration:
            errors.append("EXPIRED closed_at_utc is before the expiration date")
        if opened is not None and closed is not None and closed < opened:
            errors.append("closed_at_utc is before opened_at_utc")

    if errors:
        return None, errors
    assert strike and multiplier and contracts and expiration and state
    new = NewPosition(
        trade_id=row["trade_id"].strip(), ticker=row["ticker"].strip().upper(), expiration=expiration,
        strike=strike, multiplier=multiplier, contracts=contracts, sector=row["sector"].strip(),
        correlation_bucket=row["correlation_bucket"].strip(), account_scope=row["account_scope"].strip(),
        paper=paper_raw in ("1", "true"), notes=row["notes"],
    )
    extra = {
        "opened_at_utc": None if opened is None else to_iso(opened),
        "actual_open_credit_per_share": None if credit is None else fmt(credit),
        "actual_close_debit_per_share": None if debit is None else fmt(debit),
        "fees_usd": fmt(fees if fees is not None else Decimal("0")),
        "closed_at_utc": None if closed is None else to_iso(closed),
    }
    return (new, state, extra), []


def import_positions_csv(journal: Journal, path: Path | str, *, dry_run: bool = True, actor: str = "owner") -> ImportReport:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CsvFormatError(f"cannot read {path}: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != COLUMNS:
        raise CsvFormatError(f"header must be exactly: {','.join(COLUMNS)}")

    report = ImportReport(source=str(path), dry_run=dry_run)
    existing = {p.trade_id for p in journal.list_positions()}
    seen: set[str] = set()
    parsed: list[tuple[NewPosition, PositionState, dict[str, str]]] = []

    for line_no, row in enumerate(reader, start=2):
        if None in row or any(v is None for v in row.values()):
            report.quarantined.append(QuarantinedRow(line_no, {str(k): str(v) for k, v in row.items()}, ["wrong number of columns"]))
            continue
        result, errors = parse_row(row)
        if result is not None:
            tid = result[0].trade_id
            if tid in existing:
                errors.append("trade_id already exists in journal (imports never overwrite)")
            elif tid in seen:
                errors.append("duplicate trade_id within file")
        if errors or result is None:
            report.quarantined.append(QuarantinedRow(line_no, dict(row), errors))
            continue
        seen.add(result[0].trade_id)
        parsed.append(result)

    if not dry_run:
        with journal.tx():
            for new, state, extra in parsed:
                journal._insert_position(new, state, extra)
                journal._audit("POSITION_IMPORTED", actor, None,
                               {"trade_id": new.trade_id, "state": state.value, "source": str(path)}, "csv_import")
            for q in report.quarantined:
                journal.conn.execute(
                    "INSERT INTO import_quarantine VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), to_iso(journal.clock()), str(path), q.row_number,
                     json.dumps(q.raw, sort_keys=True), json.dumps(q.errors)),
                )
            if report.quarantined:
                journal._audit("CSV_IMPORT_QUARANTINE", actor, None,
                               {"source": str(path), "rows": [q.row_number for q in report.quarantined]}, "csv_import")
    report.accepted = [n.trade_id for n, _, _ in parsed]
    return report


def write_quarantine_csv(report: ImportReport, dest: Path | str) -> None:
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["row_number", "errors", "raw_json"])
        for q in report.quarantined:
            w.writerow([q.row_number, "; ".join(q.errors), json.dumps(q.raw, sort_keys=True)])


def export_positions_csv(journal: Journal, dest: Path | str) -> int:
    positions = journal.list_positions()
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for p in positions:
            w.writerow([
                p.trade_id, p.account_scope, int(p.paper), p.ticker, p.expiration.isoformat(), fmt(p.strike),
                p.multiplier, p.contracts, p.sector, p.correlation_bucket, p.state.value,
                "" if p.opened_at_utc is None else to_iso(p.opened_at_utc),
                "" if p.actual_open_credit_per_share is None else fmt(p.actual_open_credit_per_share),
                "" if p.actual_close_debit_per_share is None else fmt(p.actual_close_debit_per_share),
                fmt(p.fees_usd), "" if p.closed_at_utc is None else to_iso(p.closed_at_utc), p.notes,
            ])
    return len(positions)
