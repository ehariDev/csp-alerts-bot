"""Exchange session detection (spec 12.1) using exchange_calendars XNYS.

Produces the SessionInfo consumed by the entry gate. Any failure (missing library, date outside the
calendar's bounds, naive timestamp) yields UNKNOWN, which blocks new entries (spec 12.1: fail closed).
Regular equity session hours only; special option hours (for example some ETF/index options closing
at 16:15 ET) are NOT modelled and are conservative here (we treat the equity close as the close).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from csp_alerts.market.models import SessionInfo, SessionStatus

_CAL = None


def _calendar():
    global _CAL
    if _CAL is None:
        import exchange_calendars as xc

        _CAL = xc.get_calendar("XNYS")
    return _CAL


@dataclass(frozen=True)
class SessionDetail:
    info: SessionInfo
    session_date: date | None = None
    early_close: bool = False
    close_utc: datetime | None = None
    reason: str = ""


def session_detail(now_utc: datetime, calendar=None) -> SessionDetail:
    """Open/closed/early-close/minutes-to-close at `now_utc`; UNKNOWN on any failure."""
    try:
        if now_utc.tzinfo is None:
            raise ValueError("naive timestamp")
        import pandas as pd

        cal = calendar or _calendar()
        ts = pd.Timestamp(now_utc.astimezone(timezone.utc))
        if not (cal.first_minute <= ts <= cal.last_minute):
            raise ValueError("timestamp outside calendar bounds")
        if cal.is_open_on_minute(ts):
            session = cal.minute_to_session(ts)
            close = cal.session_close(session)
            minutes = int((close - ts).total_seconds() // 60)
            early = session in cal.early_closes
            return SessionDetail(SessionInfo(SessionStatus.OPEN, minutes), session.date(), bool(early),
                                 close.to_pydatetime(), "open")
        return SessionDetail(SessionInfo(SessionStatus.CLOSED, None), None, False, None, "market closed")
    except Exception as exc:  # noqa: BLE001 - any calendar failure must fail closed
        return SessionDetail(SessionInfo(SessionStatus.UNKNOWN, None), None, False, None,
                             f"calendar unavailable: {type(exc).__name__}")


def session_info(now_utc: datetime, calendar=None) -> SessionInfo:
    return session_detail(now_utc, calendar).info


def is_trading_day(d: date, calendar=None) -> bool | None:
    """True/False for a regular session on `d`; None if the calendar cannot say."""
    try:
        import pandas as pd

        return bool((calendar or _calendar()).is_session(pd.Timestamp(d)))
    except Exception:  # noqa: BLE001
        return None
