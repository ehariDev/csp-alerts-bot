"""Collateral and concentration math (spec 4.1, 4.5). Pure and deterministic; Decimal only.

Formulas
  put_collateral_usd        = sum(strike * multiplier * contracts) over PENDING/WORKING/OPEN/CLOSE_WORKING
                              (+ ADJUSTMENT_REVIEW, conservatively) puts. A close order does not release collateral.
  owned_stock_reference_usd = sum(shares * reference_price)
  assigned_pending_usd      = sum(strike * multiplier * contracts) over ASSIGNED_PENDING_RECONCILIATION puts
                              (shares owed but not yet recorded; never counted as zero)
  aggregate_equity_risk_usd = put_collateral_usd + owned_stock_reference_usd + assigned_pending_usd
                              (an exposure BUDGET, not max loss)
Per-ticker / sector / correlation-bucket exposure = put collateral + owned stock for that key.
Free cash is the owner-entered figure from the latest snapshot and is taken as ALREADY net of
collateral reserved by existing orders (see docs/DECISIONS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from csp_alerts.journal.models import BookState, PositionState, PutPosition, StockLot, norm_label
from csp_alerts.policy.config import Policy
from csp_alerts.policy.reasons import Reason

ZERO = Decimal("0")


@dataclass(frozen=True)
class ProposedPut:
    ticker: str
    asset_type: str
    sector: str
    correlation_bucket: str
    expiration: date
    strike: Decimal
    multiplier: int
    contracts: int = 1

    @property
    def collateral_usd(self) -> Decimal:
        return self.strike * self.multiplier * self.contracts


@dataclass(frozen=True)
class Exposure:
    put_collateral_usd: Decimal
    owned_stock_usd: Decimal
    active_contracts: int
    assigned_pending_usd: Decimal = Decimal("0")  # SR-03: strike x multiplier x contracts of puts awaiting reconciliation

    @property
    def aggregate_equity_risk_usd(self) -> Decimal:
        return self.put_collateral_usd + self.owned_stock_usd + self.assigned_pending_usd


def is_company(asset_type: str) -> bool:
    """Only an asset type known to be ETF is exempt from the individual-company name cap. STOCK and UNKNOWN (or anything
    unrecognised) count: a name whose concentration cannot be assessed is treated conservatively."""
    return asset_type != "ETF"


def _active_puts(book: BookState) -> list[PutPosition]:
    return [p for p in book.positions if p.reserves_collateral]


def compute_exposure(book: BookState) -> Exposure:
    puts = _active_puts(book)
    return Exposure(
        put_collateral_usd=sum((p.collateral_usd for p in puts), ZERO),
        owned_stock_usd=sum((l.exposure_usd for l in book.stock_lots), ZERO),
        active_contracts=sum(p.contracts for p in puts),
        # a linked stock lot already carries the exposure (counted once, never both)
        assigned_pending_usd=sum((p.collateral_usd for p in book.positions
                                  if p.state is PositionState.ASSIGNED_PENDING_RECONCILIATION
                                  and not any(l.linked_trade_id == p.trade_id for l in book.stock_lots)), ZERO),
    )


def iso_week(d: date) -> tuple[int, int]:
    y, w, _ = d.isocalendar()
    return (y, w)


def _key_exposure(puts: list[PutPosition], lots: tuple[StockLot, ...], attr: str, value: str) -> Decimal:
    value = norm_label(value)  # labels are owner-typed free text: compare strip+casefold
    return (
        sum((p.collateral_usd for p in puts if norm_label(getattr(p, attr)) == value), ZERO)
        + sum((l.exposure_usd for l in lots if norm_label(getattr(l, attr)) == value), ZERO)
    )


def check_portfolio_limits(book: BookState, proposed: ProposedPut, policy: Policy, free_cash_usd: Decimal | None) -> set[Reason]:
    """Return the reason codes for every portfolio rule the proposed put would violate."""
    pf = policy.portfolio
    new = proposed.collateral_usd
    puts = _active_puts(book)
    exp = compute_exposure(book)
    out: set[Reason] = set()

    # Aggregate caps: (a) aggregate equity risk <= normal ceiling, (b) put collateral <= normal ceiling.
    ceiling = pf.normal_new_entry_exposure_ceiling_usd
    if exp.aggregate_equity_risk_usd + new > ceiling or exp.put_collateral_usd + new > ceiling:
        out.add(Reason.NORMAL_EXPOSURE_LIMIT)
    # Pilot: put collateral only.
    if policy.pilot.enabled and exp.put_collateral_usd + new > policy.pilot.max_deployed_put_collateral_usd:
        out.add(Reason.PILOT_COLLATERAL_LIMIT)

    if _key_exposure(puts, book.stock_lots, "ticker", proposed.ticker) + new > pf.max_underlying_exposure_usd:
        out.add(Reason.SINGLE_TICKER_LIMIT)
    if _key_exposure(puts, book.stock_lots, "sector", proposed.sector) + new > pf.max_sector_exposure_usd:
        out.add(Reason.SECTOR_LIMIT)
    if _key_exposure(puts, book.stock_lots, "correlation_bucket", proposed.correlation_bucket) + new > pf.max_correlated_bucket_usd:
        out.add(Reason.CORRELATED_LIMIT)

    week = iso_week(proposed.expiration)
    week_total = sum((p.collateral_usd for p in puts if iso_week(p.expiration) == week), ZERO)
    if week_total + new > pf.max_expiry_week_collateral_usd:
        out.add(Reason.EXPIRY_WEEK_LIMIT)

    same_ticker_contracts = sum(p.contracts for p in puts if p.ticker == proposed.ticker)
    if (
        exp.active_contracts + proposed.contracts > pf.max_active_short_put_contracts
        or same_ticker_contracts + proposed.contracts > pf.max_contracts_per_underlying
        or (policy.entries.one_contract_per_alert and proposed.contracts != 1)
    ):
        out.add(Reason.CONTRACT_COUNT_LIMIT)

    # Individual companies: tickers with active puts or stock holdings (ETFs counted separately).
    names = {p.ticker for p in puts if is_company(p.asset_type)} | {l.ticker for l in book.stock_lots if is_company(l.asset_type)}
    if is_company(proposed.asset_type) and proposed.ticker not in names and len(names) + 1 > pf.max_individual_company_names:
        out.add(Reason.INDIVIDUAL_NAME_LIMIT)

    if free_cash_usd is None or free_cash_usd < new:
        out.add(Reason.INSUFFICIENT_FREE_CASH)
    return out


def emergency_breached(book: BookState, policy: Policy) -> bool:
    """Incident flag: current aggregate exposure above the emergency ceiling. Never entry headroom."""
    return compute_exposure(book).aggregate_equity_risk_usd > policy.portfolio.emergency_absolute_exposure_ceiling_usd


@dataclass(frozen=True)
class CapRow:
    """One capacity line: `limit` vs the figure before/after adding the proposed put. unit is USD | contracts | names."""
    key: str
    label: str
    unit: str
    limit: Decimal | None
    used_before: Decimal | None
    added: Decimal
    used_after: Decimal | None
    room_after: Decimal | None   # limit - used_after (negative = over the limit); None when the figure is unknown
    breached: bool
    reason: Reason


def capacity_rows(book: BookState, proposed: ProposedPut, policy: Policy, free_cash_usd: Decimal | None) -> list[CapRow]:
    """Per-rule capacity for ONE proposed put, using exactly the figures `check_portfolio_limits` compares.

      used_after = used_before + added          room_after = limit - used_after          breached = used_after > limit
    (free cash: used_after = proposed collateral, limit = latest NET free cash; breached when cash < collateral.)
    Every row's `reason` is the code `check_portfolio_limits` emits when that row is breached (tested for agreement).
    Display data only: this function decides nothing."""
    pf = policy.portfolio
    new = proposed.collateral_usd
    puts = _active_puts(book)
    exp = compute_exposure(book)
    rows: list[CapRow] = []

    def usd(key: str, label: str, limit: Decimal, before: Decimal, reason: Reason) -> None:
        after = before + new
        rows.append(CapRow(key, label, "USD", limit, before, new, after, limit - after, after > limit, reason))

    if policy.pilot.enabled:
        usd("pilot", "Pilot put collateral", policy.pilot.max_deployed_put_collateral_usd, exp.put_collateral_usd,
            Reason.PILOT_COLLATERAL_LIMIT)
    usd("normal_put", "Put collateral vs normal ceiling", pf.normal_new_entry_exposure_ceiling_usd, exp.put_collateral_usd,
        Reason.NORMAL_EXPOSURE_LIMIT)
    usd("normal_aggregate", "Aggregate equity risk vs normal ceiling", pf.normal_new_entry_exposure_ceiling_usd,
        exp.aggregate_equity_risk_usd, Reason.NORMAL_EXPOSURE_LIMIT)
    usd("ticker", f"Ticker {proposed.ticker} (puts + shares)", pf.max_underlying_exposure_usd,
        _key_exposure(puts, book.stock_lots, "ticker", proposed.ticker), Reason.SINGLE_TICKER_LIMIT)
    usd("sector", "Sector (puts + shares)", pf.max_sector_exposure_usd,
        _key_exposure(puts, book.stock_lots, "sector", proposed.sector), Reason.SECTOR_LIMIT)
    usd("bucket", "Correlation bucket (puts + shares)", pf.max_correlated_bucket_usd,
        _key_exposure(puts, book.stock_lots, "correlation_bucket", proposed.correlation_bucket), Reason.CORRELATED_LIMIT)
    week = iso_week(proposed.expiration)
    usd("expiry_week", "Expiration-week put collateral", pf.max_expiry_week_collateral_usd,
        sum((p.collateral_usd for p in puts if iso_week(p.expiration) == week), ZERO), Reason.EXPIRY_WEEK_LIMIT)

    def cnt(key: str, label: str, limit: int, before: int, extra_breach: bool = False) -> None:
        after = before + proposed.contracts
        rows.append(CapRow(key, label, "contracts", Decimal(limit), Decimal(before), Decimal(proposed.contracts), Decimal(after),
                           Decimal(limit - after), after > limit or extra_breach, Reason.CONTRACT_COUNT_LIMIT))
    cnt("contracts", "Active short-put contracts", pf.max_active_short_put_contracts, exp.active_contracts)
    cnt("contracts_underlying", f"Contracts on {proposed.ticker}", pf.max_contracts_per_underlying,
        sum(p.contracts for p in puts if p.ticker == proposed.ticker),
        extra_breach=policy.entries.one_contract_per_alert and proposed.contracts != 1)
    names = {p.ticker for p in puts if is_company(p.asset_type)} | {l.ticker for l in book.stock_lots if is_company(l.asset_type)}
    adds = 1 if is_company(proposed.asset_type) and proposed.ticker not in names else 0
    rows.append(CapRow("names", "Individual-company names (puts + shares)", "names", Decimal(pf.max_individual_company_names),
                       Decimal(len(names)), Decimal(adds), Decimal(len(names) + adds),
                       Decimal(pf.max_individual_company_names - len(names) - adds),
                       adds == 1 and len(names) + adds > pf.max_individual_company_names, Reason.INDIVIDUAL_NAME_LIMIT))
    if free_cash_usd is None:
        rows.append(CapRow("cash", "Free cash (latest NET snapshot)", "USD", None, None, new, new, None, True,
                           Reason.INSUFFICIENT_FREE_CASH))
    else:
        rows.append(CapRow("cash", "Free cash (latest NET snapshot)", "USD", free_cash_usd, ZERO, new, new, free_cash_usd - new,
                           free_cash_usd < new, Reason.INSUFFICIENT_FREE_CASH))
    return rows


# ---------------------------------------------------------------- portfolio page (WT-05): totals and binding limits
@dataclass(frozen=True)
class GroupRow:
    """Exposure of ONE key within a dimension, with its limit. Same figures `check_portfolio_limits` / `capacity_rows` compare.

      exposure = put collateral + owned stock reference value for the key (expiry week: put collateral only)
      room     = limit - exposure          breached = exposure > limit          exhausted = room <= 0 (nothing can be added)
    """
    dimension: str            # ticker | sector | bucket | expiry_week
    key: str                  # normalized label / ticker / ISO week "YYYY-Www"
    put_collateral_usd: Decimal
    owned_stock_usd: Decimal
    exposure_usd: Decimal
    contracts: int
    limit: Decimal
    room_usd: Decimal
    breached: bool
    exhausted: bool
    unknown: bool             # the label is 'unknown': concentration cannot be assessed (never counted as diversified)
    reason: Reason


@dataclass(frozen=True)
class LimitRow:
    """One portfolio-wide limit: `used` against `limit` (unit USD | contracts | names)."""
    key: str
    label: str
    unit: str
    limit: Decimal
    used: Decimal
    room: Decimal
    breached: bool
    exhausted: bool
    reason: Reason


UNKNOWN_LABEL = "unknown"


def _put_collateral_for(puts: list[PutPosition], attr: str, key: str) -> Decimal:
    return sum((p.collateral_usd for p in puts if norm_label(getattr(p, attr)) == key), ZERO)


def exposure_groups(book: BookState, policy: Policy) -> list[GroupRow]:
    """Exposure per ticker, sector, correlation bucket and expiration week for everything currently held or reserved."""
    pf = policy.portfolio
    puts = _active_puts(book)
    rows: list[GroupRow] = []
    for dim, attr, limit, reason in (("ticker", "ticker", pf.max_underlying_exposure_usd, Reason.SINGLE_TICKER_LIMIT),
                                     ("sector", "sector", pf.max_sector_exposure_usd, Reason.SECTOR_LIMIT),
                                     ("bucket", "correlation_bucket", pf.max_correlated_bucket_usd, Reason.CORRELATED_LIMIT)):
        keys = sorted({norm_label(getattr(p, attr)) for p in puts} | {norm_label(getattr(l, attr)) for l in book.stock_lots})
        for k in keys:
            total = _key_exposure(puts, book.stock_lots, attr, k)        # exactly the figure the entry gate uses
            stock = sum((l.exposure_usd for l in book.stock_lots if norm_label(getattr(l, attr)) == k), ZERO)
            rows.append(GroupRow(dim, k, _put_collateral_for(puts, attr, k), stock, total,
                                 sum(p.contracts for p in puts if norm_label(getattr(p, attr)) == k), limit, limit - total,
                                 total > limit, limit - total <= 0, dim != "ticker" and k == UNKNOWN_LABEL, reason))
    for wk in sorted({iso_week(p.expiration) for p in puts}):
        coll = sum((p.collateral_usd for p in puts if iso_week(p.expiration) == wk), ZERO)
        rows.append(GroupRow("expiry_week", f"{wk[0]}-W{wk[1]:02d}", coll, ZERO, coll,
                             sum(p.contracts for p in puts if iso_week(p.expiration) == wk), pf.max_expiry_week_collateral_usd,
                             pf.max_expiry_week_collateral_usd - coll, coll > pf.max_expiry_week_collateral_usd,
                             pf.max_expiry_week_collateral_usd - coll <= 0, False, Reason.EXPIRY_WEEK_LIMIT))
    return rows


def company_names(book: BookState) -> tuple[str, ...]:
    """Individual-company tickers with an active put or stock holding (ETFs are counted separately), as the name cap counts them."""
    return tuple(sorted({p.ticker for p in _active_puts(book) if is_company(p.asset_type)}
                        | {l.ticker for l in book.stock_lots if is_company(l.asset_type)}))


def portfolio_limits(book: BookState, policy: Policy) -> list[LimitRow]:
    """Portfolio-wide limits with current usage (the same figures `check_portfolio_limits` compares before adding a put)."""
    pf = policy.portfolio
    exp = compute_exposure(book)
    puts = _active_puts(book)
    out: list[LimitRow] = []

    def add(key: str, label: str, unit: str, limit: Decimal, used: Decimal, reason: Reason) -> None:
        out.append(LimitRow(key, label, unit, limit, used, limit - used, used > limit, limit - used <= 0, reason))
    if policy.pilot.enabled:
        add("pilot", "Pilot put collateral", "USD", policy.pilot.max_deployed_put_collateral_usd, exp.put_collateral_usd,
            Reason.PILOT_COLLATERAL_LIMIT)
    add("normal_put", "Put collateral vs normal ceiling", "USD", pf.normal_new_entry_exposure_ceiling_usd, exp.put_collateral_usd,
        Reason.NORMAL_EXPOSURE_LIMIT)
    add("normal_aggregate", "Aggregate equity risk vs normal ceiling", "USD", pf.normal_new_entry_exposure_ceiling_usd,
        exp.aggregate_equity_risk_usd, Reason.NORMAL_EXPOSURE_LIMIT)
    add("contracts", "Active short-put contracts", "contracts", Decimal(pf.max_active_short_put_contracts),
        Decimal(exp.active_contracts), Reason.CONTRACT_COUNT_LIMIT)
    by_ticker: dict[str, int] = {}
    for p in puts:
        by_ticker[p.ticker] = by_ticker.get(p.ticker, 0) + p.contracts
    top_ticker, top = max(by_ticker.items(), key=lambda kv: (kv[1], kv[0]), default=(None, 0))
    add("contracts_underlying", "Contracts on the most-used underlying" + (f" ({top_ticker})" if top_ticker else ""),
        "contracts", Decimal(pf.max_contracts_per_underlying), Decimal(top), Reason.CONTRACT_COUNT_LIMIT)
    add("names", "Individual-company names (puts + shares)", "names", Decimal(pf.max_individual_company_names),
        Decimal(len(company_names(book))), Reason.INDIVIDUAL_NAME_LIMIT)
    return out
