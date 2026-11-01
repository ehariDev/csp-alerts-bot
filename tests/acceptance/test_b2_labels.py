"""Stage B2 section E: audited label correction (WT-05 'owner-confirmed labels ... through an audited correction; unknown labels remain
unknown and block any candidate whose concentration cannot be assessed'; WT-AT-03/09 style web-write security).

Book L: U1 UUU 20 unknown/unknown OPEN (2,000) ; A1 AAA 40 'wrongsec'/'wrongbkt' OPEN (4,000; AAA is approved as Tech/Semis) ;
        T1 TTT 50 tech/semis (5,000) ; T2 SSS 50 tech/cloud (5,000).  Sector tech = 10,000 (T1+T2) exactly at its limit.
Hand arithmetic: relabel U1 -> sector 'Tech' : tech = 10,000 + 2,000 = 12,000 > 10,000 => OVER LIMIT (room -2,000).
                 relabel U1 -> sector 'Banks': banks = 2,000, tech stays 10,000 (equality, within).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from b1_env import B1, DASH_NOW, FORM, text_of
from b2_env import REF_APPROVED, ref_env
from test_b1_security import attack_cases
from web_helpers import fingerprint

PREV, CONF = "/portfolio/labels/preview", "/portfolio/labels/confirm"
EXTRAS = (
    dict(tid="U1", ticker="UUU", strike="20", sector="unknown", bucket="unknown", state="OPEN"),
    dict(tid="A1", ticker="AAA", strike="40", sector="wrongsec", bucket="wrongbkt", state="OPEN"),
    dict(tid="T1", ticker="TTT", strike="50", sector="tech", bucket="semis", state="OPEN"),
    dict(tid="T2", ticker="SSS", strike="50", sector="tech", bucket="cloud", state="OPEN"),
)
APPROVED = {"AAA": ("STOCK", "Tech", "Semis", "100.00")}


@pytest.fixture()
def L(tmp_path):
    w = ref_env(tmp_path, approved=APPROVED, extras=EXTRAS, lot=False)
    yield w
    w.stop()


def approvals_hash(w) -> str:
    c = sqlite3.connect(w.db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%approv%' OR name LIKE '%attest%')")]
        blob = json.dumps({n: sorted(map(str, c.execute(f"SELECT * FROM {n}").fetchall())) for n in names})
        return hashlib.sha256(blob.encode()).hexdigest()
    finally:
        c.close()


def put_labels(w, tid):
    return w.sql("SELECT sector, correlation_bucket FROM option_positions WHERE trade_id=?", (tid,))[0]


def prev(w, ticker, sector, bucket, **kw):
    return w.post(PREV, {"ticker": ticker, "sector": sector, "bucket": bucket}, csrf=w.csrf(f"/portfolio/labels/{ticker}"), **kw)


def previewable(html):
    return "tok" in B1.hidden(html)


def confirm_fields(w, html, reason="owner confirmed the sector", ack=True):
    h = B1.hidden(html)
    h.pop("csrf", None)
    f = {**h, "reason": reason}
    if ack:
        f["ack_labels"] = "1"
    return f


def code(hd):
    return parse_qs(urlsplit(hd.get("location", "")).query).get("code", [""])[0]


def correct(w, ticker, sector, bucket, **kw):
    st, _, html = prev(w, ticker, sector, bucket)
    assert st == 200 and previewable(html), text_of(html)[-600:]
    st, hd, _ = w.post(CONF, confirm_fields(w, html, **kw), csrf=w.csrf(f"/portfolio/labels/{ticker}"))
    return st, code(hd), html


def audit(w, typ="LABEL_CORRECTED"):
    return w.sql("SELECT actor, source, before_json, after_json FROM audit_log WHERE event_type=?", (typ,))


# ------------------------------------------------------------------------------------------ preview / flow
def test_preview_writes_nothing_and_shows_before_and_after_groups(L):
    before = fingerprint(L.db)
    st, _, html = prev(L, "UUU", "Banks", "Money Center")
    assert st == 200 and previewable(html)
    assert fingerprint(L.db) == before
    t = text_of(html)
    assert "Nothing has been written" in t and "banks" in t and "money center" in t
    assert re.search(r"sector unknown \$2,000\.00", t) and re.search(r"sector banks \$2,000\.00", t)


def test_confirm_rewrites_only_that_tickers_labels_audits_and_leaves_approvals_untouched(L):
    ah = approvals_hash(L)
    others = {t: put_labels(L, t) for t in ("A1", "T1", "T2")}
    n_pos = L.sql("SELECT COUNT(*) FROM option_positions")[0][0]
    rev = L.sql("SELECT COUNT(*) FROM audit_log WHERE event_type NOT IN ('LABEL_CORRECTED','WEB_LABEL_CORRECTED')")[0][0]
    st, c, _ = correct(L, "UUU", "Banks", "Money Center")
    assert (st, c) == (303, "label_recorded")
    assert put_labels(L, "U1") == ("banks", "money center")                       # casefolded like the existing comparison
    assert {t: put_labels(L, t) for t in others} == others                         # other tickers untouched
    assert L.sql("SELECT COUNT(*) FROM option_positions")[0][0] == n_pos
    assert approvals_hash(L) == ah                                                 # approvals / attestations never touched
    a = audit(L)
    assert len(a) == 1 and a[0][0] == "web:owner" and a[0][1] == "web"
    before, after = json.loads(a[0][2]), json.loads(a[0][3])
    assert "unknown" in json.dumps(before) and "banks" in json.dumps(after)
    assert L.sql("SELECT COUNT(*) FROM audit_log WHERE event_type NOT IN ('LABEL_CORRECTED','WEB_LABEL_CORRECTED')")[0][0] == rev
    assert len(audit(L, "WEB_LABEL_CORRECTED")) == 1         # the idempotency record; no other event type (no ledger event)
    r = L.get("/portfolio/label-result?code=label_recorded&ticker=UUU")
    assert r[0] == 200 and "audited" in text_of(r[2])


def test_label_correction_is_not_a_ledger_event_and_demands_no_new_checkin(L):
    from b1_env import checkin
    checkin(L)                                       # reconcile so that a spurious ledger-change flag would be visible
    codes = lambda: set(re.findall(r"(?:BLOCK|WARN) (\w+)", text_of(L.get("/portfolio")[2])))
    assert "CHANGE_AFTER_SNAPSHOT" not in codes()
    assert correct(L, "UUU", "Banks", "Money Center")[1] == "label_recorded"
    assert "CHANGE_AFTER_SNAPSHOT" not in codes()


def test_unknown_to_known_resolves_the_unknown_flag_and_a_known_label_that_breaches_a_cap_is_reported_over_limit(L):
    d0 = json.loads(L.get("/api/portfolio")[2])["data"]
    assert d0["unknown_tickers"] == ["UUU"]
    # relabel to Tech: tech = 5,000 + 5,000 + 2,000 = 12,000 > 10,000
    st, _, html = prev(L, "UUU", "Tech", "Banks")
    t = text_of(html)
    assert re.search(r"sector tech \$12,000\.00 \$10,000\.00 -\$2,000\.00 OVER LIMIT", t), t[t.index("after this correction"):][:400]
    assert correct(L, "UUU", "Tech", "Banks")[1] == "label_recorded"
    d = json.loads(L.get("/api/portfolio")[2])["data"]
    assert d["unknown_tickers"] == []
    tech = next(g for g in d["groups"]["sector"] if g["key"] == "tech")
    assert tech["breached"] is True and tech["room_usd"] in ("-2000", "-2000.00")


# ------------------------------------------------------------------------------------------ approved tickers
def test_approved_ticker_accepts_only_the_approvals_own_labels(L):
    ah = approvals_hash(L)
    for sec, bkt in (("Other", "Semis"), ("Tech", "Other"), ("wrongsec", "wrongbkt")):
        st, _, html = prev(L, "AAA", sec, bkt)
        assert not previewable(html), (sec, bkt)
        assert "CLI" in text_of(html)
    assert put_labels(L, "A1") == ("wrongsec", "wrongbkt")
    # the approval's own labels (any casing / padding) align the ledger with the approval
    assert correct(L, "AAA", " TECH ", "semis")[1] == "label_recorded"
    assert put_labels(L, "A1") == ("tech", "semis")
    assert approvals_hash(L) == ah


def test_asset_type_is_not_correctable_and_the_page_says_so(L):
    t = text_of(L.get("/portfolio/labels/UUU")[2])
    assert "set by approval (CLI)" in t and "not editable here" in t
    for extra in ({"asset_type": "ETF"}, {"asset": "ETF"}, {"type": "ETF"}):
        st, _, html = L.post(PREV, {"ticker": "UUU", "sector": "Banks", "bucket": "Money", **extra}, csrf=L.csrf("/portfolio/labels/UUU"))
        h = B1.hidden(html)
        assert "asset_type" not in h and "ETF" not in json.dumps(h)
    assert L.sql("SELECT COUNT(*) FROM approved_underlyings WHERE ticker='UUU'")[0][0] == 0


def test_unknown_ticker_or_one_without_live_rows_has_no_form(L):
    for sym in ("QQQ", "ZZZ"):
        st, _, h = L.get(f"/portfolio/labels/{sym}")
        assert st in (200, 404)
        if st == 200:
            assert "No live put position or stock lot" in text_of(h) and 'name="sector"' not in h
    st, _, html = L.post(PREV, {"ticker": "QQQ", "sector": "Banks", "bucket": "Money"})
    assert not previewable(html)


# ------------------------------------------------------------------------------------------ hostile / invalid labels
BAD = ["", " ", "unknown", "UNKNOWN", " Unknown ", "<script>alert(1)</script>", "a" * 41, "ban\nk", "ban\x00k", "ban\tk", "ＢＡＮＫ", "ban​k",
       "ban‮k", "banks;DROP", "ba'nk", 'ba"nk', "bänk", "ban﻿k", "ban k", "a\\b", "%00", "{{7*7}}", "ban/k", "ban\r\nk", "١٢"]


@pytest.mark.parametrize("bad", BAD, ids=[repr(b)[:18] for b in BAD])
def test_hostile_sector_and_bucket_labels_are_refused_and_not_reflected_raw(L, bad):
    before = fingerprint(L.db)
    for sec, bkt in ((bad, "Money"), ("Banks", bad)):
        st, _, html = prev(L, "UUU", sec, bkt)
        assert st == 200 and not previewable(html), (sec, bkt)
        assert "<script>" not in html and "alert(1)" not in html.replace("alert(1)</script>", "") or "&lt;script&gt;" in html
        assert "\x00" not in html and "‮" not in html and "​" not in html
    assert fingerprint(L.db) == before


GOOD = [("a" * 40, "ok"), ("Banks & Co.", "Money-Center_1"), ("  Banks  ", "x"), ("BANKS", "X")]


@pytest.mark.parametrize("sec,bkt", GOOD)
def test_valid_label_boundaries_and_casefold(L, sec, bkt):
    assert correct(L, "UUU", sec, bkt)[1] == "label_recorded"
    assert put_labels(L, "U1") == (sec.strip().casefold(), bkt.strip().casefold())


def test_casefolded_label_pools_with_an_existing_group(L):
    """T1/T2 hold 'tech'. Correcting UUU to 'TECH ' must pool into the same group (10,000 + 2,000 = 12,000)."""
    correct(L, "UUU", "TECH ", "Banks")
    tech = next(g for g in json.loads(L.get("/api/portfolio")[2])["data"]["groups"]["sector"] if g["key"] == "tech")
    assert float(tech["exposure_usd"]) == 12000.0 and tech["breached"] is True


# ------------------------------------------------------------------------------------------ write security (like B1)
def test_confirm_requires_reason_ack_and_every_layer_of_request_checks(L):
    html = prev(L, "UUU", "Banks", "Money")[2]
    good = L.csrf("/portfolio/labels/UUU")
    before = fingerprint(L.db)
    # reason / ack
    for kw, expect in (({"reason": ""}, "reason_required"), ({"reason": "   "}, "reason_required"), ({"ack": False}, "ack_required")):
        cf = confirm_fields(L, html, **({"reason": kw["reason"]} if "reason" in kw else {"ack": False}))
        if "reason" not in kw:
            cf.pop("reason", None)
            cf["reason"] = "x"
        st, hd, _ = L.post(CONF, cf, csrf=good)
        assert code(hd) == expect, (kw, hd)
    cf = confirm_fields(L, html)
    cf.pop("reason")
    assert code(L.post(CONF, cf, csrf=good)[1]) == "reason_required"
    for name, kw in attack_cases(good).items():
        kw = dict(kw)
        csrf = kw.pop("csrf")
        st, hd, body = L.post(CONF, confirm_fields(L, html), csrf=csrf, **kw)
        assert st in (303, 400, 401, 403, 405, 421), (name, st)
        assert code(hd) != "label_recorded", name
    for name, kw in attack_cases(good).items():
        kw = dict(kw)
        csrf = kw.pop("csrf")
        st, hd, body = L.post(PREV, {"ticker": "UUU", "sector": "Banks", "bucket": "Money"}, csrf=csrf, **kw)
        assert st in (303, 400, 401, 403, 405, 421), (name, st)
        assert not previewable(body), name
    assert fingerprint(L.db) == before


@pytest.mark.parametrize("tamper", [("sector", "Tech"), ("bucket", "evil"), ("ticker", "AAA"), ("rev", "1"), ("psha", "0" * 64),
                                    ("from_labels", "0" * 32), ("idem", "other"), ("tok", "1.deadbeef")])
def test_any_tampered_bound_field_is_rejected_and_writes_nothing(L, tamper):
    html = prev(L, "UUU", "Banks", "Money")[2]
    cf = confirm_fields(L, html)
    cf[tamper[0]] = tamper[1]
    before = fingerprint(L.db)
    st, hd, _ = L.post(CONF, cf, csrf=L.csrf("/portfolio/labels/UUU"))
    assert code(hd) not in ("label_recorded", "duplicate"), (tamper, hd)
    assert fingerprint(L.db) == before


def test_double_submit_records_exactly_once_and_returns_the_first_result(L):
    html = prev(L, "UUU", "Banks", "Money")[2]
    cf = confirm_fields(L, html)
    c1 = code(L.post(CONF, cf, csrf=L.csrf("/portfolio/labels/UUU"))[1])
    c2 = code(L.post(CONF, cf, csrf=L.csrf("/portfolio/labels/UUU"))[1])
    assert c1 == "label_recorded" and c2 in ("duplicate", "label_recorded")
    assert len(audit(L)) == 1


def test_stale_when_the_ledger_moves_or_the_labels_change_between_preview_and_confirm(L):
    html = prev(L, "UUU", "Banks", "Money")[2]
    cf = confirm_fields(L, html)
    # another write: a check-in changes the ledger revision
    from b1_env import checkin
    checkin(L)
    before = fingerprint(L.db)
    c = code(L.post(CONF, cf, csrf=L.csrf("/portfolio/labels/UUU"))[1])
    assert c == "stale", c
    assert fingerprint(L.db) == before and audit(L) == []
    # labels replaced by a different correction after a fresh preview
    h1 = prev(L, "UUU", "Banks", "Money")[2]
    h2 = prev(L, "UUU", "Utilities", "Grid")[2]
    assert code(L.post(CONF, confirm_fields(L, h2), csrf=L.csrf("/portfolio/labels/UUU"))[1]) == "label_recorded"
    assert code(L.post(CONF, confirm_fields(L, h1), csrf=L.csrf("/portfolio/labels/UUU"))[1]) == "stale"
    assert put_labels(L, "U1") == ("utilities", "grid") and len(audit(L)) == 1


def test_token_expires_after_20_minutes_and_is_not_valid_on_another_session(L, tmp_path):
    import time
    sessions = L.d.srv.sessions
    base = float(int(time.monotonic()) + 10)
    sessions._clock = lambda: base
    try:
        html = prev(L, "UUU", "Banks", "Money")[2]
        cf = confirm_fields(L, html)
        sessions._clock = lambda: base + 1201
        assert code(L.post(CONF, cf, csrf=L.csrf("/portfolio/labels/UUU"))[1]) == "expired"
        assert audit(L) == []
        sessions._clock = lambda: base + 1200
        assert code(L.post(CONF, cf, csrf=L.csrf("/portfolio/labels/UUU"))[1]) == "label_recorded"
    finally:
        sessions._clock = time.monotonic


def test_label_pages_escape_hostile_text_and_never_leak_paths_or_secrets(L):
    for p in ("/portfolio/labels/%3Cscript%3E", "/portfolio/labels/" + "A" * 300, "/portfolio/label-result?code=%3Cscript%3E&ticker=%3Cb%3E",
              "/portfolio/label-result?code=label_recorded&ticker=UUU%00"):
        st, _, h = L.get(p)
        assert st in (200, 400, 404)
        assert "<script" not in h.lower() and "Traceback" not in h and "sqlite" not in h.lower()


def test_label_correction_does_not_clear_a_breach_flag_while_another_flagged_code_still_reproduces(tmp_path):
    """A web-recorded put on an UNAPPROVED ticker with blank labels is flagged {LABELS_UNKNOWN, NOT_APPROVED}. Correcting the labels
    removes only the first; NOT_APPROVED still reproduces (the owner never approved it), so the flag MUST stay (D15 item 148: cleared
    only when NO flagged code reproduces) and no POLICY_BREACH_CLEARED is written."""
    from b1_env import sell
    w = ref_env(tmp_path, approved=APPROVED, extras=(), lot=False)
    try:
        st, loc, html = w.do(sell("ZZZ", "30", sector="", bucket=""))
        assert w.result_code(loc) == "recorded", text_of(html)[-700:]
        assert "POLICY_BREACH_UNRESOLVED" in text_of(w.get("/")[2])
        assert correct(w, "ZZZ", "Banks", "Money")[1] == "label_recorded"
        assert "POLICY_BREACH_UNRESOLVED" in text_of(w.get("/")[2])
        assert w.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_CLEARED'") == []
    finally:
        w.stop()


def test_market_page_fails_every_contract_of_a_ticker_with_unknown_labels_and_label_fix_removes_only_that_code(tmp_path):
    """UUU is held with unknown labels. Every UUU contract row must list LABELS_UNKNOWN (concentration cannot be assessed) and never
    show 'passes all gates'. After the owner's audited correction LABELS_UNKNOWN disappears (NOT_APPROVED remains: UUU is unapproved)."""
    from b2_env import chain_env, occ, row_cells
    ex = [dict(tid="U1", ticker="UUU", strike="20", sector="unknown", bucket="unknown", state="OPEN")]
    w = chain_env(tmp_path, approved={"AAA": ("STOCK", "Tech", "Semis", "100.00")}, extras=ex, spots={"UUU": 100.0}, strikes=(30.0, 40.0))
    try:
        for k in (30, 40):
            cap = text_of(row_cells(w.get("/market/UUU")[2], occ("UUU", k))[14])
            assert "LABELS_UNKNOWN" in cap and "passes all gates" not in cap, cap
        assert correct(w, "UUU", "Banks", "Money")[1] == "label_recorded"
        for k in (30, 40):
            cap = text_of(row_cells(w.get("/market/UUU")[2], occ("UUU", k))[14])
            assert "LABELS_UNKNOWN" not in cap.split("capacity by rule")[0] and "NOT_APPROVED" in cap, cap
    finally:
        w.stop()


def test_unreadable_approvals_file_fails_closed_for_label_correction_and_for_the_eligible_marker(L):
    L.appr.write_text("{not json", encoding="utf-8")
    h = L.get("/portfolio/labels/UUU")[2]
    assert 'name="sector"' not in h and "cannot be read" in text_of(h)
    before = fingerprint(L.db)
    st, _, html = L.post(PREV, {"ticker": "UUU", "sector": "Banks", "bucket": "Money"})
    assert not previewable(html) and fingerprint(L.db) == before
    assert "passes all gates" not in text_of(L.get("/market/AAA")[2])
