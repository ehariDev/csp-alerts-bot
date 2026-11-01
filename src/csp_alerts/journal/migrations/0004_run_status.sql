-- F1/F6: every scheduled run ends in exactly one visible status.
CREATE TABLE run_status (
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- SCAN | MONITOR | HEARTBEAT | RECONCILE
  started_at_utc TEXT NOT NULL,
  status TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  summary_json TEXT NOT NULL
);
CREATE INDEX idx_run_status_kind_time ON run_status(kind, started_at_utc);
