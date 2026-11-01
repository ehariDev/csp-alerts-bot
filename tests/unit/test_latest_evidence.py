"""RV-20261031-01/02: good -> invalid -> recovered sequences for the stock and per exact contract. Synthetic data only."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from chain_fixtures import raw_row
from dash_fixture import approved_file
from factories import NOW, policy, snap
from market_fixtures import FakeProvider

from csp_alerts.jobs.market_refresh import run_market_refresh
from csp_alerts.journal import watchlist
from csp_alerts.journal.models import PositionState
from csp_alerts.journal.store import Journal, NewPosition
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.policy.approved import load_approved
from csp_dashboard import views
from csp_dashboard.readonly import ReadOnlyJournal


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
        watchlist.add_symbol(self.j, "AAA", actor="t", source="synthetic")
        self.p = FakeProvider()

    def update(self, rows, minutes):
        t = NOW + timedelta(minutes=minutes)
        self.p.fetched_at = t
        self.p.set("AAA", rows=rows)
        run_market_refresh(journal=self.j, policy=self.pol, source=self.p, now_utc=t, session=SessionInfo(SessionStatus.OPEN, 300),
                           sleep=lambda _: None, pause_seconds=0)

    def update_failed(self, minutes):
        t = NOW + timedelta(minutes=minutes)
        self.p.fetched_at = t
        run_market_refresh(journal=self.j, policy=self.pol, source=self.p, now_utc=t, session=SessionInfo(SessionStatus.OPEN, 300),
                           sleep=lambda _: None, pause_seconds=0)

    def env(self, rj, minutes):
        return views.Env(rj=rj, now=NOW + timedelta(minutes=minutes), tz="America/Chicago", policy=self.pol, policy_error=None,
                         approved=self.ap, approved_error=None)

    def detail(self, minutes):
        rj = ReadOnlyJournal(self.db)
        with rj.snapshot():
            d = views.market_detail(self.env(rj, minutes), "AAA")
        rj.close()
        return d

    def position(self, minutes):
        rj = ReadOnlyJournal(self.db)
        with rj.snapshot():
            pv = views.position_view(self.env(rj, minutes), rj.get_position("HOLD"))
        rj.close()
        return pv


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.j.close()


def by_strike(d):
    return {x["strike"]: x for x in d["rows"]}


def test_stock_conflict_blocks_candidates_then_recovers(rig):
    rig.update([raw_row(strike=40., spot=48.), raw_row(strike=42., spot=48.)], 0)
    a = rig.detail(0)
    assert all(x["review"]["all_pass"] for x in a["rows"]) and a["block"]["stock_failure"] is None
    rig.update([raw_row(strike=40., spot=49.), raw_row(strike=42., spot=50.)], 5)
    b = rig.detail(5)
    blk = b["block"]
    assert blk["stock"]["price"] == "48" and blk["stock"]["retrieved_at_utc"] == NOW           # prior value, TRUE time
    assert blk["stock_failure"] is not None and blk["state"]["primary"] == "STALE"
    assert not any(x["review"]["all_pass"] for x in b["rows"])
    assert all(x["gate"]["marker"] == "NOT_EVALUATED" for x in b["rows"])
    assert {x["bid"] for x in b["rows"]} == {"0.5"} and all(x["retrieved_at_utc"] == NOW + timedelta(minutes=5) for x in b["rows"])
    assert {x["strike"] for x in b["rows"]} == {"40", "42"}                                        # put rows still displayed
    rig.update([raw_row(strike=40., spot=51.), raw_row(strike=42., spot=51.)], 10)
    c = rig.detail(10)
    assert c["block"]["stock_failure"] is None and c["block"]["stock"]["price"] == "51"
    assert all(x["review"]["all_pass"] for x in c["rows"])


def test_contract_invalid_quote_is_retained_unusable_and_unaffected_contract_stays(rig):
    rig.update([raw_row(strike=40., spot=48.), raw_row(strike=42., spot=48.)], 0)
    rig.update([raw_row(strike=40., spot=48., bid=0., ask=0.), raw_row(strike=42., spot=48., bid=.5, ask=.54)], 5)
    d = by_strike(rig.detail(5))
    assert d["40"]["retrieved_at_utc"] == NOW and d["40"]["bid"] == "0.5"                          # original time + value kept
    assert d["40"]["retained_unusable"] and not d["40"]["review"]["all_pass"] and d["40"]["gate"]["marker"] == "NOT_EVALUATED"
    assert d["42"]["review"]["all_pass"] and "retained_unusable" not in d["42"]                      # unaffected control
    rig.update([raw_row(strike=40., spot=48.), raw_row(strike=42., spot=48.)], 10)
    d = by_strike(rig.detail(10))
    assert d["40"]["review"]["all_pass"] and "retained_unusable" not in d["40"]


def hold(rig):
    rig.j.create_draft(NewPosition("HOLD", "AAA", NOW.date() + timedelta(days=40), D("40"), 100, 1, "sec_aaa", "b_aaa"))
    rig.j.transition("HOLD", PositionState.PENDING)
    rig.j.transition("HOLD", PositionState.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.50"), opened_at_utc=NOW,
                     fees_usd=D("0"))


def test_held_contract_invalid_quote_gives_no_numeric_advice_then_recovers(rig):
    hold(rig)
    rig.update([raw_row(strike=40., spot=51., bid=.13, ask=.15), raw_row(strike=42., spot=51.)], 0)
    ok = rig.position(1)
    assert ok["price_check"]["status"] == "usable" and any(w["trigger"] == "PROFIT_EXIT" for w in ok["warnings"])
    rig.update([raw_row(strike=40., spot=52., bid=0., ask=0.), raw_row(strike=42., spot=52.)], 5)
    bad = rig.position(5)
    assert bad["price_check"]["status"] == "unavailable" and bad["pnl"] is None
    assert not any(w["trigger"] == "PROFIT_EXIT" for w in bad["warnings"])
    assert "not current" in bad["price_check"]["reason"] and bad["price_check"]["evidence"]["retained_unusable"]
    assert bad["price_check"]["evidence"]["retrieved_at_utc"] == NOW                                # original timestamp kept
    rig.update([raw_row(strike=40., spot=53., bid=.30, ask=.34), raw_row(strike=42., spot=53.)], 10)
    rec = rig.position(10)
    assert rec["price_check"]["status"] == "usable" and rec["pnl"]["net_of_fees_usd"] == D("16.00")


def test_failure_survives_status_upsert_but_not_valid_recovery(rig):
    from csp_alerts.market import observations as obs
    rig.update([raw_row(strike=40., spot=48.)], 0)
    rig.update([raw_row(strike=40., spot=48., bid=0., ask=0.)], 5)
    assert list(obs.latest_evidence(rig.j.conn, "AAA")["contracts"]) and obs.get_status(rig.j.conn, "AAA")["status"]
    rig.update([raw_row(strike=40., spot=48.)], 10)
    assert "contracts" not in obs.latest_evidence(rig.j.conn, "AAA")


def test_vanished_contract_is_marked_absent_then_clears_on_reappearance(rig):
    rig.update([raw_row(strike=40., spot=48.), raw_row(strike=42., spot=48.)], 0)
    rig.update([raw_row(strike=42., spot=48.)], 5)
    d = by_strike(rig.detail(5))
    assert d["40"]["retrieved_at_utc"] == NOW and d["40"]["retained_unusable"] and not d["40"]["review"]["all_pass"]
    assert d["42"]["review"]["all_pass"]
    rig.update([raw_row(strike=40., spot=48.), raw_row(strike=42., spot=48.)], 10)
    d = by_strike(rig.detail(10))
    assert d["40"]["review"]["all_pass"] and "retained_unusable" not in d["40"]


def test_browse_observation_and_trade_context_honour_the_mark(rig):
    from csp_alerts.journal import webtrade as wt
    from csp_alerts.market import observations as obs
    from csp_alerts.policy.browse import observation_from_row
    rig.update([raw_row(strike=40., spot=48.)], 0)
    rig.update([raw_row(strike=40., spot=48., bid=0., ask=0.)], 5)
    row = obs.latest_options(rig.j.conn, "AAA")[0]
    o = observation_from_row("AAA", row)
    assert o.bid is None and o.ask is None and o.retrieved_at_utc == NOW
    ctx = wt.contract_context(rig.j.conn, "AAA", row and __import__("datetime").date.fromisoformat(row["expiration"]), D("40"))
    assert ctx["retained_unusable"] and str(ctx["reference_retrieved_at_utc"]).startswith("2026-10-30T15:05")


# ---- RV-20261031-03 (whole-fetch failures) and -04 (ordering)
def _ok_held(rig, m):
    pv = rig.position(m)
    return pv["price_check"]["status"] == "usable" and any(w["trigger"] == "PROFIT_EXIT" for w in pv["warnings"])


GOOD = lambda: [raw_row(strike=40., spot=48., bid=.13, ask=.15), raw_row(strike=42., spot=48.)]


def test_provider_outage_makes_held_quote_history_then_recovers(rig):
    hold(rig)
    rig.update(GOOD(), 0)
    assert _ok_held(rig, 1)
    rig.p.fail("AAA")
    rig.update_failed(5)
    pv = rig.position(6)
    assert pv["price_check"]["status"] == "unavailable" and pv["pnl"] is None
    assert not any(w["trigger"] in ("PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW") for w in pv["warnings"])
    assert "not current" in pv["price_check"]["reason"] and pv["price_check"]["evidence"]["retrieved_at_utc"] == NOW
    rig.update(GOOD(), 10)
    assert _ok_held(rig, 11)


def test_empty_chain_makes_held_quote_history(rig):
    hold(rig)
    rig.update(GOOD(), 0)
    rig.update([], 5)
    pv = rig.position(6)
    assert pv["price_check"]["status"] == "unavailable" and pv["pnl"] is None


def _store(rig, rows, at, source="MONITOR"):
    from csp_alerts.market import observations as obs
    from csp_alerts.market.normalize import normalize_rows
    from factories import TODAY
    norm = normalize_rows("AAA", rows, at, today_exchange=TODAY, earnings_dates=None, multiplier=None)
    return obs.store_chain(rig.j, symbol="AAA", run_id="r-" + str(at), source=source, retrieved_at=at, norm=norm)


def test_delayed_older_write_neither_replaces_rows_nor_clears_failure(rig):
    hold(rig)
    rig.update(GOOD(), 0)
    rig.update([raw_row(strike=40., spot=49., bid=0., ask=0.), raw_row(strike=42., spot=49.)], 5)
    _store(rig, GOOD(), NOW + timedelta(minutes=1))                    # delayed writer that fetched earlier
    pv = rig.position(6)
    assert pv["price_check"]["status"] == "unavailable" and pv["pnl"] is None
    d = rig.detail(6)
    assert d["block"]["stock"]["price"] == "49" and d["block"]["stock"]["retrieved_at_utc"] == NOW + timedelta(minutes=5)
    r = by_strike(d)
    assert r["42"]["retrieved_at_utc"] == NOW + timedelta(minutes=5) and r["40"]["retrieved_at_utc"] == NOW
    rig.update(GOOD(), 10)                                             # chronological recovery still works
    assert _ok_held(rig, 11)


def test_failure_in_same_stored_second_invalidates_prior_quote(rig):
    hold(rig)
    rig.update(GOOD(), 0)
    _store(rig, [raw_row(strike=40., spot=48., bid=0., ask=0.), raw_row(strike=42., spot=48.)], NOW + timedelta(microseconds=500000))
    pv = rig.position(1)
    assert pv["price_check"]["status"] == "unavailable" and pv["pnl"] is None
    r = by_strike(rig.detail(1))
    assert not r["40"]["review"]["all_pass"] and "retained_unusable" not in r["42"] and r["42"]["review"]["evaluated"]   # unaffected contract stays usable


# ---- RV-ACC-06/07/08
def test_older_touch_success_never_regresses_last_success_or_status(rig):
    from csp_alerts.market import observations as obs
    rig.update(GOOD(), 0)
    rig.update(GOOD(), 6)
    before = dict(obs.get_status(rig.j.conn, "AAA"))
    assert before["last_success_at_utc"] == "2026-10-30T15:11:00Z"
    obs.touch_success(rig.j, "AAA", NOW + timedelta(minutes=3), "late")
    assert dict(obs.get_status(rig.j.conn, "AAA")) == before


def test_touch_success_older_than_failed_attempt_keeps_failure_state(rig):
    from csp_alerts.market import observations as obs
    rig.update(GOOD(), 0)
    rig.p.fail("AAA")
    rig.update_failed(5)
    st = dict(obs.get_status(rig.j.conn, "AAA"))
    obs.touch_success(rig.j, "AAA", NOW + timedelta(minutes=2), "late")       # older than the failed attempt
    after = dict(obs.get_status(rig.j.conn, "AAA"))
    assert after["status"] == st["status"] == "ERROR" and after["next_attempt_after_utc"] == st["next_attempt_after_utc"]
    assert after["consecutive_failures"] == st["consecutive_failures"]


def test_monitor_fetch_error_marks_held_quote_not_current_and_keeps_refresh_backoff(rig):
    from csp_alerts.jobs.monitor import run_monitor
    from csp_alerts.market import observations as obs
    hold(rig)
    rig.update(GOOD(), 0)
    before = dict(obs.get_status(rig.j.conn, "AAA"))
    rig.p.fail("AAA")
    run_monitor(journal=rig.j, policy=rig.pol, source=rig.p, now_utc=NOW + timedelta(minutes=1),
                session=SessionInfo(SessionStatus.OPEN, 300))
    pv = rig.position(2)
    assert pv["price_check"]["status"] == "unavailable" and pv["pnl"] is None
    after = dict(obs.get_status(rig.j.conn, "AAA"))
    for k in ("last_attempt_at_utc", "consecutive_failures", "next_attempt_after_utc", "status"):
        assert after[k] == before[k]
    rig.update(GOOD(), 10)                                                    # newer valid evidence recovers
    assert _ok_held(rig, 11)


def test_subsecond_older_failure_does_not_invalidate_newer_quote_but_later_one_does(rig):
    hold(rig)
    rig.update(GOOD(), 0)
    bad = [raw_row(strike=40., spot=48., bid=0., ask=0.), raw_row(strike=42., spot=48.)]
    good2 = [raw_row(strike=40., spot=48., bid=.30, ask=.34), raw_row(strike=42., spot=48.)]
    _store(rig, good2, NOW + timedelta(microseconds=900000))
    _store(rig, bad, NOW + timedelta(microseconds=500000))                    # older failure arrives late
    assert rig.position(1)["price_check"]["status"] == "usable"
    _store(rig, bad, NOW + timedelta(microseconds=950000))                    # genuinely later failure
    assert rig.position(1)["price_check"]["status"] == "unavailable"


def test_run_at_eviction_keeps_newest_runs(rig):
    from csp_alerts.market import observations as obs
    rig.update(GOOD(), 0)
    for i in range(60):                       # ids sort opposite to age: newest ids are alphabetically first
        _store_id(rig, GOOD(), NOW + timedelta(seconds=i + 1), f"z{99 - i:02d}")
    ra = obs._runat_load(rig.j.conn, "AAA")
    assert len(ra) == 50 and "z40" in ra and "z99" not in ra


def _store_id(rig, rows, at, rid):
    from csp_alerts.market import observations as obs
    from csp_alerts.market.normalize import normalize_rows
    from factories import TODAY
    norm = normalize_rows("AAA", rows, at, today_exchange=TODAY, earnings_dates=None, multiplier=None)
    obs.store_chain(rig.j, symbol="AAA", run_id=rid, source="MONITOR", retrieved_at=at, norm=norm)


def test_subsecond_fetch_mark_older_than_quote_does_not_invalidate_but_later_does(rig):
    from csp_alerts.market import observations as obs
    hold(rig)
    rig.update(GOOD(), 0)
    _store_id(rig, [raw_row(strike=40., spot=48., bid=.30, ask=.34), raw_row(strike=42., spot=48.)],
              NOW + timedelta(microseconds=900000), "q900")
    obs.record_monitor_fetch_failure(rig.j, "AAA", NOW + timedelta(microseconds=500000), "FETCH_ERROR")
    assert rig.position(1)["price_check"]["status"] == "usable"
    obs.record_monitor_fetch_failure(rig.j, "AAA", NOW + timedelta(microseconds=950000), "FETCH_ERROR")
    assert rig.position(1)["price_check"]["status"] == "unavailable"
