"""Bounded dependency graph edits share the task/audit write transaction."""
import time

from .store import encode
from .task_lifecycle import intervention_reason


def selected_dependencies(value):
    values = [] if value is None or value == '' else [value] if isinstance(value, str) else value
    if (not isinstance(values, list) or len(values) > 100 or
            any(not isinstance(item, str) or not item for item in values) or len(set(values)) != len(values)):
        raise ValueError('Dependencies require up to 100 unique task IDs')
    return sorted(values)


def save(runtime, task, dependencies=None, urgency=0, reason=None):
    from .runtime import DEAD_DEPENDENCY_STATES
    intervention_reason(reason)
    selected = selected_dependencies(dependencies)
    if type(urgency) is not int or urgency not in (0, 1):
        raise ValueError('Urgency must be 0 or 1')
    with runtime.dispatch_lock, runtime.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        if not row or row['state'] not in ('queued', 'deferred', 'blocked'):
            raise ValueError('Only waiting tasks can be scheduled')
        if db.execute('SELECT 1 FROM settings WHERE key=?', ('project-archived:' + row['project'],)).fetchone():
            raise ValueError('Project is archived; restore it before starting work')
        previous = [parent[0] for parent in db.execute('SELECT dependency FROM task_dependencies WHERE task=? ORDER BY dependency', (task,))]
        for identifier in selected:
            parent = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if not parent or parent['project'] != row['project'] or identifier == task or (parent['source'] or '').startswith('system_'):
                raise ValueError('Dependency must be another task in the same project')
            if parent['state'] in DEAD_DEPENDENCY_STATES:
                raise ValueError('Dependency task cannot reach merge; retry or choose another task')
        ancestors = [item[0] for item in db.execute(
            'WITH RECURSIVE ancestors(id) AS (SELECT value FROM json_each(?) UNION '
            'SELECT d.dependency FROM task_dependencies d JOIN ancestors a ON d.task=a.id LIMIT 1001) '
            'SELECT id FROM ancestors', (encode(selected),))]
        if task in ancestors:
            raise ValueError('Task dependency cycle')
        if len(ancestors) > 1000:
            raise ValueError('Dependency graph exceeds 1000 ancestors')
        db.execute('DELETE FROM task_dependencies WHERE task=?', (task,))
        db.executemany('INSERT INTO task_dependencies(task,dependency) VALUES (?,?)', [(task, identifier) for identifier in selected])
        now = time.time()
        db.execute('UPDATE tasks SET urgency=?,updated=? WHERE id=?', (urgency, now, task))
        db.execute("INSERT INTO events(project,kind,message,data,created) VALUES (?, 'queue_settings', 'Queue dependencies or urgency changed',?,?)",
                   (row['project'], encode({'task': task, 'dependencies': selected, 'urgency': urgency, 'reason': reason,
                    'before': {'dependencies': previous, 'urgency': row['urgency']},
                    'after': {'dependencies': selected, 'urgency': urgency}}), now))
    runtime.wake.set()
