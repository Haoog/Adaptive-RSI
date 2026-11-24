ALTER TABLE tasks ADD COLUMN cooldown_waits INTEGER NOT NULL DEFAULT 0;
ALTER TABLE calls ADD COLUMN remote_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN origin TEXT;
CREATE INDEX IF NOT EXISTS stage_recovery_events ON events(run, seq)
    WHERE kind IN ('stage_reused','stage_invalidated','stage_started');
