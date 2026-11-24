"""Descriptive comparisons preserve unknown usage and independent case families."""
import math

from .evolution import CONCLUSIVE_DECISIONS, arm_decision


def measurement(value):
    return value if type(value) in (int, float) and 0 <= value <= 10**15 and math.isfinite(value) else None


def comparison(trials):
    complete = []
    cohorts = {}
    for row in trials:
        if not isinstance(row, dict):
            continue
        stable, candidate = (row.get(variant) for variant in ('stable', 'candidate'))
        decisions = (arm_decision(stable), arm_decision(candidate))
        no_change = any(isinstance(arm, dict) and arm.get('no_change') for arm in (stable, candidate))
        conclusive = all(decision in CONCLUSIVE_DECISIONS for decision in decisions)
        if conclusive and not no_change:
            complete.append(row)
        if not isinstance(row.get('case'), str) or not row['case']:
            continue
        stratum = row.get('stratum')
        size = stratum[1] if isinstance(stratum, list) and len(stratum) == 2 and isinstance(stratum[1], str) else 'unknown'
        key = tuple(row.get(key) if isinstance(row.get(key), str) else 'unknown' for key in ('source', 'split')) + (size,)
        families = cohorts.setdefault(key, {})
        outcome = 'no_change' if no_change else decisions if conclusive else None
        families.setdefault(row['case'], set()).add(outcome)
    totals = {}
    for variant in ('stable', 'candidate'):
        tokens = [measurement(row[variant].get('tokens')) if row[variant].get('usage_known') is True else None for row in complete]
        seconds = [measurement(row[variant].get('seconds')) for row in complete]
        totals[variant] = {'tokens': sum(tokens) if tokens and None not in tokens else None,
                           'seconds': sum(seconds) if seconds and None not in seconds else None}
    token_known = len(complete) == len(trials) and bool(complete) and all(totals[v]['tokens'] is not None for v in totals)
    latency_known = len(complete) == len(trials) and bool(complete) and all(totals[v]['seconds'] is not None for v in totals)
    token_ratio = totals['candidate']['tokens'] / totals['stable']['tokens'] if token_known and totals['stable']['tokens'] > 0 else None
    latency_ratio = totals['candidate']['seconds'] / totals['stable']['seconds'] if latency_known and totals['stable']['seconds'] > 0 else None
    rows = []
    for (source, split, size), families in sorted(cohorts.items()):
        wins = losses = ties = 0
        for family in families.values():
            if 'no_change' in family:
                continue
            if ('pass', 'fail') in family:
                losses += 1
            elif None not in family:
                wins += ('fail', 'pass') in family
                ties += ('fail', 'pass') not in family
        counted = wins + losses + ties
        if counted:
            rows.append({'source': source, 'split': split, 'size': size, 'families': counted,
                         'wins': wins, 'losses': losses, 'ties': ties})
    return {'unit': 'tokens_not_currency', 'trials': len(complete), 'incomplete_trials': len(trials) - len(complete),
            'totals': totals, 'token_ratio': token_ratio, 'latency_ratio': latency_ratio,
            'cost_state': 'unknown_usage' if not token_known else 'zero_baseline' if token_ratio is None else 'known',
            'latency_state': 'unknown' if not latency_known else 'zero_baseline' if latency_ratio is None else 'known',
            'comparable': token_ratio is not None and latency_ratio is not None, 'cohorts': rows}
