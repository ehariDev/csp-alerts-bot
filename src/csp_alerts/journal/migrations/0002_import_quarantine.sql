-- Rows rejected by CSV import are kept here; they are never silently corrected.
CREATE TABLE import_quarantine (
  quarantine_id TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  source TEXT NOT NULL,
  row_number INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  errors_json TEXT NOT NULL
);
