# Current open issues and verification status

> **Stakeholder acceptance correction:** The stored-data tests below passed, but the current ticker-price experience was not verified and the delivered dashboard does not satisfy that need. Dashboard stakeholder acceptance is **NOT ACCEPTED** pending [market-data requirements](PRODUCT_WORKFLOW_REQUIREMENTS.md#market-data-contract). Historical test outcomes remain valid only for their recorded scope.

Consolidated October 30, 2026 from the independent findings, status tracker and latest retest. Existing IT, AT and SR identifiers are preserved. This is the active backlog; archived documents supply historical rationale, not additional release gates.

## Latest October31 recheck — current blockers

**Overall verdict: FAIL.** [Latest report](evidence/2026-10-31-recheck/REPORT.md), [findings](evidence/2026-10-31-recheck/FINDINGS.md).

- **RV-20261031-01: original correction VERIFIED.** Contradictory stock evidence after a good fetch now blocks candidate verdicts and is visibly stale/retained.
- **RV-20261031-02: original correction VERIFIED.** Newly invalid quotes suppress current numeric advice, preserve timestamps, leave unaffected contracts usable and recover on valid updates. Three unchanged prior sequence scenarios now PASS; browser/trade-form labels verified.
- **RV-20261031-03 / P1 OPEN:** a complete provider error or empty chain still leaves the position's retained quote usable with PROFIT_EXIT/$35.00, while the market page says STALE. Reproduced in domain and actual browser.
- **RV-20261031-04 / P1 OPEN:** an older monitor write can overwrite newer evidence/clear its failure; later invalid evidence within the same stored second also fails to invalidate prior advice. Deterministic interleaving/tie reproductions, not a claim of observed real-job overlap.

Four additional scenarios FAIL across RV-03/04. The nine original ticker assertions and12 backend probes PASS. Full regression completed: **3,303 PASS, one xfail, five warnings**, exit0; all165 listed inputs unchanged. The xfail is the known B2-3b cosmetic rounding residual, not a passing case. The four supplemental failures remain blockers despite the passing existing suite. Known B2-3b half-cent display remains a cosmetic residual. Prior review results are historical; actual public-feed/owner-operation acceptance remains pending.

## Current product pickup queue — highest priority

**Overall stakeholder acceptance: NOT ACCEPTED.** The [current workflow](PRODUCT_WORKFLOW_REQUIREMENTS.md) requires a usable web price/trade/monitor/risk loop. None of WT-AT-01..10 is established by the previous stored-data review. Preserve the passing backend evidence below without presenting it as completed product acceptance.

| Work item | Priority / status | Required outcome |
| --- | --- | --- |
| WT-01/02 + MD | Original update defects verified corrected; RV-03/04 OPEN; real provider unverified | Chosen ticker watchlist, automatic stock/put feed, truthful timestamps, persist relevant monitor observations, actual provider-to-browser evidence independent of entry eligibility |
| WT-03 | Core synthetic browser fill/check-in/close verified; complete acceptance pending | Actual-fill/order/check-in/confirmed-close web workflows, audited preview/confirm, duplicate/stale protection; recorded trade becomes monitored |
| WT-04 | Synthetic exact-position signal/closure verified; RV-03/04 OPEN; real sends unverified | Exact-position data/P&L and alerts; closure invalidates obsolete advice; owner decides final exit thresholds later |
| WT-05 | Synthetic hand totals and existing regressions pass; complete acceptance pending | Portfolio labels/totals, all existing concentration limits, capacity reasons and rule-based mitigation; new-entry suppression with existing-position monitoring retained |
| WT-AT-09/10 | Release acceptance pending | Authenticated operational web journey, persistence/recovery and owner-authorized receipt/walkthrough |
| Feed selection | RESOLVED by owner; implementation/verification pending | Free Yahoo through Stockpile. Live updates with indicative/unknown-quote-age labels; no verified-real-time claim or paid subscription. See market-data acceptance. |

Implement the first complete one-ticker slice before widening scope. Basic forms and concentration visibility are now core delivery; historical charts/AI/remote hosting remain deferred. Record actual evidence against WT-AT cases, not merely test counts.

## Current agent delivery — ticker-check

**Prior agent-reported verdict: FAIL; October31 targeted correction checks now PASS.** The owner supplied a tester report of 2,000 passed / eight failed cases in a new defect test file. This is a different snapshot from the prior reviewer suite below. Exact source identity, command and raw results remain required.

See [ticker-check contract](PRODUCT_WORKFLOW_REQUIREMENTS.md#ticker-check-contract): TC-01 ticker controls, TC-02 error-message controls, TC-03 bounded batches, TC-04 contradictory spot/verdict, TC-05 unverified-earnings wording, TC-06 truthful empty-chain wording. The nine October31 independent targeted assertions pass for these original correction categories; consult the dated review for broader regression scope. Reported arithmetic, scan parity and read-only behavior should be preserved, not reimplemented or treated as newly reviewer-verified.

The command is supporting diagnostic work. Passing it will not close the missing live/updated web-data and complete trade/monitor/risk acceptance queue above. No financial threshold or entry gate is changed by these corrections.

## Current verdict and evidence

**Prior verification PASS for its limited backend/read-only test scope; clarified product acceptance remains NOT ACCEPTED.** The prior October30 run passed 1,870 unit/acceptance tests, all 12 independent probes, three persisted workflow confirmations and 30 independent HTTP assertions. A real browser verified login, all seven views and logout. This closes the previously pending regression/workflow retest for the supported paper-pilot behavior. It does not establish production deployment or owner operational acceptance.

| Executed check | Observed result | Evidence |
| --- | --- | --- |
| Full unit/acceptance suite | 1,870 PASS; zero failures/errors/skips; five pandas deprecation warnings | [Summary](evidence/2026-10-30-web-verification/suite-summary.json), [JUnit XML](evidence/2026-10-30-web-verification/suite.xml) |
| Independent backend probes | 12 PASS / 0 FAIL; exit 0 | [Probe results](evidence/2026-10-30-web-verification/backend-probes.json) |
| Persisted workflow checks | 3 PASS: same-second withdrawal ordering; valid OPEN outbox control; CLOSED advice suppression | [Workflow results](evidence/2026-10-30-web-verification/backend-workflows.json) |
| Independent dashboard checks | 30 HTTP assertions PASS; real browser seven-view/login/logout PASS; DB unchanged | [HTTP](evidence/2026-10-30-web-verification/web-http-checks.json), [Browser](evidence/2026-10-30-web-verification/browser-results.json) |
| Source consistency | 138 listed inputs; zero changes during full suite; root Git metadata unavailable | [Manifest](evidence/2026-10-30-web-verification/source-manifest.json), [Code identity](evidence/2026-10-30-web-verification/code-identity.json) |

The [current report](evidence/2026-10-30-web-verification/REPORT.md) records commands, content identity and limits. Earlier full-suite and failed-probe records are preserved in the [historical evidence index](evidence/latest/README.md); they are not current failures. No application changes or real messages were made by this review.

## Corrected probe cases — independently verified

All ten previously failing cases below now PASS the unchanged independent probes and the fresh full regression. Stronger persisted ordering and outbox confirmations also pass. The table retains original failure descriptions for traceability; do not reimplement these fixes.

P0 means necessary for correctness of the supported pilot; P1 means required before routine unattended use or an explicit resolution of the supported behavior. Advanced workflows may remain unsupported if they retain obligations and fail clearly, as allowed by FIRST_DELIVERY_SCOPE.md.

| Case | Original priority | Historical failure; current checks PASS | Expected behavior to retain / traceability |
| --- | --- | --- | --- |
| SR-01 | P0 | Bid 0 / ask 0.20 can emit PROFIT_EXIT | Enforce the documented valid two-sided Tier R quote rule consistently. IT-005; AT-06/17 |
| SR-02 | P0 | Normalizer flags a contradictory call-symbol/put-row identity, but monitor emits put profit advice | Preserve source identity and reject contradictory evidence; never replace it with the expected position key to make it match. IT-004/005; AT-05/06 |
| SR-03 | P0 | Assignment-pending position yields exposure 0, NO_POSITIONS and no monitor message | Keep an explicit unresolved obligation and reconciliation reminders until resolved; avoid double-counting converted stock afterwards. IT-006; AT-07 |
| SR-06 | P0 | Withdrawal recorded before a same-second snapshot produces a false 10% HARD_HALT | Preserve event ordering for cash flows and snapshots. A date alone does not establish order. IT-008; AT-09 |
| SR-07 | P0 | Pending -> snapshot -> working in one second leaves reconciliation clean and delivery marker unchanged | Bind reconciliation and queued decisions to an ordered ledger revision rather than second-resolution timestamp equality. IT-001/003; AT-02/04 |
| SR-05 | P1 | Queued numerical profit/close advice still dispatches for a confirmed CLOSED position | Re-evaluate position-dependent advice at delivery; regenerate current unresolved risk rather than blindly replaying old numbers or silently dropping critical warnings. IT-003/011; AT-04/11 |
| SR-08 | P1 | A stock mark one year in the future passes reconciliation | Reject future marks at input and reconciliation using documented clock tolerance. IT-017; AT-13 |
| SR-09 | P1 | paper=False inside paper_primary produces no reconciliation error | Enforce the single-paper-account contract without building multi-account functionality. IT-002; AT-03 pilot slice |
| SR-10 | P1 | Yesterday morning's snapshot alone passes without the required later daily/EOD acknowledgment | Separate freshness, daily acknowledgment and post-event reconciliation; explicitly resolve the calendar/session convention. IT-001/012; AT-02/12 |
| SR-11 | P1 | CSP_ENV=real is accepted in the paper-only initial scope | Reject unsupported real bookkeeping mode or record an explicit owner scope change; never infer authorization for brokerage integration. IT-002; AT-03 pilot slice |

**Also still passing:** SR-04 rejects queued entries after regular-session close. SR-12 correctly evaluates indicative quotes fetched after monitor start. Preserve all 12 passing behaviors.

## Complete IT ticket disposition

The underlying ticket IDs and scope are unchanged. Supported-scope functional verification is complete for this snapshot; operational or deferred limits below remain distinct. Passing tests do not establish real deployment or guarantee message delivery.

| Ticket | Status | Remaining work or limit |
| --- | --- | --- |
| IT-001 cash semantics / reconciliation | VERIFIED supported pilot | Fresh regression and SR-07/10 pass |
| IT-002 account isolation | VERIFIED single paper account; multi-account DEFERRED | SR-09/11 pass; no multi-account feature required |
| IT-003 queued eligibility | VERIFIED supported scope | SR-04/05/07 and persisted OPEN/CLOSED outbox controls pass |
| IT-004 deliverable evidence | VERIFIED contradiction rejection; OPEN source-evidence limit | Owner attestation remains a separately labeled evidence limitation |
| IT-005 quote / exit policy | VERIFIED supported scope | SR-01/12 and quote/exit regressions pass |
| IT-006 assignment exposure | VERIFIED tested lifecycle | Pending visibility and conversion regressions pass |
| IT-007 manual state granularity | VERIFIED supported slices; advanced workflows DEFERRED | Unsupported reality must remain visible and entry-blocking; partial fills not required now |
| IT-008 flows / drawdown | VERIFIED supported scope | SR-06 and persisted same-second withdrawal/snapshot workflow pass |
| IT-009 Windows scheduling | Scripts/mocks tested; deployment NOT VERIFIED | Actual registration, wake and restart behavior not demonstrated |
| IT-010 last-trade print policy | VERIFIED strict supported policy | Missing/aged-print regressions pass; print recency is not bid/ask freshness |
| IT-011 dedup / recovery | VERIFIED tested retry/dedup/applicability | Real delivery guarantees and broader acknowledgment episodes not established |
| IT-012 manual CLI | VERIFIED supported workflows | Current unit/acceptance CLI regressions pass |
| IT-013 health | VERIFIED local status/dashboard; operational limits OPEN | Installed-job/deadline/stopped-PC behavior not demonstrated |
| IT-014 recipient / dry-run | VERIFIED mocked/local boundaries only | Actual owner-private Telegram receipt not verified |
| IT-015 upstream / license | Prior local checkout verified; runtime enforcement NOT VERIFIED | Reviewed SHA 34bf6cf8101e54620ea1e2bda940f0a3aa88e791; CC BY-NC 4.0 inspected previously. No live feed call; runtime pin/shadowing enforcement not established |
| IT-016 conflicting examples | Crosswalk delivered; minor docs follow-up OPEN | Original spec retained; WEB-F02 below |
| IT-017 backup / restore / import / time | VERIFIED tested boundaries; owner restore drill NOT VERIFIED | Fresh regressions and SR-08 pass; no real account restore performed |
| IT-018 pilot measures | DEFERRED | Not a prerequisite for initial pilot delivery |

The full original rationale and proposed acceptance conditions for all 18 tickets remain in [archived findings](archive/2026-10-30/INDEPENDENT_FINDINGS_2026-10-30.md). The [acceptance criteria](ACCEPTANCE_CRITERIA.md) retain every AT identifier, including future-release scenarios.

## Deployment checks still requiring evidence

- Actual receipt in the owner's authorized private Telegram chat; mocks do not prove it.
- Installed Windows tasks and observed wake/restart behavior; a status command's next-run estimate does not prove registration.
- Independently repeated clean installation and live-data integration if those capabilities are claimed at handoff.
- Explicit contract-deliverable evidence and supported policy decisions; owner attestation must remain labeled honestly.

Do not trade, connect a brokerage, disclose secrets or send real messages merely to close a documentation ticket. The reviewer continues to edit requirements only; the implementing agent owns application changes.

## Next handoff record

### Approved next product phase

[PRODUCT_WORKFLOW_REQUIREMENTS.md](PRODUCT_WORKFLOW_REQUIREMENTS.md) is the active implementation scope. The prior roadmap's deferral of all manual forms is superseded: actual-fill/check-in/close forms are required now. WEB-0 backend evidence and WEB-1 stored-view checks remain useful supporting results; they do not establish current ticker-price or complete owner workflow acceptance. History comparisons/charts and advanced corrections remain later. No 90-day wait is required.

### Minor implementation follow-ups

| ID | Priority | Reproduction and required correction |
| --- | --- | --- |
| WEB-F01 | P2 | Open /candidates with no scan/evaluation rows: “No scan recorded” appears alongside freshness “current.” Use unavailable/not evaluated when there is no evaluation. The independent fixture reproduces this; no fabricated candidates or gate bypass observed. |
| WEB-F02 | P2 | Root README.md and docs/DELIVERY_NOTE.md still list dashboards as not built, while RUNBOOK.md section 11 describes WEB-1. Align summaries with local read-only preview delivery; keep WEB-2/3 and operational acceptance pending. |

These two minor follow-ups are secondary to the P0 product pickup queue. The five pandas warnings are maintenance work. Complete the core web journey and its acceptance before calling the product ready; charts and remote hosting remain deferred.

### Verification record

For each corrected ticket, record: disposition, requirements interpretation, application revision or content manifest, changed files, exact reproduction/test command, expected and observed result, evidence path, remaining limits, implementer/date and independent verdict. Preserve original SR/IT/AT identifiers.

Run the [independent probes](evidence/latest/reproduce_checks.py) after relevant fixes and the affected regression tests. Changes to test oracles require explicit requirements rationale; do not alter expected outputs simply to make tests green. Update this file with observed evidence rather than creating a second current status tracker.
