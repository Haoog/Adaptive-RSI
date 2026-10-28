"""CI shortcuts must preserve the complete final release gate and test discovery."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import classify_changes, release, test_parallel
from tests.test_runtime import git


class ValidationPlanningTest(unittest.TestCase):
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
            with tempfile.TemporaryFile(mode='w+') as errors,patch.object(unittest.defaultTestLoader,'loadTestsFromName',side_effect=lambda name:unittest.TestSuite([Passing()])),patch.object(test_worker.time,'monotonic',side_effect=[10,11,20,30]),redirect_stdout(io.StringIO()),redirect_stderr(errors):
                self.assertEqual(test_worker.run(['fast','slow'],destination),0)
            self.assertEqual(json.loads(destination.read_text()),{'fast':1,'slow':10})

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
