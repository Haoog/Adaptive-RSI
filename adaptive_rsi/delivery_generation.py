"""Durable local replay preparation. Remote publication is deliberately gated.

Original run/base/candidate bindings stay immutable: runtime retries still verify
that original workspace. A generation is a separate evaluated commit and ref.
"""
import json
import re
import uuid
from pathlib import Path

from .config import fingerprint
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
        keys = {'version', 'branch', 'head', 'tree', 'base', 'repository', 'evidence', 'approval', 'authorization'}
        if (grants != [{'authorization': 'per_candidate'}] or not binding or set(binding) != keys or
                type(binding['version']) is not int or binding['version'] != 1 or
                binding['approval'] != approval_id or binding['authorization'] != fingerprint(grants) or
                binding['evidence'] != fingerprint({key: run[key] for key in fields}) or
                baseline != {'repository': binding['repository'], 'branch': json.loads(run['config'])['delivery']['base_branch'], 'sha': run['base']} or
                binding['base'] != run['base'] or outcome['evaluation']['decision'] != 'pass' or
                outcome['review']['decision'] != 'pass'):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise GenerationError('Approved version-1 publication binding is missing or changed') from None
    for prefix in ('revision-pending:', 'issue-revision-pending:', 'run-stop:'):
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
    if (runtime.is_cancelled(dict(run)) or config != json.loads(run['config']) or
            config != runtime.config(run['project']) or
            not isinstance(sealed, dict) or
            runtime.evaluator.execution_identity(config) != evaluation.get('execution_environment') or
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
            'approval': snap['approval']['id'], 'authorization': snap['binding']['authorization'],
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
        db.execute('BEGIN IMMEDIATE')
        snap = _snapshot(db, run_id, approval_id)
        if (db.execute('SELECT 1 FROM settings WHERE key IN (?,?)',
                       ('delivery-generation:' + run_id, 'delivery-push:' + run_id)).fetchone()):
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
    tree = git(candidate, 'write-tree')
    if (git(candidate, 'diff', '--no-ext-diff', '--no-textconv') or
            git(candidate, 'ls-files', '--others', '--exclude-standard', '-z')):
        raise GenerationError('Replay evaluator changed the worktree')
    # Re-run index/worktree identity after evaluation; commit-tree avoids hooks
    # or a mutable checkout becoming the durable identity.
    commit = git(candidate, '-c', 'user.name=Adaptive RSI', '-c', 'user.email=adaptive-rsi@localhost',
                 'commit-tree', tree, '-p', current, '-m', '持久保存跨基线重放候选')
    generation = uuid.uuid4().hex
    ref = 'refs/adaptive-rsi/generations/' + generation
    git(candidate, 'update-ref', ref, commit, '0' * len(commit))
    record = {'version': 1, 'generation': generation, 'ref': ref, 'commit': commit,
              'parent': current, 'tree': tree, 'approval': approval_id,
              'directory': str(Path(directory).resolve()), 'snapshot': fingerprint(snap),
              'environment': environment, 'evaluation': evaluation,
              'evidence_hash': fingerprint(evaluation),
              'patch_hash': fingerprint(git(candidate, 'diff', current, commit, '--binary', '--no-ext-diff', '--no-textconv'))}
    with runtime.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _snapshot(db, run_id, approval_id) != snap or _environment(runtime, snap, config) != environment:
            raise GenerationError('Generation evidence changed during evaluation')
        if db.execute('SELECT 1 FROM settings WHERE key IN (?,?)',
                      ('delivery-generation:' + run_id, 'delivery-push:' + run_id)).fetchone():
            raise GenerationError('Generation or push appeared during evaluation')
        _original(snap)
        _commit(candidate, commit, current, tree)
        if git(candidate, 'rev-parse', '--verify', ref) != commit:
            raise GenerationError('Generation ref changed before registration')
        db.execute('INSERT INTO settings(key,value) VALUES (?,?)', ('delivery-generation:' + run_id, encode(record)))
    recover_generation(runtime, run_id, generation, config)
    return record


def recover_generation(runtime, run_id, generation, config):
    """Recover the same generation, allowing only its exact version-2 push intent."""
    try:
        with runtime.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = _setting(db, 'delivery-generation:' + run_id)
            if (not record or type(record['version']) is not int or record['version'] != 1 or
                    not re.fullmatch('[0-9a-f]{32}', generation) or record['generation'] != generation or
                    record['ref'] != 'refs/adaptive-rsi/generations/' + generation):
                raise GenerationError('Generation identity is invalid')
            snap = _snapshot(db, run_id, record['approval'])
            _original(snap)
            if fingerprint(snap) != record['snapshot'] or _environment(runtime, snap, config) != record['environment']:
                raise GenerationError('Generation authorization or environment evidence changed')
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
            if (evaluation != record['evaluation'] or evaluation.get('decision') != 'pass' or
                    fingerprint(evaluation) != record['evidence_hash'] or
                    sealed_dependency_digest(Path(record['directory']) / 'dependencies', config) !=
                    evaluation['source']['environment'] or
                    fingerprint(git(candidate, 'diff', record['parent'], record['commit'], '--binary', '--no-ext-diff', '--no-textconv')) != record['patch_hash']):
                raise GenerationError('Generation evaluation evidence changed')
            _check_push(db, record, snap)
            return record
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise GenerationError('Generation evidence is invalid') from exc
