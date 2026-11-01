"""Stage B1 acceptance environment (tester-owned). SYNTHETIC only; loopback server; fake provider; no network.

Clock: NOW = Fri 2026-10-30T15:05:02Z (11:05 EDT = 10:05 CDT, session open); the dashboard clock is NOW + 30 s.
Reference book (hand-computed expectations live in the tests):
  approved: AAA STOCK sector S1 bucket B1 | BBB STOCK sector S2 bucket B2 | CCC STOCK sector S2 bucket B3 | EEE ETF sector S3 bucket B4
            (strike ceiling 100; NOT approved: ZZZ, UUU)
  journal : baseline 100000, confirmed snapshot (equity 100000, NET free cash 60000) at NOW - 2 h
  book    : OPEN AAA put 40 exp 2026-12-04 (credit 0.50, fees 0.65)  -> collateral 40 x 100 x 1 = 4,000
Stored chain (one refresh at NOW) for every symbol: expiry 2026-12-04, strikes listed per test, bid 0.50 ask 0.54.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlencode

from acc_helpers import NOW, POLICY_PATH
from s3_helpers import Clock
from stage_a_helpers import EXP, Prov, add_wl, jopen, refresh
from web_helpers import PW, Dash

from csp_alerts.journal.models import AccountSnapshot, PositionState as S
from csp_alerts.journal.store import Journal, NewPosition

DASH_NOW = NOW + timedelta(seconds=30)
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
HIDDEN = re.compile(r'<input[^>]*type="hidden"[^>]*>')
ATTR = re.compile(r'(\w[\w-]*)="([^"]*)"')


def write_approved_custom(path: Path, spec: dict[str, tuple[str, str, str, str]]) -> Path:
    """spec: ticker -> (asset_type, sector, bucket, max_buy_strike)"""
    recs = []
    for t, (at, sec, bkt, mx) in spec.items():
        recs.append({"ticker": t, "asset_type": at, "sector": sec, "correlation_bucket": bkt, "max_buy_strike": mx,
                     "approved_through": "2026-12-31", "events_reviewed_on": "2026-10-30", "events_cleared_through": "2027-01-28",
                     "ownership_reason": "owner thesis", "approved_by": "owner", "standard_deliverable_confirmed_on": "2026-10-30"})
    path.write_text(json.dumps({"approved_underlyings": recs}), encoding="utf-8")
    return path


APPROVED = {"AAA": ("STOCK", "S1", "B1", "100.00"), "BBB": ("STOCK", "S2", "B2", "100.00"),
            "CCC": ("STOCK", "S2", "B3", "100.00"), "EEE": ("ETF", "S3", "B4", "100.00")}


DEFAULT_STRIKES = (20.0, 21.0, 30.0, 35.0, 40.0, 45.0, 50.0, 60.0, 90.0, 95.0)
EXP_OTHER = date(2026, 11, 20)      # a different ISO week than EXP (2026-12-04)


def build_db(tmp: Path, *, strikes: dict[str, tuple[float, ...]] | None = None, with_snapshot=True, existing=True,
             chain_symbols=("AAA", "BBB", "CCC", "EEE", "ZZZ", "UUU"), extras=(), chain_over=None) -> Path:
    """extras: dicts(tid, ticker, strike, exp=EXP, sector, bucket, state='OPEN', contracts=1, credit='0.50') added BEFORE the snapshot."""
    db = tmp / "b1.sqlite3"
    clk = Clock(NOW - timedelta(hours=6))
    j = Journal(db, clock=clk)
    j.migrate()
    j.confirm_baseline(D("100000"))
    if existing:
        j.create_draft(NewPosition("OPN", "AAA", EXP, D("40.00"), 100, 1, "S1", "B1"))
        j.transition("OPN", S.PENDING)
        j.transition("OPN", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.50"), opened_at_utc=clk(), fees_usd=D("0.65"))
    for x in extras:
        tid, st = x["tid"], x.get("state", "OPEN")
        j.create_draft(NewPosition(tid, x["ticker"], x.get("exp", EXP), D(x["strike"]), 100, x.get("contracts", 1),
                                   x.get("sector", "S9"), x.get("bucket", "B9")))
        j.transition(tid, S.PENDING)
        if st == "WORKING":
            j.transition(tid, S.WORKING)
            continue
        if st == "PENDING":
            continue
        j.transition(tid, S.OPEN, fill_contracts=x.get("contracts", 1), actual_open_credit_per_share=D(x.get("credit", "0.50")),
                     opened_at_utc=clk(), fees_usd=D("0.65"))
        if st in ("CLOSE_WORKING", "ADJUSTMENT_REVIEW", "ASSIGNED_PENDING_RECONCILIATION"):
            j.transition(tid, S[st])
    clk.t = NOW - timedelta(hours=2)
    if with_snapshot:
        j.add_snapshot(AccountSnapshot("s0", clk.t, D("100000"), D("60000"), True, "v1"))
    j.close()
    add_wl(db, *chain_symbols)
    p = Prov(NOW)
    for t in chain_symbols:
        p.set(t, spot=61.25, strikes=(strikes or {}).get(t, DEFAULT_STRIKES), **(chain_over or {}).get(t, {}))
    refresh(db, p, NOW)
    return db


class B1:
    """Dash + approved file + helpers to drive preview/confirm like a browser."""

    def __init__(self, tmp: Path, *, approved=APPROVED, now=DASH_NOW, **kw):
        self.tmp = tmp
        self.db = build_db(tmp, **kw)
        self.appr = write_approved_custom(tmp / "approved.json", approved)
        self.d = Dash(self.db, self.appr, now=now)
        assert self.d.login() == 303

    def stop(self):
        self.d.stop()

    # ---- http
    def get(self, path, **kw):
        return self.d.req("GET", path, **kw)

    def csrf(self, path="/checkin"):
        st, _, body = self.get(path)
        assert st == 200, (path, st)
        return re.search(r'name="csrf"\s+value="([0-9a-f]{64})"', body).group(1)

    def post(self, path, fields, *, csrf="auto", origin="auto", headers=None, host=None, auth=True, raw: bytes | None = None):
        if csrf == "auto":
            csrf = self.csrf()
        f = dict(fields)
        if csrf is not None:
            f = {"csrf": csrf, **f}
        body = raw if raw is not None else urlencode(f).encode()
        return self.d.req("POST", path, body=body, origin=self.d.origin if origin == "auto" else origin,
                          headers=FORM if headers is None else headers, host=host, auth=auth)

    # ---- flows
    def preview(self, fields, path="/trade/preview"):
        return self.post(path, fields)

    @staticmethod
    def hidden(html: str) -> dict[str, str]:
        out = {}
        for tag in HIDDEN.findall(html):
            a = dict(ATTR.findall(tag))
            if "name" in a:
                out[a["name"]] = htmllib.unescape(a.get("value", ""))
        return out

    def confirm_fields(self, preview_html: str, reason="paper fill reported by owner", acks=None, csrf=None):
        h = self.hidden(preview_html)
        h.pop("csrf", None)
        f = {**h, "reason": reason}
        for a in acks if acks is not None else self.ack_names(preview_html):
            f[a] = "1"
        return f

    @staticmethod
    def ack_names(html: str) -> list[str]:
        return re.findall(r'name="(ack_[a-z_]+)"', html)

    def do(self, fields, *, reason="paper fill reported by owner", path="/trade/preview", cpath="/trade/confirm", acks=None):
        st, _, html = self.preview(fields, path)
        assert st == 200
        cf = self.confirm_fields(html, reason, acks)
        assert "tok" in cf, "preview did not produce a confirmable form: " + text_of(html)[:600]
        st, hd, _ = self.post(cpath, cf)
        return st, hd.get("location", ""), html

    def result_code(self, loc):
        from urllib.parse import parse_qs, urlsplit
        return parse_qs(urlsplit(loc).query).get("code", [""])[0]

    # ---- db reads (never writes)
    def sql(self, q, args=()):
        c = sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return c.execute(q, args).fetchall()
        finally:
            c.close()


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html)))


def sell(symbol="BBB", strike="30", **over):
    f = {"action": "sell_filled", "symbol": symbol, "expiry": EXP.isoformat(), "strike": strike, "contracts": "1", "credit": "0.45",
         "fees": "0.65", "executed_date": "2026-10-30", "executed_time": "09:30"}
    f.update(over)
    return f


def blockers(w: "B1") -> set[str]:
    """Codes shown as BLOCK on the overview page."""
    return set(re.findall(r"BLOCK (\w+)", text_of(w.get("/")[2])))


def checkin(w: "B1", equity="100000", cash=None, expect="recorded"):
    """Web check-in with NET free cash defaulting to equity - active put collateral read from the journal."""
    if cash is None:
        rows = w.sql("SELECT strike, multiplier, contracts FROM option_positions WHERE state IN "
                     "('PENDING','WORKING','OPEN','CLOSE_WORKING','ADJUSTMENT_REVIEW')")
        cash = str(D(equity) - sum(D(r[0]) * r[1] * r[2] for r in rows))
    w.d.now += timedelta(seconds=60)     # a real clock advances; two snapshots stamped with the same instant collide
    f = {"action": "checkin", "equity": equity, "free_cash": cash, "nlots": "0"}
    st, loc, html = w.do(f, path="/checkin/preview", cpath="/checkin/confirm", reason="daily check-in")
    assert w.result_code(loc) == expect, (loc, text_of(html)[-900:])
    return loc
