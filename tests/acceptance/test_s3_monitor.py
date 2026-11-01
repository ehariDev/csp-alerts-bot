"""Slice 3 acceptance: open-position monitor (spec 8.1; MVP_SCOPE IT-005; FIRST_DELIVERY gate 5).
Frozen clock NOW=2026-10-30T15:05:02Z. Position: AAA 75 put, expiry 2026-12-04 (35 DTE), credit .50, fees .65, 1 contract.
Hand arithmetic for the profit rule: (0.50 - ask) / 0.50 >= 0.60  <=>  ask <= 0.20 (exact); loss: ask >= 2 x 0.50 = 1.00."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, TODAY, policy
from s2_helpers import Boom, FakeSource, policy_variant, raw_row
from s3_helpers import chain_rows_for, ledger, open_position

from csp_alerts.jobs.monitor import run_monitor
from csp_alerts.journal.models import PositionState as S
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import SourceError

POL = policy()
OPEN_SESSION = SessionInfo(SessionStatus.OPEN, 294)
CLOSED = SessionInfo(SessionStatus.CLOSED)
EXP = date(2026, 12, 4)
LABEL = "INDICATIVE - quote age UNKNOWN - verify at your broker"


def mon(j, rows, *, pol=POL, now=NOW, session=OPEN_SESSION, src=None):
    src = src or FakeSource({"AAA": rows} if not isinstance(rows, dict) else rows)
    return run_monitor(journal=j, policy=pol, source=src, now_utc=now, session=session), src


def trig(res):
    return {s.trigger.value for _, s in res.signals}


def msgs(res, kind):
    return [m for m in res.messages if m.kind == kind]


def one_position(**kw):
    j, _ = ledger([dict(trade_id="P1", ticker="AAA", strike="75.00", expiration=kw.pop("expiration", EXP), **kw)])
    return j


# ====================================================================== price triggers: IT-005 labelling and boundaries
@pytest.mark.parametrize("ask,fires", [(0.20, True), (0.21, False), (0.19, True), (0.30, False)])
def test_profit_exit_at_60_percent_boundary_with_fees(ask, fires):
    res, _ = mon(one_position(), chain_rows_for(75.0, EXP, ask - 0.02 if ask > 0.02 else 0.01, ask))
    assert ("PROFIT_EXIT" in trig(res)) is fires
    if fires:
        m = msgs(res, "PROFIT_EXIT")[0]
        gross = (D("0.50") - D(str(ask))) * 100          # (0.50 - 0.20) * 100 = 30.00 ; fees 0.65 -> net 29.35 at ask .20
        assert m.severity == "ACTION" and LABEL in m.text
        assert f"est gross P&L ${gross:,.0f}" in m.text or f"est gross P&L ${gross:,.2f}" in m.text
        if ask == 0.20:
            assert "net of recorded fees $29.35" in m.text and "Premium captured: 60%" in m.text
        assert "NO ORDER SENT" in m.text and "AAA 2026-12-04 PUT $75" in m.text and "35 DTE" in m.text


def test_price_alerts_are_labelled_indicative_never_live():
    res, _ = mon(one_position(), chain_rows_for(75.0, EXP, 0.95, 1.00, delta=-0.31))
    assert {"LOSS_REVIEW", "DELTA_REVIEW"} <= trig(res)
    for m in res.messages:
        if m.kind in ("PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW"):
            assert LABEL in m.text and "exchange quote time UNKNOWN" in m.text
            assert "LIVE" not in m.text.upper().replace("VERIFY AT YOUR BROKER", "")
            assert not __import__("re").search(r"real-?time|current quote|verified", m.text, __import__("re").I)


@pytest.mark.parametrize("ask,fires", [(1.00, True), (0.99, False), (1.01, True)])
def test_loss_review_exactly_2x_uses_ask_not_mid(ask, fires):
    res, _ = mon(one_position(), chain_rows_for(75.0, EXP, 0.90, ask))
    assert ("LOSS_REVIEW" in trig(res)) is fires
    if fires:
        m = msgs(res, "LOSS_REVIEW")[0]
        assert m.severity == "URGENT" and "2.00x" in m.text.replace("2.0x", "2.00x") or "x the opening credit" in m.text


@pytest.mark.parametrize("delta,fires", [(-0.30, True), (-0.2999, False), (0.30, True), (-0.45, True)])
def test_delta_review_boundary(delta, fires):
    res, _ = mon(one_position(), chain_rows_for(75.0, EXP, 0.28, 0.30, delta=delta))
    assert ("DELTA_REVIEW" in trig(res)) is fires


def test_midpoint_never_triggers_a_price_alert():
    # bid .05 / ask .25: mid .15 would be 70% captured, but the ASK is the buyback price -> 50% -> no PROFIT_EXIT
    res, _ = mon(one_position(), chain_rows_for(75.0, EXP, 0.05, 0.25))
    assert "PROFIT_EXIT" not in trig(res) and res.status == "NO_ALERTS"


def test_no_alert_when_nothing_triggers_and_status_is_explicit():
    res, _ = mon(one_position(), chain_rows_for(75.0, EXP, 0.28, 0.30))
    assert res.status == "NO_ALERTS" and res.positions_checked == 1 and res.messages == []


# ====================================================================== off switch
def test_tier_r_price_alert_switch_off_suppresses_price_alerts_only():
    pol = policy_variant(exits={"tier_r_price_alerts_enabled": False})
    src = FakeSource({"AAA": chain_rows_for(75.0, EXP, 0.10, 0.12, delta=-0.5)})
    res, _ = mon(one_position(expiration=TODAY + timedelta(days=21)), None, pol=pol, src=src)
    assert not ({"PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW"} & trig(res))
    assert "TIME_EXIT" in trig(res)                                       # calendar alerts continue
    assert src.calls == []                                               # and no quote is even fetched


def test_switch_off_does_not_spam_data_issue_for_every_position():
    """With price alerts switched OFF there is nothing to fetch, so 'no quote' is not a data problem."""
    pol = policy_variant(exits={"tier_r_price_alerts_enabled": False})
    res, _ = mon(one_position(), None, pol=pol, src=FakeSource({"AAA": []}))
    assert "DATA_ISSUE" not in trig(res), trig(res)


# ====================================================================== data problems -> DATA_ISSUE, never a numeric alert
@pytest.mark.parametrize("name,rows", [
    ("crossed", chain_rows_for(75.0, EXP, 0.30, 0.20)),                     # ask < bid
    ("zero_ask", chain_rows_for(75.0, EXP, 0.0, 0.0)),
    ("nan_ask", [raw_row(strike=75.0, expiration=EXP.isoformat(), bid=0.1, ask=float("nan"))]),
    ("missing_row", chain_rows_for(80.0, EXP, 0.10, 0.12)),                  # chain has other strikes only
    ("wrong_expiry", chain_rows_for(75.0, date(2026, 12, 11), 0.10, 0.12)),  # contract mismatch
    ("empty_chain", []),
])
def test_invalid_or_missing_quote_gives_data_issue_and_suppresses_price_triggers(name, rows):
    res, _ = mon(one_position(), rows)
    assert "DATA_ISSUE" in trig(res), name
    assert not ({"PROFIT_EXIT", "LOSS_REVIEW", "DELTA_REVIEW"} & trig(res))
    assert res.status == "DATA_UNAVAILABLE"
    assert msgs(res, "DATA_ISSUE")[0].severity == "WARN"


@pytest.mark.parametrize("exc", [SourceError("RATE_LIMIT", "429"), RuntimeError("boom"), ConnectionError("reset")])
def test_fetch_failure_gives_data_issue_not_silence_and_time_alerts_survive(exc):
    j = one_position(expiration=TODAY + timedelta(days=21))
    res, _ = mon(j, None, src=FakeSource({"AAA": exc}))
    assert {"DATA_ISSUE", "TIME_EXIT"} <= trig(res) and res.status == "ALERTS"
    assert res.issues and "AAA" in res.issues[0]


def test_fetch_failure_alone_is_data_unavailable_never_no_alerts():
    res, _ = mon(one_position(), None, src=FakeSource({"AAA": RuntimeError("boom")}))
    assert res.status == "DATA_UNAVAILABLE" and "DATA_ISSUE" in trig(res)


# ====================================================================== calendar triggers: no quote, market closed, weekend
@pytest.mark.parametrize("dte,time_exit,assign", [(22, False, False), (21, True, False), (8, True, False), (7, True, True), (1, True, True), (0, True, True)])
def test_calendar_triggers_fire_without_any_quote_when_market_closed(dte, time_exit, assign):
    j = one_position(expiration=TODAY + timedelta(days=dte))
    src = FakeSource({"AAA": RuntimeError("must not be called")})
    res, _ = mon(j, None, session=CLOSED, src=src)
    assert ("TIME_EXIT" in trig(res)) is time_exit and ("ASSIGNMENT_REVIEW" in trig(res)) is assign
    assert src.calls == [] and "DATA_ISSUE" not in trig(res)                  # closed market: no fetch, no DATA_ISSUE noise
    if time_exit:
        assert msgs(res, "TIME_EXIT")[0].severity == "ACTION" and "calendar/ledger based; no quote needed" in msgs(res, "TIME_EXIT")[0].text


def test_unknown_calendar_still_evaluates_calendar_alerts():
    j = one_position(expiration=TODAY + timedelta(days=5))
    res, _ = mon(j, None, session=SessionInfo(SessionStatus.UNKNOWN), src=FakeSource({"AAA": RuntimeError("no")}))
    assert {"TIME_EXIT", "ASSIGNMENT_REVIEW"} <= trig(res) and "calendar unavailable" in " ".join(res.notes)


def test_weekend_run_uses_real_calendar_and_still_alerts():
    sat = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    j, _ = ledger([dict(trade_id="P1", expiration=date(2026, 11, 20))], now=sat)        # 20 DTE on Saturday
    res = run_monitor(journal=j, policy=POL, source=FakeSource({"AAA": RuntimeError("no")}), now_utc=sat)
    assert "TIME_EXIT" in trig(res)


def test_itm_indicative_spot_raises_assignment_review_any_dte():
    res, _ = mon(one_position(), chain_rows_for(75.0, EXP, 2.0, 2.1, spot=74.0, delta=-0.5))
    assert "ASSIGNMENT_REVIEW" in trig(res)
    assert "in the money" in msgs(res, "ASSIGNMENT_REVIEW")[0].text


# ====================================================================== states: ADJUSTMENT_REVIEW, CLOSE_WORKING, closing stops alerts
def test_adjustment_review_gets_urgent_notice_plus_other_triggers():
    j, _ = ledger([dict(trade_id="P1", state=S.ADJUSTMENT_REVIEW, expiration=TODAY + timedelta(days=5))])
    res, _ = mon(j, chain_rows_for(75.0, TODAY + timedelta(days=5), 0.10, 0.12))
    t = trig(res)
    assert {"ADJUSTMENT_REVIEW", "TIME_EXIT", "ASSIGNMENT_REVIEW", "PROFIT_EXIT"} <= t
    m = [m for m in res.messages if "ADJUSTMENT REVIEW" in m.text][0]
    assert m.severity == "URGENT" and "non-standard" in m.text and "verify every number at your broker" in m.text.lower().replace("verify every number at your broker", "verify every number at your broker")


def test_close_working_keeps_alerting_until_the_closing_fill_is_recorded():
    j, clock = ledger([dict(trade_id="P1", state=S.CLOSE_WORKING)])
    res, _ = mon(j, chain_rows_for(75.0, EXP, 0.10, 0.12))
    assert "PROFIT_EXIT" in trig(res)
    j.transition("P1", S.CLOSED, actual_close_debit_per_share=D("0.12"), closed_at_utc=NOW, fees_usd=D("1.30"))
    src = FakeSource({"AAA": chain_rows_for(75.0, EXP, 0.10, 0.12)})
    res2, _ = mon(j, None, src=src)
    assert res2.status == "NO_POSITIONS" and res2.messages == [] and res2.positions_checked == 0 and src.calls == []


# SR-03 (OPEN_ISSUES): an assignment awaiting reconciliation is an unresolved obligation, not a terminal state; it is
# covered by test_sr03_* in test_sr_fixes.py. ASSIGNED was removed from this "no alert" list for that reason only.
@pytest.mark.parametrize("terminal", ["CLOSED", "CANCELED", "EXPIRED"])
def test_terminal_and_unfilled_states_get_no_open_position_alerts(terminal):
    j, clock = ledger([dict(trade_id="P1", expiration=date(2026, 11, 2), state=S.OPEN)] if terminal in ("EXPIRED", "ASSIGNED")
                      else [dict(trade_id="P1", state=S.PENDING)] if terminal == "CANCELED" else [dict(trade_id="P1")])
    if terminal == "CLOSED":
        j.transition("P1", S.CLOSE_WORKING)
        j.transition("P1", S.CLOSED, actual_close_debit_per_share=D("0.1"), closed_at_utc=NOW, fees_usd=D("0"))
    elif terminal == "CANCELED":
        j.transition("P1", S.CANCELED, confirmed=True)
    elif terminal == "EXPIRED":
        j.transition("P1", S.EXPIRED, confirmed=True, closed_at_utc=datetime(2026, 11, 2, 21, tzinfo=timezone.utc))
    else:
        j.transition("P1", S.ASSIGNED_PENDING_RECONCILIATION)
    res, _ = mon(j, [], session=CLOSED)
    assert res.messages == [] and res.status == "NO_POSITIONS"


def test_pending_and_working_orders_are_not_monitored_as_open_risk():
    j, _ = ledger([dict(trade_id="P1", state=S.PENDING, expiration=TODAY + timedelta(days=3))])
    res, _ = mon(j, [], session=CLOSED)
    assert res.positions_checked == 0 and res.messages == []


# ====================================================================== frozen-clock regressions
def test_a_quote_fetched_after_the_job_started_is_not_stale_or_from_the_future():
    """Regression: the fetch happens AFTER now_utc. A fresh quote (retrieved now + 5 s) must still produce price alerts."""
    j = one_position()
    src = FakeSource({"AAA": chain_rows_for(75.0, EXP, 0.10, 0.12)}, now=NOW + timedelta(seconds=5))
    res, _ = mon(j, None, src=src)
    assert "PROFIT_EXIT" in trig(res) and "DATA_ISSUE" not in trig(res)
    # and a slow fetch (almost 2 minutes after job start) is still fresh
    src = FakeSource({"AAA": chain_rows_for(75.0, EXP, 0.10, 0.12)}, now=NOW + timedelta(seconds=110))
    res, _ = mon(one_position(), None, src=src)
    assert "PROFIT_EXIT" in trig(res) and "DATA_ISSUE" not in trig(res)


def test_a_quote_retrieved_long_before_the_job_is_stale_and_suppresses_price_alerts():
    for age_s, stale in ((1800, False), (1801, True)):
        src = FakeSource({"AAA": chain_rows_for(75.0, EXP, 0.10, 0.12)}, now=NOW - timedelta(seconds=age_s))
        res, _ = mon(one_position(), None, src=src)
        assert (("DATA_ISSUE" in trig(res)) is stale) and (("PROFIT_EXIT" in trig(res)) is not stale), age_s


def test_dte_in_alert_uses_new_york_date_at_the_dst_boundary():
    # 2026-11-02T04:30Z = 23:30 EST Sun Nov 1 -> exchange date Nov 1; expiry Nov 22 = 21 DTE (TIME_EXIT), UTC date would say 20
    now = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)
    j, _ = ledger([dict(trade_id="P1", expiration=date(2026, 11, 22))], now=now)
    res = run_monitor(journal=j, policy=POL, source=FakeSource({"AAA": []}), now_utc=now, session=CLOSED)
    assert "21 DTE" in msgs(res, "TIME_EXIT")[0].text and "20 DTE" not in msgs(res, "TIME_EXIT")[0].text
    j2, _ = ledger([dict(trade_id="P1", expiration=date(2026, 11, 23))], now=now)
    assert "TIME_EXIT" not in trig(run_monitor(journal=j2, policy=POL, source=FakeSource({"AAA": []}), now_utc=now, session=CLOSED))


# ====================================================================== dedup keys, ordering, hostile text
def test_exit_dedup_keys_are_per_trade_trigger_day_and_account_scope():
    j = one_position(expiration=TODAY + timedelta(days=5))
    r1, _ = mon(j, [], session=CLOSED)
    r2, _ = mon(j, [], session=CLOSED, now=NOW + timedelta(minutes=15))
    r3, _ = mon(j, [], session=CLOSED, now=NOW + timedelta(days=1))
    k = lambda r: sorted(m.dedup_key for m in r.messages)  # noqa: E731
    assert k(r1) == k(r2) and k(r1) != k(r3)
    assert all(x.startswith("EXIT|paper_primary|P1|") for x in k(r1))
    assert any("|TIME_EXIT|2026-10-30" in x for x in k(r1)) and any("|ASSIGNMENT_REVIEW|2026-10-30" in x for x in k(r1))


def test_messages_sorted_most_severe_first():
    j = one_position(expiration=TODAY + timedelta(days=5))
    res, _ = mon(j, chain_rows_for(75.0, TODAY + timedelta(days=5), 0.10, 0.12, delta=-0.5))
    order = {"CRITICAL": 0, "URGENT": 1, "ACTION": 2, "WARN": 3, "INFO": 4}
    ranks = [order[m.severity] for m in res.messages]
    assert ranks == sorted(ranks)


def test_hostile_trade_id_and_ticker_text_is_escaped_in_exit_alerts():
    j, clock = ledger([])
    clock.t = NOW - timedelta(hours=5)
    from csp_alerts.journal.store import NewPosition
    hostile = "<script>alert(1)</script>\n/start /place_order"
    j.create_draft(NewPosition(hostile, "AAA", TODAY + timedelta(days=5), D("75.00"), 100, 1, "s", "b"))
    j.transition(hostile, S.PENDING)
    j.transition(hostile, S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.5"), opened_at_utc=clock.t)
    from csp_alerts.journal.models import AccountSnapshot
    clock.t = NOW - timedelta(hours=1)
    j.add_snapshot(AccountSnapshot("sx", clock.t, D("100000"), D("60000"), True, "v"))
    res, _ = mon(j, [], session=CLOSED)
    for m in res.messages:
        body = m.text.replace("<b>", "").replace("</b>", "")
        assert "<" not in body and ">" not in body and "\r" not in m.text
        assert len(m.text) < 4096 and "NO ORDER SENT" in m.text


def test_exit_alerts_show_snapshot_time_and_next_action():
    res, _ = mon(one_position(expiration=TODAY + timedelta(days=20)), [], session=CLOSED)
    t = msgs(res, "TIME_EXIT")[0].text
    assert "Account snapshot used: 8:05 AM CDT" in t and "Next manual action:" in t and "20 DTE" in t
