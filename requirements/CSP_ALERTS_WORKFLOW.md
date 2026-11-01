# CSP Alerts — owner workflow

Updated October 30, 2026. This document describes the required product experience, not a claim that every step is implemented. Read alongside [product requirements](PRODUCT_WORKFLOW_REQUIREMENTS.md), [first-delivery scope](PRODUCT_WORKFLOW_REQUIREMENTS.md#first-delivery-scope-and-feedback), [market-data acceptance](PRODUCT_WORKFLOW_REQUIREMENTS.md#market-data-contract) and [acceptance criteria](ACCEPTANCE_CRITERIA.md). Existing financial limits and manual-execution boundaries remain in force.

## The complete loop

**Choose tickers → see updating stock and put quotes → review eligibility and portfolio capacity → place a trade yourself → record the actual fill in the web page → monitor the position and receive alerts → record closure or another event → refresh portfolio capacity.**

The local authenticated web page is the normal interface. Installation and advanced recovery may use documented commands; routine use must not require a terminal. The app never submits an order or connects to a brokerage.

## 1. Choose what to watch

Add or remove tickers in the web watchlist. The list persists after restart. For each ticker, show whether it is watched, held, approved for entry recommendations, or blocked and why.

Watching prices is separate from approving a security for ownership. An unapproved ticker can show stock and put prices but cannot generate an eligible entry alert. Removing a ticker from the watchlist or letting its entry approval expire does not stop monitoring an existing holding. Ownership approval, sector/correlation labels and event clearance remain explicit owner-controlled inputs.

## 2. See stock prices and puts update

For each tracked ticker, display the latest retrieved stock price and available puts. Put rows identify the exact contract, expiry, strike, bid/ask, and available liquidity fields. Let the owner filter expiry/strike and select a contract. The entry scan's DTE/delta rules identify eligible research candidates; they do not hide the whole chain or stop monitoring contracts outside those ranges.

Prices update automatically through a backend market-data process, independently of entry eligibility and account reconciliation. Show provider, price type, authoritative market timestamp when available, retrieval time, age/delay status, last attempt/success and update status. Stock observations and option quotes have separate provenance. Page refresh time is not quote time.

Display delayed, untimestamped, stale, market-closed and unavailable states distinctly. Retained old prices keep their original timestamps. Do not invent values or silently use fixtures when a provider fails. The owner selected free Yahoo through Stockpile: show “Live updates · Yahoo indicative data · Quote age unknown.” Automatic retrieval is required; guaranteed exchange real-time freshness is not claimed. No further feed decision blocks implementation and no paid subscription is authorized.

## 3. Understand eligibility and portfolio impact

Keep prices visible even if no trade qualifies. For a candidate, show current exposure, proposed collateral, post-trade exposure, remaining capacity and every failed rule. Separate a browsable contract from an eligible new-entry recommendation.

The portfolio view groups obligations by ticker, sector, correlation bucket and expiration week. Show active position count, active contract count and company-name count separately from lifetime trades. Include applicable working orders, stock holdings and unresolved assignment obligations. Unknown classifications cannot count as diversified.

| Existing rule | Starting limit to retain |
| --- | --- |
| Reference account | $100,000; confirmed actual equity governs drawdown |
| Default pilot put collateral | $15,000 |
| Normal aggregate exposure ceiling | $25,000; does not override the pilot cap |
| Emergency exposure incident threshold | $30,000; never entry headroom |
| Combined exposure per ticker | $9,000 |
| Combined exposure per sector | $10,000 |
| Active short-put contracts | 4 total; one per underlying |
| Individual-company names | 2, including applicable puts and stock |
| Short-put collateral per expiration ISO week | $15,000 |

Apply all rules together with free-cash, ownership, event, liquidity, drawdown and reconciliation gates. These are exposure budgets, not guaranteed maximum losses. Reuse backend calculations; do not create a second browser risk engine.

When a limit binds, explain the concentration and remaining capacity. Suppress ineligible new-entry notifications while preserving their visible blocked reasons. Continue all applicable existing-position risk alerts. Alternative approved tickers may appear only if they independently pass every rule; sector variety is not a promise of safety. Automatic hedging, closing or capital movement is excluded.

## 4. Record what actually happened

The owner places the order externally. In the web page, select the contract and choose **Record sold put**. Enter actual quantity, fill credit per share, fees and execution date/time; verify paper account and contract identity. Preview the exposure change and explicitly confirm.

An actual filled sale becomes OPEN and starts exact-position monitoring automatically. Merely placing an unfilled order records WORKING, reserves collateral and waits for fill confirmation. A market quote is never assumed to be an execution price. Duplicate clicks/retries must not create duplicate positions.

Record already-executed reality even when it breaches a limit, using the backend's explicit exception/review path or a visible unresolved obligation. Flag the breach and block new entries; do not make an inconvenient trade disappear. Unsupported partial fills/adjustments must remain visible for reconciliation.

After a trade/account event, complete the minimal web check-in: confirm orders/positions and enter confirmed equity and net free cash under existing semantics. New capacity-verified entry alerts stay blocked until reconciliation is current. Market prices and holding monitoring continue meanwhile.

## 5. Monitor and receive alerts

The position view shows the exact held contract, actual entry premium, latest option quote and timestamps, DTE, reserved collateral, indicative closing P&L and warnings. Estimate buy-to-close cost from a qualifying option ask, not the underlying stock price or an old option last print. Missing evidence means unavailable numeric P&L plus a data warning.

Position alerts cover the configured profit/loss/delta review conditions, time/expiry, assignment, data problems and portfolio risk. Final exit thresholds will be decided with the owner later; existing policy values remain visibly provisional until explicitly changed. No threshold optimization or financial-limit relaxation is implied.

Use precise terms: **sell to open** creates a short put; **buy to close** exits it. Alerts are advisory. After explicit owner enablement, send them to the private Telegram destination and show queued/sent/failed/unknown-acknowledgment status in the page. A provider/API acknowledgment is not proof of owner receipt or action. Deduplicate routine repeats without suppressing a newly escalated warning.

Price failures suppress unsupported numeric advice, not calendar reminders or the data-failure warning itself. Entry capacity exhaustion never stops monitoring existing positions.

## 6. Record closure and other events

Record an intended closing order as CLOSE_WORKING. Keep monitoring and collateral reserved until the owner confirms the actual buy-to-close fill and fees. Confirmed closure moves the position into history and invalidates obsolete queued open-position advice. Reconcile the account before treating released capacity as verified for new entries.

Assignment is not automatic closure or zero exposure: retain the obligation, show the review requirement and block new entries until the supported reconciliation process records shares/cash correctly. Advanced assignment/partial-fill automation can follow later. Record deposits/withdrawals through the audited supported workflow; never disguise them as investment profit or clear a halt by changing a baseline silently.

## 7. Keep it running and understand failures

Backend collection, entry evaluation, position monitoring and alert delivery operate independently of whether the browser is open. They may share existing job infrastructure; this does not mandate a new service for each function. Preserve observation/run history and coherent financial records.

Show last attempted/successful updates, data coverage, blockers and delivery errors. A schedule-derived next-run estimate does not establish that a Windows task is installed. Verify actual startup, scheduling and recovery separately. The PC must be running with the required jobs active; an asleep/offline PC cannot reliably announce its own outage. Support backup and restart without losing positions or replaying expired opportunities.

## Delivery checkpoints and acceptance

1. **First complete slice:** one owner-selected ticker, actual provider-to-browser stock/put data with an automatic update, one recorded paper fill, exact-position monitoring, a controlled alert, a confirmed close and correctly updated capacity. Include the minimal labels/check-in needed for this journey.
2. **Usable pilot:** a small owner-selected list, portfolio boundary checks, outage/recovery, persistence/backup, authenticated forms, and an owner walkthrough. Actual private Telegram receipt needs explicit owner enablement and observation. Execute WT-AT-01..10 and MD-AT-01..06 with scoped evidence.
3. **Later:** charts/history comparisons, advanced corrections, AI, scenario engines, analytics, multi-account and remote hosting. Do not wait for 90 days before delivering the working loop.

## Current delivery status — do not confuse tests with acceptance

Earlier independent results remain valid for their recorded scope: 1,870 unit/acceptance tests, 12 backend probes, three persisted workflow checks and 30 HTTP assertions passed. A browser walkthrough verified stored-data navigation/login/logout. These results do not prove the complete workflow above or a live market-data experience.

**Overall stakeholder acceptance remains NOT ACCEPTED.** Actual provider-to-browser updates and the complete web trade/monitor/risk journey require independent evidence under the updated requirements. Real Telegram receipt and installed Windows task behavior remain unverified by the reviewer. See [OPEN_ISSUES.md](OPEN_ISSUES.md) for the single current status queue and the [recorded report](evidence/2026-10-30-web-verification/REPORT.md) for prior test scope. No new implementation or test execution is claimed by this document.
