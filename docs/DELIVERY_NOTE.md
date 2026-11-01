# Delivery note: first working paper-pilot loop

Status: the loop below runs on synthetic data and in dry-run. **Nothing here has been verified with a real Telegram message or with scheduled tasks on the owner's PC.** This is not approval for real-money decision support. Advisory only: the app places, changes and cancels nothing and has no broker access.

## What is supported
- One paper account (`paper_primary`); standard 100-share puts; one contract per position; owner-approved symbols only (the list ships empty; no symbol is preapproved).
- Scan (`scan`): Yahoo via the pinned Stockpile checkout, all spec entry gates, ranking (liquidity first), max 3 alternatives, digest/blocked/data-unavailable statuses with per-symbol coverage. Dry-run by default.
- Manual ledger through the CLI, preview by default with audit: baseline, approve, daily check-in, add-trade, fill, cancel, close, expire, assign (3 steps), reset, record-cash-flow, backup, CSV import/export.
- Monitor (`monitor`): profit 60%, loss 2x, delta 0.30 (indicative, Tier R, can be switched off in policy), 21-DTE and assignment reminders (no quote needed), data-issue warnings.
- Telegram: outbound private chat only, pinned; outbox with retry/backoff, unknown-ACK, dedup, entry-alert expiry and revalidation. Sends need `--send` AND `CSP_DRY_RUN=false`.
- Health: `status`, `health`, `heartbeat`, run statuses, drawdown latch (3/5/8%) cleared only by a manual reset.
- Windows task registration scripts (`ops/windows`), single-instance lock, late scan skipping.

## Known blockers and limitations
- **Real Telegram delivery is unverified**: only a local fake server was used. The owner must run `telegram-setup --apply` and `telegram-test --send` and confirm receipt.
- **Task registration not executed**: `register-tasks.ps1 -Apply` was previewed and parsed, never run. The PC must be on and logged in; a stopped PC cannot announce its own outage.
- **Contract deliverable evidence is owner attestation only** (`--standard-deliverable-confirmed-on`): Stockpile rows carry no contract symbol, so without that flag every contract is blocked.
- Yahoo data is indicative, untimestamped and may be throttled. Event/earnings clearance must come from the owner (provider earnings data is only a caution). Delta is model delta.
- Not supported (visibly blocked or absent): multi-contract/partial fills (entries frozen), multiple accounts, AI review, verified-quote feed, stress tests, backtests, reports, dashboards, restore command (restore is a manual file copy).
- Slice 2 runs a live Yahoo path (one liquid ETF fetched and gated once, read-only); no live candidate feed has been observed for approved symbols yet.

## Exact startup commands (Windows, from the repo folder)
```
# install from the lockfile (Python 3.12+; uv from https://docs.astral.sh/uv/)
uv sync --frozen
git clone https://github.com/medloh/stockpile third_party/stockpile
git -C third_party/stockpile checkout 34bf6cf8101e54620ea1e2bda940f0a3aa88e791
copy .env.example .env
uv run --frozen python -m csp_alerts.cli journal init
uv run --frozen python -m csp_alerts.cli doctor
uv run --frozen python -m csp_alerts.cli journal confirm-baseline --equity 100000
uv run --frozen python -m csp_alerts.cli alert-preview --fixture tests\fixtures\entry.json
uv run --frozen python -m csp_alerts.cli scan --dry-run
uv run --frozen python -m csp_alerts.cli status
```
(Without uv: `python -m venv .venv`, `.venv\Scripts\python -m pip install -e . pytest`, then use `.venv\Scripts\python -m csp_alerts.cli ...`.) Next: RUNBOOK.md sections 1 to 3.

## Validation evidence
- Test commands: `uv run --frozen python -m pytest tests/unit -q` and `.venv\Scripts\python -m pytest tests/unit tests/acceptance -q` (counts are in the hand-off report; the suite runs with sockets disabled except loopback, `pytest-socket`).
- (a) Clean install from `uv.lock`: copied the tree without `.venv`/`third_party`/`data` to a scratch folder, `uv sync --frozen` (Python 3.12.16), `uv run --frozen python -m csp_alerts.cli doctor` printed OK for env/policy/whitelist/boundary, and `uv run --frozen python -m pytest tests/unit -q` gave 422 passed, 1 skipped (the skip needs the Stockpile checkout).
- (b) Backup/restore: `tests/unit/test_journey.py::test_backup_restore_does_not_replay_entries_or_clear_a_halt` backs up a journal holding a latched 8% halt and a 2-hour-old queued entry alert, restores it into a new folder, and shows: halt still latched, scan BLOCKED with a CRITICAL message, the old entry alert becomes DROPPED_EXPIRED with 0 Telegram requests, and only `journal reset --reviewed` clears the halt.
- (c) Owner journey on synthetic data: `test_owner_journey_reconcile_scan_record_monitor_close` (run with `-s` to print the transcript): reconcile OK -> scan preview CANDIDATES with 0 outbox rows -> `add-trade`/`fill` (reconcile blocked until the check-in) -> monitor PROFIT_EXIT labelled INDICATIVE -> `close` -> monitor NO_POSITIONS and collateral released. It also exposed and fixed a real bug: a quote fetched after the job's start time was judged "from the future" and turned every monitor price alert into DATA_ISSUE.
- (d) Dry-run leaves no sendable outbox rows: `tests/unit/test_delivery.py::test_dry_run_never_writes_sendable_rows`, `test_send_mode_matrix` (all 32 flag combinations), `test_dry_run_default_prints_and_never_queues`; the journey test asserts 0 outbox rows after a dry-run scan.
- Safe first run: `doctor` OK with empty whitelist; boundary audit tests (no broker imports, no float money, no inbound commands). Risk checks: `test_entry.py` pilot/aggregate/ticker/sector/name caps; a $16,000 total fails the $15,000 pilot cap (`PILOT_COLLATERAL_LIMIT`).
- Live read-only Yahoo smoke test (slice 2): one liquid ETF fetched through Stockpile, 91 put rows, gated; no data kept in the repo.
- Sample alert of every kind: `docs/samples/` (synthetic).

## Feedback loop
Use `docs/FEEDBACK_LOG.md`. Review confusing/missed/duplicate alerts and time spent on manual updates before adding features.
