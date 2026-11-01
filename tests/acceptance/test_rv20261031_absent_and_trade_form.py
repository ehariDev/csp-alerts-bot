"""Follow-up acceptance for RV-ACC-03/04/05 (absent contracts, trade form, rendered pages). Synthetic only.

Same frozen clock/tick convention as test_rv20261031_latest_evidence.py (tick N = 2026-10-30T15:05Z + 5N minutes).
Oracles (hand-derived from the requirement "retain history, flag not-current, keep unaffected data usable"):
  * A contract missing from a later SUCCESSFUL chain for an expiry the fetch returned: row keeps 0.50/0.54 @ tick0, flagged
    not current, not all_pass; sibling strike unaffected; reappearing with a valid quote clears; reappearing invalid stays flagged.
  * An expiry the fetch did not return (narrow monitor window) or that falls out of the 8-expiry cap is never marked.
  * Held vanished contract: no PROFIT_EXIT / pnl (credit 0.50, ask 0.15 would be (0.50-0.15)*100 = 35.00), original time kept,
    TIME_EXIT / ASSIGNMENT_PENDING_RECONCILIATION still present; recovery at ask 0.34 -> (0.50-0.34)*100 = 16.00.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal as D

import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "unit")]
from chain_fixtures import raw_row  # noqa: E402
from test_rv20261031_latest_evidence import EXP15, TODAY, Rig, rig, two  # noqa: F401  (rig is the fixture)

from factories import NOW as TODAY_NOW

from csp_alerts.journal import webtrade as wt
from csp_alerts.journal.models import PositionState
from csp_dashboard import render as R
from csp_dashboard import views
from csp_dashboard.readonly import ReadOnlyJournal

TZ = "America/Chicago"
M = {"_csrf": "tok", "rendered_at_utc": TODAY_NOW, "schema_expected": 0}


def day(n):
    return (TODAY + timedelta(days=n)).isoformat()


def by_key(d):
    return {(str(r["expiration"]), r["strike"]): r for r in d["rows"]}


def test_vanished_contract_marked_then_clears_on_reappearance_with_valid_quote(rig):
    rig.update(two(), 0)
    rig.update([raw_row(strike=42.0, spot=48.0)], 1)
    a = rig.rows(1)["40"]
    assert a.get("retained_unusable") and not a["review"]["all_pass"]
    assert (str(a["bid"]), str(a["ask"])) == ("0.5", "0.54") and str(a["retrieved_at_utc"]).startswith("2026-10-30 15:05")
    b = rig.rows(1)["42"]
    assert b["review"]["all_pass"] and not b.get("retained_unusable"), "other strikes unaffected"
    rig.update(two(), 2)
    a2 = rig.rows(2)["40"]
    assert a2["review"]["all_pass"] and not a2.get("retained_unusable")
    assert str(a2["retrieved_at_utc"]).startswith("2026-10-30 15:15")
    assert "contracts" not in rig.evidence()


def test_vanished_contract_reappearing_invalid_stays_marked_with_original_time(rig):
    rig.update(two(), 0)
    rig.update([raw_row(strike=42.0, spot=48.0)], 1)
    rig.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], 2)
    a = rig.rows(2)["40"]
    assert not a["review"]["all_pass"] and a.get("retained_unusable")
    assert str(a["retrieved_at_utc"]).startswith("2026-10-30 15:05")
    assert rig.evidence()["contracts"]
    assert rig.rows(2)["42"]["review"]["all_pass"]


def test_expiry_not_returned_by_fetch_is_never_marked(rig):
    e40, e15 = day(40), day(15)
    rig.update([raw_row(strike=40.0, spot=48.0, exp=e40), raw_row(strike=40.0, spot=48.0, exp=e15)], 0)
    rig.update([raw_row(strike=40.0, spot=48.0, exp=e40)], 1)          # narrow window: 15-day expiry not fetched
    assert "contracts" not in rig.evidence(), rig.evidence()
    rows = by_key(rig.detail(1))
    assert not rows[(e15, "40")].get("retained_unusable")
    assert str(rows[(e15, "40")]["retrieved_at_utc"]).startswith("2026-10-30 15:05")
    assert not rows[(e40, "40")].get("retained_unusable")


def test_expiry_displaced_by_cap_is_not_marked(rig):
    # 8 expiries (days 36..43) stored; next fetch has 9 incl. nearer day 35, so the farthest (43) falls outside the cap.
    rig.update([raw_row(strike=40.0, spot=48.0, exp=day(n)) for n in range(36, 44)], 0)
    rig.update([raw_row(strike=40.0, spot=48.0, exp=day(n)) for n in range(35, 44)], 1)
    assert "contracts" not in rig.evidence(), rig.evidence()


def test_held_vanished_contract_no_numeric_advice_original_time_kept_then_recovers(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.13, ask=0.15), raw_row(strike=42.0, spot=51.0)], 0)
    assert "PROFIT_EXIT" in [w["trigger"] for w in rig.pos("HOLD", 0)["warnings"]]
    rig.update([raw_row(strike=42.0, spot=51.0)], 1)
    v = rig.pos("HOLD", 1)
    assert "PROFIT_EXIT" not in [w["trigger"] for w in v["warnings"]] and v["pnl"] is None
    assert v["price_check"]["status"] == "unavailable" and v["price_check"]["reason"]
    ev = v["price_check"]["evidence"]
    assert ev["retained_unusable"] is True and str(ev["retrieved_at_utc"]).startswith("2026-10-30 15:05") and str(ev["ask"]) == "0.15"
    rig.update([raw_row(strike=40.0, spot=52.0, bid=0.30, ask=0.34), raw_row(strike=42.0, spot=52.0)], 2)
    v2 = rig.pos("HOLD", 2)
    assert v2["price_check"]["status"] == "usable" and v2["pnl"]["net_of_fees_usd"] == D("16.00")


def test_held_vanished_contract_time_exit_survives(rig):
    rig.hold("T15", "40", "0.50", exp=TODAY + timedelta(days=15))
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.45, ask=0.48, exp=EXP15), raw_row(strike=42.0, spot=51.0, exp=EXP15)], 0)
    rig.update([raw_row(strike=42.0, spot=51.0, exp=EXP15)], 1)
    v = rig.pos("T15", 1)
    assert "TIME_EXIT" in [w["trigger"] for w in v["warnings"]] and v["pnl"] is None


def test_normal_rows_unchanged_when_nothing_is_wrong(rig):
    rig.update(two(), 0)
    rig.update(two(), 1)
    rows = rig.rows(1)
    for k in ("40", "42"):
        assert rows[k]["review"]["all_pass"] and not rows[k].get("retained_unusable")
        assert (str(rows[k]["bid"]), str(rows[k]["ask"])) == ("0.5", "0.54")
        assert str(rows[k]["retrieved_at_utc"]).startswith("2026-10-30 15:10")
    assert rig.evidence() == {}


def test_assignment_pending_warning_survives_invalid_quote(rig):
    rig.hold("ASG", "40", "0.50")
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.13, ask=0.15), raw_row(strike=42.0, spot=51.0)], 0)
    rig.j.transition("ASG", PositionState.ASSIGNED_PENDING_RECONCILIATION)
    rig.update([raw_row(strike=40.0, spot=52.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=52.0)], 1)
    v = rig.pos("ASG", 1)
    trig = [w["trigger"] for w in v["warnings"]]
    assert "ASSIGNMENT_PENDING_RECONCILIATION" in trig and "PROFIT_EXIT" not in trig and v["pnl"] is None


# ---------------------------------------------------------------- trade form
def test_trade_context_flag_for_invalid_vanished_clean_and_recovered(rig):
    exp = TODAY + timedelta(days=40)
    rig.update(two(), 0)
    assert not wt.contract_context(rig.j.conn, "AAA", exp, D("40"))["retained_unusable"]
    rig.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], 1)
    assert wt.contract_context(rig.j.conn, "AAA", exp, D("40"))["retained_unusable"]
    assert not wt.contract_context(rig.j.conn, "AAA", exp, D("42"))["retained_unusable"]
    rig.update(two(), 2)
    assert not wt.contract_context(rig.j.conn, "AAA", exp, D("40"))["retained_unusable"]
    rig.update([raw_row(strike=42.0, spot=48.0)], 3)
    assert wt.contract_context(rig.j.conn, "AAA", exp, D("40"))["retained_unusable"]


def _form(rig, n, strike):
    rj = ReadOnlyJournal(rig.db)
    with rj.snapshot():
        d = views.trade_sell_form(rig.env(rj, n), "AAA", day(40), strike, "filled")
    rj.close()
    return R.page_trade_form(d, M, TZ)


def test_rendered_trade_form_not_current_only_for_affected_contract(rig):
    rig.update(two(), 0)
    rig.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], 1)
    assert "NOT CURRENT" in _form(rig, 1, "40")
    assert "NOT CURRENT" not in _form(rig, 1, "42")


def test_rendered_trade_form_vanished_contract_not_current(rig):
    rig.update(two(), 0)
    rig.update([raw_row(strike=42.0, spot=48.0)], 1)
    assert "NOT CURRENT" in _form(rig, 1, "40")


def test_trade_assessment_flags_missing_market_data_only_for_retained_quote(rig):
    exp = TODAY + timedelta(days=40)
    rig.update(two(), 0)
    rig.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], 1)
    book = rig.j.load_book(rig.ap)
    ap = rig.ap[0] if isinstance(rig.ap, list) else None
    codes = {}
    for k in ("40", "42"):
        row = wt._stored_contract(rig.j.conn, "AAA", exp, D(k))
        a = wt._assess(book, ap, row, "AAA", exp, D(k), "sec_AAA", "b_AAA", "ETF", False, rig.pol, rig.t(1))
        codes[k] = set(a["info"]) | set(a["flagged"]) | set(a["context"])
    assert "MISSING_MARKET_DATA" in codes["40"]
    assert "MISSING_MARKET_DATA" not in codes["42"]


# ---------------------------------------------------------------- rendered market / position pages
def test_rendered_market_page_labels_affected_row_and_control_has_no_label(tmp_path):
    r = Rig(tmp_path)
    try:
        r.update(two(), 0)
        r.update(two(), 1)
        assert not re.search(r"(?i)not current", R.page_market(r.detail(1), M, TZ, {}))
        r.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], 2)
        html = R.page_market(r.detail(2), M, TZ, {})
        assert re.search(r"(?i)not current", html)
    finally:
        r.j.close()


def test_rendered_position_page_has_no_profit_exit_or_pnl_for_retained_quote(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.13, ask=0.15), raw_row(strike=42.0, spot=51.0)], 0)
    rig.update([raw_row(strike=40.0, spot=52.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=52.0)], 1)
    rj = ReadOnlyJournal(rig.db)
    with rj.snapshot():
        d = views.position_detail(rig.env(rj, 1), "HOLD")
    rj.close()
    html = R.page_position_detail(d, M, TZ)
    assert "PROFIT_EXIT" not in html and "35.00" not in html
    assert re.search(r"(?i)not current|unavailable", html)
