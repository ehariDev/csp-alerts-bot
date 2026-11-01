"""RV-20261031-01 / -02 acceptance: good -> invalid -> recovered sequences (MD-AT-03/04, WT-AT-01/02/05, TC-04).

Independent of the implementer's unit file. Synthetic provider, temporary SQLite journal, no network, no Telegram.

Frozen clock NOW = 2026-10-30T15:05Z (Friday, session OPEN). Tick N = NOW + 5*N minutes. Chain expiry = NOW.date + 40 d.

Hand oracles
------------
Stock:  tick0 both rows spot 48 -> stock 48 retrieved tick0, candidates all_pass True (control).
        tick1 rows spot 49 / 50 (contradiction) -> stock stays 48 @ tick0; stock failure visible; NO candidate all_pass;
              put rows still SHOWN with bid/ask 0.50/0.54 retrieved tick1; displayed stock is neither 49, 49.5 nor 50.
        tick2 rows spot 51 consistent -> stock 51 @ tick2, failure cleared, all_pass True again.
Contract (strike 40 broken, strike 42 control): tick0 valid 0.50/0.54 both; tick1 strike40 invalid, strike42 valid
        0.50/0.54 spot 51 -> strike40 row keeps 0.50/0.54 retrieved tick0 (original time), all_pass False, flagged retained;
        strike42 retrieved tick1, all_pass True. tick2 strike40 valid 0.30/0.34 -> retrieved tick2, all_pass True.
Held position: actual credit 0.50, 1 contract, x100, fees 0.
        ask 0.15 -> (0.50 - 0.15) * 100 = 35.00 -> PROFIT_EXIT (as in the reviewer's control)
        ask 0.34 -> (0.50 - 0.34) * 100 = 16.00 net (recovery control; no profit trigger)
        ask 0.54 on the unaffected strike 42 with credit 0.60 -> (0.60 - 0.54) * 100 = 6.00 (control stays usable).
"""
from __future__ import annotations

import math
import re
import sys
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "unit")]
from chain_fixtures import EXP, raw_row  # noqa: E402
from dash_fixture import approved_file  # noqa: E402
from factories import NOW, TODAY, policy, snap  # noqa: E402
from market_fixtures import FakeProvider  # noqa: E402

from csp_alerts.jobs.market_refresh import run_market_refresh  # noqa: E402
from csp_alerts.journal import watchlist  # noqa: E402
from csp_alerts.journal import webtrade as wt  # noqa: E402
from csp_alerts.journal.models import PositionState  # noqa: E402
from csp_alerts.journal.store import Journal, NewPosition  # noqa: E402
from csp_alerts.market.models import SessionInfo, SessionStatus  # noqa: E402
from csp_alerts.market import observations as obs  # noqa: E402
from csp_alerts.policy.approved import load_approved  # noqa: E402
from csp_dashboard import views  # noqa: E402
from csp_dashboard.readonly import ReadOnlyJournal  # noqa: E402

EXP15 = (TODAY + timedelta(days=15)).isoformat()


class Rig:
    def __init__(self, tmp_path):
        self.db = tmp_path / "j.sqlite3"
        self.pol = policy()
        self.ap = load_approved(approved_file(tmp_path / "approved.json"))
        self.j = Journal(self.db, clock=lambda: NOW)
        self.j.migrate()
        self.j.record_policy(self.pol)
        self.j.confirm_baseline(D("100000"))
        self.j.add_snapshot(snap())
        watchlist.add_symbol(self.j, "AAA", actor="acc", source="synthetic")
        self.p = FakeProvider()

    def t(self, n):
        return NOW + timedelta(minutes=5 * n)

    def update(self, rows, n):
        self.p.fetched_at = self.t(n)
        self.p.set("AAA", rows=rows)
        return run_market_refresh(journal=self.j, policy=self.pol, source=self.p, now_utc=self.t(n),
                                  session=SessionInfo(SessionStatus.OPEN, 300), sleep=lambda _: None, pause_seconds=0)

    def fail_fetch(self, n):
        self.p.fetched_at = self.t(n)
        self.p.fail("AAA")
        return run_market_refresh(journal=self.j, policy=self.pol, source=self.p, now_utc=self.t(n),
                                  session=SessionInfo(SessionStatus.OPEN, 300), sleep=lambda _: None, pause_seconds=0)

    def env(self, rj, n):
        return views.Env(rj=rj, now=self.t(n), tz="America/Chicago", policy=self.pol, policy_error=None,
                         approved=self.ap, approved_error=None)

    def detail(self, n):
        rj = ReadOnlyJournal(self.db)
        with rj.snapshot():
            d = views.market_detail(self.env(rj, n), "AAA")
        rj.close()
        return d

    def rows(self, n):
        return {r["strike"]: r for r in self.detail(n)["rows"]}

    def pos(self, tid, n):
        rj = ReadOnlyJournal(self.db)
        with rj.snapshot():
            pv = views.position_view(self.env(rj, n), rj.get_position(tid))
        rj.close()
        return pv

    def hold(self, tid, strike, credit, exp=None, close=False):
        exp_d = TODAY + timedelta(days=40) if exp is None else exp
        self.j.create_draft(NewPosition(tid, "AAA", exp_d, D(strike), 100, 1, "sec_AAA", "b_AAA"))
        self.j.transition(tid, PositionState.PENDING)
        self.j.transition(tid, PositionState.OPEN, fill_contracts=1, actual_open_credit_per_share=D(credit), opened_at_utc=NOW,
                          fees_usd=D("0"))

    def evidence(self):
        return obs.latest_evidence(self.j.conn, "AAA")


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.j.close()


def two(spot40=48.0, spot42=48.0, **kw40):
    return [raw_row(strike=40.0, spot=spot40, **kw40), raw_row(strike=42.0, spot=spot42)]


def stock_text(d):
    return str(d["block"]["stock"])


# ------------------------------------------------------------------ RV-01 stock
def test_rv01_conflicting_spots_after_good_evidence_block_all_candidates_then_recover(rig):
    rig.update(two(), 0)
    d0 = rig.detail(0)
    assert d0["rows"] and all(r["review"]["all_pass"] for r in d0["rows"]), "control: consistent spots pass"
    assert "48" in stock_text(d0)

    rig.update(two(49.0, 50.0), 1)
    d1 = rig.detail(1)
    assert d1["rows"], "put rows must remain visible"
    assert not any(r["review"]["all_pass"] for r in d1["rows"]), "conflicting spots must not leave any candidate all_pass"
    assert all(r["gate"]["marker"] != "PASS" for r in d1["rows"])
    for r in d1["rows"]:     # row provenance: tick1 rows were valid and are shown with tick1 time and the new quote
        assert r["bid"] == D("0.5") and r["ask"] == D("0.54") or (str(r["bid"]), str(r["ask"])) == ("0.5", "0.54")
        assert str(r["retrieved_at_utc"]).startswith("2026-10-30 15:10")
    assert str(d1["block"]["stock"]["price"]) == "48"        # neither 49, 49.5 (average) nor 50
    assert d1["block"].get("stock_failure") is not None, "a stock-specific latest-evidence failure must be visible"
    assert str(d1["block"]["stock"]["retrieved_at_utc"]).startswith("2026-10-30 15:05"), "retained price keeps its true (tick0) time"
    # stock row itself unchanged in the store
    row = rig.j.conn.execute("SELECT price, retrieved_at_utc FROM md_stock_latest WHERE symbol='AAA'").fetchone()
    assert (row["price"], row["retrieved_at_utc"][:16]) == ("48", "2026-10-30T15:05") or str(row["price"]).startswith("48")
    assert row["retrieved_at_utc"].startswith("2026-10-30T15:05")

    rig.update(two(51.0, 51.0), 2)
    d2 = rig.detail(2)
    assert "51" in stock_text(d2)
    assert d2["block"].get("stock_failure") is None
    assert all(r["review"]["all_pass"] for r in d2["rows"]), "recovery restores the verdict"


@pytest.mark.parametrize("bad_spot", [float("nan"), 0.0, -3.0, None])
def test_rv01_invalid_spot_after_good_evidence_not_all_pass(rig, bad_spot):
    rig.update(two(), 0)
    rows = [raw_row(strike=40.0, spot=48.0), raw_row(strike=42.0, spot=48.0)]
    for r in rows:
        r["spot"] = bad_spot
    rig.update(rows, 1)
    d = rig.detail(1)
    assert not any(r["review"]["all_pass"] for r in d["rows"])
    assert "48" in stock_text(d)
    assert d["block"].get("stock_failure") is not None


def test_rv01_fetch_failure_does_not_erase_stock_failure_mark(rig):
    rig.update(two(), 0)
    rig.update(two(49.0, 50.0), 1)
    rig.fail_fetch(2)
    d = rig.detail(2)
    assert not any(r["review"]["all_pass"] for r in d["rows"])
    rig.update(two(52.0, 52.0), 3)
    assert all(r["review"]["all_pass"] for r in rig.detail(3)["rows"])


# ------------------------------------------------------------------ RV-02 contracts
INVALID = {
    "zero_zero": dict(bid=0.0, ask=0.0),
    "bid0_ask_pos": dict(bid=0.0, ask=0.54),
    "bid_pos_ask0": dict(bid=0.5, ask=0.0),
    "negative_bid": dict(bid=-0.1, ask=0.54),
    "negative_ask": dict(bid=0.5, ask=-0.54),
    "nan_bid": dict(bid=math.nan, ask=0.54),
    "nan_ask": dict(bid=0.5, ask=math.nan),
    "inf_ask": dict(bid=0.5, ask=math.inf),
    "crossed": dict(bid=0.60, ask=0.50),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_rv02_invalid_latest_quote_makes_retained_row_unusable_other_strike_stays_usable(rig, name):
    rig.update(two(), 0)
    assert all(r["review"]["all_pass"] for r in rig.rows(0).values())
    # strike 40 invalid at tick1, strike 42 valid with a new spot 51 for both
    rig.update([raw_row(strike=40.0, spot=51.0, **INVALID[name]), raw_row(strike=42.0, spot=51.0)], 1)
    rows = rig.rows(1)
    a, b = rows["40"], rows["42"]
    assert not a["review"]["all_pass"], f"{name}: retained prior quote must not pass gates"
    assert a["gate"]["marker"] != "PASS"
    assert (str(a["bid"]), str(a["ask"])) == ("0.5", "0.54"), "history preserved, no invented values"
    assert str(a["retrieved_at_utc"]).startswith("2026-10-30 15:05"), "original timestamp preserved"
    assert a.get("retained_unusable"), "affected row flagged retained/unusable"
    assert b["review"]["all_pass"] and b["gate"]["marker"] == "PASS", "unaffected strike stays usable"
    assert str(b["retrieved_at_utc"]).startswith("2026-10-30 15:10")
    assert not b.get("retained_unusable")
    # recovery
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.55, ask=0.59), raw_row(strike=42.0, spot=51.0)], 2)
    a2 = rig.rows(2)["40"]
    assert a2["review"]["all_pass"] and not a2.get("retained_unusable")
    assert (str(a2["bid"]), str(a2["ask"])) == ("0.55", "0.59")
    assert str(a2["retrieved_at_utc"]).startswith("2026-10-30 15:15")
    assert "40|" not in str(rig.evidence()) and "contracts" not in rig.evidence(), "failure mark cleared on recovery"


def test_rv02_repeated_invalid_keeps_original_quote_time_and_stays_unusable(rig):
    rig.update(two(), 0)
    for n in (1, 2, 3):
        rig.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], n)
        a = rig.rows(n)["40"]
        assert not a["review"]["all_pass"]
        assert str(a["retrieved_at_utc"]).startswith("2026-10-30 15:05")


def test_rv02_invalid_with_no_prior_quote_is_never_a_pass(rig):
    rig.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], 0)
    rows = rig.rows(0)
    assert not rows["40"]["review"]["all_pass"]
    assert rows["42"]["review"]["all_pass"]


# ------------------------------------------------------------------ RV-02 held position
def test_rv02_held_position_no_profit_exit_from_stale_quote_then_recovers(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.13, ask=0.15), raw_row(strike=42.0, spot=51.0)], 0)
    v0 = rig.pos("HOLD", 0)
    assert "PROFIT_EXIT" in [w["trigger"] for w in v0["warnings"]]
    assert v0["price_check"]["status"] == "usable" and v0["pnl"]["net_of_fees_usd"] == D("35.00")   # (0.50-0.15)*100

    rig.update([raw_row(strike=40.0, spot=52.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=52.0)], 1)
    v1 = rig.pos("HOLD", 1)
    assert "PROFIT_EXIT" not in [w["trigger"] for w in v1["warnings"]]
    assert v1["pnl"] is None
    assert v1["price_check"]["status"] == "unavailable"
    ev = v1["price_check"]["evidence"]
    assert str(ev["retrieved_at_utc"]).startswith("2026-10-30 15:05") and str(ev["ask"]) == "0.15"   # original kept, visible
    assert ev["retained_unusable"] is True

    rig.update([raw_row(strike=40.0, spot=53.0, bid=0.30, ask=0.34), raw_row(strike=42.0, spot=53.0)], 2)
    v2 = rig.pos("HOLD", 2)
    assert v2["price_check"]["status"] == "usable" and v2["pnl"]["net_of_fees_usd"] == D("16.00")    # (0.50-0.34)*100
    assert "PROFIT_EXIT" not in [w["trigger"] for w in v2["warnings"]]


@pytest.mark.parametrize("name", ["bid0_ask_pos", "bid_pos_ask0", "negative_bid", "nan_ask", "crossed"])
def test_rv02_held_position_variants_suppress_numeric_advice(rig, name):
    rig.hold("HOLD", "40", "0.50")
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.13, ask=0.15), raw_row(strike=42.0, spot=51.0)], 0)
    rig.update([raw_row(strike=40.0, spot=52.0, **INVALID[name]), raw_row(strike=42.0, spot=52.0)], 1)
    v = rig.pos("HOLD", 1)
    assert v["pnl"] is None and "PROFIT_EXIT" not in [w["trigger"] for w in v["warnings"]]
    assert v["price_check"]["status"] == "unavailable"


def test_rv02_unaffected_held_contract_keeps_numeric_advice(rig):
    rig.hold("HOLD42", "42", "0.60")
    rig.update([raw_row(strike=40.0, spot=51.0), raw_row(strike=42.0, spot=51.0)], 0)
    rig.update([raw_row(strike=40.0, spot=52.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=52.0)], 1)
    v = rig.pos("HOLD42", 1)
    assert v["price_check"]["status"] == "usable" and v["pnl"]["net_of_fees_usd"] == D("6.00")   # (0.60-0.54)*100


def test_rv02_time_exit_and_data_warning_survive_invalid_quote(rig):
    # expiry today+15 d: DTE 15 <= close_review_at_dte_calendar 21 -> TIME_EXIT is a calendar signal, independent of price.
    rig.hold("T15", "40", "0.50", exp=TODAY + timedelta(days=15))
    ok = [raw_row(strike=40.0, spot=51.0, bid=0.45, ask=0.48, exp=EXP15), raw_row(strike=42.0, spot=51.0, exp=EXP15)]
    bad = [raw_row(strike=40.0, spot=52.0, bid=0.0, ask=0.0, exp=EXP15), raw_row(strike=42.0, spot=52.0, exp=EXP15)]
    rig.update(ok, 0)
    assert "TIME_EXIT" in [w["trigger"] for w in rig.pos("T15", 0)["warnings"]]
    rig.update(bad, 1)
    v = rig.pos("T15", 1)
    trig = [w["trigger"] for w in v["warnings"]]
    assert "TIME_EXIT" in trig, "calendar warning must persist"
    assert "PROFIT_EXIT" not in trig and v["pnl"] is None
    assert v["price_check"]["status"] == "unavailable" and v["price_check"]["reason"], "data warning is exposed"


def test_rv02_closed_position_behavior_unchanged(rig):
    rig.hold("CL", "40", "0.50")
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.13, ask=0.15), raw_row(strike=42.0, spot=51.0)], 0)
    rig.j.transition("CL", PositionState.CLOSE_WORKING)
    rig.j.transition("CL", PositionState.CLOSED, actual_close_debit_per_share=D("0.20"), closed_at_utc=rig.t(0), fees_usd=D("0"))
    before = rig.pos("CL", 0)
    rig.update([raw_row(strike=40.0, spot=52.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=52.0)], 1)
    after = rig.pos("CL", 1)
    for v in (before, after):
        assert v["warnings"] == [] and v["pnl"] is None
        assert v["price_check"] == {"status": "not applicable"}
        assert "Closed" in v["advice_note"]


# ------------------------------------------------------------------ probes: other readers of the stored row
def _retained_unusable_rig(rig):
    rig.update(two(), 0)
    rig.update([raw_row(strike=40.0, spot=48.0, bid=0.0, ask=0.0), raw_row(strike=42.0, spot=48.0)], 1)
    assert not rig.rows(1)["40"]["review"]["all_pass"]      # the market page already refuses it


def test_probe_trade_form_marks_retained_unusable_quote(rig):
    _retained_unusable_rig(rig)
    ctx = wt.contract_context(rig.j.conn, "AAA", TODAY + timedelta(days=40), D("40"))
    assert ctx is not None
    flagged = ctx.get("retained_unusable") or ctx.get("reference_unusable") or ctx.get("quote_not_current")
    assert flagged or ctx.get("reference_bid") is None


def test_probe_trade_assessment_does_not_treat_retained_quote_as_current(rig):
    _retained_unusable_rig(rig)
    row = wt._stored_contract(rig.j.conn, "AAA", TODAY + timedelta(days=40), D("40"))
    book = rig.j.load_book(rig.ap)
    a = wt._assess(book, rig.ap[0] if isinstance(rig.ap, list) else None, row, "AAA", TODAY + timedelta(days=40), D("40"),
                   "sec_AAA", "b_AAA", "ETF", False, rig.pol, rig.t(1))
    codes = set(a["flagged"]) | set(a["info"]) | set(a["context"])
    assert "MISSING_MARKET_DATA" in codes or any("QUOTE" in c or "STALE" in c for c in codes)


def test_probe_browse_observation_from_retained_row_is_not_current(rig):
    _retained_unusable_rig(rig)
    from csp_alerts.policy.browse import observation_from_row
    row = [r for r in obs.latest_options(rig.j.conn, "AAA") if r["strike"] == "40"][0]
    o = observation_from_row("AAA", row)
    assert o.bid is None and o.ask is None


def test_probe_contract_vanishing_from_successful_chain_is_reported(rig):
    """Not an invalid quote per the findings, but the old row must not look current: after strike 40 vanishes the row keeps its ORIGINAL
    time and must not claim a newer one; with the observation older than the policy quote-age limit it cannot pass."""
    rig.update(two(), 0)
    rig.update([raw_row(strike=42.0, spot=48.0)], 1)
    a = rig.rows(1)["40"]
    assert str(a["retrieved_at_utc"]).startswith("2026-10-30 15:05")
    assert (str(a["bid"]), str(a["ask"])) == ("0.5", "0.54")
    # far later (2h) with only strike 42 refreshed: the vanished row is old and must not pass
    rig.update([raw_row(strike=42.0, spot=48.0)], 24)
    rows = rig.rows(24)
    assert not rows["40"]["review"]["all_pass"], "a contract absent from the newest chain must not stay all_pass 2 hours later"
    assert rows["42"]["review"]["all_pass"]


def test_probe_vanished_contract_right_after_good_evidence_is_flagged_not_current(rig):
    rig.update(two(), 0)
    rig.update([raw_row(strike=42.0, spot=48.0)], 1)
    a = rig.rows(1)["40"]
    assert not a["review"]["all_pass"] or a.get("retained_unusable"), \
        "contract missing from the newest successful chain is presented as passing with no staleness/absence marker"


def test_probe_vanished_held_contract_gets_no_current_numeric_advice(rig):
    rig.hold("HOLD", "40", "0.50")
    rig.update([raw_row(strike=40.0, spot=51.0, bid=0.13, ask=0.15), raw_row(strike=42.0, spot=51.0)], 0)
    rig.update([raw_row(strike=42.0, spot=51.0)], 1)
    v = rig.pos("HOLD", 1)
    # The quote was observed at tick0 and still carries its original time; the dashboard must not present it as the newest evidence.
    ev = v["price_check"].get("evidence") or {}
    assert str(ev.get("retrieved_at_utc", "")).startswith("2026-10-30 15:05")


def test_no_averaging_or_row_selection_in_store(rig):
    rig.update(two(), 0)
    rig.update(two(49.0, 50.0), 1)
    hist = [r["price"] for r in rig.j.conn.execute("SELECT price FROM md_stock_history WHERE symbol='AAA'")]
    assert hist == ["48"], f"no 49 / 49.5 / 50 may be stored: {hist}"


def test_no_secret_or_order_text_in_failure_reason(rig):
    rig.update(two(), 0)
    rig.update(two(49.0, 50.0), 1)
    assert not re.search(r"(?i)token|bot\d+:|place order", str(rig.evidence()))
