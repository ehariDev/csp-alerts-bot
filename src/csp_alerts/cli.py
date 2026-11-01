"""Operator CLI (spec 14). Read-only research tooling and an audited manual journal.

There is deliberately no command that trades, syncs a brokerage account, or sends Telegram
messages in this slice. Run from the repository root.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from csp_alerts import __version__, boundary
from csp_alerts.settings import Settings, StartupError, load_settings

DEFAULT_POLICY = "config/policy.yaml"
DEFAULT_APPROVED = "config/approved.json"
REMINDER_FREE_CASH = ("REMINDER: enter NET free unencumbered cash in each snapshot (cash genuinely free for NEW puts, "
                      "after reserving collateral for existing/pending puts).")


def _send_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--send", action="store_true", help="explicit send; also requires CSP_DRY_RUN=false and a pinned private chat")
    p.add_argument("--dry-run", action="store_true", help="print only (default); combining with --send is an error")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="csp-alerts", description="Advisory-only cash-secured put research tooling.")
    p.add_argument("--env-file", default=".env", help="path to .env (default: .env)")
    p.add_argument("--policy", default=DEFAULT_POLICY)
    p.add_argument("--approved", default=DEFAULT_APPROVED)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="validate environment, policy, whitelist and journal; no network")
    sub.add_parser("policy-check", help="validate config/policy.yaml and print its hash")
    sub.add_parser("approved-list", help="show the owner-approved underlying whitelist")
    sub.add_parser("reconcile-check", help="report manual-ledger problems that block new entry alerts")
    sub.add_parser("account-check", help="show latest manual snapshot and drawdown state")

    sc = sub.add_parser("scan", help="entry research scan (dry-run by default; --send queues and sends to Telegram)")
    _send_flags(sc)
    sc.add_argument("--scheduled-for", default=None, metavar="HH:MM",
                    help="scheduler time (policy scheduler tz); a run more than 15 min late is skipped, never replayed")
    mo = sub.add_parser("monitor", help="monitor open puts for exit/assignment/data alerts (dry-run by default)")
    _send_flags(mo)
    hb = sub.add_parser("heartbeat", help="daily heartbeat message (dry-run by default)")
    _send_flags(hb)
    sub.add_parser("status", help="last/next runs, blockers, delivery state (read-only)")
    sub.add_parser("health", help="like status; exit code 1 when a run is overdue/missed or delivery failed")
    tsu = sub.add_parser("telegram-setup", help="verify the chat is private and pin its id (network only with --apply)")
    tsu.add_argument("--apply", action="store_true")
    tt = sub.add_parser("telegram-test", help="send one test message (needs --send and CSP_DRY_RUN=false)")
    _send_flags(tt)
    sub.add_parser("outbox", help="list recent outbox rows (redacted)")
    ap = sub.add_parser("alert-preview", help="render an entry alert from a synthetic fixture JSON")
    ap.add_argument("--fixture", required=True)
    tc = sub.add_parser("ticker-check", help="read-only pre-approval check: real fetched data + entry gates for symbols "
                                              "(no whitelist entry needed; writes nothing, sends nothing)")
    tc.add_argument("--ticker", action="append", required=True, metavar="SYM", help="symbol; repeat for several")
    tc.add_argument("--all-strikes", action="store_true", help="list every put in the DTE window (default: focus rows)")
    tc.add_argument("--json", action="store_true", help="machine-readable output")
    wl = sub.add_parser("watchlist", help="symbols to TRACK for prices (not approval). add/remove preview by default; --apply writes")
    ws = wl.add_subparsers(dest="watchlist_command", required=True)
    ws.add_parser("list", help="tracked symbols with reason (watchlist / active holding / both), approval and latest stored price")
    for nm, hp in (("add", "add symbols to the watchlist"), ("remove", "remove symbols from the watchlist")):
        w = ws.add_parser(nm, help=hp + " (preview unless --apply; audited)")
        w.add_argument("symbols", nargs="+", metavar="SYM")
        w.add_argument("--apply", action="store_true", help="write the change (default: preview only)")
        w.add_argument("--reason", default="", help="optional note stored in the audit event")
        w.add_argument("--actor", default="owner")
    mr = sub.add_parser("market-refresh", help="fetch + persist stock price and put chain for tracked symbols (no Telegram, no ledger "
                                               "writes; writes market-data/run records only)")
    mr.add_argument("--dry-run", action="store_true", help="fetch and print only; persist nothing")
    mr.add_argument("--force", action="store_true", help="ignore the regular interval (never backoff or the 60 s minimum gap)")
    mr.add_argument("--ticker", default=None, metavar="SYM", help="refresh one tracked symbol only")
    cc = sub.add_parser("calendar-check", help="show NYSE session state now (or --at ISO time)")
    cc.add_argument("--dry-run", action="store_true")
    cc.add_argument("--at", default=None, help="UTC ISO time, e.g. 2026-11-27T17:30:00Z")

    j = sub.add_parser("journal", help="manual journal tools")
    js = j.add_subparsers(dest="journal_command", required=True)
    js.add_parser("init", help="create/upgrade the journal database and record the current policy")
    imp = js.add_parser("import-csv", help="import manually recorded puts from CSV")
    imp.add_argument("path")
    imp.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    cb = js.add_parser("confirm-baseline", help="owner-run: record the confirmed starting equity (audited, once)")
    cb.add_argument("--equity", required=True, help="starting equity in USD, e.g. 100000")
    cb.add_argument("--note", default="")
    bk = js.add_parser("backup", help="consistent SQLite online backup (default data/backups/)")
    bk.add_argument("--output", default=None)
    from csp_alerts import ledger_cli
    ledger_cli.add_parsers(js, sub)
    rr = sub.add_parser("reconcile-reminder", help="remind to run daily-check-in if today is not confirmed (dry-run default)")
    _send_flags(rr)
    exp = js.add_parser("export-csv", help="export journal puts to CSV")
    exp.add_argument("--output", required=True)
    return p


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(args.env_file)


def _journal(settings: Settings):
    from csp_alerts.journal.store import Journal

    j = Journal(settings.database_path)
    j.migrate()
    return j


def cmd_policy_check(args: argparse.Namespace) -> int:
    from csp_alerts.policy.config import load_policy

    pol = load_policy(args.policy)
    print(f"OK policy {pol.policy_id} v{pol.policy_version} sha256={pol.sha256}")
    print(f"pilot_enabled={pol.pilot.enabled} put_collateral_cap_usd={pol.put_collateral_cap_usd}")
    return 0


def cmd_approved_list(args: argparse.Namespace) -> int:
    from csp_alerts.policy.approved import load_approved

    items = load_approved(args.approved)
    if not items:
        print("approved list is EMPTY (valid safe state): no entry candidates will be produced")
    for a in items:
        print(f"{a.ticker} {a.asset_type} sector={a.sector} bucket={a.correlation_bucket} "
              f"max_buy_strike={a.max_buy_strike} approved_through={a.approved_through}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from csp_alerts.policy.approved import load_approved
    from csp_alerts.policy.config import load_policy

    problems: list[str] = []
    try:
        s = _settings(args)
        print(f"OK env: dry_run={s.dry_run} env={s.env} broker_access_disabled={s.disable_broker_access} "
              f"telegram_configured={s.telegram_configured}")
    except StartupError as exc:
        print("FAIL env:")
        for pr in exc.problems:
            print(f"  - {pr}")
        return 2
    from csp_alerts.settings import env_warnings
    import os
    for name in env_warnings({**os.environ}):
        print(f"WARN credential-looking environment variable present (name only): {name}")
    print(REMINDER_FREE_CASH)
    try:
        pol = load_policy(args.policy)
        print(f"OK policy v{pol.policy_version} sha256={pol.sha256[:12]}")
    except Exception as exc:  # noqa: BLE001 - report everything to the operator
        problems.append(f"policy: {exc}")
    try:
        n = len(load_approved(args.approved))
        print(f"OK whitelist: {n} approved underlying(s)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"approved list: {exc}")
    try:
        boundary.assert_no_broker_modules_loaded()
        print("OK no broker modules loaded")
    except boundary.BrokerBoundaryError as exc:
        problems.append(str(exc))
    if s.database_path.exists():
        with _journal(s) as j:
            print(f"OK journal schema={j.schema_version()} integrity={'ok' if j.integrity_ok() else 'FAILED'}")
    else:
        print(f"INFO journal not created yet ({s.database_path}); run: csp-alerts journal init")
    for pr in problems:
        print(f"FAIL {pr}")
    return 1 if problems else 0


def cmd_journal(args: argparse.Namespace) -> int:
    from csp_alerts import ledger_cli
    from csp_alerts.journal import import_csv as ic
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    with _journal(s) as j:
        if args.journal_command == "init":
            new = j.record_policy(load_policy(args.policy))
            print(f"journal ready at {s.database_path} (schema {j.schema_version()}); policy {'recorded' if new else 'already recorded'}")
            return 0
        if args.journal_command == "import-csv":
            try:
                rep = ic.import_positions_csv(j, args.path, dry_run=args.dry_run)
            except ic.CsvFormatError as exc:
                print(f"FAIL {exc}")
                return 2
            mode = "DRY-RUN (nothing written)" if args.dry_run else "written"
            print(f"{mode}: accepted={len(rep.accepted)} quarantined={len(rep.quarantined)}")
            for q in rep.quarantined:
                print(f"  row {q.row_number}: {'; '.join(q.errors)}")
            if rep.quarantined and not args.dry_run:
                qpath = Path(args.path).with_suffix(".quarantine.csv")
                ic.write_quarantine_csv(rep, qpath)
                print(f"quarantined rows saved to {qpath} and to the journal")
            return 0 if rep.ok else 1
        if args.journal_command == "confirm-baseline":
            from decimal import Decimal, InvalidOperation

            from csp_alerts.money import MoneyError, dec
            try:
                eq = dec(args.equity)
            except MoneyError:
                print("FAIL --equity must be a plain decimal number")
                return 2
            pol = load_policy(args.policy)
            if eq != pol.account_reference_value_usd:
                print(f"WARN equity {eq} differs from policy account_reference_value_usd {pol.account_reference_value_usd}")
            j.confirm_baseline(eq, note=args.note)
            print(f"baseline equity {eq} confirmed and audited; drawdown peak is seeded from it")
            return 0
        if args.journal_command == "backup":
            from csp_alerts.journal.store import Journal as _J
            dest = Path(args.output) if args.output else s.database_path.parent / "backups" / f"csp-{_now():%Y%m%d-%H%M%S}.sqlite3"
            import os as _os
            live = Path(s.database_path)
            same = dest.resolve() == live.resolve() or (dest.exists() and live.exists() and _os.path.samefile(dest, live))
            if same:
                print("FAIL backup destination is the live database file (same file, alias or hard link); choose another path")
                return 2
            j.backup(dest)
            chk = _J(dest)
            ok = chk.integrity_ok()
            chk.close()
            print(f"backup written to {dest} (integrity {'ok' if ok else 'FAILED'}); keep copies off this PC")
            return 0 if ok else 1
        if args.journal_command in ledger_cli.LEDGER_COMMANDS:
            return ledger_cli.execute(args.journal_command, args, j, load_policy(args.policy), Path(args.approved), _now())
        if args.journal_command == "export-csv":
            n = ic.export_positions_csv(j, args.output)
            print(f"exported {n} position(s) to {args.output}")
            return 0
    return 2


def _book_and_policy(args: argparse.Namespace):
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    try:
        from csp_alerts.policy.approved import load_approved
        approved = load_approved(args.approved)
    except Exception:  # noqa: BLE001 - unreadable approvals: asset types stay UNKNOWN (counted conservatively)
        approved = None
    with _journal(s) as j:
        return j.load_book(approved), pol


def cmd_reconcile_check(args: argparse.Namespace) -> int:
    from csp_alerts.journal.reconcile import reconcile

    book, pol = _book_and_policy(args)
    rep = reconcile(book, pol, _now())
    if not rep.issues:
        print("OK ledger reconciled")
        return 0
    for i in rep.issues:
        print(f"{i.severity} {i.code} -> {i.reason_code}: {i.message} {i.ref}".rstrip())
    return 1 if rep.blocking else 0


def cmd_account_check(args: argparse.Namespace) -> int:
    from csp_alerts.policy.drawdown import compute_drawdown
    from csp_alerts.policy.portfolio import compute_exposure

    book, pol = _book_and_policy(args)
    dd = compute_drawdown(book.snapshots, pol, baseline_asof_utc=book.last_risk_reset_utc,
                          baseline_equity_usd=book.baseline_equity_usd, cash_flows=book.cash_flows)
    ex = compute_exposure(book)
    snap = book.latest_snapshot
    print(REMINDER_FREE_CASH, file=sys.stderr)  # stderr keeps stdout valid JSON
    print(json.dumps({
        "drawdown_level": dd.level.value,
        "drawdown": None if dd.drawdown is None else format(dd.drawdown, "f"),
        "halt_latched": dd.halt_latched,
        "latched": dd.latched,
        "baseline_equity_usd": None if book.baseline_equity_usd is None else format(book.baseline_equity_usd, "f"),
        "latest_snapshot_utc": None if snap is None else snap.asof_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "put_collateral_usd": format(ex.put_collateral_usd, "f"),
        "aggregate_equity_risk_usd": format(ex.aggregate_equity_risk_usd, "f"),
        "active_contracts": ex.active_contracts,
    }, indent=2))
    return 0


def _print_messages(msgs, dry: bool = True) -> None:
    for m in msgs:
        print(f"--- {m.kind} [{m.severity}] dedup={m.dedup_key}")
        print(m.text)
        print("--- (DRY-RUN: printed only, nothing sent)" if dry else "--- (queued for Telegram delivery)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decide(args, s, j):
    from csp_alerts import runner
    d = runner.decide(j, s, flag_send=getattr(args, "send", False), flag_dry_run=getattr(args, "dry_run", False))
    return d


def _deliver(args, s, j, pol, messages, kind_label):
    """Print messages; in SEND mode (already validated) enqueue + flush. Returns exit code."""
    from csp_alerts import runner
    from csp_alerts.policy.approved import load_approved
    d = _decide(args, s, j)
    _print_messages(messages, dry=not d.can_send)
    if not d.can_send:
        return 0
    rep = runner.dispatch(j, messages, d, s, pol, _now(), runner.make_transport(s), lambda: load_approved(args.approved), _now)
    print(f"{kind_label}: sent={rep.sent} retry={rep.retry} failed={rep.failed} unknown_ack={rep.unknown} dropped={rep.dropped}")
    for n in rep.notes:
        print(f"note: {n}")
    return 1 if rep.failed or rep.unknown else 0


def _refuse_if_bad_send(args, s, j) -> int | None:
    d = _decide(args, s, j)
    if (args.send or args.dry_run) and d.error:
        print(f"FAIL {d.error}")
        return 2
    return None


def cmd_scan(args: argparse.Namespace) -> int:
    from csp_alerts.alerts import templates
    from csp_alerts.jobs.runmeta import run_context
    from csp_alerts.jobs.scan import run_scan
    from csp_alerts.market import failures
    from csp_alerts.market.stockpile import StockpileSource
    from csp_alerts.policy.approved import load_approved
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    approved = load_approved(args.approved)
    with _journal(s) as j:
        bad = _refuse_if_bad_send(args, s, j)
        if bad is not None:
            return bad
        j.sync_approved(approved)
        started = _now()
        if args.scheduled_for:
            from datetime import timedelta
            from zoneinfo import ZoneInfo
            hh, mm = map(int, args.scheduled_for.split(":"))
            zone = ZoneInfo(pol.market.scheduler_timezone)
            sched = datetime.combine(started.astimezone(zone).date(), datetime.min.time().replace(hour=hh, minute=mm), tzinfo=zone)
            if started - sched > timedelta(minutes=15) or started < sched - timedelta(minutes=2):
                import uuid
                j.record_run(str(uuid.uuid4()), "SCAN", started, "SKIPPED_LATE", ["OUTSIDE_SCHEDULED_WINDOW"],
                             {"scheduled_for": args.scheduled_for, **run_context(j, pol)})
                print(f"SKIPPED: scan scheduled for {args.scheduled_for} ran outside its 15-minute window; entries are never replayed late")
                return 0
        try:
            res = run_scan(journal=j, policy=pol, approved=approved, source=StockpileSource(s.stockpile_root),
                           now_utc=started, tz=s.tz)
        except Exception as exc:  # noqa: BLE001 - F1: a failed run is itself a visible status
            import uuid
            j.record_run(str(uuid.uuid4()), "SCAN", started, "SERVICE_FAILURE", ["EXCEPTION"],
                         {"error": failures.SERVICE_ERROR, **run_context(j, pol)})
            msg = templates.service_failure("scan", failures.service_failure_text(), started, pol.account_scope, s.tz)
            _deliver(args, s, j, pol, [msg], "scan-failure")
            print(f"FAIL scan: {failures.SERVICE_ERROR}", file=sys.stderr)
            return 1
        print(f"scan run={res.run_id} status={res.status} tickers={res.tickers_scanned} evaluated={res.contracts_evaluated} "
              f"candidates={len(res.candidates)} ai={res.ai_status}")
        for note in res.notes:
            print(f"note: {note}")
        return _deliver(args, s, j, pol, res.messages, "scan")


def cmd_monitor(args: argparse.Namespace) -> int:
    from csp_alerts.alerts import templates
    from csp_alerts.jobs.monitor import run_monitor
    from csp_alerts.jobs.runmeta import run_context
    from csp_alerts.market import failures
    from csp_alerts.market.stockpile import StockpileSource
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    with _journal(s) as j:
        bad = _refuse_if_bad_send(args, s, j)
        if bad is not None:
            return bad
        started = _now()
        try:
            res = run_monitor(journal=j, policy=pol, source=StockpileSource(s.stockpile_root), now_utc=started, tz=s.tz)
        except Exception as exc:  # noqa: BLE001
            import uuid
            j.record_run(str(uuid.uuid4()), "MONITOR", started, "SERVICE_FAILURE", ["EXCEPTION"],
                         {"error": failures.SERVICE_ERROR, **run_context(j, pol)})
            msg = templates.service_failure("monitor", failures.service_failure_text(), started, pol.account_scope, s.tz)
            _deliver(args, s, j, pol, [msg], "monitor-failure")
            print(f"FAIL monitor: {failures.SERVICE_ERROR}", file=sys.stderr)
            return 1
        j.record_run(res.run_id, "MONITOR", started, res.status, sorted({s.trigger.value for _, s in res.signals}),
                     {"positions": res.positions_checked, "unresolved": res.unresolved, "signals": len(res.signals),
                      "fetch_issues": res.issues[:10], **run_context(j, pol)})
        print(f"monitor run={res.run_id} status={res.status} positions={res.positions_checked} unresolved={res.unresolved} signals={len(res.signals)}")
        for note in res.notes:
            print(f"note: {note}")
        return _deliver(args, s, j, pol, res.messages, "monitor")


def cmd_daily_check_in(args: argparse.Namespace) -> int:
    from csp_alerts import ledger_cli
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    with _journal(s) as j:
        return ledger_cli.execute("daily-check-in", args, j, load_policy(args.policy), Path(args.approved), _now())


def cmd_reconcile_reminder(args: argparse.Namespace) -> int:
    from csp_alerts.alerts import templates
    from csp_alerts.journal.reconcile import exchange_date, reconcile
    from csp_alerts.market import calendar as mcal
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    with _journal(s) as j:
        bad = _refuse_if_bad_send(args, s, j)
        if bad is not None:
            return bad
        now = _now()
        if mcal.is_trading_day(exchange_date(now)) is False:
            print("not a trading day: no reminder")
            return 0
        book = j.load_book()
        snap = book.latest_snapshot
        rep = reconcile(book, pol, now)
        issues = sorted({i.code for i in rep.issues if i.severity == "BLOCK"})
        today_done = snap is not None and snap.manual_confirmed and exchange_date(snap.asof_utc) == exchange_date(now) and not rep.blocking
        if today_done:
            print("OK: today's check-in is recorded and the ledger is clean; no reminder needed")
            return 0
        msg = templates.reconcile_reminder(issues, now, pol.account_scope, s.tz)
        return _deliver(args, s, j, pol, [msg], "reconcile-reminder")


def cmd_status(args: argparse.Namespace) -> int:
    from csp_alerts.jobs.status import collect_status, status_lines
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    with _journal(s) as j:
        st = collect_status(j, pol, _now(), s.tz)
    print("\n".join(status_lines(st, s.tz)))
    return 1 if (args.command == "health" and st["flags"]) else 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    from csp_alerts.alerts import templates
    from csp_alerts.jobs.status import heartbeat_line
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    with _journal(s) as j:
        bad = _refuse_if_bad_send(args, s, j)
        if bad is not None:
            return bad
        now = _now()
        msg = templates.heartbeat([heartbeat_line(j, pol, now, s.tz)], now, pol.account_scope, s.tz)
        return _deliver(args, s, j, pol, [msg], "heartbeat")


def cmd_telegram_setup(args: argparse.Namespace) -> int:
    from csp_alerts import runner
    from csp_alerts.alerts.telegram import chat_id_sha256

    s = _settings(args)
    if not s.telegram_configured:
        print("FAIL TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env (token is never printed)")
        return 2
    if not args.apply:
        print("PREVIEW: would call Telegram getChat once, require chat type 'private', and pin the chat id hash. "
              "Re-run with --apply. Start a chat with your bot first (send /start).")
        return 0
    with _journal(s) as j:
        kind, err = runner.make_transport(s).get_chat_type()
        if kind is None:
            print(f"FAIL could not verify chat: {err}")
            return 1
        if kind != "private":
            print(f"FAIL chat type is {kind!r}; only a private chat may be the recipient")
            return 1
        j.pin_chat(chat_id_sha256(s.telegram_chat_id.reveal()), kind)
        print("OK private chat verified and pinned (id stored only as a hash)")
    return 0


def cmd_telegram_test(args: argparse.Namespace) -> int:
    from csp_alerts.alerts import templates
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    with _journal(s) as j:
        bad = _refuse_if_bad_send(args, s, j)
        if bad is not None:
            return bad
        now = _now()
        msg = templates.AlertMessage("SERVICE_HEALTH", "INFO",
                                     "<b>CSP TELEGRAM TEST</b>\nOutbound test only. NO ORDER SENT.",
                                     f"TELEGRAM_TEST|{pol.account_scope}|{now.isoformat()}")
        return _deliver(args, s, j, pol, [msg], "telegram-test")


def cmd_outbox(args: argparse.Namespace) -> int:
    s = _settings(args)
    with _journal(s) as j:
        for r in j.conn.execute("SELECT alert_id, class, severity, status, attempts, created_at_utc FROM alert_outbox "
                                "ORDER BY rowid DESC LIMIT 30"):
            print(f"{r['created_at_utc']} {r['class']:<24} {r['severity']:<7} {r['status']:<15} attempts={r['attempts']} {r['alert_id'][:8]}")
    return 0


def cmd_alert_preview(args: argparse.Namespace) -> int:
    from csp_alerts.alerts import templates
    from csp_alerts.jobs.scan import Candidate, _components
    from csp_alerts.journal.models import AccountSnapshot, BookState
    from csp_alerts.journal.reconcile import exchange_date
    from csp_alerts.market.models import MarketObservation, SessionInfo, SessionStatus
    from csp_alerts.policy.approved import ApprovedUnderlying
    from csp_alerts.policy.config import load_policy
    from csp_alerts.policy.entry import evaluate_entry
    from datetime import timedelta
    from decimal import Decimal

    data = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    pol = load_policy(args.policy)
    now = datetime.fromisoformat(data["now_utc"].replace("Z", "+00:00"))
    obs = MarketObservation.from_dict(data["observation"])
    appr = ApprovedUnderlying.model_validate(data["approval"])
    from csp_alerts.jobs.scan import deliverable_multiplier
    from dataclasses import replace as _replace
    # same rule as the scan: the deliverable is standard only if the owner attested it recently
    obs = _replace(obs, multiplier=deliverable_multiplier(appr, pol, exchange_date(now)))
    eq, cash = Decimal(str(data["equity_usd"])), Decimal(str(data["free_cash_usd"]))
    book = BookState(snapshots=(AccountSnapshot("preview", now - timedelta(hours=1), eq, cash, True, "preview"),),
                     baseline_equity_usd=eq)
    d = evaluate_entry(obs, appr, book, pol, now, SessionInfo(SessionStatus.OPEN, 300))
    print(f"[preview of a fixture - synthetic unless you supplied real data] decision={d.decision.value} "
          f"reasons={[r.value for r in d.reason_codes]}")
    if d.passed:
        dte = (obs.expiration - exchange_date(now)).days
        cand = Candidate(obs, appr, d, "preview", _components(obs, dte))
        _print_messages([templates.entry_alert(cand, pol, _settings(args).tz)])
        return 0
    return 1


def cmd_ticker_check(args: argparse.Namespace) -> int:
    """Read-only: no journal, no outbox, no Telegram. Network is Yahoo via the Stockpile adapter only."""
    from csp_alerts.jobs import ticker_check as tck
    from csp_alerts.market import calendar as mcal
    from csp_alerts.market.stockpile import StockpileSource
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    now = _now()
    seen: list[str] = []
    for raw in args.ticker:
        t = tck.normalize_symbol(raw) or raw.strip()   # never case-fold a rejected symbol
        if t not in seen:
            seen.append(t)
    if len(seen) > tck.MAX_TICKERS:
        print(f"FAIL too many distinct tickers ({len(seen)}); the maximum per run is {tck.MAX_TICKERS}", file=sys.stderr)
        return 2
    source = StockpileSource(s.stockpile_root)
    reports = [tck.check_ticker(t, source, pol, now) for t in seen]
    if args.json:
        print(json.dumps(tck.to_json_obj(reports, now), indent=2))
    else:
        det = mcal.session_detail(now)
        note = f"Market session now: {det.info.status.value} ({det.reason}); the scan blocks entries when closed, this check does not."
        print(tck.render_text(reports, pol, now, pol.market.scheduler_timezone, all_strikes=args.all_strikes,
                              session_note=note))
    return 0 if all(r.status == "OK" for r in reports) else 1


def cmd_watchlist(args: argparse.Namespace) -> int:
    """Tracking only: never approves a ticker and never produces an entry alert. Local DB only, no network."""
    from csp_alerts.journal import watchlist as wl
    from csp_alerts.market import observations as obs
    from csp_alerts.market.freshness import MAX_WATCHLIST
    from csp_alerts.policy.approved import load_approved

    s = _settings(args)
    with _journal(s) as j:
        if args.watchlist_command == "list":
            try:
                approved = {a.ticker for a in load_approved(args.approved)}
            except Exception:  # noqa: BLE001
                approved = set()
            rows = wl.tracked(j)
            print(f"tracked symbols: {len(rows)} (watchlist {len(wl.watchlist_symbols(j))}/{MAX_WATCHLIST}; active holdings are always tracked)")
            for t in rows:
                st = obs.latest_stock(j.conn, t.symbol)
                ss = obs.get_status(j.conn, t.symbol)
                px = (f"{st['price']} ({st['price_type']}, retrieved {st['retrieved_at_utc']}, market time "
                      f"{st['market_time_utc'] or 'UNKNOWN'})") if st else "no price stored"
                print(f"  {t.symbol:<8} reason={t.reason:<14} "
                      f"{'approved for entry alerts' if t.symbol in approved else 'NOT approved for entry alerts'}"
                      f" | status={ss['status'] if ss else 'never fetched'} | {px}")
            print("Tracked is not approved: a tracked, unapproved symbol shows data but can never produce an entry alert.")
            return 0
        adding = args.watchlist_command == "add"
        try:
            syms = list(dict.fromkeys(wl.normalize_symbol(x) for x in args.symbols))
        except wl.WatchlistError as exc:
            print(f"FAIL {exc}")
            return 2
        have = {x for x, _, _ in wl.watchlist_symbols(j)}
        for sym in syms:
            if (adding and sym in have) or (not adding and sym not in have):
                print(f"  {sym}: {'already on' if adding else 'not on'} the watchlist; nothing to do")
                continue
            if not args.apply:
                print(f"  PREVIEW {sym}: would {'add to' if adding else 'remove from'} the watchlist")
                continue
            try:
                fn = wl.add_symbol if adding else wl.remove_symbol
                fn(j, sym, actor=args.actor, source="cli", reason=args.reason)
            except wl.WatchlistError as exc:
                print(f"  FAIL {sym}: {exc}")
                return 2
            print(f"  {sym}: {'added to' if adding else 'removed from'} the watchlist (audited)")
        if not args.apply:
            print("PREVIEW ONLY: nothing was written. Re-run with --apply. Tracking is not approval.")
        elif adding:
            print("Prices appear after the next `market-refresh` run. This did not approve anything.")
        return 0


def cmd_market_refresh(args: argparse.Namespace) -> int:
    """Network: Yahoo via the Stockpile adapter only. Writes md_* market-data/run tables only (nothing with --dry-run)."""
    from csp_alerts.jobs.market_refresh import run_market_refresh
    from csp_alerts.market.freshness import PROVIDER_LABEL
    from csp_alerts.market.stockpile import StockpileSource
    from csp_alerts.policy.config import load_policy

    s = _settings(args)
    pol = load_policy(args.policy)
    with _journal(s) as j:
        res = run_market_refresh(journal=j, policy=pol, source=StockpileSource(s.stockpile_root), now_utc=_now(),
                                 force=args.force, only=args.ticker, persist=not args.dry_run, clock=_now)
        mode = "DRY-RUN (nothing persisted)" if args.dry_run else "persisted to market-data tables only"
        attempted = sum(1 for o in res.outcomes if o.attempted)
        print(f"market-refresh run={res.run_id} trigger={res.trigger} session={res.session} status={res.status} "
              f"attempted={attempted}/{len(res.outcomes)} | {mode}")
        print(PROVIDER_LABEL)
        for o in res.outcomes:
            if o.attempted:
                print(f"  {o.symbol:<8} {o.status:<13} price={o.price or 'UNAVAILABLE'} contracts={o.contracts_written}"
                      f" retained={o.contracts_retained}" + (f" | {o.error}" if o.error else ""))
            else:
                print(f"  {o.symbol:<8} skipped ({o.skipped})")
        for n in res.notes:
            print(f"note: {n}")
        return 1 if res.status in ("ERROR", "RATE_LIMITED") else 0


def cmd_calendar_check(args: argparse.Namespace) -> int:
    from csp_alerts.market.calendar import session_detail

    now = _now() if not args.at else datetime.fromisoformat(args.at.replace("Z", "+00:00"))
    from csp_alerts.policy.config import load_policy

    d = session_detail(now)
    min_left = load_policy(args.policy).market.no_entry_in_final_minutes_of_session
    allowed = d.info.status.value == "OPEN" and d.info.minutes_to_close is not None and d.info.minutes_to_close >= min_left
    print(json.dumps({"now_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": d.info.status.value,
                      "minutes_to_close": d.info.minutes_to_close, "early_close": d.early_close,
                      "session_date": None if d.session_date is None else d.session_date.isoformat(),
                      "reason": d.reason, "entries_allowed_by_calendar": allowed}, indent=2))
    return 0


def _safe_error_text(exc: BaseException) -> str:
    """Last-resort CLI error line. A provider failure (SourceError) is shown as its canned code; anything else is shown
    with its class name and a bounded message, tolerating an exception whose __str__ raises or returns a non-str."""
    from csp_alerts.market import failures
    from csp_alerts.market.stockpile import SourceError

    if isinstance(exc, SourceError):
        return failures.stored_for(exc)
    try:
        msg = str(exc)
        msg = msg[:300] if isinstance(msg, str) else ""
    except Exception:  # noqa: BLE001
        msg = ""
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "doctor": cmd_doctor, "policy-check": cmd_policy_check, "approved-list": cmd_approved_list,
        "reconcile-check": cmd_reconcile_check, "scan": cmd_scan, "alert-preview": cmd_alert_preview,
        "calendar-check": cmd_calendar_check, "ticker-check": cmd_ticker_check, "watchlist": cmd_watchlist, "market-refresh": cmd_market_refresh, "daily-check-in": cmd_daily_check_in, "reconcile-reminder": cmd_reconcile_reminder, "monitor": cmd_monitor, "status": cmd_status, "health": cmd_status,
        "heartbeat": cmd_heartbeat, "telegram-setup": cmd_telegram_setup, "telegram-test": cmd_telegram_test, "outbox": cmd_outbox, "account-check": cmd_account_check, "journal": cmd_journal,
    }
    try:
        if args.command in ("scan", "monitor", "heartbeat", "reconcile-reminder"):
            from csp_alerts.lock import AlreadyRunning, single_instance
            s = _settings(args)
            try:
                with single_instance(s.database_path.parent / "locks" / "csp-jobs.lock"):
                    return handlers[args.command](args)
            except AlreadyRunning as exc:
                print(f"SKIPPED: {exc}", file=sys.stderr)
                return 3
        if args.command == "market-refresh":
            # its own lock: a slow provider fetch must never make the 15-minute monitor skip (exit 3 there)
            from csp_alerts.lock import AlreadyRunning, single_instance
            s = _settings(args)
            try:
                with single_instance(s.database_path.parent / "locks" / "market-refresh.lock"):
                    return handlers[args.command](args)
            except AlreadyRunning as exc:
                print(f"SKIPPED: {exc}", file=sys.stderr)
                return 3
        return handlers[args.command](args)
    except StartupError as exc:
        print("FAIL startup validation:", file=sys.stderr)
        for pr in exc.problems:
            print(f"  - {pr}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {_safe_error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
