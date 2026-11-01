# DATA DICTIONARY (concise)

Money is stored as decimal TEXT (never float). Timestamps are UTC ISO-8601 `...Z`. Dates are `YYYY-MM-DD`. Sector/bucket labels are stored lower-cased and trimmed.

## SQLite tables (`data\csp_alerts.sqlite3`)
| Table | Purpose / key fields |
|---|---|
| `policy_versions` | Each distinct `config/policy.yaml` (id, sha256, JSON) recorded by `journal init`. |
| `approved_underlyings` | Mirror of `config/approved.json` (the owner's whitelist file is the source): ticker, asset_type, sector, correlation_bucket, max_buy_strike, approved_through, events_reviewed_on, events_cleared_through, ownership_reason. `standard_deliverable_confirmed_on` lives only in the file. |
| `option_positions` | Manual puts. `state` in DRAFT, PENDING, WORKING, OPEN, CLOSE_WORKING, CLOSED, CANCELED, EXPIRED, ASSIGNED_PENDING_RECONCILIATION, ASSIGNED_RECONCILED, ADJUSTMENT_REVIEW. `actual_open_credit_per_share`, `actual_close_debit_per_share`, `fees_usd`, `account_scope` (default `paper_primary`), `contracts` (MVP: 1). PENDING/WORKING/OPEN/CLOSE_WORKING/ADJUSTMENT_REVIEW reserve strike x multiplier x contracts of collateral. |
| `stock_lots` | Owned shares: shares, reference_price + `reference_asof_utc` (mark), `cost_basis_reference_usd` (cash paid), `linked_trade_id` (the assigned put; at most one lot per put). |
| `account_snapshots` | Owner-confirmed `equity_usd` and **NET** `free_unencumbered_cash_usd` at `asof_utc` (unique per timestamp). |
| `market_observations` | Every normalized option row seen by a scan: contract key, retrieval time, `quote_at_utc` (always null for Yahoo), provider, tier `INDICATIVE_UNTIMESTAMPED`, `raw_sha256`, JSON. |
| `entry_evaluations` | Every gate decision per scan run: decision, reason codes, full decision JSON. |
| `alert_outbox` | Queued Telegram messages. `status`: PENDING, SENDING, SENT, RETRY, FAILED_FINAL, UNKNOWN_ACK, DROPPED_EXPIRED, DROPPED_STALE. `dedup_key` UNIQUE (account-scoped); `alert_id` = uuid5 of it; `evidence_json` holds expiry, policy hash, revalidation facts, last error (redacted). |
| `run_status` | One row per scan/monitor run: SCAN = CANDIDATES, NO_OPPORTUNITIES, BLOCKED_LEDGER, DATA_UNAVAILABLE, MARKET_CLOSED, SKIPPED_LATE, SERVICE_FAILURE; MONITOR = ALERTS, NO_ALERTS, NO_POSITIONS, DATA_UNAVAILABLE, SERVICE_FAILURE. |
| `import_quarantine` | CSV rows rejected on import with their errors. |
| `audit_log` | Append-only. Notable events: POSITION_*, STOCK_LOT_ADDED, SNAPSHOT_ADDED, BASELINE_CONFIRMED, RISK_RESET, CASH_FLOW, MANUAL_COMMAND (actor + reason; web records use actor `web:owner`, source `web`), WEB_TRADE_RECORDED (idempotency_key, action, trade_id, state), POLICY_BREACH_RECORDED (trade_id, codes),  DATA_ISSUE, ALERT_DROPPED, TELEGRAM_CHAT_PINNED, APPROVAL_*. Events in POSITION_CREATED/TRANSITION/IMPORTED, STOCK_LOT_ADDED and CASH_FLOW require a newer confirmed snapshot. |
| `schema_migrations` | Applied migration versions (currently 0001-0004). |

## Observation (`market/models.py`, spec 6.1)
`contract_key` = `TICKER|expiration|P|strike|multiplier`; `spot`, `bid`, `ask`, `delta` (model/Black-Scholes), `open_interest`, `daily_volume`, `last_option_trade_at_utc` (last print, never a quote time), `retrieved_at_utc`, `quote_at_utc` (null = unknown), `event_check` (UNKNOWN is never "no earnings"). Missing stays null; `0` for bid/ask/volume/OI is treated as unverified and blocks.

## Decision (`policy/entry.py`)
`decision` PASS_RESEARCH_ONLY | PASS_VERIFIED_DATA | BLOCKED | MORE_RESEARCH | DATA_UNAVAILABLE; `reason_codes` (stable, listed in `policy/reasons.py`; extras STALE_LAST_TRADE, STRIKE_NOT_BELOW_SPOT); exposure before/after, additional collateral, free cash after, data tier, `requires_broker_manual_quote_check` (always true).

## Alert classes and dedup keys
ENTRY_RESEARCH `ENTRY_RESEARCH|day|TICKER|expiry|P|strike`; exits `EXIT|scope|trade|trigger|day`; digests/blocked `NO_TRADE_DIGEST|day`, `BLOCKED|day|codes[|level]`; heartbeat, reconcile reminder and failures are per day. Every key is prefixed with the account scope in the outbox. Severity: INFO, WARN, ACTION, URGENT, CRITICAL (8% halt).

## Config files
`config/policy.yaml` (+ `policy.schema.json`): every threshold; `config/approved.json`: whitelist (ships empty); `.env`: secrets/mode; `data/`: runtime (git-ignored). Samples of every alert kind: `docs/samples/` (synthetic).

## Stage A market-data tables (migration 0005; additive, prices are quoted decimal TEXT, times UTC `...Z`)
- `watchlist(symbol PK, added_at_utc, added_by)`: owner-tracked symbols. Not approval. Audit events `WATCHLIST_ADDED` / `WATCHLIST_REMOVED` (not ledger events: they never require a new snapshot).
- `md_symbol_status(symbol PK, status, last_attempt_at_utc, last_success_at_utc, last_error, consecutive_failures, next_attempt_after_utc, session_at_attempt, last_run_id, detail_json)`. `status` = OK | PARTIAL | RATE_LIMITED | ERROR | MARKET_CLOSED. `last_error` is sanitised. A failure never clears `last_success_at_utc`.
- `md_stock_latest(symbol PK, price, price_type, provider, tier, market_time_utc, retrieved_at_utc, source, run_id, raw_sha256)` and `md_stock_history` (last 300 per symbol). `market_time_utc` NULL = the provider gave none (never filled from the retrieval time). `price_type` is LAST for the Yahoo path (the provider does not say whether that is an intraday last or the previous close). `source` = REFRESH | MONITOR.
- `md_option_latest(symbol, expiration, option_right, strike PK; occ_symbol, occ_source PROVIDER|DERIVED, bid, ask, last, volume, open_interest, delta, implied_volatility, spot, last_trade_at_utc, quote_time_utc, provider, tier, retrieved_at_utc, source, run_id, raw_sha256, identity_note)`: latest quote per exact contract; a re-fetch with an unusable quote never overwrites a prior usable one. `last_trade_at_utc` is the option's last print time, not a quote time; `quote_time_utc` NULL = unknown. `md_option_history` keeps up to 30 changes per contract. Expired contracts are pruned after 7 days.
- `md_refresh_runs(run_id PK, started_at_utc, finished_at_utc, trigger SCHEDULED|FORCED|REQUEST, session_status, status, symbols_json, summary_json)`: last 500 runs.
- `md_refresh_requests(request_id PK, requested_at_utc, requested_by, scope ALL|symbol, status PENDING|DONE|EXPIRED, picked_up_at_utc, run_id, note)`: written by the web page, consumed by the job.
- Constants (cadence, caps, thresholds) are in `market/freshness.py`; they are not policy and not in `policy.yaml`.

## Stage B1 web recording (no schema change; additive audit events and one reconcile issue)
| Item | Meaning |
|---|---|
| `audit_log` `WEB_TRADE_RECORDED` | One per confirmed web record. `after_json`: `idempotency_key` (32 hex, unique per preview), `action` (sell_filled, sell_working, mark_filled, close_order, close_filled, cancel_open_order, cancel_close_order, expire, checkin), `trade_id`, `state` (resulting state or null for a check-in). A repeated key means "already recorded". |
| `audit_log` `POLICY_BREACH_RECORDED` | `trade_id`, `codes` (reason codes plus `LABELS_UNKNOWN`). The position keeps its true state; the flag only blocks new entries while it reserves collateral. Loaded into `BookState.policy_breaches`. |
| `audit_log` `MANUAL_COMMAND` (web) | `command` = the action name, `reason` (owner text, max 300 chars, control characters removed), `args` = trade_id and idempotency_key; `actor` `web:owner`; `source` `web`. |
| Reconcile `POLICY_BREACH_UNRESOLVED` | BLOCK, maps to `LEDGER_UNRECONCILED`. Present while a flagged trade is PENDING/WORKING/OPEN/CLOSE_WORKING/ADJUSTMENT_REVIEW. |
| `option_positions.trade_id` for web trades | `WEB-<TICKER>-<YYYYMMDD of expiry>-<first 8 hex of the idempotency key>`. `notes` = `web-recorded; <reason>` (max 200 chars). |
| Preview token | Not stored. HMAC over session, fields, ledger revision (`ledger_seqs()[1]`), policy sha256 and issue time; lives only in the page and the server process. |


## Stage B2 (no schema change; additive audit events)
| Item | Meaning |
|---|---|
| `audit_log` `WEB_TRADE_RECORDED.contract_info_codes` | List of contract-level reason codes (delta, DTE window, spread, open interest, last print, ...) true at the stored quote when the trade was recorded. Information only: they never blocked recording. Shown on the position page. |
| `audit_log` `POLICY_BREACH_CLEARED` | `after_json`: `trade_id`, `cleared_codes` (the recorded codes), `reason` (what triggered the re-check), `policy_sha256`, `evaluated_against`; `before_json`: the recorded codes. Written only when the position triggers NO flagged code against the current book/approvals/labels/policy. Actor `web:owner` (web record or label correction), `system:scan` or `system:ledger-cli`. `Journal.policy_breaches()` applies RECORDED then CLEARED in journal order. |
| `audit_log` `LABEL_CORRECTED` | `before_json`: ticker plus every relabelled row (`positions`: trade id, sector, bucket; `lots`: lot id, sector, bucket) as stored before. `after_json`: ticker, new `sector`, new `correlation_bucket`, `reason`, `idempotency_key`. NOT a ledger event (`LEDGER_EVENT_TYPES` unchanged): no new check-in is demanded. |
| `audit_log` `WEB_LABEL_CORRECTED` | `idempotency_key`, `ticker`, `sector`, `correlation_bucket`; a repeated key means "already recorded". |
| Label store | Unchanged: `option_positions.sector` / `correlation_bucket` (rows that reserve collateral or await assignment reconciliation) and `stock_lots.sector` / `correlation_bucket`, normalized strip+casefold. Asset type is NOT a column there: it is read from `approved_underlyings`, default STOCK. The value `unknown` is the "labels unknown" marker. |
| Portfolio figures (display only) | From `policy/portfolio.py`: `compute_exposure`, `exposure_groups` (ticker/sector/bucket = put collateral + owned stock; expiration week = put collateral only; `room = limit - exposure`, `breached = exposure > limit`, `exhausted = room <= 0`), `portfolio_limits`, `capacity_rows`. The browser computes nothing. |
