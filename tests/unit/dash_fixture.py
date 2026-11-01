"""Synthetic journal + live server helpers for the dashboard tests. ALL VALUES ARE SYNTHETIC."""
from __future__ import annotations

import http.client
import json
import threading
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from chain_fixtures import raw_row
from factories import NOW, ROOT, TODAY, approval, policy, snap

from csp_alerts.jobs.scan import run_scan
from csp_alerts.journal.models import PositionState as S, StockLot
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import RawChain
from csp_dashboard import auth, views
from csp_dashboard.server import DashboardServer

PASSWORD = "correct horse battery"
HASH = auth.make_hash(PASSWORD, n=16)        # tiny cost parameter: tests only
POLICY = ROOT / "config" / "policy.yaml"
EVIL = "<script>alert(1)</script> ‮&amp;\x1b[31m"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Src:
    def __init__(self, chains):
        self.chains = chains

    def fetch_chain(self, ticker, a, b):
        return RawChain(ticker, list(self.chains.get(ticker, [])), NOW)

    def fetch_earnings_dates(self, ticker):
        return None


@dataclass
class Paths:
    db: Path
    approved: Path
    clock: Clock

    def sources(self, now=NOW) -> views.Sources:
        return views.Sources(self.db, POLICY, self.approved, "America/Chicago", lambda: now)


def approved_file(path: Path, tickers=("AAA",)) -> Path:
    recs = [json.loads(approval(ticker=t, asset_type="ETF", sector=f"sec_{t}", correlation_bucket=f"b_{t}",
                                max_buy_strike=D("100"), standard_deliverable_confirmed_on=TODAY,
                                events_cleared_through=TODAY + timedelta(days=400),
                                ownership_reason="owner diligence notes").model_dump_json()) for t in tickers]
    path.write_text(json.dumps({"approved_underlyings": recs}), encoding="utf-8")
    return path


def build(tmp: Path, *, scan: bool = True, tickers=("AAA",), evil_notes: bool = True, chains: dict | None = None) -> Paths:
    db = tmp / "j.sqlite3"
    clk = Clock(NOW - timedelta(hours=4))
    j = Journal(db, clock=clk)
    j.migrate()
    j.record_policy(policy())
    j.confirm_baseline(D("100000"))
    exp = TODAY + timedelta(days=40)
    j.create_draft(NewPosition("P-OPEN", "BBB", exp, D("40"), 100, 1, "sec_b", "b_b", notes=EVIL if evil_notes else ""))
    j.transition("P-OPEN", S.PENDING)
    j.transition("P-OPEN", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.50"), opened_at_utc=clk.t, fees_usd=D("0.65"))
    j.create_draft(NewPosition("P-PEND", "CCC", exp, D("30"), 100, 1, "sec_c", "b_c"))
    j.transition("P-PEND", S.PENDING)
    j.create_draft(NewPosition("P-DONE", "DDD", exp, D("20"), 100, 1, "sec_d", "b_d"))
    j.transition("P-DONE", S.PENDING)
    j.transition("P-DONE", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.40"), opened_at_utc=clk.t)
    j.transition("P-DONE", S.CLOSE_WORKING)
    j.transition("P-DONE", S.CLOSED, actual_close_debit_per_share=D("0.10"), closed_at_utc=clk.t, fees_usd=D("1.30"))
    j.add_stock_lot(StockLot("L-1", "EEE", D("100"), D("10"), clk.t, "sec_e", "b_e", "PURCHASE", asset_type="ETF"))
    clk.t = NOW - timedelta(hours=1)
    j.add_snapshot(snap(equity="100000", cash="60000", asof=clk.t, sid="s1"))
    clk.t = NOW
    if scan:
        appr = [approval(ticker=t, asset_type="ETF", sector=f"sec_{t}", correlation_bucket=f"b_{t}", max_buy_strike=D("100"),
                         standard_deliverable_confirmed_on=TODAY, events_cleared_through=TODAY + timedelta(days=400))
                for t in tickers]
        run_scan(journal=j, policy=policy(), approved=appr, source=Src(chains if chains is not None else {t: [raw_row(), raw_row(strike=39.0, bid=0.0, ask=0.05)] for t in tickers}),
                 now_utc=NOW, session=SessionInfo(SessionStatus.OPEN, 300), sleep=lambda s: None)
    for i, (st, cls, sev, tid) in enumerate([("SENT", "ENTRY_RESEARCH", "INFO", None), ("UNKNOWN_ACK", "TIME_EXIT", "ACTION", "P-OPEN"),
                                              ("DROPPED_STALE", "PROFIT_EXIT", "ACTION", "P-OPEN"), ("RETRY", "DATA_ISSUE", "WARN", None)]):
        ev = {"revalidate": {"trade_id": tid, "trigger": cls} if tid else {"ticker": "AAA"}, "policy_sha": policy().sha256,
              "last_error": "token 123 redacted", "part": 1, "parts": 1}
        j.conn.execute("INSERT INTO alert_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (f"alert-{i}", f"k{i}", cls, sev, st, "2026-10-30T15:00:00Z", None, i, None, f"hello {EVIL}", json.dumps(ev)))
    j.close()
    return Paths(db, approved_file(tmp / "approved.json", tickers), clk)


class Client:
    def __init__(self, srv: DashboardServer):
        self.port = srv.server_address[1]
        self.cookie: str | None = None

    def req(self, method, path, body=None, headers=None, host=None, origin="auto"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if method == "POST" and origin == "auto":
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin not in ("auto", None):
            h["Origin"] = origin
        if self.cookie:
            h["Cookie"] = f"csp_session={self.cookie}"
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, data, hdrs

    def login(self, pw=PASSWORD):
        st, _, h = self.req("POST", "/login", body="password=" + pw.replace(" ", "+"))
        if st == 303:
            self.cookie = h["set-cookie"].split(";")[0].split("=", 1)[1]
        return st

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def api(self, path, **kw):
        st, body, _ = self.get("/api" + path, **kw)
        assert st == 200, (path, st, body[:200])
        return json.loads(body)


class Running:
    def __init__(self, paths: Paths, now=NOW):
        self.srv = DashboardServer(paths.sources(now), HASH, port=0)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self) -> Client:
        self.t.start()
        c = Client(self.srv)
        assert c.login() == 303
        return c

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
