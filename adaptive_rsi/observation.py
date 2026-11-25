"""Read-only dispositions from fixed checks and generation-scoped task outcomes."""


def timestamp(value):
    return type(value) in (int, float) and 0 < value < float('inf')


def disposition(window, now):
    passed, failed = window['passed'], window['failed']
    samples = passed + failed
    ratio = passed / samples if samples else None
    expired = window['deadline'] is not None and now >= window['deadline']
    if window.get('missing') or not samples or samples < window['minimum_samples']:
        state = 'pending_evidence'
    elif window['attribution'] in ('unknown', 'shared', 'mixed', 'subsequent_mainline'):
        state = 'pending_attribution'
    elif ratio < window['minimum_pass_ratio']:
        state = 'failed'
    else:
        state = 'continue'
    return {**window, 'mode': 'dry_run', 'state': state, 'samples': samples,
            'pass_ratio': ratio, 'expired': expired}


def delivery_window(outcome, config, now, polled=None):
    merged = outcome.get('merged_sha')
    if not merged:
        return None
    start = outcome.get('merged_at')
    start = start if timestamp(start) else None
    feedback = outcome.get('feedback')
    feedback = feedback if isinstance(feedback, dict) else {}
    policy = feedback.get('observation_policy')
    policy = policy if isinstance(policy, dict) else {}
    interval = policy.get('interval_seconds', config['delivery']['feedback_minutes'] * 60)
    deadline = policy.get('deadline', start + config['limits']['retention_days'] * 86400 if start is not None else None)
    deadline = deadline if timestamp(deadline) else None
    evaluation = feedback.get('evaluation') or {}
    merge = feedback.get('merge') or {}
    attribution = feedback.get('attribution') or {}
    checked = feedback.get('checked_at')
    head = feedback.get('head')
    def in_window(value):
        return start is not None and timestamp(deadline) and timestamp(value) and start <= value <= min(now, deadline)

    measured = (isinstance(head, str) and bool(head) and in_window(checked)
                and isinstance(evaluation, dict) and evaluation.get('decision') in ('pass', 'fail'))
    passed = int(measured and feedback.get('state') == 'verified' and evaluation['decision'] == 'pass')
    failed = int(measured and feedback.get('state') == 'regressed' and evaluation['decision'] == 'fail')
    merge_measured = isinstance(merge, dict) and merge.get('sha') == merged and in_window(merge.get('checked_at'))
    merge_verified = merge_measured and merge.get('state') == 'verified' and merge.get('decision') == 'pass'
    if merge_measured and merge.get('state') == 'regressed' and merge.get('decision') == 'fail':
        head, checked, passed, failed = merged, merge['checked_at'], 0, 1
    scope = 'none'
    if failed:
        scope = ('measured_version' if head == merged else
                 'subsequent_mainline' if isinstance(attribution, dict) and
                 attribution.get('scope') == 'subsequent_mainline' else 'unknown')
    last = max((value for value in (polled, outcome.get('feedback_polled_at'), checked) if timestamp(value)), default=None)
    return disposition({'metric': 'fixed_check_verdict', 'version': merged, 'observed_version': head,
                        'started_at': start, 'checked_at': checked, 'last_poll_at': last,
                        'interval_seconds': interval, 'minimum_samples': 1, 'minimum_pass_ratio': 1.0,
                        'deadline': deadline, 'passed': passed, 'failed': failed, 'attribution': scope,
                        'missing': bool(outcome.get('feedback_attempts')) or bool(passed and head != merged and not merge_verified),
                        'reused_from': feedback.get('reused_from')}, now)


def strategy_policy(candidate, config):
    start = candidate['promoted'] or candidate['created']
    return {'metric': 'verified_task_pass_ratio', 'version': candidate['created'], 'started_at': start,
            'interval_seconds': 0, 'minimum_samples': config['evolution']['monitor_tasks'],
            'minimum_pass_ratio': 0.8, 'deadline': start + config.get('limits', {}).get('retention_days', 30) * 86400,
            'checked_at': None, 'passed': 0, 'failed': 0, 'unattributed_failures': 0,
            'missing': False, 'attribution': 'none', 'runs': []}


def valid_strategy_window(window):
    counters = ('passed', 'failed', 'unattributed_failures', 'minimum_samples')
    return (isinstance(window, dict) and timestamp(window.get('deadline'))
            and isinstance(window.get('runs'), list)
            and all(type(window.get(key)) is int and window[key] >= 0 for key in counters)
            and type(window.get('minimum_pass_ratio')) in (int, float)
            and 0 <= window['minimum_pass_ratio'] <= 1
            and (window.get('checked_at') is None or timestamp(window['checked_at']))
            and window.get('attribution') in ('none', 'single', 'shared', 'mixed'))


def strategy_window(previous, candidate, config, run_id, checked, passed, attributable, shared):
    start = candidate['promoted'] or candidate['created']
    window = strategy_policy(candidate, config)
    if (valid_strategy_window(previous) and window.keys() <= previous.keys()
            and previous['started_at'] == start and previous['version'] == candidate['created']):
        window = previous
    if checked < start or checked > window['deadline'] or run_id in window['runs'] or len(window['runs']) >= 1000:
        return window
    window = {**window, 'runs': [*window['runs'], run_id]}
    window['checked_at'] = max(window['checked_at'] or checked, checked)
    if passed:
        window['passed'] += 1
    elif attributable:
        window['failed'] += 1
        window['unattributed_failures'] += int(shared)
        unknown = window['unattributed_failures']
        window['attribution'] = ('shared' if unknown == window['failed'] else 'mixed' if unknown else 'single')
    else:
        window['missing'] = True
    return window


def strategy_view(window, now):
    if not valid_strategy_window(window):
        return None
    return disposition({key: value for key, value in window.items() if key != 'runs'}, now)
