"""Only reconstructed mechanical release patches may use metadata-only CI."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import release


DOCUMENTS = {name + suffix + '.md' for name in ('README', 'CHANGELOG', 'CONTRIBUTING', 'DESKTOP', 'EXPERIENCE',
             'RELEASING', 'RELIABILITY', 'SERVER', 'VALIDATION', 'LIVE_VALIDATION') for suffix in ('', '.en')}


def classify(base, head):
    paths = subprocess.check_output(['git', 'diff', '--name-only', '-z', base, head], cwd=release.ROOT).split(b'\0')
    try:
        changed = {path.decode('utf-8') for path in paths if path}
    except UnicodeError:
        return 'full'
    if changed and all(path in DOCUMENTS or path.startswith('docs/') for path in changed):
        return 'docs'
    if changed != set(release.RELEASE_FILES):
        return 'full'
    try:
        release.verify_generated(base, head)
    except (ValueError, KeyError, TypeError, subprocess.CalledProcessError):
        return 'full'
    return 'version'


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: classify_changes.py BASE HEAD')
    print(classify(sys.argv[1], sys.argv[2]))
