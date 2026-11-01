"""Closed list of the alert-delivery reasons the application itself writes into the outbox evidence (`last_error`).

The dashboard renders a stored reason ONLY if it equals one of these exactly (or is "entries now blocked: " followed by
nothing but known reason codes). Anything else, such as transport text from a provider, an exception, a restored backup or a
hand edit, is shown as WITHHELD without being parsed. This is an allowlist, not a redactor (see DECISIONS item 133)."""
from __future__ import annotations

from csp_alerts.policy.reasons import Reason

WITHHELD = "older record, text withheld"
CANNED_REASONS = frozenset({
    "process stopped while sending; delivery unknown",
    "an earlier part of this message was not delivered",
    "expired before send",
    "timeout after request; delivery unknown",
    "position reference missing; cannot confirm the position is still open",
    "position no longer exists in the journal",
    "position terms or opening evidence changed since queued",
    "profit-exit quote evidence is older than two monitor intervals or untimed",
    "policy revision changed since queued",
    "owner approval record changed or removed since queued",
    "ledger or snapshot changed since queued",
})
_BLOCKED = "entries now blocked: "
_CODES = frozenset(r.value for r in Reason)


def display_reason(value: object) -> str | None:
    """None for no reason; the value itself when it is a known application reason; otherwise WITHHELD."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return WITHHELD
    if value in CANNED_REASONS:
        return value
    if value.startswith(_BLOCKED):
        codes = value[len(_BLOCKED):].split(",")
        if 0 < len(codes) <= 40 and all(c in _CODES for c in codes):
            return value
    return WITHHELD
