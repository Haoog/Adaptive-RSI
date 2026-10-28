"""Validate audit replay references and derive bilingual progress from one ledger."""
from collections import Counter
import argparse
import json
from pathlib import Path
import re
import shlex
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LEDGER = Path('docs/validation/audit-200-implementation-20261028.json')
PROGRESS = 'docs/validation/audit-200-progress-20261028'
INDEX = 'docs/validation/audit-200-evidence-20261028'
STATUSES = {'implemented', 'partial', 'pending'}


def local_path(value, root):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Evidence path must stay inside the repository')
    return path


def artifact(value, root=ROOT):
    try:
        return not any(character.isspace() for character in value) and local_path(value, root).is_file()
    except (ValueError, OSError):
        return False


def check(data, root=ROOT):
    errors = []
    items = data.get('items', [])
    if (not isinstance(items, list) or len(items) != 201 or
            any(not isinstance(item, dict) or type(item.get('id')) is not int for item in items) or
            sorted(item['id'] for item in items) != list(range(1, 202))):
        return ['Ledger must contain exactly IDs 1..201']
    # Discovery imports trusted test modules and may execute their load_tests hooks.
    loader = unittest.TestLoader()
    generated = {INDEX + '.md', INDEX + '.en.md'}
    for item in items:
        label = 'Audit ' + str(item['id'])
        if not isinstance(item.get('title'), dict) or any(not isinstance(item['title'].get(language), str) or
                not item['title'][language].strip() for language in ('zh', 'en')):
            errors.append(label + ': both titles are required')
        if item.get('status') not in STATUSES:
            errors.append(label + ': invalid status')
        if item.get('status') != 'implemented':
            continue
        if any(not isinstance(item.get(field), list) or not item[field] or
               any(not isinstance(value, str) or not value for value in item[field]) for field in ('implementation', 'validation')):
            errors.append(label + ': implemented requires files and replay evidence')
            continue
        for value in item.get('implementation', []):
            try:
                if not local_path(value, root).is_file() and value not in generated:
                    errors.append(label + ': missing implementation ' + value)
            except (ValueError, TypeError, OSError):
                errors.append(label + ': invalid implementation path')
        for value in item.get('validation', []):
            try:
                if artifact(value, root):
                    continue
                command = shlex.split(value)
                if not command or command[0] not in {'python3', 'node', 'cargo', 'uv'} or any(
                        token in {'&&', ';', '|', '||'} for token in command):
                    errors.append(label + ': evidence must be an existing artifact or one replay command')
                    continue
                for token in command[1:]:
                    if token.endswith(('.py', '.cjs', '.toml', '.yml')):
                        if '..' in Path(token).parts or not list(root.glob(token)):
                            errors.append(label + ': missing replay target ' + token)
                if 'unittest' in command:
                    selectors = [token for token in command[command.index('unittest') + 1:] if token.startswith('tests.')]
                    before = len(loader.errors)
                    loader.loadTestsFromNames(selectors)
                    if len(loader.errors) > before:
                        errors.append(label + ': invalid unittest selector')
            except (ValueError, TypeError, OSError):
                errors.append(label + ': malformed replay evidence')
    commit = data.get('implementation_commit')
    if data.get('release_ready') and (any(item['status'] != 'implemented' for item in items) or
            not data.get('ci_evidence') or not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit)):
        errors.append('Release readiness requires complete scope, an exact commit and CI evidence')
    return errors


def index(data, english=False):
    labels = {'implemented': 'Implemented', 'partial': 'Partial', 'pending': 'Pending'} if english else {
        'implemented': '已实现', 'partial': '部分完成', 'pending': '待做'}
    lines = ['# ' + ('Audit Replay Index' if english else '审计验收重放索引'), '',
             '[中文](audit-200-evidence-20261028.md)' if english else '[English](audit-200-evidence-20261028.en.md)', '',
             ('Generated from the JSON ledger. Commands are replay entry points, not proof they passed on the current commit. '
              'Read residual limitations and final exact-commit CI evidence before release.' if english else
              '由 JSON 台账生成。命令是验收重放入口，不代表其在当前提交均已通过；发版前须核对残余边界及最终精确提交 CI。'), '',
             '| ID | ' + ('Status | Item | Implementation | Replay / artifact |' if english else '状态 | 项目 | 实现 | 重放 / 产物 |'),
             '| --- | --- | --- | --- | --- |']
    for item in data['items']:
        links = ' <br> '.join('[' + value + '](../../' + value.replace(' ', '%20') + ')' for value in item.get('implementation', [])) or '—'
        validations = ' <br> '.join(('[' + value + '](../../' + value + ')') if artifact(value) else
                                    '`' + value.replace('`', '\\`') + '`' for value in item.get('validation', [])) or '—'
        title = item['title']['en' if english else 'zh'].replace('|', '\\|')
        lines.append('| {} | {} | {} | {} | {} |'.format(item['id'], labels[item['status']], title, links, validations))
    lines += ['', ('Residual boundaries remain in the [JSON ledger](audit-200-implementation-20261028.json).' if english else
                    '每项残余边界保留在 [JSON 台账](audit-200-implementation-20261028.json)。'), '']
    return '\n'.join(lines)


def outputs(data):
    counts = Counter(item['status'] for item in data['items'])
    result = {}
    for english in (False, True):
        suffix = '.en.md' if english else '.md'
        path = ROOT / (PROGRESS + suffix)
        text = path.read_text()
        summary = ('Current ledger: {} implemented with targeted acceptance, {} partial, and {} pending.' if english else
                   '当前台账：{} 项有实现及针对性验收，{} 项部分完成，{} 项尚待实施。').format(
                       counts['implemented'], counts['partial'], counts['pending'])
        text, changes = re.subn(r'^Current ledger: .*?pending\.' if english else r'^当前台账：.*?尚待实施。', summary, text, count=1, flags=re.M)
        if changes != 1:
            raise ValueError('Progress summary marker is missing')
        for item in data['items']:
            text = re.sub(r'^' + str(item['id']) + r'\. \*\*(?:implemented|partial|pending)\*\*',
                          str(item['id']) + '. **' + item['status'] + '**', text, flags=re.M)
        result[PROGRESS + suffix] = text
        result[INDEX + suffix] = index(data, english)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args(argv)
    data = json.loads((ROOT / LEDGER).read_text())
    errors = check(data)
    if not errors:
        for name, expected in outputs(data).items():
            target = ROOT / name
            if args.write:
                target.write_text(expected)
            elif not target.is_file() or target.read_text() != expected:
                errors.append('Stale generated audit summary/index: ' + name)
    if errors:
        raise ValueError('\n'.join(errors))
    print('Audit ledger and replay references passed:', dict(Counter(item['status'] for item in data['items'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
