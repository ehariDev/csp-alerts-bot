"""Acceptance for the five owner decisions made after the first verification round.

1. Drawdown 3/5/8% ALL latch until Journal.record_risk_reset.
2. Starting equity confirmed once via audited BASELINE_CONFIRMED; peak = max(baseline, confirmed snapshots);
   no baseline and no reset -> entries blocked (NO_BASELINE -> LEDGER_UNRECONCILED).
3. Free cash is NET; CASH_INCONSISTENT blocks when free cash + active put collateral > equity.
4. ADJUSTMENT_REVIEW positions are monitored (+ URGENT ADJUSTMENT_REVIEW notice).
5. Broker env prefixes error; other credential-looking names warn (name only); example whitelist guard is content-based.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from acc_helpers import NOW, POLICY_PATH, REPO, S, TODAY, book, codes, lot, obs, policy, put, run, snap
from test_settings_policy_boundary import cli

from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_alerts.policy.approved import load_approved
from csp_alerts.policy.config import PolicyError
from csp_alerts.policy.drawdown import DrawdownLevel as L
from csp_alerts.policy.drawdown import compute_drawdown
from csp_alerts.policy.exits import OptionQuote, Trigger, evaluate_exits
from csp_alerts.settings import StartupError, env_warnings, settings_from_mapping

POL = policy()
BASE = D("100000")


def snaps(*equities, confirmed=True):
    n = len(equities)
    return [AccountSnapshot(f"s{i}", NOW - timedelta(hours=2 + (n - 1 - i) * 24), D(e), D("50000"), confirmed, "v1")
            for i, e in enumerate(equities)]


# ======================================================================= decision 1: latch at every level
@pytest.mark.parametrize("dip,level,reason", [
    ("96900", L.PAUSE, "DRAWDOWN_PAUSE"),          # 3.1%
    ("94900", L.REDUCE_REVIEW, "DRAWDOWN_PAUSE"),  # 5.1%
    ("91900", L.HARD_HALT, "HARD_HALT"),           # 8.1%
])
@pytest.mark.parametrize("rebound", ["100000", "100500", "99000"])
def test_every_level_latches_after_any_rebound(dip, level, reason, rebound):
    # snapshots: 100,000 -> dip -> rebound. Level is the highest reached; rebound (even to a new high) never re-enables.
    st = compute_drawdown(snaps("100000", dip, rebound), POL, baseline_equity_usd=BASE)
    assert st.level is level and st.latched
    d = run(b=book(snaps=snaps("100000", dip, rebound)))
    assert reason in codes(d) and not d.passed


def test_latch_level_is_highest_reached_and_never_decreases_without_reset():
    st = compute_drawdown(snaps("100000", "96900", "94900", "99000"), POL, baseline_equity_usd=BASE)
    assert st.level is L.REDUCE_REVIEW       # PAUSE then REDUCE then rebound
    st = compute_drawdown(snaps("100000", "94900", "96900", "99900"), POL, baseline_equity_usd=BASE)
    assert st.level is L.REDUCE_REVIEW       # recovering to 3.1% does not downgrade


@pytest.mark.parametrize("equity,level", [
    ("100000", L.NONE), ("97000.01", L.NONE), ("97000", L.PAUSE), ("95000.01", L.PAUSE), ("95000", L.REDUCE_REVIEW),
    ("92000.01", L.REDUCE_REVIEW), ("92000", L.HARD_HALT),
])
def test_boundaries_with_baseline_as_peak_on_first_snapshot(equity, level):
    # peak seeded by baseline 100,000: loss 3,000 / 5,000 / 8,000 exactly at the thresholds
    st = compute_drawdown(snaps(equity), POL, baseline_equity_usd=BASE)
    assert st.level is level


def test_85k_first_snapshot_reads_15pct_and_latches_halt():
    st = compute_drawdown(snaps("85000"), POL, baseline_equity_usd=BASE)
    assert st.level is L.HARD_HALT and st.halt_latched
    assert st.drawdown == D("0.15") and st.peak_equity_usd == BASE
    d = run(b=book(snaps=snaps("85000")))
    assert "HARD_HALT" in codes(d) and not d.passed


def test_peak_is_max_of_baseline_and_snapshots():
    # baseline 100,000; snapshot 120,000 raises the peak; 110,400 is exactly 8% below 120,000 -> HALT
    assert compute_drawdown(snaps("120000", "110400"), POL, baseline_equity_usd=BASE).level is L.HARD_HALT
    assert compute_drawdown(snaps("120000", "110400.01"), POL, baseline_equity_usd=BASE).level is L.REDUCE_REVIEW
    # a snapshot BELOW baseline never lowers the peak: 100,000 baseline, 96,000 then 99,000 stays latched PAUSE
    assert compute_drawdown(snaps("96000", "99000"), POL, baseline_equity_usd=BASE).level is L.PAUSE


def test_unconfirmed_snapshots_do_not_set_peak_or_level_even_with_baseline():
    s = snaps("120000", "85000", confirmed=False)
    st = compute_drawdown(s, POL, baseline_equity_usd=BASE)
    assert st.level is L.UNKNOWN


def test_reset_cutoff_drops_old_baseline_and_rebaselines():
    s = snaps("100000", "91000", "99000")
    # reset recorded at the 99,000 snapshot: old peak (and baseline equity) stop counting
    st = compute_drawdown(s, POL, baseline_asof_utc=s[-1].asof_utc, baseline_equity_usd=BASE)
    assert st.level is L.NONE and st.peak_equity_usd == D("99000")
    # fresh decline after the reset latches again, measured from the NEW peak (not the 100,000 baseline)
    s2 = snaps("100000", "91000", "99000", "96000")
    st2 = compute_drawdown(s2, POL, baseline_asof_utc=s2[2].asof_utc, baseline_equity_usd=BASE)
    assert st2.level is L.PAUSE and abs(st2.drawdown - D("0.030303")) < D("0.000001")   # 3,000/99,000
    # baseline 100,000 must NOT be re-seeded after reset: 99,000 first post-reset snapshot is 0%, not 1%
    assert compute_drawdown(snaps("99000"), POL, baseline_asof_utc=NOW - timedelta(days=1),
                            baseline_equity_usd=BASE).level is L.NONE


def make_journal(clock_t=NOW):
    j = Journal(":memory:", clock=lambda: clock_t)
    j.migrate()
    return j


def add(j, sid, hours_ago, eq, cash="50000"):
    j.add_snapshot(AccountSnapshot(sid, NOW - timedelta(hours=hours_ago), D(eq), D(cash), True, "v1"))


def test_journal_end_to_end_latch_reset_and_resume():
    j = make_journal()
    j.confirm_baseline(BASE)
    add(j, "s0", 72, "100000")
    add(j, "s1", 48, "96900")        # 3.1% -> PAUSE latch
    add(j, "s2", 2, "99900")         # rebound
    d = run(b=j.load_book())
    assert "DRAWDOWN_PAUSE" in codes(d) and not d.passed
    j.record_risk_reset("reviewed 3% pause")
    b = j.load_book()
    assert run(b=b).passed                                      # reset clears; peak is now 99,900
    # new decline after reset latches again from the NEW peak: 99,900 -> 96,000 = 3.9%
    add(j, "s3", 1, "96000")
    assert "DRAWDOWN_PAUSE" in codes(run(b=j.load_book()))


def test_journal_halt_then_reset_rebaselines_at_current_equity():
    j = make_journal()
    j.confirm_baseline(BASE)
    add(j, "s0", 3, "85000")
    b = j.load_book()
    assert "HARD_HALT" in codes(run(b=b))
    j.record_risk_reset("reviewed after 15% drawdown")
    b = j.load_book()
    assert b.last_risk_reset_utc == NOW - timedelta(hours=3)
    assert run(b=b).passed                                      # re-baselined at 85,000
    assert [e["event_type"] for e in j.audit_events() if e["event_type"] in ("BASELINE_CONFIRMED", "RISK_RESET")] == \
        ["BASELINE_CONFIRMED", "RISK_RESET"]


def test_reset_requires_note_and_confirmed_snapshot_still():
    j = make_journal()
    j.confirm_baseline(BASE)
    with pytest.raises(JournalError):
        j.record_risk_reset("note")          # no confirmed snapshot
    add(j, "s0", 3, "85000")
    with pytest.raises(JournalError):
        j.record_risk_reset("   ")


# ======================================================================= decision 2: baseline event
def test_no_baseline_blocks_entries_even_with_fresh_confirmed_snapshot():
    b = book(baseline=None)
    d = run(b=b)
    assert codes(d) == {"LEDGER_UNRECONCILED"} and not d.passed
    rep = reconcile(b, POL, NOW)
    assert "NO_BASELINE" in {i.code for i in rep.issues} and rep.blocking
    assert run(b=book()).passed


def test_baseline_confirmable_exactly_once_and_audited():
    j = make_journal()
    assert j.baseline() is None
    j.confirm_baseline(BASE, note="starting equity")
    assert j.baseline()[0] == BASE
    with pytest.raises(JournalError):
        j.confirm_baseline(D("90000"))
    assert j.baseline()[0] == BASE                    # unchanged by the rejected second call
    ev = j.audit_events("BASELINE_CONFIRMED")
    assert len(ev) == 1 and ev[0]["actor"] == "owner" and json.loads(ev[0]["after_json"])["equity_usd"] == "100000"
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):         # audited and tamper-proof
        j.conn.execute("DELETE FROM audit_log WHERE event_type='BASELINE_CONFIRMED'")
    with pytest.raises(sqlite3.DatabaseError):
        j.conn.execute("UPDATE audit_log SET after_json='{}' WHERE event_type='BASELINE_CONFIRMED'")


@pytest.mark.parametrize("bad", [D("0"), D("-1"), D("NaN"), D("Infinity"), 100000.0, "100000", None])
def test_baseline_rejects_invalid_values_and_writes_nothing(bad):
    j = make_journal()
    with pytest.raises(Exception):
        j.confirm_baseline(bad)
    assert j.baseline() is None and j.audit_events("BASELINE_CONFIRMED") == []


def test_baseline_event_lands_in_book_and_clears_no_baseline():
    j = make_journal()
    add(j, "s0", 2, "100000")
    assert "NO_BASELINE" in {i.code for i in reconcile(j.load_book(), POL, NOW).issues}
    j.confirm_baseline(BASE)
    b = j.load_book()
    assert b.baseline_equity_usd == BASE and "NO_BASELINE" not in {i.code for i in reconcile(b, POL, NOW).issues}


def test_baseline_event_is_not_a_trade_so_does_not_require_new_snapshot():
    j = make_journal()
    add(j, "s0", 2, "100000")
    j.confirm_baseline(BASE)
    assert not reconcile(j.load_book(), POL, NOW).blocking


def test_cli_confirm_baseline_flow(tmp_path):
    r = cli(["journal", "init"], tmp_path)
    assert r.returncode == 0
    r = cli(["reconcile-check"], tmp_path)
    assert "NO_BASELINE" in r.stdout and r.returncode == 1
    r = cli(["journal", "confirm-baseline", "--equity", "100000"], tmp_path)
    assert r.returncode == 0 and "WARN" not in r.stdout
    r = cli(["journal", "confirm-baseline", "--equity", "90000"], tmp_path)           # once only
    assert r.returncode != 0
    r = cli(["account-check"], tmp_path)
    assert json.loads(r.stdout)["baseline_equity_usd"] == "100000"
    assert "NO_BASELINE" not in cli(["reconcile-check"], tmp_path).stdout


@pytest.mark.parametrize("val", ["abc", "-5", "0", "NaN", "Infinity", ""])
def test_cli_confirm_baseline_rejects_bad_values(tmp_path, val):
    cli(["journal", "init"], tmp_path)
    r = cli(["journal", "confirm-baseline", "--equity", val], tmp_path)
    assert r.returncode != 0
    assert "NO_BASELINE" in cli(["reconcile-check"], tmp_path).stdout


def test_cli_confirm_baseline_warns_when_differs_from_reference(tmp_path):
    cli(["journal", "init"], tmp_path)
    r = cli(["journal", "confirm-baseline", "--equity", "85000"], tmp_path)
    assert r.returncode == 0 and "WARN" in r.stdout and "100000" in r.stdout


# ======================================================================= decision 3: CASH_INCONSISTENT
@pytest.mark.parametrize("cash,blocked", [("95000", False), ("94999.99", False), ("95000.01", True), ("100000", True)])
def test_cash_inconsistent_boundary_with_active_put(cash, blocked):
    # equity 100,000; existing active put collateral 5,000 -> free cash + 5,000 must be <= 100,000
    p = put("E1", "ET1", "50.00", sector="x1", bucket="y1")
    b = book(positions=[p], snaps=[snap(equity="100000", cash=cash)])
    rep = reconcile(b, POL, NOW)
    assert ("CASH_INCONSISTENT" in {i.code for i in rep.issues}) is blocked
    d = run(b=b)
    assert ("LEDGER_UNRECONCILED" in codes(d)) is blocked and (not d.passed) is blocked


@pytest.mark.parametrize("cash,blocked", [("100000", False), ("100000.01", True)])
def test_cash_inconsistent_boundary_without_positions(cash, blocked):
    b = book(snaps=[snap(equity="100000", cash=cash)])
    assert ("CASH_INCONSISTENT" in {i.code for i in reconcile(b, POL, NOW).issues}) is blocked


@pytest.mark.parametrize("state", [S.PENDING, S.WORKING, S.OPEN, S.CLOSE_WORKING, S.ADJUSTMENT_REVIEW])
def test_cash_consistency_counts_all_collateral_states(state):
    p = put("E1", "ET1", "50.00", state=state)
    ok = book(positions=[p], snaps=[snap(cash="95000")])
    bad = book(positions=[p], snaps=[snap(cash="95000.01")])
    assert "CASH_INCONSISTENT" not in {i.code for i in reconcile(ok, POL, NOW).issues}
    assert "CASH_INCONSISTENT" in {i.code for i in reconcile(bad, POL, NOW).issues}


@pytest.mark.parametrize("state", [S.CLOSED, S.CANCELED, S.EXPIRED, S.DRAFT])
def test_cash_consistency_ignores_non_collateral_states(state):
    p = put("E1", "ET1", "50.00", state=state)
    assert "CASH_INCONSISTENT" not in {i.code for i in reconcile(book(positions=[p], snaps=[snap(cash="100000")]), POL, NOW).issues}


def test_cash_inconsistent_message_never_blocks_exits():
    p = put("E1", "AAA", "75.00", expiration=TODAY + timedelta(days=10), asset_type="STOCK")
    b = book(positions=[p], snaps=[snap(cash="100000")])
    assert not run(b=b).passed
    q = OptionQuote(p.contract_key, NOW, D("0.1"), D("0.2"), D("-0.2"), D("80"))
    assert Trigger.TIME_EXIT in {s.trigger for s in evaluate_exits(p, q, POL, NOW)}


# ======================================================================= decision 4: ADJUSTMENT_REVIEW monitored
def adj(days=30, **kw):
    return put("ADJ1", "AAA", "75.00", state=S.ADJUSTMENT_REVIEW, expiration=TODAY + timedelta(days=days),
               asset_type="STOCK", sector="tech", bucket="b1", **kw)


def q(p, **kw):
    d = dict(ask="0.30", bid="0.28", delta="-0.20", spot="80.00", age=60)
    d.update(kw)
    mk = lambda v: None if v is None else D(v)  # noqa: E731
    return OptionQuote(kw.get("key", p.contract_key), NOW - timedelta(seconds=d["age"]), mk(d["bid"]), mk(d["ask"]),
                       mk(d["delta"]), mk(d["spot"]))


def sg(p, quote):
    return {s.trigger: s for s in evaluate_exits(p, quote, POL, NOW)}


def test_adjustment_review_notice_is_urgent_with_valid_quote_and_no_false_triggers():
    p = adj()
    r = sg(p, q(p))
    assert r[Trigger.ADJUSTMENT_REVIEW].severity == "URGENT"
    assert set(r) == {Trigger.ADJUSTMENT_REVIEW}          # DTE 30, OTM, ask .30 vs credit .50 (40%), delta .20


def test_adjustment_review_price_triggers_fire_with_valid_quote():
    p = adj()
    r = sg(p, q(p, ask="0.20", bid="0.18", delta="-0.35"))
    assert {Trigger.ADJUSTMENT_REVIEW, Trigger.PROFIT_EXIT, Trigger.DELTA_REVIEW} <= set(r)
    r = sg(p, q(p, ask="1.00", bid="0.95"))
    assert Trigger.LOSS_REVIEW in r and Trigger.ADJUSTMENT_REVIEW in r


def test_adjustment_review_stale_quote_still_notices_and_flags_data_issue():
    p = adj(days=10)
    r = sg(p, q(p, age=1801, ask="0.10", bid="0.05"))
    assert {Trigger.ADJUSTMENT_REVIEW, Trigger.DATA_ISSUE, Trigger.TIME_EXIT} <= set(r)
    assert Trigger.PROFIT_EXIT not in r                    # numeric suppressed on stale quote


def test_adjustment_review_no_quote_and_itm():
    p = adj(days=30)
    r = sg(p, None)
    assert {Trigger.ADJUSTMENT_REVIEW, Trigger.DATA_ISSUE} <= set(r) and Trigger.TIME_EXIT not in r
    r = sg(p, q(p, spot="74.99"))                          # ITM
    assert Trigger.ASSIGNMENT_REVIEW in r and Trigger.ADJUSTMENT_REVIEW in r
    r = sg(adj(days=3), None)
    assert {Trigger.ADJUSTMENT_REVIEW, Trigger.TIME_EXIT, Trigger.ASSIGNMENT_REVIEW, Trigger.DATA_ISSUE} <= set(r)


def test_adjustment_notice_only_for_adjustment_state_and_other_terminal_states_silent():
    for st in (S.OPEN, S.CLOSE_WORKING):
        p = put("O1", "AAA", "75.00", state=st, expiration=TODAY + timedelta(days=30), asset_type="STOCK")
        assert Trigger.ADJUSTMENT_REVIEW not in sg(p, q(p))
    for st in (S.CLOSED, S.CANCELED, S.EXPIRED, S.DRAFT, S.PENDING, S.WORKING, S.ASSIGNED_PENDING_RECONCILIATION,
               S.ASSIGNED_RECONCILED):
        p = put("O1", "AAA", "75.00", state=st, expiration=TODAY + timedelta(days=3), asset_type="STOCK")
        assert evaluate_exits(p, None, POL, NOW) == [], st


def test_adjustment_review_still_reserves_collateral_blocks_entries_and_alert_dict_ok():
    p = adj()
    b = book(positions=[p])
    from csp_alerts.policy.portfolio import compute_exposure
    assert compute_exposure(b).put_collateral_usd == D("7500")
    assert "LEDGER_UNRECONCILED" in codes(run(b=b)) and not run(b=b).passed
    d = sg(p, None)[Trigger.ADJUSTMENT_REVIEW].to_dict()
    assert d["severity"] == "URGENT" and d["trade_id"] == "ADJ1"


# ======================================================================= decision 5: env prefixes / warnings / whitelist
@pytest.mark.parametrize("name", [
    "ETRADE_KEY", "FUTU_PWD", "WEBULL_X", "TOS_TOKEN", "TDA_KEY", "SCHWAB", "SCHWAB_APP_KEY", "SCHWABX", "etrade_key",
    "Futu_Pwd", "tos_token", "tda_key", "IBKR_USER", "IB_PORT", "MOOMOO", "ALPACA_API_KEY", "TRADIER_TOKEN",
    "TASTYTRADE_USER", "ROBINHOOD_PW",
])
def test_broker_prefixed_env_vars_error(name):
    with pytest.raises(StartupError) as ei:
        settings_from_mapping({name: "supersecretvalue123"})
    assert "supersecretvalue123" not in str(ei.value) and "supersecretvalue123" not in repr(ei.value)


@pytest.mark.parametrize("name", ["ETRADE_KEY", "SCHWAB", "TOS_TOKEN", "FUTU_X", "WEBULL_X", "TDA_KEY"])
def test_broker_prefixed_env_vars_empty_value_allowed(name):
    settings_from_mapping({name: ""})


@pytest.mark.parametrize("name", ["CSP_API_TOKEN", "CSP_DRY_RUN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_X_KEY"])
def test_own_prefixed_names_allowed(name):
    env = {name: "value"}
    if name == "CSP_DRY_RUN":
        env[name] = "true"
    settings_from_mapping(env)
    assert name not in env_warnings(env)


def test_normal_windows_environment_never_flagged():
    win = {"PATH": r"C:\Windows;C:\x", "USERPROFILE": r"C:\Users\u", "APPDATA": r"C:\Users\u\AppData\Roaming",
           "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "SYSTEMROOT": r"C:\Windows", "COMPUTERNAME": "PC",
           "USERNAME": "u", "TEMP": r"C:\t", "TMP": r"C:\t", "HOMEDRIVE": "C:", "PROCESSOR_ARCHITECTURE": "AMD64",
           "NUMBER_OF_PROCESSORS": "8", "ProgramFiles(x86)": r"C:\Program Files (x86)", "PATHEXT": ".EXE",
           "COMSPEC": r"C:\Windows\system32\cmd.exe", "OS": "Windows_NT", "TOSHIBA_HOME": "x", "IBM_HOME": "x"}
    s = settings_from_mapping(win)
    assert s.dry_run is True
    assert env_warnings(win) == []


def test_real_process_environment_is_accepted_or_names_the_culprit():
    env = dict(os.environ)
    try:
        settings_from_mapping({k: v for k, v in env.items() if not k.upper().startswith(("CSP_", "TELEGRAM_"))})
    except StartupError as exc:           # a genuine broker-prefixed var on this machine would be correct to flag
        assert "brokerage" in str(exc)
        pytest.fail(f"host environment flagged: {exc}")


def test_credential_looking_non_broker_names_warn_only_and_never_print_value():
    secret = "sup3r-s3cret-value"
    env = {"MY_SERVICE_TOKEN": secret, "OPENAI_API_KEY": secret, "GITHUB_TOKEN": secret, "DB_PASSWORD": secret,
           "PATH": "x"}
    settings_from_mapping(env)                                      # no error
    w = env_warnings(env)
    assert set(w) == {"MY_SERVICE_TOKEN", "OPENAI_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD"}
    assert secret not in " ".join(w)
    assert env_warnings({"MY_SERVICE_TOKEN": ""}) == []             # empty value: nothing to warn about


def test_doctor_warns_names_only_and_exits_zero(tmp_path):
    secret = "sup3r-s3cret-value"
    r = cli(["doctor"], tmp_path, {"MY_SERVICE_TOKEN": secret})
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "WARN" in out and "MY_SERVICE_TOKEN" in out and secret not in out


def test_doctor_errors_on_broker_prefix_and_never_prints_value(tmp_path):
    secret = "sup3r-s3cret-value"
    for name in ("ETRADE_KEY", "FUTU_PWD", "WEBULL_X", "TOS_TOKEN", "TDA_KEY", "SCHWAB"):
        r = cli(["doctor"], tmp_path, {name: secret})
        assert r.returncode != 0 and secret not in r.stdout + r.stderr, name


def test_doctor_ordinary_env_has_no_credential_warning(tmp_path):
    r = cli(["doctor"], tmp_path)
    assert r.returncode == 0 and "WARN credential" not in r.stdout


# ---- whitelist guard is content-based
def example_rec():
    return json.loads((REPO / "config" / "approved.example.json").read_text(encoding="utf-8"))["approved_underlyings"][0]


def write_list(path, recs, **top):
    path.write_text(json.dumps({"approved_underlyings": recs, **top}), encoding="utf-8")
    return path


@pytest.mark.parametrize("fname", ["live.json", "approved.json", "my_whitelist.JSON", "approved.example.json", "x.example.json"])
def test_renamed_copy_of_example_is_still_rejected(tmp_path, fname):
    dest = tmp_path / fname
    shutil.copyfile(REPO / "config" / "approved.example.json", dest)
    with pytest.raises(PolicyError):
        load_approved(dest)


@pytest.mark.parametrize("mut", [
    {"ticker": "EXAMPLE", "ownership_reason": "clean reason"},                      # example ticker
    {"ticker": "ZZZ", "ownership_reason": "Fictional example only"},                # marker words
    {"ticker": "ZZZ", "ownership_reason": "SYNTHETIC data"},                        # case-insensitive
    {"ticker": "ZZZ", "ownership_reason": "placeholder, replace me"},
])
def test_synthetic_content_rejected_regardless_of_filename(tmp_path, mut):
    rec = {**{k: v for k, v in example_rec().items() if k != "synthetic"}, **mut}   # per-record flag removed: content only
    with pytest.raises(PolicyError):
        load_approved(write_list(tmp_path / "totally_live.json", [rec]))


def test_synthetic_flag_rejected_and_real_looking_record_loads(tmp_path):
    clean = {**{k: v for k, v in example_rec().items() if k != "synthetic"}, "ticker": "ABCD", "ownership_reason": "long-term owner thesis, valuation reviewed"}
    with pytest.raises(PolicyError):
        load_approved(write_list(tmp_path / "a.json", [clean], synthetic=True))
    assert [a.ticker for a in load_approved(write_list(tmp_path / "b.json", [clean]))] == ["ABCD"]
    # one synthetic record poisons the whole list
    with pytest.raises(PolicyError):
        load_approved(write_list(tmp_path / "c.json", [clean, example_rec()]))


def test_shipped_whitelist_still_empty_after_changes():
    assert load_approved(REPO / "config" / "approved.json") == []


# ======================================================================= D1-D4 regression pins (named, explicit)
def test_regression_D1_label_normalization():
    p = put("E1", "ETF1", "60.00", sector=" TECH ", bucket=" B1 ", expiration=date(2026, 11, 20))
    d = run(obs(strike=D("50.00"), spot=D("55")), b=book(positions=[p], snaps=[snap(cash="90000")]))
    assert {"SECTOR_LIMIT", "CORRELATED_LIMIT"} <= codes(d)


def test_regression_D2_missing_last_print_blocks():
    assert not run(obs(last_option_trade_at_utc=None)).passed


def test_regression_D3_csv_open_with_closed_at_quarantined(tmp_path):
    from test_journal_csv import row, write_csv
    from csp_alerts.journal.import_csv import import_positions_csv
    j = make_journal()
    rep = import_positions_csv(j, write_csv(tmp_path / "x.csv", [row(closed_at_utc="2026-10-25T15:00:00Z")]), dry_run=True)
    assert len(rep.quarantined) == 1


def test_regression_D4_float_and_nan_clean_errors():
    for kw in ({"bid": 0.5}, {"strike": 75.0}, {"delta": -0.13}):
        with pytest.raises((TypeError, ValueError)):
            run(obs(**kw))
    j = make_journal()
    j.create_draft(NewPosition("T1", "AAA", date(2026, 12, 4), D("75"), 100, 1, "tech", "b1"))
    j.transition("T1", S.PENDING)
    from csp_alerts.journal.state_machine import TransitionError
    with pytest.raises(TransitionError):
        j.transition("T1", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("NaN"), opened_at_utc=NOW)
