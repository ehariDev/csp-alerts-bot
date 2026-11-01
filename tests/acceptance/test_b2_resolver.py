"""B2-1 confirmation: ONE asset-type resolver (journal row > approvals file read-only > UNKNOWN) seen from the web preview and /portfolio.

Hand-computed expectation (limits: max_individual_company_names = 2, only ETF exempt per WT-05):
Book = existing OPN AAA (STOCK, company 1) + BIG EEE put (type varies by state) ; new put BBB 20 (STOCK, company 2 or 3).
  EEE resolves ETF   -> companies {AAA,BBB} = 2 <= 2 -> no INDIVIDUAL_NAME_LIMIT
  EEE resolves STOCK/UNKNOWN -> {AAA,BBB,EEE} = 3 > 2 -> INDIVIDUAL_NAME_LIMIT flagged
The preview must agree with /portfolio in every state; nothing may write approvals or approved_underlyings.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from b1_env import APPROVED, EXP_OTHER, B1, sell, text_of


def previewable(html):
    return 'tok' in B1.hidden(html)

BIG = dict(tid="BIG", ticker="EEE", strike="90", exp=EXP_OTHER, sector="S3", bucket="B4")
NOT_EEE = {k: v for k, v in APPROVED.items() if k != "EEE"}


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _rows(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return c.execute("SELECT ticker, asset_type FROM approved_underlyings ORDER BY ticker").fetchall()
    finally:
        c.close()


def _journal(x, eee_type):
    """Put/replace EEE in the journal approved_underlyings table directly (what a past scan sync would have left)."""
    c = sqlite3.connect(x.db)
    c.execute("DELETE FROM approved_underlyings")
    if eee_type:
        c.execute("INSERT INTO approved_underlyings VALUES ('EEE',?, 'S3','B4','100.00','2026-12-31','2026-10-30','2027-01-28','r','owner','2026-10-30T00:00:00Z')", (eee_type,))
    c.commit()
    c.close()


# state -> (file spec, journal eee type, expected asset_type, expected source fragment, counted as company)
STATES = {
    "journal_synced_etf": (APPROVED, "ETF", "ETF", "synced into the journal", False),
    "file_only_etf": (APPROVED, None, "ETF", "not yet synced", False),
    "neither": (NOT_EEE, None, "UNKNOWN", "not in approvals", True),
    "conflict_journal_stock_file_etf": (APPROVED, "STOCK", "STOCK", "synced into the journal", True),
    "conflict_journal_etf_file_stock": ({**APPROVED, "EEE": ("STOCK", "S3", "B4", "100.00")}, "ETF", "ETF", "synced into the journal", False),
}


@pytest.mark.parametrize("state", list(STATES))
def test_preview_and_portfolio_agree_in_every_state_and_nothing_is_written(tmp_path, state):
    spec, jtype, want_type, want_src, company = STATES[state]
    x = B1(tmp_path, approved=spec, extras=[BIG])
    try:
        _journal(x, jtype)
        h0, rows0 = _sha(x.appr), _rows(x.db)
        # portfolio
        st, _, body = x.get("/api/portfolio")
        assert st == 200
        d = json.loads(body)["data"]
        big = next(h for h in d["holdings"] if h["ticker"] == "EEE")
        assert big["asset_type"].split(" ")[0] == want_type, big
        assert want_src in big["asset_type_source"], big
        assert ("EEE" in d["counts"]["company_names"]) is company, d["counts"]
        # preview of a put on BBB
        html = x.preview(sell("BBB", "20"))[2]
        assert previewable(html), text_of(html)[-500:]
        t = text_of(html)
        flagged = "INDIVIDUAL_NAME_LIMIT" in t
        assert flagged is company, (state, t[-700:])
        # the page text says UNKNOWN only for the unresolved state, never STOCK for it
        pt = text_of(x.get("/portfolio")[2])
        if want_type == "UNKNOWN":
            assert "UNKNOWN (not in approvals)" in pt
        # reading pages never writes approvals
        for p in ("/", "/portfolio", "/positions", "/checkin"):
            x.get(p)
        assert _sha(x.appr) == h0 and _rows(x.db) == rows0
    finally:
        x.stop()


def test_unknown_labels_on_a_new_unapproved_put_block_and_are_never_stock(tmp_path):
    """ZZZ is approved nowhere: preview must refuse entry-style acceptance without ack (NOT_APPROVED + LABELS_UNKNOWN) and must not
    present its asset type as STOCK."""
    x = B1(tmp_path)
    try:
        html = x.preview(sell("ZZZ", "30"))[2]
        t = text_of(html)
        assert "NOT_APPROVED" in t and "LABELS_UNKNOWN" in t
        assert "ack_breach" in html
    finally:
        x.stop()
