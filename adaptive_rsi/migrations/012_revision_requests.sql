CREATE TABLE revision_requests (
    request TEXT PRIMARY KEY NOT NULL,
    task TEXT NOT NULL REFERENCES tasks(id),
    signature TEXT NOT NULL,
    revision INTEGER NOT NULL,
    response TEXT NOT NULL
);
