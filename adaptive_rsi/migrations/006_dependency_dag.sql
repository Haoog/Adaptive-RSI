ALTER TABLE task_dependencies RENAME TO legacy_task_dependencies;
CREATE TABLE task_dependencies (
    task TEXT NOT NULL REFERENCES tasks(id),
    dependency TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY(task,dependency),
    CHECK(task<>dependency)
);
INSERT INTO task_dependencies(task,dependency)
SELECT task,dependency FROM legacy_task_dependencies;
DROP TABLE legacy_task_dependencies;
CREATE INDEX task_dependencies_dependency ON task_dependencies(dependency);
