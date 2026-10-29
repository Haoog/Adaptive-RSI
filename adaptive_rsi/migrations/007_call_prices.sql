ALTER TABLE calls ADD COLUMN input_price REAL;
ALTER TABLE calls ADD COLUMN output_price REAL;
ALTER TABLE calls ADD COLUMN cache_read_price REAL;
ALTER TABLE calls ADD COLUMN cache_write_price REAL;
ALTER TABLE calls ADD COLUMN price_basis TEXT;
CREATE INDEX IF NOT EXISTS calls_history ON calls(project,role,model,created DESC);
CREATE INDEX IF NOT EXISTS events_run_kind ON events(run,kind);
