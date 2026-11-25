"""Durable replay preparation and journaled publication of immutable generations.

Original run/base/candidate bindings stay immutable: runtime retries still verify
that original workspace. A generation is a separate evaluated commit and ref.
"""
import json
import re
import uuid
from pathlib import Path

from .config import fingerprint
from .delivery_approval import authority
from .delivery_replay_checks import (_acceptance, replay_on_advanced_baseline,
                                     sealed_dependency_digest)
from .gitops import git as _git
from .store import encode


class GenerationError(RuntimeError):
    pass


def git(repo, *args):
    return _git(repo, '--no-replace-objects', *args)


def _setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        raise GenerationError("Malformed generation identity: " + key) from None
    if not isinstance(value, dict) or not value:
        raise GenerationError("Malformed generation identity: " + key)
    return value


def _raw_setting(db, key):
    """Compare persisted bytes, including absent versus explicitly empty config."""
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return tuple(row) if row is not None else None


def _snapshot(db, run_id, approval_id):
    def row(sql, args):
        value = db.execute(sql, args).fetchone()
        return dict(value) if value else None
    run = row("SELECT id,task,project,state,base,result,config,path FROM runs WHERE id=?", (run_id,))
    if not run or run['state'] != 'awaiting_approval':
        raise GenerationError('Run is not awaiting approval')
    project = row('SELECT id,path,paused,config FROM projects WHERE id=?', (run['project'],))
    task = row('SELECT id,project,state,source,fingerprint,title,body,evidence,acceptance,overrides FROM tasks WHERE id=?', (run['task'],))
    approval = row('SELECT id,project,source,body,state,evidence FROM tasks WHERE id=?', (approval_id,))
    if (not project or project['paused'] or not task or task['project'] != run['project'] or
            task['state'] != 'awaiting_approval' or not approval or
            (approval['project'], approval['source'], approval['body'], approval['state']) !=
            (run['project'], 'system_publish', run_id, 'planning')):
        raise GenerationError('Generation authorization changed')
    baseline = _setting(db, 'run-baseline:' + run_id)
    binding = _setting(db, 'delivery-candidate:' + run_id)
    try:
        grants = json.loads(approval['evidence'])
        outcome = json.loads(run['result'])
        fields = ('task', 'project', 'base', 'result', 'config', 'path')
        config = json.loads(run['config'])
        # An empty setting resolves to the GitHub default at baseline capture.
        # Local recovery trusts that sealed target; a future remote writer must
        # resolve the live target again and compare it before any publication.
        configured_branch = config['delivery']['base_branch']
        prefix = 'fix' if task['source'] in ('issue', 'rollback', 'inspection') else 'feat'
        branch = prefix + '/rsi-' + re.sub(r'[^a-zA-Z0-9._-]', '-', run_id)
        keys = {'version', 'branch', 'head', 'tree', 'base', 'repository', 'evidence', 'approval', 'authorization'}
        # The live grant must be explicit. Version-1 approval fields retain the
        # first attempt's audit snapshot, as in delivery.publish.matches_binding.
        authorized_by = authority(grants[0]) if isinstance(grants, list) and len(grants) == 1 else None
        if (not authorized_by or (authorized_by == 'project_policy' and config['delivery']['mode'] != 'pr') or
                not binding or set(binding) != keys or
                type(binding['version']) is not int or binding['version'] != 1 or
                not isinstance(binding['approval'], str) or not binding['approval'] or
                not isinstance(binding['authorization'], str) or
                not re.fullmatch('[0-9a-f]{64}', binding['authorization']) or
                binding['branch'] != branch or
                binding['evidence'] != fingerprint({key: run[key] for key in fields}) or
                not baseline or set(baseline) != {'repository', 'branch', 'sha'} or
                any(not isinstance(baseline[key], str) or not baseline[key].strip()
                    for key in ('repository', 'branch', 'sha')) or
                baseline['repository'] != binding['repository'] or
                baseline['repository'] != config['github']['repository'] or
                baseline['sha'] != run['base'] or not re.fullmatch('[0-9a-f]{40,64}', baseline['sha']) or
                not isinstance(configured_branch, str) or
                (configured_branch and configured_branch != baseline['branch']) or
                binding['base'] != run['base'] or outcome['evaluation']['decision'] != 'pass' or
                outcome['review']['decision'] != 'pass'):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise GenerationError('Approved version-1 publication binding is missing or changed') from None
    for prefix in ('revision-pending:', 'issue-revision-pending:', 'run-stop:', 'delivery-update:'):
        if db.execute('SELECT 1 FROM settings WHERE key=?', (prefix + run_id,)).fetchone():
            raise GenerationError('Revision or stop is pending')
    # Push is intentionally outside this snapshot. Recovery validates it against
    # the generation rather than rejecting every attempt after a transient push.
    return {'run': run, 'project': project, 'task': task, 'approval': approval,
            'baseline': baseline, 'binding': binding}


def _commit(repo, sha, parent, tree):
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40,64}', sha):
        raise GenerationError('Invalid generation object ID')
    if (git(repo, 'rev-parse', '--verify', sha + '^{commit}') != sha or
            git(repo, 'rev-list', '--parents', '-n', '1', sha).split() != [sha, parent] or
            git(repo, 'rev-parse', sha + '^{tree}') != tree):
        raise GenerationError('Generation head, parent or tree changed')


def _original(snap):
    workspace = Path(snap['run']['path']) / 'candidate'
    binding = snap['binding']
    outcome = json.loads(snap['run']['result'])
    _commit(workspace, binding['head'], snap['run']['base'], binding['tree'])
    if (git(workspace, 'rev-parse', 'HEAD') != binding['head'] or
            git(workspace, 'rev-parse', binding['head'] + '^{tree}') != binding['tree'] or
            git(workspace, 'write-tree') != binding['tree'] or
            git(workspace, 'diff', 'HEAD', '--no-ext-diff', '--no-textconv') or
            git(workspace, 'ls-files', '--others', '--exclude-standard', '-z') or
            fingerprint(git(workspace, 'diff', snap['run']['base'], '--binary', '--no-ext-diff', '--no-textconv')) != outcome.get('patch_hash')):
        raise GenerationError('Original approved candidate changed')
    return workspace


def _environment(runtime, snap, config):
    run = snap['run']
    outcome = json.loads(run['result'])
    evaluation = outcome['evaluation']
    sealed = evaluation.get('source')
    try:
        identity = runtime.evaluator.execution_identity(config)
    except Exception:
        raise GenerationError('Generation execution environment cannot be verified') from None
    original_config = json.loads(run['config'])
    # Publishing policy may be revoked and then replaced by explicit approval.
    # These two delivery switches do not alter the sealed evaluator contract;
    # all execution, dependency, target and validation inputs must still match.
    sealed_policy = {key: original_config['delivery'][key] for key in ('mode', 'auto_merge')}
    comparable = {**config, 'delivery': {**config['delivery'], **sealed_policy}}
    if (runtime.is_cancelled(dict(run)) or comparable != original_config or
            config != runtime.config(run['project']) or
            not isinstance(sealed, dict) or
            not identity or not evaluation.get('execution_environment') or
            identity != evaluation['execution_environment'] or
            sealed.get('validation') != config.get('validation') or
            sealed_dependency_digest(Path(run['path']) / 'dependencies', config) != sealed.get('environment')):
        raise GenerationError('Generation execution or dependency evidence changed')
    _acceptance(Path(run['path']), evaluation, sealed)
    return fingerprint({'config': config, 'execution': evaluation['execution_environment'],
                        'dependencies': sealed['environment']})


def _push_binding(record, snap):
    return {'version': 2, 'generation': record['generation'], 'ref': record['ref'],
            'branch': snap['binding']['branch'], 'head': record['commit'], 'tree': record['tree'],
            'base': record['parent'], 'repository': snap['binding']['repository'],
            'approval': snap['approval']['id'], 'authorization': fingerprint(json.loads(snap['approval']['evidence'])),
            'evidence': record['evidence_hash'], 'environment': record['environment']}


def _check_push(db, record, snap):
    push = _setting(db, 'delivery-push:' + snap['run']['id'])
    if push is not None:
        expected = _push_binding(record, snap)
        if (set(push) not in (set(expected), set(expected) | {'created'}) or
                type(push.get('version')) is not int or
                any(push.get(key) != value for key, value in expected.items())):
            raise GenerationError('Push journal does not match the generation; reconcile before retrying')


def create_generation(runtime, project, run_id, approval_id, workspace, source, current, config, directory):
    """Prepare a replay using the real sealed evaluator, never a caller callback.

    No remote write, original run mutation, or push intent is performed here.
    `directory` must be a new caller-owned persistent directory.
    """
    with runtime.store.connect() as db:
        db.execute('BEGIN')
        snap = _snapshot(db, run_id, approval_id)
        global_config = _raw_setting(db, 'config')
        if (db.execute('SELECT 1 FROM settings WHERE key IN (?,?,?)',
                       ('delivery-generation:' + run_id, 'delivery-push:' + run_id, 'delivery-generation-pr:' + run_id)).fetchone()):
            raise GenerationError('Generation or unresolved push already exists')
    if project['id'] != snap['run']['project']:
        raise GenerationError('Generation project changed')
    if Path(workspace).resolve() != _original(snap).resolve():
        raise GenerationError('Original candidate directory changed')
    if (not isinstance(current, str) or not re.fullmatch('[0-9a-f]{40,64}', current) or
            current == snap['run']['base'] or
            git(source, 'rev-parse', '--verify', current + '^{commit}') != current or
            git(source, 'merge-base', snap['run']['base'], current) != snap['run']['base']):
        raise GenerationError('Baseline is not a verified advance')
    environment = _environment(runtime, snap, config)
    candidate, evaluation = replay_on_advanced_baseline(
        runtime, project, dict(snap['run']), workspace, json.loads(snap['run']['result']),
        source, current, config, directory)
    tree = evaluation['source'].get('tree')
    if (not isinstance(tree, str) or not re.fullmatch('[0-9a-f]{40,64}', tree) or
            git(candidate, 'write-tree') != tree):
        raise GenerationError('Replay evaluated tree changed before registration')
    if (git(candidate, 'diff', '--no-ext-diff', '--no-textconv') or
            git(candidate, 'ls-files', '--others', '--exclude-standard', '-z')):
        raise GenerationError('Replay evaluator changed the worktree')
    # Only the evaluated immutable tree may become the durable commit.
    commit = git(candidate, '-c', 'user.name=Adaptive RSI', '-c', 'user.email=adaptive-rsi@localhost',
                 'commit-tree', tree, '-p', current, '-m', '持久保存跨基线重放候选')
    patch_hash = fingerprint(git(candidate, 'diff', current, commit, '--binary', '--no-ext-diff', '--no-textconv'))
    if patch_hash != evaluation['source'].get('patch_hash'):
        raise GenerationError('Generation commit patch differs from evaluated evidence')
    generation = uuid.uuid4().hex
    ref = 'refs/adaptive-rsi/generations/' + generation
    git(candidate, 'update-ref', ref, commit, '0' * len(commit))
    record = {'version': 1, 'generation': generation, 'ref': ref, 'commit': commit,
              'parent': current, 'tree': tree, 'approval': approval_id,
              'authorization': fingerprint(json.loads(snap['approval']['evidence'])),
              'directory': str(Path(directory).resolve()), 'snapshot': fingerprint(snap),
              'environment': environment, 'evaluation': evaluation,
              'evidence_hash': fingerprint(evaluation),
              'patch_hash': patch_hash}
    # External writers do not honor SQLite locks. Retain the final checks, but
    # never run Docker, dependency/file reads or Git under the database write lock.
    # The durable payload binds only the evaluated immutable tree and commit.
    if _environment(runtime, snap, config) != environment:
        raise GenerationError('Generation evidence changed during evaluation')
    _original(snap)
    _commit(candidate, commit, current, tree)
    if git(candidate, 'rev-parse', '--verify', ref) != commit:
        raise GenerationError('Generation ref changed before registration')
    if (git(candidate, 'write-tree') != tree or
            git(candidate, 'diff', '--no-ext-diff', '--no-textconv') or
            git(candidate, 'ls-files', '--others', '--exclude-standard', '-z')):
        raise GenerationError('Replay evaluated tree changed before registration')
    payload = encode(record)
    with runtime.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if (_snapshot(db, run_id, approval_id) != snap or
                _raw_setting(db, 'config') != global_config):
            raise GenerationError('Generation evidence changed during evaluation')
        if db.execute('SELECT 1 FROM settings WHERE key IN (?,?,?)',
                      ('delivery-generation:' + run_id, 'delivery-push:' + run_id, 'delivery-generation-pr:' + run_id)).fetchone():
            raise GenerationError('Generation or push appeared during evaluation')
        if runtime.stop.is_set():
            raise GenerationError('Generation stopped before registration')
        db.execute('INSERT INTO settings(key,value) VALUES (?,?)', ('delivery-generation:' + run_id, payload))
    recover_generation(runtime, run_id, generation, config)
    return record


def recover_generation(runtime, run_id, generation, config):
    """Recover the same generation, allowing only its exact version-2 push intent."""
    try:
        with runtime.store.connect() as db:
            db.execute('BEGIN')
            record = _setting(db, 'delivery-generation:' + run_id)
            if (not record or type(record['version']) is not int or record['version'] != 1 or
                    not re.fullmatch('[0-9a-f]{32}', generation) or record['generation'] != generation or
                    record['ref'] != 'refs/adaptive-rsi/generations/' + generation):
                raise GenerationError('Generation identity is invalid')
            snap = _snapshot(db, run_id, record['approval'])
            global_config = _raw_setting(db, 'config')
            generation_value = _raw_setting(db, 'delivery-generation:' + run_id)
            push_value = _raw_setting(db, 'delivery-push:' + run_id)
            _check_push(db, record, snap)
        # Release the consistent read snapshot before any external validation.
        _original(snap)
        if fingerprint(snap) != record['snapshot'] or _environment(runtime, snap, config) != record['environment']:
            raise GenerationError('Generation authorization or environment evidence changed; prepare a new generation')
        candidate = Path(record['directory']) / 'candidate'
        _commit(candidate, record['commit'], record['parent'], record['tree'])
        if git(candidate, 'rev-parse', '--verify', record['ref']) != record['commit']:
            raise GenerationError('Generation ref changed')
        if (git(candidate, 'rev-parse', 'HEAD') != record['parent'] or
                git(candidate, 'write-tree') != record['tree'] or
                git(candidate, 'diff', '--no-ext-diff', '--no-textconv') or
                git(candidate, 'ls-files', '--others', '--exclude-standard', '-z')):
            raise GenerationError('Generation workspace changed')
        evaluation = json.loads((Path(record['directory']) / 'replay-evidence.json').read_text())
        # Legacy patch hashes may describe a tree staged after evaluation.
        # This unpublished prototype requires fresh preparation, not migration
        # by copying the registered tree into old evaluation evidence.
        if evaluation.get('source') is None:
            raise GenerationError('Generation evaluation evidence is missing')
        if not evaluation['source'].get('tree'):
            raise GenerationError('Generation evaluated tree is missing; prepare a new generation')
        if (evaluation != record['evaluation'] or evaluation.get('decision') != 'pass' or
                fingerprint(evaluation) != record['evidence_hash'] or
                evaluation['source'].get('base') != record['parent'] or
                evaluation['source'].get('tree') != record['tree'] or
                evaluation['source'].get('patch_hash') != record['patch_hash'] or
                evaluation.get('execution_environment') != json.loads(snap['run']['result'])['evaluation']['execution_environment'] or
                sealed_dependency_digest(Path(record['directory']) / 'dependencies', config) !=
                evaluation['source']['environment'] or
                fingerprint(git(candidate, 'diff', record['parent'], record['commit'], '--binary', '--no-ext-diff', '--no-textconv')) != record['patch_hash']):
            raise GenerationError('Generation evaluation evidence changed')
        with runtime.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if (_snapshot(db, run_id, record['approval']) != snap or
                    _raw_setting(db, 'config') != global_config or
                    _raw_setting(db, 'delivery-generation:' + run_id) != generation_value or
                    _raw_setting(db, 'delivery-push:' + run_id) != push_value):
                raise GenerationError('Generation evidence changed during recovery')
            _check_push(db, record, snap)
            if runtime.stop.is_set():
                raise GenerationError('Generation stopped before recovery')
            return record
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise GenerationError('Generation evidence is invalid') from exc


def journal_policy(store, run, intent):
    """Validate a v2 receipt without live approval or a surviving workspace.

    Superseded runs must still observe remote facts after authorization is gone.
    This function grants no authority to write a branch or create a PR.
    """
    with store.connect() as db:
        return journal_policy_db(db, run, intent)


def journal_policy_db(db, run, intent):
    """Same receipt validation on the caller's consistent transaction."""
    try:
        record = _setting(db, 'delivery-generation:' + run['id'])
        binding = _setting(db, 'delivery-candidate:' + run['id'])
        baseline = _setting(db, 'run-baseline:' + run['id'])
        expected = {'version': 2, 'generation': record['generation'], 'ref': record['ref'],
                    'branch': binding['branch'], 'head': record['commit'], 'tree': record['tree'],
                    'base': record['parent'], 'repository': binding['repository'],
                    'approval': record['approval'], 'authorization': record['authorization'],
                    'evidence': record['evidence_hash'], 'environment': record['environment']}
        if (type(intent.get('version')) is not int or
                set(intent) not in (set(expected), set(expected) | {'created'}) or
                any(intent.get(k) != v for k, v in expected.items()) or
                baseline != {'repository': binding['repository'], 'branch': baseline['branch'], 'sha': run['base']} or
                binding['base'] != run['base'] or
                record['ref'] != 'refs/adaptive-rsi/generations/' + record['generation'] or
                fingerprint(record['evaluation']) != record['evidence_hash'] or
                record['evaluation']['source']['base'] != record['parent'] or
                record['evaluation']['source']['tree'] != record['tree']):
            raise GenerationError('Generation journal identity differs')
        return {'head': record['commit'], 'base': record['parent'],
                'branch': baseline['branch'], 'repository': binding['repository']}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GenerationError('Generation receipt evidence is invalid') from exc


def _publication_root(runtime, run):
    """Only managed run directories may own replay publication artifacts."""
    root = runtime.store.root / 'runs'
    directory = Path(run['path'])
    if (Path(run['id']).parts != (run['id'],) or run['id'] in ('.', '..') or
            root.is_symlink() or directory.is_symlink() or
            directory.resolve() != root.resolve() / run['id']):
        raise GenerationError('Generation publication requires a managed run directory')
    generations = directory / 'generations'
    if generations.is_symlink():
        raise GenerationError('Generation directory is a symlink')
    generations.mkdir(exist_ok=True)
    return generations.resolve()


def prepare_publication(runtime, project, run, approval_id, workspace, source, current, config):
    # Each attempt has a fresh name. A crash before registration leaves evidence,
    # never a directory that a later attempt can adopt as evaluated output.
    # The journal proves ownership even if evaluation dies before registration.
    # A directory lock spans external evaluation without holding a SQLite lock.
    import os
    import time
    from .generation_retention import managed_directory, fcntl
    if fcntl is None or not all(hasattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW')):
        raise GenerationError('Generation preparation requires no-follow directory locking')
    root = _publication_root(runtime, run)
    directory = root / uuid.uuid4().hex
    with managed_directory(runtime.store, run['id']) as parent:
        fd = os.open('generations', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with runtime.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                _snapshot(db, run['id'], approval_id)
                if _raw_setting(db, 'delivery-generation-pr:' + run['id']) is not None:
                    raise GenerationError('Unresolved PR receipt blocks generation preparation')
                db.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                    ('delivery-generation-preparing:' + run['id'] + ':' + directory.name,
                     encode({'version': 1, 'directory': str(directory), 'created': time.time()})))
            return create_generation(runtime, project, run['id'], approval_id, workspace,
                                     source, current, config, directory)
        finally:
            os.close(fd)


def retire_unpublished_generation(store, run_id, record):
    key = 'delivery-generation:' + run_id
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if (_setting(db, key) != record or _raw_setting(db, 'delivery-push:' + run_id) is not None or
                _raw_setting(db, 'delivery-generation-pr:' + run_id) is not None):
            raise GenerationError('Generation changed or has an unresolved write; reconcile before reapproval')
        suffix = run_id + ':' + record['generation']
        db.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                   ('delivery-generation-history:' + suffix, encode(record)))
        db.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                   ('delivery-generation-history-proof:' + suffix,
                    encode({'unpublished': True, 'record': fingerprint(record)})))
        db.execute('DELETE FROM settings WHERE key=?', (key,))


def publish_generation(runtime, project, task, run, config, approval_id, authorize_write, *, version_bound=False):
    """Publish a sealed replay through the existing runtime approval boundary.

    Version-2 intents are immutable. Every write verifies live authority, both
    candidate identities, the resolved remote target and the fetched baseline.
    Receipt recovery is read-only and may observe an already completed write.
    """
    from . import delivery
    import time
    store = runtime.store
    key = 'delivery-generation:' + run['id']
    push_key = 'delivery-push:' + run['id']
    with store.connect() as db:
        record = _setting(db, key)
        push = _setting(db, push_key)
    if not record:
        raise GenerationError('Generation record is missing')
    # Explicit new approval can replace only an unpublished generation. Preserve
    # the old record and files for audit; an uncertain push always blocks this.
    if record.get('approval') != approval_id:
        if push is not None or store.one('SELECT 1 FROM settings WHERE key=?', ('delivery-generation-pr:' + run['id'],)):
            raise GenerationError('Old generation has an unresolved push or PR receipt; reconcile before reapproval')
        authorize_write()
        source, current, branch = delivery.baseline(runtime, project, config)
        identity = store.setting('run-baseline:' + run['id'])
        if identity != {'repository': config['github']['repository'], 'branch': branch, 'sha': run['base']}:
            raise GenerationError('Generation target changed')
        retire_unpublished_generation(store, run['id'], record)
        record = prepare_publication(runtime, project, run, approval_id,
                                     Path(run['path']) / 'candidate', source, current, config)
    generation = record.get('generation')
    record = recover_generation(runtime, run['id'], generation, config)
    directory = Path(record['directory'])
    owned = _publication_root(runtime, run)
    if directory.is_symlink() or directory.resolve().parent != owned:
        raise GenerationError('Generation workspace is outside the managed run')
    candidate = directory / 'candidate'
    repo = config['github']['repository']
    identity = store.setting('run-baseline:' + run['id'])
    target = identity['branch']
    with store.connect() as db:
        snap = _snapshot(db, run['id'], approval_id)
    intent = _push_binding(record, snap)
    branch = intent['branch']

    def verify(require_remote=False):
        authorize_write()
        if delivery.origin_repository(runtime.project(project['id'])['path']).lower() != repo.lower():
            raise GenerationError('Generation repository changed')
        _, current, resolved = delivery.baseline(runtime, runtime.project(project['id']), config)
        if resolved != target or current != record['parent']:
            raise GenerationError('Generation target or baseline changed; reapproval and new replay required')
        if recover_generation(runtime, run['id'], generation, config) != record:
            raise GenerationError('Generation identity changed')
        remote_ref = 'refs/heads/' + branch
        observed = delivery.github_git(candidate, 'ls-remote', '--heads',
                                       'https://github.com/' + repo + '.git', remote_ref).splitlines()
        expected_remote = record['commit'] + '\t' + remote_ref
        if observed not in ([], [expected_remote]) or (require_remote and observed != [expected_remote]):
            raise GenerationError('Remote generation branch differs; reconcile before retrying')
        authorize_write()

    # Full identity inspection, including repository ownership, prevents a PR
    # from a fork or a different head from masquerading as our lost receipt.
    records = json.loads(delivery.checked(['gh', 'pr', 'list', '--repo', repo, '--state', 'all',
        '--head', branch, '--limit', '100', '--json', 'url']))
    if not isinstance(records, list) or len(records) >= 100 or len(records) > 1:
        raise GenerationError('Ambiguous or incomplete generation PR inventory')
    receipt_key = 'delivery-generation-pr:' + run['id']
    receipt = store.setting(receipt_key)
    if receipt is not None and (not isinstance(receipt, dict) or
            receipt.get('generation') != generation or not isinstance(receipt.get('url'), str)):
        raise GenerationError('Generation PR receipt is invalid; reconcile before retrying')
    url = None
    if records or receipt:
        url = receipt['url'] if receipt else records[0]['url']
        if records and records[0]['url'] != url:
            raise GenerationError('Generation PR inventory differs from saved receipt')
        if not isinstance(url, str) or not re.fullmatch(
                r'https://github\.com/' + re.escape(repo) + r'/pull/[1-9][0-9]*', url, re.I):
            raise GenerationError('Generation PR URL does not match repository')
        # A PR receipt must have a preceding, exactly matching push intent.
        with store.connect() as db:
            if _setting(db, push_key) is None:
                raise GenerationError('Generation PR has no local push intent')
            _check_push(db, record, snap)
    else:
        if version_bound and push is None:
            authorize_write()
            source, current, resolved = delivery.baseline(runtime, project, config)
            if resolved != target:
                raise GenerationError('Generation target or baseline changed; reapproval and new replay required')
            if current != record['parent']:
                if git(source, 'merge-base', record['parent'], current) != record['parent']:
                    raise GenerationError('Generation target or baseline changed; reapproval and new replay required')
                retire_unpublished_generation(store, run['id'], record)
                prepare_publication(runtime, project, run, approval_id,
                                    Path(run['path']) / 'candidate', source, current, config)
                raise GenerationError('Replay prepared on a new baseline; approve the new candidate before publishing')
        with runtime.dispatch_lock:
            verify()
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if (_snapshot(db, run['id'], approval_id) != snap or _setting(db, key) != record):
                    raise GenerationError('Generation approval changed before push intent')
                _check_push(db, record, snap)
                if _raw_setting(db, push_key) is None:
                    db.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                               (push_key, encode({**intent, 'created': time.time()})))
            verify()
            delivery.github_git(candidate, 'push', 'https://github.com/' + repo + '.git',
                                record['commit'] + ':refs/heads/' + branch)
        outcome = json.loads(run['result'])
        body = directory / 'pr-body.md'
        # The PR describes the replay gates; the original run evidence remains
        # untouched and continues to validate every runtime retry.
        body.write_text(delivery.pr_body(task, {**run, 'base': record['parent']},
            {**outcome, 'evaluation': record['evaluation'], 'patch_hash': record['patch_hash']}), encoding='utf-8')
        with runtime.dispatch_lock:
            verify(require_remote=True)
            url = delivery.checked(['gh', 'pr', 'create', '--repo', repo, '--head', branch,
                '--base', target, '--title', '完成任务：' + task['title'], '--body-file', str(body)])
        if not isinstance(url, str) or not re.fullmatch(
                r'https://github\.com/' + re.escape(repo) + r'/pull/[1-9][0-9]*', url, re.I):
            raise GenerationError('Generation PR response is invalid; recover receipt before retrying')
    # Keep an unverified receipt separate from successful delivery state. A
    # failed identity read must retry this URL even if PR inventory is delayed.
    receipt = {**(receipt or {}), 'generation': generation, 'url': url}
    store.put_setting(receipt_key, receipt)
    try:
        pr = delivery.pr_view(repo, url)
        store.put_setting(receipt_key, {**receipt, 'observation': pr})
        if (pr.get('url') != url or pr.get('state') not in ('OPEN', 'MERGED') or
                not delivery._update_pr_identity({**pr, 'state': 'OPEN'}, repo, target, branch, record['commit'])):
            raise GenerationError('Generation PR identity differs or is closed; reconcile saved receipt')
    except Exception as exc:
        observed = store.setting(receipt_key)
        store.put_setting(receipt_key, {**observed, 'error': str(exc)[:500]})
        raise
    # Save observed facts atomically, even when supersession raced with the
    # remote response. Runtime then completes the system_publish job as usual.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _setting(db, key) != record:
            raise GenerationError('Generation record changed before receipt')
        _check_push(db, record, snap)
        row = db.execute('SELECT state,result FROM runs WHERE id=?', (run['id'],)).fetchone()
        outcome = delivery._json_object(row['result'])
        if outcome.get('pr') not in (None, url):
            raise GenerationError('Run already records another PR')
        outcome.update(pr=url, delivery_generation=generation)
        state = 'pr_open' if row['state'] in ('awaiting_approval', 'pr_open') else 'superseded'
        if state == 'superseded':
            outcome.update(replanned=True, close_pending=True)
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('delivery:' + run['id'], encode(
            {'head': record['commit'], 'base': record['parent'], 'branch': target, 'repository': repo})))
        db.execute('UPDATE runs SET state=?,result=? WHERE id=?', (state, encode(outcome), run['id']))
        if state == 'pr_open':
            db.execute("UPDATE tasks SET state='pr_open',updated=? WHERE id=? AND state='awaiting_approval'",
                       (time.time(), task['id']))
    store.event('pr', 'Generation PR receipt recorded', project['id'], run['id'],
                {'url': url, 'head': record['commit'], 'generation': generation})
    return url


def record_verified_merge(db, run, policy, pr):
    """Persist full remote merge identity atomically with a tracking transition.

    Return False on any ambiguity; never promote an unchecked publication URL.
    This helper performs database reads and pure comparisons only.
    """
    from .delivery import _update_pr_identity
    try:
        record = _setting(db, 'delivery-generation:' + run['id'])
        if not record:
            return False
        intent = _setting(db, 'delivery-push:' + run['id'])
        receipt = _setting(db, 'delivery-generation-pr:' + run['id'])
        if (not receipt or receipt.get('generation') != record['generation'] or
                receipt.get('url') != pr.get('url') or pr.get('state') != 'MERGED' or
                not isinstance(pr.get('mergeCommit'), dict) or
                not re.fullmatch('[0-9a-f]{40,64}', pr['mergeCommit'].get('oid', '')) or
                journal_policy_db(db, run, intent) != policy or
                not re.fullmatch(r'https://github\.com/' + re.escape(policy['repository']) + r'/pull/[1-9][0-9]*', receipt['url'], re.I) or
                not _update_pr_identity({**pr, 'state': 'OPEN'}, policy['repository'],
                                        policy['branch'], intent['branch'], policy['head'])):
            return False
        receipt.pop('error', None)
        receipt['observation'] = pr
        receipt['verified_merge'] = {'observation': pr, 'binding': fingerprint(
            {'generation': record, 'intent': intent, 'policy': policy})}
        db.execute('UPDATE settings SET value=? WHERE key=?',
                   (encode(receipt), 'delivery-generation-pr:' + run['id']))
        return True
    except (GenerationError, KeyError, TypeError, ValueError, AttributeError):
        return False
