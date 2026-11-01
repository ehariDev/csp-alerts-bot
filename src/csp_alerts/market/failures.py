"""Closed set of provider-failure codes with fixed canned messages (allowlist, not a redactor).

Provider/exception text is UNTRUSTED and may contain tokens, URLs, file paths or forged verdicts. This project therefore
never stores or renders it: a failure is mapped to one code from CODES and only the canned message for that code is ever
persisted, printed or shown on a page. The classifier reads the raw text only through plain substring tests (no regex) and
its output can never contain any part of the input. Raw text is not logged anywhere (the project has no provider-text
log path).

Stored form: "<CODE>: <canned message>". `display()` re-validates a stored value against this table; anything else (a
free-text row written by an older version) is shown as UNKNOWN_ERROR with the text withheld, without any parsing of it.
"""
from __future__ import annotations

RATE_LIMITED = "RATE_LIMITED"
NOT_FOUND_OR_DELISTED = "NOT_FOUND_OR_DELISTED"
NO_OPTIONS_LISTED = "NO_OPTIONS_LISTED"
TIMEOUT = "TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
PROVIDER_FORMAT_ERROR = "PROVIDER_FORMAT_ERROR"
PARTIAL_DATA = "PARTIAL_DATA"
UNKNOWN_ERROR = "UNKNOWN_ERROR"

CANNED: dict[str, str] = {
    RATE_LIMITED: "Provider is throttling; wait a few minutes and retry. Nothing was estimated.",
    NOT_FOUND_OR_DELISTED: ("The provider returned no price: the symbol may not exist (typo?), be delisted, or the provider has no data. "
                            "Check the spelling; nothing was estimated."),
    NO_OPTIONS_LISTED: ("No put rows were returned for the window (no listed expiry in range, an unknown symbol, or a throttled "
                        "response; these cannot be told apart). Nothing was estimated."),
    TIMEOUT: "The provider did not answer in time. Nothing was estimated; it will be retried later.",
    NETWORK_ERROR: "Network failure reaching the provider. Check your connection; nothing was estimated.",
    PROVIDER_FORMAT_ERROR: "The provider response could not be parsed or had an unexpected shape. Nothing was estimated.",
    PARTIAL_DATA: ("Only part of the data was usable (a missing stock price, a mostly unquoted chain, or an unknown exchange "
                   "calendar). Missing values are shown as unavailable."),
    UNKNOWN_ERROR: "The provider call failed for an unrecognised reason. Nothing was estimated.",
}
CODES = tuple(CANNED)
WITHHELD = f"{UNKNOWN_ERROR}: older record, text withheld"


def stored(code: str) -> str:
    """The only string ever persisted for a failure/partial status."""
    if code not in CANNED:
        code = UNKNOWN_ERROR
    return f"{code}: {CANNED[code]}"


KNOWN_KINDS = frozenset({"RATE_LIMIT", "STOCKPILE_MISSING", "STOCKPILE_IMPORT", "FETCH_ERROR", "TIMEOUT"})
SERVICE_ERROR = "SERVICE_ERROR"
SERVICE_ERROR_TEXT = "Unexpected internal error; error text deliberately not shown or stored. Check the PC logs."


def safe_kind(kind: object) -> str:
    """Our own failure label, or FETCH_ERROR for anything not on the closed list (a kind is never free text)."""
    return kind if isinstance(kind, str) and kind in KNOWN_KINDS else "FETCH_ERROR"


def service_failure_text() -> str:
    """The only text a job-level (SERVICE_FAILURE) message, run summary or stdout line may carry."""
    return f"{SERVICE_ERROR}: {SERVICE_ERROR_TEXT}"


def stored_for(exc: object) -> str:
    """'<CODE>: <canned message>' for ANY exception object, however hostile (a __str__ that raises or returns a non-str,
    a hostile `kind`). Never copies exception text; never raises."""
    try:
        kind = getattr(exc, "kind", None)
    except Exception:  # noqa: BLE001
        kind = None
    return stored(classify(safe_kind(kind), exc))


def classify(kind: str | None, text: object = None) -> str:
    """Map a failure to a code. `kind` is SourceError.kind (our own label); `text` is raw provider/exception text that is
    only inspected, never copied. Order matters: the first matching rule wins. An exception that already carries a valid
    `.code` (SourceError built by `from_exception`) keeps it."""
    if isinstance(text, BaseException):
        try:
            c = getattr(text, "code", None)
        except Exception:  # noqa: BLE001
            c = None
        if isinstance(c, str) and c in CANNED:
            return c
    k = kind if isinstance(kind, str) else ""
    try:
        t = str(text).lower() if text is not None else ""
    except Exception:  # noqa: BLE001 - a hostile __str__ must not break classification
        t = ""
    t = t[:2000]                                   # bound the substring scans
    if k == "RATE_LIMIT" or "ratelimit" in t or "rate limit" in t or "too many requests" in t or "429" in t:
        return RATE_LIMITED
    if "timeout" in t or "timed out" in t or "time out" in t:
        return TIMEOUT
    if "could not fetch live price" in t or "delisted" in t or "not found" in t or "no data found" in t or "404" in t:
        return NOT_FOUND_OR_DELISTED
    if "no options" in t or "no expiration" in t or "no put" in t:
        return NO_OPTIONS_LISTED
    if any(w in t for w in ("connection", "network", "ssl", "dns", "resolve", "unreachable", "socket", "name or service")):
        return NETWORK_ERROR
    if any(w in t for w in ("json", "decode", "parse", "schema", "keyerror", "format", "unexpected", "indexerror", "typeerror")):
        return PROVIDER_FORMAT_ERROR
    return UNKNOWN_ERROR


def display(value: object) -> str:
    """Page/CLI text for a stored status message. Valid canned values pass through; None -> 'none'; anything else (free text
    from an older version) is withheld WITHOUT being inspected beyond an equality test."""
    if value is None or value == "":
        return "none"
    if isinstance(value, str):
        code, sep, rest = value.partition(": ")
        if sep and code in CANNED and value == stored(code):
            return value
    return WITHHELD
