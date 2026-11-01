"""Stage B2 acceptance environment (tester-owned, SYNTHETIC).

Reference book R (hand-computed oracle; policy.yaml values: pilot 15,000, normal 25,000, emergency 30,000, ticker 9,000,
sector 10,000, bucket 10,000, expiry-week 15,000, 4 contracts, 1 per underlying, 2 names):
  puts (strike x 100 x 1):
    P1 AAA 75 OPEN             exp 2026-12-04 (ISO 2026-W49) sector Tech   bucket Semis  -> 7,500
    P2 BBB 50 WORKING          exp 2026-12-04 (W49)          sector Tech   bucket Cloud  -> 5,000
    P3 CCC 20 CLOSE_WORKING    exp 2026-11-20 (W47)          sector Health bucket Pharma -> 2,000
    P4 DDD 10 PENDING          exp 2026-11-20 (W47)          sector Energy bucket Oil    -> 1,000
    P5 EEE  5 OPEN (ETF)       exp 2026-12-04 (W49)          sector Fund   bucket Index  ->   500
    P6 FFF 30 ASSIGNED_PENDING exp 2026-11-20                sector Misc   bucket Misc   -> 3,000 (obligation, no lot)
  stock: LOT AAA 100 sh x 40.00 = 4,000 sector Tech bucket Semis
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

from acc_helpers import NOW
from b1_env import APPROVED, B1, EXP_OTHER, build_db
from stage_a_helpers import EXP

from csp_alerts.journal.models import AccountSnapshot, StockLot
from csp_alerts.journal.store import Journal

REF_APPROVED = {"AAA": ("STOCK", "Tech", "Semis", "100.00"), "BBB": ("STOCK", "Tech", "Cloud", "100.00"),
                "CCC": ("STOCK", "Health", "Pharma", "100.00"), "EEE": ("ETF", "Fund", "Index", "100.00")}
# DDD, FFF are deliberately NOT approved (asset type defaults to STOCK = counts as an individual company)

REF_EXTRAS = (
    dict(tid="P1", ticker="AAA", strike="75", exp=EXP, sector="Tech", bucket="Semis", state="OPEN"),
    dict(tid="P2", ticker="BBB", strike="50", exp=EXP, sector="Tech", bucket="Cloud", state="WORKING"),
    dict(tid="P3", ticker="CCC", strike="20", exp=EXP_OTHER, sector="Health", bucket="Pharma", state="CLOSE_WORKING"),
    dict(tid="P4", ticker="DDD", strike="10", exp=EXP_OTHER, sector="Energy", bucket="Oil", state="PENDING"),
    dict(tid="P5", ticker="EEE", strike="5", exp=EXP, sector="Fund", bucket="Index", state="OPEN"),
    dict(tid="P6", ticker="FFF", strike="30", exp=EXP_OTHER, sector="Misc", bucket="Misc", state="ASSIGNED_PENDING_RECONCILIATION"),
)


def add_lot(db, lot_id, ticker, shares, price, sector, bucket, asset_type="STOCK", at=None):
    from s3_helpers import Clock
    j = Journal(db, clock=Clock(at or (NOW - timedelta(hours=1))))
    j.add_stock_lot(StockLot(lot_id, ticker, D(shares), D(price), (at or NOW) - timedelta(hours=2), sector, bucket, "OWNED", asset_type, None, None))
    j.close()


def ref_env(tmp, *, approved=REF_APPROVED, lot=True, extras=REF_EXTRAS, sync=True, **kw) -> B1:
    """B1 harness over the reference book (no default OPN position)."""
    import b1_env
    orig = b1_env.build_db

    def bd(t, **k):
        db = orig(t, existing=False, extras=extras, chain_symbols=k.pop("chain_symbols", ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "ZZZ")), **k)
        if lot:
            add_lot(db, "LOT1", "AAA", "100", "40.00", "Tech", "Semis")
        return db
    b1_env.build_db = bd
    try:
        x = B1(tmp, approved=approved, **kw)
    finally:
        b1_env.build_db = orig
    if sync:        # what `csp scan` does: make the journal's approved_underlyings (the ONLY asset-type store) equal the file
        from csp_alerts.policy.approved import load_approved
        j = Journal(x.db)
        j.sync_approved(load_approved(x.appr))
        j.close()
    return x


# ------------------------------------------------------------------------------------------------ market-page helpers
import re  # noqa: E402

from stage_a_helpers import Prov, add_wl, refresh  # noqa: E402


def chain_env(tmp, *, approved, extras=(), lots=(), spots=None, strikes=(75.0,), existing=False, now=None, sync=True, chain_symbols=()):
    """Harness with NO stored chain, then one refresh at NOW with the given spots/strikes for every symbol in `spots`."""
    import b1_env
    orig = b1_env.build_db

    def bd(t, **k):
        o_ref, o_wl = b1_env.refresh, b1_env.add_wl
        b1_env.refresh = lambda *a, **kw: None            # the base builder must not fetch (a second fetch inside 60 s is skipped)
        b1_env.add_wl = lambda *a, **kw: None
        try:
            db = orig(t, existing=existing, extras=extras, chain_symbols=chain_symbols, **k)
        finally:
            b1_env.refresh, b1_env.add_wl = o_ref, o_wl
        for lot in lots:
            add_lot(db, *lot, at=NOW - timedelta(hours=1, minutes=30))
        if lots:        # a fresh confirmed snapshot AFTER the lots (otherwise CHANGE_AFTER_SNAPSHOT blocks every entry)
            from s3_helpers import Clock
            j = Journal(db, clock=Clock(NOW - timedelta(hours=1)))
            j.add_snapshot(AccountSnapshot("s1", NOW - timedelta(hours=1), D("100000"), D("60000"), True, "v1"))
            j.close()
        return db
    b1_env.build_db = bd
    try:
        kw = {} if now is None else {"now": now}
        x = B1(tmp, approved=approved, **kw)
    finally:
        b1_env.build_db = orig
    if sync:
        from csp_alerts.policy.approved import load_approved
        j = Journal(x.db)
        j.sync_approved(load_approved(x.appr))
        j.close()
    add_wl(x.db, *spots)
    p = Prov(NOW)
    for t, spot in spots.items():
        p.set(t, spot=spot, strikes=strikes)
    refresh(x.db, p, NOW)
    return x


def row_cells(html: str, occ: str) -> list[str]:
    """Inner HTML of each top-level <td> of the put-chain row whose first cell is <code>occ</code> (nested tables respected)."""
    i = html.index(f"<tr><td><code>{occ}</code>")
    j = i + 4
    cells, depth, start = [], 0, None
    for m in re.finditer(r"<(/?)(td|table)\b[^>]*>", html[j:]):
        tag, closing = m.group(2), m.group(1) == "/"
        pos = j + m.start()
        if tag == "table":
            depth += -1 if closing else 1
        elif tag == "td":
            if not closing and depth == 0:
                start = j + m.end()
            elif closing and depth == 0:
                cells.append(html[start:pos])
                if len(cells) == 16:
                    break
    return cells


def occ(sym: str, strike: int, ymd="261204") -> str:
    return f"{sym}{ymd}P{strike * 1000:08d}"


def rule_rows(cell: str) -> dict[str, tuple[str, ...]]:
    """capacity-by-rule table -> {rule label: (limit, used now, adds, used after, room after, result text)}"""
    out = {}
    for tr in re.findall(r"<tr>(<td>.*?)</tr>", cell, re.S):
        tds = [re.sub(r"<[^>]+>", "", t).strip() for t in re.findall(r"<td>(.*?)</td>", tr, re.S)]
        if tds:
            out[tds[0]] = tuple(tds[1:])
    return out
