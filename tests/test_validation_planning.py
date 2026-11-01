"""CI shortcuts must preserve the complete final release gate and test discovery."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from scripts import classify_changes, release, test_parallel, test_selected
from tests.test_runtime import git


class CIMetricsTest(unittest.TestCase):
    @staticmethod
    def evidence():
        # Synthetic fixtures following GitHub REST run/job shapes; not measurements.
        return {'run': {'id': 42, 'head_sha': 'a' * 40, 'run_attempt': 2,
                        'status': 'completed', 'conclusion': 'failure',
                        'created_at': '2026-10-01T00:00:00Z'},
                'attempts': [{'attempt': 1, 'jobs': [{'id': 11, 'name': 'python',
                    'run_attempt': 1, 'status': 'completed', 'conclusion': 'success',
                    'created_at': '2026-10-01T00:01:00Z',
                    'started_at': '2026-10-01T00:01:01Z',
                    'completed_at': '2026-10-01T00:03:00Z', 'runner_id': 7,
                    'steps': [{'name': 'Set up job', 'number': 1,
                               'started_at': '2026-10-01T00:01:20Z',
                               'completed_at': '2026-10-01T00:01:30Z'},
                              {'name': 'Run tests', 'number': 2, 'conclusion': 'success',
                               'started_at': '2026-10-01T00:01:30Z',
                               'completed_at': '2026-10-01T00:03:00Z'}]}]},
                    {'attempt': 2, 'jobs': [{'id': 22, 'name': 'build',
                        'run_attempt': 2, 'status': 'completed', 'conclusion': 'failure',
                        'started_at': '2026-10-01T01:00:00Z',
                        'completed_at': '2026-10-01T01:20:00Z', 'runner_id': 0,
                        'steps': [], 'logs': 'secret-log-not-for-report'}]}]}

    def test_ci_metrics_separates_wait_execution_and_unallocated_attempts(self):
        from scripts import ci_metrics
        report = ci_metrics.build_report(self.evidence())
        self.assertEqual(report['run']['head_sha'], 'a' * 40)
        self.assertEqual(report['run']['run_attempt'], 2)
        allocated, unallocated = report['jobs']
        self.assertEqual(allocated['attempt'], 1)
        self.assertEqual(allocated['dependency_wait_seconds'], 60)
        self.assertEqual(allocated['runner_allocation_seconds'], 30)
        self.assertEqual(allocated['execution_seconds'], 90)
        self.assertEqual(allocated['steps'][1]['duration_seconds'], 90)
        self.assertEqual(unallocated['allocation'], 'unallocated')
        self.assertIsNone(unallocated['execution_seconds'])
        self.assertIsNone(unallocated['runner_allocation_seconds'])
        self.assertNotIn('secret-log-not-for-report', json.dumps(report))


    def test_ci_metrics_missing_runner_steps_and_timestamps_stay_unknown(self):
        from scripts import ci_metrics
        evidence = self.evidence()
        job = evidence['attempts'][0]['jobs'][0]
        job.pop('created_at')
        report = ci_metrics.build_report(evidence)
        self.assertIsNone(report['jobs'][0]['dependency_wait_seconds'])
        self.assertIsNone(report['jobs'][0]['runner_allocation_seconds'])
        self.assertEqual(report['jobs'][0]['execution_seconds'], 90)
        for change in ({'runner_id': None}, {'steps': []},
                       {'steps': [{'name': 'Set up job', 'started_at': job['started_at']}]},
                       {'steps': [{'name': 'Skipped', 'conclusion': 'skipped', 'started_at': job['started_at']}]}):
            with self.subTest(change=change):
                candidate = self.evidence()
                candidate['attempts'][0]['jobs'][0].update(change)
                result = ci_metrics.build_report(candidate)['jobs'][0]
                self.assertEqual(result['allocation'], 'unallocated')
                self.assertIsNone(result['execution_seconds'])
        self.assertIsNone(ci_metrics.elapsed('bad', 'bad'))
        self.assertIsNone(ci_metrics.elapsed('2026-10-01T01:00:00Z', '2026-10-01T00:00:00Z'))

    def test_ci_metrics_preserves_attempt_results_and_reuse_job_evidence(self):
        from scripts import ci_metrics
        evidence = self.evidence()
        evidence['attempts'][0]['run'] = dict(evidence['run'], run_attempt=1, conclusion='success')
        evidence['attempts'][1]['run'] = evidence['run']
        for result in ('success', 'failure', 'skipped'):
            with self.subTest(result=result):
                evidence['attempts'][0]['jobs'][0].update(name='reuse', conclusion=result)
                report = ci_metrics.build_report(evidence)
                self.assertEqual(report['attempts'][0]['conclusion'], 'success')
                self.assertEqual(report['attempts'][1]['conclusion'], 'failure')
                self.assertEqual(report['release_job_evidence'][0]['conclusion'], result)
                self.assertIsNone(report['release']['path'])

    def test_skipped_jobs_do_not_report_runner_allocation_failure(self):
        from scripts import ci_metrics
        evidence = self.evidence()
        job = evidence['attempts'][1]['jobs'][0]
        job.update(conclusion='skipped')
        report = ci_metrics.build_report(evidence)
        self.assertEqual(report['jobs'][1]['allocation'], 'not_requested')
        self.assertIsNone(report['jobs'][1]['execution_seconds'])

    def test_runner_capacity_reason_comes_from_annotations_not_duration(self):
        from scripts import ci_metrics
        evidence = self.evidence()
        job = evidence['attempts'][1]['jobs'][0]
        job['annotations'] = [{'message': 'The job was not acquired by Runner of type hosted even after multiple attempts'},
                              {'message': 'Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times.'}]
        report = ci_metrics.build_report(evidence)
        self.assertEqual(report['jobs'][1]['allocation_failure_reason'], 'hosted_runner_unavailable')
        self.assertTrue(report['jobs'][1]['capacity_warning'])
        job['annotations'] = []
        self.assertIsNone(ci_metrics.build_report(evidence)['jobs'][1]['allocation_failure_reason'])

    def test_capacity_only_annotation_is_visible_in_summary(self):
        from scripts import ci_metrics
        evidence = self.evidence()
        evidence['attempts'][1]['jobs'][0]['annotations'] = [
            {'message': 'Due to capacity constraints, jobs may experience longer queue times.'}]
        report = ci_metrics.build_report(evidence)
        self.assertTrue(report['jobs'][1]['capacity_warning'])
        self.assertIsNone(report['jobs'][1]['allocation_failure_reason'])
        self.assertIn('capacity_warning', ci_metrics.markdown(report))

    def test_ci_metrics_offline_cli_writes_json_and_safe_summary(self):
        import subprocess
        from scripts import ci_metrics
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.evidence()
            evidence['attempts'][0]['jobs'][0]['name'] = 'python|<script>\nunsafe'
            source = root / 'evidence.json'
            source.write_text(json.dumps(evidence))
            output, summary = root / 'metrics.json', root / 'summary.md'
            summary.write_text('existing summary\n')
            result = subprocess.run(['python3', '-B', str(Path(ci_metrics.__file__)),
                '--input', str(source), '--output', str(output), '--summary', str(summary),
                '--release-path', 'reuse', '--reuse-reason', 'exact SHA verified'],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['release'], {'path': 'reuse', 'reuse_reason': 'exact SHA verified'})
            text = summary.read_text()
            self.assertTrue(text.startswith('existing summary\n'))
            self.assertIn('unallocated', text)
            self.assertIn('Run tests', text)
            self.assertIn('90.0', text)
            self.assertNotIn('<script>', text)
            self.assertNotIn('secret-log-not-for-report', text)


class CIMetricsCollectTest(unittest.TestCase):
    def test_api_transport_uses_get_and_sanitizes_failure(self):
        from scripts import ci_metrics
        with patch.object(ci_metrics.subprocess, 'run', return_value=Mock(
                returncode=0, stdout='{"id": 42}')) as execute:
            self.assertEqual(ci_metrics.gh_api('repos/owner/repo/actions/runs/42'), {'id': 42})
        self.assertEqual(execute.call_args.args[0],
                         ['gh', 'api', '--method', 'GET', 'repos/owner/repo/actions/runs/42'])
        with patch.object(ci_metrics.subprocess, 'run', return_value=Mock(
                returncode=1, stdout='', stderr='private diagnostic')):
            with self.assertRaises(ValueError) as failure:
                ci_metrics.gh_api('repos/owner/repo/actions/runs/42')
        self.assertNotIn('private diagnostic', str(failure.exception))

    def test_required_actions_api_failure_does_not_write_a_complete_report(self):
        from scripts import ci_metrics
        base = 'repos/owner/repo/actions/runs/42'
        endpoints = [base, base + '/attempts/1/jobs?per_page=100&page=1',
                     base + '/attempts/1']
        for failed_endpoint in endpoints:
            with self.subTest(endpoint=failed_endpoint):
                def api(endpoint):
                    if endpoint == failed_endpoint:
                        raise ValueError('private diagnostic')
                    return {'run_attempt': 1} if endpoint == base else {'jobs': []}
                with tempfile.TemporaryDirectory() as directory, \
                        patch.object(ci_metrics, 'gh_api', side_effect=api), \
                        patch.object(ci_metrics.subprocess, 'Popen', side_effect=AssertionError('API must be mocked')) as spawn, \
                        redirect_stderr(io.StringIO()) as errors:
                    output = Path(directory) / 'metrics.json'
                    self.assertEqual(ci_metrics.main(['--repository', 'owner/repo', '--run-id', '42',
                                                      '--output', str(output)]), 1)
                    self.assertFalse(output.exists())
                    self.assertNotIn('private diagnostic', errors.getvalue())
                    spawn.assert_not_called()

    def test_collect_paginates_jobs_annotations_and_attempts_without_spawning(self):
        from scripts import ci_metrics
        base = 'repos/owner/repo/actions/runs/42'
        jobs = [{'id': index, 'name': 'job-' + str(index), 'conclusion': 'success'}
                for index in range(1, 102)]
        jobs[0].update(conclusion='failure', runner_id=0,
                       check_run_url='https://api.github.com/repos/owner/repo/check-runs/77')
        annotation = 'repos/owner/repo/check-runs/77/annotations?per_page=100&page='
        responses = {
            base: {'id': 42, 'run_attempt': 2},
            base + '/attempts/1/jobs?per_page=100&page=1': {'jobs': jobs[:100]},
            base + '/attempts/1/jobs?per_page=100&page=2': {'jobs': [jobs[0], jobs[100]]},
            base + '/attempts/2/jobs?per_page=100&page=1': {'jobs': [dict(jobs[100])]},
            base + '/attempts/1': {'conclusion': 'failure'},
            base + '/attempts/2': {'conclusion': 'success'},
            annotation + '1': [{'message': 'unrelated'} for _ in range(100)],
            annotation + '2': [{'message': 'Due to capacity constraints, queue times may increase.'}],
        }
        with patch.object(ci_metrics, 'gh_api', side_effect=lambda endpoint: responses[endpoint]) as api, \
                patch.object(ci_metrics.subprocess, 'Popen', side_effect=AssertionError('API must be mocked')) as spawn:
            evidence = ci_metrics.collect('owner/repo', '42')
            spawn.assert_not_called()
        self.assertCountEqual([call.args[0] for call in api.call_args_list], responses)
        self.assertEqual([len(batch['jobs']) for batch in evidence['attempts']], [101, 1])
        self.assertEqual(len(evidence['attempts'][0]['jobs'][0]['annotations']), 101)
        report = ci_metrics.build_report(evidence)
        self.assertEqual([batch['conclusion'] for batch in report['attempts']], ['failure', 'success'])
        self.assertEqual([job['attempt'] for job in report['jobs']], [1] * 101 + [2])
        self.assertTrue(report['jobs'][0]['capacity_warning'])

    def test_optional_annotation_failure_preserves_cli_metrics_and_marks_evidence_unavailable(self):
        import subprocess
        from scripts import ci_metrics
        base = 'repos/owner/repo/actions/runs/42'
        for failure in (ValueError('private API diagnostic'), OSError('private API diagnostic'),
                        subprocess.TimeoutExpired('gh', 60)):
            for failed_page in (1, 2):
                with self.subTest(failure=type(failure).__name__, page=failed_page):
                    evidence = CIMetricsTest.evidence()
                    job = evidence['attempts'][1]['jobs'][0]
                    job['check_run_url'] = 'https://api.github.com/repos/owner/repo/check-runs/77'
                    responses = {
                        base: evidence['run'],
                        base + '/attempts/1/jobs?per_page=100&page=1': {'jobs': evidence['attempts'][0]['jobs']},
                        base + '/attempts/2/jobs?per_page=100&page=1': {'jobs': [job]},
                        base + '/attempts/1': {'conclusion': 'success'},
                        base + '/attempts/2': {'conclusion': 'failure'},
                    }
                    def api(endpoint):
                        if '/annotations?' in endpoint:
                            if endpoint.endswith('page=' + str(failed_page)):
                                raise failure
                            return [{'message': 'capacity constraints'} for _ in range(100)]
                        return responses[endpoint]
                    with tempfile.TemporaryDirectory() as directory, \
                            patch.object(ci_metrics, 'gh_api', side_effect=api), \
                            patch.object(ci_metrics.subprocess, 'Popen', side_effect=AssertionError('API must be mocked')) as spawn, \
                            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                        output, summary = Path(directory) / 'metrics.json', Path(directory) / 'summary.md'
                        result = ci_metrics.main(['--repository', 'owner/repo', '--run-id', '42',
                                                  '--output', str(output), '--summary', str(summary)])
                        self.assertEqual(result, 0, errors.getvalue())
                        report = json.loads(output.read_text())
                        self.assertEqual(len(report['jobs']), 2)
                        self.assertEqual(report['jobs'][0]['execution_seconds'], 90)
                        self.assertEqual(report['jobs'][1]['annotation_evidence'], 'unavailable')
                        self.assertIsNone(report['jobs'][1]['allocation_failure_reason'])
                        self.assertFalse(report['jobs'][1]['capacity_warning'])
                        self.assertIn('annotation evidence unavailable', summary.read_text())
                        self.assertNotIn('private API diagnostic', output.read_text() + summary.read_text())
                        spawn.assert_not_called()


class CIMetricsWorkflowTest(unittest.TestCase):
    def test_metrics_workflow_is_read_only_and_uses_trusted_checkout(self):
        from tests.test_release import WorkflowConfigurationTest
        path = Path(__file__).resolve().parents[1] / '.github/workflows/ci-metrics.yml'
        self.assertTrue(path.exists(), 'parent agent must implement isolated ci-metrics.yml')
        workflow = WorkflowConfigurationTest().workflow('ci-metrics.yml')
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'actions': 'read', 'checks': 'read'})
        steps = [step for job in workflow['jobs'].values() for step in job['steps']]
        checkouts = [step for step in steps if step.get('uses', '').startswith('actions/checkout@')]
        self.assertTrue(checkouts)
        for checkout in checkouts:
            self.assertIs(checkout['with']['persist-credentials'], False)
            self.assertIn('default_branch', checkout['with']['ref'])
        collectors = [step for step in steps if 'scripts/ci_metrics.py' in step.get('run', '')]
        self.assertTrue(collectors)
        for step in collectors:
            self.assertIn('env', step)
            self.assertIn('RUN_ID', step['env'])
            self.assertIn('--run-id', step['run'])
            self.assertIn('$RUN_ID', step['run'])
            self.assertNotIn('${{', step['run'])
        self.assertNotIn('--log', path.read_text())
        self.assertNotIn('/logs', path.read_text())

    def test_release_metrics_receives_prepare_reason_and_all_gate_results(self):
        from tests.test_release import WorkflowConfigurationTest
        jobs = WorkflowConfigurationTest().workflow('release.yml')['jobs']
        self.assertIn('timings', jobs)
        job = jobs['timings']
        self.assertIs(job.get('continue-on-error'), True, 'Observational metrics must not fail a completed release')
        self.assertEqual(set(job['needs']), {'prepare', 'reuse', 'tests', 'node', 'browser', 'build', 'publish'})
        self.assertEqual(job['if'], 'always()')
        self.assertEqual(job['permissions'], {'contents': 'read', 'actions': 'read', 'checks': 'read'})
        step = next(step for step in job['steps'] if 'scripts/ci_metrics.py' in step.get('run', ''))
        self.assertIn('needs.prepare.outputs.artifact_reuse_reason', step['env']['REUSE_REASON'])
        self.assertIn('--reuse-reason "$REUSE_REASON"', step['run'])
        self.assertNotIn('${{', step['run'])


class EmptySelection(unittest.TestCase):
    pass


class ValidationPlanningTest(unittest.TestCase):
    def test_selected_rejects_empty_and_overlapping_expansions(self):
        with patch.object(test_parallel.subprocess, 'Popen', side_effect=AssertionError('Rejected selections must never spawn')) as spawn:
            for selectors in ([__name__ + '.EmptySelection'],
                              [__name__ + '.ValidationPlanningTest', __name__ + '.ValidationPlanningTest.test_selected_rejects_empty_and_overlapping_expansions']):
                with self.subTest(selectors=selectors), patch('sys.argv', ['selected', *selectors]), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        test_selected.main()
                    self.assertNotEqual(result.exception.code, 0)
                    spawn.assert_not_called()

    def test_duplicate_selectors_are_idempotent_but_overlap_and_empty_stay_rejected(self):
        selector = __name__ + '.ValidationPlanningTest.test_selected_rejects_empty_and_overlapping_expansions'
        with patch('sys.argv', ['selected', selector, selector]), \
                patch.object(test_selected, 'run_groups', return_value=0) as run, redirect_stderr(io.StringIO()):
            self.assertEqual(test_selected.main(), 0)
        self.assertEqual(run.call_args.args[0], [[selector]])
        whole = __name__ + '.ValidationPlanningTest'
        loader = unittest.TestLoader()
        expected = list(test_parallel.test_ids(loader.loadTestsFromName(whole)))
        expanded = test_selected.expand([whole, whole])
        self.assertEqual(expanded, expected)
        self.assertTrue(len(expanded) > 1 and len(expanded) == len(set(expanded)))
        self.assertEqual(test_selected.expand([selector, selector,
                                               __name__ + '.ValidationPlanningTest.test_selected_keeps_same_module_fixtures_in_one_worker']),
                         [selector, __name__ + '.ValidationPlanningTest.test_selected_keeps_same_module_fixtures_in_one_worker'])
        with patch.object(test_parallel.subprocess, 'Popen', side_effect=AssertionError('Rejected selections must never spawn')) as spawn:
            with self.assertRaises(test_selected.SelectionError):
                test_selected.expand([whole, selector])
            with patch('sys.argv', ['selected', whole, selector]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    test_selected.main()
                self.assertNotEqual(result.exception.code, 0)
            with patch('sys.argv', ['selected', __name__ + '.EmptySelection']), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    test_selected.main()
                self.assertNotEqual(result.exception.code, 0)
            spawn.assert_not_called()

    def test_selected_keeps_same_module_fixtures_in_one_worker(self):
        names = [__name__ + '.ValidationPlanningTest.test_selected_rejects_empty_and_overlapping_expansions',
                 __name__ + '.ValidationPlanningTest.test_selected_keeps_same_module_fixtures_in_one_worker']
        with patch('sys.argv', ['selected', *names]), patch.object(test_selected, 'run_groups', return_value=0) as run:
            self.assertEqual(test_selected.main(), 0)
        self.assertEqual(len(run.call_args.args[0]), 1)

    def test_worker_preserves_class_fixture_once_and_rejects_empty(self):
        from scripts import test_worker
        events = []
        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls): events.append('setup')
            @classmethod
            def tearDownClass(cls): events.append('teardown')
            def test_a(self): events.append('a')
            def test_b(self): events.append('b')
        def load(name):
            return unittest.TestSuite([Case(name)]) if name.startswith('test_') else unittest.TestSuite()
        with tempfile.TemporaryDirectory() as directory, patch.object(unittest.TestLoader, 'loadTestsFromName', side_effect=load), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            output = Path(directory) / 'timing.json'
            self.assertEqual(test_worker.run(['test_a', 'test_b'], output), 0)
            self.assertEqual(events, ['setup', 'a', 'b', 'teardown'])
            with self.assertRaises(ValueError):
                test_worker.run(['empty'], output)

    def test_selected_tests_ignore_invalid_timing_cache_and_run_every_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'durations.json'
            for content in ('{broken', '[]', '{"tests.fast": "slow"}', '{"tests.fast": -1}'):
                with self.subTest(content=content):
                    cache.write_text(content)
                    argv = ['test_selected.py', '--durations', str(cache), 'tests.fast', 'tests.slow']
                    with patch('sys.argv', argv), patch.object(test_selected, 'run_groups', return_value=0) as execute:
                        self.assertEqual(test_selected.main(), 0)
                    self.assertCountEqual(sum(execute.call_args.args[0], []), ['tests.fast', 'tests.slow'])

    def test_real_release_patch_is_metadata_only_but_any_extra_or_edited_content_is_full(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(release, 'ROOT', Path(directory)):
            root = Path(directory)
            contents = {'plugin.json':'{"version":"1.2.3"}\n','update.json':'{"version":"1.2.3"}\n',
                        'pyproject.toml':'version = "1.2.3"\n','adaptive_rsi/__init__.py':'__version__ = "1.2.3"\n',
                        'CHANGELOG.md':'# 版本\n\n## Unreleased\n\n- 改进。\n\n## 1.2.3\n',
                        'CHANGELOG.en.md':'# Changelog\n\n## Unreleased\n\n- Improvement.\n\n## 1.2.3\n'}
            for name, value in contents.items():
                (root/name).parent.mkdir(parents=True, exist_ok=True)
                (root/name).write_text(value)
            git(root,'init','-q');git(root,'add','.')
            git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','基础')
            base=git(root,'rev-parse','HEAD')
            for name,value in release.bump_files(base,'1.2.4').items():(root/name).write_text(value)
            git(root,'add','.');git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','准备版本')
            mechanical=git(root,'rev-parse','HEAD')
            self.assertEqual(classify_changes.classify(base,mechanical),'version')
            git(root,'update-index','--chmod=+x','adaptive_rsi/__init__.py')
            git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','改变执行权限')
            self.assertEqual(classify_changes.classify(base,'HEAD'),'full')
            blob=git(root,'rev-parse',mechanical+':adaptive_rsi/__init__.py')
            git(root,'update-index','--cacheinfo','120000,'+blob+',adaptive_rsi/__init__.py')
            git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','改变文件类型')
            self.assertEqual(classify_changes.classify(base,'HEAD'),'full')
            (root/'adaptive_rsi/__init__.py').write_text((root/'adaptive_rsi/__init__.py').read_text()+'executable_change = True\n')
            git(root,'add','.');git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','额外改动')
            self.assertEqual(classify_changes.classify(base,'HEAD'),'full')
            # A generated version patch plus an unrelated file also requires the full gate.
            (root/'adaptive_rsi/__init__.py').write_text(release.bump_files(base,'1.2.4')['adaptive_rsi/__init__.py'])
            (root/'extra.py').write_text('value = 1\n')
            git(root,'add','.');git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','增加文件')
            self.assertEqual(classify_changes.classify(base,'HEAD'),'full')
            self.assertEqual(classify_changes.classify('HEAD','HEAD'),'full')

    def test_actual_timing_weights_balance_skewed_modules_without_losing_tests(self):
        weights = [100,1,90,1,80,1,70,1]
        def suite():
            cases=[]
            for index in range(8):
                cls=type('Case',(unittest.TestCase,),{'__module__':'tests.synthetic_'+str(index),'test_one':lambda self:None})
                cases.extend(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
            return unittest.TestSuite(cases)
        loader=Mock(errors=[])
        loader.discover.side_effect=lambda *args,**kwargs:suite()
        loader.loadTestsFromNames.side_effect=lambda names:suite()
        durations={'tests.synthetic_'+str(index):weight for index,weight in enumerate(weights)}
        with patch.object(test_parallel.unittest,'TestLoader',return_value=loader):
            before=test_parallel.discover_groups(workers=2)
            after=test_parallel.discover_groups(workers=2,durations=durations)
        total=lambda group:sum(durations[name] for name in group)
        self.assertEqual(sorted(total(group) for group in before),[4,340])
        self.assertEqual([total(group) for group in after],[172,172])
        self.assertCountEqual(sum(before,[]),sum(after,[]))

    def test_invalid_timing_cache_never_omits_tests_or_crashes_the_planner(self):
        for value in ([],None,{'name':True},{'name':-1},{'name':float('nan')},{'name':float('inf')},{'name':'100'},{'name':10**1000}):
            with self.subTest(value=str(value)[:30]):
                self.assertEqual(test_parallel.timing_cache(value),{})
        self.assertEqual(test_parallel.timing_cache({'known':.5,'unknown':None}),{'known':.5})
        with tempfile.TemporaryDirectory() as directory:
            destination=Path(directory)/'timings.json';destination.write_text('{invalid')
            with patch.object(test_parallel,'discover_groups',return_value=[['all-tests']]) as discover,patch.object(test_parallel,'run_groups',return_value=0) as execute,redirect_stderr(io.StringIO()):
                self.assertEqual(test_parallel.main(['--durations',str(destination)]),0)
            self.assertEqual(discover.call_args.kwargs['durations'],{})
            self.assertEqual(execute.call_args.args[0],[['all-tests']])
            with patch.object(Path,'read_text',side_effect=PermissionError('cache unreadable')),patch.object(test_parallel,'discover_groups',return_value=[['all-tests']]) as discover,patch.object(test_parallel,'run_groups',return_value=0),redirect_stderr(io.StringIO()):
                self.assertEqual(test_parallel.main(['--durations',str(destination)]),0)
            self.assertEqual(discover.call_args.kwargs['durations'],{})

    def test_per_selector_timing_does_not_charge_one_selector_for_another(self):
        from scripts import test_worker
        class Passing(unittest.TestCase):
            def runTest(self):pass
        with tempfile.TemporaryDirectory() as directory:
            destination=Path(directory)/'timings.json'
            with tempfile.TemporaryFile(mode='w+') as errors,patch.object(unittest.defaultTestLoader,'loadTestsFromName',side_effect=lambda name:unittest.TestSuite([type('Passing_'+name,(unittest.TestCase,),{'runTest':lambda self:None})()])),patch.object(test_worker.time,'monotonic',side_effect=[10,11,20,30]),redirect_stdout(io.StringIO()),redirect_stderr(errors):
                self.assertEqual(test_worker.run(['fast','slow'],destination),0)
            self.assertEqual(json.loads(destination.read_text()),{'fast':1,'slow':10})

    def test_worker_refuses_to_spawn_another_runner(self):
        import os
        from scripts import test_parallel
        calls = []
        def intercept(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('recursive worker must never spawn')
        with patch.dict(os.environ, {'RSI_TEST_RUNNING': __name__}), patch.object(test_parallel.subprocess, 'Popen', intercept):
            for selector in (__name__, __name__ + '.ValidationPlanningTest',
                             __name__ + '.ValidationPlanningTest.test_worker_refuses_to_spawn_another_runner'):
                with self.subTest(selector=selector):
                    with self.assertRaisesRegex(ValueError, 'already running'):
                        test_parallel.run_groups([[selector]])
                    self.assertEqual(calls, [])

    def test_matrix_checker_rejects_duplicate_invalid_and_out_of_scope_selectors(self):
        import types
        from scripts import check_test_matrix
        from scripts.test_parallel import test_ids
        loader = unittest.TestLoader()
        expected = set(test_ids(loader.discover(str(test_parallel.ROOT), top_level_dir=str(test_parallel.ROOT))))
        self.assertTrue(expected)
        module_selector = __name__ + '.CIMetricsTest'
        real = __name__ + '.ValidationPlanningTest.test_matrix_checker_rejects_duplicate_invalid_and_out_of_scope_selectors'
        empty_selector = 'tests.browser_fixture'
        probe = types.ModuleType('tests.probe_out_of_discovery')
        setattr(probe, 'OutOfScopeCase', type('OutOfScopeCase', (unittest.TestCase,),
               {'__module__': 'tests.probe_out_of_discovery', 'runTest': lambda self: None}))
        sys.modules['tests.probe_out_of_discovery'] = probe
        setattr(sys.modules['tests'], 'probe_out_of_discovery', probe)
        self.addCleanup(sys.modules.pop, 'tests.probe_out_of_discovery', None)
        self.addCleanup(delattr, sys.modules['tests'], 'probe_out_of_discovery')
        out_of_scope = 'tests.probe_out_of_discovery.OutOfScopeCase'
        plain_matrix = [selector for group in check_test_matrix.matrix_groups(
            test_parallel.ROOT / '.github/workflows/checks.yml') for selector in group]
        cases = [('duplicate in-group selector', 'repeats tests', [module_selector, real, real]),
                 ('invalid selector', 'Invalid CI selector', [module_selector, real, 'tests.probe_matrix_nonexistent']),
                 ('selector expanding to no test', 'expands to no test', [module_selector, real, empty_selector]),
                 ('selector outside discovery', 'outside discovery', [module_selector, real, out_of_scope])]
        for label, fragment, selectors in cases:
            with self.subTest(case=label), patch.object(check_test_matrix, 'matrix_groups',
                                                        return_value=[selectors]):
                with self.assertRaisesRegex(ValueError, fragment):
                    check_test_matrix.check()

    def test_matrix_checker_overlaps_and_discovered_count(self):
        from scripts import check_test_matrix
        # Real matrix: one pair of identical interpreter shards (Python
        # 3.9/3.14) plus four jobs deliberately re-running slow core modules.
        groups = check_test_matrix.matrix_groups(test_parallel.ROOT / '.github/workflows/checks.yml')
        self.assertEqual(check_test_matrix.overlap_report(groups), (1, 4))
        self.assertEqual(check_test_matrix.overlap_report([['a'], ['a'], ['b']]), (1, 0))
        self.assertEqual(check_test_matrix.overlap_report([['a'], ['a', 'b']]), (0, 1))
        self.assertEqual(check_test_matrix.overlap_report([['a', 'b'], ['b']]), (0, 1))
        self.assertEqual(check_test_matrix.overlap_report([['a'], ['b']]), (0, 0))
        from scripts.test_parallel import test_ids
        loader = unittest.TestLoader()
        discovered = set(test_ids(loader.discover(str(test_parallel.ROOT), top_level_dir=str(test_parallel.ROOT))))
        self.assertEqual(check_test_matrix.check(), len(discovered))

    def test_worker_parent_watch_exits_when_parent_changes(self):
        from scripts import test_worker
        class ParentGone(BaseException):
            pass
        with patch.object(test_worker.time, 'sleep') as sleep, patch.object(test_worker.os, 'getppid', side_effect=[123, 1]), patch.object(test_worker.os, '_exit', side_effect=ParentGone) as exit_process:
            with self.assertRaises(ParentGone):
                test_worker.watch_parent(123)
            self.assertEqual(sleep.call_count, 2)
            exit_process.assert_called_once_with(1)

    def test_changed_classifier_cannot_select_the_metadata_shortcut_in_workflow(self):
        import os
        import subprocess
        from tests.test_release import WorkflowConfigurationTest
        workflow=WorkflowConfigurationTest().workflow('checks.yml')
        script=next(step['run'] for step in workflow['jobs']['changes']['steps'] if step.get('id')=='classify')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'README.md').write_text('base\n')
            git(root,'init','-q');git(root,'add','.')
            git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','基础')
            base=git(root,'rev-parse','HEAD')
            (root/'scripts').mkdir()
            (root/'scripts/classify_changes.py').write_text("from pathlib import Path\nPath('unsafe-executed').write_text('bad')\nprint('version')\n")
            git(root,'add','.');git(root,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','修改分类器')
            output=root/'output'
            result=subprocess.run(['bash','-e','-c',script.replace('/tmp/rsi-changed-paths',str(root/'paths'))],cwd=root,
                env={**os.environ,'EVENT_NAME':'pull_request','BASE_SHA':base,'BASE_REF':'main','BEFORE_SHA':'','GITHUB_OUTPUT':str(output)},
                text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('verification_mode=full',output.read_text())
            self.assertFalse((root/'unsafe-executed').exists())


class WorkerDiagnosticsTest(unittest.TestCase):
    """Diagnostics must localize a stalled worker without dispatching it again."""

    @staticmethod
    def transcript():
        from scripts import test_worker
        lines = []
        return test_worker.Progress(emit=lines.append), lines

    def test_worker_marks_running_finished_timings_and_completion(self):
        from scripts import test_worker
        events = []
        class Case(unittest.TestCase):
            def test_a(self): events.append('a')
            def test_b(self): events.append('b')
        def load(name):
            return unittest.TestSuite([Case(name)])
        progress, lines = self.transcript()
        chosen = 'test_a test_b'
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(unittest.defaultTestLoader, 'loadTestsFromName', side_effect=load), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            output = Path(directory) / 'timing.json'
            self.assertEqual(test_worker.run(['test_a', 'test_b'], output, progress=progress), 0)
            self.assertTrue(output.exists())
        text = '\n'.join(lines)
        for phase in ('loading', 'running', 'finished', 'timings', 'completed'):
            self.assertIn('phase=' + phase, text)
        self.assertIn('phase=running selector=' + chosen, text)
        self.assertIn('phase=finished selector=' + chosen, text)
        self.assertIn('phase=timings selector=' + chosen, text)
        order = [text.index('phase=' + phase) for phase in
                 ('loading', 'running', 'finished', 'timings', 'completed')]
        self.assertEqual(order, sorted(order))

    def test_stall_diagnostic_reports_phase_named_threads_and_only_owned_children(self):
        from scripts import test_worker
        lines, gate = [], threading.Event()
        progress = test_worker.Progress(emit=lines.append)
        progress.set('running', 'tests.demo.Case')
        probe = threading.Thread(target=gate.wait, name='rsi-diagnostic-probe', daemon=True)
        probe.start()
        own, child, sibling = os.getpid(), os.getpid() + 7, os.getpid() + 101
        rows = ('  {child}  {own} S+ /usr/bin/child\n'
                '  {sibling}  1 Ss /usr/bin/sibling\n'
                '  {own}  1 Ss /usr/bin/python\n').format(own=own, child=child, sibling=sibling)
        completed = subprocess.CompletedProcess(['ps'], 0, rows, '')
        try:
            with patch.object(test_worker.subprocess, 'run', return_value=completed) as run:
                progress.stalled('probe')
        finally:
            gate.set()
            probe.join(5)
        self.assertFalse(probe.is_alive())
        text = '\n'.join(lines)
        self.assertIn('reason=probe', text)
        self.assertIn('phase=running', text)
        self.assertIn('selector=tests.demo.Case', text)
        self.assertIn('rsi-diagnostic-probe', text)
        self.assertIn('child pid={}'.format(child), text)
        self.assertNotIn(str(sibling), text)
        self.assertEqual(run.call_args.args[0], ['ps', '-axo', 'pid=,ppid=,stat=,comm='])
        self.assertNotIn('args', ' '.join(run.call_args.args[0]))
        self.assertLessEqual(run.call_args.kwargs['timeout'], 5)

    def test_stall_diagnostic_survives_replaced_streams_without_reads_or_spawns(self):
        from scripts import test_worker
        read_fd, write_fd = os.pipe()
        try:
            progress = test_worker.Progress(emit=test_worker.emit_fd(write_fd))
            progress.set('running', 'tests.demo')
            completed = subprocess.CompletedProcess(['ps'], 0, '', '')
            with patch.dict(os.environ, {'RSI_DIAGNOSTIC_SENTINEL': 'must-not-appear'}), \
                    patch('builtins.open', side_effect=AssertionError('diagnostics must not read files')), \
                    patch.object(test_worker.subprocess, 'run', return_value=completed), \
                    patch.object(test_worker.subprocess, 'Popen',
                                 side_effect=AssertionError('diagnostics must not spawn a runner')), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                progress.stalled('redirected')
        finally:
            os.close(write_fd)
        try:
            report = os.read(read_fd, 1 << 20).decode('utf-8')
        finally:
            os.close(read_fd)
        self.assertIn('reason=redirected', report)
        self.assertIn('phase=running', report)
        self.assertIn('MainThread', report)
        self.assertNotIn('must-not-appear', report)

    def test_stall_watchdog_is_a_daemon_and_reports_after_completion(self):
        from scripts import test_worker
        lines, reported = [], threading.Event()
        def emit(line):
            lines.append(line)
            if 'diagnostic' in line:
                reported.set()
        progress = test_worker.Progress(emit=emit)
        progress.set('completed')
        stop = threading.Event()
        with patch.object(test_worker, 'describe_threads', return_value=[]), \
                patch.object(test_worker, 'describe_children', return_value=[]):
            watcher = test_worker.start_stall_watch(progress, interval=0.01, stop=stop)
            try:
                self.assertTrue(reported.wait(10))
                self.assertTrue(watcher.daemon)
                self.assertEqual(watcher.name, 'rsi-test-stall-watch')
            finally:
                stop.set()
                watcher.join(5)
        self.assertFalse(watcher.is_alive())
        self.assertIn('phase=completed', '\n'.join(lines))

    def test_crash_dumps_are_process_wide_and_never_cancelled_between_groups(self):
        from scripts import test_worker
        class Fast(unittest.TestCase):
            def test_one(self): pass
        class Slow(unittest.TestCase):
            def test_two(self): pass
        mapping = {'fast': Fast('test_one'), 'slow': Slow('test_two')}
        def load(name):
            return unittest.TestSuite([mapping[name]])
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(unittest.defaultTestLoader, 'loadTestsFromName', side_effect=load), \
                patch.object(test_worker.faulthandler, 'dump_traceback_later') as schedule, \
                patch.object(test_worker.faulthandler, 'cancel_dump_traceback_later') as cancel, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertTrue(test_worker.install_crash_dumps(30, stream))
            self.assertEqual(test_worker.run(['fast', 'slow'], Path(directory) / 'timing.json'), 0)
        schedule.assert_called_once_with(30, repeat=True, file=stream)
        cancel.assert_not_called()

    def test_child_listing_without_ps_reports_unavailable_instead_of_failing(self):
        from scripts import test_worker
        with patch.object(test_worker.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired('ps', 5)) as run:
            lines = test_worker.describe_children()
        self.assertEqual(len(lines), 1)
        self.assertIn('children unavailable', lines[0])
        self.assertIn('TimeoutExpired', lines[0])
        self.assertLessEqual(run.call_args.kwargs['timeout'], 5)
