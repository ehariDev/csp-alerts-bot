"""Stage B2 section G: security / regression / truthfulness of the new pages. Synthetic data, loopback, no network."""
from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest
from b1_env import B1, text_of
from b2_env import REF_EXTRAS, ref_env
from test_b1_security import _closure
from web_helpers import fingerprint

REPO = Path(__file__).resolve().parents[2]
NEW_PAGES = ["/portfolio", "/portfolio/labels/AAA", "/portfolio/labels/UUU", "/portfolio/label-result?code=label_recorded&ticker=AAA",
             "/portfolio/label-result?code=stale", "/alerts", "/market/AAA", "/market/BBB", "/market/ZZZ", "/checkin", "/trade/result?code=recorded",
             "/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled", "/api/portfolio", "/api/alerts", "/api/overview", "/api/positions"]


@pytest.fixture()
def r(tmp_path):
    w = ref_env(tmp_path)
    yield w
    w.stop()


def test_dashboard_import_closure_includes_label_module_and_still_has_no_network_subprocess_telegram_or_broker():
    edges = _closure()
    assert "csp_alerts.journal.weblabels" in edges and "csp_alerts.journal.webtrade" in edges and "csp_alerts.policy.portfolio" in edges
    from test_b1_security import BAD_INTERNAL, BAD_ROOTS
    for mod, imps in edges.items():
        for i in imps:
            ok = mod == "csp_dashboard.server" and i in ("http.server", "http.cookies")
            assert i.split(".")[0] not in BAD_ROOTS or ok, (mod, i)
            assert not i.startswith(BAD_INTERNAL), (mod, i)


def test_new_source_modules_contain_no_order_or_broker_vocabulary():
    pat = re.compile(r"place_?order|submit_?order|send_?order|broker|brokerage|ib_insync|alpaca|schwab|tradier", re.I)
    for f in ("journal/weblabels.py",):
        tree = ast.parse((REPO / "src" / "csp_alerts" / f).read_text(encoding="utf-8"))
        names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)] + [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)] + \
                [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        assert [n for n in names if pat.search(n)] == [], f


def test_every_get_page_and_json_twin_leaves_database_bytes_unchanged(r):
    before = fingerprint(r.db)
    for p in NEW_PAGES:
        for suffix in ("", "&confirm=1&action=label_correct&tok=1&sector=x" if "?" in p else "?confirm=1&action=label_correct&tok=1&sector=x"):
            st, _, _ = r.get(p + suffix)
            assert st in (200, 400, 404), (p, st)
    assert fingerprint(r.db) == before


def test_json_twin_has_no_write_route_and_get_on_label_writes_is_refused(r):
    for p in ("/api/portfolio/labels/preview", "/api/portfolio/labels/confirm", "/api/alerts/resend", "/api/trade/preview"):
        assert r.post(p, {"ticker": "AAA"})[0] in (404, 405), p
    for p in ("/portfolio/labels/preview", "/portfolio/labels/confirm"):
        assert r.get(p)[0] in (404, 405), p


def test_forms_on_new_pages_post_only_to_the_nine_write_routes_with_csrf(r):
    allowed = {"/logout", "/watchlist/add", "/watchlist/remove", "/market/refresh-request", "/trade/preview", "/trade/confirm",
               "/checkin/preview", "/checkin/confirm", "/portfolio/labels/preview", "/portfolio/labels/confirm"}
    for p in NEW_PAGES:
        if p.startswith("/api"):
            continue
        h = r.get(p)[2]
        low = h.lower()
        assert "<script" not in low and "javascript:" not in low and not re.search(r"\son[a-z]+\s*=", low), p
        for m in re.finditer(r"<form\b[^>]*>(.*?)</form>", h, re.S | re.I):
            tag = re.match(r"<form\b[^>]*>", m.group(0), re.I).group(0).lower()
            if 'method="post"' in tag:
                act = re.search(r'action="([^"]+)"', tag).group(1)
                assert act in allowed, (p, act)
                if act != "/logout":
                    assert 'name="csrf"' in m.group(1), (p, act)
        assert not re.search(r'(href|src|action)\s*=\s*"\s*(https?:)?//', h, re.I), p


def test_new_pages_keep_truthful_market_wording_no_live_claim_no_advice(r):
    blob = {p: text_of(r.get(p)[2]) for p in ("/portfolio", "/market/AAA", "/alerts", "/portfolio/labels/AAA")}
    for p, t in blob.items():
        low = t.lower()
        for bad in ("real-time", "realtime", "live quote", "live price", "guaranteed", "risk-free", "risk free", "safe to trade", "we recommend",
                    "you should sell", "you should buy", "best trade", "maximum loss is", "cannot lose"):
            assert bad not in low, (p, bad)
        assert "indicative" in low, p
        if p.startswith("/market"):
            assert "yahoo" in low
    m = blob["/market/AAA"].lower()
    assert "quote age is unknown" in m or "quote age unknown" in m
    assert "not a live exchange feed" in m
    assert "exposure budget, not maximum possible loss" in blob["/portfolio"].lower() or "not maximum possible loss" in blob["/portfolio"].lower()


def test_hostile_text_never_breaks_new_pages(r):
    for p in ("/market/%3Cscript%3E", "/portfolio/labels/%3Cimg%20src=x%3E", "/portfolio?x=%3Cscript%3E", "/alerts?status=%3Cscript%3E",
              "/market/AAA?expiry=%3Cscript%3E&strike_min=%3Cb%3E", "/portfolio/label-result?code=%3Cscript%3E&ticker=%3Cscript%3E"):
        st, _, h = r.get(p)
        assert st in (200, 400, 404) and "<script>" not in h and "Traceback" not in h, p


def test_secrets_and_paths_never_appear_in_new_pages(r):
    for p in NEW_PAGES:
        h = r.get(p)[2]
        assert "BOT_TOKEN" not in h and "chat_id" not in h.lower() and "scrypt" not in h.lower(), p
        assert str(r.tmp) not in h and "Traceback" not in h, p


def test_the_real_journal_is_untouched_still_zero_positions_and_zero_web_events():
    db = REPO / "data" / "csp_alerts.sqlite3"
    if not db.exists():
        return
    c = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert c.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type LIKE 'WEB_%' OR event_type LIKE 'POLICY_BREACH%' "
                         "OR event_type='LABEL_CORRECTED'").fetchone()[0] == 0
    finally:
        c.close()
