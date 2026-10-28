CREATE TABLE task_group_members (
    group_task TEXT NOT NULL REFERENCES tasks(id),
    original_task TEXT NOT NULL REFERENCES tasks(id),
    restored REAL,
    PRIMARY KEY(group_task,original_task)
);
CREATE INDEX task_group_original ON task_group_members(original_task,group_task);
INSERT OR IGNORE INTO task_group_members(group_task,original_task)
SELECT grouped.id,original.id FROM tasks grouped,
json_each(CASE WHEN json_valid(grouped.evidence) THEN grouped.evidence ELSE '[]' END,'$[0].batch_tasks') member
JOIN tasks original ON original.id=member.value AND original.project=grouped.project
WHERE grouped.source='goal' AND grouped.fingerprint LIKE 'batch:%' AND grouped.id<>original.id
AND member.type='text'
AND json_type(CASE WHEN json_valid(grouped.evidence) THEN grouped.evidence ELSE '[]' END,'$[0].batch_tasks')='array'
AND json_array_length(CASE WHEN json_valid(grouped.evidence) THEN grouped.evidence ELSE '[]' END,'$[0].batch_tasks') BETWEEN 1 AND 100;
