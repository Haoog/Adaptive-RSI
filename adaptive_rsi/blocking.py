"""Bounded, read-only causal explanations retain simultaneous queue blockers."""
import time


def explain(runtime, project, task, limit=1000):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Invalid blocking explanation limit')
    owner = runtime.project(project)
    root = runtime.store.one('SELECT id,state FROM tasks WHERE id=? AND project=?', (task, project))
    if not root:
        raise ValueError('Task not found in this project')
    now = time.time()
    with runtime.store.snapshot():
        rows = runtime.store.query(
            'WITH RECURSIVE ancestry(id) AS (SELECT id FROM tasks WHERE id=? AND project=? UNION '
            'SELECT p.id FROM ancestry a JOIN task_dependencies d ON d.task=a.id '
            "JOIN tasks p ON p.id=d.dependency AND p.project=? WHERE p.state<>'merged' LIMIT ?) "
            'SELECT t.id,t.title,t.state,t.available FROM ancestry a JOIN tasks t ON t.id=a.id',
            (task, project, project, limit + 1))
        truncated = len(rows) > limit
        rows = rows[:limit]
        ids = [row['id'] for row in rows]
        edges = runtime.store.query('SELECT d.task,d.dependency,p.state,p.project FROM task_dependencies d '
                                    'LEFT JOIN tasks p ON p.id=d.dependency WHERE d.task IN (' + ','.join('?' for _ in ids) +
                                    ") AND (p.state IS NULL OR p.state<>'merged') LIMIT ?", (*ids, limit + 1)) if ids else []
        truncated = truncated or len(edges) > limit
        edges = edges[:limit]
        busy = runtime.store.query('SELECT id,task FROM runs WHERE project=? AND ended IS NULL LIMIT 5', (project,))
        active_count = runtime.store.one('SELECT count(*) AS n FROM runs WHERE ended IS NULL')['n']
    nodes = []
    known = set(ids)
    for row in rows:
        reasons = []
        if row['state'] in ('failed', 'cancelled', 'rejected', 'expired', 'blocked', 'interrupted', 'deferred', 'insufficient_evidence'):
            reasons.append({'code': 'needs_review', 'state': row['state']})
        if row['available'] and row['available'] > now:
            reasons.append({'code': 'backoff', 'until': row['available']})
        parents = [edge['dependency'] for edge in edges if edge['task'] == row['id']]
        if parents:
            reasons.append({'code': 'dependency', 'parents': parents})
        if row['state'] == 'pr_open':
            reasons.append({'code': 'awaiting_merge'})
        nodes.append({**row, 'reasons': reasons})
    for edge in edges:
        if edge['dependency'] not in known:
            known.add(edge['dependency'])
            if len(nodes) >= limit:
                truncated = True
                continue
            code = 'invalid_dependency' if edge['project'] != project else 'omitted_dependency'
            nodes.append({'id': edge['dependency'], 'title': None, 'state': None, 'reasons': [{'code': code}]})
    shared = []
    if owner['paused']:
        shared.append({'code': 'paused'})
    if busy:
        shared.append({'code': 'running', 'runs': [row['id'] for row in busy]})
    capacity = runtime.config()['limits']['concurrency']
    if active_count >= capacity:
        shared.append({'code': 'capacity', 'active': active_count, 'capacity': capacity})
    opening = runtime.next_effective_schedule(project, now)
    if opening > now + 1:
        shared.append({'code': 'working_hours', 'until': opening})
    pressure = runtime.resource_pressure.projects.get(project)
    limits = runtime.config(project)['limits']
    if (pressure and now - pressure['checked_at'] < 60 and not pressure['admitted'] and
            pressure['min_free_disk_bytes'] == limits['min_free_disk_mb'] * 1048576 and
            pressure['min_available_memory_bytes'] == limits['min_available_memory_mb'] * 1048576):
        shared.append({'code': 'resource_pressure', 'reasons': pressure['reasons']})
    # Explanation must not recover update journals or release admission leases.
    update = runtime.store.setting('update-status', {})
    lease = runtime.store.setting('desktop-update-barrier', {})
    if (getattr(runtime.updates, 'installing', False) or getattr(runtime.updates, 'recovering', False) or
            update.get('state') == 'restarting' or lease.get('phase') in ('draining', 'installing')):
        shared.append({'code': 'update_in_progress'})
    if runtime.import_in_progress:
        shared.append({'code': 'project_import'})
    return {'root': task, 'project': project, 'checked_at': now, 'nodes': nodes,
            'edges': [{'task': row['task'], 'dependency': row['dependency']} for row in edges],
            'shared_reasons': shared, 'truncated': truncated, 'limit': limit}
