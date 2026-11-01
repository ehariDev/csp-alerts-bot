# Requirements — start here

The product is a local web put tracker: **choose tickers → see updating stock/put quotes → record a manual paper trade → monitor and alert → review concentration/capacity → record closure**.

## Active files

| File | Purpose |
| --- | --- |
| [PRODUCT_WORKFLOW_REQUIREMENTS.md](PRODUCT_WORKFLOW_REQUIREMENTS.md) | Single product contract, first-delivery scope, feed choice, market-data/ticker-check behavior and retained future backlog |
| [CSP_ALERTS_WORKFLOW.md](CSP_ALERTS_WORKFLOW.md) | Owner-facing explanation of the complete journey |
| [CSP_ALERTS_FLOWCHART.md](CSP_ALERTS_FLOWCHART.md) | Visual flowchart |
| [PUT_SELLER_TELEGRAM_SPEC.md](PUT_SELLER_TELEGRAM_SPEC.md) | Preserved authoritative financial/data-quality rules and manual-execution/no-broker boundary |
| [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md) | Single test contract; AT, WT-AT, MD-AT, TC-AT and WD-AT identifiers retained |
| [OPEN_ISSUES.md](OPEN_ISSUES.md) | Single delivery-status queue, unresolved defects and operational checks |

Read product requirements, specification, acceptance criteria and current issues for implementation. The owner's newer web workflow supersedes old CLI-only/read-only sequencing, not financial limits. Routine fill/check-in/close forms and automatic prices are part of the first useful delivery. Charts/advanced workflows remain later.

## Current decision and verdict

Feed: **free Yahoo through Stockpile**, automatically updated and labeled **Live updates · Yahoo indicative data · Quote age unknown**. No paid feed is planned; exchange-real-time freshness is not claimed.

**Stakeholder acceptance: FAIL / not accepted.** In the [latest recheck](evidence/2026-10-31-recheck/REPORT.md), the original three failing update scenarios now PASS and browser labels confirm the fixes. Four additional cases FAIL across two follow-up P1 findings: total provider failure/empty-chain numeric advice, and older/same-second evidence ordering. [Current findings](evidence/2026-10-31-recheck/FINDINGS.md). The full suite finished with **3,303 PASS, one expected failure and five warnings**; all165 tracked inputs stayed unchanged. Passing suite results do not close the four supplemental failures. Real-provider-to-browser behavior, owner-authorized Telegram receipt and actual scheduled operation remain unverified.

## Evidence and history

[evidence/](evidence/README.md) contains dated execution records and working review scripts. Evidence is not a second backlog. [archive/](archive/README.md) preserves superseded documents/research; it is background, not active implementation instructions.

October31 consolidation moved overlapping scope/roadmap/market-data/ticker-check documents into the product and acceptance files. No requirement was marked delivered by archiving. [Cleanup manifest](archive/2026-10-31-consolidation/manifest.json) records preserved originals and hashes. The specification and raw test artifacts were not changed.

REQUIREMENTS.md and IMPLEMENTATION_ACCELERATORS.md are tiny compatibility redirects retained because documents outside this folder reference them. All cleanup work stays inside requirements.
