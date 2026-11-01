-- Persisted deterministic gate decisions per scan run (spec 7.1 persist_evaluation).
CREATE TABLE entry_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  observation_id TEXT,
  contract_key TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason_codes_json TEXT NOT NULL,
  decision_json TEXT NOT NULL,
  evaluated_at_utc TEXT NOT NULL
);
CREATE INDEX idx_entry_evaluations_run ON entry_evaluations(run_id);
