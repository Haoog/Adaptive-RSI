"""Replay single-writer repository scheduling with explicit eligibility and mainline evidence."""
from bisect import bisect_right
from collections import Counter, defaultdict, deque
import math
import statistics

from .store import Store


def _number(value, minimum=0):
    if type(value) not in (int, float) or not minimum <= value <= 4102444800 or not math.isfinite(value):
        raise ValueError(f'Expected a finite number in [{minimum}, 4102444800]')
    return value


def _count(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2000:
        raise ValueError(f'Expected an integer in [{minimum}, 2000]')
    return value


def _label(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError('Use nonempty, redacted labels of at most 100 characters')
    return value


def _eligibility(row, created):
    ready_at = _number(row['ready_at'], created)
    windows = row.get('eligible_windows', [[ready_at, None]])
    if not windows or windows[0][0] != ready_at or windows[-1][1] is not None:
        raise ValueError('Eligibility windows must begin at ready_at and end with an open window')
    intervals = []
    previous_end = ready_at
    for index, (start, until) in enumerate(windows):
        start = _number(start, previous_end)
        if index < len(windows) - 1:
            until = _number(until, start)
            if start == until:
                raise ValueError('Eligibility windows must have positive duration')
            previous_end = until
        intervals.append((start, until))
    return intervals


def _eligible_wait(row, now):
    return sum(min(now, until if until is not None else now) - start
               for start, until in row['eligible_windows'] if start <= now)


def _is_eligible(row, now):
    return any(start <= now and (until is None or now < until) for start, until in row['eligible_windows'])


def evaluate(trace, *, workers=2, planning_slots=1, old_after=86400, old_quota=1,
             urgent_quota=1, quota_window=3600):
    """Compare three independent changes against the serial-planning baseline."""
    workers, planning_slots = _count(workers, 1), _count(planning_slots, 1)
    old_quota, urgent_quota = _count(old_quota), _count(urgent_quota)
    old_after, quota_window = _number(old_after, 1), _number(quota_window, 1)
    if trace.get('format') != 1 or trace.get('provenance') not in ('fixed_fixture', 'redacted_history'):
        raise ValueError('Expected format 1 with fixed_fixture or redacted_history provenance')
    if len(trace['tasks']) > 2000:
        raise ValueError('Replay accepts at most 2000 tasks')
    changes = {_label(repo): sorted({_number(at) for at in times})
               for repo, times in trace['mainline_changes'].items()}
    projects = {_label(project): (_label(row['repo']), _number(row.get('weight', 1), 1))
                for project, row in trace['projects'].items()}
    tasks = {}
    for row in trace['tasks']:
        key = _label(row['id'])
        project = _label(row['project'])
        if key in tasks or project not in projects or projects[project][0] not in changes:
            raise ValueError('Task IDs must be distinct, with known project and repository mainline coverage')
        created = _number(row['created'])
        origin = row.get('urgency_origin', 'unknown')
        urgency = _count(row.get('urgency', 0))
        if origin not in ('user', 'system', 'unknown') or (origin == 'user' and not urgency):
            raise ValueError('Invalid urgency or urgency origin')
        tasks[key] = dict(id=key, project=project, created=created,
                          ready_at=_number(row['ready_at'], created), eligible_windows=_eligibility(row, created),
                          source=_label(row['source']),
                          priority=_count(row.get('priority', 50)), urgency=urgency, urgency_origin=origin,
                          plan_seconds=_number(row['plan_seconds']), work_seconds=_number(row['work_seconds'], 1))
    results = {}
    for name in ('baseline', 'isolated_planning', 'old_task_opportunity', 'cross_project_user_urgency'):
        results[name] = _replay(tasks, projects, changes, workers,
                                planning_slots if name == 'isolated_planning' else 0,
                                old_after, old_quota if name == 'old_task_opportunity' else 0,
                                urgent_quota if name == 'cross_project_user_urgency' else 0, quota_window)
    baseline = results['baseline']
    for name, result in results.items():
        if name == 'baseline':
            continue
        for key, row in result['tasks'].items():
            original = baseline['tasks'][key]
            row['claim_seconds_saved'] = original['claimable_wait_seconds'] - row['claimable_wait_seconds']
            row['work_start_seconds_saved'] = original['work_started'] - row['work_started']
        result['normal_wait_delta_seconds'] = (result['normal_wait_seconds']['sum'] -
                                               baseline['normal_wait_seconds']['sum'])
        result['claim_seconds_saved'] = sum(row['claim_seconds_saved'] for row in result['tasks'].values())
        result['work_start_seconds_saved'] = sum(row['work_start_seconds_saved'] for row in result['tasks'].values())
        for project, row in result['projects'].items():
            original = baseline['projects'][project]
            row['ordinary_wait_delta_seconds'] = row['ordinary_wait_seconds'] - original['ordinary_wait_seconds']
            row['max_wait_delta_seconds'] = row['max_wait_seconds'] - original['max_wait_seconds']
    return {'format': 1, 'provenance': trace['provenance'], 'sample_tasks': len(tasks),
            'status': 'ok' if tasks else 'insufficient_samples',
            'urgent_origins': dict(Counter(row['urgency_origin'] for row in tasks.values() if row['urgency'])),
            'settings': dict(workers=workers, planning_slots=planning_slots, old_after=old_after,
                             old_quota=old_quota, urgent_quota=urgent_quota, quota_window=quota_window),
            'scenarios': results,
            'interpretation': 'Counterfactual replay. Eligibility, task durations and mainline updates are supplied '
                              'by the trace. Writer slots are exclusive per repository; isolated planners use separate slots.'}


def _replay(tasks, projects, changes, workers, plan_slots, old_after, old_quota, urgent_quota, window):
    arrivals = deque(sorted(tasks.values(), key=lambda row: (row['ready_at'], row['id'])))
    pending, active, planned, records = {}, {}, {}, {}
    recent, history, consumed_old = defaultdict(lambda: deque(maxlen=3)), defaultdict(deque), defaultdict(deque)
    consumed_urgent, waiting, services = deque(), {}, {}
    promotions = Counter()
    clock = 0
    now = arrivals[0]['ready_at'] if arrivals else 0

    def trim(queue, before):
        while queue and queue[0] <= before:
            queue.popleft()

    while arrivals or pending or active:
        while arrivals and arrivals[0]['ready_at'] <= now:
            row = arrivals.popleft()
            pending[row['id']] = row
        active = {repo: end for repo, end in active.items() if end > now}
        pools = defaultdict(list)
        for key, row in pending.items():
            project = row['project']
            if (_is_eligible(row, now) and projects[project][0] not in active and
                    (key not in planned or planned[key][1] <= now)):
                pools[project].append(row)
        if pools and len(active) < workers:
            waiting = {key: at for key, at in waiting.items() if key in pools}
            for key in pools:
                services[key] = min(clock + 2, max(clock - 2, services.get(key, clock))) if key in waiting else clock
                waiting.setdefault(key, now)

            def rank(project):
                intervals = history[project]
                while intervals and intervals[0][1] <= now - 86400:
                    intervals.popleft()
                seconds = sum(max(1, min(end, now) - max(start, now - 86400)) for start, end in intervals)
                return Store.project_claim_key(now, waiting[project], seconds, projects[project][1], services[project])

            def claim_key(row):
                sources = recent[row['project']]
                fair = len(sources) == 3 and all(source == 'system_delivery' for source in sources)
                return Store.claim_key(row, now, fair)

            heads = {key: min(rows, key=claim_key) for key, rows in pools.items()}
            project = min(sorted(pools), key=rank)
            row = heads[project]
            action = None
            trim(consumed_urgent, now - window)
            urgent = [key for key, head in heads.items() if head['urgency_origin'] == 'user']
            if urgent and row['urgency_origin'] != 'user' and len(consumed_urgent) < urgent_quota:
                project = min(sorted(urgent), key=rank)
                row = heads[project]
                consumed_urgent.append(now)
                action = 'cross_project_user_urgency'
            old = consumed_old[project]
            trim(old, now - window)
            if len(old) < old_quota:
                oldest = min(pools[project], key=lambda item: (-_eligible_wait(item, now), claim_key(item)))
                if oldest is not row and _eligible_wait(oldest, now) >= old_after:
                    row = oldest
                    old.append(now)
                    action = 'old_task_opportunity'
            key = row['id']
            repo, weight = projects[project]
            plan_started, plan_ended = planned.pop(key, (now, now))
            isolated = plan_started < now
            mainline_updates = bisect_right(changes[repo], now) - bisect_right(changes[repo], plan_started)
            stale = isolated and mainline_updates > 0
            rework = row['plan_seconds'] if stale else 0
            work_started = now if isolated and not stale else now + row['plan_seconds']
            end = work_started + row['work_seconds']
            records[key] = {'project': project, 'repo': repo, 'ready_at': row['ready_at'], 'claimed_at': now,
                            'claimable_wait_seconds': _eligible_wait(row, now), 'plan_started': plan_started,
                            'isolated_plan_used': isolated and not stale, 'stale_plan': stale,
                            'mainline_updates_since_plan': mainline_updates,
                            'rework_seconds': rework, 'isolated_planning_seconds': plan_ended - plan_started,
                            'work_started': work_started, 'finished_at': end,
                            'promotion': action}
            if action:
                promotions[project] += 1
            active[repo] = end
            pending.pop(key)
            history[project].append((now, end))
            recent[project].append(row['source'])
            services[project] += 1 / weight
            clock = max(clock, min(services[key] for key in pools))
            waiting[project] = now
            continue
        if plan_slots:
            occupied = {projects[pending[key]['project']][0] for key, (_, end) in planned.items() if end > now}
            for row in sorted(pending.values(), key=lambda row: Store.claim_key(row, now)) if active else ():
                repo = projects[row['project']][0]
                key = row['id']
                if len(occupied) >= plan_slots:
                    break
                if (repo in active and repo not in occupied and key not in planned and row['plan_seconds']
                        and _is_eligible(row, now)):
                    planned[key] = (now, now + row['plan_seconds'])
                    occupied.add(repo)
        times = [*active.values(), *(end for _, end in planned.values() if end > now)]
        times.extend(start for row in pending.values() for start, _ in row['eligible_windows'] if start > now)
        if arrivals:
            times.append(arrivals[0]['ready_at'])
        if times:
            now = min(times)
    normal = [row['claimable_wait_seconds'] for key, row in records.items() if not tasks[key]['urgency']]
    by_project = {}
    for key, row in records.items():
        item = by_project.setdefault(row['project'], {'claims': 0, 'worker_seconds': 0, 'wait_seconds': 0,
                                                      'max_wait_seconds': 0, 'ordinary_wait_seconds': 0,
                                                      'promotions': promotions[row['project']]})
        item['claims'] += 1
        item['worker_seconds'] += row['finished_at'] - row['claimed_at']
        item['wait_seconds'] += row['claimable_wait_seconds']
        item['max_wait_seconds'] = max(item['max_wait_seconds'], row['claimable_wait_seconds'])
        if not tasks[key]['urgency']:
            item['ordinary_wait_seconds'] += row['claimable_wait_seconds']
    for key, item in by_project.items():
        item['weight'] = projects[key][1]
        item['worker_seconds_per_weight'] = item['worker_seconds'] / item['weight']
    return {'tasks': records, 'projects': by_project, 'promotions': sum(promotions.values()),
            'normal_wait_seconds': {'samples': len(normal), 'sum': sum(normal),
                                    'median': statistics.median(normal) if normal else None,
                                    'max': max(normal, default=None)},
            'stale_tasks': sum(row['stale_plan'] for row in records.values()),
            'task_rework_seconds': sum(row['rework_seconds'] for row in records.values()),
            'isolated_planning_seconds': sum(row['isolated_planning_seconds'] for row in records.values())}
