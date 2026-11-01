# Acceptance criteria

## Current ticker-check defect acceptance

[ticker-check contract](PRODUCT_WORKFLOW_REQUIREMENTS.md#ticker-check-contract) defines TC-01..06 and TC-AT-01..07. October31 targeted rerun: nine supplemental assertions PASS; see the dated review for full regression and remaining web defects. Preserve all previously reported arithmetic/parity/read-only checks while correcting output safety, batch limits and verdict truthfulness. These supplemental checks do not replace WT/MD acceptance.

## Current web workflow acceptance — required for the usable pilot

Scope: [PRODUCT_WORKFLOW_REQUIREMENTS.md](PRODUCT_WORKFLOW_REQUIREMENTS.md), superseding earlier CLI-only/read-only release sequencing. Overall status: **NOT ACCEPTED**. The October31 review provides partial synthetic browser/domain evidence for this journey; [current report](evidence/2026-10-31-current-verification/REPORT.md) maps its limits and two confirmed market-data failures. No complete WT case is automatically closed by the suite count. Earlier suite results are supporting regression evidence only. Real sends require separate explicit owner enablement; deterministic alert tests use fake transports.

| Case | Required scenario and independent evidence |
| --- | --- |
| WT-AT-01 | In the browser add an owner-selected ticker, load stock price and put rows, filter expiry/strike and observe a subsequent automatic update. Verify exact provider/normalized/browser values and observation/retrieval times. Complete MD-AT-01..06, including an actual public-data run; clearly distinguish delay/unknown timestamps from live. |
| WT-AT-02 | Track a ticker without entry approval, then with entries blocked by reconciliation/capacity/events. Stock/put data remains visible; approval and failed gates are explicit; no eligible entry send occurs. Watchlist removal or approval expiry does not stop monitoring an active holding. |
| WT-AT-03 | Select a put, enter an actual paper fill and fees/time, preview collateral/portfolio changes, confirm once and verify OPEN in journal and monitor. Reload/restart retains it. Duplicate or stale submission creates no duplicate trade. No broker/order call or assumed fill at current bid. |
| WT-AT-04 | Record an unfilled order separately as WORKING; confirm fill; record CLOSE_WORKING then actual close. Reservations persist until confirmation. Closed-position queued advice is suppressed while a valid OPEN control still alerts. Unsupported/over-limit reported reality stays visibly recorded or unresolved, never omitted. |
| WT-AT-05 | Use controlled option quotes across provisional active thresholds: assert actual entry credit/current ask/multiplier/fees produce the expected indicative P&L and alerts. Inadequate quote evidence suppresses numeric advice and exposes data warnings; time/assignment reminders continue. Check exact contract identity. |
| WT-AT-06 | Hand-calculate pending/open/close-working put collateral plus stock and unresolved assignment obligations. Verify all ticker, sector, correlation, expiry-week, company-name and contract counts separately from lifetime trade count. Missing labels cannot silently pass concentration checks. |
| WT-AT-07 | Exercise all existing limits at boundary and just over, including $15,000 pilot collateral, $9,000 ticker and $10,000 sector, four contracts/one per underlying, two company names and $15,000 expiry-week collateral. Assert binding reasons/post-trade capacity and actual configured rules; all gates apply together. |
| WT-AT-08 | Generate candidate then record another trade or change relevant account/policy state before dispatch. Revalidation suppresses newly ineligible entry advice. Exhausted entry capacity never suppresses existing-position risk alerts. Mitigation text states the breached bucket and capacity without inventing a safe alternative or placing orders. |
| WT-AT-09 | Exercise authenticated browser preview/confirm/check-in/close with invalid input, cross-origin mutation, duplicate submission and stale record. No accidental ledger writes on GET; coherent state, audit trail and reconciled capacity. Jobs continue with browser closed; restart/backup preserves positions and alert state. Verify actual installed-job operation separately from policy schedule estimates. |
| WT-AT-10 | Owner completes the full one-ticker web journey without routine CLI use. With explicit owner enablement, verify private Telegram receipt, then confirmed closure. Capture source identity, commands, sanitized provider/browser evidence and observed outcomes. A reviewer mock or agent claim cannot substitute for owner receipt or live-feed evidence. |

Deciding exit thresholds later does not delay implementing configurable monitoring or authorize changing existing provisional policy. The owner selected free Yahoo through Stockpile for live updates with indicative/unknown-quote-age labels. Feed selection is resolved; actual automatic updates and honest timestamps still require verification. Final exit thresholds remain an owner decision. Retain backend AT-01..18 below and MD/WD identifiers; use the active backlog for current results.


Current test design for the agreed first-delivery scope. Read [first-delivery scope](PRODUCT_WORKFLOW_REQUIREMENTS.md#first-delivery-scope-and-feedback) for applicable pilot slices, [OPEN_ISSUES.md](OPEN_ISSUES.md) for current verdicts, and the [latest delivery report](evidence/latest/RETEST_DELIVERY_REPORT_2026-10-30.md) for execution evidence. Passing suite counts do not imply all scenarios below pass. Original AT and IT identifiers are retained.

The complete scenarios are retained for traceability; full multi-account behavior, advanced alert episodes, pilot analytics and other deferred features are not initial-delivery gates. Test only the corresponding supported slice before the first release.

Use synthetic data, frozen clocks, mocked services and temporary stores. Never trade, connect a brokerage, or use real secrets in tests. For unresolved policy choices, record an explicit expected result rather than deriving it from existing code.

## Fixture conventions

- Synthetic standard put: ticker TESTA, verified standard 100-share deliverable, strike $75, spot $80, DTE 40, delta -0.13, bid/ask $0.50/$0.54, OI 1,000, volume 100. Quotes/events/approval provenance are explicitly synthetic. Configure all required manual approvals and cleared events.
- Default pilot enabled; $100,000 reconciled equity baseline; genuinely free cash $70,000 unless changed by a case. No initial holdings unless stated. Separate paper_a/paper_b synthetic scopes.
- Freeze an open regular-session time outside the final-20-minute exclusion window. Tier R quote timestamp is null. Do not reuse the spec's example PASS $16,000 collateral under default pilot.
- Where timestamped behavior is tested, use a synthetic independently timestamped Tier V fixture; never promote Yahoo retrieval time to quote time.

| ID / finding | Given / action | Observable expected result |
| --- | --- | --- |
| AT-01 / IT-016 | Compare archived REQUIREMENTS.md with CSP-TELEGRAM-001 and implementer docs | Written crosswalk retires entry TBD/default overrides, YAML-only canonical store and inbound Telegram position commands; preserves applicable Windows requirement. No undocumented precedence choice |
| AT-02 / IT-001 | Snapshot explicitly records $20,000 genuinely free after $8,500 reserved; evaluate TESTA, then record a new commitment without reconciliation | Before change, cash gate uses $20,000 free, without deducting $8,500 again; portfolio/pilot gates independently apply. After change, entry blocked until fresh complete reconciliation. Confirm cancellation never prematurely releases cash |
| AT-03 / IT-002 | In paper_a record stock and a $100,000 peak; paper_b has the identical contract but different equity/cash | Scope-specific exposure/peak/halt/notifications remain independent. Cross-scope stock-assignment link and inconsistent paper/scope input reject |
| AT-04 / IT-003 | Queue passing entry, receive 429, then independently test ledger change, policy revision, approval revocation, expired TTL and market closure; retry/restart | No obsolete entry is sent; status/reason is inspectable. Fresh eligible recomputation may create a new decision under documented rules. Urgent/time reminders are evaluated separately |
| AT-05 / IT-004 | Supply unknown deliverable; adjusted deliverable with premium multiplier 100; accepted standard-contract evidence | First two block with stable identity/data reason; only evidenced standard contract can proceed through other gates. Unknown metadata never defaults to standard |
| AT-06 / IT-005,010 | Fresh fetch with null quote timestamp and old last trade; missing print; exactly 1.5 days old; slightly older; future timestamp | Versioned policy determines missing/aged-print behavior consistently. Quote age remains UNKNOWN. Numerical exit follows approved Tier R policy; no LIVE/current-verified label arises from retrieval time. Invalid future evidence rejects |
| AT-07 / IT-006 | OPEN becomes assignment pending; inject crash during option retirement/new-lot/cash update; then complete reconciliation | Entries frozen and obligation visible while pending; failed transaction leaves coherent prior state; completed transition records shares/cash/fees once. ADJUSTMENT_REVIEW remains visible and monitored |
| AT-08 / IT-007 | Cancel close-working order, report early assignment during close-working, report external two-contract holding and partial close, import duplicate fill | Remaining obligations never vanish. Unsupported reality freezes entries with explicit review state rather than silent omission. Reimport has zero duplicate economic effect. Corrected records preserve audit history |
| AT-09 / IT-008 | Peak $100,000 then $96,900/$94,900/$91,900; restart/rebound; independently add withdrawal/deposit | 3% pause, 5% escalation and 8% persistent halt. Flow treatment matches recorded policy choice; deposit/restart cannot silently clear halt. Manual reset requires auditable evidence and allowed scope |
| AT-10 / IT-009 | Simulate 08:30 CT regular open, early close, holidays, DST, weekends, Windows restart and wake after missed jobs | Intraday monitor covers opening half-hour. No entries outside valid session/buffer. Independent daily calendar checks run on closed days. Wake triggers safe monitoring/reconciliation, not stale opportunity replay. No Linux-only runtime requirement for Windows deployment |
| AT-11 / IT-011 | Repeat same risk trigger; acknowledge; resolve/reactivate intraday; escalate drawdown; crash before POST and after POST before commit | Documented event/episode policy controls repeats; escalation never suppressed. Sender leases recover. Unknown ACK remains explicitly ambiguous; stable alert ID makes possible duplicates recognizable. No claim of exactly-once delivery |
| AT-12 / IT-012 | Follow documented setup, approval, pending/working/open/canceled/closed/assigned, reconciliation and reset workflows; exercise all send-mode combinations | Each workflow has usable exact syntax, validation, actor/reason and audit evidence. Preview does not mutate canonical state. No Telegram send without documented explicit send authorization; conflicting flags cannot enable sends |
| AT-13 / IT-017 | Import duplicate/conflicting/future-dated/nonfinite input; back up active WAL DB; restore with pending/SENT outbox and hard halt | Invalid input leaves canonical state unchanged or explicitly quarantined. Backup is coherent. Restore preserves holdings and halt, requires appropriate reconciliation and does not blindly send historical entries. Safe CSV export handles formula-like notes |
| AT-14 / IT-013 | Of 10 tickers, 3 fail; then all fail; then Telegram fails; separately stop scheduler | Partial result names coverage/gaps; total failure gives data issue rather than healthy no-trade. Local health exposes delivery failure and overdue jobs by documented deadlines. Does not promise a stopped process can notify of its own death |
| AT-15 / IT-014 | Configure group/channel/unapproved recipient; run dry-run with populated outbox; trigger HTTP exceptions containing token URL; use hostile HTML | Disallowed recipient fails; no Telegram request and no future-send queue created by dry-run. Production sender cannot consume fixture messages. Text safely escaped and secrets/IDs/private account details redacted |
| AT-16 / IT-015 | Install exact upstream baseline in clean isolated environment; block broker network paths and instrument import/initialization | Record verified SHA/license/dependencies/signatures/available fields. No main substitution, broker credential lookup, SDK initialization or broker egress. Adapter tests fail truthfully on missing required evidence |
| AT-17 / IT-010,016 | Use exact boundary matrix below; run fixture preview with pilot enabled/disabled | Documented numeric decisions, active profile, binding cap and failed gates agree. Pilot $16,000 collateral never shown PASS. All-zero eligibility yields useful reasons without threshold relaxation |
| AT-18 / IT-018 | Generate report from synthetic successes, failures, declined signals, assignments, fees, external flows and missing observations over 90 days | Includes coverage, missed/late/duplicate alerts, reconciliation completion, denominator and data gaps; missing outcome is UNKNOWN. Separates simulator and conservative results. Pilot caps persist until explicit reviewed transition |

## Boundary matrix for AT-17

All other gates must pass; use exact Decimal arithmetic. Test below / equal / above each bound independently.

- Put collateral pilot $15,000, normal exposure $25,000, per ticker $9,000, sector/correlated bucket $10,000, expiry ISO-week $15,000: equality allowed, overage blocked. $30,000 is incident detection, never new-entry headroom.
- Four active contracts maximum; one per underlying; two distinct company names maximum including owned shares. Exercise each reserved state and ETF bucket overlap.
- Delta -0.10/-0.15 pass; -0.099/-0.151/+0.13/nonfinite fail. DTE 35/45 pass; 34/46 fail. Strike must be strictly below spot and no higher than ownership ceiling.
- OI 500, volume 50, bid $0.20 pass at equality. Both spread gates apply: $0.05 absolute and 10% of actual bid/ask midpoint. Upstream fallback last-derived midpoint must not influence gating.
- Profit capture credit $0.50 / ask $0.20 triggers exactly 60%; ask $0.21 does not. Ask $1.00 triggers exactly 2x loss review; abs delta 0.30 triggers. Unknown/nonpositive opening credit cannot divide or generate a fabricated percentage.
- Snapshot age 30 hours and stock-mark age 30 hours: choose/document exact equality convention; reject just older and future-as-of bypasses. Include daily EOD acknowledgment and post-trade reconciliation requirements independently of age.
- Approval expiry and seven-calendar-day event review: verify exchange-local date boundaries and clearance through expiration; failed provider check plus manual independent evidence follows explicit precedence without silently overriding a known adverse event.
- No-entry final-minute cutoff at 20 minutes: resolve exact boundary from spec wording (less than 20 blocked); test 19:59/20:00/20:01 remaining.

## Execution record required for each case

Case ID; finding ID; application SHA; dependency lock/baseline SHA; policy hash; approved oracle; fixture paths; UTC clock/session; exact command; expected output; actual output; network-call count/destinations where relevant; artifacts/log excerpts with secrets redacted; verdict PASS/FAIL/BLOCKED; reviewer/date.

Documentation-only scenarios may use reviewed document evidence. Runtime/security/transport assertions require execution evidence. Real private Telegram delivery remains a separate explicit owner-operated acceptance step; mocked success alone cannot prove it.

## Market-data acceptance (MD)


- MD-AT-01: Controlled provider observations A then B propagate through backend persistence to the actual browser without a new entry candidate. Assert exact ticker/value/source/timestamps, not merely HTTP 200.
- MD-AT-02: Repeat with entries blocked, outside entry scan hours during the applicable market session, and an active holding not eligible for new entries. Monitoring data remains visible; entry gates remain unchanged.
- MD-AT-03: Provider outage, stale data, absent market timestamp, rate limit and market closure show distinct truthful states. Refresh cannot reset quote age. No fixture fallback.
- MD-AT-04: Monitor produces a newer observation than the last entry scan; the dashboard selects the newer applicable evidence and preserves contract identity. A stock price cannot substitute for an option ask.
- MD-AT-05: Perform a read-only real-provider smoke test for an owner-approved ticker during a relevant market session. Capture redacted raw response or response hash, provider/market/retrieval times, normalized record and actual browser value. Compare with an independently timestamped public reference, documenting differences and delay rather than demanding false exact simultaneity. No Telegram or broker access is needed. If unavailable, report this acceptance case BLOCKED, never PASS based on mocks.
- MD-AT-06: Verify polling limits, recovery and no duplicate job execution. Market updates do not change account records, positions, reconciliation, risk latches or send messages.


## Repeated-update regression requirements

The latest recheck adds concrete failure-order cases under the same existing data-quality requirements: complete provider exceptions and empty chains must suppress current numeric position advice; an older delayed write cannot overwrite newer latest evidence or clear its failure marker; a truly later invalid update within one stored second must invalidate prior advice. Preserve exact-contract scope, original timestamps, independent calendar/assignment warnings, valid unaffected-contract controls and normal recovery. See RV-20261031-03/04 in OPEN_ISSUES.md and the dated recheck evidence. These are missing regression cases, not authorization for a new feed or altered financial limits.

For MD-AT-03/04 and WT-AT-01/05, include valid → contradictory/invalid → recovered observations. Verify stock-level and exact-contract failure state, suppression of false all-gates-pass/numeric advice while invalid, original retained timestamps, valid unaffected-contract controls and successful recovery. See the active RV findings in OPEN_ISSUES.md.

## ticker-check acceptance (TC)


TC-AT-01 / TC-01: symbols containing newline, carriage return, ESC/ANSI and representative other control characters yield a clear safe validation error, no raw controls/forged verdict lines and zero provider calls. Include ordinary valid symbols supported by the adapter as controls.

TC-AT-02 / TC-02: simulated provider exceptions containing multiline forged verdicts, ANSI cursor controls and long text produce safe bounded diagnostics with truthful failure status. Test both stdout and stderr and structured output if it exists.

TC-AT-03 / TC-03: test zero/one/exact-limit/over-limit symbols, duplicates/case normalization and 200 input symbols. Verify documented normalization/limit semantics, zero fetches for rejected batches, bounded retries and rejected mutation flags. A limit must not truncate silently.

TC-AT-04 / TC-04: compare consistent-spot control with contradictory/missing/invalid spot evidence. A global unavailable-price state cannot coexist with an unqualified positive candidate verdict based on that same evidence. Assert semantic outcome as well as text; preserve matching scan gate behavior where inputs are valid.

TC-AT-05 / TC-05: all checked technical gates pass while earnings/event clearance is unknown. The output explicitly lists that missing prerequisite and does not claim full entry eligibility, ownership approval, available portfolio capacity or live quote verification. Check any actual supported output modes.

TC-AT-06 / TC-06: successful empty chain, window with no expirations, unknown symbol and actual provider exception produce appropriate distinct or explicitly uncertain outcomes. No empty result alone proves an outage.

TC-AT-07 / preserved regression: rerun the reported boundary matrix and scan parity cases against identical policy/time/inputs. Confirm read-only behavior on absent and existing journals, files/rows unchanged, no migrations/outbox records, and zero send calls. `--send`, `--approve` and comparable mutation flags stay rejected. A zero exit from diagnostic execution, if used, must be documented separately from candidate eligibility.

## Handoff and independent sign-off

Implementer supplies source revision or manifest, changed files, defined batch limits, exact targeted/full-suite commands, test paths, raw outputs and expected/observed results for each TC item. Rerun all eight originally failing cases without weakening their oracles, plus affected boundary/parity/read-only regression. Reconcile new tests/counts explicitly rather than substituting an old suite total. Reviewer must independently rerun focused checks before marking any TC item verified. Synthetic tests establish deterministic behavior; actual provider-to-browser market-data acceptance remains separately pending under MD/WT requirements.

## Retained dashboard acceptance (WD)

The old WEB phase labels in these historical test IDs describe logical capabilities, not permission to defer the WT core journey. WT-AT-10 now requires the complete owner journey.

| Test ID | Required scenario and expected result |
| --- | --- |
| WD-AT-01 | Rerun current backend regressions and all 12 unchanged independent probes, including persisted same-second withdrawal/closed-position outbox confirmations. Record the exact current revision/manifest and outcomes |
| WD-AT-02 | On a synthetic reconciled book, every overview/position number matches the existing backend output and active policy; reserved/review states remain visible |
| WD-AT-03 | Empty whitelist, stale/unreconciled snapshot, no opportunities, partial data and provider outage produce distinct visible states; no invented candidates or prices |
| WD-AT-04 | Untimestamped/stale data never becomes verified/live because the page refreshes. Missing P&L is unavailable, not zero. Evidence time and display refresh time are distinct |
| WD-AT-05 | Browse/refresh all WEB-1 views and verify zero product-state writes, scans, migrations or Telegram sends. Unauthenticated access is denied; host/origin boundaries and escaping of untrusted notes are tested |
| WD-AT-06 | Scan/monitor writing concurrently with a dashboard read yields a coherent versioned response. Large histories paginate; changing pages does not lose or alter records |
| WD-AT-07 | Closing/restarting only the browser/dashboard leaves independent scheduled jobs intact. Policy-derived next-run estimates are labeled separately from verified task registration |
| WD-AT-08 | Migrate an existing synthetic journal, restore its backup and confirm positions, audit links, halt/reconciliation state and outbox history survive without replaying old alerts |
| WD-AT-09 | WEB-2 check-in and manual lifecycle forms give the same domain outcomes as CLI; preview/cancel does not mutate, stale preview rejects and duplicate submit has no second financial effect |
| WD-AT-10 | An unsupported manual state remains visible and blocks entry; web forms cannot bypass caps, review requirements, policy versioning or the no-broker boundary |
| WD-AT-11 | WEB-3 comparisons distinguish genuine eligibility changes, changed policy and missing/failed observations; displayed deltas trace to stored records |
| WD-AT-12 | An owner walkthrough completes WEB-1 inspection first, then WEB-2 check-in/recorded paper lifecycle when that stage is delivered. Collect friction/confusion feedback before adding more features |

