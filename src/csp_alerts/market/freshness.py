"""Market-data cadence, limits and truthful data-state labels (Stage A). Pure functions, no I/O, no network.

Cadence (owner proposal, implemented as bounded constants; they are scheduling hints, NOT financial policy and are
deliberately not in policy.yaml so changing them never changes the policy hash):
  * regular US session OPEN : a symbol is refreshed at most once every 5 minutes
  * session CLOSED / UNKNOWN: at most once every 60 minutes
  * a failed fetch backs off: rate limit 15 -> 30 -> 60 min; other errors 5 -> 10 -> 20 -> 30 min (cap)
  * a symbol is never fetched twice within MIN_GAP_SECONDS, not even for an owner request
  * per run: at most MAX_SYMBOLS_PER_RUN symbols; per symbol at most MAX_EXPIRIES_PER_SYMBOL nearest expiries stored
The Windows task fires every 5 minutes; the job itself decides what is due, so a woken or double-fired task never
produces extra provider calls.

Data-state labels. Retrieval time never proves exchange freshness. Labels are derived at READ time from stored facts
and the clock, so reloading a page can never make old evidence current:
  UNAVAILABLE   no stored observation at all
  STALE         the newest attempt failed (outage / rate limit: retained values keep their ORIGINAL timestamps) or the
                newest retrieval is older than STALE_AFTER_OPEN_SECONDS while the market is open
  MARKET-CLOSED the regular session is closed now: the value is the last one retrieved, not a live price
  DELAYED       the provider supplied a market timestamp and it is older than DELAYED_AFTER_SECONDS
  UNTIMESTAMPED the provider supplied no market timestamp: quote age is UNKNOWN (always the case for Yahoo here)
"""
from __future__ import annotations


from dataclasses import dataclass
from datetime import datetime, timedelta

from csp_alerts.market.models import SessionStatus

OPEN_INTERVAL_SECONDS = 300
CLOSED_INTERVAL_SECONDS = 3600
INTERVAL_GRACE_SECONDS = 30            # scheduler jitter: a task firing 20 s early is still "due"
MIN_GAP_SECONDS = 60
STALE_AFTER_OPEN_SECONDS = 900         # three missed 5-minute cycles
DELAYED_AFTER_SECONDS = 60
REQUEST_EXPIRES_SECONDS = 900
REQUEST_MIN_SPACING_SECONDS = 60       # web: at most one refresh request per minute
REQUESTS_PER_HOUR = 10
MAX_SYMBOLS_PER_RUN = 16
MAX_WATCHLIST = 12
MAX_EXPIRIES_PER_SYMBOL = 8
BROWSE_MIN_DTE = 14                    # browsing window (NOT the entry gate window 35-45); widened to cover held contracts
BROWSE_MAX_DTE = 60
STOCK_HISTORY_KEEP = 300
OPTION_HISTORY_KEEP = 30
PROVIDER_LABEL = "Yahoo indicative data · Quote age unknown"      # state-independent part (CLI output)
FEED_NORMAL = "Auto-refreshing page"
FEED_FAILING = "Auto-refresh failing: showing last known data"
FEED_CLOSED = "Auto-refresh paused, market closed: showing last known data"


def feed_label(label_sets, session: SessionStatus | None = None) -> str:
    """State-aware page banner (D12 item 130). `label_sets` = DataState.labels of every symbol on the page.
    Precedence: any STALE label (newest attempt failed or data older than the open-market limit) -> failing;
    else regular session closed -> closed; else the neutral 'Auto-refreshing page' (the page reloads itself, which never
    makes old evidence current). Never an unqualified 'live' claim."""
    if any("STALE" in ls for ls in label_sets):
        lead = FEED_FAILING
    elif session is SessionStatus.CLOSED or any("MARKET-CLOSED" in ls for ls in label_sets):
        lead = FEED_CLOSED
    else:
        lead = FEED_NORMAL
    return f"{lead} · {PROVIDER_LABEL}"

STATUSES = ("OK", "PARTIAL", "RATE_LIMITED", "ERROR", "MARKET_CLOSED")


def interval_seconds(session: SessionStatus) -> int:
    return OPEN_INTERVAL_SECONDS if session is SessionStatus.OPEN else CLOSED_INTERVAL_SECONDS


def backoff_seconds(status: str, consecutive_failures: int) -> int:
    """Delay before the next attempt after a failure. Doubles per consecutive failure, capped."""
    n = max(1, consecutive_failures)
    if status == "RATE_LIMITED":
        return min(3600, 900 * 2 ** (n - 1))
    return min(1800, 300 * 2 ** (n - 1))


def is_due(*, now: datetime, last_attempt: datetime | None, next_attempt_after: datetime | None,
           session: SessionStatus, force: bool = False) -> tuple[bool, str]:
    """(due, reason). Backoff and the minimum gap always apply; `force` (owner request / --force) skips only the
    regular interval."""
    if next_attempt_after is not None and now < next_attempt_after:
        return False, "backoff"
    if last_attempt is None:
        return True, "never attempted"
    elapsed = (now - last_attempt).total_seconds()
    if elapsed < MIN_GAP_SECONDS:
        return False, "minimum gap"
    if force:
        return True, "forced"
    if elapsed >= interval_seconds(session) - INTERVAL_GRACE_SECONDS:
        return True, "interval elapsed"
    return False, "not due"


def next_update_estimate(*, now: datetime, last_attempt: datetime | None, next_attempt_after: datetime | None,
                         session: SessionStatus) -> datetime | None:
    """Policy-derived estimate of the next refresh. NOT proof that a Windows task is installed or will run."""
    if last_attempt is None:
        return None
    t = last_attempt + timedelta(seconds=interval_seconds(session))
    if next_attempt_after is not None and next_attempt_after > t:
        t = next_attempt_after
    return t


@dataclass(frozen=True)
class DataState:
    labels: tuple[str, ...]            # ordered, distinct, e.g. ("STALE", "UNTIMESTAMPED")
    primary: str                       # first label
    age_seconds: int | None            # now - retrieval time (retrieval, NOT exchange age)
    market_age_seconds: int | None     # now - provider market time; None when the provider gave none
    note: str


def describe_state(*, now: datetime, session: SessionStatus, retrieved_at: datetime | None,
                   market_time: datetime | None, status: str | None, attempt_failed: bool = False) -> DataState:
    if retrieved_at is None:
        return DataState(("UNAVAILABLE",), "UNAVAILABLE", None, None, "No observation has been stored for this item.")
    age = max(0, int((now - retrieved_at).total_seconds()))
    mage = None if market_time is None else max(0, int((now - market_time).total_seconds()))
    labels: list[str] = []
    notes: list[str] = []
    failing = attempt_failed or status in ("ERROR", "RATE_LIMITED")
    if failing:
        labels.append("STALE")
        why = "provider rate limit" if status == "RATE_LIMITED" else "provider error" if status == "ERROR" else "no usable data returned"
        notes.append(f"the newest refresh attempt failed ({why}); this is the last retained value with its original retrieval time")
    elif session is SessionStatus.OPEN and age > STALE_AFTER_OPEN_SECONDS:
        labels.append("STALE")
        notes.append(f"last retrieved {age // 60} min ago while the market is open (updates are expected every "
                     f"{OPEN_INTERVAL_SECONDS // 60} min)")
    if session is SessionStatus.CLOSED:
        labels.append("MARKET-CLOSED")
        notes.append("the regular session is closed; this is the last retrieved value, not a live price")
    elif session is SessionStatus.UNKNOWN:
        labels.append("SESSION-UNKNOWN")
        notes.append("the exchange calendar could not say whether the market is open")
    if market_time is None:
        labels.append("UNTIMESTAMPED")
        notes.append("the provider gave no market timestamp: quote age is UNKNOWN (retrieval time proves nothing about "
                     "exchange freshness)")
    elif mage is not None and mage > DELAYED_AFTER_SECONDS:
        labels.append("DELAYED")
        notes.append(f"the provider's own market timestamp is {mage // 60} min old")
    if not labels:
        labels.append("RETRIEVED")
        notes.append("retrieved recently with a provider market timestamp")
    return DataState(tuple(labels), labels[0], age, mage, "; ".join(notes))
