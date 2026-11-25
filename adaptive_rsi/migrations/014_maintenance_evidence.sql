CREATE TABLE IF NOT EXISTS inspection_observations (
    task TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    signal TEXT NOT NULL,
    data TEXT NOT NULL,
    observed REAL NOT NULL,
    PRIMARY KEY(task, signal)
);
CREATE INDEX IF NOT EXISTS inspection_observations_recent ON inspection_observations(task, observed DESC);
CREATE TABLE IF NOT EXISTS maintenance_reviews (
    project TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    data TEXT NOT NULL,
    reviewed REAL NOT NULL,
    PRIMARY KEY(project, sha)
);
CREATE TABLE IF NOT EXISTS maintenance_rules (
    project TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data TEXT NOT NULL,
    confirmed REAL NOT NULL,
    PRIMARY KEY(project, id)
);
