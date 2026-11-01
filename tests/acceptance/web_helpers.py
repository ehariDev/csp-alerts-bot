"""Acceptance helpers for the local read-only dashboard (tester-owned, WD-AT-02..08). Synthetic data, loopback only.

Reference book (all synthetic), frozen clock NOW = 2026-10-30T15:05:02Z (Fri 11:05 EDT = 10:05 CDT):

  trade  ticker state                       strike  collateral(strike*100*1)  reserves?
  OPN    AAA    OPEN                         75.00   7,500                     yes   (credit 0.50, fees 0.65, exp 2026-12-04, DTE 35)
  WRK    BBB    WORKING                      50.00   5,000                     yes
  CLW    CCC    CLOSE_WORKING                20.00   2,000                     yes   (exp 2026-11-20: DTE = 1 + 20 = 21 -> TIME_EXIT at the limit)
  ADJ    DDD    ADJUSTMENT_REVIEW            10.00   1,000                     yes
  PND    EEE    PENDING                       5.00     500                     yes
  ASG    FFF    ASSIGNED_PENDING_RECON.      30.00   3,000                     NO (assigned_pending, counted separately)
  CLS    HHH    CLOSED                       90.00   9,000                     NO
  DRF    III    DRAFT                        60.00   6,000                     NO
  stock lot GGG 100 sh x 40.00 = 4,000 (unlinked)

  put collateral      = 7,500 + 5,000 + 2,000 + 1,000 + 500 = 16,000
  active contracts    = 5 (OPN, WRK, CLW, ADJ, PND)
  owned stock         = 100 * 40.00 = 4,000
  assigned pending    = 3,000
  aggregate           = 16,000 + 4,000 + 3,000 = 23,000
  limits (policy.yaml): pilot cap 15,000 -> room -1,000 (binding: smallest room); normal ceiling 25,000 ->
                        aggregate room 2,000, put-collateral room 9,000; contracts 4 -> room -1; emergency 30,000 -> not breached
  snapshots: s0 equity 100,000 (NOW-2h), s1 equity 96,500 (NOW-1h), net free cash 60,000
  drawdown   = (100,000 - 96,500) / 100,000 = 0.035 -> between 3% (pause) and 5% (review) -> PAUSE
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from acc_helpers import NOW, POLICY_PATH
from s3_helpers import Clock

from csp_alerts.journal.models import AccountSnapshot, PositionState as S, StockLot, to_iso
from csp_alerts.journal.store import Journal, NewPosition

PW = "correct-horse-battery-staple-42"
REPO = Path(__file__).resolve().parents[2]
EXP35 = date(2026, 12, 4)

EXPECT = {
    "put_collateral": D("16000"), "owned_stock": D("4000"), "assigned_pending": D("3000"), "aggregate": D("23000"),
    "contracts": 5, "pilot_limit": D("15000"), "pilot_room": D("-1000"), "agg_room": D("2000"), "put_room": D("9000"),
    "contract_room": -1, "emergency_limit": D("30000"),
    "peak": D("100000"), "latest_equity": D("96500"), "drawdown": D("0.035"), "free_cash": D("60000"),
}


def scrypt_hash(pw: str, n: int = 16) -> str:
    """Independent implementation of the documented hash format scrypt$N$r$p$salt$hash (r=8, p=1, 32 bytes)."""
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${n}$8$1${salt.hex()}${h.hex()}"


def build_book(path: Path, *, notes: dict[str, str] | None = None, tickers: dict[str, str] | None = None) -> Journal:
    """Create the reference book. Returns an OPEN Journal at clock NOW (caller closes it)."""
    notes, tickers = notes or {}, tickers or {}
    clock = Clock(NOW - timedelta(hours=6))
    j = Journal(path, clock=clock)
    j.migrate()
    j.confirm_baseline(D("100000"))

    def new(tid, tk, strike, exp=EXP35):
        tk = tickers.get(tid, tk)
        j.create_draft(NewPosition(tid, tk, exp, D(strike), 100, 1, f"sec_{tid}", f"bk_{tid}", notes=notes.get(tid, "")))

    def fill(tid, credit="0.50", fees="0.65"):
        j.transition(tid, S.OPEN, fill_contracts=1, actual_open_credit_per_share=D(credit), opened_at_utc=clock(), fees_usd=D(fees))

    new("OPN", "AAA", "75.00"); j.transition("OPN", S.PENDING); fill("OPN")
    new("WRK", "BBB", "50.00"); j.transition("WRK", S.PENDING); j.transition("WRK", S.WORKING)
    new("CLW", "CCC", "20.00", date(2026, 11, 20)); j.transition("CLW", S.PENDING); fill("CLW", "0.40", "0.65")
    j.transition("CLW", S.CLOSE_WORKING)
    new("ADJ", "DDD", "10.00"); j.transition("ADJ", S.PENDING); fill("ADJ", "0.20", "0.65"); j.transition("ADJ", S.ADJUSTMENT_REVIEW)
    new("PND", "EEE", "5.00"); j.transition("PND", S.PENDING)
    new("ASG", "FFF", "30.00"); j.transition("ASG", S.PENDING); fill("ASG", "0.30", "0.65")
    j.transition("ASG", S.ASSIGNED_PENDING_RECONCILIATION)
    new("CLS", "HHH", "90.00"); j.transition("CLS", S.PENDING); fill("CLS", "0.90", "0.65"); j.transition("CLS", S.CLOSE_WORKING)
    j.transition("CLS", S.CLOSED, actual_close_debit_per_share=D("0.10"), closed_at_utc=clock(), fees_usd=D("1.30"))
    new("DRF", "III", "60.00")
    clock.t = NOW - timedelta(hours=3)
    j.add_stock_lot(StockLot("L-GGG", "GGG", D("100"), D("40.00"), NOW - timedelta(hours=3), "sec_g", "bk_g", "OWNED", "ETF", None, None))
    clock.t = NOW - timedelta(hours=2)
    j.add_snapshot(AccountSnapshot("s0", clock.t, D("100000"), D("60000"), True, "v1"))
    clock.t = NOW - timedelta(hours=1)
    j.add_snapshot(AccountSnapshot("s1", clock.t, D("96500"), D("60000"), True, "v1"))
    clock.t = NOW
    return j


def add_observation(path: Path, ticker: str, strike: str, exp: date, retrieved: datetime, bid: str, ask: str,
                    delta: str = "-0.12", spot: str = "80.00") -> None:
    key = f"{ticker}|{exp.isoformat()}|P|{D(strike):.2f}|100"
    d = {"contract_key": key, "retrieved_at_utc": to_iso(retrieved), "quote_at_utc": None, "provider": "yahoo_via_stockpile",
         "data_tier": "INDICATIVE_UNTIMESTAMPED", "bid": bid, "ask": ask, "delta": delta, "spot": spot}
    j = Journal(path, clock=lambda: NOW)
    j.record_observations([(str(uuid.uuid4()), d, "0" * 64)])
    j.close()


def insert_alerts(path: Path, n: int, start: int = 0, status: str = "SENT", content: str = "synthetic alert") -> None:
    c = sqlite3.connect(path)
    for i in range(start, start + n):
        ev = {"policy_sha": "x", "revalidate": {"trade_id": "OPN", "trigger": "TIME_EXIT"}, "part": 1, "parts": 1}
        c.execute("INSERT INTO alert_outbox(alert_id, dedup_key, class, severity, status, created_at_utc, next_attempt_at_utc, attempts,"
                  " telegram_message_id, content_text, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (f"AL-{i:05d}", f"dk-{i}", "EXIT", "WARN", status, to_iso(NOW - timedelta(minutes=i)), None, 1, "42", content,
                   json.dumps(ev)))
    c.commit()
    c.close()


def fingerprint(db: Path) -> dict[str, Any]:
    """Everything that defines product state: main-file bytes, non-empty WAL bytes, a full logical dump, mtime."""
    out: dict[str, Any] = {"main": hashlib.sha256(db.read_bytes()).hexdigest(), "mtime": db.stat().st_mtime_ns}
    wal = Path(str(db) + "-wal")
    out["wal"] = hashlib.sha256(wal.read_bytes()).hexdigest() if wal.exists() and wal.stat().st_size else None
    c = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    out["dump"] = hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()
    out["counts"] = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    c.close()
    return out


class Dash:
    """In-process DashboardServer on 127.0.0.1:<ephemeral> in a thread, plus a tiny raw http.client driver."""

    def __init__(self, db: Path, approved: Path, now: datetime = NOW, pw: str = PW, tz: str = "America/Chicago"):
        from csp_dashboard.server import DashboardServer
        from csp_dashboard.views import Sources
        self.now = now
        self.sources = Sources(db, POLICY_PATH, approved, tz, clock=lambda: self.now)
        self.srv = DashboardServer(self.sources, scrypt_hash(pw), port=0)
        self.port = self.srv.server_address[1]
        self.cookie: str | None = None
        self.pw = pw
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method: str, path: str, *, auth: bool = True, host: str | None = None, origin: str | None = None,
            headers: dict[str, str] | None = None, body: bytes | None = None) -> tuple[int, dict[str, str], str]:
        h: dict[str, str] = {}
        if auth and self.cookie:
            h["Cookie"] = f"csp_session={self.cookie}"
        if host is not None:
            h["Host"] = host
        if origin is not None:
            h["Origin"] = origin
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read().decode("utf-8", "replace")
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            c.close()

    def login(self, pw: str | None = None) -> int:
        body = ("password=" + (self.pw if pw is None else pw)).encode()
        st, hd, _ = self.req("POST", "/login", auth=False, origin=self.origin, body=body,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        m = re.search(r"csp_session=([^;]+)", hd.get("set-cookie", ""))
        if st == 303 and m:
            self.cookie = m.group(1)
        return st

    def api(self, path: str) -> dict[str, Any]:
        st, hd, body = self.req("GET", path)
        assert st == 200, (path, st, body[:200])
        assert hd["content-type"].startswith("application/json")
        return json.loads(body)


def no_floats(text: str) -> list[float]:
    found: list[float] = []

    def hook(s: str) -> float:
        found.append(float(s))
        return 0.0
    json.loads(text, parse_float=hook)
    return found
