"""Send-mode precedence. Dry-run is the default; nothing can send unless ALL conditions hold.

A send requires every one of:
  1. the explicit CLI flag --send
  2. CSP_DRY_RUN=false in the environment/.env (a flag alone can never flip the env switch)
  3. no --dry-run flag on the same command line (a conflict is an error, never a send)
  4. Telegram token and chat id configured
  5. a pinned private chat that matches the configured chat id (checked again at flush time)
Every failed condition yields mode DRY_RUN plus an error message; the CLI exits non-zero and sends nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Mode(StrEnum):
    DRY_RUN = "DRY_RUN"
    SEND = "SEND"


class SendModeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SendDecision:
    mode: Mode
    error: str | None = None

    @property
    def can_send(self) -> bool:
        return self.mode is Mode.SEND


def resolve_send_mode(*, env_dry_run: bool, flag_send: bool, flag_dry_run: bool, telegram_configured: bool,
                      chat_pinned_ok: bool) -> SendDecision:
    if flag_send and flag_dry_run:
        return SendDecision(Mode.DRY_RUN, "conflicting flags: --send and --dry-run; nothing sent")
    if not flag_send:
        return SendDecision(Mode.DRY_RUN)
    if env_dry_run:
        return SendDecision(Mode.DRY_RUN, "--send refused: CSP_DRY_RUN is true. Set CSP_DRY_RUN=false in .env to enable sending")
    if not telegram_configured:
        return SendDecision(Mode.DRY_RUN, "--send refused: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")
    if not chat_pinned_ok:
        return SendDecision(Mode.DRY_RUN, "--send refused: no pinned private chat matching TELEGRAM_CHAT_ID; run telegram-setup --apply")
    return SendDecision(Mode.SEND)
