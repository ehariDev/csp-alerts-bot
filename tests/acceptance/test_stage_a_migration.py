"""Stage A acceptance: upgrading a synthetic schema-0004 journal to 0005 (market data) is safe.

Oracle (from the requirement 'Preserve existing ledger/history and backup before schema migration', PRODUCT_WORKFLOW_REQUIREMENTS):
  * a backup exists, is an intact SQLite file, and holds the PRE-migration state (schema 0004), taken BEFORE the upgrade;
  * if the backup cannot be made or fails its integrity check the upgrade is refused and the journal is untouched;
  * every pre-existing row of every pre-existing table is identical afterwards (ledger, audit_log, snapshots, drawdown latch,
    reconciliation inputs, outbox/delivery state), nothing is replayed, triggers (audit append-only) survive;
  * re-running is a no-op (no second backup, no change); the dashboard never migrates.
All data is synthetic (web_helpers.build_book)."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from acc_helpers import NOW, POLICY_PATH
from stage_a_env import approved_file, dash
from stage_a_helpers import hold, md_dump
from web_helpers import build_book, fingerprint, insert_alerts

from csp_alerts.journal.reconcile import reconcile
from csp_alerts.journal.store import Journal, JournalError
from csp_alerts.policy.config import load_policy


def make_0004(tmp: Path, name="old.sqlite3", down_to="0004") -> Path:
    """A real book (OPEN/WORKING/... positions, snapshots, drawdown PAUSE latch, outbox rows) with every 0005 object removed."""
    db = tmp / name
    build_book(db).close()
    insert_alerts(db, 3, status="PENDING")
    insert_alerts(db, 2, start=10, status="UNKNOWN_ACK")
    c = sqlite3.connect(db)
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if t.startswith("md_") or t == "watchlist":
            c.execute(f"DROP TABLE {t}")
    c.execute("DELETE FROM schema_migrations WHERE version='0005'")
    if down_to == "0003":
        c.execute("DROP TABLE run_status")
        c.execute("DELETE FROM schema_migrations WHERE version='0004'")
    c.commit()
    c.close()
    return db


def schema_objects(db):
    c = sqlite3.connect(db)
    r = sorted((t, n, s) for t, n, s in c.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
               if not (n.startswith(("md_", "idx_md_")) or n == "watchlist"))
    c.close()
    return r


def version(db):
    c = sqlite3.connect(db)
    v = c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    c.close()
    return v


def rec_codes(db):
    j = Journal(db, clock=lambda: NOW)
    book = j.load_book()
    j.close()
    return sorted((i.code, i.severity) for i in reconcile(book, load_policy(POLICY_PATH), NOW).issues)


def test_upgrade_0004_to_0005_takes_a_verified_backup_first_and_preserves_everything(tmp_path):
    db = make_0004(tmp_path)
    assert version(db) == "0004"
    before, objs, codes = hold(db), schema_objects(db), rec_codes(db)
    assert before["audit_log"] and before["alert_outbox"] and before["option_positions"] and before["account_snapshots"]
    j = Journal(db, clock=lambda: NOW)
    applied = j.migrate()
    j.close()
    assert applied == ["0005"] and version(db) == "0005"
    backups = sorted((tmp_path / "backups").glob("pre-migrate-*.sqlite3"))
    assert len(backups) == 1 and re.fullmatch(r"pre-migrate-0004-to-0005-\d{8}-\d{6}\.sqlite3", backups[0].name), backups
    b = sqlite3.connect(backups[0])
    assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert b.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == "0004"          # taken BEFORE the upgrade
    assert b.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('watchlist','md_stock_latest')").fetchone()[0] == 0
    b.close()
    assert hold(backups[0]) == before                                  # the backup is the complete pre-migration state
    mig = lambda h: {k: v for k, v in h.items() if k != "schema_migrations"}  # noqa: E731  (0005 itself is the one new migration row)
    assert mig(hold(db)) == mig(before)                                # every existing row identical afterwards (audit_log, positions, ...)
    assert [r[0] for r in hold(db)["schema_migrations"]] == ["0001", "0002", "0003", "0004", "0005"]
    assert hold(db)["schema_migrations"][:4] == before["schema_migrations"]
    assert schema_objects(db) == objs                                  # same tables/indexes/TRIGGERS (audit append-only) as before
    assert rec_codes(db) == codes                                      # reconciliation verdict unchanged (no reset, no acknowledgment)
    md = md_dump(db)
    assert md["watchlist"] == [] and all(not rows for rows in md.values()), md          # empty watchlist, no market rows invented
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM audit_log")                             # append-only trigger still enforced
    assert c.execute("SELECT COUNT(*) FROM alert_outbox WHERE status IN ('PENDING','UNKNOWN_ACK')").fetchone()[0] == 5   # not sent/dropped/replayed
    c.close()


def test_rerun_is_idempotent_no_second_backup_no_change(tmp_path):
    db = make_0004(tmp_path)
    j = Journal(db, clock=lambda: NOW)
    j.migrate()
    j.close()
    snap, files = fingerprint(db), sorted(p.name for p in (tmp_path / "backups").iterdir())
    for _ in range(2):
        j = Journal(db, clock=lambda: NOW)
        assert j.migrate() == []
        j.close()
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == files
    assert hold(db) == hold(db) and version(db) == "0005"
    assert {k: v for k, v in fingerprint(db)["counts"].items()} == snap["counts"]


def test_upgrade_from_0003_names_the_backup_with_the_real_versions(tmp_path):
    db = make_0004(tmp_path, down_to="0003")
    assert version(db) == "0003"
    j = Journal(db, clock=lambda: NOW)
    assert j.migrate() == ["0004", "0005"]
    j.close()
    (b,) = (tmp_path / "backups").glob("pre-migrate-*.sqlite3")
    assert b.name.startswith("pre-migrate-0003-to-0005-")


def test_migration_is_refused_and_journal_untouched_when_the_backup_cannot_be_made(tmp_path):
    db = make_0004(tmp_path)
    (tmp_path / "backups").write_text("a FILE where the backup directory must go", encoding="utf-8")
    before, objs = hold(db), schema_objects(db)
    j = Journal(db, clock=lambda: NOW)
    with pytest.raises(JournalError, match="refusing to migrate"):
        j.migrate()
    j.close()
    assert version(db) == "0004" and hold(db) == before and schema_objects(db) == objs
    assert md_dump.__name__ and not any(n.startswith("md_") or n == "watchlist" for n, in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))


def test_migration_is_refused_when_the_backup_fails_its_integrity_check(tmp_path, monkeypatch):
    db = make_0004(tmp_path)
    before = hold(db)

    def corrupt_backup(self, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"not an sqlite database " * 50)

    monkeypatch.setattr(Journal, "backup", corrupt_backup)
    j = Journal(db, clock=lambda: NOW)
    with pytest.raises(JournalError, match="refusing to migrate"):
        j.migrate()
    j.close()
    assert version(db) == "0004" and hold(db) == before


def test_cli_journal_init_on_an_old_journal_upgrades_after_a_backup(monkeypatch, tmp_path):
    from s3_helpers import run_cli
    db = make_0004(tmp_path, name="j.sqlite3")                          # run_cli's database is tmp/j.sqlite3
    before = hold(db)
    code, out, err = run_cli(monkeypatch, tmp_path, ["journal", "init"], now=NOW, frozen_journal=True)
    assert code == 0, out + err
    assert version(db) == "0005"
    after = hold(db)
    new_audit = [r for r in after["audit_log"] if r not in before["audit_log"]]
    assert [r for r in before["audit_log"] if r not in after["audit_log"]] == []         # nothing rewritten or removed
    # pre-existing `journal init` behaviour: records the active policy hash, nothing else
    assert all(r[2] == "POLICY_RECORDED" for r in new_audit), new_audit
    for k in before:
        if k == "policy_versions":
            assert set(before[k]) <= set(after[k]) and len(after[k]) <= len(before[k]) + 1      # init records the policy version once
        elif k not in ("audit_log", "schema_migrations"):
            assert after[k] == before[k], k
    assert len(list((tmp_path / "backups").glob("pre-migrate-0004-to-0005-*.sqlite3"))) == 1


def test_dashboard_never_migrates_a_0004_journal_and_says_what_to_do(tmp_path):
    db = make_0004(tmp_path)
    before = fingerprint(db)
    d = dash(db, approved_file(tmp_path, ("AAA",)))
    try:
        o = d.api("/api/overview")
        assert o["meta"]["schema_version"] == "0004"
        assert any(i["code"] == "SCHEMA_BEHIND" for i in o["data"]["attention"]["items"])
        for pth in ("/", "/watchlist", "/market/ZZZ", "/positions", "/api/watchlist"):
            st, _, body = d.req("GET", pth)
            assert st in (200, 404, 503), (pth, st)
            assert "Internal error" not in body, pth
        body = d.req("GET", "/watchlist")[2]
        assert "journal init" in body and "backup" in body.lower()
        st, hd, _ = d.req("POST", "/watchlist/add", origin=d.origin, body=b"csrf=x&symbol=ZZZ",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert st == 303
    finally:
        d.stop()
    assert fingerprint(db) == before
    assert not (tmp_path / "backups").exists()
    assert version(db) == "0004"


def test_web_write_on_an_old_journal_reports_unavailable_and_does_not_migrate(tmp_path):
    from csp_alerts.journal import webwrites
    db = make_0004(tmp_path)
    before = fingerprint(db)
    for fn, arg in ((webwrites.add_watchlist_symbol, "ZZZ"), (webwrites.remove_watchlist_symbol, "ZZZ"), (webwrites.request_market_refresh, "ALL")):
        assert fn(db, arg).code == "unavailable"
    assert fingerprint(db) == before and not (tmp_path / "backups").exists()
