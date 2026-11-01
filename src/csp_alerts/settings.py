"""Environment settings and startup validation (spec 9.2).

Secrets are held in `Secret` wrappers whose repr/str never reveal the value.
DRY_RUN defaults to true. CSP_DISABLE_BROKER_ACCESS must be exactly true.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from csp_alerts.boundary import BROKER_ENV_PREFIXES as _BROKER_ENV_PREFIXES

_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}
ALLOWED_ENVS = ("paper", "real")  # recognised values
SUPPORTED_ENVS = ("paper",)  # initial delivery is paper-only (SR-11)
ALLOWED_PROVIDERS = ("yahoo",)
ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Env var names that would indicate brokerage credentials. Presence is a startup error.
BROKER_ENV_PREFIXES = _BROKER_ENV_PREFIXES  # defined in boundary.py (single source of truth)
# Credential-looking names: unknown ones only WARN (doctor), unless they also carry a broker prefix.
CREDENTIAL_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "ACCOUNT", "OAUTH", "CLIENT_ID", "API")
OWN_PREFIXES = ("CSP_", "TELEGRAM_")
# Ordinary OS/tool variables that are never flagged (matched case-insensitively; * = prefix).
ENV_ALLOWLIST_EXACT = frozenset({
    "PATH", "PATHEXT", "USERPROFILE", "USERNAME", "USERDOMAIN", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "HOME", "HOMEDRIVE", "HOMEPATH", "SHELL", "TERM", "LANG", "VIRTUAL_ENV", "CI", "OS",
    "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PSMODULEPATH", "PUBLIC", "UV_CACHE_DIR",
})
ENV_ALLOWLIST_PREFIXES = ("PYTHON", "PIP_", "UV_", "PYTEST_", "GITHUB_ACTIONS", "RUNNER_", "PROCESSOR_",
                          "COMMONPROGRAMFILES", "ONEDRIVE", "NODE", "NPM_", "CLAUDE")


def _has_broker_prefix(name: str) -> bool:
    return name.upper().startswith(BROKER_ENV_PREFIXES)


def env_warnings(env: Mapping[str, str]) -> list[str]:
    """Names (never values) that look like credentials but are not ours, not allowlisted and not broker-prefixed."""
    out = []
    for k, v in env.items():
        u = k.upper()
        if not v or u.startswith(OWN_PREFIXES) or u in ENV_ALLOWLIST_EXACT or u.startswith(ENV_ALLOWLIST_PREFIXES):
            continue
        if _has_broker_prefix(u):
            continue  # already a startup error
        if any(h in u for h in CREDENTIAL_HINTS):
            out.append(k)
    return sorted(out)


class StartupError(RuntimeError):
    """Raised with a list of problems; messages never contain secret values."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class Secret:
    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __bool__(self) -> bool:
        return bool(self._v)

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


def _parse_bool(name: str, raw: str | None, default: bool, problems: list[str]) -> bool:
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    problems.append(f"{name} must be true or false")
    return default


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: Secret = field(default_factory=lambda: Secret(""))
    telegram_chat_id: Secret = field(default_factory=lambda: Secret(""))
    env: str = "paper"
    dry_run: bool = True
    tz: str = "America/Chicago"
    database_path: Path = Path("data/csp_alerts.sqlite3")
    stockpile_root: Path = Path("third_party/stockpile")
    data_provider: str = "yahoo"
    disable_broker_access: bool = True
    ai_enabled: bool = False
    log_level: str = "INFO"
    # Local read-only dashboard (csp_dashboard). The hash is a Secret and lives only in .env; the port is loopback-only.
    dashboard_password_hash: Secret = field(default_factory=lambda: Secret(""))
    dashboard_port: int = 8765

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id)


def parse_database_url(url: str) -> Path:
    prefix = "sqlite:///"
    if not url.startswith(prefix) or len(url) == len(prefix):
        raise ValueError("CSP_DATABASE_URL must look like sqlite:///./path/to/file.sqlite3")
    return Path(url[len(prefix):])


def settings_from_mapping(env: Mapping[str, str]) -> Settings:
    """Validate and build Settings. Raises StartupError listing every problem."""
    problems: list[str] = []

    broker_flag = env.get("CSP_DISABLE_BROKER_ACCESS")
    if broker_flag is None or broker_flag.strip() == "":
        disable = True  # default; the only permitted value
    else:
        disable = _parse_bool("CSP_DISABLE_BROKER_ACCESS", broker_flag, True, problems)
        if broker_flag.strip().lower() in _FALSE:
            problems.append(
                "CSP_DISABLE_BROKER_ACCESS must be true; brokerage integration is prohibited"
            )

    dry_run = _parse_bool("CSP_DRY_RUN", env.get("CSP_DRY_RUN"), True, problems)
    ai_enabled = _parse_bool("CSP_AI_ENABLED", env.get("CSP_AI_ENABLED"), False, problems)

    csp_env = (env.get("CSP_ENV") or "paper").strip().lower()
    if csp_env == "real":
        problems.append("CSP_ENV=real is not supported in this version: the initial delivery is paper-only "
                        "(manual paper bookkeeping). Set CSP_ENV=paper; real-money mode needs an explicit owner scope decision")
    elif csp_env not in ALLOWED_ENVS:
        problems.append(f"CSP_ENV must be one of {SUPPORTED_ENVS} (paper-only in this version)")

    provider = (env.get("CSP_DATA_PROVIDER") or "yahoo").strip().lower()
    if provider not in ALLOWED_PROVIDERS:
        problems.append(f"CSP_DATA_PROVIDER must be one of {ALLOWED_PROVIDERS} (broker providers are prohibited)")

    log_level = (env.get("CSP_LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in ALLOWED_LOG_LEVELS:
        problems.append(f"CSP_LOG_LEVEL must be one of {ALLOWED_LOG_LEVELS}")

    tz = (env.get("CSP_TZ") or "America/Chicago").strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        problems.append("CSP_TZ is not a valid IANA timezone (is the tzdata package installed?)")

    port_raw = (env.get("CSP_DASHBOARD_PORT") or "8765").strip()
    dashboard_port = 8765
    if port_raw.isdigit() and 1024 <= int(port_raw) <= 65535:
        dashboard_port = int(port_raw)
    else:
        problems.append("CSP_DASHBOARD_PORT must be an integer from 1024 to 65535")

    db_path = Path("data/csp_alerts.sqlite3")
    try:
        db_path = parse_database_url(env.get("CSP_DATABASE_URL") or "sqlite:///./data/csp_alerts.sqlite3")
    except ValueError as exc:
        problems.append(str(exc))

    present_broker = sorted(k for k in env if _has_broker_prefix(k) and env[k] and not k.upper().startswith(OWN_PREFIXES))
    if present_broker:
        problems.append(f"brokerage credential variables must not be set: {present_broker}")

    if problems:
        raise StartupError(problems)

    def secret(name: str) -> Secret:
        raw = (env.get(name) or "").strip()
        return Secret("" if raw in ("", "REPLACE_LOCALLY") else raw)

    return Settings(
        telegram_bot_token=secret("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=secret("TELEGRAM_CHAT_ID"),
        env=csp_env,
        dry_run=dry_run,
        tz=tz,
        database_path=db_path,
        stockpile_root=Path(env.get("CSP_STOCKPILE_ROOT") or "third_party/stockpile"),
        data_provider=provider,
        disable_broker_access=disable,
        ai_enabled=ai_enabled,
        log_level=log_level,
        dashboard_password_hash=secret("CSP_DASHBOARD_PASSWORD_HASH"),
        dashboard_port=dashboard_port,
    )


def load_settings(env_file: Path | str | None = ".env", environ: Mapping[str, str] | None = None) -> Settings:
    """Merge `.env` (if present) under real environment variables, then validate."""
    merged: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        merged.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    merged.update(os.environ if environ is None else environ)
    return settings_from_mapping(merged)
