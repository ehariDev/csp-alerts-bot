# Conservative Put Alerts — Engineering Specification

**Document ID:** CSP-TELEGRAM-001  
**Version:** 1.0 (implementation specification)  
**Prepared:** 2026-10-30  
**Status:** Ready for an engineering agent to implement and validate; **not** a claim of live deployment or profitability  
**Priority:** Safety, data integrity, and capital preservation before alert volume  
**Timezone:** `America/Chicago`; timestamps persisted in UTC  
**Primary upstream:** [`medloh/stockpile`](https://github.com/medloh/stockpile)  
**Pinned upstream baseline for initial implementation:** commit [`34bf6cf8101e54620ea1e2bda940f0a3aa88e791`](https://github.com/medloh/stockpile/commit/34bf6cf8101e54620ea1e2bda940f0a3aa88e791) (2026-09-25). Revalidate interfaces before upgrading.

> **Non-negotiable:** Build a **read-only research and Telegram alerting system**. The account owner manually executes all trades and paper trades in an external brokerage/simulator interface. **No brokerage integration of any kind**: no brokerage credentials, account synchronization, order APIs, automated orders, automated closing, order tickets, OAuth, trading SDK initialization, or user-facing “place trade” button. Telegram sends advisory notifications only. No inbound Telegram bot commands may alter positions or trade anything.

> **Risk statement:** A cash-secured put can lose most of the strike collateral if the underlying collapses. Delta is not the probability of success, simulated profit probability is not real-world edge, and stops/alerts do not cap losses. All quotes from the initial public-data source are **indicative and potentially stale**. The owner must independently confirm each trade at the manual trading platform.

---

## 0. Directions to the implementing software agent

1. First inspect the pinned Stockpile repository, this document, and the optional prior starter archive (`put_alerts_starter.zip`, if present); do not assume the starter has complete implementation or production-ready tests.
2. Implement in a **separate `telegram-put-alerts/` subproject** rather than modifying Stockpile's trading screens or order-entry modules. Preserve upstream compatibility. Where this specification conflicts with older starter behavior, **this specification takes precedence**.
3. Develop test-first: deterministic risk rules and manual-ledger reconciliation before Telegram delivery, LLM integration, or background scheduling.
4. Run unit, integration-with-mocks, end-to-end dry-run, and security-boundary tests. Demonstrate what actually runs; do not claim success based solely on source inspection.
5. Ship an empty whitelist and empty position ledger; **do not preapprove real symbols** or fabricate option quotes.
6. Provide operational documentation, an `.env.example`, an installation script, a sample alert, and a clear **DRY_RUN** default. Use secret placeholders, never actual secrets.
7. Protect hard constraints in code and tests; neither configuration UI nor an LLM may bypass them silently. Changes to budgets require manual configuration edits, validation, and a recorded policy revision.
8. If upstream rate limits or missing fields prevent reliable alerts, emit `DATA_UNAVAILABLE` or a research-only alert; never invent a quote, timestamp, delta, earnings date, credit, or approval.
9. Never invoke, import, or instrument Stockpile's broker/trade-order paths as part of the running service. Use dependency and network-egress tests to enforce the exclusion.
10. Ask the owner to configure their **approved underlying list**, valuation ceilings, account snapshot, and Telegram bot credentials locally. These are deliberately unspecified; there are no preselected live trades.

### 0.1 Deliverables

- Runnable Python 3.12+ service and CLI with the commands in §14.
- Versioned configuration + strict schema validation; approved-list template.
- Stockpile-backed **read-only** market adapter with timestamp/quality metadata and provider degradation handling.
- Portfolio reconciliation and pure, deterministic candidate/exit risk engines.
- Persistent manual journal (`SQLite` canonical; import/export CSV) including manual paper fills and assignments.
- Telegram outbound-only sender with persistent outbox, deduplication, backoff, and alert provenance.
- Timezone-aware exchange-session scheduler, health checks, logging, and operator runbook.
- Optional source-grounded AI analyst layer, disabled by default until validated.
- Offline tests and simulation fixtures; a 90-day paper-pilot checklist and performance reporting.
- `README.md`, `SECURITY.md`, `RUNBOOK.md`, `DATA_DICTIONARY.md` (or consolidated equivalents) and explicit deployment status.

---

## 1. Product outcome and scope

### 1.1 User goal

Support disciplined cash-secured-put income generation in a **$100,000 reference account**, balancing modest option-premium income against capital preservation. Assignment is acceptable **only on a security explicitly approved for ownership at an independently acceptable purchase price**. Candidate alerts are discretionary research, not instructions. The owner makes and records every trading decision manually, initially using a paper-trading simulator such as thinkorswim paperMoney; no simulator account is connected to the service.

### 1.2 In scope

- Scheduled scans of 10–15 (eventually) **owner-approved** liquid large-cap stocks and ETFs.
- Deterministic quality, valuation, event, option-liquidity, account-capital, position-concentration and drawdown gates.
- Candidate ranking **only after** every entry hard gate passes.
- Telegram `ENTRY_RESEARCH`, `NO_TRADE_DIGEST`, `PROFIT_EXIT`, `TIME_EXIT`, `LOSS_REVIEW`, `DELTA_REVIEW`, `ACCOUNT_RISK`, `DATA_ISSUE`, `RECONCILIATION_REQUIRED`, and `SERVICE_HEALTH` alerts.
- Manual entry/import of simulated or real **self-reported** opening/closing fills, working orders, assignments, dividends/corporate actions, positions and daily account snapshots.
- Reconciliation checks and analytical reporting including fees, estimated slippage, opportunity cost, and account-exposure changes.
- Read-only public market data from Stockpile's Yahoo path initially; later optionally a separate licensed **non-broker** options data vendor with valid quote timestamps.
- Read-only AI analysis of owner-provided and independently sourced public information; strict veto-only and evidence requirements.

### 1.3 Explicitly out of scope

- Brokerage authentication, account connection, broker portfolio read, paper-trading platform login, or scraping a trading platform.
- Broker execution, order entry, order amendments, rolling transactions, cancel requests, covered-call execution, leverage, uncovered shorts, automated trading, or auto-following Telegram alerts.
- Promises about annual returns, a particular win rate, or “90% safe” trades.
- AI autonomous changes to portfolio risk rules, ownership approvals or valuation caps.
- Tax calculation or interpreting assignment as tax advice.
- Guaranteed near-real-time alerts when using Yahoo Finance or consumer internet connections.

### 1.4 Core quality bar

**No alert is preferable to an unsafe, misleading, stale, or untraceable alert.** If the service is uncertain whether a candidate is eligible, entry alert status is `BLOCKED`; if position risk might be present but quotes are missing, issue calendar/risk/data warnings rather than implying the position is safe.

---

## 2. Reference architecture

```text
                                     USER-CONTROLLED INPUTS
                         +------------------------------------------+
                         | Approved whitelist / max purchase price  |
                         | Manual option trade journal              |
                         | Manual owned shares / assignments        |
                         | Manual equity / truly free cash snapshot |
                         +--------------------+---------------------+
                                              |
                                              v
PUBLIC MARKET DATA                     [SQLite canonical store] <--- [CSV importer]
  Stockpile `chain.fetch_chain`                   |
  Yahoo quotes + Greeks                         | validated snapshot
  `earnings.fetch_earnings_dates`                v
  `iv_surface.compute_iv_excess` ---> [Data normalizer/quality gate]
       |                                      |
       v                                      v
  [source provenance] ---------> [Deterministic risk/entry/exit engine]
                                               |
                            +------------------+------------------+
                            |                                     |
                 [Owner-controlled AI reviewer]         [Alert policy engine]
                 Sources/citations, veto-only                  |
                            |                                  v
                            +--------------------------> [SQLite outbox]
                                                               |
                                                    [Telegram sendMessage]
                                                               |
                                                       [Owner's private chat]
                                                               |
                                                 [Manual paper/live trading]
                                                               |
                                              [Manual journal reconciliation]
```

**Trust boundary:** market feeds and AI output are *untrusted inputs*. The risk engine is pure and deterministic. Only alert transport owns a Telegram token. No executable trading action exists anywhere in the service. The manual ledger, not the options vendor, is the source of truth for holdings, pending orders, and assignment status.

### 2.1 Execution components

| Component | Responsibility | Failure mode |
|---|---|---|
| `market/stockpile.py` | Read-only Stockpile chain, earnings and optional surface IV | Distinguish missing/stale vs. no candidates |
| `market/normalize.py` | Unit normalization, decimal types, timestamp provenance, OCC contract ID, quote checks | Reject bad row, never infer missing values |
| `market/calendar.py` | Exchange sessions/early closes, UTC time, calendar-day DTE | Fail closed for entry when unavailable |
| `policy/config.py` | Typed configuration, risk-rule versioning, approval expiry | Reject invalid or unsafe config |
| `policy/entry.py` | Pure eligibility decisions and rejection codes | Any unknown prerequisite blocks entry |
| `policy/exits.py` | Profit/time/delta/loss alerts, missing-data escalation | Time alerts survive quote outages |
| `policy/portfolio.py` | Cash collateral, equity exposure, sectors, expiry weeks, drawdown, scenario stress | Fail closed if ledger unreconciled |
| `journal/store.py` | Durable manual trades, holdings, account snapshots, audit trail | Atomic updates, backup, reject inconsistent state |
| `journal/import_csv.py` | Strict import/export from manual paper trades | Quarantine invalid rows, no silent correction |
| `ai/reviewer.py` | OPTIONAL cited risk assessment; may veto, not permit blocked trades | Timeout/parse failure = `MORE_RESEARCH` |
| `alerts/policy.py` | Severity, dedup, trigger transitions, rate limits | Never suppress newly escalated risk |
| `alerts/telegram.py` | **Outbound only** HTTPS `sendMessage`; retry and observability | Track acknowledged delivery vs. unknown |
| `jobs/scan.py` | Entry research scan and no-trade report | No invented fills or approvals |
| `jobs/monitor.py` | Open-put monitor and account-risk notifications | Still check time/ledger when feed fails |
| `jobs/reconcile.py` | Daily manual input health and reminders | Block fresh entries until reconciled |
| `cli.py` | Dry-run, import/export, checks, historical replay, test Telegram | No execute or broker command |

---

## 3. Stockpile integration: precise supported hooks

### 3.1 Upstream baseline and reuse

Pinned baseline: [`34bf6cf`](https://github.com/medloh/stockpile/commit/34bf6cf8101e54620ea1e2bda940f0a3aa88e791). Stockpile is a Python monorepo requiring Python >=3.12 and a `uv` workspace. Existing useful paths:

| Stockpile module | Existing function / behavior | How to use |
|---|---|---|
| `options-scanner/options_scanner/chain.py` | `fetch_chain(ticker, opt_type='both', min_dte=30, max_dte=90, provider='yahoo', ...)` -> DataFrame | **Primary read-only adapter**; use `provider='yahoo'` only |
| `options-scanner/options_scanner/chain_common.py` | `build_option_row(...)` canonical fields; may fall back to `last` | Validate bid and ask independently for any entry; never use fallback `last` as executable quote |
| `options-scanner/options_scanner/earnings.py` | `fetch_earnings_dates(ticker)`; `annotate_earnings(df, dates)` | Secondary screening, not proof that no event exists |
| `options-scanner/options_scanner/iv_surface.py` | `compute_iv_excess(df, ...)` | Secondary ranking metric only |
| `options-scanner/options_scanner/iv_scores.py` | IV surface scores; supplementary | Never label IV excess as demonstrated edge |
| `options-scanner/options_scanner/montecarlo/*` | Underlying-path simulations; GBM + earnings jumps | OPTIONAL diagnostic; **not** actual historical options backtest |
| `options-scanner/options_scanner/main.py` | `--puts --min-dte --max-dte --min-delta --max-delta --json/--agent` | Useful for debugging but **NOT** the production entry interface |

**Important upstream JSON limitation:** `main.py::_to_candidate` exposes `bid`, `ask`, `mid`, delta, open interest and `earnings_before_exp`, but not full daily `volume`, `last_trade_days`, and quote source details. It also uses filtering/ranking shortcuts intended for presentation. **Do not base production liquidity/freshness gating on the CLI's ranked JSON.** Read raw chain rows with all available fields and do filtering in the new engine.

**Important upstream surface-fit limitation:** for IV fitting, fetch **both calls and puts** to anchor both wings when possible, then select puts **after** the fit (the upstream `fetch.fetch_and_enrich` explicitly describes this). A puts-only fetch is acceptable when surface scoring is unavailable but should not invent a high-confidence IV anomaly. Data/earnings gates remain independent of IV fitting.

**Explicitly forbidden import paths at runtime:** `options_scanner.trade_actions`, `options_scanner.confirm_gate`, `options_scanner.tabs.trades`, `options_scanner.tabs.rolls`, `options_scanner.trades_store`, `stocks_shared.schwab_live`, `schwab.*`, `moomoo_*` gateways, and any order/place/submit/cancel/modify endpoint. Stockpile package dependencies may include SDKs, but the service must not instantiate or invoke them. Avoid importing Streamlit presentation modules.

### 3.2 Stockpile raw-row fields to preserve

`type`, `strike`, `expiration`, `dte`, `spot`, `bid`, `ask`, `mid`, `last`, `iv`, `iv_excess`, `signal_score`, `delta`, `gamma`, `theta`, `vega`, `open_interest`, `volume`, `last_trade_days`, `last_trade_ms`, `earnings_count`, `hv_20`, `vr_ratio`.

Capture `provider='yahoo'`, `fetched_at_utc`, `underlying_price_observed_at_utc` if authoritative and present, `option_quote_observed_at_utc` **nullable**, `option_quote_age_seconds` **nullable**, `data_class='INDICATIVE_UNTIMESTAMPED'` for the initial Yahoo adapter. **Never** label `fetched_at` or `last_trade_ms` as a bid/ask timestamp; `last_trade_ms` refers to the option's last trade print. Yahoo-computed delta is Black–Scholes model delta, not broker-quoted Greeks. The upstream Yahoo path currently uses a fixed 4.5% risk-free rate in `chain.py`; model assumptions must be surfaced or documented.

The upstream chain can return a `mid` derived from a last trade when a two-sided quote is absent; **reject** such entries at our validation stage. Upstream `main.py` may relax a daily-volume filter after a failed scan; **we must not silently relax any hard entry constraint**.

### 3.3 Market data tiers

- **Tier R — research-only / initial implementation:** Yahoo via Stockpile; no reliable per-contract exchange quote timestamp. Send entry notices only as **`INDICATIVE / VERIFY AT YOUR BROKER`**, never `LIVE_VERIFIED`. Require observed bid >0, ask >= bid, volume and OI thresholds, recent *last trade print* as a weak secondary check, and all other hard gates. An absent trustworthy quote timestamp is not evidence of quote recency. Never calculate a “guaranteed execution price.”
- **Tier V — optional future verified feed:** independent licensed options vendor supplying exchange bid/ask timestamps and market-session flags, without linking a brokerage account. Validate quote age <= configurable 120 seconds during an open session; otherwise fall back to Tier R or block an actionable rating. New vendor requires separate license/contract and integration tests.
- **No data:** classify `DATA_UNAVAILABLE`, not `NO_TRADE`. Never substitute old last prices for current ask to compute an exit.

All messages show data tier, source, retrieved time, and **quote timestamp if actually known**. Entry/exit math uses conservative bid for potential entry and ask for hypothetical buyback.

---

## 4. Capital policy — hard, deterministic requirements

### 4.1 Account budgets (USD)

| Rule | Value | Implementation |
|---|---:|---|
| Reference account value | `$100,000` | Initial baseline; real manually entered equity governs drawdown; review limits if equity shifts materially |
| Pilot deployed put collateral | `$15,000` | 90-day pilot; configurable `PILOT` profile enabled by default |
| Normal deployed risk exposure | `$20,000–$25,000` | New entries may bring **aggregate exposure** up to **$25,000**; prefer `$20K–$25K` operating band |
| Emergency absolute exposure limit | `$30,000` | **Incident threshold**, never optional entry headroom |
| Uncommitted cash intent | `>= $70,000` | Informational goal, subject to holdings/collateral and actual snapshot |
| Max exposure per ticker | `$9,000` | Combine put collateral + owned equity exposure for same underlying |
| Max combined sector exposure | `$10,000` | Count put collateral and owned equity, not just new premium |
| Max active short-put contracts | `4` | Includes working/pending and open; 1 per underlying |
| Max individual-company names | `2` | Count companies with active puts or stock holdings; ETFs separately categorized but overlapping exposure flagged |
| Max short-put collateral per expiration ISO week | `$15,000` | Sum all pending+open puts assigned to expiry week |
| No margin or leverage | `true` | Use only owner-entered **free unencumbered cash** available to reserve for new puts; no buying-power proxy |
| Aggregate sector / correlated ETF exposure | Cap as above + manual correlated bucket review | ETFs require owner-assigned correlation bucket and conservative stress review |

**Two exposure metrics must be displayed:**

1. `put_collateral_usd = Σ(strike × deliverable_multiplier × contracts)` for active (`PENDING`, `WORKING`, `OPEN`, `CLOSE_WORKING`) puts; a close order does **not** release collateral until manually confirmed filled.
2. `aggregate_equity_risk_usd = put_collateral_usd + owned_stock_risk_reference_usd`, where owned-stock reference equals `shares × reference_price` (current manual mark). Include pending stock purchases if manually recorded. This is a conservative *exposure budget*, **not** mathematical maximum loss or an account's actual margin requirement. On stock drawdowns, marking down stock reduces measured exposure; keep separate stress metrics and optionally a high-water reference to avoid re-levering just because prices fell.

For initial safety, require `aggregate_equity_risk_usd + proposed_put_collateral <= 25,000` AND `put_collateral_usd + proposed_put_collateral <=25,000` (<=15,000 in pilot). Also require the per-ticker, sector, expiration-week, max-contract, max-names, correlated-bucket, and manually available-cash rules to pass simultaneously.

**Working/pending orders reserve full collateral**; never assume a working order is unfilled, or a closing order has filled, until the owner updates its status.

### 4.2 Drawdown thresholds

Define `drawdown = max(0, (historical_recorded_equity_peak - latest_equity) / historical_recorded_equity_peak)`, using all valid, chronologically ordered **manually reconciled** snapshots from account inception. If historical snapshots are missing, starting baseline is configured but **do not invent** an earlier equity peak; require the owner to confirm a reset/new baseline.

- `>=3%`: immediately **pause all new entry alerts**; continue position and portfolio warnings.
- `>=5%`: escalate **reduce-exposure review**, require manual action/review and suspension of entries.
- `>=8%`: **hard halt** for all new entries until explicit manual risk-reset after review; continue critical monitoring and assignment reminders. No automatic re-enable from a transient rebound.

The equity snapshot must be no older than **30 hours** for routine scanning, and **reconciled since the latest executed trade/assignment**. If no recent equity snapshot exists, block new-entry alerts. Daily reconciliation is strongly preferred and a **daily snapshot is mandatory** for alerts characterized as portfolio-capacity verified. These thresholds are decision rules, not guaranteed loss limits.

### 4.3 Underlying and event gates

Each underlying requires human-created approval with `ticker`, `asset_type` (`STOCK|ETF`), `sector`, `correlation_bucket`, `max_buy_strike`, `approved_through`, `events_reviewed_on`, `events_cleared_through`, `ownership_reason`, and `approved_by='owner'`. A 10–15-name list is the goal, **not** a prefilled list.

- Approval must be current; manual company/news review <=7 calendar days old.
- A strike may **not exceed** the owner-approved acquisition price (`max_buy_strike`); current option premium does not make an otherwise unacceptable acquisition acceptable.
- Underlying must be liquid, an ETF or approved high-quality business; user owns the valuation decision. Financial filters can produce a `MORE_RESEARCH` flag but cannot authorize an unapproved ticker.
- No entry through an earnings release, unresolved major corporate action, special dividend risk, merger vote, regulatory decision, known high-impact litigation/catalyst, etc. For individual companies, owner-documented clearance must run at least through expiration. For ETFs, review constituent-specific concentration and known fund events.
- **Unknown earnings date** or failed event fetch is **not** “no earnings”; no new entry until independent event evidence clears the period. The upstream earnings parser only returns the nearest future date; handle limitations with a separate event-review record.
- No split-adjusted/non-standard contract deliverables until correctly modeled. Standard contracts generally represent 100 shares, but **never hard-code that assumption without checking standard OCC multiplier/deliverable**. Block if multiplier/deliverable is unknown or non-standard.
- No new puts on the same underlying while a put is already open/working or while owned shares would breach the per-name limit.

### 4.4 Option entry gates

| Field | Hard gate / decision |
|---|---|
| Right | PUT only, SELL research only |
| DTE | 35–45 **calendar** days, inclusive, calculated from actual expiration date in exchange-local context |
| Absolute delta | `0.10 <= abs(delta) <= 0.15`, inclusive; put delta must be negative and finite |
| Moneyness | Strike strictly below currently observed underlying price |
| Min OI | `>= 500` contracts |
| Min daily contract volume | `>= 50`; **never** silently relax if volume is 0 after close; scan only during exchange session |
| Bid/ask | Both >0, finite, ask >= bid |
| Minimum bid | `>= $0.20/share`, configurable and logged |
| Spread | `(ask-bid) <= $0.05/share` **AND** `(ask-bid)/mid <=10%` (strict conservative starting values; adjust only with recorded policy version) |
| Last option trade | <=1.5 days old **if available**; older/missing print is a caution or block per configured policy and must never masquerade as quote timestamp |
| Cash collateral | Full `strike × multiplier × contracts`; exactly one contract per new suggestion in pilot |
| Price to evaluate opening premium | **Bid**, not theoretical midpoint; fees/slippage shown separately |
| Earnings/event | Explicitly cleared through expiration |
| IV / IV+pp | Supplemental ordering, **not** hard evidence of profitable mispricing |
| Calendar | Verified regular relevant US options session; avoid immediately before close, unknown hours, holidays/early closes |
| Account data | Fresh, reconciled manual account/holdings journal; drawdown below 3%; no hard-halt state |

This conservative bid-ask rule will exclude many legitimate contracts; that is intentional for the initial pilot. Policy should be adjustable only as a versioned, manually approved change after analyzing actual exclusion rates and realistic fills.

### 4.5 Example risk decisions

- `$85` strike, 1 standard put -> `$8,500` gross collateral. If active same-sector exposure is `$4,000`, new sector exposure `$12,500` -> **block** because `$10,000` sector cap.
- `$100` strike, 1 standard put -> `$10,000` -> **block** due to `$9,000` per-underlying cap even if cash is plentiful.
- Two already eligible alternative alerts for different contracts are **not authorization** to open both. After entering the first as `WORKING` or `OPEN`, all other candidates must be revalidated against the updated journal.
- A 3.1% drawdown with zero open positions -> **no new entry** until manual policy-approved resume conditions are satisfied.
- If option OI is 700 but bid/ask is `$0.40/$0.55`, spread is `$0.15` and 31.6% of midpoint -> **block**.

---

## 5. Manual position journal and authoritative state

**Canonical store:** local SQLite with WAL, transactions, constraints, audit log, encrypted disk where practical, and daily backup. CSV is a user-friendly import/export; CSV is **not** sufficient alone for concurrency, state transitions, or exactly-once alert handling. A simpler CSV-only pilot may exist, but graduating to the canonical SQLite record is an acceptance requirement before routine use.

### 5.1 Required tables (schematic)

```sql
CREATE TABLE policy_versions (
  policy_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL,
  sha256 TEXT NOT NULL, approved_by TEXT NOT NULL, config_json TEXT NOT NULL
);
CREATE TABLE approved_underlyings (
  ticker TEXT PRIMARY KEY, asset_type TEXT NOT NULL,
  sector TEXT NOT NULL, correlation_bucket TEXT NOT NULL,
  max_buy_strike TEXT NOT NULL, approved_through TEXT NOT NULL,
  events_reviewed_on TEXT NOT NULL, events_cleared_through TEXT NOT NULL,
  ownership_reason TEXT NOT NULL, approved_by TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);
CREATE TABLE option_positions (
  trade_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL,
  paper INTEGER NOT NULL CHECK(paper IN (0,1)),
  ticker TEXT NOT NULL, option_right TEXT NOT NULL CHECK(option_right='P'),
  expiration TEXT NOT NULL, strike TEXT NOT NULL, multiplier INTEGER NOT NULL,
  contracts INTEGER NOT NULL, sector TEXT NOT NULL, correlation_bucket TEXT NOT NULL,
  state TEXT NOT NULL, opened_at_utc TEXT, actual_open_credit_per_share TEXT,
  actual_close_debit_per_share TEXT, fees_usd TEXT NOT NULL DEFAULT '0',
  closed_at_utc TEXT, parent_event_id TEXT,
  last_reconciled_at_utc TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE stock_lots (
  lot_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, shares TEXT NOT NULL,
  reference_price TEXT NOT NULL, reference_asof_utc TEXT NOT NULL,
  cost_basis_reference_usd TEXT, acquisition_kind TEXT NOT NULL,
  linked_trade_id TEXT, sector TEXT NOT NULL, correlation_bucket TEXT NOT NULL
);
CREATE TABLE account_snapshots (
  snapshot_id TEXT PRIMARY KEY, asof_utc TEXT NOT NULL,
  equity_usd TEXT NOT NULL, free_unencumbered_cash_usd TEXT NOT NULL,
  manual_confirmed INTEGER NOT NULL, reconciliation_version TEXT NOT NULL
);
CREATE TABLE market_observations (
  observation_id TEXT PRIMARY KEY, contract_key TEXT NOT NULL,
  retrieved_at_utc TEXT NOT NULL, quote_at_utc TEXT,
  provider TEXT NOT NULL, tier TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
  quote_json TEXT NOT NULL
);
CREATE TABLE alert_outbox (
  alert_id TEXT PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE,
  class TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
  created_at_utc TEXT NOT NULL, next_attempt_at_utc TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, telegram_message_id TEXT,
  content_text TEXT NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE audit_log (
  event_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL,
  event_type TEXT NOT NULL, actor TEXT NOT NULL,
  before_json TEXT, after_json TEXT, source TEXT NOT NULL
);
```

Use `Decimal` for all finance values; avoid binary floating-point for collateral, cash, fees, premiums and thresholds. Store numeric decimal values as strings in JSON and SQLite if not using integer cents. Add schema migrations and uniqueness constraints on business keys.

### 5.2 Manual ledger states and transitions

```text
DRAFT -> PENDING (user intends manual order) -> WORKING (user reports working)
                                             -> OPEN (user confirms filled)
                    PENDING / WORKING ------> CANCELED (user verifies cancellation)
OPEN -> CLOSE_WORKING -> CLOSED (user confirms closing fill)
OPEN -> EXPIRED (verified expiry; no automatic assumption)
OPEN -> ASSIGNED_PENDING_RECONCILIATION -> ASSIGNED_RECONCILED
OPEN -> ADJUSTMENT_REVIEW (split/merger/nonstandard contract)
```

- `PENDING`, `WORKING`, `OPEN`, `CLOSE_WORKING`: reserve full strike collateral. `DRAFT`: does **not** reserve collateral but does not constitute permission to trade.
- `OPEN` requires explicit manual actual fill quantity, time, and actual premium. If a paper simulator reports an unrealistically good fill, tag it, and separately estimate conservative execution slippage.
- `CLOSED`: only on manually recorded confirmed closing fill, with actual debit, commissions, and time.
- `EXPIRED`: require owner confirmation the contract expired and no late exercise/assignment remains to be reconciled; do not auto-close when DTE becomes 0.
- `ASSIGNED_PENDING_RECONCILIATION`: freeze entries; owner records received shares and cash payment, then transitions to `ASSIGNED_RECONCILED`. Avoid double-counting the same put collateral and assigned stock after reconciliation; preserve audit history.
- `CLOSE_WORKING` must still receive risk and price alerts until closing fill confirmed; avoid repeated redundant profit messages after acknowledged action but never assume closure.
- In v1, only one contract per underlying is proposed; schema supports quantity/partial fills and reconciliation for a later phase.
- Include `account_scope` (e.g. `paper_primary`) to avoid mixing paper and real portfolios. Do not assume external account data or sync.

### 5.3 Manual input required

1. `approved_underlyings`: owner-maintained current whitelist, acceptable maximum strike, review/clearance dates, sector/correlation bucket.
2. New opening fill: `trade_id`, exact ticker/expiry/strike/right/multiplier, status, fill time, quantity, credit/share, actual fees.
3. Manual closing fill: date, debit/share, quantity, fees and verified state.
4. Assignment: expiry/notification date, stock lot and actual purchase cash, pending until resolved.
5. Owned shares acquired independently: size, mark, sector and recent mark timestamp.
6. Account equity and **cash genuinely free for new puts** once daily and after every position change; include all other trading activity affecting the account and any funds reserved elsewhere.
7. **Explicit end-of-day reconciliation acknowledgment** confirming recorded positions, orders, owned shares, available cash and equity are complete. If not, no entry alerts the following day.

Reconciliation must be **manual by design**. No OAuth/import from broker statements unless user later explicitly changes product scope (currently prohibited). CSV exports typed by the user are allowed; do not connect to provider accounts automatically.

### 5.4 Equity and fees math

- Gross collateral = `strike × multiplier × contracts`.
- Indicative entry premium at bid = `bid × multiplier × contracts`; actual entry premium = `actual_fill_credit × multiplier × contracts`.
- Current conservative short-put buyback liability = `ask × multiplier × contracts` (only if an actual current ask is available).
- Estimated close P&L = `(actual_open_credit - current_ask) × multiplier × contracts - fees`; show **unrealized and indicative**.
- Realized P&L = `(actual_open_credit - actual_close_debit) × multiplier × closed_contracts - both-side fees`, except assignment/stock adjustments must be accounted for separately.
- Expiry break-even = `strike - opening_credit_per_share`, excluding fees; use *actual* fill for journal, indicative bid for research alert.
- Do **not** use annualized option premium yield as account return or risk-adjusted expected return; if displayed, label denominator and assumptions.

---

## 6. Typed contract / data interfaces

### 6.1 Canonical market observation

```json
{
  "contract_key": "EXAMPLE|2026-12-04|P|75.00|100",
  "underlying": "EXAMPLE",
  "option_right": "P",
  "expiration": "2026-12-04",
  "strike": "75.00",
  "multiplier": 100,
  "spot": "80.00",
  "bid": "0.50",
  "ask": "0.54",
  "delta": "-0.130",
  "delta_source": "STOCKPILE_BLACK_SCHOLES",
  "implied_volatility": "0.2400",
  "open_interest": 1000,
  "daily_volume": 100,
  "last_option_trade_at_utc": "2026-10-30T14:40:00Z",
  "quote_at_utc": null,
  "retrieved_at_utc": "2026-10-30T15:05:00Z",
  "provider": "YAHOO_VIA_STOCKPILE",
  "data_tier": "INDICATIVE_UNTIMESTAMPED",
  "event_check": "VERIFIED_EXTERNALLY",
  "iv_excess_pp": "2.1"
}
```

**ALL VALUES ABOVE ARE SYNTHETIC SCHEMA FIXTURES**, not historical or current prices. `quote_at_utc=null` is required where no trustworthy quote timestamp exists. Store raw source payload hash and source errors separately. Contract key supports standard 100-share cases; nonstandard multipliers must be explicitly checked and initially blocked.

### 6.2 Approved security record

```json
{
  "ticker": "EXAMPLE",
  "asset_type": "STOCK",
  "sector": "consumer_staples",
  "correlation_bucket": "defensive_consumer",
  "max_buy_strike": "75.00",
  "approved_through": "2026-12-31",
  "events_reviewed_on": "2026-10-30",
  "events_cleared_through": "2026-12-31",
  "ownership_reason": "Fictional example only; replace with owner's actual diligence",
  "approved_by": "owner"
}
```

### 6.3 Deterministic gate decision

```json
{
  "candidate_key": "EXAMPLE|2026-12-04|P|75.00|100",
  "policy_version": "1.0.0",
  "decision": "PASS_RESEARCH_ONLY",
  "reason_codes": [],
  "exposure_before_usd": "8500.00",
  "additional_collateral_usd": "7500.00",
  "exposure_after_usd": "16000.00",
  "manual_free_cash_after_usd": "12500.00",
  "data_tier": "INDICATIVE_UNTIMESTAMPED",
  "requires_broker_manual_quote_check": true,
  "evaluated_at_utc": "2026-10-30T15:05:02Z"
}
```

`decision` enum: `PASS_RESEARCH_ONLY | PASS_VERIFIED_DATA | BLOCKED | MORE_RESEARCH | DATA_UNAVAILABLE`. `PASS_VERIFIED_DATA` only for a future genuinely timestamped licensed feed; even then **no automatic trading**. `reason_codes` must be machine-stable: `NOT_APPROVED`, `APPROVAL_EXPIRED`, `VALUATION_TOO_HIGH`, `EVENT_UNVERIFIED`, `EARNINGS_IN_WINDOW`, `DTE_OUT_OF_RANGE`, `DELTA_OUT_OF_RANGE`, `INSUFFICIENT_OI`, `INSUFFICIENT_VOLUME`, `BAD_BID_ASK`, `SPREAD_TOO_WIDE`, `MISSING_MARKET_DATA`, `NONSTANDARD_CONTRACT`, `INSUFFICIENT_FREE_CASH`, `SINGLE_TICKER_LIMIT`, `SECTOR_LIMIT`, `CORRELATED_LIMIT`, `EXPIRY_WEEK_LIMIT`, `CONTRACT_COUNT_LIMIT`, `INDIVIDUAL_NAME_LIMIT`, `NORMAL_EXPOSURE_LIMIT`, `PILOT_COLLATERAL_LIMIT`, `DRAWDOWN_PAUSE`, `HARD_HALT`, `LEDGER_STALE`, `LEDGER_UNRECONCILED`, `MARKET_CLOSED`, `UNKNOWN_SESSION`, `AI_VETO`, `AI_UNAVAILABLE`.

### 6.4 Alert event schema

```json
{
  "alert_id": "uuid",
  "kind": "ENTRY_RESEARCH",
  "severity": "INFO",
  "contract_key": "EXAMPLE|2026-12-04|P|75.00|100",
  "trade_id": null,
  "policy_version": "1.0.0",
  "source_observation_id": "uuid",
  "manual_snapshot_id": "uuid",
  "decision_id": "uuid",
  "occurred_at_utc": "2026-10-30T15:05:02Z",
  "dedup_key": "ENTRY_RESEARCH|2026-10-30|EXAMPLE|2026-12-04|P|75.00",
  "payload": {"indicative": true, "manual_action_required": true},
  "transport_state": "PENDING"
}
```

Transport states `PENDING | SENDING | SENT | RETRY | FAILED_FINAL | UNKNOWN_ACK`. Persist before send; record Telegram `message_id` and API acknowledgment. Network timeouts after POST might have delivered a message, so **at-most-once / exactly-once delivery cannot be guaranteed**; document duplicate possibility and attempt a controlled reconciliation strategy.

---

## 7. Entry scan algorithm

### 7.1 Hard gate pipeline (order matters)

```python
# PSEUDOCODE ONLY; this module must not import broker SDKs.
def scan_entries(now_utc):
    assert exchange_calendar.is_trading_session_open(now_utc)
    policy = validated_signed_off_policy()
    book = load_manual_reconciled_book_or_block()
    if book.drawdown >= 0.03 or book.hard_halt: return blocked_digest()
    approved = current_approved_underlyings()
    if not approved: return no_trade_digest("EMPTY_WHITELIST")
    candidates = []
    for ticker in approved:
        raw = stockpile_read_only_chain(ticker, min_dte=35, max_dte=45)
        normalized = normalize_and_record_quotes(raw)
        enriched = annotate_events_and_optional_iv_surface(normalized)
        for option in puts_only(enriched):
            decision = deterministic_entry_gate(option, policy, book, ticker)
            persist_evaluation(decision)
            if decision.passed:
                ai = ai_review_or_disabled(decision)  # VETO-ONLY; no override
                if ai.status in ("PASS_REVIEW", "DISABLED"):
                    candidates.append((option, decision, ai))
    return alert_top_alternatives(risk_rank(candidates), limit=3)
```

1. Fail closed on calendar failure, stale/unreconciled account snapshot, unknown whitelist or any 3%+ drawdown.
2. Fetch once per approved ticker; cache for short appropriate TTL, respecting Yahoo throttling and provider limits.
3. Normalize and persist price/option data with source provenance; do not treat a scan timestamp as a quote timestamp.
4. Run event checks independently. Empty list of fetched earnings dates is **unknown**, not automatically safe.
5. Reject all violating contracts with structured `reason_codes`; never relax volume, OI, spread, DTE or delta implicitly.
6. Apply research layer only to deterministically passing contracts. An LLM response may veto or request investigation; it cannot change thresholds, size, strike, ownership price, or hard-gate status.
7. Rank among passing opportunities by a **transparent** heuristic (e.g. cash-premium-at-bid per collateral plus a liquidity penalty and conservative event score); high premium must never override a gate. Provide rationale, not “chance of winning.”
8. Emit no more than three distinct **alternative** candidates per scheduled run and preferably one per underlying. Multiple alerts are not jointly approved. If no candidate meets rules, send a once-daily `NO_TRADE` digest with reason counts; otherwise avoid spam.
9. Automatically suppress entry-alert sends if any required manual ledger update occurred after the latest account reconciliation acknowledgment.
10. Every entry alert states manual verification is required and explicitly: **NO ORDER SENT**.

### 7.2 Rank definition (initial proposed)

Calculate on **passing** candidates:

`gross_bid_yield_per_calendar_day = 100 * bid / (strike * DTE)` as a **research ordering indicator**, not a return forecast. Optional rule: rank by liquidity quality first, then bid yield, then IV+pp and OI. **Never** treat a stock with higher premium as intrinsically safer. Expose each ranking component and rule-version; add a full numerical score only after it is calibrated and historically tested.

For all candidate ranking, use the manual-approval valuation as dominant prerequisite. Ranking does **not** change sizing; propose 1 contract at most in pilot.

---

## 8. Exit, assignment, and account monitoring

### 8.1 Existing short-put triggers

For each journal state `OPEN` or `CLOSE_WORKING`:

| Trigger | Rule | Severity | Required text |
|---|---|---|---|
| `PROFIT_EXIT` | `(actual_open_credit - current_buyback_ASK) / actual_open_credit >= 0.60` | ACTION | Consider manual buy-to-close; indicative, fees/slippage may differ |
| `TIME_EXIT` | Calendar `DTE <=21` | ACTION | Close/assess according to system; remains active with no quote |
| `LOSS_REVIEW` | `current_buyback_ASK >= 2.0 × actual_open_credit` | URGENT | Review close; not a guaranteed stop-loss |
| `DELTA_REVIEW` | `abs(current_delta) >=0.30` | URGENT | Review downside/assignment risk, independently verify delta |
| `FUNDAMENTAL_REVIEW` | Ownership thesis invalid, earnings/corporate-action risk discovered | URGENT | Review/close; block new positions |
| `DATA_ISSUE` | Missing/invalid bid/ask; stale feed; mismatch contract | WARN | Cannot estimate exit price; check broker manually |
| `ASSIGNMENT_REVIEW` | ITM/near-expiry/assignment reported or corporate action | URGENT | Verify manually; assignment can occur before expiry |
| `ACCOUNT_RISK` | Snapshot drawdown 3%, 5%, 8%+ or portfolio exposure breach | URGENT/CRITICAL | Pause/reduction/halt state, quote snapshot timestamp |
| `RECONCILIATION_REQUIRED` | Manual book/snapshot stale or unreconciled | WARN/URGENT | New entries suspended; update ledger |

**Conservative calculation:** use **current ask** to estimate buy-to-close debit, not the midpoint or last print. Report dollar P&L with `×100×contracts`, net of known fees where possible, and label as an estimate. For short puts, a doubling of the buyback cost may happen quickly and the final loss can far exceed the original premium; do not imply alerts guarantee an exit price. Note that `PROFIT_EXIT` is 60% of original premium **earned**, not 60% remaining.

**Missing price:** emit `TIME_EXIT` from the calendar even if the market is closed, and a `DATA_ISSUE` if the option cannot be quoted; suppress numeric profit/loss triggers whose data cannot be established. Do not claim “no exit signal” merely because a fetch failed.

**Approaching expiry:** at DTE <=7, reinforce manual assignment review, including early exercise and after-hours/expiration risk; at DTE <=1, warn even if the position appears far OTM on an old quote. A user is responsible for handling real/virtual shares and assignment in their broker/simulator.

### 8.2 Alert lifecycle

- Track each `trade_id + trigger` with `INACTIVE -> ACTIVE -> ACKNOWLEDGED -> RESOLVED` states; manual acknowledgment **does not** change the journal position or suppress critical escalations.
- Entry candidate dedup: `day + contract_key + policy_version` and scan-hour grouping; avoid repeated identical opportunity texts.
- Exit trigger dedup: `trade_id + type + local trading date`; send only on initial transition plus a daily reminder if still open/critical. **Escalation to 5% or 8% drawdown creates a new alert immediately**, even after a 3% acknowledgment.
- Risk `CRITICAL` can repeat if unresolved daily; cap redundant intraday messages, keep escalation history. Use optional quiet hours for noncritical messages only (never quietly drop urgent notifications).
- Every alert is advisory; acknowledgment may be recorded in CLI/local database, but no inbound Telegram command is necessary or supported.

### 8.3 Portfolio stress diagnostics (Phase 2)

Scenario-mark owned shares and short puts under parallel underlying price declines of `-10%, -20%, -30%, -40%`, higher implied volatility, and simultaneous sector correlation. Output indicative assignment obligations, cash needed, unrealized stress losses and coverage of free unencumbered cash. Avoid claiming path-based scenarios are worst-case guarantees. Treat ETF-overlap look-through as unavailable until data coverage is established; manually flag concentrated ETFs as correlated.

---

## 9. Telegram delivery and security

### 9.1 Delivery contract

Use official HTTPS `POST https://api.telegram.org/bot<TOKEN>/sendMessage` with JSON `chat_id`, `text`, and optional `disable_notification`. Return `ok`, `message_id` and API errors. Initial chat discovery may use `getUpdates` **once during local setup** only after owner starts bot; normal service must not poll or process Telegram updates.

Use plain text or escaped Telegram `HTML`; avoid MarkdownV2 parse errors. Keep messages comfortably below the `sendMessage` 4,096-character normal-text limit. On HTTP 429 respect Telegram `retry_after`; apply exponential backoff with bounded jitter to 5xx/timeouts, and dead-letter permanent 4xx errors. Never print the full API URL containing the token. Redact tokens, chat IDs, personally identifiable details, and full account snapshots in logs.

Official docs: <https://core.telegram.org/bots/api#sendmessage>, <https://core.telegram.org/bots/api#getupdates>, <https://core.telegram.org/bots/faq>.

### 9.2 Allowed environment variables

```dotenv
# .env.example; fill these ONLY on the device running the service.
TELEGRAM_BOT_TOKEN=REPLACE_LOCALLY
TELEGRAM_CHAT_ID=REPLACE_LOCALLY
CSP_ENV=paper
CSP_DRY_RUN=true
CSP_TZ=America/Chicago
CSP_DATABASE_URL=sqlite:///./data/csp_alerts.sqlite3
CSP_STOCKPILE_ROOT=../stockpile
CSP_DATA_PROVIDER=yahoo
CSP_DISABLE_BROKER_ACCESS=true
CSP_AI_ENABLED=false
CSP_LOG_LEVEL=INFO
# Optional later: independent AI service key in local secret store, NOT this template.
```

`CSP_DISABLE_BROKER_ACCESS` must be `true`; a `false` value is a startup validation **error**, not a way to enable brokerage integration. `CSP_ENV=paper` denotes manual paper-trade bookkeeping, not a Stockpile broker/Schwab `paper` setting.

Keep `.env`, database, raw market data, private valuation notes and logs out of version control. Use a secret manager or restricted-file owner permissions and HTTPS verification; no webhook or inbound open port is necessary for Telegram. Private bot conversations are **not end-to-end encrypted secret chats**; message only limited trade details, not account numbers or full net worth.

### 9.3 Example Telegram messages (synthetic, not live recommendations)

```text
🔎 CSP RESEARCH CANDIDATE — INDICATIVE
EXAMPLE | 2026-12-04 PUT | $75 strike | 36 DTE
1 contract | Spot $80.00 | Model Δ -0.130
Bid/Ask $0.50/$0.54 | OI 1,000 | Today vol 100
Potential premium at bid $50 | Cash collateral $7,500
Expiry break-even: $74.50 (before fees)
Exposure after fill: $16,000 / $25,000 normal cap
Source: Yahoo via Stockpile; retrieved 10:05 AM CT
Bid/ask exchange timestamp: UNAVAILABLE — VERIFY LIVE
Approved acquisition ceiling: $75 | Event review: checked
Manual action: verify the exact contract, earnings, quote and available cash.
NO ORDER SENT. Other alerts are alternatives, not approval to stack positions.
```

```text
⚠️ CSP PROFIT EXIT — MANUAL REVIEW
Trade PAPER-0007 | EXAMPLE $75 PUT 2026-12-04
Actual opening credit: $0.50/sh | 1 contract
Indicative buyback ASK: $0.19/sh
Estimated premium captured: 62% | Est. gross P&L: +$31
Rule: >=60% of premium captured
Data: indicative Yahoo quote, time 1:05 PM CT, quote timestamp unknown
Check actual closing ask at simulator/broker and record fill if acted on.
NO ORDER SENT.
```

```text
🚨 ACCOUNT RISK / ENTRIES PAUSED
Manual account snapshot: $96,600 equity
Recorded peak: $100,000 | Drawdown: 3.4%
New-entry alerts BLOCKED. Existing-position monitoring continues.
Data last manually reconciled: 2026-10-30 4:30 PM CT
Review account and record any manual response.
```

All alert examples are synthetic. The application must not insert template fixture prices in actual alerts.

---

## 10. Full example policy file

Store as `config/policy.yaml` and validate against a schema. Numbers below are starting policy values and must not be portrayed as empirically optimized.

```yaml
policy_id: csp-conservative-v1
policy_version: '1.0.0'
approved_by: owner
asset_scope: [STOCK, ETF]
option_right: PUT
trade_execution: MANUAL_ONLY
broker_connections: DISABLED
account_scope: paper_primary
account_reference_value_usd: '100000.00'

pilot:
  enabled: true
  duration_calendar_days: 90
  max_deployed_put_collateral_usd: '15000.00'
  require_manual_reconciliation_daily: true

portfolio:
  normal_min_deployed_collateral_usd: '20000.00'  # nonbinding target; DO NOT force trades
  normal_new_entry_exposure_ceiling_usd: '25000.00'
  emergency_absolute_exposure_ceiling_usd: '30000.00'
  desired_uncommitted_cash_usd: '70000.00'
  max_underlying_exposure_usd: '9000.00'
  max_sector_exposure_usd: '10000.00'
  max_correlated_bucket_usd: '10000.00'
  max_active_short_put_contracts: 4
  max_contracts_per_underlying: 1
  max_individual_company_names: 2
  max_expiry_week_collateral_usd: '15000.00'
  allow_margin: false
  allow_naked_options: false
  treat_working_and_pending_as_committed: true
  block_on_unknown_cash_or_positions: true

entries:
  one_contract_per_alert: true
  min_dte_calendar: 35
  max_dte_calendar: 45
  min_abs_delta: '0.10'
  max_abs_delta: '0.15'
  require_negative_put_delta: true
  require_strike_below_spot: true
  require_strike_at_or_below_owner_buy_limit: true
  min_open_interest: 500
  min_daily_volume: 50
  min_bid_usd_per_share: '0.20'
  max_absolute_spread_usd_per_share: '0.05'
  max_fractional_spread_of_mid: '0.10'
  max_last_trade_age_calendar_days: '1.5'
  require_positive_bid_and_ask: true
  require_standard_100_share_deliverable: true
  block_unverified_events: true
  block_earnings_through_expiration: true
  manual_event_review_max_age_calendar_days: 7
  max_alternative_alerts_per_scan: 3
  rank_basis: bid_premium_per_collateral_after_all_gates

manual_data:
  max_account_snapshot_age_hours: 30
  max_stock_mark_age_hours: 30
  require_reconciliation_after_every_trade: true
  require_daily_end_of_day_reconciliation: true
  fail_closed_for_entries: true

exits:
  profit_captured_fraction: '0.60'
  close_review_at_dte_calendar: 21
  loss_buyback_ask_multiple: '2.00'
  delta_review_abs: '0.30'
  assignment_warning_dte: 7
  use_buyback_ask_not_mid: true
  stale_quote_suppresses_price_based_alerts: true
  calendar_alerts_even_when_market_closed: true

drawdown:
  pause_new_entries_at: '0.03'
  reduce_exposure_review_at: '0.05'
  hard_halt_at: '0.08'
  hard_halt_requires_manual_reset: true

market:
  provider: yahoo_via_stockpile
  initial_quote_tier: INDICATIVE_UNTIMESTAMPED
  option_market: US_LISTED_EQUITY_ETF
  session_calendar: XNYS
  calendar_timezone: America/New_York
  scheduler_timezone: America/Chicago
  entry_scans_local: ['10:05', '13:05']
  exit_monitor_interval_minutes: 15
  no_entry_in_final_minutes_of_session: 20
  provider_retries: 2
  quote_ttl_seconds_if_timestamped: 120
  disable_cached_last_price_as_quote: true

alerts:
  telegram_private_chat_only: true
  transport: sendMessage
  default_dry_run: true
  no_inbound_trade_commands: true
  dedup_entry_by_trading_date: true
  dedup_exit_by_trade_trigger_date: true
  critical_repeat_daily_until_resolved: true
  no_trade_digest_once_per_session: true

ai:
  enabled: false
  mode: SOURCE_GROUNDED_VETO_ONLY
  on_error: MORE_RESEARCH
  can_override_risk_engine: false
  can_approve_new_underlyings: false
  can_send_orders: false
```

**Policy consistency rule:** pilot max (`$15K`) applies to **short-put collateral** as well as aggregate cap. Post-pilot normal `$25K` is a maximum, not a minimum allocation. `$30K` is never a permissible incremental trade target. All other limits apply simultaneously regardless of pilot state.

---

## 11. AI evidence reviewer (optional, separate from numerical rules)

### 11.1 Position in pipeline

Only after deterministic `PASS_RESEARCH_ONLY` or `PASS_VERIFIED_DATA`, construct a source-grounded evidence packet of:

- Owner's approval record and acceptable strike/valuation rationale.
- Normalized quote packet with source/provider and quote/retrieval timestamps.
- Earnings/event status and verified public citations; recent 10-K/10-Q or SEC XBRL metrics when pertinent.
- Cash flow, leverage, structural company risk, and sector concentration where sourced.
- Portfolio exposure, stress test results, and hard gate reasons.
- Cite URL, publisher, publication/effective date, retrieval date and a short excerpt/evidence ID for each material claim.

### 11.2 Output schema and control policy

```json
{
  "status": "PASS_REVIEW",
  "confidence_in_evidence": "MEDIUM",
  "top_risks": ["Synthetic placeholder: replace with cited actual risks"],
  "source_ids_used": ["SOURCE_ID"],
  "missing_evidence": [],
  "human_alert_summary": "Evidence-grounded summary; manual review required."
}
```

`status`: `PASS_REVIEW | MORE_RESEARCH | VETO`.

A timeout, parsing failure, ambiguous earnings date, stale filing, missing valuation source, or LLM contradiction => `MORE_RESEARCH` and **no entry alert** if AI review is enabled as a required stage. AI can never turn `BLOCKED` into PASS; cannot update approvals, positions or risk config; cannot fabricate probabilities, quotes or source URLs; cannot operate a Telegram sender, scheduler or broker. Structured data must be validated before inclusion in an alert. Store model ID/version, prompt version, context/source hashes and review decision for audit/replay.

Note: a personal ChatGPT subscription does **not** by itself run a hosted software-agent endpoint from a local server; any future LLM invocation needs a separately configured model service and applicable cost/security policy.

---

## 12. Scheduler and market sessions

### 12.1 Preferred schedule (US Central)

| Work | Time | Behavior |
|---|---|---|
| Morning entry scan | Trading days **10:05 a.m. CT** | Refresh approved names, screen and send up to 3 candidate alternatives |
| Afternoon entry scan | Trading days **1:05 p.m. CT** | Rescan; dedup same contract/day, notify changed material eligibility |
| Position monitor | Every **15 minutes** while market open | Quote-dependent profit, ask multiple and delta alerts |
| Calendar-based monitor | At least daily, including weekends/holidays | 21-DTE, 7-DTE, expiry, missing reconciliation and snapshots |
| EOD reconciliation reminder | Trading days after market close | Require manual confirmation of orders, shares, equity and cash |
| Weekly approval review | Once weekly | Owner reviews valuations, financial thesis, known earnings/events |
| Operational heartbeat | Once each trading day | Surface provider outages, scheduler failures, Telegram delivery issues |

Use `exchange_calendars` `XNYS` as a baseline for regular US equity sessions, but confirm the contract's actual options market hours for selected securities; some products have special hours. Treat early closes and holidays as authoritative; do not rely on `weekday()<5`. The authoritative NYSE calendar is <https://www.nyse.com/trade/hours-calendars>. Exchange-local ET controls session logic, and CT only controls preferred delivery time. Both have DST changes; use IANA timezone databases, not fixed UTC offsets.

Near close: block new-entry scan if remaining market time <20 minutes. Time-based exit reminders do **not** require open market. If the exchange calendar cannot determine whether a market is open, block **new entries**; still run ledger reminders.

### 12.2 Deployment choice

Recommended v1: Linux VPS or always-on local machine, Python 3.12+, `uv`, pinned Stockpile checkout, `exchange-calendars`, SQLite on persistent volume, and `systemd` timers/services with explicit timezone handling. A process manager should supervise independent entry, intraday monitor, and daily calendar tasks.

Alternative: `cron` with `CRON_TZ=America/Chicago` where supported, plus an exchange-session check in every job and `flock` to prevent overlaps. Never assume host cron implements `CRON_TZ`; test DST and replace with systemd timers or an in-process scheduler if needed. Do not expose a public HTTP service solely for Telegram—outbound HTTPS is sufficient.

Example **illustrative** cron, subject to host compatibility:

```cron
CRON_TZ=America/Chicago
5 10,13 * * 1-5 cd /opt/stockpile && flock -n /tmp/csp-scan.lock .venv/bin/python telegram-put-alerts/cli.py scan --send
*/15 9-15 * * 1-5 cd /opt/stockpile && flock -n /tmp/csp-monitor.lock .venv/bin/python telegram-put-alerts/cli.py monitor --send
15 8 * * * cd /opt/stockpile && flock -n /tmp/csp-calendar.lock .venv/bin/python telegram-put-alerts/cli.py calendar-check --send
30 15 * * 1-5 cd /opt/stockpile && flock -n /tmp/csp-reconcile.lock .venv/bin/python telegram-put-alerts/cli.py reconcile-reminder --send
```

Calendar and quote session checks inside the code are **mandatory** even with cron. Set up a local secret/environment file accessed by the service account; restrict permissions. The exact executable location must match the final project layout.

---

## 13. Alert quality, data gaps and failure handling

### 13.1 Quote failure rules

- Option chain fetch error, Yahoo rate limit, or 85% zeroed quotes: do not send stale candidates; record `DATA_ISSUE`, bound retries, and retain position risk/calendar monitoring.
- Missing option bid or ask: entry **BLOCKED** and no numerical exit based on mid/last; issue check-broker review.
- Quote timestamps absent: explicitly mark `INDICATIVE_UNTIMESTAMPED`; do not claim quotes are live. If available provider-specific quote age fails threshold, block entry.
- Wrong/missing multiplier, expiration, or option right: fail closed; no inference from ticker alone.
- Potential splits/symbol changes: disallow until owner reconciles identities.
- Failed earnings lookup: manual verified event clearance is still required; absent provider result does not establish absence of earnings.
- Model IV fit failure: do not falsely raise expected profitability; basic raw quote/risk analysis may continue with `IV_UNAVAILABLE`.

### 13.2 Storage / alert resilience

- Each job gets a `run_id`, policy-version hash, results summary and severity-coded error count.
- One concurrent writer at a time for CSV imports; SQLite transactions, `busy_timeout`, WAL, daily backups, recovery test.
- Outbox messages committed before delivery; status and message ID committed after API acknowledgment. Use finite retry queue, backoff, and dead-letter review. Handle delivery ambiguity explicitly.
- Alerts carry stable contract identifiers, position IDs, prices and UTC-local timestamps; prohibit arbitrary external content from injecting Telegram formatting.
- Logs must **not** leak Telegram bot token, user chat ID, sensitive portfolio details, raw headers, or complete private investment notes. Ensure restricted filesystem permissions.
- When the scheduler or alert sender fails, provide local health status for the operator. Do not say Telegram alerts are reliable until actual end-to-end success and uptime checks pass.
- No unattended trading logic may be introduced during retries, failure recovery or health checks.

### 13.3 Severity and notifications

`INFO` for research candidates and daily no-trade digest; `WARN` for data/ledger issues; `ACTION` for profit and 21-DTE exits; `URGENT` for delta/loss/assignment or 5% drawdown; `CRITICAL` for 8% halt and data corruption. User should be able to silence `INFO` while preserving higher-level notices. Handle repeated `URGENT` conditions without flooding the chat; include a unique alert ID and manual check requirement.

---

## 14. CLI and operator interfaces

CLI is the primary v1 interface; a local, password-protected read-only dashboard is optional later. **No trade execution command may exist.** Proposed commands:

```bash
# Runs against manual local data; no Telegram calls unless --send is explicit.
python telegram-put-alerts/cli.py doctor
python telegram-put-alerts/cli.py policy-check
python telegram-put-alerts/cli.py account-check
python telegram-put-alerts/cli.py approved-list
python telegram-put-alerts/cli.py scan --dry-run
python telegram-put-alerts/cli.py monitor --dry-run
python telegram-put-alerts/cli.py calendar-check --dry-run
python telegram-put-alerts/cli.py reconcile-check
python telegram-put-alerts/cli.py journal import-csv data/manual-trades.csv --dry-run
python telegram-put-alerts/cli.py journal export-csv --output data/exports/journal.csv
python telegram-put-alerts/cli.py alert-preview --fixture tests/fixtures/entry.json
python telegram-put-alerts/cli.py telegram-test --send  # outbound test only
python telegram-put-alerts/cli.py scan --send
python telegram-put-alerts/cli.py monitor --send
python telegram-put-alerts/cli.py report --period 90d
```

`--send` means **send Telegram messages only**. Dry-run is default for newly deployed code and first setup. No `--live-trade`, `--place`, `--close`, `--roll`, or `--broker-sync` flag. Manual journal updates should use an audited local CLI or CSV import, not a Telegram command. Document actual syntax in `README.md` and keep aligned with tests.

### 14.1 Filesystem layout

```text
stockpile/
  options-scanner/                   # upstream untouched, pinned
  shared/                            # upstream untouched
  telegram-put-alerts/
    README.md
    SECURITY.md
    RUNBOOK.md
    DATA_DICTIONARY.md
    pyproject.toml
    .env.example
    cli.py
    config/
      policy.yaml
      policy.schema.json
      approved.example.json          # synthetic examples only
    src/csp_alerts/
      market/{stockpile.py,normalize.py,calendar.py,models.py}
      policy/{config.py,entry.py,exits.py,portfolio.py,drawdown.py}
      journal/{store.py,models.py,import_csv.py,reconcile.py,migrations/}
      alerts/{policy.py,telegram.py,outbox.py,templates.py}
      ai/{reviewer.py,evidence.py,schemas.py}
      jobs/{scan.py,monitor.py,calendar_check.py,reconcile.py}
      reporting/{performance.py,stress.py}
    tests/
      fixtures/{synthetic_chains/,journals/,responses/}
      unit/
      integration/
      e2e/
      security/
    data/                            # gitignored database, snapshots, exports
    ops/{systemd/,cron.example,backup.sh,restore-test.sh}
```

The prior `telegram-put-alerts` starter archive contains smaller `app.py`, `engine.py`, `notifier.py`, `stockpile_adapter.py`, CSV templates, and offline unit tests. It is useful as a **prototype input**. Refactor/port rather than assuming it fulfills the canonical database, verified event clearance, robust outbox, exact reconciliation, portfolio stress, or AI-review stages.

---

## 15. Test and validation plan

### 15.1 Offline unit tests — minimum coverage

1. Normal one-contract `$85` put reserves `$8,500`, premium bid `$0.45` yields `$45` indicative credit and `$84.55` gross expiry break-even; standard 100-share multiplier used only if verified.
2. Strike `>= $90.01` fails `$9,000` per-underlying cap for one standard 100-share put; `$100` strike is blocked.
3. `$25,000` normal total cap blocks a new put even though the `$30,000` emergency threshold has room.
4. `$15,000` pilot cap blocks a second large contract; pilot disabled explicitly in policy transition.
5. Same ticker existing working or open put blocks another; aggregate exposure includes pending and close-working orders.
6. Owned stock exposure counts toward same ticker and sector caps; an assigned put reconciles once and does not double count its converted shares.
7. Company-name count max two; cap four total active contracts and one per underlying; ISO expiry-week cap `$15K`.
8. Sector and correlation-bucket overlap; ETF may not evade concentration check by using a generic sector label.
9. Delta `-0.10` and `-0.15` pass boundaries; `-0.16`, `+0.13`, NaN or missing delta blocked.
10. DTE 35 and 45 pass; 34 or 46 blocked; expiry date/daylight-saving boundaries tested.
11. OI 499 and volume 49 blocked; zero-volume after market close **never relaxed**.
12. Spread `$0.06` absolute blocked even if fractional ratio acceptable; 11% midpoint spread blocked even if dollar spread acceptable.
13. Missing/zero bid or ask, stale/missing last-print or unknown contract multiplier blocked; upstream fallback midpoint from last trade must not pass.
14. Owner acquisition strike, approval expiry, stale review, unknown earnings and corporate-action coverage all block entry correctly.
15. Missing/unreconciled account snapshot, insufficient true free cash, unmarked owned stock, and manual assignment backlog block entry.
16. Recorded peak equity `$100K` and equity `$96,900` pause entries (3.1% drawdown); `$94,900` escalates 5%; `$91,900` hard-halts 8% and requires manual release.
17. Profit exit: actual credit `$0.50`, ask `$0.20` triggers exactly 60%; ask `$0.21` does not. Multiple contracts and fees correctly handled.
18. Risk review: opening credit `$0.50`, ask `$1.00` triggers 2x; abs delta `0.30` triggers. Neither uses midpoint or stale last.
19. Time exit at 21 DTE triggers even with provider outage, market closure or invalid current quote; `DATA_ISSUE` separately emitted.
20. Pending close still reserves collateral until actual manual confirmed closing fill; expired and assigned positions require confirmation.
21. Duplicate entry/exit alerts dedup within policy window; crossing 5% drawdown immediately escalates after 3% alert.
22. Input payload with invalid/hostile Telegram markup is escaped; secret redaction in exceptions.

### 15.2 Integration tests (mocks, no real trades)

- Stockpile baseline `fetch_chain` mocked with normal puts, stale/zero Yahoo chains, missing earnings and malformed fields; typed adapter produces canonical observations.
- Demonstrate CLI JSON is not used as a substitute for missing `volume`/last-trade print.
- No `stockpile.trade_actions` or `schwab.*` imports during entry/monitor calls; monkeypatch all potential broker network paths to raise, verify zero calls.
- Telegram mock server: 200 acknowledged, timeout/unknown ACK, HTTP 429 `retry_after`, 401/403 permanent failure, 500 retried. Verify outbox persistence and no secrets in logs.
- Concurrent scanner/monitor operations cannot double-allocate collateral or overwrite journal status.
- Exchange holidays, early closes, DST spring/fall, early-market scans, missing calendar dataset.
- Journal CSV import with duplicate, negative, missing, malformed and contradictory statuses fails cleanly or quarantines issues.

### 15.3 End-to-end acceptance (no brokerage)

- Fresh install with empty approved list -> no candidates and a useful `NO_TRADE` explanation.
- Seed synthetic whitelist/ledger -> run scanner with `--dry-run`; inspect exact formatted alert; **no Telegram network request**.
- Configure temporary private test bot -> send test; observe private Telegram message; verify token hidden from logs.
- Manually add simulated fill to journal -> monitor triggers synthetic 60% target and 21-DTE alerts; changing journal to `CLOSED` stops open-position alerts.
- Simulate old account snapshot -> scanner sends no new candidate; exit time reminder still fires.
- Simulate bad options chain -> `DATA_ISSUE` not false `NO_TRADE`.
- Capture scheduler operation across a regular session and holiday; check scan counts, duplicate suppression, retries, and reconciliation reminders.
- Audit static imports and runtime network destinations for **zero brokerage connections and zero trade APIs**.

### 15.4 Historical research validation (separate from implementation)

For any claims about profitability, historical testing must include actual timestamped historical option chains or defensible reconstructed data, realistic bid/ask spread, brokerage fees, tax assumptions only if supplied, close at 60% credit or 21 DTE, stop-review triggers, assignment/corporate actions, slow crashes, sharp gap-down days, earnings avoidance, concentration constraints, and realistic capital sitting idle. Compare after-cost results to Treasury bills and/or a chosen passive benchmark. Hold out years and out-of-sample instruments; avoid tuning on the same performance window. Stockpile's GBM Monte Carlo is a separate risk diagnostic, **not** a substitute.

### 15.5 Paper pilot

- 90 calendar days minimum; reference account `$100,000`, **max `$15,000` put collateral** until explicit pilot exit and validation.
- Record every alerted and declined candidate, quote time, reason, actual simulator execution, slippage, fees, close decisions and differences between simulator and hypothetical bid/ask.
- Daily manual journal and equity/cash reconciliation; weekly operational report; no live order connectivity.
- Review results across broader volatility regimes before making claims about long-run profitability; three months alone is insufficient to establish edge.

---

## 16. Implementation work plan (sequenced tickets)

| Ticket | Deliverable | Must pass before next stage |
|---|---|---|
| P0-01 | Create isolated subproject, pin Stockpile SHA, document no-broker boundary | Dependency/network audit passes |
| P0-02 | Typed policy validation and empty owner whitelist | Invalid/unknown configs reject at startup |
| P0-03 | SQLite ledger, migrations, CSV import/export, manual transitions and reconciliation | Unit tests for pending/assignment/working close |
| P0-04 | Pure collateral/concentration/drawdown/entry rules | All deterministic gate tests pass |
| P0-05 | Stockpile raw-chain adapter, provenance, normalization, earnings checks | Stale/invalid Yahoo data tests; no CLI-field assumptions |
| P0-06 | Entry scan, ranking, no-trade digest and local preview | Dry-run makes zero external alert calls |
| P0-07 | Exit/calendar/portfolio monitor, severity and dedup | Profit/time/loss/delta/quote-failure cases pass |
| P0-08 | Telegram private delivery, secret handling, durable outbox | Mock transport tests then owner-approved real test bot |
| P1-01 | Timezone/exchange-calendar scheduling, locks, logs and health | DST, holiday and early-close tests |
| P1-02 | Full runbook, operations, backup/restore and smoke tests | Reproducible clean install and end-to-end dry run |
| P1-03 | 90-day paper pilot and reporting | No brokerage access; audited manual records |
| P2-01 | Optional citations-based AI reviewer with veto-only boundary | Evidence tests, structured output, no override |
| P2-02 | Licensed read-only quote provider with trustworthy quote timestamps | Source/quote-delay and licensing verified |
| P2-03 | Historical option-chain backtest and portfolio stress analysis | Out-of-sample/fees/slippage/assignment coverage |

**Phase gate:** Do not begin real-money decision support until P0/P1 tests pass, the owner has supplied complete and regularly updated manual records, source-quality limitations are visible, and a paper pilot has been evaluated. The system still does **not** place orders after the gate.

---

## 17. Definition of done / release criteria

### Required for v1 release

- [ ] Service can be installed and run from a clean checkout using documented Python 3.12+/`uv` commands.
- [ ] Whitelist starts empty, requires owner approval; no fabricated live examples or assumed acceptable companies.
- [ ] Policy file validates every threshold and cannot be changed by untrusted AI or Telegram messages.
- [ ] Deterministic, well-tested account risk engine enforces **every** stated exposure, concentration, cash, earnings, delta, DTE and liquidity constraint.
- [ ] Pilot cap `$15K`; ongoing new-entry cap `$25K`; emergency `$30K` is warning only.
- [ ] Missing, stale or unreconciled manual portfolio data blocks entry candidate notifications.
- [ ] Adapter uses Stockpile read-only chain and earnings paths, not the lossy CLI candidate JSON; no broker SDK is invoked.
- [ ] Bid/ask and model-derived delta are accurately labeled; `fetched_at` never presented as an exchange quote timestamp.
- [ ] Manual journal supports opening, pending, closing, assignment, expiry, fees, and owned shares; proper state transitions and daily reconciliation.
- [ ] Exit alerts use current ask for P&L, remain independent from entry-scanning window, and issue 21-DTE reminders despite price outages.
- [ ] Exchange calendar, DST, early close, failure and retry tests pass.
- [ ] Telegram Bot API messages successfully deliver to **owner-authorized private chat**; no inbound trading actions.
- [ ] Outbox deduplicates reasonable repeats without suppressing new dangerous conditions.
- [ ] Secrets are stored outside git and redacted from logs; execution/network audit confirms **no broker endpoints**.
- [ ] Dry-run is the default, with a documented test message and end-to-end setup check.
- [ ] Operator runbook documents outages, token rotation, data gaps, journal updates, assignment handling and backup/restore.
- [ ] No profitability or risk-reduction claim is made without clearly scoped historical evidence.

### Not part of initial release

- [ ] Autonomous trading or brokerage integration (**explicitly prohibited**, not a future default).
- [ ] Automatically approving stocks, overriding buy limits, or deciding to roll positions.
- [ ] Treating Yahoo option quotes or Black–Scholes delta as guaranteed real-time data.
- [ ] Claiming that paperMoney perfectly models early assignment, fills, liquidity or dividend/corporate-event processing.

---

## 18. First-run manual setup checklist

1. Choose a secure always-on device or server; install Python >=3.12 and `uv`.
2. Clone Stockpile and pin the reviewed SHA; review upstream CC BY-NC license for personal/noncommercial use and any different contemplated usage.
3. Install the new isolated alert service and required exchange calendar dependencies.
4. Create a private Telegram bot via [`@BotFather`](https://t.me/BotFather), open `/start` in a private chat, and set token/chat ID in local environment (never share token with an AI agent or place it in a prompt).
5. Run `doctor`, `policy-check` and `telegram-test` locally; confirm no external message on dry-run.
6. Approve an initial 10–15-name whitelist **only after independent fundamental/valuation review**, including current owner-approved maximum acquisition price and dated event clearance. Zero approvals is a valid safe state.
7. Record starting paper account equity `$100,000` and genuinely free collateral cash, existing simulated stocks/puts and working orders, if any; confirm reconciliation.
8. Run entry scan in local preview; compare every quoted contract with simulator option chain and spot, note differences and missing timestamps.
9. Configure and verify scheduler; enable **Telegram alert sends only** after the private test passes.
10. Manually open paper contracts if you choose, record actual paper fills, and confirm subsequent profit/time/risk warnings. Never open more than capital rules permit.
11. Monitor for at least 90 days, reconcile daily, and measure false alerts, missing data, spread/slippage and drawdowns.

---

## 19. Sources and provenance

- Stockpile repository: <https://github.com/medloh/stockpile>
- Pinned commit: <https://github.com/medloh/stockpile/commit/34bf6cf8101e54620ea1e2bda940f0a3aa88e791>
- Stockpile raw options chain: <https://github.com/medloh/stockpile/blob/main/options-scanner/options_scanner/chain.py>
- Stockpile normalizer: <https://github.com/medloh/stockpile/blob/main/options-scanner/options_scanner/chain_common.py>
- Stockpile CLI and JSON caveats: <https://github.com/medloh/stockpile/blob/main/options-scanner/options_scanner/main.py>
- Stockpile earnings helper: <https://github.com/medloh/stockpile/blob/main/options-scanner/options_scanner/earnings.py>
- Stockpile Monte Carlo assumptions: <https://github.com/medloh/stockpile/blob/main/options-scanner/MONTE_CARLO.md>
- Stockpile volatility interpretation: <https://github.com/medloh/stockpile/blob/main/options-scanner/INTERPRETING_IV.md>
- Telegram Bot API: <https://core.telegram.org/bots/api>
- NYSE calendars and market hours: <https://www.nyse.com/trade/hours-calendars>
- `exchange_calendars` Python project: <https://github.com/gerrymanoim/exchange_calendars>

**Source-review boundary:** This specification is based on repository content reviewed on 2026-10-30 and on an existing local prototype/starter. The implementing agent must run and validate against actual pinned source and tests in its own environment. No live broker account, market-data subscription, Telegram bot, scheduler or production deployment has been set up by this document.

---

## 20. Final security contract (must remain testable)

```text
THE APP MAY: request public or approved non-broker market data; read manually
entered records; calculate and explain; save local records; send advisory
Telegram notifications to one owner-approved private chat.

THE APP MUST NOT: authenticate to any brokerage; read broker account balances;
load trading credentials; build or submit exchange/broker orders; trade on
behalf of user; take trade instructions from Telegram; select an unapproved
underlying; change risk limits on its own; treat model output as a risk gate
override; imply a notification limits potential trading losses.

EVERY TRADE, FILL, CANCELLATION, ASSIGNMENT AND POSITION UPDATE IS ENTERED OR
CONFIRMED BY THE HUMAN OWNER. IF THAT MANUAL RECORD IS UNRELIABLE, NEW-ENTRY
ALERTS MUST STOP; EXIT AND DATA WARNINGS MUST CONTINUE WHERE POSSIBLE.
```
