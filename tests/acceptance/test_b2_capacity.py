"""Stage B2 section D: per-candidate capacity on the market page (WT-05 'for each candidate display current exposure, proposed
addition, post-trade exposure, remaining capacity and each failing rule'; PRODUCT_WORKFLOW budget baseline table; AT-17 boundaries:
equality allowed, overage blocked).

Policy (config/policy.yaml, read independently of src): pilot 15,000 / normal 25,000 / ticker 9,000 / sector 10,000 / bucket 10,000 /
expiry-week 15,000 / 4 contracts / 1 per underlying / 2 names. Collateral = strike x 100 x 1.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal as D

import pytest
from b1_env import DASH_NOW, text_of
from b2_env import chain_env, occ, row_cells, rule_rows

AP_S = {"AAA": ("STOCK", "Tech", "Semis", "100.00"), "BBB": ("STOCK", "Health", "Cloud", "100.00")}
BOOK_S = [dict(tid="P1", ticker="AAA", strike="75", sector="Tech", bucket="Semis")]      # AAA 75 OPEN = 7,500, week 2026-W49
SPOTS = {"AAA": 100.0, "BBB": 100.0, "ZZZ": 100.0}


def usd(x: str) -> D:
    return D(x.replace("$", "").replace(",", ""))


def plain(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


@pytest.fixture()
def s_env(tmp_path):
    w = chain_env(tmp_path, approved=AP_S, extras=BOOK_S, spots=SPOTS, strikes=(74.0, 75.0, 76.0))
    yield w
    w.stop()


@pytest.mark.parametrize("strike", [74, 75, 76])
def test_pilot_and_expiry_week_boundary_equality_passes_overage_fails(s_env, strike):
    """Book: pilot used 7,500 (AAA 75 x 100). Candidate BBB strike k: adds 100k.
    k=74: after 7,400+7,500 = 14,900 -> room 100 -> passes.   k=75: 15,000 -> room 0 -> equality passes.
    k=76: 15,100 -> room -100 -> pilot AND expiry-week (same week, same total) fail; every other rule within."""
    h = s_env.get("/market/BBB")[2]
    cells = row_cells(h, occ("BBB", strike))
    cap = cells[14]
    rr = rule_rows(cap)
    add = D(strike * 100)
    after = D(7500) + add
    pil = rr["Pilot put collateral"]
    assert (usd(pil[0]), usd(pil[1]), usd(pil[2]), usd(pil[3]), usd(pil[4])) == (D(15000), D(7500), add, after, D(15000) - after)
    wk = rr["Expiration-week put collateral"]
    assert (usd(wk[1]), usd(wk[3]), usd(wk[4])) == (D(7500), after, D(15000) - after)
    assert rr["Ticker BBB (puts + shares)"][1:3] == ("$0.00", f"${add:,.2f}")
    for k, v in rr.items():
        if k not in ("Pilot put collateral", "Expiration-week put collateral"):
            assert v[-1] == "within", (k, v)
    if strike <= 75:
        assert "passes all gates" in cap and pil[-1] == "within"
    else:
        assert "fails 2 rule(s)" in cap and "PILOT_COLLATERAL_LIMIT" in cap and "EXPIRY_WEEK_LIMIT" in cap
        assert "passes all gates" not in cap
        assert pil[-1] == wk[-1] == "BREACH"
    assert plain(cells[4]) == "0.5" and plain(cells[5]) == "0.54"             # prices never hidden by capacity


def test_summary_line_states_current_proposed_post_and_tightest_room(s_env):
    cap = row_cells(s_env.get("/market/BBB")[2], occ("BBB", 76))[14]
    t = text_of(cap)
    # current aggregate 7,500 (AAA put); proposed 7,600; post 15,100; tightest room after = -100
    assert "exposure $7,500.00 + $7,600.00 = $15,100.00" in t and "-$100.00" in t and "EXHAUSTED" in t


def test_existing_underlying_fails_contract_count_and_names_row_not_misleading(s_env):
    """AAA already holds 1 contract (1 per underlying) so AAA 74 -> CONTRACT_COUNT_LIMIT regardless of dollars."""
    cap = row_cells(s_env.get("/market/AAA")[2], occ("AAA", 74))[14]
    rr = rule_rows(cap)
    assert rr["Contracts on AAA"][-1] == "BREACH" and rr["Contracts on AAA"][3] == "2"
    assert "CONTRACT_COUNT_LIMIT" in cap
    nm = rr["Individual-company names (puts + shares)"]
    assert nm[1:4] == ("1", "0", "1") and nm[-1] == "within"


def test_unapproved_ticker_shows_prices_not_approved_and_no_eligible_marker(s_env):
    h = s_env.get("/market/ZZZ")[2]
    cells = row_cells(h, occ("ZZZ", 75))
    assert plain(cells[4]) == "0.5"
    cap = text_of(cells[14])
    assert "NOT APPROVED" in cap and "NOT_APPROVED" in cap and "passes all gates" not in cap
    assert "passes all gates" not in text_of(h)
    assert "Not approved for entry alerts" in text_of(h)


def test_stale_data_blocks_the_eligible_marker_but_keeps_prices(s_env):
    """Retrieval at NOW; page clock NOW+20 min while the market is open: older than the 900 s open-market limit => STALE."""
    s_env.d.now = DASH_NOW + timedelta(minutes=20)
    h = s_env.get("/market/BBB")[2]
    cells = row_cells(h, occ("BBB", 74))
    assert "passes all gates" not in text_of(cells[14]), text_of(cells[14])
    assert plain(cells[4]) == "0.5"
    assert "STALE" in h


def test_fresh_data_unapproved_never_eligible_even_when_capacity_free(tmp_path):
    w = chain_env(tmp_path, approved=AP_S, extras=[], spots={"BBB": 100.0, "ZZZ": 100.0}, strikes=(50.0,))
    try:
        assert "passes all gates" in text_of(row_cells(w.get("/market/BBB")[2], occ("BBB", 50))[14])
        assert "passes all gates" not in text_of(row_cells(w.get("/market/ZZZ")[2], occ("ZZZ", 50))[14])
    finally:
        w.stop()


LOTS = [("LOT1", "AAA", "100", "40.00", "Tech", "Semis")]      # AAA 100 x 40 = 4,000, no puts


@pytest.mark.parametrize("strike,fails", [
    (50, set()),                                                  # ticker 4,000+5,000 = 9,000 (equality) ; sector/bucket 9,000
    (51, {"SINGLE_TICKER_LIMIT"}),                                # 9,100 > 9,000 ; sector 9,100 <= 10,000
    (60, {"SINGLE_TICKER_LIMIT"}),                                # ticker 10,000 > 9,000 ; sector/bucket 10,000 equality passes
    (61, {"SINGLE_TICKER_LIMIT", "SECTOR_LIMIT", "CORRELATED_LIMIT"}),   # 10,100 > all three
])
def test_shares_count_toward_ticker_sector_and_bucket_with_exact_boundaries(tmp_path, strike, fails):
    w = chain_env(tmp_path, approved=AP_S, lots=LOTS, spots={"AAA": 100.0}, strikes=(50.0, 51.0, 60.0, 61.0))
    try:
        cap = row_cells(w.get("/market/AAA")[2], occ("AAA", strike))[14]
        rr = rule_rows(cap)
        assert usd(rr["Ticker AAA (puts + shares)"][1]) == D(4000)
        assert usd(rr["Ticker AAA (puts + shares)"][3]) == D(4000 + 100 * strike)
        got = set(re.findall(r"[A-Z]+(?:_[A-Z]+)*_LIMIT", cap.split("<details>")[0]))
        assert got == fails, (strike, got)
        assert ("passes all gates" in cap) is (not fails)
        assert usd(rr["Aggregate equity risk vs normal ceiling"][1]) == D(4000)
        assert usd(rr["Put collateral vs normal ceiling"][1]) == D(0)
    finally:
        w.stop()


def test_company_name_cap_third_individual_name_fails_etf_does_not(tmp_path):
    """Book: AAA put + CCC put = 2 names. Candidate BBB (STOCK) would be name 3 -> INDIVIDUAL_NAME_LIMIT (used 2, adds 1, after 3,
    room -1). An approved ETF candidate (EEE) is counted separately: adds 0."""
    ap = {**AP_S, "CCC": ("STOCK", "Energy", "Oil", "100.00"), "EEE": ("ETF", "Fund", "Index", "100.00")}
    ex = [dict(tid="P1", ticker="AAA", strike="30", sector="Tech", bucket="Semis"),
          dict(tid="P2", ticker="CCC", strike="30", sector="Energy", bucket="Oil")]
    w = chain_env(tmp_path, approved=ap, extras=ex, spots={"BBB": 100.0, "EEE": 100.0}, strikes=(30.0,))
    try:
        b = row_cells(w.get("/market/BBB")[2], occ("BBB", 30))[14]
        assert "INDIVIDUAL_NAME_LIMIT" in b
        assert rule_rows(b)["Individual-company names (puts + shares)"][1:5] == ("2", "1", "3", "-1")
        e = row_cells(w.get("/market/EEE")[2], occ("EEE", 30))[14]
        assert "INDIVIDUAL_NAME_LIMIT" not in e
        assert rule_rows(e)["Individual-company names (puts + shares)"][1:3] == ("2", "0")
    finally:
        w.stop()


def test_free_cash_boundary_exactly_equal_passes_one_dollar_short_fails(tmp_path):
    """NET free cash 60,000. Strike 600 needs 60,000 -> cash row within (equality); 601 needs 60,100 -> breach."""
    ap = {"BBB": ("STOCK", "Health", "Cloud", "1000.00")}
    w = chain_env(tmp_path, approved=ap, spots={"BBB": 1000.0}, strikes=(600.0, 601.0))
    try:
        h = w.get("/market/BBB")[2]
        for k, ok in ((600, True), (601, False)):
            cash = rule_rows(row_cells(h, occ("BBB", k))[14])["Free cash (latest NET snapshot)"]
            assert usd(cash[0]) == D(60000) and usd(cash[2]) == D(k * 100)
            assert (cash[-1] == "within") is ok, (k, cash)
    finally:
        w.stop()


def test_page_size_bounded_for_a_large_chain_and_every_row_keeps_a_verdict(tmp_path):
    strikes = tuple(float(x) for x in range(1, 100)) + tuple(x + 0.5 for x in range(1, 100))   # 198 puts
    w = chain_env(tmp_path, approved=AP_S, spots={"BBB": 100.0}, strikes=strikes)
    try:
        h = w.get("/market/BBB")[2]
        assert len(re.findall(r"<tr><td><code>BBB\d{6}P", h)) == len(strikes)
        assert h.count("<details>") <= 45
        assert len(h.encode()) < 1_500_000
        for k in (1, 50, 99):
            assert re.search(r"passes all gates|fails \d+ rule", text_of(row_cells(h, occ("BBB", k))[14])), k
    finally:
        w.stop()


def test_truncated_rule_tables_are_disclosed_not_silently_missing(tmp_path):
    """Rows beyond the first 40 have no per-rule table; the page must say so (nothing may look like 'no rules apply')."""
    strikes = tuple(float(x) for x in range(1, 100))
    w = chain_env(tmp_path, approved=AP_S, spots={"BBB": 100.0}, strikes=strikes)
    try:
        h = w.get("/market/BBB")[2]
        late = row_cells(h, occ("BBB", 99))[14]
        assert "<details>" not in late                       # the cap really truncated (otherwise this test would be vacuous)
        assert "expanded for the first 40 rows" in text_of(h) and "every row still shows its verdict" in text_of(h)
        assert "fails 1 rule(s)" in text_of(late) and "SINGLE_TICKER_LIMIT" in late      # 9,900 > 9,000 ticker cap
    finally:
        w.stop()


def test_header_pins_market_columns_and_navigation(s_env):
    h = s_env.get("/market/BBB")[2]
    heads = re.findall(r'<th scope="col">([^<]*)</th>', h)
    assert "Contract-gate marker" in heads and "All entry gates, exposure and capacity" in heads
    assert "Entry-gate marker" not in heads
    nav = text_of(h[:h.index("</nav>")] if "</nav>" in h else h[:3000])
    for item in ("Overview", "Watchlist", "Candidates", "Positions", "Portfolio", "Check-in", "Alerts"):
        assert item in nav, item


@pytest.mark.parametrize("mode,used,after,fails", [("synced", "1", "2", False), ("file_only", "1", "2", False), ("not_approved", "2", "3", True)])
def test_market_capacity_names_row_uses_the_one_resolver_for_an_existing_etf_put(tmp_path, mode, used, after, fails):
    """B2-1. Book: AAA put (STOCK) + EEE put. Candidate CCC (STOCK, approved) adds 1 name. Limit 2.
    EEE approved as ETF (journal-synced OR only in the file): used 1 (AAA), after 2 <= 2, room 0 -> no breach.
    EEE absent from approvals: type UNKNOWN is counted conservatively as a company: used 2, after 3 > 2 -> INDIVIDUAL_NAME_LIMIT."""
    ap = {**AP_S, "CCC": ("STOCK", "Energy", "Oil", "100.00")}
    if mode != "not_approved":
        ap["EEE"] = ("ETF", "Fund", "Index", "100.00")
    ex = [dict(tid="P1", ticker="AAA", strike="30", sector="Tech", bucket="Semis"),
          dict(tid="P2", ticker="EEE", strike="30", sector="Fund", bucket="Index")]
    w = chain_env(tmp_path, approved=ap, extras=ex, spots={"CCC": 100.0}, strikes=(30.0,), sync=(mode == "synced"))
    try:
        c = row_cells(w.get("/market/CCC")[2], occ("CCC", 30))[14]
        r = rule_rows(c)["Individual-company names (puts + shares)"]
        assert r[1:4] == (used, "1", after), r
        assert ("INDIVIDUAL_NAME_LIMIT" in c) is fails
    finally:
        w.stop()
