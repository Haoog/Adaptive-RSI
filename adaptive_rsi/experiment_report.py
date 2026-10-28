"""Descriptive comparisons preserve unknown usage and independent case families."""
import math


def measurement(value):
    return value if type(value) in (int, float) and 0 <= value <= 10**15 and math.isfinite(value) else None


def comparison(trials):
    complete = [row for row in trials if isinstance(row, dict) and
                all(isinstance(row.get(variant), dict) and type(row[variant].get('pass')) is bool
                    for variant in ('stable', 'candidate'))]
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
    cohorts = {}
    for row in complete:
        if not isinstance(row.get('case'), str) or not row['case']:
            continue
        stratum = row.get('stratum')
        size = stratum[1] if isinstance(stratum, list) and len(stratum) == 2 and isinstance(stratum[1], str) else 'unknown'
        key = tuple(row.get(key) if isinstance(row.get(key), str) else 'unknown' for key in ('source', 'split')) + (size,)
        families = cohorts.setdefault(key, {})
        families.setdefault(row['case'], []).append(row)
    rows = []
    for (source, split, size), families in sorted(cohorts.items()):
        wins = losses = 0
        for family in families.values():
            lost = any(row['stable']['pass'] and not row['candidate']['pass'] for row in family)
            won = not lost and any(not row['stable']['pass'] and row['candidate']['pass'] for row in family)
            wins += won
            losses += lost
        rows.append({'source': source, 'split': split, 'size': size, 'families': len(families),
                     'wins': wins, 'losses': losses, 'ties': len(families) - wins - losses})
    return {'unit': 'tokens_not_currency', 'trials': len(complete), 'incomplete_trials': len(trials) - len(complete),
            'totals': totals, 'token_ratio': token_ratio, 'latency_ratio': latency_ratio,
            'cost_state': 'unknown_usage' if not token_known else 'zero_baseline' if token_ratio is None else 'known',
            'latency_state': 'unknown' if not latency_known else 'zero_baseline' if latency_ratio is None else 'known',
            'comparable': token_ratio is not None and latency_ratio is not None, 'cohorts': rows}
