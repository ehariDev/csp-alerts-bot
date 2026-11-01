"""Slice-3 acceptance helpers (tester-owned): local fake Telegram server, ledger builders, in-process CLI runner.

Everything is synthetic. The ONLY socket opened is a loopback listener on 127.0.0.1 that this module starts; the real
Telegram host is never contacted (the transport is constructed with a loopback base URL, https enforcement relaxed
only through the transport's own test flag).
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from acc_helpers import NOW, TODAY, policy
from s2_helpers import FakeSource, appr, raw_row

from csp_alerts.alerts.telegram import TelegramTransport
from csp_alerts.journal.models import AccountSnapshot, PositionState as S
from csp_alerts.journal.store import Journal, NewPosition

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopqrs"
CHAT = "987654321"
CHAT_OTHER = "555000111"


# ---------------------------------------------------------------------------------------- fake Telegram server
class FakeTelegram:
    """Loopback Bot API. `script` entries (consumed in order, then `default`):
         (status, json_body)   respond
         "hang"                accept the request, never answer until the client times out (delivery unknown)
         "drop"                close the connection without any response after reading the request
       `chat_type` is what getChat reports."""

    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.script: list[Any] = []
        self.chat_type = "private"
        self.default = (200, {"ok": True, "result": {"message_id": 42}})
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                outer.requests.append({"path": self.path, "method": method, "body": body})
                if method == "getChat":
                    status, payload = 200, {"ok": True, "result": {"id": 1, "type": outer.chat_type}}
                    if outer.script and isinstance(outer.script[0], tuple):
                        status, payload = outer.script.pop(0)
                else:
                    item = outer.script.pop(0) if outer.script else outer.default
                    if item == "hang":
                        time.sleep(2.0)
                        return
                    if item == "halfresponse":      # status line + headers + a partial body, then stall: request WAS sent
                        crlf = bytes([13, 10])
                        self.wfile.write(b"HTTP/1.1 200 OK" + crlf + b"Content-Type: application/json" + crlf + b"Content-Length: 200" + crlf + crlf + b"{")
                        self.wfile.flush()
                        time.sleep(2.0)
                        return
                    if item == "drop":
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    status, payload = item
                    if callable(payload):
                        payload = payload(body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def sends(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "sendMessage"]

    def transport(self, token: str = TOKEN, chat: str = CHAT, timeout: float = 0.6) -> TelegramTransport:
        return TelegramTransport(token, chat, base_url=self.url, timeout=timeout, allow_insecure_base_for_tests=True)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def dead_port_transport(timeout: float = 0.5) -> TelegramTransport:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return TelegramTransport(TOKEN, CHAT, base_url=f"http://127.0.0.1:{port}", timeout=timeout,
                             allow_insecure_base_for_tests=True)


# ---------------------------------------------------------------------------------------- ledger builders
class Clock:
    def __init__(self, t: datetime = NOW):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def adv(self, **kw):
        self.t += timedelta(**kw)
        return self.t


def open_position(j: Journal, clock: Clock, trade_id="P1", ticker="AAA", strike="75.00", expiration=date(2026, 12, 4),
                  credit="0.50", fees="0.65", contracts=1, state: S = S.OPEN, sector=None, bucket=None):
    j.create_draft(NewPosition(trade_id, ticker, expiration, D(strike), 100, contracts, sector or f"sec_{ticker}",
                               bucket or f"bk_{ticker}"))
    j.transition(trade_id, S.PENDING)
    if state is S.PENDING:
        return
    j.transition(trade_id, S.OPEN, fill_contracts=contracts, actual_open_credit_per_share=D(credit),
                 opened_at_utc=clock(), fees_usd=D(fees))
    if state is S.CLOSE_WORKING:
        j.transition(trade_id, S.CLOSE_WORKING)
    elif state is S.ADJUSTMENT_REVIEW:
        j.transition(trade_id, S.ADJUSTMENT_REVIEW)


def ledger(positions: list[dict] | None = None, now: datetime = NOW, equity="100000", cash="60000", snapshot=True,
           path: str | Path = ":memory:") -> tuple[Journal, Clock]:
    """Reconciled single-account ledger: baseline, optional positions recorded earlier, then a fresh confirmed snapshot."""
    clock = Clock(now - timedelta(hours=6))
    j = Journal(path, clock=clock)
    j.migrate()
    j.confirm_baseline(D("100000"))
    for kw in positions or []:
        open_position(j, clock, **kw)
    clock.t = now - timedelta(hours=2)
    if snapshot:
        j.add_snapshot(AccountSnapshot("s0", clock.t, D(equity), D(cash), True, "v1"))
    clock.t = now
    return j, clock


def chain_rows_for(strike: float, expiration: date, bid: float, ask: float, delta: float = -0.2, spot: float = 80.0,
                   now: datetime = NOW) -> list[dict[str, Any]]:
    return [raw_row(now, strike=strike, expiration=expiration.isoformat(), bid=bid, ask=ask, delta=delta, spot=spot)]


def count(j: Journal, table: str) -> int:
    return j.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def dump_text(j: Journal) -> str:
    return "\n".join(j.conn.iterdump())


# ---------------------------------------------------------------------------------------- in-process CLI
def _frozen_datetime_class(now: datetime):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)
    return FrozenDatetime


def run_cli(monkeypatch, tmp_path: Path, args: list[str], *, env: dict[str, str] | None = None, now: datetime = NOW,
            source: FakeSource | None = None, transport: TelegramTransport | None = None, approved: Path | None = None,
            frozen_journal: bool = False) -> tuple[int, str, str]:
    """Run csp_alerts.cli.main in-process with a frozen clock, an injected chain source and an injected transport.
    Returns (exit code, stdout, stderr)."""
    import csp_alerts.cli as cli
    import csp_alerts.market.stockpile as sp
    import csp_alerts.runner as runner

    for k in list(__import__("os").environ):
        if k.startswith(("CSP_", "TELEGRAM_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CSP_DATABASE_URL", "sqlite:///" + (tmp_path / "j.sqlite3").as_posix())
    monkeypatch.setenv("CSP_DRY_RUN", "true")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    ef = tmp_path / "t.env"
    ef.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "_now", lambda: now)
    # TEST-DEFECT FIX: cmd_reconcile_check reads `datetime.now(timezone.utc)` directly instead of the injectable cli._now(), so a
    # test that freezes only _now() silently depended on the real wall clock (DAILY_CHECKIN_MISSING appeared once the real
    # clock crossed into Saturday). Freeze that name too. The product-side inconsistency is reported separately and pinned by
    # test_stage_a_clock_seam.py (strict xfail).
    monkeypatch.setattr(cli, "datetime", _frozen_datetime_class(now))
    if frozen_journal:        # audit/ledger timestamps follow the frozen clock too (ledger-flow tests)
        def _frozen_journal(settings):
            j = Journal(settings.database_path, clock=lambda: now)
            j.migrate()
            return j
        monkeypatch.setattr(cli, "_journal", _frozen_journal)
    if source is not None:
        monkeypatch.setattr(sp, "StockpileSource", lambda root: source)
    if transport is not None:
        monkeypatch.setattr(runner, "make_transport", lambda settings: transport)
    ap = approved or (tmp_path / "approved.json")
    if not ap.exists():
        ap.write_text(json.dumps({"approved_underlyings": []}), encoding="utf-8")
    from acc_helpers import POLICY_PATH
    argv = ["--env-file", str(ef), "--policy", str(POLICY_PATH), "--approved", str(ap), *args]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def open_db(tmp_path: Path, clock=None) -> Journal:
    j = Journal(tmp_path / "j.sqlite3", clock=clock or (lambda: NOW)) if clock else Journal(tmp_path / "j.sqlite3")
    j.migrate()
    return j


def write_approved(path: Path, tickers=("AAA",), **over) -> Path:
    recs = []
    for t in tickers:
        rec = {"ticker": t, "asset_type": "STOCK", "sector": f"sec_{t}", "correlation_bucket": f"bk_{t}",
               "max_buy_strike": "100.00", "approved_through": "2026-12-31", "events_reviewed_on": TODAY.isoformat(),
               "events_cleared_through": "2027-01-28", "ownership_reason": "owner thesis", "approved_by": "owner",
               "standard_deliverable_confirmed_on": TODAY.isoformat()}
        rec.update(over)
        recs.append(rec)
    path.write_text(json.dumps({"approved_underlyings": recs}), encoding="utf-8")
    return path
