CREATE TABLE IF NOT EXISTS failure_monitor_outbox (
    run TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    attributable INTEGER NOT NULL CHECK (attributable IN (0, 1)),
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS failure_monitor_outbox_order ON failure_monitor_outbox(created, run);
