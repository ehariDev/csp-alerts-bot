"""Stage B1 defect fixes (B1-1, B1-2, B1-3) and the owner-approved follow-ups: persisted contract info codes, automatic clearing of
policy-breach flags, order wording, control characters in reflected values. Synthetic data, loopback only."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from decimal import Decimal as D

import pytest
from dash_fixture import POLICY, Running, build
from factories import NOW, TODAY, policy
from test_dashboard_trade import EXP, Web, hidden
from test_webtrade import TZ, env, ik, jw, plan, pos, rec, sell  # noqa: F401  (env is a fixture)

from csp_alerts.jobs.scan import run_scan
from csp_alerts.journal import webtrade as wt
from csp_alerts.journal.models import PositionState as S, StockLot
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_dashboard import render

POL = policy()


def codes_blocking(env, at=NOW):
    j = jw(env)
    try:
        return {i.code for i in reconcile(j.load_book(), POL, at).issues if i.severity == "BLOCK"}
    finally:
        j.close()


# ---------------------------------------------------------------- B1-1 no default fees / dates on any trade form
def test_no_trade_form_prefills_fees_credit_debit_time_or_dates(env):
    w = Web(env)
    try:
        forms = {"sell": f"/trade/sell?symbol=AAA&expiry={EXP}&strike=40&kind=filled"}
        assert rec(env, {"action": wt.SELL_WORKING, "idem": ik("w1"), "symbol": "AAA", "expiry": EXP, "strike": "40"}).code == "recorded"
        assert rec(env, sell(idem=ik("w2"), symbol="ZZZ", sector="s", bucket="b"), acks=("ack_breach",)).code == "recorded"
        j = jw(env)
        ids = {p.state: p.trade_id for p in j.load_book().positions if p.trade_id.startswith("WEB-")}
        j.close()
        forms.update({"mark": f"/trade/mark-filled/{ids[S.WORKING]}", "close": f"/trade/close-fill/{ids[S.OPEN]}",
                      "expire": f"/trade/expire/{ids[S.OPEN]}"})
        for name, path in forms.items():
            st, body, _ = w.c.get(path)
            assert st == 200, name
            for fld in ("credit", "debit", "fees", "executed_date", "executed_time", "expiry_date"):
                for tag in re.findall(rf'<input[^>]*name="{fld}"[^>]*>', body):
                    assert 'value=""' in tag, (name, fld, tag)
                    if fld in ("fees", "expiry_date", "executed_date", "executed_time"):
                        assert " required" in tag, (name, fld)
    finally:
        w.close()


def test_expiry_date_is_required_and_never_defaulted(env):
    r = rec(env, sell(idem=ik("e1")))
    assert r.code == "recorded"
    p = plan(env, {"action": wt.EXPIRE, "idem": ik("e2"), "trade_id": r.trade_id})
    assert not p.ok and any("never defaulted" in e for e in p.errors)
    p = plan(env, {"action": wt.EXPIRE, "idem": ik("e3"), "trade_id": r.trade_id, "expiry_date": "2026-13-45"})
    assert not p.ok                                   # a real calendar date is checked, no exception


# ---------------------------------------------------------------- B1-2 ASCII digits only
@pytest.mark.parametrize("field,value", [("credit", "٠.45"), ("fees", "٠.65"), ("strike", "٤٠"), ("expiry", "٢٠٢٦-١٢-٠٤"),
                                          ("executed_time", "٠٩:٣٠"), ("executed_date", "٢٠٢٦-١٠-٣٠")])
def test_unicode_digits_are_not_numbers(env, field, value):
    assert not plan(env, sell(idem=ik("u" + field), **{field: value})).ok


def test_unicode_symbol_that_uppercases_to_ascii_is_refused(env):
    assert not plan(env, sell(idem=ik("us"), symbol="aaaſ")).ok            # long s uppercases to S
    assert not plan(env, sell(idem=ik("us2"), symbol="ıaa")).ok            # dotless i uppercases to I
    assert plan(env, sell(idem=ik("us3"), symbol="aaa")).ok                     # plain lower case still accepted


def test_checkin_unicode_digits_refused(env):
    base = {"action": wt.CHECKIN, "idem": ik("c1"), "equity": "100000", "free_cash": "50000", "nlots": "0"}
    assert plan(env, base).ok
    assert not plan(env, {**base, "equity": "١٠٠٠٠٠"}).ok
    assert not plan(env, {**base, "free_cash": "٥٠٠٠٠"}).ok
    lots_over = plan(env, {**base, "nlots": "٩٩"})
    assert lots_over.fields["nlots"] == "0"             # non-ASCII count is not read as a number


# ---------------------------------------------------------------- B1-3 many stock lots
def test_checkin_with_more_than_twelve_lots_is_a_page_not_a_crash(tmp_path):
    paths = build(tmp_path, scan=False, evil_notes=False)
    j = Journal(paths.db, clock=lambda: NOW)
    for i in range(13):
        j.add_stock_lot(StockLot(f"LOT{i:02d}", f"T{i}", D("10"), D("5"), NOW, "sec_x", "b_x", "PURCHASE", asset_type="ETF"))
    j.close()
    w = Web(paths)
    try:
        st, body, _ = w.c.get("/checkin")
        assert st == 200 and "More than 12 stock lots: use the CLI check-in." in body
        assert 'action="/checkin/preview"' not in body          # no partial form that could not show every lot
    finally:
        w.close()


# ---------------------------------------------------------------- order wording, reflected control characters
def test_sell_to_open_and_buy_to_close_wording(env):
    w = Web(env)
    try:
        st, body, _ = w.c.get(f"/trade/sell?symbol=AAA&expiry={EXP}&strike=40&kind=filled")
        assert st == 200 and "Sell to open" in body
        r = rec(env, sell(idem=ik("o1")))
        for slug in ("close-order", "close-fill"):
            st, body, _ = w.c.get(f"/trade/{slug}/{r.trade_id}")
            assert st == 200 and "Buy to close" in body, slug
        csrf = w.csrf()
        st, body, _ = w.post("/trade/preview", {"csrf": csrf, "action": wt.CLOSE_ORDER, "trade_id": r.trade_id})
        assert st == 200 and "Buy to close" in body
    finally:
        w.close()


def test_rejected_form_values_are_reflected_without_control_characters(env):
    w = Web(env)
    try:
        csrf = w.csrf()
        bad = "1\n2\t3\x07‮4 "
        st, body, _ = w.post("/trade/preview", {"csrf": csrf, "action": wt.SELL_FILLED, "symbol": "AAA", "expiry": EXP, "strike": "40",
                                                 "contracts": "1", "credit": bad, "fees": "0", "executed_date": "2026-10-30",
                                                 "executed_time": "09:30"})
        assert st == 200
        m = re.search(r'<input name="credit" value="([^"]*)"', body)
        assert m and m.group(1) == "1234", m and m.group(1)
    finally:
        w.close()
    assert render.clean_value("a\nb\tc\x00d​e") == "abcde"


# ---------------------------------------------------------------- contract-level info codes persisted and shown
def test_contract_info_codes_are_recorded_in_the_audit_event_and_shown_on_the_position_page(env):
    c = sqlite3.connect(env.db)
    c.execute("UPDATE md_option_latest SET ask='2.00' WHERE symbol='AAA' AND strike='40'")
    c.commit()
    c.close()
    f = sell(idem=ik("i1"))
    p = plan(env, f)
    assert p.ok and "SPREAD_TOO_WIDE" in p.info_codes and not p.breach_codes          # information only: not blocking
    r = rec(env, f)
    assert r.code == "recorded"
    ev = [json.loads(e["after_json"]) for e in jw(env).audit_events("WEB_TRADE_RECORDED")]
    assert ev and "SPREAD_TOO_WIDE" in ev[-1]["contract_info_codes"]
    w = Web(env)
    try:
        st, body, _ = w.c.get(f"/positions/{r.trade_id}")
        assert st == 200 and "Recorded with off-strategy contract characteristics (information)" in body and "SPREAD_TOO_WIDE" in body
        assert "Active exit thresholds (provisional)" in body
    finally:
        w.close()


def test_no_info_codes_means_no_information_section(env):
    r = rec(env, sell(idem=ik("i2")))
    w = Web(env)
    try:
        st, body, _ = w.c.get(f"/positions/{r.trade_id}")
        assert st == 200 and "off-strategy contract characteristics" not in body
    finally:
        w.close()


# ---------------------------------------------------------------- policy-breach flags clear when they stop reproducing
def _two_aaa(env):
    a = rec(env, sell(idem=ik("b1"), strike="40"))
    b = rec(env, sell(idem=ik("b2"), strike="42"), acks=("ack_breach",))
    assert a.code == b.code == "recorded" and b.breach_codes
    return a.trade_id, b.trade_id


def test_breach_stays_while_a_cap_is_still_breached_and_clears_when_it_stops_with_an_audit_event(env):
    a, b = _two_aaa(env)
    assert "POLICY_BREACH_UNRESOLVED" in codes_blocking(env)
    # an unrelated record (a check-in) re-checks the flag: still breached (second contract on the same underlying)
    ci = {"action": wt.CHECKIN, "idem": ik("b3"), "equity": "100000", "free_cash": "50000", "nlots": "0"}
    assert rec(env, ci, acks=("ack_reconciled",), now=NOW + timedelta(minutes=1)).code == "recorded"
    assert not jw(env).audit_events("POLICY_BREACH_CLEARED")
    assert "POLICY_BREACH_UNRESOLVED" in codes_blocking(env, NOW + timedelta(minutes=2))
    # closing the OTHER (non-flagged) put removes the cause: the flag on `b` clears by itself
    close = {"action": wt.CLOSE_FILLED, "idem": ik("b4"), "trade_id": a, "debit": "0.2", "fees": "0", "executed_date": "2026-10-30",
             "executed_time": "10:00"}
    assert rec(env, close, now=NOW + timedelta(minutes=3)).code == "recorded"
    cleared = jw(env).audit_events("POLICY_BREACH_CLEARED")
    assert len(cleared) == 1
    d = json.loads(cleared[0]["after_json"])
    assert d["trade_id"] == b and "CONTRACT_COUNT_LIMIT" in d["cleared_codes"] and cleared[0]["actor"] == "web:owner"
    assert "POLICY_BREACH_UNRESOLVED" not in codes_blocking(env, NOW + timedelta(minutes=4))
    assert pos(env, b).state is S.OPEN                 # the position itself is untouched


def test_scan_clears_a_flag_after_a_cli_style_close(env):
    a, b = _two_aaa(env)
    j = jw(env)
    j.transition(a, S.CLOSE_WORKING)
    j.transition(a, S.CLOSED, actual_close_debit_per_share=D("0.2"), closed_at_utc=NOW, fees_usd=D("0"))
    approved = env.approved_list

    class Src:
        def fetch_chain(self, *a):
            from csp_alerts.market.stockpile import RawChain
            return RawChain("AAA", [], NOW)

        def fetch_earnings_dates(self, t):
            return None
    run_scan(journal=j, policy=POL, approved=approved, source=Src(), now_utc=NOW + timedelta(minutes=5),
             session=SessionInfo(SessionStatus.OPEN, 300), sleep=lambda s: None)
    assert [json.loads(e["after_json"])["trade_id"] for e in j.audit_events("POLICY_BREACH_CLEARED")] == [b]
    j.close()


def test_unreadable_approvals_clear_nothing(env):
    a, b = _two_aaa(env)
    j = jw(env)
    j.transition(a, S.CLOSE_WORKING)
    j.transition(a, S.CLOSED, actual_close_debit_per_share=D("0.2"), closed_at_utc=NOW, fees_usd=D("0"))
    assert wt.sweep_breaches(j, POL, None, NOW, reason="t") == []
    assert not j.audit_events("POLICY_BREACH_CLEARED")
    assert wt.sweep_breaches(j, POL, env.approved_list, NOW, reason="t") == [b]
    j.close()


def test_free_cash_breach_clears_after_a_fresh_confirmed_check_in_not_before(env):
    j = jw(env)
    j.add_snapshot(__import__("factories").snap(equity="100000", cash="1000", asof=NOW - timedelta(minutes=30), sid="low"))
    j.close()
    r = rec(env, sell(idem=ik("k1")), acks=("ack_breach",))
    assert r.code == "recorded" and "INSUFFICIENT_FREE_CASH" in r.breach_codes
    assert not jw(env).audit_events("POLICY_BREACH_CLEARED")
    ci = {"action": wt.CHECKIN, "idem": ik("k2"), "equity": "100000", "free_cash": "50000", "nlots": "0"}
    assert rec(env, ci, acks=("ack_reconciled",), now=NOW + timedelta(minutes=1)).code == "recorded"
    assert len(jw(env).audit_events("POLICY_BREACH_CLEARED")) == 1


def test_reconcile_text_no_longer_says_review_it(env):
    _two_aaa(env)
    j = jw(env)
    msgs = [i.message for i in reconcile(j.load_book(), POL, NOW).issues if i.code == "POLICY_BREACH_UNRESOLVED"]
    j.close()
    assert msgs and all("review it" not in m and "re-checked automatically" in m for m in msgs)
    w = Web(env)
    try:
        st, body, _ = w.c.get("/")
        assert st == 200 and "review it;" not in body and "No action button exists" in body
    finally:
        w.close()


@pytest.mark.parametrize("fees,ok", [("", False), ("   ", False), ("0", True), ("0.00", True), ("0.65", True)])
def test_fees_are_required_on_the_server_for_every_form(env, fees, ok):
    p = plan(env, sell(idem=ik("f" + fees), fees=fees))
    assert p.ok is ok
    if not ok:
        assert wt.FEES_REQUIRED in p.errors
    f = sell(idem=ik("fm"))
    f.pop("fees")
    assert not plan(env, f).ok                                      # missing key behaves like blank
    r = rec(env, sell(idem=ik("fc")))
    close = {"action": wt.CLOSE_FILLED, "idem": ik("fc2"), "trade_id": r.trade_id, "debit": "0.2", "executed_date": "2026-10-30", "executed_time": "10:00"}
    assert wt.FEES_REQUIRED in plan(env, close).errors
    assert plan(env, {**close, "fees": "0"}).ok
