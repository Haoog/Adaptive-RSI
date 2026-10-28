"""Low-water admission protects new work without failing queued tasks."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


MIB = 1024 * 1024


def available_memory():
    """Best available host estimate, capped by a visible Linux cgroup limit."""
    if sys.platform == 'darwin':
        output = subprocess.check_output(['/usr/bin/vm_stat'], timeout=1, text=True)
        page_size = re.search(r'page size of (\d+) bytes', output)
        fields = dict(re.findall(r'^(Pages [\w ]+):\s*(\d+)\.', output, re.M))
        if not page_size or not all(key in fields for key in ('Pages free', 'Pages inactive', 'Pages speculative')):
            raise ValueError('Memory availability could not be measured')
        return sum(int(fields[key]) for key in ('Pages free', 'Pages inactive', 'Pages speculative')) * int(page_size[1]), 'macos_reclaimable_estimate'
    with Path('/proc/meminfo').open() as source:
        info = source.read(65536)
    match = re.search(r'^MemAvailable:\s*(\d+) kB$', info, re.M)
    if not match:
        raise ValueError('Memory availability could not be measured')
    available, basis = int(match[1]) * 1024, 'linux_memavailable'
    roots = [Path('/sys/fs/cgroup')]
    try:
        with Path('/proc/self/cgroup').open() as source:
            group = next((line.strip()[3:] for line in source if line.startswith('0::')), '')
        if group and '..' not in Path(group).parts:
            current = roots[0] / group.lstrip('/')
            while current != roots[0]:
                roots.append(current)
                current = current.parent
    except OSError:
        pass
    for root in roots:
        try:
            maximum = (root / 'memory.max').read_text()[:100].strip()
            used = (root / 'memory.current').read_text()[:100].strip()
            if maximum.isdigit() and used.isdigit():
                available = min(available, max(0, int(maximum) - int(used)))
                basis = 'linux_memavailable_and_visible_cgroup'
        except OSError:
            continue
    return available, basis


class PressureMonitor:
    def __init__(self):
        self.memory = None
        self.memory_at = None
        self.projects = {}

    def check(self, state_root, project, config):
        limits = config['limits']
        disk_min = limits['min_free_disk_mb'] * MIB
        memory_min = limits['min_available_memory_mb'] * MIB
        report = {'checked_at': time.time(), 'admitted': True, 'reasons': [], 'disk': [],
                  'min_free_disk_bytes': disk_min, 'min_available_memory_bytes': memory_min}
        if disk_min:
            for label, path in [('state', state_root), ('project', project['path'])]:
                try:
                    free = shutil.disk_usage(path).free
                    report['disk'].append({'scope': label, 'free_bytes': free})
                    if free < disk_min:
                        report['reasons'].append('disk_' + label)
                except OSError:
                    report['reasons'].append('disk_' + label + '_unknown')
        if memory_min:
            now = time.monotonic()
            if self.memory_at is None or now - self.memory_at >= 5:
                try:
                    free, basis = available_memory()
                    self.memory = {'available_bytes': free, 'basis': basis, 'checked_at': time.time()}
                except (OSError, ValueError, subprocess.SubprocessError):
                    self.memory = {'available_bytes': None, 'basis': 'unavailable', 'checked_at': time.time()}
                self.memory_at = now
            report['memory'] = dict(self.memory)
            if self.memory['available_bytes'] is None:
                report['reasons'].append('memory_unknown')
            elif self.memory['available_bytes'] < memory_min:
                report['reasons'].append('memory')
        report['admitted'] = not report['reasons']
        if project['id'] not in self.projects and len(self.projects) >= 1024:
            self.projects.pop(next(iter(self.projects)))
        self.projects[project['id']] = report
        return report
