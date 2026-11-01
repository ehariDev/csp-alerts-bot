"""Pure read-only journal queries shared by the dashboard (must not import the Telegram transport)."""
from __future__ import annotations

from typing import Any

from csp_alerts.journal.store import Journal


def delivery_summary(journal: Journal) -> dict[str, Any]:
    counts = {r["status"]: r["n"] for r in journal.conn.execute("SELECT status, COUNT(*) n FROM alert_outbox GROUP BY status")}
    last = journal.conn.execute(
        "SELECT status, class, created_at_utc, telegram_message_id FROM alert_outbox ORDER BY rowid DESC LIMIT 1").fetchone()
    return {"counts": counts, "last": None if last is None else dict(last)}
