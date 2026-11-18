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
import sqlite3
import threading
import weakref
from collections.abc import Mapping
from contextlib import contextmanager, closing
from pathlib import Path

from .config import DEFAULTS, fingerprint, merge, validate

PREFIXES = ('delivery-generation:', 'delivery-generation-history:',
            'delivery-generation-pr:', 'delivery-generation-preparing:',
            'delivery-generation-history-proof:')
LIFECYCLE = PREFIXES + ('delivery-push:', 'delivery-candidate:', 'delivery:',
                        'run-baseline:', 'merge-intent:', 'automation:')


def settings(db, run_id, prefixes=PREFIXES):
    """Compatibility view: page keys and load bounded records only on demand."""
    return Evidence(db, run_id, prefixes)


def object_value(value):
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError('Expected object evidence')
    return parsed


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _lifecycle_closed(db, run_id, project, now, checked):
    """Validate the fixed core after every generation record passed in slices."""
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
    if checked.has_prefix(('automation:',)):
        return False
    # Enabled automation may need artifacts even after monitoring. Until its
    # entire workflow/health/rollback receipt is reconciled, keep the evidence.
    if any(saved.get('delivery', {}).get(kind, {}).get('enabled') for kind in ('release', 'deploy')):
        return False
    data = Evidence(db, run_id)
    if not checked.has_prefix(PREFIXES):
        return False
    parsed = ParsedEvidence(data)
    active = parsed.get('delivery-generation:' + run_id)
    push_row = db.execute('SELECT value FROM settings WHERE key=?', ('delivery-push:' + run_id,)).fetchone()
    receipt = parsed.get('delivery-generation-pr:' + run_id)
    for key in ('delivery-update:', 'merge-intent:', 'revision-pending:', 'issue-revision-pending:'):
        if db.execute('SELECT 1 FROM settings WHERE key=?', (key + run_id,)).fetchone():
            return False
    # Only newly archived history with the atomic unpublished proof above can
    # retire without a current generation. Legacy history remains held.
    if not active:
        return (not checked.has_prefix(('delivery-generation:', 'delivery-generation-pr:')) and
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


def _record_valid(key, value, parsed, run, run_id, now, retention_days):
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
                value['created'] >= now - retention_days * 86400 or
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
# Limits are per validation slice/record, never a lifetime namespace limit.
# JSON escaping can expand the default 12 MiB captured checks output sixfold.
VALIDATION_RECORDS = 32
VALIDATION_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 96 * 1024 * 1024


class Evidence(Mapping):
    """Keyset pages and one bounded raw record at a time, in a read snapshot."""
    def __init__(self, db, run_id, prefixes=PREFIXES):
        self.db, self.run_id, self.prefixes = db, run_id, prefixes
        self.bytes_read = 0

    def __iter__(self):
        for prefix in self.prefixes:
            key = prefix + self.run_id
            last = ''
            while True:
                rows = self.db.execute(
                    'SELECT CASE WHEN length(CAST(key AS BLOB))<=4096 THEN key END AS key '
                    'FROM settings WHERE (key=? OR (key>=? AND key<?)) '
                    'AND key>? ORDER BY key LIMIT ?',
                    (key, key + ':', key + ';', last, VALIDATION_RECORDS)).fetchall()
                if not rows:
                    break
                for row in rows:
                    last = row['key']
                    if last is None or len(last) > 4096:
                        raise ValueError('Oversized evidence key')
                    if self.owns(last, prefix):
                        yield last

    def owns(self, key, prefix):
        suffix = key[len(prefix):]
        while suffix != self.run_id:
            if self.db.execute('SELECT 1 FROM runs WHERE id=?', (suffix,)).fetchone():
                return False
            if ':' not in suffix:
                return False
            suffix = suffix.rsplit(':', 1)[0]
        return True

    def __getitem__(self, key):
        if not any(key.startswith(p) and self.owns(key, p) for p in self.prefixes):
            raise KeyError(key)
        row = self.db.execute(
            'SELECT CASE WHEN length(CAST(value AS BLOB))<=? THEN value END AS value, '
            'length(CAST(value AS BLOB)) AS bytes '
            'FROM settings WHERE key=?', (MAX_RECORD_BYTES, key)).fetchone()
        if row is None:
            raise KeyError(key)
        if row['value'] is None:
            raise ValueError('Evidence record exceeds supported size')
        self.bytes_read += row['bytes']
        return row['value']

    def __len__(self):
        return sum(1 for _ in self)

    def __bool__(self):
        # Classification is conservative for nested run namespaces and never
        # scans their keys or values. Exact ownership is proved in slices.
        for prefix in self.prefixes:
            key = prefix + self.run_id
            if self.db.execute('SELECT 1 FROM settings WHERE key=? OR (key>=? AND key<?) LIMIT 1',
                               (key, key + ':', key + ';')).fetchone():
                return True
        return False


class ParsedEvidence(Mapping):
    def __init__(self, raw):
        self.raw = raw

    def __getitem__(self, key):
        return object_value(self.raw[key])

    def __iter__(self):
        return iter(self.raw)

    def __len__(self):
        return len(self.raw)


_validation_lock = threading.RLock()
_validations = weakref.WeakKeyDictionary()
_initialized = weakref.WeakSet()


def _ensure_revision(store):
    """Install a transactional invalidation clock, including direct SQL writers.

    Conservative across runs/projects: unrelated lifecycle writes can restart a
    proof, but ordinary settings writes do not. No evidence is discarded here.
    Schema creation is constant work, once per Store instance, outside dispatch.
    """
    if store in _initialized:
        return
    watched = LIFECYCLE + ('legal-hold:', 'legal-hold-project:', 'delivery-update:',
                         'revision-pending:', 'issue-revision-pending:')
    def relevant(alias):
        return '(' + ' OR '.join([alias + ".key='config'"] + [
            alias + ".key LIKE '" + prefix + "%'" for prefix in watched]) + ')'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS retention_revision '
                   '(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)')
        db.execute('INSERT OR IGNORE INTO retention_revision VALUES (1,0)')
        for table in ('settings', 'runs', 'tasks', 'projects'):
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                when = ''
                if table == 'settings':
                    aliases = ('OLD', 'NEW') if operation == 'UPDATE' else (
                        'OLD' if operation == 'DELETE' else 'NEW',)
                    when = ' WHEN ' + ' OR '.join(relevant(alias) for alias in aliases)
                db.execute('CREATE TRIGGER IF NOT EXISTS retention_revision_' + table + '_' + operation +
                           ' AFTER ' + operation + ' ON ' + table + when +
                           ' BEGIN UPDATE retention_revision SET version=version+1 WHERE id=1; END')
    _initialized.add(store)


def _revision(db):
    row = db.execute('SELECT version FROM retention_revision WHERE id=1').fetchone()
    if not row or type(row[0]) is not int:
        raise CleanupStopped('Invalid retention revision')
    return row[0]


class ValidationCache:
    """One disposable disk database per Store, with bounded SQLite page cache.

    Progress for every run lives on disk, so a busy store cannot evict a large
    lifecycle's partial proof and prevent it from ever finishing.
    """
    def __init__(self):
        self.db = sqlite3.connect('', check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA cache_size=-256')
        self.db.execute('CREATE TABLE progress (run TEXT, project TEXT, revision INTEGER, '
                        'at REAL, prefix INTEGER, cursor TEXT, complete INTEGER, PRIMARY KEY(run,project))')
        self.db.execute('CREATE INDEX progress_revision ON progress(revision)')
        self.db.execute('CREATE TABLE records (run TEXT, project TEXT, revision INTEGER, key TEXT, directory TEXT, '
                        'PRIMARY KEY(run,project,revision,key))')
        self.db.execute('CREATE INDEX owned_directory ON records(run,project,revision,directory)')
        self.db.execute('CREATE INDEX record_revision ON records(revision)')

    def __del__(self):
        self.db.close()

    def drop(self, run_id, project):
        self.db.execute('DELETE FROM progress WHERE run=? AND project=?', (run_id, project))
        # Old index rows are collected in bounded pages, never a lifecycle-wide
        # DELETE when a large proof is invalidated.

    def load(self, run_id, project, revision, now):
        row = self.db.execute('SELECT revision,at,prefix,cursor,complete FROM progress '
                              'WHERE run=? AND project=?', (run_id, project)).fetchone()
        if row is not None and row[0] == revision and now < row[1]:
            return None  # Never replace an index belonging to a live proof.
        if row is None or row[0] != revision:
            self.drop(run_id, project)
            row = (revision, now, 0, '', False)
        return Authorization(self, run_id, project, *row)

    def save(self, state):
        self.db.execute('INSERT OR REPLACE INTO progress VALUES (?,?,?,?,?,?,?)',
                        (state.run_id, state.project, state.revision, state.now,
                         state.prefix, state.cursor, state.complete))


class Authorization:
    """Compact trusted index, bound to every checked source by one revision."""
    def __init__(self, cache, run_id, project, revision, now, prefix, cursor, complete):
        self.cache, self.run_id, self.project = cache, run_id, project
        self.revision, self.now = revision, now
        self.prefix, self.cursor, self.complete = prefix, cursor, complete

    def current(self, db):
        return self.complete and _revision(db) == self.revision and time.time() >= self.now

    def owns(self, name):
        with _validation_lock:
            return self.cache.db.execute('SELECT 1 FROM records WHERE run=? AND project=? AND revision=? '
                                         'AND directory=? LIMIT 1', (self.run_id, self.project, self.revision, name)).fetchone() is not None

    def has_prefix(self, prefixes):
        for prefix in prefixes:
            if self.cache.db.execute('SELECT 1 FROM records WHERE run=? AND project=? AND revision=? AND key>=? AND key<? LIMIT 1',
                                      (self.run_id, self.project, self.revision, prefix, prefix + '~')).fetchone():
                return True
        return False

    def record(self, key, directory):
        self.cache.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)',
                              (self.run_id, self.project, self.revision, key, directory))


def _validation_boundary():
    """Slow validation hook. Never called in a SQLite write transaction."""


def authorization(store, run_id, project, now=None):
    """Advance at most one bounded slice, or return a current complete proof.

    Every slice reads a consistent WAL snapshot without a writer lock. Any
    relevant commit between slices invalidates the entire disposable index.
    Final authorization uses the same revision as ALL checked source records.
    """
    from .delivery_generation import GenerationError
    now = time.time() if now is None else now
    if fcntl is None or not all(hasattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW')):
        return None
    with _validation_lock:
        _ensure_revision(store)
        cache = _validations.get(store)
        if cache is None:
            cache = _validations[store] = ValidationCache()
        try:
            with store.connect() as db:
                db.execute('BEGIN')
                revision = _revision(db)
                obsolete = cache.db.execute('SELECT run,project FROM progress WHERE revision<? OR revision>? LIMIT 8',
                                             (revision, revision)).fetchall()
                for obsolete_run, obsolete_project in obsolete:
                    cache.drop(obsolete_run, obsolete_project)
                cache.db.execute('DELETE FROM records WHERE rowid IN (SELECT rowid FROM records '
                                 'WHERE revision<? OR revision>? LIMIT 128)', (revision, revision))
                state = cache.load(run_id, project, revision, now)
                if state is None:
                    return None
                if state.complete:
                    return state
                # Bound all non-lifecycle evidence before loading or JSON parsing.
                row = db.execute('SELECT length(CAST(r.config AS BLOB)), length(CAST(r.result AS BLOB)), '
                                 'length(CAST(p.config AS BLOB)) FROM runs r JOIN projects p ON p.id=r.project '
                                 'WHERE r.id=? AND r.project=?', (run_id, project)).fetchone()
                if not row or any(n is None or n > MAX_RECORD_BYTES for n in row):
                    raise ValueError('Oversized or missing run evidence')
                for key in ('config', 'legal-hold:' + run_id, 'legal-hold-project:' + project):
                    size = db.execute('SELECT length(CAST(value AS BLOB)) FROM settings WHERE key=?', (key,)).fetchone()
                    if size and (size[0] is None or size[0] > MAX_RECORD_BYTES):
                        raise ValueError('Oversized guard evidence')
                run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                p = db.execute('SELECT config FROM projects WHERE id=?', (project,)).fetchone()
                global_row = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
                live = validate(merge(DEFAULTS, object_value(global_row[0]) if global_row else {}, object_value(p[0])))
                raw = Evidence(db, run_id, LIFECYCLE)
                parsed = ParsedEvidence(raw)
                count = 0
                while state.prefix < len(LIFECYCLE):
                    prefix = LIFECYCLE[state.prefix]
                    key = prefix + run_id
                    rows = db.execute(
                        'SELECT CASE WHEN length(CAST(key AS BLOB))<=4096 THEN key END AS key '
                        'FROM settings WHERE (key=? OR (key>=? AND key<?)) AND key>? '
                        'ORDER BY key LIMIT ?', (key, key + ':', key + ';', state.cursor, VALIDATION_RECORDS)).fetchall()
                    if not rows:
                        state.prefix += 1
                        state.cursor = ''
                        continue
                    for entry in rows:
                        key = entry['key']
                        if key is None or len(key) > 4096:
                            raise ValueError('Oversized evidence key')
                        state.cursor = key
                        count += 1
                        if raw.owns(key, prefix):
                            value = raw[key]
                            _validation_boundary()
                            directory = None
                            if prefix in PREFIXES:
                                obj = object_value(value)
                                if not _record_valid(key, obj, parsed, run, run_id, now, live['limits']['retention_days']):
                                    raise ValueError('Invalid generation record')
                                if prefix in ('delivery-generation:', 'delivery-generation-history:', 'delivery-generation-preparing:'):
                                    directory = Path(obj['directory']).name
                            state.record(key, directory)
                        if count >= VALIDATION_RECORDS or raw.bytes_read >= VALIDATION_BYTES:
                            state.now = now
                            cache.save(state)
                            return None
                _validation_boundary()
                if not _lifecycle_closed(db, run_id, project, now, state):
                    raise ValueError('Lifecycle remains held')
                state.complete = True
                state.now = now
                cache.save(state)
                return state
        except (GenerationError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            cache.drop(run_id, project)
            return None


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
        fds, self.fds = self.fds, []
        error = None
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError as exc:
                error = exc
        if error is not None:
            raise error

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
    def __init__(self, store, run_id, project, snapshot, authorization):
        self.store, self.run_id, self.project = store, run_id, project
        self.snapshot, self.authorization = snapshot, authorization
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
            if not self.authorization.current(db):
                raise CleanupStopped('Cleanup authorization changed')
            self.check_chain(chain)
            action()

    def remove(self, parent, name, chain=(), expected=None, directory_only=False):
        """Streaming, bounded depth walk; never recursively delete under BEGIN."""
        self.visit(chain)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise CleanupStopped('Original artifact disappeared') from exc
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
    original path chain and the validated source revision under BEGIN IMMEDIATE.
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
    # A private disk index bounds RAM even with many registered directories.
    # Remember each original child inode before the mutation pass.
    with closing(sqlite3.connect('')) as children:
        children.execute('PRAGMA cache_size=-256')
        children.execute('CREATE TABLE original (name TEXT PRIMARY KEY, dev INTEGER, ino INTEGER)')
        with os.scandir(fd) as entries:
            for entry in entries:
                batch.snapshot.validate()
                info = entry.stat(follow_symlinks=False)
                if not batch.authorization.owns(entry.name) or not stat.S_ISDIR(info.st_mode):
                    return False
                children.execute('INSERT INTO original VALUES (?,?,?)', (entry.name, *_identity(info)))
        with os.scandir(fd) as entries:
            for entry in entries:
                identity = children.execute('SELECT dev,ino FROM original WHERE name=?', (entry.name,)).fetchone()
                if identity is None:
                    return False
                batch.remove(fd, entry.name, expected=tuple(identity), directory_only=True)
    # The pinned generations descriptor must stop participating in validate
    # only AFTER rmdir succeeded. Nothing mutates that snapshot before then.
    batch.mutate((), lambda: os.rmdir('generations', dir_fd=batch.snapshot.fds[2]))
    os.close(batch.snapshot.fds[-1])
    batch.snapshot.fds.pop()
    batch.snapshot.identities.pop()
    return True


def remove_run_artifacts(store, run_id, project, directory, snapshot, stored=None):
    """Generation-managed entry point; all interruption leaves journals intact."""
    from .maintenance import CLEANUP_NAMES, RUN_FILE_ARTIFACTS, _run_directory_settled
    try:
        snapshot.validate()
        proof = authorization(store, run_id, project)
        if proof is None:
            return False
        with store.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT path FROM runs WHERE id=?', (run_id,)).fetchone()
            if (not row or row['path'] != (stored if stored is not None else str(directory)) or
                    not proof.current(db)):
                return False
        batch = CleanupBatch(store, run_id, project, snapshot, proof)
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
            if not proof.current(db):
                return False
            row = db.execute('SELECT path FROM runs WHERE id=?', (run_id,)).fetchone()
            snapshot.validate()
            if len(snapshot.fds) >= 3 and not _run_directory_settled(snapshot.fds[2]):
                return False
            db.execute('UPDATE runs SET path=NULL WHERE id=? AND path=?', (run_id, row['path']))
        return True
    except (CleanupStopped, FileNotFoundError):
        return False


def retire_settings(store, now):
    """Validate outside locks; retire history/proof pairs in small transactions.

    No journal is removed until the ENTIRE lifecycle has passed verification.
    Core receipts survive each partial batch so the remaining history can be
    proved again. Pending/corrupt records retain all evidence indefinitely.
    """
    cursor_key = 'maintenance-generation-retirement-cursor'
    cursor = store.setting(cursor_key, '')
    if not isinstance(cursor, str):
        cursor = ''
    rows = store.query("SELECT id,project FROM runs WHERE state IN ('merged','failed','cancelled','expired') "
                       "AND path IS NULL AND ended<? AND id>? AND EXISTS (SELECT 1 FROM settings WHERE "
                       "key='delivery-generation:'||runs.id OR (key>='delivery-generation-preparing:'||runs.id||':' "
                       "AND key<'delivery-generation-preparing:'||runs.id||';') OR "
                       "(key>='delivery-generation-history:'||runs.id||':' "
                       "AND key<'delivery-generation-history:'||runs.id||';')) ORDER BY id LIMIT 8",
                       (now - 90 * 86400, cursor))
    store.put_setting(cursor_key, rows[-1]['id'] if len(rows) == 8 else '')
    for run in rows:
        proof = authorization(store, run['id'], run['project'], now)
        if proof is None:
            continue
        # Delete at most one page of non-core keys. History and its proof must
        # leave atomically, otherwise the next validation would correctly hold.
        core = {prefix + run['id'] for prefix in LIFECYCLE}
        keys = []
        # Reserve room to delete the fixed core in the last batch, including
        # orphan history-only runs that would otherwise disappear from scans.
        pairs = (VALIDATION_RECORDS - len(core)) // 2
        with _validation_lock:
            candidates = proof.cache.db.execute(
                'SELECT key FROM records WHERE run=? AND project=? AND revision=? AND key NOT IN (' +
                ','.join('?' for _ in core) + ") AND key NOT LIKE 'delivery-generation-history-proof:%' "
                'ORDER BY key LIMIT ?', (run['id'], run['project'], proof.revision, *sorted(core), pairs + 1)).fetchall()
        for (key,) in candidates[:pairs]:
            keys.append(key)
            if key.startswith('delivery-generation-history:'):
                keys.append('delivery-generation-history-proof:' + key[len('delivery-generation-history:'):])
        if len(candidates) <= pairs:
            keys.extend(sorted(core))
        _cleanup_boundary()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not proof.current(db):
                continue
            row = db.execute('SELECT path,ended FROM runs WHERE id=?', (run['id'],)).fetchone()
            if not row or row['path'] is not None or not _number(row['ended']) or row['ended'] >= now - 90 * 86400:
                continue
            for key in keys:
                db.execute('DELETE FROM settings WHERE key=?', (key,))
