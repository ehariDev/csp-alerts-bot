"""Dashboard read layer + views (WD-010..WD-016, WD-018). Synthetic journal, in-process loopback server, no network."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import timedelta
from decimal import Decimal as D

import pytest
from chain_fixtures import raw_row
from dash_fixture import EVIL, Running, build
from factories import NOW, TODAY, policy

from csp_alerts.journal.models import PositionState as S
from csp_alerts.journal.store import Journal, JournalError, NewPosition
from csp_dashboard.readonly import JournalMissing, ReadOnlyJournal


def states(c, path="/candidates"):
    return {s["code"] for s in c.api(path)["data"]["states"]}


def sub(tmp_path, name, **kw):
    (tmp_path / name).mkdir()
    return build(tmp_path / name, **kw)


# ---------------------------------------------------------------- read-only connection
def test_readonly_journal_rejects_every_write(tmp_path):
    p = build(tmp_path)
    rj = ReadOnlyJournal(p.db)
    for sql in ("INSERT INTO run_status VALUES ('x','SCAN','t','S','[]','{}')", "DELETE FROM option_positions", "CREATE TABLE t(x)",
                "PRAGMA journal_mode=DELETE", "UPDATE audit_log SET actor='x'"):
        with pytest.raises(sqlite3.Error):
            rj.conn.execute(sql)
    for fn in (rj.tx, rj.migrate):
        with pytest.raises(JournalError):
            fn()
    with pytest.raises(JournalError):
        rj.record_run("r", "SCAN", NOW, "X")
    rj.close()


def test_missing_journal_is_reported_and_never_created(tmp_path):
    with pytest.raises(JournalMissing):
        ReadOnlyJournal(tmp_path / "nope.sqlite3")
    p = build(tmp_path)
    p.db.unlink()
    for ext in ("-wal", "-shm"):
        (tmp_path / ("j.sqlite3" + ext)).unlink(missing_ok=True)
    with Running(p) as c:
        d = c.api("/overview")["data"]
        assert d["attention"]["items"][0]["code"] == "JOURNAL_MISSING"
        st, body, _ = c.get("/")
        assert st == 200 and "JOURNAL_MISSING" in body
        assert c.get("/positions/P-OPEN")[0] == 404
        assert c.get("/health")[0] == 200 and c.get("/alerts")[0] == 200 and c.get("/runs")[0] == 200
    assert not p.db.exists()


# ---------------------------------------------------------------- WD-AT-02 overview matches backend
def test_overview_numbers_match_backend_and_decimals_are_exact(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        r = c.api("/overview")
        d, m = r["data"], r["meta"]
    from csp_alerts.policy.portfolio import compute_exposure
    j = Journal(p.db, clock=lambda: NOW)
    ex = compute_exposure(j.load_book())
    j.close()
    assert d["exposure"]["put_collateral_usd"] == format(ex.put_collateral_usd, "f") == "7000"
    assert d["exposure"]["aggregate_usd"] == format(ex.aggregate_equity_risk_usd, "f") == "8000"
    assert d["latest_confirmed_snapshot"]["equity_usd"] == "100000" and d["latest_confirmed_snapshot"]["free_cash_usd"] == "60000"
    lim = {x["name"]: x for x in d["limits"]}
    pil = lim["Pilot put-collateral cap"]
    assert pil["limit"] == "15000.00" and pil["room"] == "8000.00" and pil["binding"] is True     # exact, no float
    assert all(isinstance(v, str) for v in (pil["used"], pil["limit"], pil["room"]))
    assert d["account"]["scope"] == "paper_primary" and d["account"]["profile"] == "pilot"
    assert m["policy_sha256"] == policy().sha256 and isinstance(m["ledger_revision"], int) and m["code_revision"]
    assert d["drawdown"]["level"] == "NONE" and d["reconciliation"]["blocking"] is False
    assert [o["trade_id"] for o in d["obligations"]] == ["P-PEND"]          # reserved/pending orders stay visible
    assert d["exposure"]["note"].startswith("Exposure budget")


def test_overview_page_labels_and_blockers_come_first(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        st, body, _ = c.get("/")
    assert body.index("Current blockers and next manual steps") < body.index("<h2>Account</h2>")
    assert "not maximum possible loss" in body and "Page refreshed (not data time)" in body
    assert "(2026-10-30T15:05:00Z)" in body and "CDT" in body            # Central + UTC evidence


def test_no_snapshot_means_unavailable_not_zero(tmp_path):
    p = build(tmp_path, scan=False)
    j = Journal(p.db, clock=lambda: NOW)
    j.conn.execute("DELETE FROM account_snapshots")
    j.close()
    with Running(p) as c:
        d = c.api("/overview")["data"]
        assert d["latest_snapshot"] is None and d["drawdown"]["peak_equity_usd"] is None and d["drawdown"]["drawdown_fraction"] is None
        _, body, _ = c.get("/")
        assert "unavailable" in body and "NO_SNAPSHOT" in body


# ---------------------------------------------------------------- WD-AT-03 distinct states, no invented candidates
def test_distinct_states_for_empty_whitelist_blocked_partial_outage_and_no_contracts(tmp_path):
    p = sub(tmp_path, "a", tickers=())
    with Running(p) as c:
        s = states(c)
        assert "EMPTY_WHITELIST" in s and not s & {"NO_QUALIFYING_CONTRACTS", "PROVIDER_OUTAGE", "PARTIAL_COVERAGE"}
        assert c.api("/candidates")["data"]["rows"] == []
    p = sub(tmp_path, "b", chains={"AAA": [raw_row(delta=-0.5), raw_row(strike=41.0, delta=-0.6)]})
    with Running(p) as c:
        d = c.api("/candidates")["data"]
        assert {x["code"] for x in d["states"]} == {"NO_QUALIFYING_CONTRACTS"}
        assert d["counts"]["passing"] == 0 and d["counts"]["rejected"] == 2 and d["rows"] == []
        assert "DELTA_OUT_OF_RANGE" in {r["reason"] for r in d["reason_histogram"]}
    p = sub(tmp_path, "c", tickers=("AAA", "ZZZ"), chains={"AAA": [raw_row(delta=-0.5)], "ZZZ": []})
    with Running(p) as c:
        d = c.api("/candidates")["data"]
        assert {x["code"] for x in d["states"]} == {"PARTIAL_COVERAGE"}
        assert [i["ticker"] for i in d["not_evaluated"]] == ["ZZZ"] and d["coverage"]["ok"] == ["AAA"]
    p = sub(tmp_path, "d", tickers=("AAA", "ZZZ"), chains={"AAA": [], "ZZZ": []})
    with Running(p) as c:
        d = c.api("/candidates")["data"]
        assert {x["code"] for x in d["states"]} == {"PROVIDER_OUTAGE"} and d["rows"] == []
        body = c.get("/candidates")[1]
        assert "Provider outage" in body and "NOT a no-trade" in body
    p = sub(tmp_path, "e")
    with Running(p, now=NOW + timedelta(days=3)) as c:       # a stale snapshot blocks entries
        assert "BLOCKED_LEDGER" in states(c)
        assert any(i["code"] == "SNAPSHOT_STALE" for i in c.api("/")["data"]["attention"]["items"])


def test_passing_decision_becomes_stale_history_not_a_live_alternative(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        d = c.api("/candidates")["data"]
        assert d["counts"]["passing"] == 1 and d["rows"][0]["bucket"] == "pass" and d["selected_run"]["stale"] is False
    with Running(p, now=NOW + timedelta(minutes=45)) as c:      # beyond the 30-minute entry-alert expiry
        d = c.api("/candidates")["data"]
        assert d["selected_run"]["stale"] is True and d["counts"]["passing"] == 0 and d["counts"]["stale"] == 1
        assert d["rows"] == []                                   # not listed as a live alternative
        r = c.api("/candidates?bucket=stale")["data"]["rows"][0]
        assert r["bucket"] == "stale"
        assert r["data_tier"] == "INDICATIVE_UNTIMESTAMPED" and r["quote_at_utc"] is None   # refresh never upgrades the tier
        body = c.get(f"/candidates/{r['evaluation_id']}")[1]
        assert "Exchange quote time" in body and "unavailable" in body


def test_candidate_detail_has_context_and_filters(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        row = c.api("/candidates")["data"]["rows"][0]
        d = c.api(f"/candidates/{row['evaluation_id']}")["data"]
        assert d["approval"]["ticker"] == "AAA" and d["observation"]["bid"] == "0.5" and d["decision"]["policy_version"] == "1.1.0"
        assert c.api("/candidates?ticker=NOPE")["data"]["rows"] == []
        assert c.get("/candidates?bucket=bogus")[0] == 400
        assert c.get("/candidates/does-not-exist")[0] == 404


# ---------------------------------------------------------------- positions: unavailable is not zero
def test_positions_list_detail_states_and_unavailable_pnl(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        live = {r["trade_id"]: r for r in c.api("/positions")["data"]["rows"]}
        assert set(live) == {"P-OPEN", "P-PEND"}
        allp = {r["trade_id"]: r for r in c.api("/positions?group=all")["data"]["rows"]}
        assert allp["P-DONE"]["state"] == "CLOSED" and allp["P-DONE"]["warnings"] == []
        assert "no open-position advice" in allp["P-DONE"]["advice_note"]
        assert live["P-OPEN"]["pnl"] is None and live["P-OPEN"]["price_check"]["status"] == "unavailable"
        d = c.api("/positions/P-OPEN")["data"]
        assert d["position"]["open_credit_per_share"] == "0.50" and d["position"]["fees_usd"] == "0.65"
        assert {a["alert_id"] for a in d["alerts"]} == {"alert-1", "alert-2"}
        assert d["audit"][-1]["event_type"] == "POSITION_DRAFT_CREATED"
        assert c.api("/positions?group=history")["data"]["rows"][0]["trade_id"] == "P-DONE"
        assert c.get("/positions/NOPE")[0] == 404 and c.get("/positions?group=zzz")[0] == 400


def test_indicative_pnl_only_with_a_fresh_stored_quote(tmp_path):
    p = build(tmp_path)
    j = Journal(p.db, clock=lambda: NOW)
    obs = {"contract_key": "BBB|%s|P|40.00|100" % (TODAY + timedelta(days=40)).isoformat(), "retrieved_at_utc": "2026-10-30T15:00:00Z",
           "quote_at_utc": None, "provider": "YAHOO_VIA_STOCKPILE", "data_tier": "INDICATIVE_UNTIMESTAMPED", "bid": "0.15",
           "ask": "0.18", "delta": "-0.10", "spot": "50"}
    j.record_observations([("o1", obs, "sha")])
    j.close()
    with Running(p) as c:
        pos = c.api("/positions/P-OPEN")["data"]["position"]
        assert pos["price_check"]["status"] == "usable"
        assert pos["pnl"]["gross_usd"] == "32.00" and pos["pnl"]["net_of_fees_usd"] == "31.35"
        assert "PROFIT_EXIT" in {w["trigger"] for w in pos["warnings"]}
    with Running(p, now=NOW + timedelta(hours=2)) as c:       # evidence aged out: unavailable, never a stale number
        pos = c.api("/positions/P-OPEN")["data"]["position"]
        assert pos["pnl"] is None and pos["price_check"]["status"] == "unavailable"


def test_calendar_warnings_need_no_quote(tmp_path):
    p = build(tmp_path)
    with Running(p, now=NOW + timedelta(days=22)) as c:      # 18 days to expiry: TIME_EXIT (<=21 DTE) with no quote at all
        pos = c.api("/positions/P-OPEN")["data"]["position"]
        assert "TIME_EXIT" in {w["trigger"] for w in pos["warnings"]} and pos["pnl"] is None


def test_assignment_pending_stays_visible_with_obligation(tmp_path):
    p = build(tmp_path)
    j = Journal(p.db, clock=lambda: NOW)
    j.transition("P-OPEN", S.ASSIGNED_PENDING_RECONCILIATION)
    j.close()
    with Running(p) as c:
        d = c.api("/")["data"]
        assert d["exposure"]["assigned_pending_usd"] == "4000"
        assert "P-OPEN" in {o["trade_id"] for o in d["obligations"]}
        assert "ASSIGNMENT_PENDING" in {i["code"] for i in d["attention"]["items"]}
        assert c.api("/positions/P-OPEN")["data"]["position"]["warnings"][0]["trigger"] == "ASSIGNMENT_PENDING_RECONCILIATION"


# ---------------------------------------------------------------- alerts, health
def test_alert_delivery_states_are_distinct_and_empty_state_is_explained(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        rows = {r["alert_id"]: r for r in c.api("/alerts")["data"]["rows"]}
        assert rows["alert-0"]["delivery_state"] == "acknowledged by Telegram API"
        assert rows["alert-1"]["delivery_state"].startswith("unknown acknowledgment")
        assert rows["alert-2"]["delivery_state"].startswith("dropped") and rows["alert-3"]["delivery_state"] == "retry scheduled"
        assert [r["alert_id"] for r in c.api("/alerts?status=SENT")["data"]["rows"]] == ["alert-0"]
        assert "does not prove" in c.get("/alerts")[1]
    p2 = sub(tmp_path, "x")
    j = Journal(p2.db, clock=lambda: NOW)
    j.conn.execute("DELETE FROM alert_outbox")
    j.close()
    with Running(p2) as c:
        assert "Dry-run" in c.get("/alerts")[1]


def test_health_never_implies_tasks_are_installed(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        d = c.api("/health")["data"]
        assert "does NOT show that any Windows scheduled task" in d["next_runs"]["basis"]
        assert any("Windows scheduled tasks: not checked" in u for u in d["unverified"])
        assert d["runs"]["last_scan_completed"]["status"] == "CANDIDATES" and d["runs"]["last_monitor_attempt"] is None
        assert d["runs"]["last_scan_attempt"]["policy_sha256"] == policy().sha256
        body = c.get("/health")[1]
        assert "policy-derived estimate, NOT proof a task is installed" in body and "never recorded" in body
        assert "stopped or sleeping PC cannot announce" in body


# ---------------------------------------------------------------- WD-003: run history without a migration
def test_scan_run_record_carries_identity_and_coverage(tmp_path):
    p = build(tmp_path)
    j = Journal(p.db)
    r = j.last_run("SCAN")
    j.close()
    s = json.loads(r["summary_json"])
    assert s["policy_sha256"] == policy().sha256 and s["scope"] == "paper_primary" and isinstance(s["ledger_revision"], int)
    assert s["code_revision"].split(":")[0] in ("git", "manifest") and s["coverage"] == [{"ticker": "AAA", "status": "OK", "detail": "2 puts"}]


def test_old_run_rows_without_identity_read_as_not_recorded_and_schema_is_untouched(tmp_path):
    p = build(tmp_path, scan=False)
    j = Journal(p.db, clock=lambda: NOW)
    j.record_run("old-run", "SCAN", NOW - timedelta(hours=2), "NO_OPPORTUNITIES", ["EMPTY_WHITELIST"], {"tickers": 0})
    j.close()
    with Running(p) as c:
        rows = c.api("/runs")["data"]["rows"]
        assert rows[0]["recorded_identity"] is False and rows[0]["policy_sha256"] is None
        assert "not recorded" in c.get("/runs")[1] and "not recorded" in c.get("/runs/old-run")[1]
        assert c.api("/overview")["meta"]["schema_version"] == "0005"
    j = Journal(p.db)
    assert j.schema_version() == "0005"
    j.close()


def test_monitor_cli_records_identity(tmp_path, monkeypatch):
    from csp_alerts.cli import main
    db = tmp_path / "m.sqlite3"
    monkeypatch.setenv("CSP_DATABASE_URL", "sqlite:///" + db.as_posix())
    monkeypatch.setenv("CSP_DRY_RUN", "true")
    nofile = str(tmp_path / "none.env")
    assert main(["--env-file", nofile, "journal", "init"]) == 0
    assert main(["--env-file", nofile, "monitor"]) == 0
    j = Journal(db)
    r = j.last_run("MONITOR")
    j.close()
    s = json.loads(r["summary_json"])
    assert r["status"] == "NO_POSITIONS" and s["policy_sha256"] == policy().sha256 and "code_revision" in s


# ---------------------------------------------------------------- escaping and static content
def test_untrusted_text_is_escaped_everywhere(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        for path in ("/positions/P-OPEN", "/alerts/alert-0", "/alerts", "/activity", "/positions?group=all", "/runs", "/health", "/"):
            st, body, _ = c.get(path)
            assert st == 200
            assert "<script" not in body.lower() and "‮" not in body and "\x1b" not in body, path
        body = c.get("/positions/P-OPEN")[1]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "&amp;amp;" in body
        assert "&lt;script&gt;" in c.get("/alerts/alert-0")[1]
        assert c.api("/positions/P-OPEN")["data"]["position"]["notes"] == EVIL     # JSON keeps raw data, never markup


def test_pages_have_no_scripts_or_external_assets(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        for path in ("/", "/candidates", "/positions", "/alerts", "/activity", "/runs", "/health"):
            body = c.get(path)[1]
            assert "<script" not in body.lower() and "javascript:" not in body.lower() and "onclick" not in body.lower()
            assert not re.search(r'(src|href|action)="(https?:)?//', body), path
        css = c.get("/static/app.css")[1]
        assert "@import" not in css and "url(" not in css


# ---------------------------------------------------------------- pagination (WD-AT-06)
def test_keyset_pagination_does_not_lose_or_duplicate_when_rows_arrive(tmp_path):
    p = build(tmp_path)
    j = Journal(p.db, clock=lambda: NOW)
    for i in range(120):
        j.audit_manual("cmd", "owner", f"r{i}", {"i": i})
    total = j.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    j.close()
    with Running(p) as c:
        first = c.api("/activity")["data"]
        assert len(first["rows"]) == 50 and first["newer"] is None
        seen = [r["event_id"] for r in first["rows"]]
        j = Journal(p.db, clock=lambda: NOW)                    # new events arrive between page loads
        for i in range(7):
            j.audit_manual("cmd", "owner", f"late{i}", {})
        j.close()
        cur = first["older"]
        while cur:
            pg = c.api(f"/activity?before={cur}")["data"]
            seen += [r["event_id"] for r in pg["rows"]]
            assert pg["newer"] is not None
            cur = pg["older"]
        assert len(seen) == total and len(set(seen)) == total   # nothing lost, nothing repeated
        back = c.api(f"/activity?after={first['rows'][0]['rid']}")["data"]
        assert len(back["rows"]) == 7                           # the newer rows are reachable too
        assert c.get("/activity?before=abc")[0] == 400
        assert len(c.api("/activity?event_type=MANUAL_COMMAND")["data"]["rows"]) == 50


# ---------------------------------------------------------------- consistent read during a write (WD-AT-06)
def test_reads_are_coherent_while_a_writer_commits(tmp_path):
    p = build(tmp_path, scan=False)
    stop = threading.Event()
    errors: list[str] = []

    def writer():
        j = Journal(p.db, clock=lambda: NOW)
        i = 0
        while not stop.is_set() and i < 150:
            j.create_draft(NewPosition(f"W-{i}", "WWW", TODAY + timedelta(days=40), D("10"), 100, 1, "s", "b"))  # position + audit, ONE tx
            i += 1
        j.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        with Running(p) as c:
            for _ in range(40):
                rj = ReadOnlyJournal(p.db)
                with rj.snapshot():
                    pos = rj.conn.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0]
                    aud = rj.conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='POSITION_DRAFT_CREATED'").fetchone()[0]
                    pos2 = rj.conn.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0]
                rj.close()
                if not (pos == aud == pos2):
                    errors.append(f"incoherent snapshot {pos} {aud} {pos2}")
                r = c.api("/activity")
                rows = r["data"]["rows"]
                if rows and rows[0]["rid"] != r["meta"]["audit_revision"]:
                    errors.append("response mixes revisions")
    finally:
        stop.set()
        t.join()
    assert errors == []


def test_refresh_rereads_persisted_state(tmp_path):
    p = build(tmp_path)
    with Running(p) as c:
        before = c.api("/")["meta"]["audit_revision"]
        j = Journal(p.db, clock=lambda: NOW)
        j.audit_manual("cmd", "owner", "later", {})
        j.close()
        assert c.api("/")["meta"]["audit_revision"] == before + 1
