# CSP Alerts — visual workflow

Required target workflow; not a claim of completed implementation. Read the [owner workflow](CSP_ALERTS_WORKFLOW.md) and [product requirements](PRODUCT_WORKFLOW_REQUIREMENTS.md).

```mermaid
flowchart TD
    A[Open web page] --> B[Choose tickers to track]
    B --> C[Backend updates stock prices and put quotes]
    C --> D{Data usable?}
    D -->|No| E[Show unavailable or stale status
Keep applicable time and risk warnings]
    E --> C
    D -->|Yes| F[Show stock price and put chain
Source, timestamps and delay status]
    F --> G[Evaluate ownership approval and portfolio limits]
    G --> H{All entry gates pass?}
    H -->|No| I[Show blocked reasons and capacity
Suppress new-entry alerts; keep prices updating]
    H -->|Yes| J[Show eligible contracts
Send enabled entry alerts]
    I -.->|Continue updates| C
    J --> K[Owner places trade externally]
    K --> L[Record order or actual fill in web page
Preview and confirm]
    L --> M{Fill confirmed?}
    M -->|No| N[WORKING order
Reserve collateral; await fill or cancellation]
    N --> L
    M -->|Yes| O[OPEN position
Monitor exact contract automatically]
    O --> P[Update quote, P&L and risk warnings
Send enabled position alerts]
    P --> Q{Owner action?}
    Q -->|Keep holding| O
    Q -->|Closing order| R[CLOSE_WORKING
Keep collateral and monitoring]
    R --> S[Owner confirms actual close fill in web page]
    S --> T[CLOSED
Stop obsolete open-position alerts]
    Q -->|Assignment or unsupported event| U[Keep obligation visible
Block new entries pending reconciliation]
    T --> V[Web check-in and reconciliation]
    U --> V
    L --> W[Recalculate portfolio exposure
Ticker, sector, expiry, contracts and cash]
    W --> G
    V --> W
```

- Market-data updates and existing-position monitoring continue when entry gates block new opportunities. Watching a ticker does not approve it for entry alerts.
- “Usable” follows the documented evidence policy; recently retrieved indicative data is not guaranteed live. Show stock and option timestamps separately.
- Owner execution occurs outside the app. Web actions record reality, never submit orders. Recorded trades remain visible even if they breach limits.
- Reserve collateral for pending/working/open/close-working obligations; reconcile before treating released capacity as verified. Cancellation of an unfilled order is recorded and reconciled through the same web workflow.
- Portfolio limits retain $15K pilot collateral, $9K per ticker, $10K per sector, four active contracts and every other existing gate. Never suppress existing-position risk warnings just because entries are blocked.
- Backend jobs continue with the browser closed while the PC and jobs are running. Telegram delivery requires explicit enablement. Final exit thresholds remain an owner decision.

Acceptance remains pending under WT-AT-01..10 and MD-AT-01..06. [Current status](OPEN_ISSUES.md).
