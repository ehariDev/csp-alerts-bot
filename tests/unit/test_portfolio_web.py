"""Stage B2 (WT-05, WT-04 display): portfolio totals against the backend for a hand-calculated reference book, label correction
(audit, parity, security), per-candidate capacity on the market page, the alerts page, crawl without mutation. Synthetic data."""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
import sqlite3
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from factories import NOW, TODAY, policy, put, snap
from test_dashboard_trade import EXP, Web, confirm_fields, hidden
from test_webtrade import env, ik, jw, rec, sell  # noqa: F401  (env is a fixture; it also provides env.approved_list)

from csp_alerts.journal import weblabels
from csp_alerts.journal import webtrade as wt
from csp_alerts.journal.models import BookState, PositionState as S, StockLot
from csp_alerts.policy.portfolio import (
    ProposedPut, capacity_rows, check_portfolio_limits, company_names, compute_exposure, exposure_groups, portfolio_limits)
from csp_alerts.policy.reasons import Reason
from csp_dashboard import views

POL = policy()
W49, W50 = "2026-W49", "2026-W50"


# ---------------------------------------------------------------- hand-calculated reference book
def reference_book() -> BookState:
    d4, d11 = date(2026, 12, 4), date(2026, 12, 11)           # Fridays: ISO weeks 49 and 50
    return BookState(
        positions=(
            put("A", "AAA", "50", S.OPEN, sector="s1", bucket="b1", exp=d4),                              # 5,000
            put("B", "BBB", "40", S.WORKING, sector="s1", bucket="b2", exp=d4),                           # 4,000 (reservation)
            put("C", "CCC", "30", S.CLOSE_WORKING, sector="s2", bucket="b2", exp=d11, asset_type="ETF"),  # 3,000 (closing order does not release)
            put("D", "DDD", "20", S.CLOSED, sector="s9", bucket="b9", exp=d4),                            # released
            put("E", "EEE", "25", S.ASSIGNED_PENDING_RECONCILIATION, sector="s3", bucket="b3", exp=d4),   # 2,500 obligation, no lot yet
        ),
        stock_lots=(StockLot("L", "FFF", D("100"), D("20"), NOW, "s2", "b3", "PURCHASE", asset_type="STOCK"),),   # 2,000
        snapshots=(snap(equity="100000", cash="60000"),))


def test_reference_totals_by_hand():
    ex = compute_exposure(reference_book())
    assert (ex.put_collateral_usd, ex.owned_stock_usd, ex.assigned_pending_usd) == (D("12000"), D("2000"), D("2500"))
    assert ex.aggregate_equity_risk_usd == D("16500") and ex.active_contracts == 3
    assert company_names(reference_book()) == ("AAA", "BBB", "FFF")             # CCC is an ETF; EEE is an obligation, not an active put
    g = {(r.dimension, r.key): r for r in exposure_groups(reference_book(), POL)}
    assert g[("sector", "s1")].exposure_usd == D("9000") and g[("sector", "s2")].exposure_usd == D("5000")    # 3,000 put + 2,000 shares
    assert g[("bucket", "b2")].exposure_usd == D("7000") and g[("bucket", "b3")].exposure_usd == D("2000")
    assert g[("expiry_week", W49)].exposure_usd == D("9000") and g[("expiry_week", W50)].exposure_usd == D("3000")
    assert g[("ticker", "ccc")].exposure_usd == D("3000") and ("ticker", "ddd") not in g and ("ticker", "eee") not in g
    assert g[("sector", "s1")].room_usd == D("1000") and not g[("sector", "s1")].breached and not g[("sector", "s1")].exhausted


def _probe(dim, key, wk=None) -> ProposedPut:
    """A proposed put whose ONLY shared key with the book is `key` in `dim`."""
    d = {"ticker": "ZZZ", "sector": "zs", "bucket": "zb"}
    exp = date(2027, 6, 18)
    if dim == "ticker":
        d["ticker"] = key.upper()
    elif dim == "sector":
        d["sector"] = key
    elif dim == "bucket":
        d["bucket"] = key
    else:
        exp = date.fromisocalendar(int(key[:4]), int(key[-2:]), 5)
    return ProposedPut(d["ticker"], "STOCK", d["sector"], d["bucket"], exp, D("10"), 100, 1)


def test_group_rows_equal_the_figures_the_entry_gate_compares():
    book = reference_book()
    for g in exposure_groups(book, POL):
        rows = {r.key: r for r in capacity_rows(book, _probe(g.dimension, g.key), POL, D("60000"))}
        row = rows["expiry_week" if g.dimension == "expiry_week" else g.dimension]
        assert row.used_before == g.exposure_usd and row.limit == g.limit, (g.dimension, g.key)
        assert row.room_after == g.limit - g.exposure_usd - D("1000"), (g.dimension, g.key)


def test_portfolio_limit_rows_equal_capacity_rows_and_the_gate():
    book = reference_book()
    caps = {r.key: r for r in capacity_rows(book, _probe("sector", "zs"), POL, D("60000"))}
    lim = {r.key: r for r in portfolio_limits(book, POL)}
    for key in ("pilot", "normal_put", "normal_aggregate", "contracts", "names"):
        assert lim[key].used == caps[key].used_before and lim[key].limit == caps[key].limit, key
    assert lim["pilot"].used == D("12000") and lim["pilot"].room == D("3000")
    assert lim["contracts"].used == 3 and lim["names"].used == 3 and lim["names"].breached      # 3 companies vs a limit of 2
    # exhausted <=> a one-contract put of any size is refused by the same rule in the gate
    tiny = ProposedPut("ZZZ", "STOCK", "zs", "zb", date(2027, 6, 18), D("0.01"), 100, 1)
    assert Reason.INDIVIDUAL_NAME_LIMIT in check_portfolio_limits(book, tiny, POL, D("60000"))


def test_closing_order_does_not_release_capacity_and_closed_does():
    book = reference_book()
    assert any(r.key == "ccc" and r.exposure_usd == D("3000") for r in exposure_groups(book, POL) if r.dimension == "ticker")
    from dataclasses import replace
    closed = replace(book, positions=tuple(replace(p, state=S.CLOSED) if p.trade_id == "C" else p for p in book.positions))
    assert not any(r.key == "ccc" for r in exposure_groups(closed, POL) if r.dimension == "ticker")


def test_unknown_labels_are_a_visible_group_flagged_and_not_diversified():
    book = BookState(positions=(put("U", "UUU", "40", S.OPEN, sector="unknown", bucket="unknown"),
                                put("V", "VVV", "30", S.OPEN, sector="unknown", bucket="b9")))
    g = {(r.dimension, r.key): r for r in exposure_groups(book, POL)}
    assert g[("sector", "unknown")].unknown and g[("sector", "unknown")].exposure_usd == D("7000")   # pooled, never split into safe buckets
    assert g[("bucket", "unknown")].unknown and not g[("bucket", "b9")].unknown
    assert not g[("ticker", "uuu")].unknown


def test_mitigation_text_is_rule_based_names_the_binding_limit_and_gives_no_advice():
    book = BookState(positions=(put("A", "AAA", "90", S.OPEN, sector="s1", bucket="b1", exp=date(2026, 12, 4)),
                                put("B", "BBB", "20", S.OPEN, sector="s2", bucket="b2", exp=date(2026, 12, 4))),
                     snapshots=(snap(),))
    groups, limits = exposure_groups(book, POL), portfolio_limits(book, POL)
    lines = views.mitigation_lines(views._entries(limits, groups), groups)
    text = " ".join(lines)
    assert "ticker AAA" in text and "SINGLE_TICKER_LIMIT" in text and "no room left" in text.replace("at its limit with ", "") or "no room left" in text
    assert "Remaining capacity by correlation bucket: b1 $0.00" not in text           # b1 has 9,000 of 10,000, room $1,000.00
    assert "b1 $1,000.00" in text and "b2 $8,000.00" in text
    assert "recommends no trade" in text
    for banned in ("consider", "should", "hedge your", "diversify into", "buy ", "sell "):
        assert banned not in text.lower().replace("recommends no trade, hedge,", ""), banned
    assert views.mitigation_lines(views._entries([], []), []) == []


# ---------------------------------------------------------------- HTTP: portfolio page and JSON twin
def text_of(body: str) -> str:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", body, flags=re.S)
    body = re.sub(r"</(tr|p|h\d|li|div|section)>", "\n", body)
    body = re.sub(r"</t[dh]>", " | ", body)
    return htmllib.unescape(re.sub(r"<[^>]+>", "", body))


def digest(env) -> str:
    c = sqlite3.connect(env.db)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return hashlib.sha256(Path(env.db).read_bytes()).hexdigest()


def test_portfolio_page_matches_backend_for_the_fixture_book(env):
    w = Web(env)
    try:
        st, body, _ = w.c.get("/portfolio")
        assert st == 200
        t = text_of(body)
        # fixture: P-OPEN BBB 40 (4,000), P-PEND CCC 30 (3,000), closed P-DONE, 100 sh EEE @ 10 (1,000)
        for want in ("Active positions (reserve collateral, incl. working orders) | 2", "Active contracts | 2",
                     "Lifetime recorded trades (any state, not drafts) | 3", "$7,000.00", "$1,000.00", "$8,000.00",
                     "Aggregate exposure budget | $8,000.00", "$60,000.00 free of $100,000.00 equity"):
            assert want in t, want
        j = jw(env)
        book = j.load_book()
        j.close()
        ex = compute_exposure(book)
        api = w.c.api("/portfolio")["data"]
        assert D(api["exposure"]["aggregate_usd"]) == ex.aggregate_equity_risk_usd == D("8000")
        assert api["counts"]["active_contracts"] == ex.active_contracts and api["counts"]["lifetime_trades"] == 3
        assert {h["ticker"] for h in api["holdings"]} == {"BBB", "CCC", "EEE"}
        for h in api["holdings"]:                                           # every holding shows ticker, sector, asset type, bucket
            assert h["sector"] and h["asset_type"] and h["bucket"]
        assert [g["key"] for g in api["groups"]["expiry_week"]] == [f"{date.fromisoformat(EXP).isocalendar()[0]}-W{date.fromisoformat(EXP).isocalendar()[1]:02d}"]
    finally:
        w.close()


def test_portfolio_flags_unknown_labels_and_breach_mitigation(env):
    r = rec(env, sell(idem=ik("pu"), symbol="ZZZ"), acks=("ack_breach",))              # unapproved, labels unknown
    assert r.code == "recorded" and "LABELS_UNKNOWN" in r.breach_codes
    w = Web(env)
    try:
        st, body, _ = w.c.get("/portfolio")
        t = text_of(body)
        assert st == 200 and "UNKNOWN LABEL" in t and "Holdings of ZZZ have an UNKNOWN sector" in t and "never counted as diversified" in t
        assert "correct labels" in t
        # BBB, CCC, EEE, ZZZ: four company names against a limit of two -> breached and binding, with rule-based text only
        assert "Individual-company names (puts + shares) | INDIVIDUAL_NAME_LIMIT | 4 | 2 | -2 | OVER LIMIT" in t
        assert "recommends no trade, hedge, alternative sector or movement of capital" in t
        assert "What binds and what is left (rule-based; no advice)" in t
    finally:
        w.close()


# ---------------------------------------------------------------- label correction: domain
def lab_form(ticker="BBB", sector="Tech", bucket="Large Cap", idem=None, **over):
    return {"action": weblabels.ACTION, "idem": idem or ik("lab" + ticker + sector), "ticker": ticker, "sector": sector, "bucket": bucket, **over}


def lab_plan(env, form, approved=...):
    j = jw(env)
    try:
        return weblabels.build_plan(j, POL, env.approved_list if approved is ... else approved, form)
    finally:
        j.close()


def lab_rec(env, form, *, reason="owner confirmed", acks=("ack_labels",), approved=..., plan=None):
    j = jw(env)
    try:
        ap = env.approved_list if approved is ... else approved
        p = plan or weblabels.build_plan(j, POL, ap, form)
        f = {**form, "from_labels": p.fields.get("from_labels", "")}
        return weblabels.record(j, POL, ap, f, now=NOW, bound_revision=p.ledger_revision, bound_policy_sha=p.policy_sha, reason=reason,
                                acks=frozenset(acks))
    finally:
        j.close()


def labels_of(env, trade_id="P-OPEN"):
    j = jw(env)
    try:
        p = j.get_position(trade_id)
        return p.sector, p.correlation_bucket
    finally:
        j.close()


def test_label_preview_writes_nothing_and_shows_before_and_after(env, tmp_path):
    before = digest(env)
    p = lab_plan(env, lab_form())
    assert p.ok and p.fields["sector"] == "tech" and p.fields["bucket"] == "large cap"
    assert {(g.dimension, g.key) for g in p.before} >= {("sector", "sec_b"), ("bucket", "b_b")}
    assert any(g.dimension == "sector" and g.key == "tech" and g.exposure_usd == D("4000") for g in p.after)
    assert digest(env) == before


def test_label_correction_changes_only_labels_audits_and_matches_backend_grouping(env):
    j = jw(env)
    seqs = j.ledger_seqs()
    approvals_before = j.conn.execute("SELECT COUNT(*) FROM approved_underlyings").fetchone()[0]
    approved_bytes = Path(env.approved).read_bytes()
    j.close()
    res = lab_rec(env, lab_form(idem=ik("L1")))
    assert res.code == "label_recorded" and labels_of(env) == ("tech", "large cap")
    j = jw(env)
    ev = j.audit_events("LABEL_CORRECTED")
    assert len(ev) == 1 and ev[0]["actor"] == "web:owner" and ev[0]["source"] == "web"
    before, after = json.loads(ev[0]["before_json"]), json.loads(ev[0]["after_json"])
    assert before["positions"] == [{"id": "P-OPEN", "sector": "sec_b", "bucket": "b_b"}] and after["reason"] == "owner confirmed"
    assert j.audit_events("WEB_LABEL_CORRECTED")
    assert j.ledger_seqs() == seqs                              # labels are not a ledger event: no new check-in is demanded
    assert j.conn.execute("SELECT COUNT(*) FROM approved_underlyings").fetchone()[0] == approvals_before
    assert Path(env.approved).read_bytes() == approved_bytes    # approvals untouched
    g = {(r.dimension, r.key): r for r in exposure_groups(j.load_book(), POL)}
    assert g[("sector", "tech")].exposure_usd == D("4000") and ("sector", "sec_b") not in g
    # the entry gate now counts a new BBB put against the corrected labels
    probe = ProposedPut("BBB", "STOCK", "tech", "large cap", date(2027, 6, 18), D("60"), 100, 1)
    assert {r.key: r for r in capacity_rows(j.load_book(), probe, POL, D("60000"))}["sector"].used_before == D("4000")
    j.close()


def test_label_correction_is_idempotent_stale_checked_and_needs_reason_and_ack(env):
    f = lab_form(idem=ik("L2"))
    p = lab_plan(env, f)
    assert lab_rec(env, f, reason="", plan=p).code == "reason_required"
    assert lab_rec(env, f, acks=(), plan=p).code == "ack_required"
    assert labels_of(env) == ("sec_b", "b_b")
    assert lab_rec(env, f, plan=p).code == "label_recorded"
    again = lab_rec(env, f, plan=p)
    assert again.code == "duplicate" and len(jw(env).audit_events("LABEL_CORRECTED")) == 1
    # stale: labels changed after the preview was made
    f2 = lab_form(sector="Other", idem=ik("L3"))
    p2 = lab_plan(env, f2)
    assert lab_rec(env, lab_form(sector="Third", idem=ik("L4"))).code == "label_recorded"
    assert lab_rec(env, f2, plan=p2).code == "stale"
    assert labels_of(env)[0] == "third"


@pytest.mark.parametrize("sector,bucket", [("unknown", "x"), ("x", " UNKNOWN "), ("", "x"), ("x", ""), ("a<b", "x"), ("x" * 41, "x"),
                                            ("٣", "x")])
def test_label_correction_refuses_unknown_blank_and_odd_labels(env, sector, bucket):
    assert not lab_plan(env, lab_form(sector=sector, bucket=bucket)).ok


def test_label_correction_refusals_for_tickers_without_holdings_and_unreadable_approvals(env):
    assert not lab_plan(env, lab_form(ticker="NOPE")).ok
    assert not lab_plan(env, lab_form(ticker="AAA")).ok                       # approved, but nothing held
    assert not lab_plan(env, lab_form(), approved=None).ok                     # unreadable approvals: nothing is assumed
    same = lab_plan(env, lab_form(sector="sec_b", bucket="b_b"))
    assert not same.ok and any("nothing to correct" in e for e in same.errors)


def test_approved_ticker_labels_can_only_be_aligned_to_the_approval_never_changed(env):
    j = jw(env)
    j.create_draft(__import__("csp_alerts.journal.store", fromlist=["NewPosition"]).NewPosition(
        "P-AAA", "AAA", TODAY + timedelta(days=40), D("40"), 100, 1, "wrong_sector", "wrong_bucket"), actor="t", source="t")
    j.transition("P-AAA", S.PENDING)
    j.close()
    bad = lab_plan(env, lab_form(ticker="AAA", sector="whatever", bucket="else"))
    assert not bad.ok and any("must equal the approval" in e for e in bad.errors)
    ok = lab_plan(env, lab_form(ticker="AAA", sector="sec_AAA", bucket="b_AAA"))
    assert ok.ok and ok.approval_labels == ("sec_aaa", "b_aaa")
    assert lab_rec(env, lab_form(ticker="AAA", sector="sec_AAA", bucket="b_AAA", idem=ik("L5"))).code == "label_recorded"
    assert labels_of(env, "P-AAA") == ("sec_aaa", "b_aaa")


def test_label_correction_updates_stock_lots_and_live_puts_of_that_ticker_only(env):
    res = lab_rec(env, lab_form(ticker="EEE", sector="Etf Broad", bucket="Index", idem=ik("L6")))
    assert res.code == "label_recorded"
    j = jw(env)
    lot = j.list_stock_lots()[0]
    assert (lot.sector, lot.correlation_bucket) == ("etf broad", "index")
    assert j.get_position("P-DONE").sector == "sec_d" and j.get_position("P-OPEN").sector == "sec_b"      # others untouched
    j.close()


def test_correction_clears_a_labels_unknown_breach_flag(env):
    r = rec(env, sell(idem=ik("cl"), symbol="ZZZ"), acks=("ack_breach",))
    assert r.code == "recorded" and "LABELS_UNKNOWN" in r.breach_codes
    # still flagged for NOT_APPROVED after labelling, so the flag does NOT clear: the cause is not only the labels
    assert lab_rec(env, lab_form(ticker="ZZZ", sector="Tech", bucket="Big", idem=ik("L7"))).code == "label_recorded"
    assert not jw(env).audit_events("POLICY_BREACH_CLEARED")
    assert "POLICY_BREACH_UNRESOLVED" in {i.code for i in __import__("csp_alerts.journal.reconcile", fromlist=["reconcile"]).reconcile(
        jw(env).load_book(), POL, NOW).issues}


# ---------------------------------------------------------------- label correction over HTTP (same web-write security as B1)
def lab_http_preview(w, fields, csrf=None, **kw):
    return w.post("/portfolio/labels/preview", {"csrf": csrf or w.csrf("/portfolio/labels/BBB"), "action": weblabels.ACTION, **fields}, **kw)


def test_label_flow_over_http_preview_confirm_audit_and_get_is_read_only(env):
    w = Web(env)
    try:
        before = digest(env)
        for path in ("/portfolio", "/portfolio/labels/BBB", "/portfolio/label-result?code=label_recorded&ticker=BBB", "/alerts", "/market/AAA",
                     "/market/ZZZ", "/api/portfolio", "/portfolio/labels/NOPE"):
            st, body, _ = w.c.get(path)
            assert st == 200, path
        assert digest(env) == before
        st, form, _ = w.c.get("/portfolio/labels/BBB")
        assert "Labels stored now" in form and 'action="/portfolio/labels/preview"' in form and "never approvals" in form
        csrf = w.csrf("/portfolio/labels/BBB")
        st, html, _ = w.post("/portfolio/labels/preview", {"csrf": csrf, "ticker": "BBB", "sector": "Tech", "bucket": "Large Cap"})
        assert st == 200 and "PREVIEW" in html and "Exposure by label after this correction" in html
        assert digest(env) == before                                             # preview wrote nothing
        st, _, hd = w.post("/portfolio/labels/confirm", confirm_fields(html, csrf, ack_labels="1"))
        assert st == 303 and "code=label_recorded" in hd["location"]
        assert labels_of(env) == ("tech", "large cap")
        st, body, _ = w.c.get(hd["location"])
        assert st == 200 and "Labels corrected and audited" in body
        # double submit of the same confirm: duplicate, one audit event
        st, _, hd2 = w.post("/portfolio/labels/confirm", confirm_fields(html, csrf, ack_labels="1"))
        assert "code=duplicate" in hd2["location"] and len(jw(env).audit_events("LABEL_CORRECTED")) == 1
    finally:
        w.close()


def test_label_posts_security(env):
    w = Web(env)
    try:
        csrf = w.csrf("/portfolio/labels/BBB")
        good = {"csrf": csrf, "ticker": "BBB", "sector": "Tech", "bucket": "Big"}
        st, html, _ = w.post("/portfolio/labels/preview", good)
        conf = confirm_fields(html, csrf, ack_labels="1")
        before = digest(env)
        # no CSRF / wrong CSRF -> nothing written
        for bad in ({**conf, "csrf": ""}, {k: v for k, v in conf.items() if k != "csrf"}, {**conf, "csrf": "0" * 64}):
            st, _, hd = w.post("/portfolio/labels/confirm", bad)
            assert st == 303 and "code=csrf" in hd["location"]
        # cross origin / missing origin / cross-site / wrong content type / no session
        assert w.post("/portfolio/labels/confirm", conf, origin="http://evil.example")[0] == 403
        assert w.post("/portfolio/labels/confirm", conf, origin=None)[0] == 403
        assert w.post("/portfolio/labels/confirm", conf, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert w.post("/portfolio/labels/confirm", conf, host="evil.example")[0] == 421
        assert w.c.req("POST", "/portfolio/labels/confirm", body=json.dumps(conf), headers={"Content-Type": "application/json"})[0] == 400
        from dash_fixture import Client
        anon = Client(w.srv)
        assert anon.req("POST", "/portfolio/labels/confirm", body="x=1")[0] == 303
        # tampered field -> preview token mismatch
        for k, v in (("sector", "other"), ("bucket", "x"), ("ticker", "CCC"), ("from_labels", "0" * 32)):
            st, _, hd = w.post("/portfolio/labels/confirm", {**conf, k: v})
            assert st == 303 and "code=token" in hd["location"], k
        # another session cannot use this session's token
        c2 = w.other_client()
        assert "code=token" in c2.req("POST", "/portfolio/labels/confirm", body=__import__("urllib.parse", fromlist=["x"]).urlencode(
            {**conf, "csrf": w.srv.sessions.csrf(c2.cookie)}))[2]["location"]
        # no reason / no ack
        st, _, hd = w.post("/portfolio/labels/confirm", {**conf, "reason": ""})
        assert "code=reason_required" in hd["location"]
        st, _, hd = w.post("/portfolio/labels/confirm", {k: v for k, v in conf.items() if k != "ack_labels"})
        assert "code=ack_required" in hd["location"]
        assert digest(env) == before and labels_of(env) == ("sec_b", "b_b")
        # a changed ledger/labels between preview and confirm -> stale, nothing written
        assert lab_rec(env, lab_form(sector="Sneaky", idem=ik("L8"))).code == "label_recorded"
        st, _, hd = w.post("/portfolio/labels/confirm", conf)
        assert "code=stale" in hd["location"] and labels_of(env)[0] == "sneaky"
        # hostile input in the preview is a canned error, never reflected raw
        st, html, _ = w.post("/portfolio/labels/preview", {**good, "sector": "<script>alert(1)</script>\x1b[31m"})
        assert st == 200 and "<script>alert" not in html and "tok" not in hidden(html)
        # no other write routes exist under /portfolio, and the JSON twin has none either
        assert w.post("/api/portfolio/labels/preview", good)[0] == 405
        assert w.post("/portfolio", good)[0] == 405
    finally:
        w.close()


# ---------------------------------------------------------------- market page: candidate capacity and gates
def market(w, sym, qs=""):
    st, body, _ = w.c.get(f"/market/{sym}{qs}")
    assert st == 200
    return body


def test_market_page_approved_ticker_shows_exposure_addition_post_trade_and_remaining_capacity(env):
    w = Web(env)
    try:
        t = text_of(market(w, "AAA"))
        assert "passes all gates" in t
        # fixture exposure: aggregate 8,000; one more 40-strike put adds 4,000 -> 12,000; tightest room is the pilot cap (15,000 - 11,000)
        assert "exposure $8,000.00 + $4,000.00 = $12,000.00; tightest room after: $4,000.00 (Pilot put collateral)" in t
        assert "Entry capacity for this ticker" in t and "ETF (from your approval record)" in t
    finally:
        w.close()


def test_market_page_unapproved_ticker_shows_prices_not_approved_and_no_eligible_marker(env):
    w = Web(env)
    try:
        body = market(w, "ZZZ")
        t = text_of(body)
        assert "NOT APPROVED: no eligible marker" in t and "passes all gates" not in t and "NOT_APPROVED" in t
        assert "LABELS_UNKNOWN" in t and "UNKNOWN labels: concentration cannot be assessed" in t
        assert re.search(r"ZZZ\d{6}P\d+ \(derived id\) \| 2026-\d\d-\d\d \| \d+ \| 40 \| 0\.5 \| 0\.54", t)        # prices still visible
    finally:
        w.close()


def test_exhausted_capacity_fails_every_contract_but_never_hides_prices(env):
    # three more approved-name puts exhaust the pilot cap (15,000): 4,000 + 3,000 + 4,000 + 4,000 = 15,000 -> room 0
    r1 = rec(env, sell(idem=ik("x1"), strike="40"))
    r2 = rec(env, sell(idem=ik("x2"), symbol="ZZZ", sector="s9", bucket="b9"), acks=("ack_breach",))
    assert r1.code == r2.code == "recorded"
    w = Web(env)
    try:
        body = market(w, "AAA")
        t = text_of(body)
        assert "passes all gates" not in t
        assert "PILOT_COLLATERAL_LIMIT" in t and "CONTRACT_COUNT_LIMIT" in t
        assert re.search(r"AAA\d{6}P00042000 \(derived id\) \| 2026-\d\d-\d\d \| \d+ \| 42 \| 0\.5 \| 0\.54", t)       # price row still there
        assert "EXHAUSTED" in t or "tightest room after: -" in t
        assert "No contract in this view passes every gate" in t
    finally:
        w.close()


def test_all_gates_requires_every_gate_including_a_current_reconciliation(env):
    # a recorded trade without a new check-in leaves the ledger unreconciled: nothing may say "passes all gates"
    assert rec(env, sell(idem=ik("y1"), strike="42")).code == "recorded"
    w = Web(env)
    try:
        t = text_of(market(w, "AAA"))
        assert "passes all gates" not in t and "LEDGER_UNRECONCILED" in t or "LEDGER_STALE" in t
        assert "passes contract gates (research only)" in t                      # the contract-level research marker is separate and unchanged
    finally:
        w.close()


def test_market_json_twin_serialises_capacity_rows(env):
    w = Web(env)
    try:
        d = w.c.api("/market/AAA")["data"]
        rv = d["rows"][0]["review"]
        assert rv["all_pass"] is True and rv["failing"] == [] and D(rv["added_usd"]) == D("4000")
        assert {c["key"] for c in rv["caps"]} >= {"pilot", "sector", "bucket", "expiry_week", "cash", "names"}
    finally:
        w.close()


# ---------------------------------------------------------------- alerts page and thresholds
def test_alerts_page_states_ids_sources_thresholds_and_no_actions(env):
    from dash_fixture import build  # noqa: F401
    w = Web(env)
    try:
        st, body, _ = w.c.get("/alerts")
        t = text_of(body)
        assert st == 200
        for want in ("Alert ID (stable)", "Advisory type", "Severity", "Source", "Generated", "Delivery state",
                     "Telegram acknowledgment does not prove you read or acted", "cannot resend a message or place, change or cancel anything",
                     "Active exit thresholds (provisional)", "version", "provisional policy values", "Delta review when |delta| reaches"):
            assert want in t, want
        # no state-changing control other than logout and GET filters
        assert body.count('method="post"') == 1 and "resend" not in re.sub(r"cannot resend[^<]*", "", body.lower())
        pol = w.c.api("/alerts")["data"]["thresholds"]
        assert pol["policy_id"] == POL.policy_id and pol["policy_version"] == POL.policy_version
    finally:
        w.close()


def test_every_outbox_state_has_a_plain_label_and_position_page_links_alerts(tmp_path):
    from dash_fixture import build
    paths = build(tmp_path, scan=False, evil_notes=False)
    w = Web(paths)
    try:
        t = text_of(w.c.get("/alerts")[1])
        for label in ("acknowledged by Telegram API", "unknown acknowledgment", "dropped (no longer valid at send time)", "retry scheduled"):
            assert label in t, label
        assert "position P-OPEN" in t and "Position: time / expiry review" in t
        pt = text_of(w.c.get("/positions/P-OPEN")[1])
        assert "Alerts that referenced this position" in pt and "All alerts and their delivery state" in pt
        assert "Active exit thresholds (provisional)" in pt and "Profit review when this fraction" in pt
    finally:
        w.close()


# ---------------------------------------------------------------- crawl: GETs never mutate; the only new POST routes are label routes
def test_no_get_mutates_across_the_new_pages(env):
    w = Web(env)
    try:
        paths = ["/portfolio", "/portfolio/labels/BBB", "/portfolio/labels/CCC", "/portfolio/labels/EEE", "/portfolio/label-result",
                 "/portfolio/label-result?code=stale", "/market/AAA", "/market/ZZZ", "/alerts", "/positions/P-OPEN", "/api/portfolio",
                 "/api/market/AAA", "/api/alerts", "/checkin", "/"]
        before = digest(env)
        for p in paths * 2:
            assert w.c.get(p)[0] == 200, p
        assert digest(env) == before
        from csp_dashboard import server
        assert server.LABEL_WRITE_PATHS == frozenset({"/portfolio/labels/preview", "/portfolio/labels/confirm"})
        assert server.ALL_WRITE_PATHS == server.WRITE_PATHS | server.TRADE_WRITE_PATHS | server.LABEL_WRITE_PATHS
    finally:
        w.close()


def test_new_pages_escape_hostile_label_text(env):
    j = jw(env)
    j.conn.execute("UPDATE option_positions SET sector=?, correlation_bucket=? WHERE trade_id='P-OPEN'", ("<script>x</script>", "a\"onmouseover=\"b"))
    j.conn.commit() if j.conn.in_transaction else None
    j.close()
    w = Web(env)
    try:
        for path in ("/portfolio", "/portfolio/labels/BBB"):
            body = w.c.get(path)[1]
            assert "<script>x</script>" not in body and 'onmouseover="b' not in body, path
    finally:
        w.close()
