"""Read-only Stockpile adapter (spec 3.1, section 16 P0-05).

Only these upstream callables are used: options_scanner.chain.fetch_chain (provider forced to 'yahoo')
and options_scanner.earnings.fetch_earnings_dates. The upstream checkout is added to sys.path HERE only
(appended, never prepended) and the options-scanner package is NOT pip-installed (it hard-depends on
broker SDKs). After loading, the broker boundary is re-asserted.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from csp_alerts import boundary
from csp_alerts.market import failures

MIN_DTE_MARGIN_DAYS = 2  # upstream filters DTE by the HOST date; fetch wider, the gate recomputes exactly


class SourceError(RuntimeError):
    """Provider failure. kind is a stable label. Built from a caught exception it carries a failure CODE and the canned
    message only (`from_exception`): the raw exception text is never copied into it."""

    def __init__(self, kind: str, message: str, code: str | None = None):
        self.kind = kind
        self.code = code
        super().__init__(f"{kind}: {message}")

    @classmethod
    def from_exception(cls, exc: BaseException, kind: str = "FETCH_ERROR") -> "SourceError":
        """Canned error for any exception, however hostile (a __str__ that raises or returns a non-str is tolerated)."""
        code = failures.classify(kind, exc)
        return cls(kind, failures.CANNED[code], code)


@dataclass(frozen=True)
class RawChain:
    ticker: str
    rows: list[dict[str, Any]]
    fetched_at_utc: datetime
    rows_seen_upstream: int | None = None
    notes: list[str] = field(default_factory=list)


class ChainSource(Protocol):
    """Small seam so tests inject fixtures; production uses StockpileSource."""

    def fetch_chain(self, ticker: str, min_dte: int, max_dte: int) -> RawChain: ...

    def fetch_earnings_dates(self, ticker: str) -> list[date] | None:
        """None or [] means UNKNOWN (never 'no earnings')."""
        ...


class StockpileSource:
    def __init__(self, stockpile_root: Path | str):
        self.root = Path(stockpile_root).resolve()
        self._fetch_chain = None
        self._fetch_earnings = None

    def _load(self) -> None:
        if self._fetch_chain is not None:
            return
        scanner, shared = self.root / "options-scanner", self.root / "shared"
        if not (scanner / "options_scanner" / "chain.py").is_file() or not (shared / "stocks_shared").is_dir():
            raise SourceError("STOCKPILE_MISSING", "pinned Stockpile checkout not found (check CSP_STOCKPILE_ROOT)")
        for p in (str(scanner), str(shared)):
            if p not in sys.path:
                sys.path.append(p)
        try:
            from options_scanner.chain import fetch_chain
            from options_scanner.earnings import fetch_earnings_dates
        except Exception as exc:  # noqa: BLE001
            raise SourceError.from_exception(exc, "STOCKPILE_IMPORT") from exc
        boundary.assert_no_broker_modules_loaded()
        self._fetch_chain, self._fetch_earnings = fetch_chain, fetch_earnings_dates

    def fetch_chain(self, ticker: str, min_dte: int, max_dte: int) -> RawChain:
        self._load()
        try:
            df = self._fetch_chain(ticker, opt_type="puts", min_dte=max(0, min_dte - MIN_DTE_MARGIN_DAYS),
                                   max_dte=max_dte + MIN_DTE_MARGIN_DAYS, provider="yahoo")
        except Exception as exc:  # noqa: BLE001 - classify, never leak
            kind = "RATE_LIMIT" if "RateLimit" in type(exc).__name__ else "FETCH_ERROR"
            raise SourceError.from_exception(exc, kind) from exc
        fetched = datetime.now(timezone.utc)
        rows = [] if df is None or len(df) == 0 else df.to_dict("records")
        boundary.assert_no_broker_modules_loaded()
        return RawChain(ticker, rows, fetched, None, ["rows_seen_upstream unavailable: Stockpile drops rows silently"])

    def fetch_expirations(self, ticker: str, max_dte: int = 120) -> list[date] | None:
        """Expirations (0..max_dte DTE) that have at least one quoted put, derived from the SAME upstream fetch_chain
        call (no extra upstream module). Read-only and heavier than a chain fetch, so ticker-check calls it only when
        the entry window has no rows. None = could not be read (UNKNOWN, never 'no expirations'). Indicative: an
        expiry whose puts are all unquoted is not listed here."""
        self._load()
        try:
            df = self._fetch_chain(ticker, opt_type="puts", min_dte=0, max_dte=max_dte, provider="yahoo")
        except Exception:  # noqa: BLE001
            return None
        boundary.assert_no_broker_modules_loaded()
        if df is None or len(df) == 0:
            return None
        out: set[date] = set()
        for e in df["expiration"].tolist():
            try:
                out.add(date.fromisoformat(str(e)[:10]))
            except ValueError:
                continue
        return sorted(out) or None

    def fetch_earnings_dates(self, ticker: str) -> list[date] | None:
        self._load()
        try:
            got = self._fetch_earnings(ticker)
        except Exception:  # noqa: BLE001
            return None
        out: list[date] = []
        for d in got or []:
            if hasattr(d, "date") and callable(d.date):
                d = d.date()
            if isinstance(d, date):
                out.append(d)
        return out or None
