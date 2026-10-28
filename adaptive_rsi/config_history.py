"""Private configuration snapshots and conservative public audit projections."""
import json
import math
import re
import time

from .config import fingerprint
from .store import encode


REDACTED = '[redacted]'
PRIVATE_SUBTREES = {'providers', 'environment', 'checks', 'protected_paths'}


def redacted_changes(before, after, limit=200, root=''):
    rows = []
    def visit(first, second, path):
        if first == second:
            return
        mappings = (isinstance(first, dict) or first is None) and (isinstance(second, dict) or second is None)
        if path.split('.')[0] not in PRIVATE_SUBTREES and mappings and (isinstance(first, dict) or isinstance(second, dict)):
            first_map, second_map = first or {}, second or {}
            for key in sorted(set(first_map) | set(second_map)):
                visit(first_map.get(key), second_map.get(key), path + '.' + key if path else key)
            return
        def safe(value):
            sensitive = re.search(r'password|secret|credential|authorization|(?:^|[._])(?:api_key|access_token|refresh_token|token)$', path, re.I)
            scalar = value is None or type(value) in (bool, int) or type(value) is float and math.isfinite(value)
            return value if not sensitive and scalar else REDACTED
        rows.append({'path': path, 'before': safe(first), 'after': safe(second)})
    try:
        visit(before, after, root)
    except (RecursionError, TypeError):
        return {'changes': [], 'truncated': True, 'error': 'invalid_snapshot'}
    return {'changes': rows[:limit], 'truncated': len(rows) > limit}


def snapshot_key(project, revision):
    return 'config-history:' + (project or 'global') + ':' + str(revision).zfill(20)


def write_snapshot(db, project, before, after, revision, source, undo_of=None):
    key = snapshot_key(project, revision)
    payload = {'version': 1, 'project': project, 'before': before, 'after': after,
               'before_hash': fingerprint(before), 'after_hash': fingerprint(after),
               'revision': revision, 'created': time.time(), 'source': source, 'undo_of': undo_of}
    db.execute('INSERT INTO settings VALUES (?,?)', (key, encode(payload)))
    return {'history': key, 'source': source, 'revision': revision, 'undo_of': undo_of,
            **redacted_changes(before, after)}


def decode_snapshot(raw):
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1 or
                type(value.get('revision')) is not int or not 0 < value['revision'] < 2**63 or
                type(value.get('created')) not in (int, float) or not 0 <= value['created'] < 2**63 or
                value.get('source') not in ('console', 'runtime', 'undo') or
                value.get('project') is not None and not isinstance(value['project'], str) or
                value.get('undo_of') is not None and (type(value['undo_of']) is not int or value['undo_of'] <= 0) or
                not isinstance(value.get('before'), dict) or not isinstance(value.get('after'), dict) or
                value.get('before_hash') != fingerprint(value['before']) or
                value.get('after_hash') != fingerprint(value['after'])):
            return None
        return value
    except (ValueError, TypeError, RecursionError):
        return None


def public_events(events):
    for event in events:
        if event.get('kind') != 'config':
            continue
        try:
            data = json.loads(event.get('data') or '{}')
        except (ValueError, TypeError, RecursionError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Original legacy evidence remains private in SQLite; no old raw snapshot
        # is exposed by the ordinary activity endpoints.
        if isinstance(data.get('before'), dict) and isinstance(data.get('after'), dict):
            safe = redacted_changes(data['before'], data['after'])
        else:
            changes = data.get('changes', [])
            safe = {'changes': [], 'truncated': data.get('truncated') is True}
            for row in changes[:200] if isinstance(changes, list) else []:
                if isinstance(row, dict) and isinstance(row.get('path'), str):
                    safe['changes'].extend(redacted_changes(row.get('before'), row.get('after'), root=row['path'][:300])['changes'] or
                                           [{'path': row['path'], 'before': REDACTED, 'after': REDACTED}])
                    safe['changes'][-1]['path'] = row['path'][:300]
        safe.update(source=data.get('source') if data.get('source') in ('console', 'runtime', 'undo') else 'legacy',
                    revision=data.get('revision') if type(data.get('revision')) is int else None)
        event['data'] = encode(safe)
    return events


def page(runtime, project=None, before=2**63 - 1):
    if type(before) is not int or not 0 < before <= 2**63 - 1:
        raise ValueError('Invalid revision pagination')
    if project:
        runtime.project(project)
    prefix = 'config-history:' + (project or 'global') + ':'
    rows = runtime.store.query('SELECT key,value FROM settings WHERE key>=? AND key<? ORDER BY key DESC LIMIT 51',
                               (prefix, snapshot_key(project, before)))
    current = runtime.store.config_revision(project)
    try:
        raw = json.loads(runtime.project(project)['config']) if project else runtime.store.setting('config', {})
        current_hash = fingerprint(raw)
    except (ValueError, TypeError, RecursionError):
        current_hash = None
    items = []
    for row in rows[:50]:
        snapshot = decode_snapshot(row['value'])
        if (not snapshot or snapshot.get('project') != project or
                row['key'] != snapshot_key(project, snapshot['revision'])):
            items.append({'revision': None, 'can_undo': False, 'error': 'invalid_snapshot'})
            continue
        items.append({'revision': snapshot['revision'], 'created': snapshot['created'], 'source': snapshot['source'],
                      'undo_of': snapshot.get('undo_of'),
                      'can_undo': current == snapshot['revision'] and current_hash == snapshot['after_hash'],
                      **redacted_changes(snapshot['before'], snapshot['after'])})
    cursor = rows[49]['key'].rsplit(':', 1)[-1] if len(rows) > 50 else None
    return {'entries': items, 'revision': current, 'has_more': len(rows) > 50,
            'next_before': int(cursor) if cursor and cursor.isdigit() else None, 'limit': 50}


def undo(runtime, project, revision, expected):
    if type(revision) is not int or type(expected) is not int:
        raise ValueError('Configuration changed in another window; reload before saving')
    with runtime.dispatch_lock:
        if project:
            runtime.require_active_project(project)
        current = runtime.store.config_revision(project)
        row = runtime.store.one('SELECT value FROM settings WHERE key=?', (snapshot_key(project, revision),))
        snapshot = decode_snapshot(row['value']) if row else None
        raw = json.loads(runtime.project(project)['config']) if project else runtime.store.setting('config', {})
        if (current != expected or current != revision or not snapshot or snapshot.get('project') != project or
                snapshot['revision'] != revision or
                fingerprint(raw) != snapshot['after_hash']):
            raise ValueError('Configuration changed in another window; reload before saving')
        return runtime._configure(snapshot['before'], project, expected_revision=expected,
                                  replacement=True, source='undo', undo_of=revision)
