"""Decimal helpers. All finance values are Decimal; floats are rejected at boundaries."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")


class MoneyError(ValueError):
    pass


def dec(value: object, *, allow_nonfinite: bool = False) -> Decimal:
    """Strict conversion: accepts Decimal, int (not bool), or str. Rejects float/bool/None."""
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise MoneyError(f"refusing to convert {type(value).__name__} to Decimal (use a string)")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, str)):
        try:
            d = Decimal(value.strip() if isinstance(value, str) else value)
        except InvalidOperation as exc:
            raise MoneyError(f"not a decimal: {value!r}") from exc
    else:
        raise MoneyError(f"unsupported type for Decimal: {type(value).__name__}")
    if not allow_nonfinite and not d.is_finite():
        raise MoneyError(f"non-finite decimal: {value!r}")
    return d


def fmt(d: Decimal) -> str:
    """Canonical string for storage/JSON (plain notation, no exponent)."""
    return format(d, "f")


def usd2(d: Decimal) -> str:
    """Money text with a leading sign and two decimals: Decimal('-1500') -> '-$1,500.00' (never '$-1,500.00')."""
    q = d.quantize(Decimal("0.01"))
    return f"-${abs(q):,.2f}" if q < 0 else f"${abs(q):,.2f}"        # abs() also removes a negative zero
