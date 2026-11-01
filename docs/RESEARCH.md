# Prior-art research: cash-secured put (CSP) Telegram alerts

Researched 2026-10-30. Goal: reuse what works, do not reinvent. **Status: from web search snippets only; no repo has been cloned or run yet.** Every item below needs a hands-on evaluation (license, last commit, does it run, data source) before we depend on it. The implementer does this in Phase 0.

## Candidate repos

| Repo | What it is | Fit | Notes / risks |
|---|---|---|---|
| [liuxie066/options-monitor](https://github.com/liuxie066/options-monitor) | Local Python (3.12+, MIT) tool: Sell Put / Covered Call candidate screening, position tracking, `om daily-brief` notifications. Advisory only, no auto-orders. | **Closest match.** Evaluate first: fork/wrap vs. use as a library. | README in Chinese. Telegram support unverified. Data source / API key requirements unknown. |
| [brndnmtthws/thetagang](https://github.com/brndnmtthws/thetagang) | IBKR bot running the Wheel (sells puts/calls). On PyPI as `thetagang`. | Strike/DTE/delta selection logic worth borrowing. | Executes trades and needs IBKR; we only want alerts. No Telegram mentioned. |
| tasty-wheel (Alpaca CSP wheel engine) | Automated CSP wheel via Alpaca API. | Reference only. | Executes trades. Not verified. |
| [thunderalgo Nasdaq-100 puts screener](https://thunderalgo.substack.com/p/building-a-nasdaq-100-puts-screener) | Blog post, Python + Polygon. | Scoring ideas. | Not clearly open source; Polygon key. |
| [TradingView "Cash secured put finder"](https://my.tradingview.com/script/HeYwhZvQ-Cash-secured-put-finder) | Pine Script indicator. | Logic reference only. | Not Python. |
| PutFinder | Hosted screener (pandas/numpy/scipy). | Scoring reference only. | Not open source. |

## Claude skills / MCP servers

| Item | Use for | Notes |
|---|---|---|
| [himself65/trade-skills](https://github.com/himself65/trade-skills) | General options-strategy assistant skill (MIT). | Install: `npx skills add himself65/trade-skills`. CSP coverage unconfirmed. |
| [staskh/trading_skills](https://github.com/staskh/trading_skills) | `option-chain`, spread analysis, IB short/roll skills. | `npx -y skills add staskh/trading_skills --skill option-chain --agent claude-code`. IB-focused. |
| [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) | Broad trading skill collection. | Check for earnings/volatility skills useful as filters. |
| [GreeksSurge CSP research](https://www.agensi.io/skills/greekssurge-cash-secured-put-research) / `greekssurge-mcp` | CSP research MCP (MIT, npm). | Directly on-topic; verify data source and terms. |
| [Stig-Johnny/telegram-mcp-server](https://lobehub.com/mcp/stig-johnny-telegram-mcp-server), [parthjadhav/telegram-notify-mcp](https://claudemarketplaces.com/mcp/parthjadhav/telegram-notify-mcp) | Telegram from Claude sessions. | Dev-time notifications only. The production bot should call the Telegram Bot API directly, not depend on a Claude session. |

## Building blocks (not yet searched in detail)

Candidates to confirm: `yfinance` (free, delayed, flaky chains), `python-telegram-bot` or plain `httpx` against the Bot API, `APScheduler` for scheduling, `pandas-market-calendars` for trading days. Paid-data alternatives if yfinance is inadequate: Polygon, Tradier, Alpaca options data.

## Open questions for the user

1. Alert trigger: scheduled digest (e.g. daily pre-market top-N) vs. real-time threshold alerts vs. both?
2. Watchlist: fixed tickers, index constituents, or a screener-generated universe?
3. Entry criteria: delta range, DTE range, min annualized yield, min open interest, earnings blackout, IV rank floor?
4. Data source: free (yfinance) or paid (Polygon/Tradier/IBKR)?
5. Position awareness: track open CSPs and alert on roll / assignment risk, or new-entry alerts only?
6. Hosting: local machine, VPS, or GitHub Actions cron?

## Decision (pending Phase 0)

Preferred path: wrap or fork whichever candidate passes evaluation (likely options-monitor), add a thin Telegram notifier, and borrow selection logic from thetagang. Write our own code only for the gaps.
