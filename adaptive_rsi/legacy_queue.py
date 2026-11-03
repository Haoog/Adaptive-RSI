"""Explicit offline import of a quiescent legacy queue snapshot."""
import hashlib
import json
import time
from pathlib import Path

from .store import encode, uid


RESERVED = ('.claim', '.hold', 'failed', 'done')


def import_queue(store, project, directory):
    """Import root Markdown files once; never mutate or execute the source queue.

    The caller must ensure the snapshot and old workers are quiescent. A path is
    the identity; changed contents require manual reconciliation, not a new task.
    """
    root = Path(directory).resolve(strict=True)
    if not root.is_dir() or root.name in RESERVED:
        raise ValueError('Expected a queue snapshot root')
    reserved = []
    for name in RESERVED:
        folder = root / name
        if folder.is_symlink():
            raise ValueError('Reserved directories must not be symlinks')
        if folder.is_dir():
            reserved.extend({'status': name, 'source_path': str(item)}
                            for item in sorted(folder.iterdir()) if item.name.endswith('.md'))
    items = []
    for path in sorted(root.iterdir()):
        if path.name.startswith('.') or path.suffix != '.md' or path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        body = raw.decode('utf-8')
        if not body.strip() or len(body) > 30000:
            raise ValueError(f'Invalid or oversized task: {path.name}')
        digest = hashlib.sha256(raw).hexdigest()
        key = 'legacy-queue:' + hashlib.sha256(str(path).encode('utf-8')).hexdigest()
        items.append((path, body, digest, key))
    result = {'imported': [], 'existing': [], 'reserved': reserved}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        project_row = db.execute('SELECT paused FROM projects WHERE id=?', (project,)).fetchone()
        if not project_row:
            raise ValueError('Project does not exist')
        if not project_row['paused'] or db.execute(
                'SELECT 1 FROM runs WHERE project=? AND ended IS NULL', (project,)).fetchone():
            raise ValueError('Project must be paused with no active runs')
        if db.execute('SELECT 1 FROM settings WHERE key=?', ('project-archived:' + project,)).fetchone():
            raise ValueError('Project is archived')
        for path, body, digest, key in items:
            row = db.execute('SELECT id,evidence FROM tasks WHERE project=? AND fingerprint=?', (project, key)).fetchone()
            if row:
                try:
                    evidence = json.loads(row['evidence'])
                    prior = evidence[0]['sha256']
                except (ValueError, TypeError, KeyError, IndexError):
                    raise ValueError(f'Cannot reconcile existing task: {path.name}') from None
                if prior != digest:
                    raise ValueError(f'Legacy task changed; reconcile manually: {path.name}')
                result['existing'].append(row['id'])
                continue
            task, now = uid(), time.time()
            evidence = [{'source': 'legacy_queue', 'source_path': str(path),
                         'legacy_id': path.name, 'sha256': digest}]
            db.execute('''INSERT INTO tasks
                (id,project,source,fingerprint,title,body,evidence,acceptance,state,priority,overrides,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (task, project, 'goal', key, path.stem[:300], body, encode(evidence), '[]',
                 'queued', 50, '{}', now, now))
            result['imported'].append(task)
    return result
