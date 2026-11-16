"""Offline, fail-closed retirement of publication-derived evidence.

No remote query, Git or evaluator is allowed here. Legacy receipts without an
atomic verified merge observation cannot release their generation hold.
"""
try:
    import fcntl
except ImportError:  # Unsupported locking platforms retain generation evidence.
    fcntl = None
import json
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULTS, fingerprint, merge, validate

PREFIXES = ('delivery-generation:', 'delivery-generation-history:',
            'delivery-generation-pr:', 'delivery-generation-preparing:',
            'delivery-generation-history-proof:')
LIFECYCLE = PREFIXES + ('delivery-push:', 'delivery-candidate:', 'delivery:',
                        'run-baseline:', 'merge-intent:', 'automation:')


def settings(db, run_id, prefixes=PREFIXES):
    """Longest existing run ID owns a settings namespace, including history."""
    result = {}
    for prefix in prefixes:
        key = prefix + run_id
        for row in db.execute('SELECT key,value FROM settings WHERE key=? OR (key>=? AND key<?)',
                              (key, key + ':', key + ';')):
            suffix = row['key'][len(prefix):]
            while suffix != run_id:
                if db.execute('SELECT 1 FROM runs WHERE id=?', (suffix,)).fetchone():
                    break
                if ':' not in suffix:
                    break
                suffix = suffix.rsplit(':', 1)[0]
            if suffix == run_id:
                result[row['key']] = row['value']
    return result


def object_value(value):
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError('Expected object evidence')
    return parsed


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def releasable(db, run_id, project, now=None):
    """Prove an expired, closed lifecycle using one database snapshot.

    Returns False on unknown shapes, identity changes or pending work. An orphan
    preparation can also retire after an expired failed/cancelled run, provided
    there is no publication evidence whatsoever. Recovery abandonment is never
    treated as proof that a remote write did not happen.
    """
    from .delivery_generation import GenerationError
    if fcntl is None or not all(hasattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW')):
        return False
    try:
        return _releasable(db, run_id, project, time.time() if now is None else now)
    except (GenerationError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def _releasable(db, run_id, project, now):
    from .delivery_generation import journal_policy_db
    from .delivery import _update_pr_identity
    from .storage_tools import pending_investigation
    run = db.execute('SELECT * FROM runs WHERE id=? AND project=?', (run_id, project)).fetchone()
    p = db.execute('SELECT config FROM projects WHERE id=?', (project,)).fetchone()
    if not run or not p or not _number(run['ended']):
        return False
    for key in ('legal-hold:' + run_id, 'legal-hold-project:' + project):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if row and json.loads(row[0]) is not False:
            return False
    global_row = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    live = validate(merge(DEFAULTS, object_value(global_row[0]) if global_row else {}, object_value(p[0])))
    if run['ended'] >= now - live['limits']['retention_days'] * 86400:
        return False
    outcome = object_value(run['result'])
    saved = object_value(run['config'])
    task = db.execute('SELECT state FROM tasks WHERE id=? AND project=?', (run['task'], project)).fetchone()
    if not task or db.execute("SELECT 1 FROM tasks WHERE project=? AND body=? AND source IN "
            "('system_publish','system_release','system_deploy') AND state NOT IN "
            "('completed','failed','cancelled','expired','obsolete','merged','superseded')",
            (project, run_id)).fetchone():
        return False
    closing = outcome.get('close_pending')
    if ((closing is not None and closing is not False) or
            ('feedback' in outcome and not isinstance(outcome['feedback'], dict)) or
            pending_investigation(outcome.get('feedback')) is not None):
        return False
    # A disabled current policy does not disprove an older dispatched workflow.
    # Retiring automation receipts needs its own complete reconciliation proof.
    if settings(db, run_id, ('automation:',)):
        return False
    # Enabled automation may need artifacts even after monitoring. Until its
    # entire workflow/health/rollback receipt is reconciled, keep the evidence.
    if any(saved.get('delivery', {}).get(kind, {}).get('enabled') for kind in ('release', 'deploy')):
        return False
    data = settings(db, run_id)
    if not data:
        return False
    parsed = {key: object_value(value) for key, value in data.items()}
    active = parsed.get('delivery-generation:' + run_id)
    push_row = db.execute('SELECT value FROM settings WHERE key=?', ('delivery-push:' + run_id,)).fetchone()
    receipt = parsed.get('delivery-generation-pr:' + run_id)
    for key in ('delivery-update:', 'merge-intent:', 'revision-pending:', 'issue-revision-pending:'):
        if db.execute('SELECT 1 FROM settings WHERE key=?', (key + run_id,)).fetchone():
            return False
    for key, value in parsed.items():
        if key.startswith(('delivery-generation:', 'delivery-generation-history:', 'delivery-generation-preparing:')):
            directory = Path(value['directory'])
            if (not directory.is_absolute() or '..' in directory.parts or
                    not re.fullmatch('[0-9a-f]{32}', directory.name) or
                    directory.parent.name != 'generations' or directory.parent.parent.name != run_id or
                    directory.parent.parent.parent.name != 'runs' or directory.is_symlink() or
                    directory.parent.is_symlink() or directory.parent.parent.is_symlink() or
                    directory.parent.parent.parent.is_symlink() or
                    (run['path'] is not None and directory.parent.parent != Path(run['path']))):
                return False
        if key.startswith('delivery-generation-preparing:'):
            if (set(value) != {'version', 'directory', 'created'} or type(value['version']) is not int or
                    value['version'] != 1 or not _number(value['created']) or
                    value['created'] >= now - live['limits']['retention_days'] * 86400 or
                    key.rsplit(':', 1)[1] != Path(value['directory']).name):
                return False
        elif key.startswith('delivery-generation-history-proof:'):
            history_key = 'delivery-generation-history:' + key[len('delivery-generation-history-proof:'):]
            if value != {'unpublished': True, 'record': fingerprint(parsed.get(history_key))} or history_key not in parsed:
                return False
        elif key.startswith(('delivery-generation:', 'delivery-generation-history:')):
            if key.startswith('delivery-generation-history:'):
                proof_key = 'delivery-generation-history-proof:' + key[len('delivery-generation-history:'):]
                if parsed.get(proof_key) != {'unpublished': True, 'record': fingerprint(value)}:
                    return False

            if (type(value.get('version')) is not int or value['version'] != 1 or
                    not re.fullmatch('[0-9a-f]{32}', value['generation']) or
                    value['ref'] != 'refs/adaptive-rsi/generations/' + value['generation'] or
                    any(not re.fullmatch('[0-9a-f]{40,64}', value[field]) for field in ('commit', 'parent', 'tree')) or
                    any(not re.fullmatch('[0-9a-f]{64}', value[field]) for field in
                        ('authorization', 'snapshot', 'environment', 'evidence_hash', 'patch_hash')) or
                    not isinstance(value['approval'], str) or not value['approval'] or
                    fingerprint(value['evaluation']) != value['evidence_hash'] or
                    value['evaluation']['decision'] != 'pass' or
                    value['evaluation']['source']['base'] != value['parent'] or
                    value['evaluation']['source']['tree'] != value['tree'] or
                    value['evaluation']['source']['patch_hash'] != value['patch_hash']):
                return False
    # Only newly archived history with the atomic unpublished proof above can
    # retire without a current generation. Legacy history remains held.
    if not active:
        return (all(key.startswith(('delivery-generation-preparing:',
                                   'delivery-generation-history:',
                                   'delivery-generation-history-proof:')) for key in parsed) and
                run['state'] in ('failed', 'cancelled', 'expired') and
                task['state'] in ('failed', 'cancelled', 'expired') and not push_row and not receipt and
                not outcome.get('pr') and not outcome.get('delivery_recovery_checked') and
                not outcome.get('delivery_recovery_abandoned') and
                not db.execute('SELECT 1 FROM settings WHERE key=?', ('delivery:' + run_id,)).fetchone())
    if (run['state'] != 'merged' or task['state'] != 'merged' or
            outcome.get('monitoring_completed') is not True or outcome.get('delivery_status') != 'merged' or
            outcome.get('delivery_generation') != active['generation'] or not _number(outcome.get('merged_at')) or
            not re.fullmatch('[0-9a-f]{40,64}', outcome.get('merged_sha', '')) or not push_row or not receipt or
            'error' in receipt or receipt.get('generation') != active['generation']):
        return False
    intent = object_value(push_row[0])
    if 'created' in intent and not _number(intent['created']):
        return False
    binding_row = db.execute('SELECT value FROM settings WHERE key=?', ('delivery-candidate:' + run_id,)).fetchone()
    binding = object_value(binding_row[0]) if binding_row else {}
    if (set(binding) != {'version', 'branch', 'head', 'tree', 'base', 'repository', 'evidence', 'approval', 'authorization'} or
            type(binding['version']) is not int or binding['version'] != 1 or
            any(not re.fullmatch('[0-9a-f]{40,64}', binding[field]) for field in ('head', 'tree', 'base')) or
            any(not re.fullmatch('[0-9a-f]{64}', binding[field]) for field in ('evidence', 'authorization')) or
            not isinstance(binding['approval'], str) or not binding['approval']):
        return False
    policy = journal_policy_db(db, dict(run), intent)
    stored = db.execute('SELECT value FROM settings WHERE key=?', ('delivery:' + run_id,)).fetchone()
    if not stored or object_value(stored[0]) != policy:
        return False
    proof = receipt['verified_merge']
    pr = proof['observation']
    if (set(proof) != {'observation', 'binding'} or proof['binding'] != fingerprint(
            {'generation': active, 'intent': intent, 'policy': policy}) or
            receipt.get('observation') != pr or pr.get('state') != 'MERGED' or pr.get('url') != receipt['url'] or
            receipt['url'] != outcome.get('pr') or
            not re.fullmatch(r'https://github\.com/' + re.escape(policy['repository']) + r'/pull/[1-9][0-9]*', receipt['url'], re.I) or
            pr.get('mergeCommit', {}).get('oid') != outcome['merged_sha'] or
            not _update_pr_identity({**pr, 'state': 'OPEN'}, policy['repository'], policy['branch'], intent['branch'], policy['head'])):
        return False
    return True


@contextmanager
def managed_directory(store, run_id, missing_ok=False):
    """Pin both controller-owned ancestors; never follow a swapped runs link."""
    if Path(run_id).parts != (run_id,) or run_id in ('.', '..'):
        raise ValueError('Unsafe run ID')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(store.root / 'runs', flags)
    try:
        try:
            run = os.open(run_id, flags, dir_fd=root)
        except FileNotFoundError:
            if not missing_ok:
                raise
            yield None
            return
        try:
            yield run
        finally:
            os.close(run)
    finally:
        os.close(root)


# A pass has a fixed mutation/visit budget. Exhaustion keeps path and journals;
# the next maintenance pass starts from the surviving tree. No filesystem walk
# or iterator advance is allowed inside a database write transaction.
CLEANUP_STEPS = 64
CLEANUP_DEPTH = 32
CLEANUP_RECORDS = 256
CLEANUP_EVIDENCE_BYTES = 1024 * 1024


def _bounded_authorization(db, run_id):
    """Reject oversized namespaces before loading/parsing them under a lock.

    Include nested namespaces conservatively. An ambiguous/oversized namespace
    retains evidence rather than making a supposedly small batch unbounded.
    """
    count = size = 0
    for prefix in LIFECYCLE:
        key = prefix + run_id
        for row in db.execute(
                'SELECT length(CAST(value AS BLOB)) FROM settings '
                'WHERE key=? OR (key>=? AND key<?) LIMIT ?',
                (key, key + ':', key + ';', CLEANUP_RECORDS + 1)):
            count += 1
            size += row[0] or 0
            if count > CLEANUP_RECORDS or size > CLEANUP_EVIDENCE_BYTES:
                return False
    row = db.execute('SELECT length(CAST(r.config AS BLOB)) + '
                     'length(CAST(r.result AS BLOB)) + length(CAST(p.config AS BLOB)) '
                     'FROM runs r JOIN projects p ON p.id=r.project WHERE r.id=?', (run_id,)).fetchone()
    if not row or row[0] is None:
        return False
    size += row[0]
    for row in db.execute(
            "SELECT length(CAST(value AS BLOB)) FROM settings WHERE key IN "
            "('config', ?, (SELECT 'legal-hold-project:'||project FROM runs WHERE id=?))",
            ('legal-hold:' + run_id, run_id)):
        size += row[0] or 0
    return size <= CLEANUP_EVIDENCE_BYTES


class CleanupStopped(OSError):
    """Ownership, authorization, or this pass's bounded budget was lost."""


def _identity(info):
    return info.st_dev, info.st_ino


class DirectorySnapshot:
    """Keep the original root/run/generations identities across every call.

    NOFOLLOW and flock constrain cooperating writers, not rename by outsiders.
    validate() checks both named locations and descriptors before each mutation.
    POSIX offers no atomic validate-and-unlink: an uncooperative rename in the
    final syscall window remains outside this protocol's protection.
    """
    def __init__(self, store, run_id):
        if (fcntl is None or not all(hasattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW'))):
            raise CleanupStopped('Safe directory operations unavailable')
        if Path(run_id).parts != (run_id,) or run_id in ('.', '..'):
            raise CleanupStopped('Unsafe run ID')
        self.paths = [store.root, store.root / 'runs', store.root / 'runs' / run_id,
                      store.root / 'runs' / run_id / 'generations']
        self.fds = []
        self.identities = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            for index, path in enumerate(self.paths):
                try:
                    fd = (os.open(path, flags) if index == 0 else
                          os.open(path.name, flags, dir_fd=self.fds[-1]))
                except FileNotFoundError:
                    # Only run and generations may already have disappeared.
                    if index < 2:
                        raise
                    break
                self.fds.append(fd)
                self.identities.append(_identity(os.fstat(fd)))
            self.validate()
        except BaseException:
            self.close()
            raise

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []

    def validate(self):
        for index, path in enumerate(self.paths):
            try:
                info = (os.stat(path, follow_symlinks=False) if index == 0 else
                        os.stat(path.name, dir_fd=self.fds[index - 1], follow_symlinks=False))
            except FileNotFoundError:
                if index == len(self.fds):
                    return
                raise CleanupStopped('Original directory disappeared')
            if (index >= len(self.fds) or not stat.S_ISDIR(info.st_mode) or
                    _identity(info) != self.identities[index] or
                    _identity(os.fstat(self.fds[index])) != self.identities[index] or
                    _identity(os.stat(path, follow_symlinks=False)) != self.identities[index]):
                raise CleanupStopped('Original directory moved or replaced')


def _cleanup_boundary():
    """Outside-transaction scheduling boundary (also used by event regressions)."""


class CleanupBatch:
    def __init__(self, store, run_id, project, snapshot, run, evidence):
        self.store, self.run_id, self.project = store, run_id, project
        self.snapshot, self.run, self.evidence = snapshot, run, evidence
        self.remaining = CLEANUP_STEPS
        self.mutations = CLEANUP_STEPS

    def check_chain(self, chain):
        self.snapshot.validate()
        for parent, name, fd, identity in chain:
            if (_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity or
                    _identity(os.fstat(fd)) != identity):
                raise CleanupStopped('Artifact moved or replaced')

    def visit(self, chain):
        if self.remaining <= 0:
            raise CleanupStopped('Cleanup batch exhausted')
        self.remaining -= 1
        self.check_chain(chain)

    def mutate(self, chain, action):
        # Traversal, arbitrary scheduling delays and cooperative pause happen
        # before BEGIN. Exactly one filesystem mutation follows revalidation.
        if self.mutations <= 0:
            raise CleanupStopped('Cleanup mutation budget exhausted')
        self.mutations -= 1
        _cleanup_boundary()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not _bounded_authorization(db, self.run_id):
                raise CleanupStopped('Cleanup authorization exceeds budget')
            current = db.execute('SELECT * FROM runs WHERE id=?', (self.run_id,)).fetchone()
            if (not current or dict(current) != self.run or
                    settings(db, self.run_id, LIFECYCLE) != self.evidence or
                    not releasable(db, self.run_id, self.project)):
                raise CleanupStopped('Cleanup authorization changed')
            self.check_chain(chain)
            action()

    def remove(self, parent, name, chain=(), expected=None, directory_only=False):
        """Streaming, bounded depth walk; never recursively delete under BEGIN."""
        self.visit(chain)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        identity = _identity(info)
        if ((expected is not None and identity != expected) or
                (directory_only and not stat.S_ISDIR(info.st_mode))):
            raise CleanupStopped('Original artifact replaced')
        def entry_check():
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity:
                raise CleanupStopped('Artifact entry replaced')
        if not stat.S_ISDIR(info.st_mode):
            def unlink():
                entry_check()
                os.unlink(name, dir_fd=parent)
            self.mutate(chain, unlink)
            return
        if len(chain) >= CLEANUP_DEPTH:
            raise CleanupStopped('Cleanup depth limit')
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            child_chain = (*chain, (parent, name, fd, identity))
            self.check_chain(child_chain)
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode & stat.S_IRWXU != stat.S_IRWXU:
                self.mutate(child_chain, lambda: os.fchmod(fd, mode | stat.S_IRWXU))
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.remove(fd, entry.name, child_chain)
            def rmdir():
                entry_check()
                os.rmdir(name, dir_fd=parent)
            self.mutate(child_chain, rmdir)
        finally:
            os.close(fd)


def remove_directories(batch, directory):
    """Retire only registered children, retaining everything on ambiguity.

    The caller captured batch.snapshot before entering this function. No write
    transaction spans a walk. Each unlink/rmdir/fchmod separately rechecks the
    original path chain and the exact lifecycle snapshot under BEGIN IMMEDIATE.
    """
    batch.snapshot.validate()
    if directory != batch.snapshot.paths[2]:
        return False
    if len(batch.snapshot.fds) < 4:
        return True
    fd = batch.snapshot.fds[3]
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    owned = set()
    for key, raw in batch.evidence.items():
        if not key.startswith(('delivery-generation:', 'delivery-generation-history:',
                               'delivery-generation-preparing:')):
            continue
        path = Path(object_value(raw)['directory'])
        if not re.fullmatch('[0-9a-f]{32}', path.name) or path != directory / 'generations' / path.name:
            return False
        owned.add(path.name)
    # Validate the whole top-level ownership set before touching any child.
    # Stream rather than allocating an unbounded directory listing.
    original_children = {}
    with os.scandir(fd) as entries:
        for entry in entries:
            batch.snapshot.validate()
            info = entry.stat(follow_symlinks=False)
            if entry.name not in owned or not stat.S_ISDIR(info.st_mode):
                return False
            original_children[entry.name] = _identity(info)
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.name not in original_children:
                return False
            batch.remove(fd, entry.name, expected=original_children[entry.name], directory_only=True)
    # The pinned generations descriptor must stop participating in validate
    # only AFTER rmdir succeeded. Nothing mutates that snapshot before then.
    batch.mutate((), lambda: os.rmdir('generations', dir_fd=batch.snapshot.fds[2]))
    os.close(batch.snapshot.fds.pop())
    batch.snapshot.identities.pop()
    return True


def remove_run_artifacts(store, run_id, project, directory, snapshot, stored=None):
    """Generation-managed entry point; all interruption leaves journals intact."""
    from .maintenance import CLEANUP_NAMES, RUN_FILE_ARTIFACTS, _run_directory_settled
    try:
        snapshot.validate()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not _bounded_authorization(db, run_id):
                return False
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if (not row or row['path'] != (stored if stored is not None else str(directory)) or
                    not releasable(db, run_id, project)):
                return False
            batch = CleanupBatch(store, run_id, project, snapshot, dict(row), settings(db, run_id, LIFECYCLE))
        if not remove_directories(batch, directory):
            return False
        if len(snapshot.fds) >= 3:
            for name in CLEANUP_NAMES + RUN_FILE_ARTIFACTS:
                try:
                    info = os.stat(name, dir_fd=snapshot.fds[2], follow_symlinks=False)
                except FileNotFoundError:
                    continue
                is_directory = name in CLEANUP_NAMES
                if not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
                    continue
                batch.remove(snapshot.fds[2], name, expected=_identity(info), directory_only=is_directory)
        _cleanup_boundary()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not _bounded_authorization(db, run_id):
                return False
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if (not row or dict(row) != batch.run or
                    settings(db, run_id, LIFECYCLE) != batch.evidence or
                    not releasable(db, run_id, project)):
                return False
            snapshot.validate()
            if len(snapshot.fds) >= 3 and not _run_directory_settled(snapshot.fds[2]):
                return False
            db.execute('UPDATE runs SET path=NULL WHERE id=? AND path=?', (run_id, row['path']))
        return True
    except (CleanupStopped, FileNotFoundError):
        return False


def retire_settings(store, now):
    """Settings keep their existing 90-day window and survive failed unlink."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        cursor_key = 'maintenance-generation-retirement-cursor'
        cursor_row = db.execute('SELECT value FROM settings WHERE key=?', (cursor_key,)).fetchone()
        try:
            cursor = json.loads(cursor_row[0]) if cursor_row else ''
        except (TypeError, ValueError):
            cursor = ''
        if not isinstance(cursor, str):
            cursor = ''
        rows = db.execute("SELECT id,project FROM runs WHERE state IN ('merged','failed','cancelled','expired') "
                          "AND path IS NULL AND ended<? AND id>? AND EXISTS (SELECT 1 FROM settings WHERE "
                          "key='delivery-generation:'||runs.id OR (key>='delivery-generation-preparing:'||runs.id||':' "
                          "AND key<'delivery-generation-preparing:'||runs.id||';') OR "
                          "(key>='delivery-generation-history:'||runs.id||':' "
                          "AND key<'delivery-generation-history:'||runs.id||';')) ORDER BY id LIMIT 200",
                          (now - 90 * 86400, cursor)).fetchall()
        # Unreconciled/corrupt records must not permanently occupy the batch.
        db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                   (cursor_key, json.dumps(rows[-1]['id'] if len(rows) == 200 else '')))
        for run in rows:
            if releasable(db, run['id'], run['project'], now):
                for key in settings(db, run['id'], LIFECYCLE):
                    db.execute('DELETE FROM settings WHERE key=?', (key,))
