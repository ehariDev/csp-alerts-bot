"""SQLite outbox (spec 5.1 alert_outbox, 6.4, 13.2): persist BEFORE sending, then record the outcome.

States: PENDING -> SENDING -> SENT | RETRY | FAILED_FINAL | UNKNOWN_ACK, plus DROPPED_EXPIRED / DROPPED_STALE
(entry alerts that expired or whose eligibility changed before send, IT-003).
  * Exactly-once delivery is NOT guaranteed: a timeout after POST becomes UNKNOWN_ACK and is never auto-resent.
  * A row found in SENDING at the start of a flush (crash mid-send) is also moved to UNKNOWN_ACK.
  * alert_id is stable (uuid5 of the account-scoped dedup key); dedup_key is UNIQUE, so duplicates are skipped.
  * Dry-run never reaches this module: `enqueue` raises unless the decision is SEND.
"""
from __future__ import annotations

import json
import random
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from csp_alerts.alerts.sendmode import Mode, SendDecision, SendModeError
from csp_alerts.alerts.telegram import Outcome, TelegramTransport, redact
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.journal.models import from_iso, to_iso
from csp_alerts.journal.store import Journal
from csp_alerts.journal.queries import delivery_summary  # noqa: F401  (re-export; pure read lives in journal/)

MAX_PART = 3800
MAX_ATTEMPTS = 5
SEVERITY_RANK = {"CRITICAL": 0, "URGENT": 1, "ACTION": 2, "WARN": 3, "INFO": 4}
FINAL = {"SENT", "FAILED_FINAL", "UNKNOWN_ACK", "DROPPED_EXPIRED", "DROPPED_STALE"}


def split_text(text: str, limit: int = MAX_PART) -> list[str]:
    """Split at line boundaries so each part is <= limit characters (Telegram caps at 4096)."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


INFO_KINDS = {"NO_TRADE_DIGEST", "DATA_ISSUE", "RECONCILIATION_REQUIRED"}
INFO_EXPIRY_MINUTES = 24 * 60
REQUEUEABLE = ("DROPPED_EXPIRED", "DROPPED_STALE", "FAILED_FINAL")


def _is_informational(msg: AlertMessage) -> bool:
    """Informational classes lose value with time and are dropped after 24h. ACCOUNT_RISK and CRITICAL never expire."""
    if msg.kind == "ACCOUNT_RISK" or msg.severity == "CRITICAL":
        return False
    return msg.kind in INFO_KINDS or (msg.kind == "SERVICE_HEALTH" and msg.severity == "INFO")


def _base_and_part(row) -> tuple[str, int]:
    key = row["dedup_key"]
    if "#" in key:
        base, _, tail = key.rpartition("#")
        try:
            return base, int(tail.split("/")[0])
        except ValueError:
            pass
    return key, 1


def stable_alert_id(dedup_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "csp-alerts/" + dedup_key))


def enqueue(journal: Journal, msg: AlertMessage, decision: SendDecision, *, now: datetime, scope: str, policy_sha: str,
            expires_in_minutes: int | None = None, revalidate: dict[str, Any] | None = None) -> list[str]:
    """Persist a sendable alert (split into parts). Returns new alert_ids; [] if it is a duplicate."""
    if decision.mode is not Mode.SEND:
        raise SendModeError("dry-run never writes sendable outbox rows")
    base_key = f"{scope}|{msg.dedup_key}"
    parts = split_text(msg.text)
    ids: list[str] = []
    if expires_in_minutes is None and _is_informational(msg):
        expires_in_minutes = INFO_EXPIRY_MINUTES
    expires = None if expires_in_minutes is None else to_iso(now + timedelta(minutes=expires_in_minutes))
    with journal.tx():
        for i, text in enumerate(parts, 1):
            key = base_key if len(parts) == 1 else f"{base_key}#{i}/{len(parts)}"
            aid = stable_alert_id(key)
            evidence = {"policy_sha": policy_sha, "parse_mode": msg.parse_mode, "expires_at_utc": expires,
                        "revalidate": revalidate, "part": i, "parts": len(parts), "last_error": None, "scope": scope}
            try:
                journal.conn.execute(
                    "INSERT INTO alert_outbox(alert_id, dedup_key, class, severity, status, created_at_utc, "
                    "next_attempt_at_utc, attempts, telegram_message_id, content_text, evidence_json) "
                    "VALUES (?,?,?,?, 'PENDING', ?, ?, 0, NULL, ?, ?)",
                    (aid, key, msg.kind, msg.severity, to_iso(now), to_iso(now), text, json.dumps(evidence, sort_keys=True)))
            except sqlite3.IntegrityError:
                old = journal.conn.execute("SELECT status FROM alert_outbox WHERE dedup_key=?", (key,)).fetchone()
                if old is None or old["status"] not in REQUEUEABLE:
                    continue  # PENDING/RETRY/SENDING/SENT/UNKNOWN_ACK: never queue a duplicate
                # the previous row ended without delivery: a fresh evaluation may queue again (same stable id)
                journal.conn.execute(
                    "UPDATE alert_outbox SET status='PENDING', created_at_utc=?, next_attempt_at_utc=?, attempts=0, "
                    "telegram_message_id=NULL, content_text=?, evidence_json=? WHERE dedup_key=?",
                    (to_iso(now), to_iso(now), text, json.dumps(evidence, sort_keys=True), key))
                journal._audit("ALERT_REQUEUED", "system", {"status": old["status"]}, {"alert_id": aid, "dedup_key": key}, "outbox")
            ids.append(aid)
    return ids


@dataclass
class FlushReport:
    sent: int = 0
    retry: int = 0
    failed: int = 0
    unknown: int = 0
    dropped: int = 0
    notes: list[str] = field(default_factory=list)


def _set(journal: Journal, aid: str, **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    journal.conn.execute(f"UPDATE alert_outbox SET {sets} WHERE alert_id=?", (*cols.values(), aid))


def _evidence_update(row: sqlite3.Row, **kv: Any) -> str:
    ev = json.loads(row["evidence_json"])
    ev.update(kv)
    return json.dumps(ev, sort_keys=True)


def flush(journal: Journal, transport: TelegramTransport, *, now: datetime,
          revalidator: Callable[[dict[str, Any], str], str | None] | None = None,
          chat_check: Callable[[], str | None] | None = None, secrets: tuple[str, ...] = (),
          rng: random.Random | None = None) -> FlushReport:
    """Send due rows. `revalidator(evidence, kind)` returns a reason string to DROP an alert, or None to send.
    `chat_check()` returns an error string if the recipient is not the pinned private chat (nothing is sent)."""
    rep = FlushReport()
    rng = rng or random.Random()
    with journal.tx():  # crash recovery: a row stuck in SENDING may or may not have been delivered
        for r in journal.conn.execute("SELECT * FROM alert_outbox WHERE status='SENDING'").fetchall():
            _set(journal, r["alert_id"], status="UNKNOWN_ACK",
                 evidence_json=_evidence_update(r, last_error="process stopped while sending; delivery unknown"))
            rep.unknown += 1
    err = chat_check() if chat_check else None
    if err:
        rep.notes.append(err)
        return rep
    rows = journal.conn.execute(
        "SELECT * FROM alert_outbox WHERE status IN ('PENDING','RETRY') AND next_attempt_at_utc<=?", (to_iso(now),)).fetchall()
    # deterministic: severity, creation time, then message group and part number (parts arrive in order)
    rows.sort(key=lambda r: (SEVERITY_RANK.get(r["severity"], 9), r["created_at_utc"], *_base_and_part(r)))
    blocked_groups: set[str] = set()
    for r in rows:
        group, part = _base_and_part(r)
        if group in blocked_groups:
            continue  # an earlier part of this message did not go out in this flush
        if part > 1:
            # persistent hold: a later part is eligible only when ALL earlier parts of the group are SENT
            earlier = [x["status"] for x in journal.conn.execute(
                "SELECT dedup_key, status FROM alert_outbox WHERE dedup_key LIKE ?", (group + "#%",)).fetchall()
                if _base_and_part(x)[0] == group and _base_and_part(x)[1] < part]
            if any(st in ("PENDING", "RETRY", "SENDING") for st in earlier):
                continue  # earlier part still waiting (e.g. 429 retry not yet due): keep order across flushes
            if any(st != "SENT" for st in earlier):
                with journal.tx():  # earlier part ended without delivery: an incomplete message is not sent
                    _set(journal, r["alert_id"], status="DROPPED_STALE",
                         evidence_json=_evidence_update(r, last_error="an earlier part of this message was not delivered"))
                    journal._audit("ALERT_DROPPED", "system", None,
                                   {"alert_id": r["alert_id"], "reason": "earlier part not delivered", "class": r["class"]}, "outbox")
                rep.dropped += 1
                continue
        ev = json.loads(r["evidence_json"])
        aid = r["alert_id"]
        if ev.get("expires_at_utc") and from_iso(ev["expires_at_utc"]) <= now:
            with journal.tx():
                _set(journal, aid, status="DROPPED_EXPIRED", evidence_json=_evidence_update(r, last_error="expired before send"))
                journal._audit("ALERT_DROPPED", "system", None, {"alert_id": aid, "reason": "expired", "class": r["class"]}, "outbox")
            rep.dropped += 1
            continue
        if revalidator is not None:
            why = revalidator(ev, r["class"])
            if why:
                with journal.tx():
                    _set(journal, aid, status="DROPPED_STALE", evidence_json=_evidence_update(r, last_error=why))
                    journal._audit("ALERT_DROPPED", "system", None, {"alert_id": aid, "reason": why, "class": r["class"]}, "outbox")
                rep.dropped += 1
                continue
        with journal.tx():  # persist intent BEFORE the POST
            _set(journal, aid, status="SENDING", attempts=r["attempts"] + 1)
        res = transport.send(r["content_text"], parse_mode=ev.get("parse_mode", "HTML"), silent=r["severity"] == "INFO")
        attempts = r["attempts"] + 1
        msg = redact(res.error, *secrets)
        mark_digest = False
        with journal.tx():
            if res.outcome is Outcome.OK:
                _set(journal, aid, status="SENT", telegram_message_id=res.message_id)
                rep.sent += 1
                mark_digest = r["class"] == "NO_TRADE_DIGEST" and "|PARTIAL" not in r["dedup_key"]
            elif res.outcome is Outcome.UNKNOWN:
                _set(journal, aid, status="UNKNOWN_ACK", evidence_json=_evidence_update(r, last_error=msg))
                rep.unknown += 1
                blocked_groups.add(group)
            elif res.outcome is Outcome.PERMANENT or attempts >= MAX_ATTEMPTS:
                _set(journal, aid, status="FAILED_FINAL", evidence_json=_evidence_update(r, last_error=msg))
                rep.failed += 1
                blocked_groups.add(group)
            else:
                blocked_groups.add(group)
                if res.outcome is Outcome.RETRY_AFTER:
                    delay = float(res.retry_after or 30)
                else:
                    base = min(300.0, 5.0 * 2 ** (attempts - 1))
                    delay = base + rng.uniform(0, 0.2 * base)
                _set(journal, aid, status="RETRY", next_attempt_at_utc=to_iso(now + timedelta(seconds=delay)),
                     evidence_json=_evidence_update(r, last_error=msg))
                rep.retry += 1
        if mark_digest:
            journal.mark_digest_sent(from_iso(r["created_at_utc"]).date(), source="outbox")
    return rep
