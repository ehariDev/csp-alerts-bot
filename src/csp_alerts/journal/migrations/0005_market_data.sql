-- Stage A: owner watchlist + persisted market-data observations + refresh run/request records.
-- Purely additive. No existing table is altered; ledger, snapshots, drawdown, reconciliation and outbox are untouched.
-- Money/price values are quoted decimal strings (TEXT). Timestamps are UTC ISO 'YYYY-MM-DDTHH:MM:SSZ'.

-- Owner-selected symbols to track for prices. Distinct from approved_underlyings: tracking never approves anything.
CREATE TABLE watchlist (
  symbol TEXT PRIMARY KEY,
  added_at_utc TEXT NOT NULL,
  added_by TEXT NOT NULL
);

-- Per-symbol fetch status (one row per symbol that was ever attempted).
CREATE TABLE md_symbol_status (
  symbol TEXT PRIMARY KEY,
  status TEXT NOT NULL,                 -- OK | PARTIAL | RATE_LIMITED | ERROR | MARKET_CLOSED
  last_attempt_at_utc TEXT,
  last_success_at_utc TEXT,
  last_error TEXT,                      -- sanitised provider error text, never a secret
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  next_attempt_after_utc TEXT,          -- backoff after a rate limit / error
  session_at_attempt TEXT,              -- OPEN | CLOSED | UNKNOWN at the attempt
  last_run_id TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

-- Latest stock price observation per symbol. A failed fetch never touches this row (kept with ORIGINAL timestamps).
CREATE TABLE md_stock_latest (
  symbol TEXT PRIMARY KEY,
  price TEXT NOT NULL,
  price_type TEXT NOT NULL,             -- LAST | BID | ASK | PREVIOUS_CLOSE (as labelled for the provider path)
  provider TEXT NOT NULL,
  tier TEXT NOT NULL,
  market_time_utc TEXT,                 -- provider market timestamp; NULL = unknown (never inferred)
  retrieved_at_utc TEXT NOT NULL,
  source TEXT NOT NULL,                 -- REFRESH | MONITOR
  run_id TEXT NOT NULL,
  raw_sha256 TEXT
);

CREATE TABLE md_stock_history (
  obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  price TEXT NOT NULL,
  price_type TEXT NOT NULL,
  provider TEXT NOT NULL,
  tier TEXT NOT NULL,
  market_time_utc TEXT,
  retrieved_at_utc TEXT NOT NULL,
  source TEXT NOT NULL,
  run_id TEXT NOT NULL
);
CREATE INDEX idx_md_stock_history_symbol ON md_stock_history(symbol, obs_id);

-- Latest option quote per exact contract. Separate from the stock price: a stock price is never an option quote.
CREATE TABLE md_option_latest (
  symbol TEXT NOT NULL,
  expiration TEXT NOT NULL,
  option_right TEXT NOT NULL,           -- P
  strike TEXT NOT NULL,                 -- canonical plain decimal string
  occ_symbol TEXT NOT NULL,
  occ_source TEXT NOT NULL,             -- PROVIDER | DERIVED (derived from root+expiry+right+strike; not proof of deliverable)
  bid TEXT, ask TEXT, last TEXT,
  volume INTEGER, open_interest INTEGER,
  delta TEXT, implied_volatility TEXT, spot TEXT,
  last_trade_at_utc TEXT,               -- the option's last trade print time (NOT a quote time)
  quote_time_utc TEXT,                  -- provider quote timestamp; NULL = unknown
  provider TEXT NOT NULL,
  tier TEXT NOT NULL,
  retrieved_at_utc TEXT NOT NULL,
  source TEXT NOT NULL,                 -- REFRESH | MONITOR
  run_id TEXT NOT NULL,
  raw_sha256 TEXT,
  identity_note TEXT,
  PRIMARY KEY (symbol, expiration, option_right, strike)
);
CREATE INDEX idx_md_option_latest_exp ON md_option_latest(symbol, expiration);

-- Bounded recent history: a row is added only when bid/ask/volume/open interest/delta changed.
CREATE TABLE md_option_history (
  obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  expiration TEXT NOT NULL,
  option_right TEXT NOT NULL,
  strike TEXT NOT NULL,
  occ_symbol TEXT NOT NULL,
  bid TEXT, ask TEXT, last TEXT,
  volume INTEGER, open_interest INTEGER,
  delta TEXT,
  provider TEXT NOT NULL,
  retrieved_at_utc TEXT NOT NULL,
  source TEXT NOT NULL,
  run_id TEXT NOT NULL
);
CREATE INDEX idx_md_option_history_contract ON md_option_history(symbol, expiration, option_right, strike, obs_id);

-- One row per market-refresh execution (a run record; not a ledger event).
CREATE TABLE md_refresh_runs (
  run_id TEXT PRIMARY KEY,
  started_at_utc TEXT NOT NULL,
  finished_at_utc TEXT,
  trigger TEXT NOT NULL,                -- SCHEDULED | FORCED | REQUEST
  session_status TEXT NOT NULL,
  status TEXT NOT NULL,                 -- OK | PARTIAL | RATE_LIMITED | ERROR | IDLE | SERVICE_FAILURE
  symbols_json TEXT NOT NULL,
  summary_json TEXT NOT NULL
);
CREATE INDEX idx_md_refresh_runs_time ON md_refresh_runs(started_at_utc);

-- Owner "refresh now" requests written by the web page; picked up by the next market-refresh run.
CREATE TABLE md_refresh_requests (
  request_id TEXT PRIMARY KEY,
  requested_at_utc TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  scope TEXT NOT NULL,                  -- ALL or one symbol
  status TEXT NOT NULL,                 -- PENDING | DONE | EXPIRED
  picked_up_at_utc TEXT,
  run_id TEXT,
  note TEXT
);
CREATE INDEX idx_md_refresh_requests_time ON md_refresh_requests(requested_at_utc);
