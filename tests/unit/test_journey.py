"""Focused first-delivery gate: owner journey on synthetic data and backup/restore (run with -s to see the transcript)."""
from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from chain_fixtures import raw_row
from fake_telegram import FakeTelegram

from csp_alerts import runner
from csp_alerts.alerts import outbox
from csp_alerts.alerts.sendmode import Mode, SendDecision
from csp_alerts.alerts.telegram import TelegramTransport, chat_id_sha256
from csp_alerts.alerts.templates import AlertMessage
from csp_alerts.cli import main
from csp_alerts.jobs.monitor import run_monitor
from csp_alerts.jobs.scan import run_scan
from csp_alerts.market.models import SessionInfo, SessionStatus
from csp_alerts.market.stockpile import RawChain
from csp_alerts.policy.approved import load_approved
from csp_alerts.policy.config import load_policy
from csp_alerts.policy.drawdown import DrawdownLevel, compute_drawdown
from csp_alerts.journal.models import AccountSnapshot, PositionState as S
from csp_alerts.journal.store import Journal

ROOT = Path(__file__).resolve().parents[2]
OPEN = SessionInfo(SessionStatus.OPEN, 300)
POL = load_policy(ROOT / "config" / "policy.yaml")


class Src:
    def __init__(self, rows):
        self.rows = rows

    def fetch_chain(self, ticker, a, b):
        return RawChain(ticker, list(self.rows), datetime.now(timezone.utc))

    def fetch_earnings_dates(self, t):
        return None


def say(msg):
    print(f"[journey] {msg}")


class Env:
    def __init__(self, tmp):
        self.tmp = tmp
        (tmp / ".env").write_text(f"CSP_DATABASE_URL=sqlite:///{(tmp / 'db.sqlite3').as_posix()}\n", encoding="utf-8")
        self.approved = tmp / "approved.json"
        self.approved.write_text('{"approved_underlyings": []}', encoding="utf-8")
        self.base = ["--env-file", str(tmp / ".env"), "--policy", str(ROOT / "config" / "policy.yaml"), "--approved", str(self.approved)]

    def cli(self, *a, apply=False):
        extra = ["--apply", "--reason", "journey"] if apply else []
        return main(self.base + list(a) + extra)

    def journal(self):
        return Journal(self.tmp / "db.sqlite3")


def test_owner_journey_reconcile_scan_record_monitor_close(tmp_path, capsys):
    e = Env(tmp_path)
    today = date.today()
    exp = (today + timedelta(days=40)).isoformat()
    # 1. reconcile: baseline, approval, first confirmed check-in
    assert e.cli("journal", "init") == 0 and e.cli("journal", "confirm-baseline", "--equity", "100000") == 0
    assert e.cli("journal", "approve", "--ticker", "ABC", "--asset-type", "ETF", "--sector", "broad", "--bucket", "us",
                 "--max-buy-strike", "50", "--approved-through", (today + timedelta(days=60)).isoformat(),
                 "--events-reviewed-on", today.isoformat(), "--events-cleared-through", (today + timedelta(days=90)).isoformat(),
                 "--ownership-reason", "owner diligence notes (journey test)", "--standard-deliverable-confirmed-on", today.isoformat(),
                 apply=True) == 0
    assert e.cli("daily-check-in", "--equity", "100000", "--free-cash", "100000", "--ack-reconciled", apply=True) == 0
    assert e.cli("reconcile-check") == 0
    say("1. reconcile-check: OK ledger reconciled")
    capsys.readouterr()

    # 2. scan preview (synthetic chain; dry-run: nothing queued)
    j = e.journal()
    now = datetime.now(timezone.utc)
    res = run_scan(journal=j, policy=POL, approved=load_approved(e.approved), source=Src([raw_row(strike=40.0, exp=exp)]),
                   now_utc=now, session=OPEN, sleep=lambda s: None)
    assert res.status == "CANDIDATES" and [m.kind for m in res.messages] == ["ENTRY_RESEARCH"]
    assert "NO ORDER SENT" in res.messages[0].text and j.conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0] == 0
    say(f"2. scan preview: status={res.status}, 1 candidate, outbox rows={0} (dry-run), first lines: "
        + " / ".join(res.messages[0].text.replace('<b>', '').replace('</b>', '').split(chr(10))[:2]))
    j.close()

    # 3. record the paper position (owner acted manually) and re-confirm
    assert e.cli("journal", "add-trade", "--trade-id", "PAPER-1", "--ticker", "ABC", "--expiration", exp, "--strike", "40", apply=True) == 0
    assert e.cli("journal", "fill", "--trade-id", "PAPER-1", "--credit", "0.50", "--fees", "0.65", apply=True) == 0
    assert e.cli("reconcile-check") == 1                                        # F3: blocked until re-confirmed
    import time as _t
    _t.sleep(1.1)
    assert e.cli("daily-check-in", "--equity", "100000", "--free-cash", "96000", "--ack-reconciled", apply=True) == 0
    assert e.cli("reconcile-check") == 0
    say("3. recorded PAPER-1 (PENDING -> OPEN), reconcile-check blocked until the check-in, then OK")

    # 4. monitor warning: 60% of the premium captured (ask 0.20 vs credit 0.50) on a synthetic quote
    j = e.journal()
    mon = run_monitor(journal=j, policy=POL, source=Src([raw_row(strike=40.0, bid=0.15, ask=0.20, exp=exp)]),
                      now_utc=datetime.now(timezone.utc), session=OPEN)
    assert mon.status == "ALERTS" and [m.kind for m in mon.messages] == ["PROFIT_EXIT"]
    assert "INDICATIVE - quote age UNKNOWN" in mon.messages[0].text
    say("4. monitor: status=ALERTS, PROFIT_EXIT (indicative, quote age UNKNOWN, NO ORDER SENT)")
    j.close()

    # 5. record the close; the position stops receiving open-risk alerts and collateral is released
    assert e.cli("journal", "close", "--trade-id", "PAPER-1", "--debit", "0.20", "--fees", "0.65", apply=True) == 0
    j = e.journal()
    assert j.get_position("PAPER-1").state is S.CLOSED
    mon2 = run_monitor(journal=j, policy=POL, source=Src([raw_row(strike=40.0, bid=0.15, ask=0.20, exp=exp)]),
                       now_utc=datetime.now(timezone.utc), session=OPEN)
    assert mon2.status == "NO_POSITIONS" and mon2.messages == []
    from csp_alerts.policy.portfolio import compute_exposure
    assert compute_exposure(j.load_book()).put_collateral_usd == 0
    say("5. recorded close: PAPER-1 CLOSED, monitor status=NO_POSITIONS (no open-risk alerts), collateral released")
    j.close()


def test_backup_restore_does_not_replay_entries_or_clear_a_halt(tmp_path, capsys):
    src_dir, dst_dir = tmp_path / "live", tmp_path / "restored"
    src_dir.mkdir(); dst_dir.mkdir()
    live = Env(src_dir)
    assert live.cli("journal", "init") == 0 and live.cli("journal", "confirm-baseline", "--equity", "100000") == 0
    j = live.journal()
    j.add_snapshot(AccountSnapshot("s1", datetime.now(timezone.utc) - timedelta(minutes=2), D("90000"), D("90000"), True, "t"))
    # an entry alert queued 2 hours ago that has not been sent yet
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    outbox.enqueue(j, AlertMessage("ENTRY_RESEARCH", "INFO", "stale entry", "ENTRY|ABC", meta={"ticker": "ABC", "approval_sha": "x"}),
                   SendDecision(Mode.SEND), now=old, scope="paper_primary", policy_sha=POL.sha256, expires_in_minutes=30,
                   revalidate={"ticker": "ABC", "approval_sha": "x", "ledger_marker": ""})
    j.close()
    backup = tmp_path / "bk" / "backup.sqlite3"
    assert live.cli("journal", "backup", "--output", str(backup)) == 0
    say(f"backup written ({backup.stat().st_size} bytes), integrity checked by the command")

    # disaster: restore the backup onto a new machine/folder
    restored = Env(dst_dir)
    shutil.copy(backup, dst_dir / "db.sqlite3")
    j = restored.journal()
    book = j.load_book()
    st = compute_drawdown(book.snapshots, POL, baseline_asof_utc=book.last_risk_reset_utc,
                          baseline_equity_usd=book.baseline_equity_usd, cash_flows=book.cash_flows)
    assert st.level is DrawdownLevel.HARD_HALT and st.latched                      # halt survives restore
    res = run_scan(journal=j, policy=POL, approved=[], source=Src([]), now_utc=datetime.now(timezone.utc), session=OPEN)
    assert res.status == "BLOCKED" and res.messages[0].severity == "CRITICAL"       # still blocked: no entries
    say("restored journal: drawdown level HARD_HALT (latched), scan status=BLOCKED, CRITICAL account-risk message")

    tg = FakeTelegram()
    try:
        j.pin_chat(chat_id_sha256("987654321"), "private")
        t = TelegramTransport("123456789:ABCdefGhIJKlmnoPQRstuVWXyz0123", "987654321", base_url=tg.url, allow_insecure_base_for_tests=True)
        rep = outbox.flush(j, t, now=datetime.now(timezone.utc),
                           revalidator=runner.make_revalidator(j, POL, lambda: [], lambda: datetime.now(timezone.utc)))
        assert rep.sent == 0 and rep.dropped == 1 and tg.requests == []             # the old entry alert is NOT replayed
        row = j.conn.execute("SELECT status FROM alert_outbox").fetchone()
        assert row["status"] == "DROPPED_EXPIRED"
        say("restored outbox: queued 2h-old entry alert -> DROPPED_EXPIRED, 0 Telegram requests (no replay)")
    finally:
        tg.close()
    j.close()
    assert restored.cli("status") == 0
    assert "HARD_HALT" in capsys.readouterr().out
    # only the owner's manual reset clears it
    assert restored.cli("journal", "reset", "--reviewed", apply=True) == 0
    j = restored.journal()
    b2 = j.load_book()
    assert compute_drawdown(b2.snapshots, POL, baseline_asof_utc=b2.last_risk_reset_utc, baseline_equity_usd=b2.baseline_equity_usd,
                            cash_flows=b2.cash_flows).level is DrawdownLevel.NONE
    say("manual `journal reset --reviewed` is the only thing that clears the halt")
    j.close()
