-- Spec 5.1 tables. Decimal values are stored as TEXT. Timestamps are UTC ISO-8601 'Z' strings.
CREATE TABLE policy_versions (
  policy_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  config_json TEXT NOT NULL,
  PRIMARY KEY (policy_id, sha256)
);

CREATE TABLE approved_underlyings (
  ticker TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL CHECK (asset_type IN ('STOCK','ETF')),
  sector TEXT NOT NULL,
  correlation_bucket TEXT NOT NULL,
  max_buy_strike TEXT NOT NULL,
  approved_through TEXT NOT NULL,
  events_reviewed_on TEXT NOT NULL,
  events_cleared_through TEXT NOT NULL,
  ownership_reason TEXT NOT NULL,
  approved_by TEXT NOT NULL CHECK (approved_by = 'owner'),
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE option_positions (
  trade_id TEXT PRIMARY KEY,
  account_scope TEXT NOT NULL,
  paper INTEGER NOT NULL CHECK (paper IN (0,1)),
  ticker TEXT NOT NULL,
  option_right TEXT NOT NULL CHECK (option_right = 'P'),
  expiration TEXT NOT NULL,
  strike TEXT NOT NULL,
  multiplier INTEGER NOT NULL CHECK (multiplier > 0),
  contracts INTEGER NOT NULL CHECK (contracts > 0),
  sector TEXT NOT NULL,
  correlation_bucket TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'DRAFT','PENDING','WORKING','OPEN','CLOSE_WORKING','CLOSED','CANCELED','EXPIRED',
    'ASSIGNED_PENDING_RECONCILIATION','ASSIGNED_RECONCILED','ADJUSTMENT_REVIEW')),
  opened_at_utc TEXT,
  actual_open_credit_per_share TEXT,
  actual_close_debit_per_share TEXT,
  fees_usd TEXT NOT NULL DEFAULT '0',
  closed_at_utc TEXT,
  parent_event_id TEXT,
  last_reconciled_at_utc TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_option_positions_state ON option_positions(state);
CREATE INDEX idx_option_positions_ticker ON option_positions(ticker);

CREATE TABLE stock_lots (
  lot_id TEXT PRIMARY KEY,
  ticker TEXT NOT NULL,
  shares TEXT NOT NULL,
  reference_price TEXT NOT NULL,
  reference_asof_utc TEXT NOT NULL,
  cost_basis_reference_usd TEXT,
  acquisition_kind TEXT NOT NULL,
  linked_trade_id TEXT REFERENCES option_positions(trade_id),
  sector TEXT NOT NULL,
  correlation_bucket TEXT NOT NULL
);
-- A put can convert into at most one lot (no double counting after assignment).
CREATE UNIQUE INDEX uq_stock_lots_linked_trade ON stock_lots(linked_trade_id) WHERE linked_trade_id IS NOT NULL;

CREATE TABLE account_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  asof_utc TEXT NOT NULL UNIQUE,
  equity_usd TEXT NOT NULL,
  free_unencumbered_cash_usd TEXT NOT NULL,
  manual_confirmed INTEGER NOT NULL CHECK (manual_confirmed IN (0,1)),
  reconciliation_version TEXT NOT NULL
);

CREATE TABLE market_observations (
  observation_id TEXT PRIMARY KEY,
  contract_key TEXT NOT NULL,
  retrieved_at_utc TEXT NOT NULL,
  quote_at_utc TEXT,
  provider TEXT NOT NULL,
  tier TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  quote_json TEXT NOT NULL
);

CREATE TABLE alert_outbox (
  alert_id TEXT PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,
  class TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  next_attempt_at_utc TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  telegram_message_id TEXT,
  content_text TEXT NOT NULL,
  evidence_json TEXT NOT NULL
);

CREATE TABLE audit_log (
  event_id TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  source TEXT NOT NULL
);
CREATE INDEX idx_audit_log_type_time ON audit_log(event_type, created_at_utc);
-- Audit log is append-only.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
