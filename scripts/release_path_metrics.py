#!/usr/bin/env python3
"""Measure completed release attempts through read-only GitHub Actions REST calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import re
from statistics import median
import subprocess
from urllib.parse import urlencode


WORKFLOWS = ('release.yml', 'release-verify.yml')
REBUILD_JOBS = ('tests', 'node', 'browser', 'build')
REUSE_STEPS = {
    'source_and_asset_proof': '核对源树、版本与全部资产字节',
    'provenance': '验证 GitHub provenance 与可信签名工作流',
    'dmg_and_checksum': '独立复验安装包与校验和',
    'zip_wheel_smoke': '正式发布前独立 ZIP 与 wheel smoke',
}
PACKAGE_STEPS = {
    'desktop_build_and_checks': '构建、验证并收集桌面安装包',
    'install_upgrade_smoke': ('完整安装与升级 smoke', '最终安装与升级 smoke'),
}


def seconds(start, end):
    try:
        delta = datetime.fromisoformat(end.replace('Z', '+00:00')) - datetime.fromisoformat(
            start.replace('Z', '+00:00'))
        return int(delta.total_seconds()) if delta.total_seconds() >= 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def gh_get(endpoint, key=None, **query):
    command = ['gh', 'api', '--method', 'GET', endpoint]
    if key:
        command[-1] += '?' + urlencode({'per_page': 100, **query})
        command.extend(['--paginate', '--slurp'])
    response = json.loads(subprocess.run(command, check=True, stdout=subprocess.PIPE,
                                         text=True, timeout=120).stdout)
    if not key:
        return response
    rows = [row for page in response for row in page[key]]
    if len(rows) != response[0]['total_count']:
        raise ValueError(f'Incomplete {key} pagination for {endpoint}: {len(rows)} rows')
    return rows


def attempt_origin(run, run_created_at, prior_runs):
    if run['run_attempt'] == 1:
        return run_created_at if seconds(run_created_at, run_created_at) is not None else None
    started = run.get('run_started_at')
    prior_starts = [previous.get('run_started_at') for previous in prior_runs]
    if (seconds(run_created_at, started) or 0) > 0 and all(
            (seconds(previous, started) or 0) > 0
            for previous in prior_starts if seconds(previous, previous) is not None):
        return started
    return None


def describe_job(job, step_names, rerun_start):
    result = {key: job.get(key) for key in ('id', 'html_url', 'conclusion', 'runner_id', 'created_at',
                                            'started_at', 'completed_at')}
    # Selective reruns can get a new job ID and created_at while carrying the
    # previous attempt's completed/started timestamps and step results.
    inherited = (seconds(result['completed_at'], rerun_start) or 0) > 0
    timed = not inherited and result['conclusion'] != 'skipped'
    result['inherited'] = inherited
    result['queue_seconds'] = (seconds(result['created_at'], result['started_at'])
                               if timed and result['runner_id'] else None)
    result['job_window_seconds'] = (seconds(result['started_at'], result['completed_at']) if timed else None)
    result['job_seconds'] = result['job_window_seconds'] if result['runner_id'] else None
    steps = job.get('steps', [])
    result['first_failed_step'] = next((row['name'] for row in steps if row['conclusion'] == 'failure'), None)
    if step_names:
        result['steps'] = {}
        for label, names in step_names.items():
            names = (names,) if isinstance(names, str) else names
            step = next((row for row in steps if row['name'] in names), None)
            result['steps'][label] = (dict(conclusion=step['conclusion'], seconds=seconds(
                step.get('started_at'), step.get('completed_at'))
                if timed and step['conclusion'] != 'skipped' else None) if step else None)
    return result


def measure(workflow, run, jobs, prior_runs=()):
    # The exact-attempt endpoint may return a refreshed created_at on reruns.
    first = run if run['run_attempt'] == 1 else next(
        (previous for previous in prior_runs if previous['run_attempt'] == 1), {})
    run_created_at = first.get('created_at')
    origin = attempt_origin(run, run_created_at, prior_runs)
    rerun_start = origin if run['run_attempt'] > 1 else None
    step_names = {'reuse': REUSE_STEPS, 'build': PACKAGE_STEPS, 'verify': PACKAGE_STEPS}
    by_name = {job['name']: describe_job(job, step_names.get(job['name']), rerun_start) for job in jobs}
    conclusions = {name: job['conclusion'] for name, job in by_name.items() if not job['inherited']}
    verify = workflow == 'release-verify.yml'
    publish = by_name.get('publish') or {}
    prepare = by_name.get('prepare') or {}
    reuse = by_name.get('reuse') or {}
    # Jobs omitted on selective reruns do not establish that reuse was skipped.
    fallback = [by_name[name] for name in REBUILD_JOBS
                if conclusions.get(name) not in (None, 'skipped')]
    if verify:
        path = 'exact_head_verify' if conclusions.get('verify') not in (None, 'skipped') else 'no_new_verify'
    elif conclusions.get('reuse') == 'success':
        path = 'reuse_success'
    elif fallback and conclusions.get('reuse') == 'skipped' and 'build' in by_name:
        path = 'no_reusable_artifact_rebuild'
    elif fallback and conclusions.get('reuse') in ('failure', 'timed_out'):
        path = 'revalidation_failed_rebuild'
    elif fallback and conclusions.get('reuse') == 'cancelled':
        path = 'revalidation_cancelled_rebuild'
    elif fallback or conclusions.get('reuse') not in (None, 'skipped'):
        path = 'incomplete_path_evidence'
    else:
        path = 'no_publish_path_observed'
    critical = None
    fallback_seconds = None
    if path in ('no_reusable_artifact_rebuild', 'revalidation_failed_rebuild',
                'revalidation_cancelled_rebuild'):
        # The pre-parallel workflow depended on build alone. In newer runs the
        # publish job proves every required rebuild job finished successfully.
        if publish.get('job_seconds') is not None and all(job['completed_at'] for job in fallback):
            critical = max(fallback, key=lambda job: job['completed_at'])
            trigger = prepare if path == 'no_reusable_artifact_rebuild' else reuse
            fallback_seconds = seconds(trigger.get('completed_at'), critical['completed_at'])
    gate = by_name.get('gate' if verify else 'prepare') or {}
    entry_start = gate['started_at'] if gate.get('job_window_seconds') is not None else None
    candidate = re.search(r'@([a-f0-9]{40})$', run.get('display_title') or '') if verify else None
    return {
        'run_id': run['id'], 'attempt': run['run_attempt'], 'workflow': workflow,
        'url': run['html_url'] + '/attempts/' + str(run['run_attempt']),
        'workflow_head': run['head_sha'], 'candidate_head_in_title': candidate.group(1) if candidate else None,
        'attempt_created_at': run.get('created_at'), 'attempt_started_at': run.get('run_started_at'),
        'run_created_at': run_created_at, 'attempt_origin_at': origin,
        'conclusion': run['conclusion'], 'path': path, 'jobs': by_name,
        'attempt_to_entry_seconds': seconds(origin, entry_start),
        'run_to_entry_seconds': seconds(run_created_at, entry_start),
        'prepare_to_publish_seconds': seconds(prepare.get('started_at'), publish.get('completed_at'))
        if prepare.get('job_window_seconds') is not None and publish.get('job_seconds') is not None else None,
        'rebuild_critical_job': next((name for name in REBUILD_JOBS if by_name.get(name) is critical), None)
        if critical else None,
        'rebuild_gate_seconds': fallback_seconds,
    }


def fetch_run(endpoint, workflow, run_id, attempt):
    run = gh_get(endpoint)
    observed_workflow = run['path'].split('@', 1)[0].rsplit('/', 1)[-1]
    if (run['status'] != 'completed' or run['run_attempt'] != attempt or run['id'] != run_id or
            observed_workflow not in WORKFLOWS or workflow not in (None, observed_workflow)):
        raise ValueError(f'Unusable attempt record {run_id}/{attempt}')
    return observed_workflow, run


def fetch_attempt(repo, workflow, run_id, attempt):
    base = f'repos/{repo}/actions/runs/{run_id}/attempts'
    endpoint = f'{base}/{attempt}'
    observed_workflow, run = fetch_run(endpoint, workflow, run_id, attempt)
    jobs = gh_get(endpoint + '/jobs', 'jobs')
    if any(job['run_attempt'] != attempt or job['run_id'] != run_id for job in jobs):
        raise ValueError(f'Mixed job attempts in {run_id}/{attempt}')
    prior_runs = [fetch_run(f'{base}/{previous}', observed_workflow, run_id, previous)[1]
                  for previous in range(1, attempt)]
    return measure(observed_workflow, run, jobs, prior_runs)


def distribution(values):
    measured = [value for value in values if value is not None]
    return {'n': len(measured), 'missing': len(values) - len(measured),
            'min': min(measured, default=None), 'median': median(measured) if measured else None,
            'max': max(measured, default=None)}


def summarize(rows):
    result = {}
    for path in sorted({row['path'] for row in rows} | {
            'reuse_success', 'no_reusable_artifact_rebuild', 'revalidation_failed_rebuild', 'exact_head_verify'}):
        group = [row for row in rows if row['path'] == path]
        metrics = {'runs': len({row['run_id'] for row in group}), 'attempts': len(group)}
        for key in ('attempt_to_entry_seconds', 'run_to_entry_seconds',
                    'prepare_to_publish_seconds', 'rebuild_gate_seconds'):
            metrics[key] = distribution([row[key] for row in group])
        for name in sorted({name for row in group for name, job in row['jobs'].items()
                            if job['conclusion'] != 'skipped'}):
            jobs = [row['jobs'].get(name) or {} for row in group]
            for field in ('queue_seconds', 'job_seconds'):
                metrics[f'{name}.{field}'] = distribution([job.get(field) for job in jobs])
            for step in sorted({step for job in jobs for step in job.get('steps', {})}):
                metrics[f'{name}.{step}.seconds'] = distribution([
                    (job.get('steps', {}).get(step) or {}).get('seconds') for job in jobs])
        result[path] = metrics
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='Haoog/Adaptive-RSI')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--created', metavar='FROM..TO',
                           help='GitHub Actions run creation range in UTC (inclusive)')
    selection.add_argument('--run', action='append', metavar='RUN_ID/ATTEMPT',
                           help='completed attempt to measure (repeatable)')
    args = parser.parse_args()
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', args.repo):
        parser.error('--repo must be OWNER/REPOSITORY')
    if args.created:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d\d:\d\d:\d\dZ\.\.\d{4}-\d{2}-\d{2}T\d\d:\d\d:\d\dZ',
                            args.created) or seconds(*args.created.split('..')) is None:
            parser.error('--created must be two valid, ordered UTC timestamps separated by ..')
        attempts = [(args.repo, workflow, row['id'], attempt) for workflow in WORKFLOWS
                    for row in gh_get(f'repos/{args.repo}/actions/workflows/{workflow}/runs',
                                      'workflow_runs', status='completed', created=args.created)
                    for attempt in range(1, row['run_attempt'] + 1)]
    else:
        if any(not re.fullmatch(r'[1-9]\d*/[1-9]\d*', value) for value in args.run):
            parser.error('--run must be RUN_ID/ATTEMPT')
        attempts = [(args.repo, None, *map(int, value.split('/'))) for value in dict.fromkeys(args.run)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda fields: fetch_attempt(*fields), attempts))
    rows.sort(key=lambda row: (row['workflow'], row['run_id'], row['attempt']))
    print(json.dumps({'repository': args.repo, 'created': args.created,
                      'completed_runs': len({row['run_id'] for row in rows}), 'completed_attempts': len(rows),
                      'paths': summarize(rows), 'attempts': rows}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
