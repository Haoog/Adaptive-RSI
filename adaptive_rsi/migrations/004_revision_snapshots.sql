CREATE TABLE task_revision_snapshots (
    revision INTEGER PRIMARY KEY REFERENCES task_revisions(id) ON DELETE CASCADE,
    after_body TEXT NOT NULL,
    impact TEXT NOT NULL
);
