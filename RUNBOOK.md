# RUNBOOK (Windows, advisory-only)

This service researches cash-secured puts and sends Telegram messages. **It never places, changes or cancels an order and never connects to a brokerage.** You trade manually at your broker/simulator and record what you did here. If the manual record is wrong or stale, new-entry alerts stop (exit and data warnings continue).

Commands below use `csp` as shorthand for `.venv\Scripts\python -m csp_alerts.cli --env-file .env` run from the repo folder. Every command that changes your record is a **preview unless you add `--apply --reason "..."`**.

## 1. First-time setup
1. Install Python 3.12+ and create the environment:
   `python -m venv .venv` then `.venv\Scripts\python -m pip install -e .` (pulls pandas, numpy, yfinance, exchange-calendars). Do **not** pip-install Stockpile's options-scanner package.
2. Clone the pinned data source (git-ignored, personal non-commercial use, CC BY-NC 4.0):
   `git clone https://github.com/medloh/stockpile third_party/stockpile` then `git -C third_party/stockpile checkout 34bf6cf8101e54620ea1e2bda940f0a3aa88e791`.
3. `copy .env.example .env`. Keep `CSP_DRY_RUN=true` and `CSP_DISABLE_BROKER_ACCESS=true` for now. Never share `.env`.
4. `csp journal init` then `csp doctor` (must say OK; it warns about credential-looking environment variables, names only).
5. Record your starting equity once (audited): `csp journal confirm-baseline --equity 100000`.
6. **Before approving, look at real data**: `csp ticker-check --ticker XYZ [--ticker ABC ...]` (add `--all-strikes` for every put, `--json` for machine output). It is read-only (no journal, no Telegram, no whitelist change) and prints what the scan would see from Yahoo via Stockpile: price with source and retrieval time (UTC and Central), next earnings or `UNAVAILABLE`, the 35-45 DTE puts with bid/ask/delta/OI/volume/spread/collateral, each gate result, and a verdict against the $9,000 per-ticker and $15,000 pilot caps. It is indicative research data, not a quote and not an approval: verify at your broker, and check asset type, leveraged/inverse status and standard deliverable yourself (the tool says "not checked"). A symbol that returns no price may be a typo or a network problem. Monthly-only products often have no expiry between 35 and 45 DTE on a given day; the tool then says so and lists the nearest listed expiries before and after (indicative; the entry window is never widened). If it cannot read the expiry list, an empty result is reported as `DATA UNAVAILABLE` with both possible meanings (outage or no expiry in the window). At most 20 tickers per run. Verdicts: `DATA UNAVAILABLE`, `NO LIQUID CONTRACTS`, `NOT ELIGIBLE UNDER CAPS`, `WOULD PASS GATES (research only)`.
7. Approve underlyings you have independently reviewed (the list ships EMPTY; zero approvals is a safe state). Preview, then apply:
   `csp journal approve --ticker XYZ --asset-type ETF --sector broad_market --bucket us_equity --max-buy-strike 50 --approved-through 2026-12-31 --events-reviewed-on 2026-10-30 --events-cleared-through 2026-12-31 --ownership-reason "my diligence notes" --standard-deliverable-confirmed-on 2026-10-30 --apply --reason "initial approval"`
   Without `--standard-deliverable-confirmed-on` (you checked the contract is a standard 100-share deliverable) **every contract is blocked**. Event review must be at most 7 days old; re-approve weekly.
8. Record your first snapshot: `csp daily-check-in` (preview), then confirm (section 3).

## 2. Telegram setup (BotFather + chat id)
1. In Telegram open **@BotFather**, send `/newbot`, follow the prompts, copy the token it gives you. Put it **only** in `.env` as `TELEGRAM_BOT_TOKEN=...`. Never paste it into a chat, ticket or AI prompt.
2. Open a **private chat** with your new bot and press **Start** (send `/start`).
3. Find your chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser **once** on your own PC (replace `<TOKEN>`), find `"chat":{"id":123456789,...,"type":"private"}`, and put the number in `.env` as `TELEGRAM_CHAT_ID=...`. Do not share that page. (The service itself never polls Telegram and accepts no commands.)
4. `csp telegram-setup` (preview), then `csp telegram-setup --apply`: verifies the chat is **private** and pins it (only a hash is stored). Group chats are refused.
5. Test, dry-run first: `csp telegram-test` (prints). To really send, set `CSP_DRY_RUN=false` in `.env`, then `csp telegram-test --send`. You should receive one message. All of these must hold for any send: `--send`, `CSP_DRY_RUN=false`, no `--dry-run` on the same command, token + chat id set, chat pinned. Otherwise nothing is sent and the command says why.

## 3. Daily routine (about 2 minutes)
1. After the close (or whenever your positions/orders changed): `csp daily-check-in` shows your recorded puts, orders, stock lots, last snapshot and expected collateral. Compare with your broker.
2. Confirm: `csp daily-check-in --equity <total account equity> --free-cash <NET free cash> [--mark LOT=PRICE] --ack-reconciled --apply --reason "end of day"`.
   - **NET free cash** = cash genuinely free for NEW puts, already reduced by collateral reserved for your open and working puts. The command rejects numbers where free cash + put collateral exceeds equity (`CASH_INCONSISTENT`).
   - Re-mark owned shares with `--mark`; marks older than 30 hours block entries.
   - **Required rule (daily/EOD check-in):** entries stay blocked (`DAILY_CHECKIN_MISSING`) unless your latest confirmed snapshot was taken after the close of the previous trading session. So do the check-in after the close or early the next morning, before the first scan (10:05 CT). A Friday-morning snapshot does not satisfy Monday; a Thursday-after-close one satisfies Friday.
3. `csp status` shows last/next runs, blockers, delivery state. `csp health` exits 1 if something is overdue or a message failed.

You will receive: scan results at about 10:05 and 13:05 CT (candidates, or a no-trade/blocked/data-issue status; a status message is sent once per day per distinct reason and immediately when the reason changes), monitor alerts for open puts, a heartbeat each trading morning, and a 15:30 CT reminder if today's check-in is missing. **Silence is never "all clear"**: look for the heartbeat.

## 4. After any trade event (do it right away, not only in the evening)
Each of these prints the reconciliation prompt; entries stay blocked (`LEDGER_UNRECONCILED`) until you run the daily check-in again.
| You did at the broker | Record here |
|---|---|
| Placed a sell-to-open order | `csp journal add-trade --trade-id P-001 --ticker XYZ --expiration 2026-12-18 --strike 40 --state PENDING --apply --reason "placed order"` (use `--state WORKING` if it is resting). 1 contract, standard 100 shares only. |
| Order filled | `csp journal fill --trade-id P-001 --credit 0.50 --fees 0.65 --apply --reason "filled"` |
| Cancelled an unfilled order | `csp journal cancel --trade-id P-001 --verified --apply --reason "cancelled"` |
| Placed a buy-to-close order | `csp journal close --trade-id P-001 --working --apply --reason "close order placed"` |
| Closing order filled | `csp journal close --trade-id P-001 --debit 0.20 --fees 0.65 --apply --reason "closed"` |
| Expired worthless (verified) | `csp journal expire --trade-id P-001 --confirm-expired --apply --reason "expired"` |
| Deposit / withdrawal | `csp journal record-cash-flow --amount -5000 --date 2026-10-30 --note "ACH out" --apply --reason "withdrawal"` (record it BEFORE the check-in that first includes it) |
The service never assumes a fill, a cancel or an expiry: until you record it, the put keeps reserving collateral and keeps being monitored.

## 5. Assignment procedure (3 steps)
If a put is assigned (or an alert says assignment is likely and your broker confirms):
1. **Record the assignment**: `csp journal assign --trade-id P-001 --date 2026-12-18 --apply --reason "assigned"`. The put becomes `ASSIGNED_PENDING_RECONCILIATION`; the obligation (shares x strike) stays visible and **new entries are frozen**.
2. **Record the shares and cash in one step**: `csp journal assign --trade-id P-001 --shares 100 --cost-basis-usd 4000 --fees 1 --apply --reason "shares received"`. This atomically creates the stock lot, releases the put collateral and counts the shares exactly once. `--shares` must equal contracts x 100; `--cost-basis-usd` is the total cash paid.
3. **Check in**: `csp daily-check-in ... --mark LOT-P-001=<price> --ack-reconciled --apply`. The freeze lifts only when all three are done and `csp reconcile-check` is clean.
Assigned shares are now your exposure (counts toward the ticker/sector caps). Selling them or writing calls is outside this tool.

## 6. Drawdown latch and manual reset
Drawdown is measured from the highest confirmed equity (seeded by your confirmed baseline, shifted by logged cash flows). At 3% entries pause, at 5% review exposure, at 8% hard halt. **Every level latches**: recovering equity does not re-enable entries. Exit and monitor alerts keep running. To resume after reviewing your account: `csp journal reset --reviewed --apply --reason "reviewed positions and plan"` (preview first; it re-baselines to your latest confirmed snapshot). A cash flow can never clear a latch.

## 7. Outages and what to do
| Symptom | Meaning / action |
|---|---|
| `DATA_UNAVAILABLE` / data-issue message, "Coverage ... PARTIAL" | Yahoo failed or throttled for some symbols. This is NOT a no-trade. Check the symbols manually. Retry later. |
| `DATA_ISSUE` on a position | No valid quote: check the position at your broker. Time (21 DTE) and assignment alerts still fire without quotes. |
| `BLOCKED_LEDGER` / `RECONCILIATION_REQUIRED` | Your record is stale/unreconciled/no baseline/latched. Run `csp reconcile-check` for the exact reasons, then the check-in. |
| No heartbeat / `csp health` flags `MONITOR_OVERDUE` or `SCAN_MISSED_TODAY` | The PC was off/asleep, logged out, or a task failed. See section 9. |
| `DELIVERY_UNKNOWN_ACK` | A send timed out after the request left; it may or may not have arrived and is **never auto-resent**. Check Telegram, then use `csp outbox`. |
| `DELIVERY_FAILED_FINAL` | Telegram rejected it permanently (blocked bot, wrong chat). Fix `.env` / re-run `telegram-setup --apply`. |
| `SERVICE_FAILURE` | The job crashed; nothing from that run is trustworthy. Run it by hand in dry-run to see the error. |
A stopped PC cannot announce its own outage. Duplicate or missing Telegram messages are possible (exactly-once delivery is not guaranteed).

## 8. Token rotation / compromise
1. In @BotFather: `/revoke` (or `/token`) to get a new token for your bot.
2. Edit `.env` (`TELEGRAM_BOT_TOKEN`). Never paste the token elsewhere.
3. `csp telegram-setup --apply` then `csp telegram-test --send`. Delete any chat/ticket where the old token appeared.
If your chat id changes (new account), re-run step 2.3-2.5; sends are refused while the pin does not match.

## 9. Scheduling on Windows and "the PC must be awake"
- Preview the tasks: `powershell -File ops\windows\register-tasks.ps1`. Register dry-run tasks: add `-Apply`. Register tasks that send: `-Apply -Send` (and `CSP_DRY_RUN=false`, section 2). Remove: `ops\windows\unregister-tasks.ps1 -Apply`.
- Times are Central; the script refuses unless the PC's time zone is Central. Scans 10:05 and 13:05 weekdays; monitor every 15 minutes from 08:30 CT (opening half-hour included) plus a daily 07:30 calendar check (weekends included); heartbeat 08:00; reconcile reminder 15:30.
- **The PC must be powered on and you must be logged in** (tasks are set to wake the PC, but a PC that is off or logged out cannot alert). On wake, only monitor/reconcile/heartbeat tasks catch up. **Entry scans are never replayed late** (a scan more than 15 minutes after its slot is skipped and recorded `SKIPPED_LATE`). One job runs at a time (single-instance lock); an overlapping run exits with code 3.
- Back up the journal regularly: `csp journal backup` (writes under `data\backups\`); keep a copy off this PC. Restore = stop the tasks, copy a backup over `data\csp_alerts.sqlite3`, run `csp doctor`.

## 9b. Paper-only scope and unresolved items
This version supports paper bookkeeping only: `CSP_ENV=real` and positions flagged `paper=False` are rejected (`doctor` shows FAIL; reconcile shows `REAL_POSITION_IN_PAPER_SCOPE`). An assignment you have not reconciled (`ASSIGNED_PENDING_RECONCILIATION`) is counted as exposure in `status`, produces an urgent monitor reminder each day, and blocks entries until you finish `journal assign` step 2. Future-dated snapshots or stock marks also block entries.

## 10. Known limits (read once)
Yahoo quotes are indicative and may be delayed; there is no exchange quote timestamp, so every alert says to verify at your broker. Delta is model delta, not a probability. Alerts do not cap losses. Real-money use needs your own paper-trading period first (spec 15.5). See SECURITY.md and DATA_DICTIONARY.md.

## 11. Local dashboard (read-mostly; writes: watchlist, refresh requests, and trades/check-ins you preview and confirm)
A browser view of what the journal already stores: Overview, Candidates and reasons, Positions, Alerts and activity (plus Run history), Health and reconciliation. It **only reads**. It never scans, sends Telegram, imports approvals, writes snapshots, migrates the database or changes dedup/reconciliation state; "Refresh" just reloads stored records. It is an advisory preview: it does not prove the Windows tasks are installed or that Telegram messages reached you. It is a separate process from the scheduled jobs: starting, stopping or crashing it cannot start, stop or duplicate a scan or monitor.

**One-time setup**
1. Make sure `csp journal init` has been run (the dashboard never creates or migrates the database).
2. Choose a password (12+ characters) and create its hash: `.venv\Scripts\python -m csp_dashboard hash-password` (prompts without echo, prints one `CSP_DASHBOARD_PASSWORD_HASH=...` line).
3. Paste that line into `.env` (see `.env.example`; the value is a salted scrypt hash, never the password; `.env` is git-ignored). Optional: `CSP_DASHBOARD_PORT` (default 8765).

**Start (foreground; keep the window open)**
```
.venv\Scripts\python -m csp_dashboard start
```
Then open `http://127.0.0.1:8765/` (or `http://localhost:8765/`) and sign in. Background alternative that survives closing the terminal but not logoff:
`Start-Process -WindowStyle Minimized -FilePath .venv\Scripts\python.exe -ArgumentList '-m','csp_dashboard','start'`

**Stop**: press Ctrl+C in its window, or close the window. If it was started with `Start-Process`: `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*csp_dashboard*' | ForEach-Object { Stop-Process -Id $_.ProcessId }`. A restart logs you out (sessions live in memory only). **No scheduled task is registered for the dashboard**, and none is needed.

**Rules of the page**
- Listens on 127.0.0.1 only; other Host/Origin values are refused; there is no LAN, remote or tunnel mode. Do not publish the port.
- Times are shown in Central with the UTC evidence beside them. "Page refreshed" is not the data time: look at the snapshot, run and journal-event times shown next to each figure. Anything unknown reads `unavailable`, never 0.
- Current blockers and the manual steps to clear them are always at the top. The steps are commands you run yourself in the terminal (RUNBOOK sections 3 to 6); the dashboard cannot run them.
- The next-run times on Health are estimates from the policy schedule. They do not show that a Windows task exists.
- `python -m csp_dashboard version-info [--write-manifest]` prints the code identity (Git revision when the project is in a repository, otherwise a hash of the source files) that is also stored in every scan/monitor run record.

## 12. Watchlist, market data and prices (Stage A)

**What it is.** You choose symbols to TRACK; a backend job fetches the stock price and the put chain for each and stores them; the dashboard shows what is stored. Tracking is not approval: a tracked, unapproved symbol shows data with a "not approved for entry alerts" warning and can never produce an entry alert (entry alerts come only from `config/approved.json` via the scan, which never reads the watchlist). Nothing is preselected. Active holdings (open, working, pending, close-working, adjustment-review and assignment-pending puts, and owned stock lots) are always tracked and refreshed, even if you remove them from the watchlist or their approval expires. The page shows why each symbol is tracked: `watchlist`, `active holding` or `both`.

**Feed and honesty.** Free Yahoo through the existing Stockpile adapter, shown as "Live updates · Yahoo indicative data · Quote age unknown". "Live updates" means the application keeps polling; it is not an exchange feed and not streaming. Yahoo gives no quote timestamp here, so provider market time is stored as unknown and every stock/option row is labelled UNTIMESTAMPED. Retrieval time is shown separately and never proves exchange freshness. Data-state labels on the page, computed when you look (so reloading never makes old data current): `UNAVAILABLE` (nothing stored), `STALE` (the newest attempt failed, or older than 15 min while the market is open; the last good value stays with its ORIGINAL retrieval time), `MARKET-CLOSED`, `DELAYED` (only if a provider timestamp exists and is over 60 s old), `UNTIMESTAMPED`. The stock price is a separate observation from option bid/ask; a stock price is never used as an option ask.

**Commands (Windows PowerShell, repo root).**
```
.venv\Scripts\python -m csp_alerts.cli watchlist list
.venv\Scripts\python -m csp_alerts.cli watchlist add SPYM TQQQ            # preview
.venv\Scripts\python -m csp_alerts.cli watchlist add SPYM TQQQ --apply --reason "why"
.venv\Scripts\python -m csp_alerts.cli watchlist remove SPYM --apply
.venv\Scripts\python -m csp_alerts.cli market-refresh --dry-run            # fetch + print, persist nothing
.venv\Scripts\python -m csp_alerts.cli market-refresh --force              # fetch now (still obeys backoff and a 60 s minimum gap)
.venv\Scripts\python -m csp_alerts.cli market-refresh --ticker SPYM
```
The same add/remove is on the Watchlist page of the dashboard (authenticated form with a per-session CSRF token; Origin and Host are checked; every change is an audit event; a double submit changes nothing). The watchlist holds at most 12 symbols (provider rate limits). A symbol is only format-checked when added; if Yahoo does not know it, the page shows the fetch error after the next refresh.

**Cadence (bounded, documented).** The scheduled task fires every 5 minutes on weekdays 07:30-16:30 Central and hourly at all other times (one extra hourly trigger, 00:00 to 23:00 daily); the task NEVER wakes the PC. The job decides what is due: each tracked symbol at most every **5 minutes while the US regular session is open** and at most **hourly otherwise** (weekends, holidays, overnight). A symbol is never fetched twice within 60 s. After a failure the symbol backs off (rate limit 15, 30, then 60 min; other errors 5, 10, 20, then 30 min); a rate limit stops the rest of that run (no hammering). Per run: at most 16 symbols (longest-waiting first) and the 8 nearest expiries per symbol (plus any held expiry). Browse window 14 to 60 DTE, widened to cover any held contract; it is for display only and no entry gate reads it (gates still use 35 to 45 DTE). One instance at a time: a second `market-refresh` exits with code 3 and does nothing (it has its own lock, so it can never make the monitor skip). The monitor also persists the option quotes it fetches into the same store (its exit advice gates are unchanged).

**Register the task** (preview first; this was NOT executed by the implementer): `ops\windows\register-tasks.ps1` previews; `-Apply` registers `csp-alerts-market-refresh` (weekdays 07:30-16:30 CT every 5 min plus an hourly trigger all day; does NOT wake the PC; 10 min time limit, no catch-up after wake, no overlap). Unlike the other tasks it has no wake-to-run, so a sleeping PC simply stops refreshing overnight. The task has no send mode. Check with `Get-ScheduledTask -TaskName 'csp-alerts-market-refresh' | Get-ScheduledTaskInfo`. The page's "next update" is a policy-derived estimate; it does not prove the task exists. If the PC is off or asleep nothing refreshes (market data is stale while the PC sleeps) and the page says STALE or MARKET-CLOSED with the original times; the banner then reads "Live updates failing/paused: showing last known data" instead of "Auto-refreshing page". The "Provider status" field shows only a fixed failure code and canned message (RATE_LIMITED, NOT_FOUND_OR_DELISTED, NO_OPTIONS_LISTED, TIMEOUT, NETWORK_ERROR, PROVIDER_FORMAT_ERROR, PARTIAL_DATA, UNKNOWN_ERROR); records written by an older version show "older record, text withheld".

**Refresh now (web).** The button writes a request row only; the next market-refresh run (within about 5 minutes) picks it up. The web process never contacts the provider. Limits: one request per minute, ten per hour, one pending at a time; a request not picked up in 15 minutes expires. It bypasses only the normal interval, never backoff or the 60 s gap.

**Provider limits.** Yahoo via yfinance is unofficial and rate limited; expect occasional `RATE_LIMITED` (shown on the page, backed off automatically). Option bid/ask of 0 means "no quote" (Yahoo cannot tell missing from zero) and is shown that way; when the market is closed most quotes are 0, and a previously good quote is retained rather than overwritten by zeros. The option contract id is the provider's if present, otherwise DERIVED (root + YYMMDD + P + strike); a derived id says nothing about the deliverable. Delta is Black-Scholes from Yahoo IV, indicative.

**Upgrading an existing journal.** Any CLI command opens the journal with `migrate()`; when a schema upgrade is pending on an existing journal it first writes a verified SQLite backup to `data\backups\pre-migrate-<from>-to-<to>-<time>.sqlite3` and refuses to migrate if that backup fails. Ledger, snapshots, drawdown, reconciliation and delivery state are not touched (the new tables are additive). Preferred explicit path: `.venv\Scripts\python -m csp_alerts.cli journal backup`, then `... journal init`. The dashboard never migrates: on an old journal the Watchlist page says to run `journal init`. To roll back, stop the dashboard and tasks and copy the backup over `data\csp_alerts.sqlite3`.

**Pages.** `/watchlist` (all tracked symbols: price, provider, price type, provider market time or "unknown", retrieved time and age, session, state labels, last attempt/success, next update estimate, provider status (a fixed failure code such as RATE_LIMITED or TIMEOUT with a canned message; raw provider text is never stored or shown), add/remove, refresh request, recent runs) and `/market/<SYMBOL>` (stock block plus the put chain: exact contract id, strike, expiry, bid, ask, volume, open interest, delta, last print, observation time, provenance; expiry/strike filters; a "passes contract gates (research only)" marker versus "browsable"). Both reload themselves every 60 s with a meta tag (no JavaScript); the reload only re-reads stored data. The chain stays visible when nothing passes or entries are blocked. Positions show the option bid/ask with its observation time and source (the newer of the monitor/refresh store and the last scan observation, matched on the exact contract).

**Not built yet (later stages).** Portfolio labels and the label-correction form, a per-candidate capacity display, alerts shown on the web, and the assignment web flow.

## 13. Recording trades and the account check-in on the web (Stage B1)

**What it is.** After you trade by hand at your broker or simulator, you record what happened in the browser instead of the CLI. The page records an external execution; it does not submit, change or cancel any order, and it never uses a quote as your fill: you type the ACTUAL credit/debit, fees and execution time. The CLI commands in section 4 stay available and produce the same ledger rows (the web calls the same `Journal` code and the same cash/timestamp rules).

**Sold put (filled).** Market page -> a put row -> **Record sold put**. The form shows the exact contract (ticker, OCC id, right P, strike, expiry, multiplier 100, 1 contract, paper scope) read-only, with the stored bid shown as a labelled *reference only*. You enter actual credit per share, fees, execution date (YYYY-MM-DD) and time (HH:MM, Central). Quantity other than 1 is refused with the CLI message. **Step 1 Preview** (writes nothing) shows the exact state change, the collateral reserved (strike x 100 x 1), every cap with limit, used now, this trade, used after and room (pilot $15,000, normal ceiling, ticker, sector, correlation bucket, expiry week, contract counts, company names, free cash) and every policy rule the trade would breach. **Step 2 Confirm** needs an explicit reason (stored in the audit log) and any acknowledgement box the preview lists. A confirmed fill becomes **OPEN** and is monitored by the next monitor run; no CLI step is needed.

**WORKING vs OPEN.** If you placed an order that has NOT filled, use **I placed an order, not filled yet**: the position is **WORKING**, reserves collateral, shows no premium and is not monitored as an open put. When the broker confirms the fill, open the position and use **Mark filled** (actual credit, fees, time) to make it OPEN. A WORKING order that never fills: **Order was canceled (unfilled)** (tick the "verified at my broker" box).

**Closing.** On an OPEN position: **I placed a closing order (not filled)** -> CLOSE_WORKING (collateral stays reserved, still monitored), then **Record the confirmed closing fill** (actual debit, fees, time) -> CLOSED, which releases the collateral and stops open-position alerts (queued advice for it is dropped at send time). **Closing order was canceled** returns CLOSE_WORKING to OPEN. **Record expiry** needs the "no late exercise/assignment" box, refuses a future date and a date before the expiration.

**Policy breaches are recorded, not rejected.** If what you report breaches a cap, the approval rules (not approved, expired approval, strike above your maximum, event clearance) or has unknown sector/bucket labels, the preview says so and requires an acknowledgement. The trade is then recorded as it really happened, flagged (`POLICY_BREACH_RECORDED`), shown on the Positions page, and **new entries stay blocked** (`POLICY_BREACH_UNRESOLVED`, shown in the blockers) for as long as it reserves collateral. Closing, canceling or expiry clears it, and it also clears by itself once the breach no longer reproduces (see the end of this section). Contract-level results from the stored quote (delta, DTE window, spread, ...) are shown for information only.

**Reconcile now.** Every trade confirmation shows the reconciliation prompt. New entries stay blocked (`LEDGER_UNRECONCILED` / `CHANGE_AFTER_SNAPSHOT`) until a new confirmed snapshot exists. Use **Check-in** in the top menu: it lists your positions, orders and stock lots from the journal (no retyping), then you enter total equity and NET free cash (and optional stock re-marks) and tick the reconciliation acknowledgement. Preview first, then confirm. Same rule as `csp daily-check-in`: free cash + put collateral must not exceed equity (`CASH_INCONSISTENT`).

**Safety nets.** A preview expires after 20 minutes and is bound to your session, the exact values, the ledger state and the policy: if anything changed ("ledger changed, preview again") or a field was altered, nothing is written. Each confirm carries a one-time idempotency key, so a double click or page reload records one trade. If the browser or server dies between preview and confirm, nothing was written; preview again. Everything is one database transaction: all or nothing. Every record is audited with actor `web:owner` and source `web`.

**Still CLI-only (restricted controls, by design).** Baseline confirmation (`journal confirm-baseline`), cash-flow recording, drawdown risk reset, approvals, assignment (all three steps), partial fills, adjustments and any quantity other than 1 contract. If a position is in one of those states the web page says "use the documented CLI procedure", the obligation stays visible and entries stay blocked.

**Changes after the Stage B1 test pass.** The fee, closing-fee and expiry-date boxes no longer pre-fill, and the SERVER now refuses a blank or missing fee ("Enter the actual fees; type 0 if none."; an explicit 0 or 0.00 is accepted); the order type is spelled out on the forms ("Sell to open", "Buy to close"). Numbers accept ASCII digits only. A policy-breach flag now clears BY ITSELF (audited `POLICY_BREACH_CLEARED`) when the recorded breach no longer reproduces against the current book, approvals, labels and policy (for example you closed the other put that caused the cap breach, corrected a label, or you confirmed a new check-in after an INSUFFICIENT_FREE_CASH report). It is re-checked after every web record, every real scan, and every applied ledger CLI command; the monitor job does not re-check it (it has no approvals). There is no review button: the flag stays while any cap is still breached. Contract-level results (delta, DTE window, spread, ...) never block recording; they are saved with the record and shown on the position page as "Recorded with off-strategy contract characteristics (information)".

**Not built yet.** Assignment on the web (CLI only), partial fills, multi-contract positions, editing or undoing a recorded trade (use cancel/close or the CLI), charts and history views.

## 14. Portfolio, label correction, candidate capacity and alerts on the web (Stage B2; WT-04, WT-05)

**Portfolio page (menu: Portfolio).** Read-only. It shows active positions (every put that reserves collateral, including pending/working orders and positions with a closing order) separately from active contracts and from the lifetime count of recorded trades; the company-name count; put collateral, owned-stock reference value and unresolved assignment obligations (the backend exposure rules; a closing order does NOT release capacity); cash from your latest snapshot; exposure and limit for every ticker, sector, correlation bucket and expiration week; every portfolio-wide limit; which limit is tightest in each unit; and every holding with ticker, sector, asset type and correlation bucket. A sector or bucket shown as UNKNOWN is flagged: it is never counted as diversified and a candidate whose concentration cannot be assessed fails `LABELS_UNKNOWN`. When any limit is over or has no room the page lists, in plain rule-based words, which limit binds and the remaining capacity by bucket, sector and expiration week. It recommends no trade, hedge, alternative sector or movement of capital.

**Correct labels.** Portfolio -> a holding -> **correct labels**. You state the sector and correlation bucket you confirm for a ticker you hold. Step 1 previews (writes nothing) and shows the exposure under the old and new labels; step 2 needs a reason and a tick box and is audited (`LABEL_CORRECTED`, holding every previous value, actor `web:owner`). It rewrites the labels stored on that ticker's live puts and stock lots, which is where the exposure rules read them. It never touches approvals: for a ticker on your approved list the only accepted correction is the one that makes the ledger rows equal the approval's own labels (to change the approval use the CLI `journal approve`). **Asset type cannot be corrected on the web (decision: it stays approval-only for now; no label table, no placeholder approvals)**; the label page says "asset type: set by approval (CLI)": the backend reads it only from the approval record, and a name without one is counted as an individual company (the conservative default). The web has no provider-suggested labels to override (none are stored); labels come from your approval record or from what you typed.

**Market page capacity.** For every put row the new column shows the full entry-gate verdict: "passes all gates" only when approval, event clearance, every contract rule, the ledger, drawdown, the session, every portfolio cap, free cash and the labels each pass independently; otherwise every failing rule code. It also shows current aggregate exposure + this put's collateral = post-trade exposure, the tightest remaining room, and (expand "capacity by rule") each cap with limit, used, added, used after and room. An unapproved tracked ticker shows prices, "NOT APPROVED: no eligible marker" and its capacity rows. Exhausted capacity never hides a price. The older "Contract-gate marker" column is unchanged: it is the research-only contract-level result and is not an entry marker.

**Alerts page.** One row per stored alert: stable alert id, advisory type, severity, source (position link, ticker, decision reference), generated time and delivery state (queued, sending, retry scheduled, acknowledged by the Telegram API, unknown acknowledgment, failed, dropped with the reason). An acknowledgment does not prove you read or acted. The page cannot resend or act. Position pages link to their alerts. The alerts page and each position page show the active exit thresholds and the policy version: they are provisional policy values; the owner decides the final profit, loss and delta thresholds later.

**Not built.** Assignment web flow (CLI), charts, history/time-series views, asset-type correction, scenario or AI suggestions.
