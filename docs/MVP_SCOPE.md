# MVP scope (owner direction, 2026-10-30)

> "Deliverable as early as possible. Add features in later releases; use real feedback from a working model to make it more robust."

The full spec (CSP-TELEGRAM-001) stays the long-term target. This file says what ships first. **Safety gates never get cut**: advisory-only, no broker access, fail-closed entry gates, manual ledger, drawdown latch. What is cut is breadth, polish and heavy assurance.

## MVP = a working loop on the owner's Windows PC
1. Owner enters approvals, baseline, snapshots and trades (CLI/CSV) - DONE (slice 1)
2. Scan approved tickers with Yahoo via Stockpile, apply all hard gates, rank, dry-run preview - slice 2 (in progress)
3. Monitor open puts: profit / 21-DTE / loss / delta / assignment alerts - slice 3
4. Send to ONE private Telegram chat with `--send`, outbox with retry + basic dedup - slice 3
5. Run on schedule on Windows (Task Scheduler entries + scheduler-safe single-instance lock), daily reconcile reminder, simple heartbeat/health command - slice 3
6. Runbook (setup, daily routine, assignment handling) + sample alerts - slice 3

## Triage of independent findings (IT-xxx)
| Ticket | MVP | Decision |
|---|---|---|
| IT-001 cash semantics | YES (done) | Net free cash, as-of snapshot, safeguard CASH_INCONSISTENT. Document in runbook. |
| IT-002 multi-account scope | LATER | MVP is ONE account scope (config `account_scope`, default `paper_primary`). Reject mixed scopes. |
| IT-003 stale queued eligibility | YES, minimal | Entry alerts carry an expiry (default 30 min) and are revalidated against ledger/policy revision before send; expired -> dropped, logged. |
| IT-004 standard-contract evidence | YES, minimal | Yahoo gives no deliverable data. MVP rule: contract is standard only if OCC-style symbol has no digit suffix (no adjusted `1`/`2` mini/adjusted roots) AND owner marks ticker `standard_deliverable_confirmed` in approvals. Otherwise block (`NONSTANDARD_CONTRACT`). |
| IT-005 Tier R exit alerts | DECIDED (owner accepted recommendation) | Send indicative price-review alerts (PROFIT_EXIT / LOSS_REVIEW / DELTA_REVIEW) labelled "INDICATIVE - quote age UNKNOWN - verify at your broker"; never "live/current". Suppress price alerts (DATA_ISSUE instead) when fetch failed, quote crossed/zero/invalid, or contract mismatch. Time/assignment alerts always sent. Make the Tier R price-alert switch a policy key (default on) so it can be tightened after feedback. |
| IT-006 assignment exposure | partly done | Assigned-pending and adjustment states keep exposure visible. Atomic assignment reconcile: keep. |
| IT-007 multi-contract / partial close | LATER | MVP records 1-contract positions; anything else is rejected with a clear message and freezes entries until owner resolves. |
| IT-008 deposits/withdrawals | DECIDED (owner) | Owner logs external cash flows from platform history via `journal record-cash-flow --amount <+deposit/-withdrawal> --date --note` (audited, preview first). The drawdown peak is shifted by the same amount (never below the latest equity baseline logic; peak cannot go negative), so a withdrawal is not a loss and a deposit does not hide one. A cash flow can NEVER clear or downgrade a latched drawdown level; only the manual risk reset does. Versioned policy change recorded in DECISIONS.md. |
| IT-009 Windows schedule | YES | Task Scheduler + opening-half-hour coverage (08:30 CT monitor start; do NOT copy the spec's 09:00 cron). Missed-run policy: on wake run monitors only, never replay stale entries. |
| IT-010 last-trade print | DONE | Missing/stale blocks. |
| IT-011 dedup / crash recovery | MINIMAL | Dedup keys + stable alert IDs + unknown-ACK state. No lease/worker framework. |
| IT-012 manual CLI | YES, core only | CLI for: confirm-baseline, snapshot, approve, add-trade, fill, close, expire, assign, reset. Preview by default. |
| IT-013 health | MINIMAL | `health` command + heartbeat message; per-symbol coverage in digest (partial scans labelled). |
| IT-014 recipient pinning | YES, small | Verify chat is a private chat at setup, pin chat ID; dry-run never enqueues sendable items. |
| IT-015 exact upstream / license | DONE-ish | Pinned SHA cloned and verified; CC BY-NC noted (personal use only). |
| IT-016 conflicting examples | YES, cheap | See crosswalk below; spec's $16K PASS example is invalid under the pilot cap. |
| IT-017 backup/restore/import | LATER (basic backup only) | `journal backup` using SQLite backup API; restore drill later. |
| IT-018 pilot measures | LATER | Simple 90-day report after first feedback. |

## Explicitly deferred to later releases
AI reviewer; Tier V verified quote feed; multi-account scopes; partial fills/multi-contract; portfolio stress tests; historical backtest; flow-adjusted drawdown; worker leases; restore drills; pilot analytics; dashboards; filter-driven universe (market cap/sector screener); roll suggestions.

## Crosswalk: retire stale requirements
- `REQUIREMENTS.md` R3 (entry TBD / defaults) -> superseded by spec 4.4 and `config/policy.yaml`.
- R2 (fixed tickers, later filters) -> owner-approved whitelist (filter-driven universe deferred).
- R1 "real-time" -> scheduled scans plus 15-minute monitor polling (Yahoo is delayed).
- R4 yfinance -> via Stockpile adapter only.
- R6 Windows hosting -> stays; spec 12.2 Linux/cron examples are illustrative only.
- Any YAML positions file or Telegram `/add` command idea -> dropped. SQLite is canonical; no inbound Telegram commands.
- Spec 6.3 / 9.3 examples showing $16,000 PASS conflict with the $15,000 pilot cap; treat as post-pilot examples only.

## Owner workflow feedback (2026-10-30) - MUST be in the first delivery (slice 3)
| ID | Requirement |
|---|---|
| F1 | **Silence is never ambiguous.** Every scheduled scan/monitor run ends in exactly one status the owner can see: `CANDIDATES`, `NO_OPPORTUNITIES`, `BLOCKED_LEDGER` (stale/unreconciled/no baseline/latched drawdown), `DATA_UNAVAILABLE` (incl. partial coverage per symbol), or `SERVICE_FAILURE`. Entry candidates are suppressed when blocked, but a status message (once per day per distinct reason, immediately on a change of reason) says why. A missed run is itself detected locally (`health`). |
| F2 | **One `daily check-in` command** (preview first): shows current open/working positions, orders, stock lots, last snapshot, expected collateral; asks the owner to confirm or correct equity and NET free cash and acknowledge reconciliation. Positions/orders are displayed for confirmation, not retyped. Rejects cash inconsistent with equity (CASH_INCONSISTENT). |
| F3 | **Reconciliation after any commitment-changing event** (order placed, fill, cancellation, close, assignment, expiry, cash flow), not only in the evening. The CLI prints this prompt after every such command; entries stay blocked (`LEDGER_UNRECONCILED`) until a new confirmed snapshot. |
| F4 | **Assignment procedure**, documented in RUNBOOK.md and guided by a CLI command (`journal assign` with preview): (1) record assignment of the put (trade_id, date) -> state ASSIGNED_PENDING_RECONCILIATION, obligation stays visible, entries frozen; (2) record shares received (qty, price/cost basis, date) and cash paid (+fees) in one transaction; (3) complete a daily check-in (new snapshot). Entry freeze lifts only when all three are done and reconcile-check is clean. |
| F5 | **Alert usefulness.** Every alert shows: exact contract (ticker, expiry, strike, right, contracts), the trigger/reason, data tier and quote age ("UNKNOWN"), the account snapshot time used, and the next manual action. Candidate alerts also show the active profile and binding cap (e.g. "Pilot cap $15,000: $X used, $Y room"), and "alternatives are not authorization to stack; capacity is rechecked after you record a fill". |
| F6 | **Operational confidence.** `status` command (and heartbeat line in the daily digest) shows: last successful scan, last successful monitor, next scheduled run of each, current blockers, last delivery result. Runbook states plainly: the Windows PC must be awake and logged-in (or the tasks set to run when asleep/wake) for scheduled alerts; a stopped PC cannot announce its own outage; the monitor on wake never replays stale entries. |

## Definition of "MVP done"
Owner can: install, set baseline and whitelist, record a paper trade, receive a real Telegram scan alert and a monitor alert from scheduled jobs on Windows, with all hard gates enforced, and with independent tester PASS on the acceptance suite. Real private Telegram delivery is verified by the owner, not by mocks.
