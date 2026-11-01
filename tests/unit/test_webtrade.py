"""Stage B1 domain: plan (preview) and record (confirm) of owner-reported trades. Synthetic data, no network.

Covers WT-AT-03/04: sold put -> preview -> confirm -> OPEN; WORKING -> filled; close; cancel; expiry; duplicate and stale
protection; policy breaches recorded with flags; quantity != 1; the fill price comes from the owner, never from the bid;
web results equal the CLI results for the same inputs."""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
from dash_fixture import POLICY, approved_file, build
from factories import NOW, TODAY, policy
from market_fixtures import FakeProvider

from csp_alerts import ledger_cli
from csp_alerts.jobs.market_refresh import run_market_refresh
from csp_alerts.journal import watchlist, webtrade as wt
from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.policy.approved import load_approved

POL = policy()
TZ = "America/Chicago"
OPEN = SessionInfo(SessionStatus.OPEN, 300)
EXP = (TODAY + timedelta(days=40)).isoformat()
SECRET = "SECRET-NOTE-123"


def ik(tag: str) -> str:
    """A valid 32-hex idempotency key derived from a short tag (tests only)."""
    import hashlib
    return hashlib.md5(tag.encode()).hexdigest()


@pytest.fixture
def env(tmp_path):
    paths = build(tmp_path, scan=False, evil_notes=False)
    j = Journal(paths.db, clock=lambda: NOW)
    for s in ("AAA", "ZZZ"):
        watchlist.add_symbol(j, s, actor="t", source="t")
    src = FakeProvider()
    src.set("AAA", spot=48.0, strikes=(40.0, 42.0))
    src.set("ZZZ", spot=48.0, strikes=(40.0,))
    run_market_refresh(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN, sleep=lambda s: None, pause_seconds=0)
    j.close()
    paths.approved_list = load_approved(paths.approved)       # AAA approved (ETF), ZZZ not approved
    return paths


def jw(env):
    return Journal(env.db, clock=lambda: NOW)


def sell(idem="a" * 32, **over):
    f = {"action": wt.SELL_FILLED, "idem": idem, "symbol": "AAA", "expiry": EXP, "strike": "40", "contracts": "1",
         "credit": "0.45", "fees": "0.65", "executed_date": "2026-10-30", "executed_time": "09:30"}
    f.update(over)
    return f


def plan(env, form, now=NOW):
    j = jw(env)
    try:
        return wt.build_plan(j, POL, env.approved_list, form, now, TZ)
    finally:
        j.close()


def rec(env, form, *, reason="paper fill reported", acks=(), now=NOW, bound=None):
    j = jw(env)
    try:
        p = wt.build_plan(j, POL, env.approved_list, form, now, TZ)
        rev, sha = (p.ledger_revision, p.policy_sha) if bound is None else bound
        return wt.record(j, POL, env.approved_list, form, now=now, tz=TZ, bound_revision=rev, bound_policy_sha=sha,
                         reason=reason, acks=frozenset(acks))
    finally:
        j.close()


def pos(env, tid):
    j = jw(env)
    try:
        return j.get_position(tid)
    finally:
        j.close()


def audit(env, etype=None):
    j = jw(env)
    try:
        return j.audit_events(etype)
    finally:
        j.close()


def snapshot_ledger(env):
    j = jw(env)
    try:
        return (j.conn.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0],
                j.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
                j.conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0])
    finally:
        j.close()


# ---------------------------------------------------------------- preview
def test_preview_writes_nothing_and_describes_the_exact_transition_and_collateral(env):
    before = snapshot_ledger(env)
    p = plan(env, sell())
    assert p.ok and p.trade_id == "WEB-AAA-" + EXP.replace("-", "") + "-aaaaaaaa"
    assert "OPEN" in p.state_change and p.collateral_usd == D("4000")                      # strike x 100 x 1
    assert p.contract["occ_symbol"].startswith("AAA") and p.contract["multiplier"] == 100 and p.contract["contracts"] == 1
    rows = {r.key: r for r in p.caps}
    assert rows["pilot"].used_before == D("7000") and rows["pilot"].used_after == D("11000") and rows["pilot"].room_after == D("4000")
    assert rows["pilot"].limit == D("15000") and not rows["pilot"].breached
    assert p.breach_codes == [] and p.checks == []
    assert snapshot_ledger(env) == before


def test_reference_bid_is_shown_but_never_used_as_the_fill(env):
    p = plan(env, sell(credit="0.31"))
    assert p.contract["reference_bid"] is not None and p.contract["reference_bid"] != "0.31"
    r = rec(env, sell(credit="0.31"))
    assert r.code == "recorded" and pos(env, r.trade_id).actual_open_credit_per_share == D("0.31")
    # no credit entered -> refused, the bid is never substituted
    p2 = plan(env, sell(idem="b" * 32, credit=""))
    assert not p2.ok and any("credit" in e.lower() for e in p2.errors)


@pytest.mark.parametrize("over,needle", [
    ({"contracts": "2"}, "exactly 1 contract"), ({"contracts": "0"}, "exactly 1 contract"), ({"contracts": "1.5"}, "exactly 1 contract"),
    ({"credit": "0"}, "greater than zero"), ({"credit": "-1"}, "plain number"), ({"credit": "0.4x"}, "plain number"),
    ({"credit": "41"}, "cannot exceed the strike"), ({"fees": "-1"}, "plain number"), ({"executed_time": "25:00"}, "never defaulted"),
    ({"executed_date": ""}, "never defaulted"), ({"executed_date": "2026-11-30"}, "future"),
    ({"strike": "41"}, "not in the stored chain"), ({"expiry": "2020-01-01"}, "expired"), ({"symbol": "a b"}, "not a valid symbol"),
    ({"idem": "x"}, "idempotency"),
])
def test_validation_errors_are_fixed_phrases_and_block_recording(env, over, needle):
    before = snapshot_ledger(env)
    p = plan(env, sell(**over))
    assert not p.ok and any(needle in e for e in p.errors), p.errors
    assert rec(env, sell(**over)).code == "invalid"
    assert snapshot_ledger(env) == before


def test_quantity_not_one_message_points_to_the_cli_procedure(env):
    p = plan(env, sell(contracts="3"))
    assert any("documented CLI procedure" in e for e in p.errors)


# ---------------------------------------------------------------- confirm
def test_confirm_sold_put_becomes_open_with_owner_numbers_audit_and_actor(env):
    r = rec(env, sell(credit="0.45", fees="0.65"))
    assert r.code == "recorded" and r.state == "OPEN" and r.commitment_changed and not r.breach_codes
    p = pos(env, r.trade_id)
    assert p.state is S.OPEN and p.actual_open_credit_per_share == D("0.45") and p.fees_usd == D("0.65")
    assert p.strike == D("40") and p.multiplier == 100 and p.contracts == 1 and p.account_scope == "paper_primary" and p.paper
    assert p.opened_at_utc.isoformat() == "2026-10-30T14:30:00+00:00"           # 09:30 Central -> UTC
    assert p.collateral_usd == D("4000")
    j = jw(env)
    raw = j.conn.execute("SELECT strike, actual_open_credit_per_share, fees_usd FROM option_positions WHERE trade_id=?", (r.trade_id,)).fetchone()
    j.close()
    assert tuple(raw) == ("40", "0.45", "0.65")                                   # Decimal strings
    ev = audit(env)
    mine = [e for e in ev if e["actor"] == "web:owner"]
    assert {e["event_type"] for e in mine} >= {"POSITION_DRAFT_CREATED", "POSITION_TRANSITION", "MANUAL_COMMAND", "WEB_TRADE_RECORDED"}
    assert all(e["source"] == "web" for e in mine)


def test_web_and_cli_produce_the_same_position(env, tmp_path):
    """Parity: the same owner inputs through the web path and through `journal add-trade` + `journal fill` store the same row."""
    r = rec(env, sell(credit="0.45", fees="0.65"))
    j = jw(env)
    a = next(p for p in j.load_book(env.approved_list).positions if p.trade_id == r.trade_id)
    ns = dict(apply=True, actor="owner", reason="parity", trade_id="CLI-1", ticker="AAA", expiration=EXP, strike="40", contracts="1",
              multiplier="100", sector="sec_AAA", bucket="b_AAA", state="PENDING", notes="")
    assert ledger_cli.execute("add-trade", argparse.Namespace(**ns), j, POL, env.approved, NOW) == 0
    ns2 = dict(apply=True, actor="owner", reason="parity", trade_id="CLI-1", credit="0.45", fees="0.65", time="2026-10-30T14:30:00Z")
    assert ledger_cli.execute("fill", argparse.Namespace(**ns2), j, POL, env.approved, NOW) == 0
    b = next(p for p in j.load_book(env.approved_list).positions if p.trade_id == "CLI-1")     # approvals file as asset-type fallback
    j.close()
    keys = ("state", "ticker", "expiration", "strike", "multiplier", "contracts", "sector", "correlation_bucket", "account_scope",
            "paper", "opened_at_utc", "actual_open_credit_per_share", "fees_usd", "collateral_usd", "asset_type")
    assert {k: getattr(a, k) for k in keys} == {k: getattr(b, k) for k in keys}


def test_checkin_web_and_cli_produce_the_same_snapshot_and_cash_rule(env):
    f = {"action": wt.CHECKIN, "idem": "c" * 32, "equity": "100000", "free_cash": "60000", "nlots": "1", "lot_0": "L-1", "mark_0": "11.5"}
    p = plan(env, f)
    assert p.ok and p.checks[0].name == "ack_reconciled"
    r = rec(env, f, acks=("ack_reconciled",), reason="eod")
    assert r.code == "recorded"
    j = jw(env)
    s = j.list_snapshots()[-1]
    lot = [x for x in j.list_stock_lots() if x.lot_id == "L-1"][0]
    j.close()
    assert (s.equity_usd, s.free_unencumbered_cash_usd, s.manual_confirmed, s.reconciliation_version) == (D("100000"), D("60000"), True, "daily-check-in")
    assert lot.reference_price == D("11.5")
    # same cash rule as the CLI: free cash + collateral > equity is CASH_INCONSISTENT
    bad = plan(env, {**f, "idem": "d" * 32, "free_cash": "99000"})
    assert not bad.ok and any("CASH_INCONSISTENT" in e for e in bad.errors)


def test_confirm_needs_reason_and_ack_and_nothing_is_written_without_them(env):
    before = snapshot_ledger(env)
    assert rec(env, sell(), reason="  ").code == "reason_required"
    assert snapshot_ledger(env) == before
    f = {"action": wt.CHECKIN, "idem": "e" * 32, "equity": "100000", "free_cash": "60000", "nlots": "0"}
    assert rec(env, f, acks=()).code == "ack_required" and snapshot_ledger(env) == before


def test_duplicate_submit_and_double_click_create_one_economic_event(env):
    f = sell()
    assert rec(env, f).code == "recorded"
    n = snapshot_ledger(env)
    again = rec(env, f)                                   # reload / second click: same idempotency key
    assert again.code == "duplicate" and again.trade_id == "WEB-AAA-" + EXP.replace("-", "") + "-aaaaaaaa"
    assert snapshot_ledger(env) == n
    assert len([e for e in audit(env, "WEB_TRADE_RECORDED")]) == 1
    # the duplicate is recognised even when the ledger moved on in between
    assert rec(env, sell(idem="f" * 32, strike="42"), acks=("ack_breach",)).code == "recorded"   # 2nd put on AAA: flagged
    assert rec(env, f).code == "duplicate"


def test_stale_preview_is_rejected_when_the_ledger_changed(env):
    j = jw(env)
    p = wt.build_plan(j, POL, env.approved_list, sell(), NOW, TZ)
    j.close()
    assert rec(env, sell(idem="1" * 32, strike="42"), reason="other").code == "recorded"      # the ledger moves
    before = snapshot_ledger(env)
    j = jw(env)
    r = wt.record(j, POL, env.approved_list, sell(), now=NOW, tz=TZ, bound_revision=p.ledger_revision, bound_policy_sha=p.policy_sha,
                  reason="x", acks=frozenset())
    j.close()
    assert r.code == "stale" and "preview again" in r.message
    assert snapshot_ledger(env) == before
    # a changed policy hash is stale too
    assert rec(env, sell(idem="2" * 32), bound=(wt._revision(jw(env)), "0" * 64)).code == "stale"


def test_working_order_reserves_collateral_then_mark_filled(env):
    r = rec(env, {"action": wt.SELL_WORKING, "idem": "3" * 32, "symbol": "AAA", "expiry": EXP, "strike": "42", "contracts": "1"})
    assert r.code == "recorded" and r.state == "WORKING"
    p = pos(env, r.trade_id)
    assert p.state is S.WORKING and p.actual_open_credit_per_share is None and p.reserves_collateral and p.collateral_usd == D("4200")
    j = jw(env)
    from csp_alerts.policy.portfolio import compute_exposure
    assert compute_exposure(j.load_book()).put_collateral_usd == D("11200")
    j.close()
    fill = {"action": wt.MARK_FILLED, "idem": "4" * 32, "trade_id": r.trade_id, "credit": "0.52", "fees": "0.70",
            "executed_date": "2026-10-30", "executed_time": "09:45"}
    pl = plan(env, fill)
    assert pl.ok and "WORKING -> OPEN" in pl.state_change
    r2 = rec(env, fill)
    assert r2.code == "recorded" and r2.state == "OPEN"
    p = pos(env, r.trade_id)
    assert p.state is S.OPEN and p.actual_open_credit_per_share == D("0.52") and p.fees_usd == D("0.70")
    assert rec(env, {**fill, "idem": "5" * 32}).code == "invalid"       # not WORKING any more


def test_cancel_unfilled_order_needs_verification_and_releases_collateral(env):
    r = rec(env, {"action": wt.SELL_WORKING, "idem": "6" * 32, "symbol": "AAA", "expiry": EXP, "strike": "42"})
    c = {"action": wt.CANCEL_OPEN, "idem": "7" * 32, "trade_id": r.trade_id}
    assert rec(env, c).code == "ack_required" and pos(env, r.trade_id).state is S.WORKING
    assert rec(env, c, acks=("ack_verified",)).code == "recorded"
    p = pos(env, r.trade_id)
    assert p.state is S.CANCELED and not p.reserves_collateral


def test_full_loop_sell_open_monitor_close_working_closed(env):
    from csp_alerts.jobs.monitor import run_monitor
    from market_fixtures import FakeProvider as FP
    r = rec(env, sell(idem="8" * 32))
    tid = r.trade_id
    j = jw(env)
    src = FP()
    src.set("AAA", spot=48.0, strikes=(40.0,), bid=0.10, ask=0.12)
    res = run_monitor(journal=j, policy=POL, source=src, now_utc=NOW, session=OPEN)
    j.close()
    assert res.positions_checked == 2 and res.status in ("ALERTS", "NO_ALERTS")       # the web-recorded OPEN put is monitored at once
    # close order placed, still reserved and monitored
    assert rec(env, {"action": wt.CLOSE_ORDER, "idem": "9" * 32, "trade_id": tid}).code == "recorded"
    p = pos(env, tid)
    assert p.state is S.CLOSE_WORKING and p.reserves_collateral
    fill = {"action": wt.CLOSE_FILLED, "idem": "0" * 32, "trade_id": tid, "debit": "0.10", "fees": "0.65", "executed_date": "2026-10-30",
            "executed_time": "10:00"}
    pl = plan(env, fill)
    assert pl.ok and "CLOSE_WORKING -> CLOSED" in pl.state_change and any("$" in x for x in pl.lines)
    r2 = rec(env, fill)
    assert r2.code == "recorded" and r2.state == "CLOSED"
    p = pos(env, tid)
    assert p.state is S.CLOSED and p.actual_close_debit_per_share == D("0.10") and p.fees_usd == D("1.30") and not p.reserves_collateral


def test_close_fill_straight_from_open_goes_through_close_working(env):
    r = rec(env, sell(idem=ik("a1")))
    fill = {"action": wt.CLOSE_FILLED, "idem": ik("a2"), "trade_id": r.trade_id, "debit": "0.20", "fees": "0", "executed_date": "2026-10-30",
            "executed_time": "10:00"}
    assert plan(env, fill).ok and "OPEN -> CLOSE_WORKING -> CLOSED" in plan(env, fill).state_change
    assert rec(env, fill).state == "CLOSED"
    states = [json.loads(e["after_json"])["state"] for e in audit(env, "POSITION_TRANSITION") if json.loads(e["after_json"]).get("trade_id") == r.trade_id]
    assert states == ["PENDING", "OPEN", "CLOSE_WORKING", "CLOSED"]


def test_cancel_closing_order_returns_to_open(env):
    r = rec(env, sell(idem=ik("b1")))
    assert rec(env, {"action": wt.CLOSE_ORDER, "idem": ik("b2"), "trade_id": r.trade_id}).code == "recorded"
    c = {"action": wt.CANCEL_CLOSE, "idem": ik("b3"), "trade_id": r.trade_id}
    assert rec(env, c, acks=("ack_verified",)).state == "OPEN"


def test_expiry_refuses_future_dates_and_needs_confirmation(env):
    r = rec(env, sell(idem=ik("c1")))
    e = {"action": wt.EXPIRE, "idem": ik("c2"), "trade_id": r.trade_id}
    assert not plan(env, {**e, "expiry_date": "2099-01-01"}).ok
    assert any("future" in x for x in plan(env, {**e, "expiry_date": "2099-01-01"}).errors)
    # today is well before the expiration date: the state machine refuses the record
    bad = plan(env, {**e, "expiry_date": TODAY.isoformat()})
    assert not bad.ok and any("before the expiration" in x for x in bad.errors)
    # on the expiration day it works (clamped to the journal clock)
    later = NOW + timedelta(days=40, hours=8)
    f = {**e, "expiry_date": (TODAY + timedelta(days=40)).isoformat()}
    j = jw(env)
    pl = wt.build_plan(j, POL, env.approved_list, f, later, TZ)
    j.close()
    assert pl.ok, pl.errors
    assert rec(env, f, now=later, acks=()).code == "ack_required"
    assert rec(env, f, now=later, acks=("ack_expired",)).state == "EXPIRED"


def test_unsupported_states_say_use_the_cli_and_stay_blocking(env):
    r = rec(env, sell(idem=ik("d1")))
    j = jw(env)
    j.transition(r.trade_id, S.ASSIGNED_PENDING_RECONCILIATION)
    j.close()
    p = plan(env, {"action": wt.CLOSE_ORDER, "idem": ik("d2"), "trade_id": r.trade_id})
    assert p.unsupported and "documented CLI procedure" in p.lines[0]
    assert rec(env, {"action": wt.CLOSE_ORDER, "idem": ik("d2"), "trade_id": r.trade_id}).code == "unsupported"
    j = jw(env)
    book = j.load_book()
    j.close()
    codes = {i.code for i in reconcile(book, POL, NOW).issues if i.severity == "BLOCK"}
    assert "ASSIGNMENT_PENDING" in codes


def test_multi_contract_positions_are_unsupported_here(env):
    j = jw(env)
    from csp_alerts.journal.store import NewPosition
    j.create_draft(NewPosition("MC", "AAA", TODAY + timedelta(days=40), D("40"), 100, 2, "s", "b"))
    j.transition("MC", S.PENDING)
    j.close()
    p = plan(env, {"action": wt.CANCEL_OPEN, "idem": ik("e1"), "trade_id": "MC"})
    assert p.unsupported


# ---------------------------------------------------------------- breaches
def test_breach_is_recorded_flagged_acknowledged_and_blocks_entries(env):
    first = rec(env, sell(idem=ik("f1"), strike="40"))
    assert first.code == "recorded"
    # a second AAA put pushes put collateral to 15,200 (> 15,000 pilot cap) and breaks the contracts-per-underlying cap
    f = sell(idem=ik("f2"), strike="42")
    p = plan(env, f)
    assert p.ok and {"PILOT_COLLATERAL_LIMIT", "CONTRACT_COUNT_LIMIT"} <= set(p.breach_codes)
    assert [c.name for c in p.checks] == ["ack_breach"]
    rows = {r.key: r for r in p.caps}
    assert rows["pilot"].breached and rows["pilot"].room_after < 0
    before = snapshot_ledger(env)
    assert rec(env, f).code == "ack_required" and snapshot_ledger(env) == before                  # not silently recorded
    r = rec(env, f, acks=("ack_breach",))
    assert r.code == "recorded" and "PILOT_COLLATERAL_LIMIT" in r.breach_codes
    assert pos(env, r.trade_id).state is S.OPEN                                                # reality recorded as it happened
    ev = audit(env, "POLICY_BREACH_RECORDED")
    assert len(ev) == 1 and json.loads(ev[0]["after_json"])["trade_id"] == r.trade_id
    j = jw(env)
    book = j.load_book()
    j.close()
    issues = {i.code: i for i in reconcile(book, POL, NOW).issues}
    assert issues["POLICY_BREACH_UNRESOLVED"].severity == "BLOCK" and issues["POLICY_BREACH_UNRESOLVED"].ref == r.trade_id
    # a fresh check-in does NOT clear it (the exposure is real) ...
    ci = {"action": wt.CHECKIN, "idem": ik("f3"), "equity": "100000", "free_cash": "50000", "nlots": "0"}
    assert rec(env, ci, acks=("ack_reconciled",), now=NOW + timedelta(minutes=1)).code == "recorded"
    j = jw(env)
    codes = {i.code for i in reconcile(j.load_book(), POL, NOW + timedelta(minutes=2)).issues if i.severity == "BLOCK"}
    j.close()
    assert codes == {"POLICY_BREACH_UNRESOLVED"}
    # ... closing the breaching position does
    assert rec(env, {"action": wt.CLOSE_FILLED, "idem": ik("f4"), "trade_id": r.trade_id, "debit": "0.2", "fees": "0",
                     "executed_date": "2026-10-30", "executed_time": "10:00"}, now=NOW + timedelta(minutes=3)).code == "recorded"
    j = jw(env)
    codes = {i.code for i in reconcile(j.load_book(), POL, NOW + timedelta(minutes=4)).issues}
    j.close()
    assert "POLICY_BREACH_UNRESOLVED" not in codes


def test_unapproved_name_is_flagged_with_unknown_labels_unless_the_owner_types_them(env):
    f = sell(idem=ik("g1"), symbol="ZZZ")
    p = plan(env, f)
    assert p.ok and {"NOT_APPROVED", "LABELS_UNKNOWN"} <= set(p.breach_codes) and p.contract["sector"] == "unknown"
    f2 = {**f, "sector": "Tech", "bucket": "Large Cap"}
    p2 = plan(env, f2)
    assert "LABELS_UNKNOWN" not in p2.breach_codes and "NOT_APPROVED" in p2.breach_codes and p2.contract["sector"] == "Tech"
    r = rec(env, f2, acks=("ack_breach",))
    assert r.code == "recorded" and pos(env, r.trade_id).sector == "tech"          # normalized like every label


def test_breach_flag_is_kept_when_a_working_order_fills(env):
    assert rec(env, sell(idem=ik("h1"), strike="40")).code == "recorded"
    w = rec(env, {"action": wt.SELL_WORKING, "idem": ik("h2"), "symbol": "AAA", "expiry": EXP, "strike": "42"}, acks=("ack_breach",))
    assert w.code == "recorded" and w.breach_codes
    fill = {"action": wt.MARK_FILLED, "idem": ik("h3"), "trade_id": w.trade_id, "credit": "0.5", "fees": "0", "executed_date": "2026-10-30",
            "executed_time": "10:00"}
    p = plan(env, fill)
    assert p.ok and p.breach_codes and [c.name for c in p.checks] == ["ack_breach"]
    assert rec(env, fill).code == "ack_required"
    assert rec(env, fill, acks=("ack_breach",)).code == "recorded"


# ---------------------------------------------------------------- entries blocked until a new snapshot
def test_trade_blocks_entries_until_a_new_check_in(env):
    j = jw(env)
    base = {i.code for i in reconcile(j.load_book(), POL, NOW).issues if i.severity == "BLOCK"}
    j.close()
    assert "CHANGE_AFTER_SNAPSHOT" not in base
    r = rec(env, sell(idem=ik("i1")))
    assert r.commitment_changed
    later = NOW + timedelta(minutes=1)
    j = jw(env)
    assert "CHANGE_AFTER_SNAPSHOT" in {i.code for i in reconcile(j.load_book(), POL, later).issues if i.severity == "BLOCK"}
    j.close()
    ci = {"action": wt.CHECKIN, "idem": ik("i2"), "equity": "100000", "free_cash": "50000", "nlots": "0"}
    assert rec(env, ci, acks=("ack_reconciled",), now=later).code == "recorded"
    j = jw(env)
    assert "CHANGE_AFTER_SNAPSHOT" not in {i.code for i in reconcile(j.load_book(), POL, later + timedelta(minutes=1)).issues}
    j.close()


# ---------------------------------------------------------------- atomicity and hostile text
def test_a_failure_in_the_middle_rolls_everything_back(env, monkeypatch):
    before = snapshot_ledger(env)
    orig = Journal.transition
    calls = {"n": 0}

    def flaky(self, trade_id, to_state, **kw):
        calls["n"] += 1
        if to_state is S.OPEN:
            raise RuntimeError("disk full " + SECRET)
        return orig(self, trade_id, to_state, **kw)
    monkeypatch.setattr(Journal, "transition", flaky)
    j = jw(env)
    p = wt.build_plan(j, POL, env.approved_list, sell(), NOW, TZ)
    with pytest.raises(RuntimeError):                      # unexpected errors are not swallowed into a half-write
        wt.record(j, POL, env.approved_list, sell(), now=NOW, tz=TZ, bound_revision=p.ledger_revision, bound_policy_sha=p.policy_sha,
                  reason="x", acks=frozenset())
    j.close()
    assert snapshot_ledger(env) == before and calls["n"] >= 2


def test_domain_rejection_becomes_the_canned_result_and_writes_nothing(env, monkeypatch):
    from csp_alerts.journal.state_machine import TransitionError
    before = snapshot_ledger(env)
    orig = Journal.transition

    def refuse(self, trade_id, to_state, **kw):
        if to_state is S.OPEN:
            raise TransitionError("illegal " + SECRET)
        return orig(self, trade_id, to_state, **kw)
    monkeypatch.setattr(Journal, "transition", refuse)
    r = rec(env, sell())
    assert r.code == "rejected" and SECRET not in r.message and snapshot_ledger(env) == before


def test_hostile_reason_text_is_cleaned_and_length_limited(env):
    evil = "<script>alert(1)</script>\x1b[31m‮" + "A" * 1000 + SECRET
    r = rec(env, sell(idem=ik("j1")), reason=evil)
    assert r.code == "recorded"
    mc = [e for e in audit(env, "MANUAL_COMMAND") if e["actor"] == "web:owner"][-1]
    reason = json.loads(mc["after_json"])["reason"]
    assert len(reason) <= wt.MAX_REASON and "\x1b" not in reason and "‮" not in reason and SECRET not in reason
    note = pos(env, r.trade_id).notes
    assert len(note) <= 200 and "\x1b" not in note


def test_capacity_rows_agree_with_check_portfolio_limits(env):
    from csp_alerts.policy.portfolio import ProposedPut, capacity_rows, check_portfolio_limits
    j = jw(env)
    book = j.load_book()
    j.close()
    for strike in ("40", "42", "90", "400"):
        pr = ProposedPut("AAA", "ETF", "sec_AAA", "b_AAA", TODAY + timedelta(days=40), D(strike), 100, 1)
        for cash in (D("100000"), D("1000"), None):
            rows = capacity_rows(book, pr, POL, cash)
            want = {r.value for r in check_portfolio_limits(book, pr, POL, cash)}
            assert {r.reason.value for r in rows if r.breached} == want, (strike, cash)


def test_get_style_planning_never_mutates_the_database_bytes(env):
    import hashlib
    from pathlib import Path

    def digest():
        j = jw(env)
        j.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        j.close()
        return hashlib.sha256(Path(env.db).read_bytes()).hexdigest()
    d0 = digest()
    for f in (sell(), sell(contracts="2"), {"action": wt.CHECKIN, "idem": ik("k1"), "equity": "1", "free_cash": "1", "nlots": "0"}):
        plan(env, f)
    assert digest() == d0
