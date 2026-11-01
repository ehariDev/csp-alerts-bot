# CSP Alerts Bot

Telegram alerts for cash-secured put research candidates and position warnings. **Advisory only: it never places orders and never connects to a brokerage.** You trade manually and record what you did here.

Start here: [docs/DELIVERY_NOTE.md](docs/DELIVERY_NOTE.md) (what works, blockers, startup commands, evidence) and [RUNBOOK.md](RUNBOOK.md) (setup, BotFather, daily routine, assignment, outages). Also [SECURITY.md](SECURITY.md), [DATA_DICTIONARY.md](DATA_DICTIONARY.md), sample alerts in `docs/samples/` (synthetic), spec `requirements/PUT_SELLER_TELEGRAM_SPEC.md` (see `requirements/README.md` for the current files, `evidence/latest/` and `archive/`), decisions `docs/DECISIONS.md`, agent rules `CLAUDE.md`.

## What exists now
- Manual SQLite journal with audit log, state machine, CSV import/export, reconciliation, drawdown latch, preview-by-default ledger commands (`daily-check-in`, `journal add-trade|fill|close|assign|record-cash-flow|reset|backup ...`).
- Entry scan (Yahoo via the pinned Stockpile checkout, all hard gates, ranking, no-trade/blocked/data-unavailable statuses) and position monitor (profit/loss/delta/21-DTE/assignment/data-issue alerts).
- Telegram outbound transport with outbox, retries, dedup, expiry/revalidation, recipient pinning. **Dry-run is the default**; a send needs `--send` AND `CSP_DRY_RUN=false`.
- `status`, `health`, `heartbeat`, Windows Task Scheduler scripts in `ops/windows/`.

## Not built / not verified
AI reviewer, reports/backtests/stress tests, dashboards, multi-account, multi-contract positions, verified-quote feeds. Real Telegram delivery and task registration on your PC have not been verified yet (see the delivery note).

## Install (Windows)
```
uv sync --frozen
git clone https://github.com/medloh/stockpile third_party/stockpile
git -C third_party/stockpile checkout 34bf6cf8101e54620ea1e2bda940f0a3aa88e791
copy .env.example .env
uv run --frozen python -m csp_alerts.cli doctor
```
Run tests: `uv run --frozen python -m pytest tests/unit -q`. Never pip-install Stockpile's options-scanner package (it hard-depends on broker SDKs). Stockpile is CC BY-NC 4.0 (personal, non-commercial use). The approved whitelist (`config/approved.json`) ships empty on purpose.
