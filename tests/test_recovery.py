"""额度类故障分类与 waiting_budget 自动恢复闸门的回归检查。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.recovery import BudgetExhausted, failure
from adaptive_rsi.runtime import BUDGET_WAIT_MAX_SECONDS, BUDGET_WAIT_RETRIES, BUDGET_WAIT_SECONDS, Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import CHECKS, FakeAgent, fixture


class RecoveryTest(unittest.TestCase):
    def test_context_output_overflow_is_execution_not_budget(self):
        # 上下文/输出超限是确定性执行错误；升级期间的旧消息同样不得归入 budget，
        # 否则任务会在无额度的 waiting_budget 状态下被无限重试。
        for message in ["Native model context exceeds output budget", "Native model context exceeds output limit",
                        "context exceeds output limit", "Model response exceeds output limit"]:
            with self.subTest(message=message):
                result = failure(RuntimeError(message))
                self.assertEqual(result["code"], "execution")
                self.assertEqual(result["action"], "inspect_details")

    def test_quota_exhaustion_is_budget_by_type_or_wording(self):
        # 异常类型优先于消息文本；真实供应商的配额/额度耗尽措辞仍归 budget。
        self.assertEqual(failure(BudgetExhausted("Native model context exceeds output budget"))["code"], "budget")
        for message in ["Model call budget exhausted", "quota exceeded for current requests",
                        "monthly budget insufficient", "You exceeded your current quota, check your plan and billing details",
                        "Your credit balance is too low to access the API", "RESOURCE_EXHAUSTED: per-project quota"]:
            with self.subTest(message=message):
                self.assertEqual(failure(RuntimeError(message))["code"], "budget")


class BudgetWaitGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(self.repo)
        self.runtime.configure({"checks": CHECKS, "limits": {"retries": 0}, "evolution": {"enabled": False}}, self.project)
        self.runtime.pause(self.project, False)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def claim(self):
        task = self.runtime.goal(self.project, "Fix addition")
        return task, self.store.claim(self.project, self.runtime.config(self.project))

    def fail_with_budget(self, run):
        with patch.object(self.runtime, "execute_candidate",
                          side_effect=BudgetExhausted("quota exceeded for current requests")):
            self.runtime.process(run)

    def test_budget_failure_enters_waiting_budget_with_backoff(self):
        task, run = self.claim()
        self.fail_with_budget(run)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual(row["state"], "waiting_budget")
        self.assertEqual(row["budget_waits"], 1)
        self.assertGreater(row["available"], time.time())
        self.assertLessEqual(row["available"], time.time() + BUDGET_WAIT_SECONDS + 5)
        self.assertLessEqual(row["available"], time.time() + BUDGET_WAIT_MAX_SECONDS)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertEqual(result["failure"]["code"], "budget")
        self.assertEqual(result["failure"]["retry_at"], row["available"])

    def test_resume_budget_honors_backoff_window(self):
        task, run = self.claim()
        self.fail_with_budget(run)
        # 退避期内调度不重排。
        self.runtime.resume_budget(self.project, self.runtime.config(self.project))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "waiting_budget")
        # 退避期满后下一次调度重排。
        self.store.execute("UPDATE tasks SET available=? WHERE id=?", (time.time() - 1, task))
        self.runtime.resume_budget(self.project, self.runtime.config(self.project))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    def test_backoff_is_persisted_atomically_with_the_waiting_state(self):
        # 回归：置态与退避写入必须是同一事务。若分成两条语句，调度器可在两者之间
        # 按旧的 available 立即重排，随后按 state='waiting_budget' 过滤的写入落空，
        # 闸门被绕过。这里在 transition 落库瞬间注入 resume_budget 复现该交错。
        task, run = self.claim()
        original = Store.transition

        def interleave(store, run_id, *args, **kwargs):
            result = original(store, run_id, *args, **kwargs)
            self.runtime.resume_budget(self.project, self.runtime.config(self.project))
            return result

        with patch.object(Store, "transition", interleave), patch.object(
                self.runtime, "execute_candidate", side_effect=BudgetExhausted("quota exceeded for current requests")):
            self.runtime.process(run)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual(row["state"], "waiting_budget")
        self.assertEqual(row["budget_waits"], 1)
        self.assertGreater(row["available"], time.time())

    def test_budget_wait_cap_turns_task_blocked_and_manual_retry_rearms(self):
        task, run = self.claim()
        for _ in range(BUDGET_WAIT_RETRIES):
            self.fail_with_budget(run)
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "waiting_budget")
            # 模拟退避期满后的自动重排与重新领取。
            self.store.execute("UPDATE tasks SET available=? WHERE id=? AND state='waiting_budget'",
                               (time.time() - 1, task))
            self.runtime.resume_budget(self.project, self.runtime.config(self.project))
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.assertIsNotNone(run)
        self.fail_with_budget(run)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["budget_waits"], 0)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertTrue(result["failure"]["auto_retry_exhausted"])
        # 手动重试后闸门重新生效：下一次额度故障重新进入退避等待而不是再次 blocked。
        self.runtime.retry(task)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.fail_with_budget(run)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual(row["state"], "waiting_budget")
        self.assertEqual(row["budget_waits"], 1)

    def test_legacy_waiting_budget_row_resumes_immediately(self):
        # 升级前已处于 waiting_budget 的任务没有退避记录，下一次调度即可恢复。
        task, run = self.claim()
        self.store.transition(run["id"], "waiting_budget", {"failure": {"code": "budget"}}, True)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual(row["budget_waits"], 0)
        self.assertLessEqual(row["available"], time.time())
        self.runtime.resume_budget(self.project, self.runtime.config(self.project))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
