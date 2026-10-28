"""Stable validation reasons expose only schema-owned field paths, never input."""
from .config import DEFAULTS


FIXED = {
    'working_hours requires different start/end hours in 0..24; start must be below 24': ('config_working_hours', 'schedule.working_hours'),
    'Maximum concurrency is 4': ('config_concurrency', 'limits.concurrency'),
    'Repository must be owner/name': ('config_repository', 'github.repository'),
    'Project weight must be 1..10': ('config_weight', 'schedule.weight'),
    'Resource admission thresholds must be 0..1048576 MiB': ('config_resource_threshold', 'limits'),
    'Invalid deployment health retry policy': ('config_health_observation', 'delivery.deploy'),
    'Deployment health URLs must be unique': ('config_health_targets', 'delivery.deploy.health_urls'),
    'Additional deployment health URLs require a primary URL and at most seven HTTPS targets': ('config_health_targets', 'delivery.deploy.health_urls'),
    'Deployment health URL requires HTTPS without credentials, query or fragment': ('config_health_url', 'delivery.deploy.health_url'),
    'Automatic deployment rollback requires an explicit health URL': ('config_rollback_health', 'delivery.deploy.health_url'),
    'Invalid deployment rollback data compatibility policy': ('config_rollback_data', 'delivery.deploy.rollback_data_policy'),
    'Deployment rollback recovery notes exceed 2000 characters': ('config_rollback_notes', 'delivery.deploy.rollback_recovery_notes'),
    'Configuration changed in another window; reload before saving': ('config_revision_conflict', None),
}


def fields(value=DEFAULTS, prefix=''):
    for key, child in value.items():
        path = prefix + key
        yield path
        if isinstance(child, dict):
            yield from fields(child, path + '.')


FIELDS = frozenset(fields())


def detail(error):
    message = str(error)
    if message in FIXED:
        reason, field = FIXED[message]
        return {'reason_code': reason, 'field_path': field}
    for suffix in (' must be an object', ' has the wrong type'):
        if message.endswith(suffix) and message[:-len(suffix)] in FIELDS:
            return {'reason_code': 'config_field_type', 'field_path': message[:-len(suffix)]}
    if message.startswith('Invalid positive limit: '):
        field = 'limits.' + message[len('Invalid positive limit: '):]
        if field in FIELDS:
            return {'reason_code': 'config_limit', 'field_path': field}
    return {'reason_code': 'config_invalid', 'field_path': None}
