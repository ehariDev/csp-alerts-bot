"""Outbound-only Telegram Bot API transport (spec 9.1). No polling, no inbound commands, no webhook.

Only `sendMessage` (and `getChat` once at setup to verify a private chat). The token is never logged or
put in an exception message; every error string passes through `redact`.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum

DEFAULT_BASE = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{10,}")


def chat_id_sha256(chat_id: str) -> str:
    return hashlib.sha256(str(chat_id).strip().encode("utf-8")).hexdigest()


def redact(text: str, *secrets: str) -> str:
    out = _TOKEN_RE.sub("[REDACTED_TOKEN]", str(text))
    for s in secrets:
        if s and len(s) >= 3:
            out = out.replace(s, "[REDACTED]")
    return out


class Outcome(StrEnum):
    OK = "OK"                  # API acknowledged ok=true
    RETRY_AFTER = "RETRY_AFTER"  # HTTP 429 with retry_after
    RETRY = "RETRY"            # 5xx, or connection failed before anything was sent
    PERMANENT = "PERMANENT"    # other 4xx: dead-letter
    UNKNOWN = "UNKNOWN"        # timeout / reset after POST: the message may have been delivered


@dataclass(frozen=True)
class SendResult:
    outcome: Outcome
    message_id: str | None = None
    retry_after: int | None = None
    error: str = ""


class TelegramTransport:
    def __init__(self, token: str, chat_id: str, *, base_url: str = DEFAULT_BASE, timeout: float = 10.0,
                 allow_insecure_base_for_tests: bool = False):
        if not base_url.startswith("https://") and not (allow_insecure_base_for_tests and base_url.startswith("http://127.0.0.1")):
            raise ValueError("Telegram base URL must be https")
        self._token, self._chat_id = token, str(chat_id)
        self._base, self._timeout = base_url.rstrip("/"), timeout

    # token appears only inside this private URL builder
    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    def _red(self, text: str) -> str:
        return redact(text, self._token, self._chat_id)

    def _post(self, method: str, payload: dict) -> tuple[int | None, dict | None, SendResult | None]:
        req = urllib.request.Request(self._url(method), data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:  # noqa: S310  # nosec B310 - base URL validated as https (loopback only in tests) in __init__
                return r.status, json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = None
            return e.code, body, None
        except urllib.error.URLError as e:
            # urllib wraps failures of the connect/send phase (refused, DNS, connect timeout, no network after a PC
            # wake) in URLError: nothing was processed by Telegram, so it is safe to retry.
            return None, None, SendResult(Outcome.RETRY, error=self._red(f"not sent (connect/send phase): {type(e.reason).__name__}"))
        except TimeoutError:
            # raised while waiting for the response: the request had already left, delivery is unknown
            return None, None, SendResult(Outcome.UNKNOWN, error="timeout after request; delivery unknown")
        except Exception as e:  # noqa: BLE001 - dropped connection after the request left: delivery unknown
            return None, None, SendResult(Outcome.UNKNOWN, error=self._red(f"connection dropped after request: {type(e).__name__}"))

    def send(self, text: str, *, parse_mode: str = "HTML", silent: bool = False) -> SendResult:
        payload = {"chat_id": self._chat_id, "text": text, "disable_notification": silent}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        status, body, early = self._post("sendMessage", payload)
        if early is not None:
            return early
        body = body or {}
        if status == 200 and body.get("ok") is True:
            mid = (body.get("result") or {}).get("message_id")
            return SendResult(Outcome.OK, message_id=None if mid is None else str(mid))
        desc = self._red(str(body.get("description", "")))[:200]
        if status == 429:
            ra = (body.get("parameters") or {}).get("retry_after")
            return SendResult(Outcome.RETRY_AFTER, retry_after=int(ra) if isinstance(ra, int) else 30, error=f"429 {desc}")
        if status is not None and status >= 500:
            return SendResult(Outcome.RETRY, error=f"{status} {desc}")
        if status == 200:  # ok=false with 200 is not expected; do not retry blindly
            return SendResult(Outcome.PERMANENT, error=f"200 ok=false {desc}")
        return SendResult(Outcome.PERMANENT, error=f"{status} {desc}")

    def get_chat_type(self) -> tuple[str | None, str]:
        """(chat type, error). Used once at setup to verify the target is a private chat."""
        status, body, early = self._post("getChat", {"chat_id": self._chat_id})
        if early is not None:
            return None, early.error
        if status == 200 and (body or {}).get("ok") is True:
            return str(body["result"].get("type")), ""
        return None, self._red(f"{status} {(body or {}).get('description', '')}")[:200]
