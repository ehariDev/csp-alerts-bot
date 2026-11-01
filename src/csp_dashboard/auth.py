"""Local password authentication. The password itself is never stored; .env holds only a salted scrypt hash.

Hash format: scrypt$<N>$<r>$<p>$<salt hex>$<hash hex>. Sessions are random tokens held in server memory only
(a dashboard restart logs everyone out). Failed logins are rate limited per process."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time

N_DEFAULT, R_DEFAULT, P_DEFAULT = 2 ** 15, 8, 1
N_MAX = 2 ** 17          # refuse absurd cost parameters from a corrupted value
MIN_PASSWORD_LEN = 12
SESSION_SECONDS = 8 * 3600
MAX_FAILURES, LOCKOUT_SECONDS = 5, 60


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)


def make_hash(password: str, *, n: int = N_DEFAULT) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(16)
    return f"scrypt${n}${R_DEFAULT}${P_DEFAULT}${salt.hex()}${_scrypt(password, salt, n, R_DEFAULT, P_DEFAULT).hex()}"


def verify(password: str, stored: str) -> bool:
    """Constant-time check. Any malformed stored value simply fails (never raises, never reveals why)."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        if scheme != "scrypt" or not (2 <= n <= N_MAX) or n & (n - 1) or not (1 <= r <= 16) or not (1 <= p <= 4):
            return False
        want = bytes.fromhex(hash_hex)
        got = _scrypt(password, bytes.fromhex(salt_hex), n, r, p)
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


class Sessions:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}
        self._failures: list[float] = []
        self._csrf_key = secrets.token_bytes(32)

    def locked_out(self) -> bool:
        with self._lock:
            now = self._clock()
            self._failures = [t for t in self._failures if now - t < LOCKOUT_SECONDS]
            return len(self._failures) >= MAX_FAILURES

    def record_failure(self) -> None:
        with self._lock:
            self._failures.append(self._clock())

    def csrf(self, token: str | None) -> str | None:
        """Per-session CSRF token: HMAC(process secret, session token). Stateless, unguessable, dies with the session."""
        if not self.valid(token):
            return None
        return hmac.new(self._csrf_key, str(token).encode("utf-8"), hashlib.sha256).hexdigest()

    def csrf_ok(self, token: str | None, presented: str | None) -> bool:
        want = self.csrf(token)
        return bool(want and presented) and hmac.compare_digest(want, str(presented))

    def preview_token(self, token: str | None, fields: dict[str, str], revision: int, policy_sha: str) -> str | None:
        """Signed preview token: HMAC(process secret, session, canonical fields, ledger revision, policy hash, issue time).
        Binds a confirm to ONE session, ONE preview content and ONE ledger/policy state; it dies with the process."""
        if not self.valid(token):
            return None
        issued = int(self._clock())
        return f"{issued}.{self._preview_mac(token, fields, revision, policy_sha, issued)}"

    def _preview_mac(self, token: str | None, fields: dict[str, str], revision: int, policy_sha: str, issued: int) -> str:
        body = json.dumps({"f": fields, "r": revision, "p": policy_sha, "i": issued, "s": str(token)}, sort_keys=True,
                          separators=(",", ":"))
        return hmac.new(self._csrf_key, b"preview|" + body.encode("utf-8"), hashlib.sha256).hexdigest()

    def preview_status(self, token: str | None, presented: str | None, fields: dict[str, str], revision: int, policy_sha: str,
                       ttl_seconds: int) -> str:
        """'ok' | 'expired' | 'bad' for a presented preview token."""
        if not self.valid(token) or not presented or "." not in presented:
            return "bad"
        stamp, _, mac = presented.partition(".")
        if not stamp.isdigit() or len(stamp) > 12:
            return "bad"
        issued = int(stamp)
        if not hmac.compare_digest(self._preview_mac(token, fields, revision, policy_sha, issued), mac):
            return "bad"
        age = int(self._clock()) - issued
        return "ok" if 0 <= age <= ttl_seconds else "expired"

    def create(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self._lock:
            self._failures.clear()
            self._tokens[tok] = self._clock() + SESSION_SECONDS
        return tok

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._tokens.get(token)
            if exp is None:
                return False
            if exp < self._clock():
                del self._tokens[token]
                return False
            return True

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._tokens.pop(token or "", None)
