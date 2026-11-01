"""Stage B2 section C: portfolio page (WT-05) against an independent hand-computed reference book (see b2_env.py header).

Hand arithmetic (policy.yaml: pilot 15,000; normal 25,000; emergency 30,000; ticker 9,000; sector 10,000; bucket 10,000;
expiry week 15,000; 4 contracts; 1 per underlying; 2 names):
  put collateral  = 7,500 (P1 AAA) + 5,000 (P2 BBB WORKING) + 2,000 (P3 CCC CLOSE_WORKING: a closing order releases nothing)
                    + 1,000 (P4 DDD PENDING) + 500 (P5 EEE) = 16,000                (P6 FFF assignment pending is NOT a reserving put)
  owned stock     = 100 x 40.00 = 4,000       assigned-pending obligation (no lot) = 30 x 100 = 3,000
  aggregate       = 16,000 + 4,000 + 3,000 = 23,000 (< 30,000 emergency)
  counts          = 5 active positions = 5 contracts; 6 lifetime trades (P1..P6); 1 unresolved assignment; 3 working reservations
                    (PENDING + WORKING + CLOSE_WORKING)
  company names   = STOCK-type tickers with an active put or lot = AAA, BBB, CCC, DDD (EEE is an ETF; FFF has no reserving put) = 4
  ticker  : AAA 7,500+4,000 = 11,500 (room -2,500) BBB 5,000 (4,000) CCC 2,000 (7,000) DDD 1,000 (8,000) EEE 500 (8,500)
  sector  : tech 7,500+5,000+4,000 = 16,500 (room -6,500) health 2,000 (8,000) energy 1,000 (9,000) fund 500 (9,500)
  bucket  : semis 7,500+4,000 = 11,500 (-1,500) cloud 5,000 (5,000) pharma 2,000 (8,000) oil 1,000 (9,000) index 500 (9,500)
  week    : 2026-W49 (Dec 4) P1+P2+P5 = 13,000 (room 2,000); 2026-W47 (Nov 20) P3+P4 = 3,000 (room 12,000)
  limits  : pilot 16,000/15,000 (-1,000) ; put vs normal 16,000/25,000 (9,000) ; aggregate 23,000/25,000 (2,000)
            contracts 5/4 (-1) ; most-used underlying 1/1 (0) ; names 4/2 (-2)
"""
from __future__ import annotations

import json
import re
from decimal import Decimal as D

import pytest
from b1_env import checkin, text_of
from b2_env import REF_APPROVED, REF_EXTRAS, chain_env, ref_env
from stage_a_helpers import EXP
from web_helpers import fingerprint

CLOSE_OK = {"fees": "0", "executed_date": "2026-10-30", "executed_time": "10:00"}


@pytest.fixture()
def r(tmp_path):
    w = ref_env(tmp_path)
    yield w
    w.stop()


def api(w):
    st, hd, body = w.get("/api/portfolio")
    assert st == 200
    return json.loads(body)["data"]


def usd(x) -> D:
    return D(str(x).replace("$", "").replace(",", ""))


def money(x: D) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def test_counts_position_vs_contract_vs_lifetime_and_company_names(r):
    c = api(r)["counts"]
    assert (c["active_positions"], c["active_contracts"], c["lifetime_trades"]) == (5, 5, 6)
    assert c["unresolved_assignments"] == 1 and c["working_reservations"] == 3
    assert c["company_names"] == ["AAA", "BBB", "CCC", "DDD"] and c["company_name_count"] == 4
    assert c["by_state"] == {"OPEN": 2, "WORKING": 1, "CLOSE_WORKING": 1, "PENDING": 1, "ASSIGNED_PENDING_RECONCILIATION": 1}
    t = text_of(r.get("/portfolio")[2])
    assert re.search(r"Active positions \(reserve collateral, incl\. working orders\) 5 Active contracts 5", t)
    assert "Lifetime recorded trades (any state, not drafts) 6" in t and "4: AAA, BBB, CCC, DDD" in t


def test_exposure_totals_cash_and_emergency_never_headroom(r):
    d = api(r)
    e = d["exposure"]
    assert (usd(e["put_collateral_usd"]), usd(e["owned_stock_usd"]), usd(e["assigned_pending_usd"]), usd(e["aggregate_usd"])) == \
        (D(16000), D(4000), D(3000), D(23000))
    assert usd(e["emergency_ceiling_usd"]) == D(30000) and e["emergency_breached"] is False
    assert usd(d["cash"]["free_cash_usd"]) == D(60000) and usd(d["cash"]["equity_usd"]) == D(100000)
    t = text_of(r.get("/portfolio")[2])
    for s in ("$16,000.00", "$4,000.00", "$3,000.00", "$23,000.00", "$30,000.00", "$60,000.00"):
        assert s in t, s
    assert "never headroom" in t


GROUPS = {
    "ticker": {"aaa": (7500, 4000, 11500, 1, 9000, -2500), "bbb": (5000, 0, 5000, 1, 9000, 4000), "ccc": (2000, 0, 2000, 1, 9000, 7000),
               "ddd": (1000, 0, 1000, 1, 9000, 8000), "eee": (500, 0, 500, 1, 9000, 8500)},
    "sector": {"tech": (12500, 4000, 16500, 2, 10000, -6500), "health": (2000, 0, 2000, 1, 10000, 8000),
               "energy": (1000, 0, 1000, 1, 10000, 9000), "fund": (500, 0, 500, 1, 10000, 9500)},
    "bucket": {"semis": (7500, 4000, 11500, 1, 10000, -1500), "cloud": (5000, 0, 5000, 1, 10000, 5000),
               "pharma": (2000, 0, 2000, 1, 10000, 8000), "oil": (1000, 0, 1000, 1, 10000, 9000), "index": (500, 0, 500, 1, 10000, 9500)},
    "expiry_week": {"2026-W49": (13000, 0, 13000, 3, 15000, 2000), "2026-W47": (3000, 0, 3000, 2, 15000, 12000)},
}


@pytest.mark.parametrize("dim", list(GROUPS))
def test_exposure_by_ticker_sector_bucket_and_expiry_week_json_and_html(r, dim):
    d = api(r)
    got = {g["key"].lower(): g for g in d["groups"][dim]}
    html_t = text_of(r.get("/portfolio")[2])
    assert set(got) == {k.lower() for k in GROUPS[dim]}
    for key, (put, stock, tot, n, lim, room) in GROUPS[dim].items():
        g = got[key.lower()]
        assert (usd(g["put_collateral_usd"]), usd(g["owned_stock_usd"]), usd(g["exposure_usd"]), g["contracts"], usd(g["limit"]),
                usd(g["room_usd"])) == (D(put), D(stock), D(tot), n, D(lim), D(room)), (dim, key)
        assert g["breached"] is (room < 0) and g["exhausted"] is (room <= 0)
        # the HTML twin shows the same figures, Decimal-exact, on the key's own row
        row = re.search(re.escape(key) + r" (\$[\d,.]+) (\$[\d,.]+) (\$[\d,.]+) (\d+) (\$[\d,.]+) (-?\$[\d,.]+) (within|OVER LIMIT|NO ROOM)", html_t, re.I)
        assert row, (dim, key)
        assert (usd(row.group(1)), usd(row.group(2)), usd(row.group(3)), int(row.group(4)), usd(row.group(5)), usd(row.group(6))) == \
            (D(put), D(stock), D(tot), n, D(lim), D(room)), (dim, key, row.group(0))
        assert (row.group(7) == "OVER LIMIT") is (room < 0)


def test_portfolio_wide_limits_room_and_binding_per_unit(r):
    d = api(r)
    lim = {x["key"] if "key" in x else x["label"]: x for x in d["limits"]}
    exp = {"pilot": (16000, 15000, -1000), "normal_put": (16000, 25000, 9000), "normal_aggregate": (23000, 25000, 2000),
           "contracts": (5, 4, -1), "contracts_underlying": (1, 1, 0), "names": (4, 2, -2)}
    assert set(lim) == set(exp)
    for k, (used, limit, room) in exp.items():
        assert (usd(lim[k]["used"]), usd(lim[k]["limit"]), usd(lim[k]["room"])) == (D(used), D(limit), D(room)), k
        assert lim[k]["breached"] is (room < 0) and lim[k]["exhausted"] is (room <= 0)
    # binding = tightest room within each unit across the limits shown on the page (entries include ticker/sector/bucket/week rows)
    binding = {(e["scope"], e["label"]) for e in d["entries"] if e["binding"]}
    by_unit: dict[str, list] = {}
    for e in d["entries"]:
        by_unit.setdefault(e["unit"], []).append(e)
    for unit, es in by_unit.items():
        best = min(usd(e["room"]) for e in es)
        marked = [e for e in es if e["binding"]]
        assert marked and all(usd(e["room"]) == best for e in marked), (unit, binding)
    assert {e["unit"] for e in d["entries"] if e["binding"]} == {"USD", "contracts", "names"}
    t = text_of(r.get("/portfolio")[2])
    assert t.count("BINDING (tightest in its unit)") == len([e for e in d["entries"] if e["binding"]])


def test_holdings_rows_show_ticker_sector_asset_type_bucket_and_state(r):
    h = {x["ref"]: x for x in api(r)["holdings"]}
    assert set(h) == {"P1", "P2", "P3", "P4", "P5", "P6", "LOT1"}
    UNK = "UNKNOWN (not in approvals)"     # B2-1/WT-05: DDD, FFF are not approved anywhere (CCC is approved STOCK) -> stay unknown, never silently STOCK
    want = {"P1": ("AAA", "tech", "STOCK", "semis", "OPEN", 7500), "P2": ("BBB", "tech", "STOCK", "cloud", "WORKING", 5000),
            "P3": ("CCC", "health", "STOCK", "pharma", "CLOSE_WORKING", 2000), "P4": ("DDD", "energy", UNK, "oil", "PENDING", 1000),
            "P5": ("EEE", "fund", "ETF", "index", "OPEN", 500), "P6": ("FFF", "misc", UNK, "misc", "ASSIGNED_PENDING_RECONCILIATION", 3000),
            "LOT1": ("AAA", "tech", "STOCK", "semis", "owned shares", 4000)}
    for ref, (tk, sec, at, bkt, st, ex) in want.items():
        x = h[ref]
        assert (x["ticker"], x["sector"], x["asset_type"], x["bucket"], x["state"], usd(x["exposure_usd"])) == (tk, sec, at, bkt, st, D(ex)), ref
    assert "does not release" in h["P3"]["counts_as"]                       # closing order keeps the reservation


def test_closing_order_does_not_release_capacity_until_a_closing_fill_is_recorded(r):
    """P3 (CLOSE_WORKING) is counted in pharma/W47/put collateral. After a CONFIRMED closing fill is recorded on the web page the
    2,000 disappears: put collateral 14,000, aggregate 21,000, pharma gone, W47 = 1,000, contracts 4."""
    before = api(r)
    assert usd(before["exposure"]["put_collateral_usd"]) == D(16000)
    st, loc, html = r.do({"action": "close_filled", "trade_id": "P3", "debit": "0.10", **CLOSE_OK})
    assert r.result_code(loc) in ("recorded", "duplicate"), text_of(html)[-500:]
    after = api(r)
    assert usd(after["exposure"]["put_collateral_usd"]) == D(14000) and usd(after["exposure"]["aggregate_usd"]) == D(21000)
    assert after["counts"]["active_contracts"] == 4 and after["counts"]["active_positions"] == 4
    assert after["counts"]["lifetime_trades"] == 6                              # history is not deleted
    assert {g["key"] for g in after["groups"]["bucket"]} == {"semis", "cloud", "oil", "index"}
    assert {g["key"]: usd(g["exposure_usd"]) for g in after["groups"]["expiry_week"]}["2026-W47"] == D(1000)


def test_assignment_pending_counts_in_aggregate_but_a_linked_lot_is_not_double_counted(tmp_path):
    """Spec 920: an assigned put reconciles once and does not double count its converted shares. Book: P6 FFF 30 pending + a lot
    linked to P6 (100 x 30 = 3,000): aggregate must add 3,000 ONCE (not 6,000)."""
    from datetime import timedelta
    from decimal import Decimal
    from acc_helpers import NOW
    from csp_alerts.journal.models import StockLot
    from csp_alerts.journal.store import Journal
    from s3_helpers import Clock
    ex = [e for e in REF_EXTRAS if e["tid"] == "P6"]
    w = ref_env(tmp_path, extras=ex, lot=False)
    try:
        j = Journal(w.db, clock=Clock(NOW - timedelta(minutes=50)))
        j.add_stock_lot(StockLot("LF", "FFF", Decimal("100"), Decimal("30"), NOW - timedelta(hours=1), "Misc", "Misc", "ASSIGNMENT",
                                 "STOCK", None, "P6"))
        j.close()
        e = api(w)["exposure"]
        assert usd(e["owned_stock_usd"]) == D(3000) and usd(e["assigned_pending_usd"]) == D(0) and usd(e["aggregate_usd"]) == D(3000)
    finally:
        w.stop()


def test_unknown_labels_are_flagged_banner_and_never_counted_as_diversified(tmp_path):
    ex = [dict(tid="U1", ticker="UUU", strike="20", sector="unknown", bucket="unknown", state="OPEN"),
          dict(tid="U2", ticker="VVV", strike="20", sector="unknown", bucket="Tech", state="OPEN")]
    w = ref_env(tmp_path, extras=ex, lot=False)
    try:
        d = api(w)
        assert d["unknown_tickers"] == ["UUU", "VVV"]
        sec = {g["key"]: g for g in d["groups"]["sector"]}
        assert sec["unknown"]["unknown"] is True and usd(sec["unknown"]["exposure_usd"]) == D(4000)       # pooled 2,000 + 2,000
        assert {x["ref"]: x["unknown"] for x in d["holdings"]} == {"U1": True, "U2": True}
        t = text_of(w.get("/portfolio")[2])
        assert re.search(r"unknown", t, re.I) and "never counted as diversified" in t
        assert "/portfolio/labels/UUU" in w.get("/portfolio")[2]
    finally:
        w.stop()


def test_empty_book_and_no_snapshot_say_unknown_never_zero_cash(tmp_path):
    w = ref_env(tmp_path, extras=(), lot=False, with_snapshot=False)
    try:
        st, _, h = w.get("/portfolio")
        assert st == 200
        d = api(w)
        assert d["cash"] is None and d["counts"]["active_positions"] == 0 and d["counts"]["lifetime_trades"] == 0
        assert re.search(r"unknown|no snapshot|none", text_of(h), re.I)
        assert "$0.00 free" not in text_of(h)
    finally:
        w.stop()


def test_portfolio_get_is_read_only_and_html_json_agree_on_every_holding_amount(r):
    before = fingerprint(r.db)
    h = text_of(r.get("/portfolio")[2])
    d = api(r)
    for x in d["holdings"]:
        assert money(usd(x["exposure_usd"])) in h and x["ref"] in h
    assert fingerprint(r.db) == before


def test_mitigation_text_is_rule_based_with_no_advice_and_names_binding_rule(r):
    h = text_of(r.get("/portfolio")[2])
    assert "recommends no trade, hedge, alternative sector or movement of capital" in h
    low = h.lower()
    for bad in ("you should", "we recommend", "consider selling", "consider buying", "diversif" + "y into", "rebalance", "hedge with"):
        assert bad not in low, bad
    assert "binds hardest" in low and "sector tech" in low
    d = api(r)
    assert d["mitigation"], "mitigation lines missing"


def test_asset_type_source_is_truthful_when_the_approval_exists_only_in_the_file(tmp_path):
    """Asset type is read only from the journal's approved_underlyings (filled by `csp scan`). If the owner approved an ETF in the
    FILE but no scan has synced it yet, the page must not claim the 'approval record' decided the type while counting the ETF as an
    individual company. Expected: either ETF (not a company name) or an explicit not-yet-synced note."""
    w = ref_env(tmp_path, sync=False)
    try:
        d = api(w)
        eee = next(x for x in d["holdings"] if x["ref"] == "P5")
        names = d["counts"]["company_names"]
        consistent = (eee["asset_type"] == "ETF" and "EEE" not in names) or \
                     (eee["asset_type"] == "STOCK" and "EEE" in names and "approval record" not in eee["asset_type_source"])
        assert consistent, (eee["asset_type"], eee["asset_type_source"], names)
    finally:
        w.stop()
