#!/usr/bin/env python3
"""Read-only GitHub Actions timing evidence (no job logs or candidate code).

Offline input: {"run": REST-run, "attempts": [{"attempt": 1, "jobs": [...]}]}.
Every step is included so consumers can select critical steps without reading logs.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def elapsed(start, end):
    """Return observed seconds, preserving absent/invalid evidence as unknown."""
    try:
        value = (datetime.fromisoformat(end.replace('Z', '+00:00')) -
                 datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
        return value if value >= 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def select(source, fields):
    return {key: source.get(key) for key in fields.split()}


def build_report(evidence, release_path=None, reuse_reason=None):
    run = evidence['run']
    jobs = []
    for batch in evidence['attempts']:
        for job in batch['jobs']:
            steps = [dict(select(step, 'name number status conclusion started_at completed_at'),
                          duration_seconds=elapsed(step.get('started_at'), step.get('completed_at')))
                     for step in job.get('steps', [])]
            real_steps = [step for step in steps if step['started_at'] and
                          step['name'] not in ('Set up job', 'Complete job') and
                          step['status'] != 'skipped' and step['conclusion'] != 'skipped']
            first = min((step['started_at'] for step in real_steps), default=None)
            allocated = bool(job.get('runner_id') and first)
            messages = [str(item.get('message', '')) for item in job.get('annotations', [])]
            unavailable = any('not acquired by Runner' in message for message in messages)
            jobs.append(dict(select(job, 'id name status conclusion created_at started_at completed_at runner_id runner_name labels'),
                attempt=job.get('run_attempt') or batch['attempt'],
                allocation=('not_requested' if job.get('conclusion') == 'skipped' else
                            'allocated' if allocated else 'unallocated'),
                allocation_failure_reason='hosted_runner_unavailable' if unavailable else None,
                annotation_evidence=job.get('annotation_evidence',
                    'available' if 'annotations' in job else 'not_collected'),
                capacity_warning=any('capacity constraints' in message for message in messages),
                first_real_step_at=first,
                dependency_wait_seconds=elapsed(run.get('created_at'), job.get('created_at')),
                runner_allocation_seconds=elapsed(job.get('created_at'), first) if allocated else None,
                execution_seconds=elapsed(first, job.get('completed_at')) if allocated else None,
                steps=steps))
    return {'schema_version': 1,
            'run': select(run, 'id name head_sha run_attempt status conclusion event created_at run_started_at updated_at html_url'),
            'attempts': [dict(select(batch.get('run', {}),
                'head_sha status conclusion created_at run_started_at updated_at'), attempt=batch['attempt'])
                for batch in evidence['attempts']],
            'release_job_evidence': [select(job, 'id name attempt status conclusion') for job in jobs
                                     if any(word in (job['name'] or '').lower()
                                            for word in ('reuse', 'fallback', 'build', '复用', '构建'))],
            'release': {'path': release_path, 'reuse_reason': reuse_reason},
            'timing_notes': [
                'dependency_wait_seconds is workflow creation to job creation; includes dependency and scheduling wait, not pure queue time.',
                'runner_allocation_seconds is job creation to first real step; includes allocation and setup, not a direct runner queue measurement.',
                'execution_seconds is first real step to job completion; missing runner or real steps means unallocated and unknown execution.',
                'Missing job created_at leaves waiting metrics unknown. timeout-minutes is not assumed to include queueing.',
                'A running collector job may be incomplete in this snapshot.'],
            'jobs': jobs}


def markdown(report):
    import html

    def cell(value):
        if value is None:
            return 'unknown'
        return html.escape(str(value)).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ')

    lines = ['## CI 耗时采集', '',
             'Run {} · SHA {} · attempt {} · {}'.format(*[
                 cell(report['run'][key]) for key in ('id', 'head_sha', 'run_attempt', 'conclusion')]), '',
             '等待包含依赖与调度；runner 分配估计包含启动准备，不等于纯排队时间。缺证据用 unknown，不据 timeout-minutes 推断排队。', '',
             '| Attempt | Job | Result | Allocation | Dependency wait (s) | Runner allocation + setup (s) | Execution (s) | Capacity / allocation reason |',
             '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for job in report['jobs']:
        reasons = [job['allocation_failure_reason']] if job['allocation_failure_reason'] else []
        if job['capacity_warning']:
            reasons.append('capacity_warning')
        if job['annotation_evidence'] == 'unavailable':
            reasons.append('annotation evidence unavailable')
        lines.append('| ' + ' | '.join(map(cell, [job[key] for key in (
            'attempt', 'name', 'conclusion', 'allocation', 'dependency_wait_seconds',
            'runner_allocation_seconds', 'execution_seconds')] + ['; '.join(reasons) or None])) + ' |')
    lines += ['', '| Attempt | Job | Step | Result | Duration (s) |', '| --- | --- | --- | --- | --- |']
    for job in report['jobs']:
        for step in job['steps']:
            lines.append('| ' + ' | '.join(map(cell, [job['attempt'], job['name'], step['name'],
                                                       step['conclusion'], step['duration_seconds']])) + ' |')
    lines += ['', 'Release path: {} · Reuse reason: {}'.format(
        cell(report['release']['path']), cell(report['release']['reuse_reason'])), '']
    return '\n'.join(lines)


def gh_api(endpoint):
    # Never echo gh stderr: credentials and unrelated remote data are not report data.
    result = subprocess.run(['gh', 'api', '--method', 'GET', endpoint],
                            text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('GitHub API read failed; check actions:read access and run ID')
    return json.loads(result.stdout)


def collect(repository, run_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
        raise ValueError('repository must be owner/repo')
    if not re.fullmatch(r'[1-9][0-9]*', str(run_id or '')):
        raise ValueError('run-id must be a positive integer')
    base = 'repos/{}/actions/runs/{}'.format(repository, run_id)
    run = gh_api(base)
    attempts = []
    for attempt in range(1, int(run.get('run_attempt', 1)) + 1):
        jobs, seen = [], set()
        page = 1
        while True:
            payload = gh_api('{}/attempts/{}/jobs?per_page=100&page={}'.format(base, attempt, page))
            batch = payload['jobs']
            for job in batch:
                if job['id'] not in seen:
                    seen.add(job['id'])
                    if job.get('conclusion') in ('failure', 'cancelled', 'timed_out') and not job.get('runner_id'):
                        check_url = job.get('check_run_url', '')
                        prefix = 'https://api.github.com/repos/' + repository + '/check-runs/'
                        if check_url.startswith(prefix) and check_url[len(prefix):].isdigit():
                            annotations, annotation_page = [], 1
                            try:
                                while True:
                                    rows = gh_api('repos/' + repository + '/check-runs/' +
                                                  check_url[len(prefix):] + '/annotations?per_page=100&page=' + str(annotation_page))
                                    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                                        raise ValueError('Invalid annotation evidence')
                                    annotations.extend(rows)
                                    if len(rows) < 100:
                                        break
                                    annotation_page += 1
                            except (OSError, ValueError, subprocess.TimeoutExpired):
                                # Optional checks:read evidence must not discard Actions timings.
                                # A partial page sequence cannot establish complete evidence.
                                job['annotations'] = []
                                job['annotation_evidence'] = 'unavailable'
                            else:
                                job['annotations'] = annotations
                                job['annotation_evidence'] = 'available'
                    jobs.append(job)
            if len(batch) < 100:
                break
            page += 1
        attempt_run = gh_api('{}/attempts/{}'.format(base, attempt))
        attempts.append({'attempt': attempt, 'run': attempt_run, 'jobs': jobs})
    return {'run': run, 'attempts': attempts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--input', type=Path, help='offline REST evidence JSON')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--summary', type=Path, default=os.environ.get('GITHUB_STEP_SUMMARY'))
    parser.add_argument('--release-path', help='prepare output, e.g. reuse or fallback-build')
    parser.add_argument('--reuse-reason', help='prepare output; never pass credentials or logs')
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.input.read_text()) if args.input else collect(args.repository, args.run_id)
        report = build_report(evidence, args.release_path, args.reuse_reason)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        if args.summary:
            with args.summary.open('a', encoding='utf-8') as stream:
                stream.write('\n' + markdown(report))
        print('CI metrics: {} jobs across {} attempts'.format(len(report['jobs']), len(evidence['attempts'])))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        print('CI metrics collection failed: invalid/inaccessible evidence or output; no logs collected.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
