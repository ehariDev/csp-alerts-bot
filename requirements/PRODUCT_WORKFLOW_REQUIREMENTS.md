# Working web put tracker — current product requirements

Updated October 30, 2026 from the owner's clarified workflow. Status: required, not delivered or accepted. This is the active product scope for the implementing agent. The reviewer writes requirements and independently verifies; application implementation belongs to the software agent.

## Outcome and precedence

The owner chooses tickers, sees automatically updated stock and put-contract prices, records a manually executed paper trade in the web page, and receives position alerts while seeing portfolio concentration and remaining capacity. A viewer of historical records alone does not meet this outcome.

This clarification supersedes the earlier CLI-only first-delivery sequence and the deferral of all web forms until after a read-only release. Existing specification financial limits, data-evidence rules, one-paper-account scope, manual execution, authentication and no-brokerage boundary remain. The original specification is preserved; it is not permission to omit the web workflow now explicitly requested. If implementation policy differs from the limits below, report the discrepancy; do not silently change either.

## WT-01 — choose what to track

Provide a web watchlist: add/remove an owner-selected ticker and see tracking status and fetch errors. Validate symbols and persist the list across restart. No preselected investments. Tracking a ticker for prices is distinct from approving it for new-entry recommendations: a tracked but unapproved ticker may show market data with an explicit approval warning, but cannot produce an eligible entry alert. Existing holdings remain monitored if removed from the watchlist or their entry approval expires. Show the reason a symbol is tracked (watchlist, active holding, or both).

## WT-02 — stock and put market data

For every tracked ticker, show the latest retrieved stock price and put contracts with exact contract identifier, strike, expiration, bid, ask, available volume/open interest and data provenance. Allow expiry/strike filtering and selection of a contract to record a trade. Missing fields remain unavailable. Show prices even when no contract passes entry gates; distinguish the browsable chain from eligible research candidates. Never filter away all price visibility because portfolio capacity or event clearance blocks new entries.

Automatically refresh from the backend, without needing an eligible entry scan or manual account check-in. The latest relevant monitor observations must be persisted and available to the page. Follow [market-data contract](PRODUCT_WORKFLOW_REQUIREMENTS.md#market-data-contract), including independent stock/option timestamps, provider, delay classification, last attempt/success, outage states, bounded polling and real-provider-to-browser evidence. Freshly retrieved does not mean exchange-fresh. The owner selected free Yahoo through Stockpile. Provide live updates with the visible qualifier “Yahoo indicative data · Quote age unknown,” not LIVE_VERIFIED or guaranteed exchange real-time data. Feed selection is resolved and does not block delivery; automatic update and provider-to-browser verification are still required. No paid feed subscription is authorized.

## WT-03 — “I placed this trade” through the web

From a put row, offer Record sold put. This records an external manual execution; it does not submit an order. Show the exact ticker/right/strike/expiry/multiplier, account and paper scope, quantity, execution date/time, actual credit per share and fees. The owner enters/confirms the actual fill: never use the current bid as proof of execution. Preview the resulting record and portfolio impact, then explicitly confirm. Preserve audit history and reject duplicate/stale submissions transactionally. Reuse backend validation and Decimal math.

A correctly recorded filled trade becomes OPEN and immediately enters monitoring without a second CLI registration. An order merely placed but not filled is separately WORKING and reserves collateral; ask for actual fill confirmation before treating it as OPEN. Support recording a confirmed buy-to-close fill and canceling an unfilled order through the web. CLOSE_WORKING remains monitored and reserved until confirmed closed. Show unresolved assignment/review obligations and manual next steps; advanced assignment/partial-fill automation can remain deferred. Unsupported fills must retain an explicit unresolved obligation and block new entries rather than disappear.

An owner-reported execution that already breaches a policy must not be silently rejected out of existence. Record it through the existing explicit exception/review path or retain a visible unresolved exposure pending review; flag the breach and block further eligible entries. Recording reality is different from recommending a new trade. A recorded trade requires the existing account reconciliation acknowledgment before new capacity-verified entries; quotes must continue updating meanwhile. Provide the minimal web account snapshot/check-in confirmation needed for this journey, using existing CLI semantics; do not require routine CLI commands after setup.

## WT-04 — track exact positions and alert

Show every active position with actual entry credit, current option bid/ask, observation times, DTE, reserved collateral, indicative buy-to-close P&L and active warnings. Use the option ask for conservative estimated closing cost when evidence qualifies; never substitute stock price or option last print. Inadequate evidence means unavailable P&L and a data warning, not zero risk. Preserve time/assignment reminders during price outages.

Send position-management alerts to the explicitly enabled private Telegram destination and show their generation/delivery state on the page. Recorded closure stops obsolete open-position alerts, including already queued advice after revalidation. No automatic trade execution. Use clear terms: sell to open creates the short put; buy to close exits it. Existing thresholds are provisional policy values, not newly approved optimization: the owner will decide profit/loss/delta thresholds later. Expose the active values and policy version; preserve existing risk/data warnings and do not silently retune thresholds or disable monitoring while awaiting that decision. Production sends require explicit owner enablement; reviewer tests use fake transports unless separately authorized.

## WT-05 — portfolio labels and mitigation

Display ticker, sector, asset type and correlation bucket on holdings and in portfolio totals. Sector/correlation labels are required risk inputs, not decoration. Owner-confirmed labels override an unverified provider suggestion through an audited correction; unknown labels remain unknown and block any candidate whose concentration cannot be assessed. Do not treat ETFs or unknown sectors as automatically diversified.

Show open positions, active contracts including working reservations, company-name count, ticker/sector/correlated exposure, expiry-week collateral, cash and binding limits. Distinguish position count from contract count and cumulative historical trade count. Include owned equity and unresolved assignments according to the backend exposure rules; a closing order does not release capacity.

For each candidate, display current exposure, proposed addition, post-trade exposure, remaining capacity and each failing rule. Suppress eligible new-entry notifications that breach any existing hard gate; keep the ticker's prices and blocked explanation visible. Revalidate queued candidates after every relevant trade/account/policy change. Existing-position risk alerts must continue when entry capacity is exhausted. Track-only observations may remain visible but must never masquerade as an eligible trade alert.

Mitigation guidance starts with rule-based explanations: no additional exposure to the breached ticker/sector, remaining capacity by existing bucket, expiry clustering, and which independent limit binds. Candidates from other owner-approved sectors can be shown only if all gates independently pass; do not invent a diversification recommendation or promise that sector labels prevent losses. Do not automatically close, hedge or move capital. Scenario engines and personalized AI strategy recommendations are deferred.

### Existing budget baseline — no policy change authorized

| Rule | Preserved starting limit |
| --- | --- |
| Reference account | $100,000; actual confirmed equity governs drawdown |
| Default pilot put collateral | $15,000 |
| Normal aggregate exposure ceiling | $25,000; not permission to leave PILOT |
| Emergency exposure | $30,000 incident threshold, never entry headroom |
| Combined exposure per ticker | $9,000 |
| Combined exposure per sector | $10,000 |
| Active short-put contracts | 4 total; one per underlying |
| Individual-company names | 2, counting applicable puts and stock holdings |
| Expiration ISO-week short-put collateral | $15,000 |
| Cash | Fully secured by actual free unencumbered cash; no margin |

Apply all limits simultaneously, plus existing event, ownership, liquidity, drawdown and reconciliation gates. Put collateral is strike × verified multiplier × contracts; premium is not the capital-at-risk denominator. Preserve separate put-collateral and combined stock/put exposure totals. These are exposure budgets, not guaranteed loss ceilings.

## Delivery sequence and definition of done

1. **Vertical slice:** one owner-selected ticker → actual provider stock/put data → automatic browser update → record one paper fill in the web page → track the exact contract → controlled position alert → record close → obsolete alert suppressed. Include minimal portfolio labels/capacity and reconciliation needed for this loop. Synthetic demonstration alone is not completion.
2. **Usable pilot:** expand to the small owner-selected list; verify all concentration gates, expiry filters, durable restart, outage/recovery, deduplication, close/order states, backup and private Telegram enablement. Owner walkthrough is required.
3. **Later:** history comparisons/charts, advanced forms and lifecycle corrections, performance analytics, AI, multi-account and remote hosting. Do not wait 90 days to deliver the usable pilot.

Local authenticated Windows web deployment first. Backend jobs operate independently of browser lifetime. Document polling/scheduling, startup and failure behavior. Do not require cloud hosting or public exposure. Preserve existing ledger/history and backup before schema migration.

## Decisions and effort

Feed choice is resolved: free Yahoo through Stockpile, live-updating with indicative/unknown-quote-age labels. Show actual limitations, cadence and access failures; do not purchase a feed or promise verified exchange freshness. Entry limits remain enforced; deciding exit thresholds later is not approval to change them now. Alert channel remains the existing private Telegram channel unless the owner changes it.

Preliminary planning estimate only: 3–5 engineering days market data/page, 2–3 days web recording, 2–4 days monitoring/concentration, 2–3 days independent verification/fixes; 9–15 working days total for one engineer, subject to feed access and actual reuse quality. This is not a deadline or a reason to skip acceptance. Implementer must reassess after demonstrating the first complete slice, with concrete blockers and evidence.

See [acceptance criteria](ACCEPTANCE_CRITERIA.md) for WT-AT cases and [current backlog](OPEN_ISSUES.md) for status. No new implementation or verification is claimed by authoring this document.

## Market-data contract

Consolidated October31. Owner choice remains free Yahoo through Stockpile. Show **Live updates · Yahoo indicative data · Quote age unknown** with retrieval time and authoritative market time when available. Live updates means ongoing retrieval, not guaranteed real-time exchange quotes. No paid subscription or additional feed-selection decision is required. Document actual polling cadence and provider limitations. MD-AT checks are in the single acceptance document.


For an approved ticker or active-position ticker, the owner can see the latest successfully retrieved underlying stock price, its provider, price type (last/bid/ask/previous close), authoritative market timestamp when available, retrieval time, age and market-session status. Stock price and option-contract bid/ask are separate observations. Unknown market timestamps remain unknown: retrieval time never establishes exchange quote freshness. Yahoo remains indicative under the authoritative specification; no claim of guaranteed real-time data or paid provider commitment is authorized here.

1. Establish one backend market-data refresh path independent of whether entry eligibility, reconciliation, approval expiry, or the entry scan window permits a candidate. Data retrieval must not relax any decision gate. An active holding must remain observable even when entries are blocked.
2. Persist the latest relevant monitor observations with ticker/contract identity, source and timestamp provenance. Make them available to the dashboard rather than selecting only observations from entry scans. Preserve observation evidence used by historical decisions.
3. Implement a bounded, documented polling cadence and visible last-attempt, last-success and next-update status. The agent must state the chosen cadence and provider limitations; do not promise streaming. Page reads stay free of financial-ledger writes and Telegram sends. If an explicit update action is provided, use a separate authenticated, bounded operation; it may update only market-data/run records, never fills, account snapshots or risk settings.
4. Show the latest price and its recency directly on ticker/position views. Show delayed, untimestamped, stale, market-closed and unavailable states distinctly. A page reload must not make old evidence current. On outage, retained prices may be displayed with their original timestamp and stale status; numeric advice must retain existing evidence gates.
5. Do not require owner-entered stock marks to masquerade as fetched prices. Keep recorded accounting marks, observed market prices and confirmed account equity distinct. Reuse the existing backend decision calculations.
6. Make this part of the first complete web journey, alongside its minimal forms; charts and remote hosting remain later. Do not add brokerage connectivity, automatic trades or a new paid service without an explicit owner decision.


Latest-attempt validity is specific to the stock and each exact contract. Retaining an old good value must not hide a newer contradictory/invalid observation or keep affected candidates/numeric position advice eligible. Retained evidence keeps its original timestamp and failure state; valid other contracts and later recovery must remain usable. RV-20261031-01/02 are unresolved in OPEN_ISSUES.md.

## ticker-check contract

This read-only CLI diagnostic supports investigation; it is not a replacement for the web journey. Preserve technical-gate arithmetic/parity, no journal creation or changes, and no send/approve/mutation flags. A component cap pass does not mean full portfolio eligibility. Existing standard-contract identity checks remain in force.

| ID | Defect | Required behavior |
| --- | --- | --- |
| TC-01 | Raw ticker text can inject terminal controls or forged lines | Validate symbols before any provider call. Reject control characters including CR/LF and escape sequences. Any displayed rejected input must be safely escaped so it cannot move the cursor, change styling, create a new verdict line or hide output. Apply to stdout and stderr. |
| TC-02 | Provider/exception text can inject controls or forged verdicts | Sanitize/escape all untrusted error text through the same output boundary; bound displayed length and retain useful error classification. No raw terminal controls or forged structural lines in text output. Structured output, if supported, must escape correctly. Never leak tokens or credentials. |
| TC-03 | Unbounded ticker input causes unbounded sequential Yahoo requests | Enforce a finite documented maximum per invocation before network activity. Normalize and deduplicate symbols first; reject excessive input clearly without fetching a prefix. Set and document a conservative default maximum, bounded retries/timeouts and any override's hard ceiling. Tests must assert zero requests for rejected batches and a bounded request count for accepted batches. A 200-ticker invocation cannot silently issue 200 chain requests. |
| TC-04 | Contradictory underlying prices show UNAVAILABLE but a passing verdict | Validate underlying-price evidence before issuing any passing gate summary. If disagreement makes spot unusable, mark affected evaluation DATA_UNAVAILABLE/inconsistent and suppress its positive verdict; preserve diagnostics. Do not select an arbitrary row, average conflicting values, or infer a fresh spot. Any numerical consistency tolerance must be explicit and tested. A later successful independent stock fetch must not silently repair historical option-row evidence without documented evaluation rules. |
| TC-05 | Passing verdict omits unverified earnings | State precisely which technical/data/cap checks passed and explicitly list unverified earnings/event clearance, ownership/approval and portfolio/cash/reconciliation prerequisites where applicable. A bare WOULD PASS GATES cannot suggest a fully eligible trade or verified live quote. Suggested wording: PASSES CHECKED TECHNICAL GATES — earnings/event clearance unverified; not an entry authorization. Use the actual checked/unchecked scope rather than a fixed disclaimer that contradicts checks performed. |
| TC-06 | Empty chain described as provider outage | Distinguish a successful empty response/no contracts in the requested expiry window from a provider failure, unknown symbol and malformed/unusable response. State the requested window and what is known. Do not infer an invalid ticker, no opportunities, no earnings or outage solely from an empty chain. If the provider cannot distinguish causes, state that limitation rather than inventing certainty. |


These corrections do not loosen financial or data gates. TC acceptance and source/evidence requirements are consolidated in ACCEPTANCE_CRITERIA.md; current results live only in OPEN_ISSUES.md.

## Retained dashboard design requirements and future backlog

WD identifiers remain traceable below. Earlier WEB-1/read-only then WEB-2/forms sequencing is superseded by the complete WT first-delivery journey; it cannot postpone basic forms or automatic market updates. The retained design requirements apply without recreating separate release gates.

**WD-001: Current verification.** Rerun applicable current regressions and independent probes/workflows; retain source identity, policy/schema, commands and actual outcomes. Historical counts never establish current acceptance. Use OPEN_ISSUES.md for status.

**WD-002: Version identity.** Establish Git history if the project is not already covered by a repository. First check for an enclosing repository; do not create a second nested repository accidentally. Exclude .env, account records, databases, logs and private notes before committing. Record code revision, policy hash and schema version with runs/releases where available. Until version identity is available, retain a source content manifest. This does not require publishing or pushing any repository.

**WD-003: Persistent history.** Reuse existing SQLite snapshots, audit events, entry evaluations, observations, run records and outbox IDs. For each completed or failed scan/monitor, retain the timestamp, scope, run ID, policy identity, relevant ledger/reconciliation revision, coverage, outcome and structured reasons. Preserve manual position/cash/approval changes and alert delivery outcomes through their existing audit paths. Record explicit unavailable/failed results, not fabricated zeros.

**WD-004: Preserve provenance and useful history.** Retain the full 90-day pilot's account snapshots, decision summaries, run outcomes and audit history by default. Reuse existing records rather than collecting duplicate full chains for the dashboard. Do not add automatic deletion of canonical manual/audit records in this phase. If raw quote payload growth needs a retention policy, separate it from summary/audit retention and document it explicitly. No historical prices or earlier decisions may be invented to fill missing history.

**WD-005: Safe schema evolution.** Any additional history fields require a versioned migration and a consistent SQLite backup before upgrade. Existing ledgers and CLI commands remain usable. Test upgrade of an existing synthetic database, stable record IDs, backup/restore and no replay of old advisory messages. Do not reset drawdown, reconciliation or delivery state as a migration shortcut.

**WD-010: Overview.** Show paper-account identity, active profile, latest confirmed equity, recorded drawdown peak/current/latched level, net free cash, put collateral, stock reference exposure and aggregate exposure. Show binding limits, reconciliation state and snapshot time beside values. Pending/working/close-working and unresolved assignment/adjustment obligations must remain visible according to the verified backend semantics. Label exposure as an exposure budget, not maximum possible loss.

**WD-011: Candidates and reasons.** Show the last evaluated exact contracts, decision time, policy, bid/ask, model delta, DTE, liquidity fields, collateral, failed gates and coverage. Separate passing research alternatives, rejected evaluations, stale decisions and symbols not evaluated because data failed. Show ownership/event-clearance context and data tier. An empty whitelist, no qualifying contracts, blocked ledger, partial coverage and provider outage must have distinct explanations. Never relax criteria to populate the page.

**WD-012: Positions.** Show active/reserved/review states with exact contract, recorded fills, fees, expiry, obligations and triggered warnings. Drill down to linked audit records and assignment lots where present. Indicative numeric P&L requires usable quote evidence and backend math; unknown/stale data is unavailable rather than a zero P&L or a safe position. Closed positions remain in history without continuing open-position advice.

**WD-013: Alerts and activity.** Show advisory type, severity, stable alert ID, source run/position/decision references, generated time and delivery state. Distinguish queued, retry, acknowledged, unknown acknowledgment, failed and dropped states. Telegram acknowledgment does not prove the owner read or acted. No automatic resend or trade execution; supported manual recording is included through explicit preview/confirm forms.

**WD-014: Health and reconciliation.** Show last attempted/successful scan and monitor, current blockers, data/provider coverage, last delivery result and whether required manual records are current. A next-run time calculated from policy must not imply that a Windows task is installed. Show pending setup/verification honestly. Explain PC awake/logon requirements and the limit that a stopped PC cannot announce its own outage.

**WD-015: Useful presentation.** Put current blockers and manual next steps above charts. Provide basic ticker/date/state filters and readable detail pages. Format money and counts consistently; display Central time with zone/date, retaining UTC evidence. Display data time separately from page refresh time. The first version must work at a normal laptop viewport; complex charts, animations and a mobile-app design are deferred.

**WD-016: Refresh without financial side effects.** GET/page reload rereads persisted state only; backend collection updates market observations independently of entry eligibility. Any explicit request uses a bounded authenticated operation. No scan-triggered trade, Telegram send, account snapshot, migration or reconciliation/dedup/risk reset on a page read. Closing/restarting the browser must not stop or duplicate jobs.

**WD-017: Local access boundary.** Bind to loopback by default, with local authentication suitable for the specification's password-protected dashboard. Keep authentication secrets outside Git and out of page source, URLs and logs. Do not enable LAN/public access, cloud hosting or tunneling in WEB-1. Do not treat localhost alone as authorization to expose a financial journal to arbitrary browser origins; validate allowed host/origin and avoid permissive CORS. No third-party analytics or transmission of account data is needed.

**WD-018: Safe data access.** Read through a defined read/query layer and consistent SQLite read transactions. Return the account/ledger revision, policy and observation timestamps used so one response does not combine incompatible before/after states. Reuse Decimal values, serialized exactly where JSON is used; JavaScript must not recompute financial eligibility. Paginate growing history rather than loading the entire audit log. Do not expose database paths, raw secrets, unbounded SQL or arbitrary filesystem access through routes.

**WD-020: Daily check-in in the initial web journey.** Show existing positions/orders/stock lots for confirmation, then collect equity, net free cash, supported stock marks and explicit reconciliation acknowledgment. Reuse CLI validation/audit/revision semantics. Do not make the owner retype every position daily.

**WD-021: Add supported manual lifecycle forms incrementally.** Record intended/working orders, opening fills, closing orders/fills, verified cancellation/expiry and the documented assignment steps. Every form clearly records an external human action; it does not place or execute anything. Keep unsupported quantities, adjusted contracts and review states visible and safely blocked. Approvals require the same owner/event/deliverable evidence as the backend.

**WD-022: Preview, confirmation and concurrency.** Before committing, show the exact transition, financial inputs, collateral/reconciliation consequences and required reason. Apply through the existing domain/journal commands, with an actor and audit event. Reject a stale preview if ledger/policy inputs changed; require a fresh preview. Duplicate submit/reload must not create a second economic event. Use authenticated write requests with CSRF/origin protection; GET requests never mutate product state. Validation failures must preserve entered form values without partially applying changes.

**WD-023: Restricted controls.** Cash-flow recording and risk resets must retain their explicit audited review/confirmation rules and cannot be hidden inside a generic save or refresh. Financial-policy editing remains manual configuration plus validation/versioning in this phase. No browser control may silently raise caps, release a latch, enable real bookkeeping or override a hard gate. Deployment controls, task registration, token entry and arbitrary command execution are not dashboard features.

**WD-030: Three distinct change histories.** Keep (a) manual account/position events, (b) observed market/candidate/alert changes and (c) code/policy/schema changes separate. A market-price change is not a fill; an alert is not a trade; a new software release does not change the historical rule version used for an earlier decision.

**WD-031: Candidate comparisons.** Compare a symbol/contract's latest comparable successful evaluations and show newly eligible, no longer eligible and unchanged decisions with reason changes. Failed/partial runs must be shown as unknown/not evaluated for affected symbols, not as proof that an opportunity disappeared. Show the two comparison timestamps and policy versions; identify policy changes rather than attributing every change to the market.

**WD-032: Account and operational trends.** Start with historical confirmed equity/drawdown, recorded exposure/collateral and run/data/delivery outcomes. Include only observed timestamps; mark gaps and stale points. Do not interpolate missing quotes as facts, treat deposits as strategy gains, or display annualized option yield as account return. Add outcome/performance attribution only when definitions, fees, external flows and assignment treatment have been verified. A simple history table can ship before charts.

**WD-033: Traceable drill-down.** Each displayed change should lead to stored before/after values or evidence IDs, effective/recorded times when available, actor/source and policy/ledger revision. Preserve append-only financial audit semantics; corrections are new reviewed events, not silent edits of history. Read-only history export, if added, must exclude secrets and retain units/data-quality labels.

WD-030..033 are **future release work**: observed history comparisons/trends and traceable drill-down, without invented gaps or false performance claims. Retain their necessary evidence now. Advanced assignment/partial-fill/correction forms beyond the supported loop, multi-account, cloud/public/remote/mobile access, AI approvals, broad-universe screening, extra strategies and elaborate analytics/backtests remain deferred. Cash-flow/reset/approval extensions retain explicit audited controls; they must not weaken existing restrictions.

## First-delivery scope and feedback

### Smallest useful journey

1. Owner chooses one ticker in the local authenticated web page.
2. Actual stock price and put bid/ask load with source, observation/retrieval times and delay/unknown status; the page updates automatically. No eligible candidate is required to see prices.
3. Owner records a manually executed paper put, previews and confirms the actual fill and resulting exposure.
4. The exact position is monitored, with price/data/time warnings and controlled alerts; portfolio ticker/sector labels, counts, collateral and binding limits are visible.
5. Owner records a confirmed close; monitoring and queued advice reflect closure. Records persist across restart.

Routine use of this loop must not require CLI commands. Initial installation, credentials and advanced recovery may use documented commands. One ticker is sufficient for the first slice; a small chosen list is enough for the usable pilot. No invented approvals or fixture-price fallback.

### Include now

Market-data collection/persistence independent of entry eligibility; automatically updating stock/put views; watchlist distinct from entry approval; basic expiry/strike filters; actual-fill web recording and duplicate protection; minimal account check-in, working/open/close handling; exact-position monitoring; private Telegram delivery after explicit enablement; diversification labels and existing portfolio gates; authenticated local access, audit history, backup, operational health and restart behavior.

Preserve one paper account, standard puts, manual execution and all specification limits. Unsupported assignment/partial-fill/adjustment states must retain obligations, warn and block further entries until reconciled. No automatic trade or brokerage connection. No new risk-threshold tuning is authorized; exit thresholds remain provisional pending owner decision.

### Acceptance gate

Complete WT-AT-01..10 in [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md), applicable [market-data checks](PRODUCT_WORKFLOW_REQUIREMENTS.md#market-data-contract), supported backend regression and an owner walkthrough. Capture real provider-to-browser evidence separately from controlled deterministic tests. Unknown/delayed data cannot be passed off as live. A blocked feed is an explicit delivery blocker for the core price experience; it must not be used to label a historical-data viewer complete. Independently useful partial work may be delivered as partial.

### Defer

Charts, historical comparisons, AI, backtesting, advanced scenario/assignment/partial-fill workflows, multi-account support, broker integration, automatic execution, remote hosting and elaborate analytics. Use the owner-selected free Yahoo through Stockpile feed with live updates and indicative/unknown-quote-age labels. No feed purchase is planned; truthful automatic data access is required now. No 90-day waiting period.

Collect feedback after the first complete web journey. Prioritize unavailable/incorrect prices, lost trade records, misleading risk and missed/duplicate alerts before visual embellishments. No implementation completion is asserted by this scope change.
