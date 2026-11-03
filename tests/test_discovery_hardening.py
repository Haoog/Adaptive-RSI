import contextlib
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS, merge
from adaptive_rsi.context import terms
from adaptive_rsi.discovery import related_task, related_work, sync
from adaptive_rsi.store import Store, encode


class DiscoveryHardeningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.execute("INSERT INTO projects VALUES ('p','p','/tmp/discovery-project','{}',1,0)")
        self.runtime = SimpleNamespace(store=self.store)
        self.project = {"id": "p"}
        self.config = merge(DEFAULTS, {"github": {"repository": "owner/project"}})

    def tearDown(self):
        self.temp.cleanup()

    def test_related_task_tolerates_null_and_invalid_evidence(self):
        """tasks.evidence 列是 TEXT 未加 NOT NULL；历史数据或手动插入可为 NULL。
        related_task() 之前对 NULL 裸 json.loads 抛 TypeError，逃出
        inspection 循环的 except (ValueError, OSError) 围栏，一个坏行终止整次扫描。"""
        self.store.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,state,"
                          "priority,available,created,updated) "
                          "VALUES('t1','p','inspection','fp1','null-evidence','body',NULL,'queued',50,0,0,0)")
        self.store.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,state,"
                          "priority,available,created,updated) "
                          "VALUES('t2','p','inspection','fp2','bad-json','body','not-json{','queued',50,0,0,0)")
        backlog = self.store.query("SELECT * FROM tasks WHERE project='p'")
        finding = {"title": "new defect", "path": "x.py", "quote": "x", "acceptance": ["test"]}
        self.assertIsNone(related_task(backlog, finding))

    def synchronize(self, rows):
        sync(self.runtime, self.project, self.config, lambda *args, **kwargs: encode(rows))

    def test_held_issue_revision_resumes_after_failed_or_interrupted_run(self):
        for state in ["failed", "interrupted"]:
            for change in ["open", "closed", "excluded"]:
                with self.subTest(state=state, change=change):
                    number = len(self.store.query("SELECT id FROM tasks")) + 1
                    issue = {"number": number, "title": "Old requirements", "body": "old body",
                             "labels": [], "state": "open", "updated_at": "2026-10-20T00:00:00Z",
                             "html_url": "https://github.com/owner/project/issues/" + str(number)}
                    self.synchronize([issue])
                    task = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/project:" + str(number),))
                    self.store.execute("UPDATE tasks SET state='executing' WHERE id=?", (task["id"],))
                    revised = {**issue, "title": "New requirements", "body": "new body",
                               "updated_at": "2026-10-21T00:00:00Z"}
                    if change == "closed":
                        revised["state"] = "closed"
                    elif change == "excluded":
                        revised["labels"] = [self.config["github"]["exclude_labels"][0]]
                    self.synchronize([revised])
                    self.assertEqual(self.store.one("SELECT body FROM tasks WHERE id=?", (task["id"],))["body"], "old body")
                    self.assertIn(str(number), self.store.setting("issue-sync:p")["pending"])
                    self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, task["id"]))
                    self.store = self.runtime.store = Store(self.root)
                    self.synchronize([])
                    refreshed = self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
                    self.assertNotIn(str(number), self.store.setting("issue-sync:p")["pending"])
                    if change == "open":
                        self.assertEqual((refreshed["state"], refreshed["title"], refreshed["body"]),
                                         ("queued", "New requirements", "new body"))
                        self.assertEqual(json.loads(refreshed["evidence"])[0]["updated"], revised["updated_at"])
                    else:
                        self.assertEqual(refreshed["state"], "obsolete")

    def test_unchanged_revision_and_cancelled_task_do_not_resume(self):
        issue = {"number": 1, "title": "Requirements", "body": "body", "labels": [], "state": "open",
                 "updated_at": "2026-10-20T00:00:00Z", "html_url": "https://github.com/owner/project/issues/1"}
        self.synchronize([issue])
        for state in ["failed", "interrupted", "cancelled"]:
            with self.subTest(state=state):
                self.store.execute("UPDATE tasks SET state=? WHERE fingerprint='issue:owner/project:1'", (state,))
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:owner/project:1'")["state"], state)
        self.synchronize([{**issue, "updated_at": "2026-10-21T00:00:00Z"}])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:owner/project:1'")["state"], "cancelled")

    def test_delivered_issue_tasks_ignore_edits_and_return_on_reopen(self):
        issue = {"number": 1, "title": "Requirements", "body": "body", "labels": [], "state": "open",
                 "updated_at": "2026-10-20T00:00:00Z", "comments": 2,
                 "html_url": "https://github.com/owner/project/issues/1"}
        self.synchronize([issue])
        self.store.execute("UPDATE tasks SET state='merged' WHERE fingerprint='issue:owner/project:1'")
        # 已交付任务的 Issue 普通编辑：只刷新证据，不重排。
        self.synchronize([{**issue, "body": "edited", "updated_at": "2026-10-21T00:00:00Z"}])
        row = self.store.one("SELECT * FROM tasks WHERE fingerprint='issue:owner/project:1'")
        self.assertEqual(row["state"], "merged")
        self.assertEqual(row["body"], "body")
        self.assertEqual(json.loads(row["evidence"])[0]["state"], "open")
        # 关闭再重新打开：值得重跑，任务带最新正文重新入队。
        self.synchronize([{**issue, "state": "closed", "updated_at": "2026-10-22T00:00:00Z"}])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:owner/project:1'")["state"], "merged")
        self.synchronize([{**issue, "state": "open", "body": "reopened body", "updated_at": "2026-10-23T00:00:00Z"}])
        row = self.store.one("SELECT * FROM tasks WHERE fingerprint='issue:owner/project:1'")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["body"], "reopened body")

    @staticmethod
    def issue(repository="owner/project", number=7):
        return {"number": number, "title": repository + " requirement", "body": repository + " body",
                "labels": ["bug"], "state": "open", "updated_at": "2026-10-20T00:00:00Z",
                "html_url": "https://github.com/" + repository + "/issues/" + str(number)}

    def test_same_number_in_different_repositories_preserves_tasks_and_history(self):
        for state in ["queued", "merged"]:
            for legacy in [False, True]:
                with self.subTest(state=state, legacy=legacy):
                    number = len(self.store.query("SELECT id FROM tasks")) + 1
                    alpha, beta = self.issue("owner/alpha", number), self.issue("owner/beta", number)
                    self.config["github"]["repository"] = "owner/alpha"
                    self.synchronize([alpha])
                    old = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/alpha:" + str(number),))
                    self.store.execute("UPDATE tasks SET state=?,fingerprint=? WHERE id=?",
                                       (state, "issue:" + str(number) if legacy else old["fingerprint"], old["id"]))
                    self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,'p','merged','{}',1,2)",
                                       ("run-" + old["id"], old["id"]))
                    # 种子决定行直接写库；store.defer() 已改为只对 queued/deferred
                    # 任务生效，merged 状态下用它借副作用不会再落库。
                    self.store.execute("INSERT OR REPLACE INTO task_decisions VALUES (?,?,?)",
                                       (old["id"], encode({"reason": "original decision"}), 0))
                    before = self.store.one("SELECT * FROM tasks WHERE id=?", (old["id"],))
                    self.config["github"]["repository"] = "owner/beta"
                    self.synchronize([beta])
                    self.synchronize([beta])
                    new = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/beta:" + str(number),))
                    self.assertNotEqual(new["id"], old["id"])
                    self.assertEqual((new["state"], new["body"]), ("queued", beta["body"]))
                    self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (old["id"],)), before)
                    # Returning to the old repository adopts legacy identity without changing its task/history.
                    self.config["github"]["repository"] = "OWNER/ALPHA"
                    self.synchronize([alpha])
                    self.synchronize([alpha])
                    after = self.store.one("SELECT * FROM tasks WHERE id=?", (old["id"],))
                    self.assertEqual(after, {**before, "fingerprint": "issue:owner/alpha:" + str(number)})
                    self.assertEqual(self.store.one("SELECT task FROM runs WHERE id=?", ("run-" + old["id"],))["task"], old["id"])
                    self.assertEqual(json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (old["id"],))["data"]),
                                     {"reason": "original decision"})
                    self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE fingerprint IN (?,?)",
                                                        ("issue:owner/alpha:" + str(number), "issue:owner/beta:" + str(number)))), 2)

    def test_unverifiable_legacy_issue_is_never_adopted_from_current_configuration(self):
        for evidence in [[{"number": 7}], [{"number": 7, "url": "https://github.com/other/repo/issues/7"}],
                         [{"number": 8, "url": "https://github.com/owner/project/issues/7"}], {}, [None]]:
            with self.subTest(evidence=evidence):
                self.store.execute("DELETE FROM tasks")
                legacy = self.store.add_task("p", "issue", "issue:7", "Legacy", evidence=evidence)
                before = self.store.one("SELECT * FROM tasks WHERE id=?", (legacy,))
                self.synchronize([self.issue()])
                self.synchronize([self.issue()])
                self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (legacy,)), before)
                self.assertEqual(len(self.store.query("SELECT id FROM tasks")), 2)

    def test_label_policy_relaxation_restores_unchanged_task_once_after_restart(self):
        for policy in ["required", "excluded"]:
            with self.subTest(policy=policy):
                issue = self.issue(number=1 if policy == "required" else 2)
                self.synchronize([issue])
                task = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/project:" + str(issue["number"]),))
                if policy == "required":
                    self.config["github"]["labels"] = ["security"]
                else:
                    self.config["github"]["exclude_labels"] = ["bug"]
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"], "obsolete")
                self.store.close()
                self.store = self.runtime.store = Store(self.root)
                self.config["github"]["labels"] = []
                self.config["github"]["exclude_labels"] = []
                self.synchronize([issue])
                restored = self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
                self.assertEqual(restored["state"], "queued")
                self.assertEqual(restored["evidence"], task["evidence"])
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],)), restored)

    def test_policy_relaxation_preserves_planning_decisions_without_repeated_queueing(self):
        for state in ["obsolete", "deferred", "rejected", "cancelled"]:
            with self.subTest(state=state):
                issue = self.issue(number=len(self.store.query("SELECT id FROM tasks")) + 1)
                self.synchronize([issue])
                task = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/project:" + str(issue["number"]),))
                self.store.defer(task["id"], {"reason": "Planning found no actionable change"})
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, task["id"]))
                self.config["github"]["labels"] = ["security"]
                self.synchronize([issue])
                self.config["github"]["labels"] = []
                self.synchronize([issue])
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"], state)
                self.assertEqual(json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (task["id"],))["data"]),
                                 {"reason": "Planning found no actionable change"})

    def test_old_exclusion_marker_does_not_override_a_newer_obsolete_decision(self):
        issue = self.issue()
        self.synchronize([issue])
        self.config["github"]["labels"] = ["security"]
        self.synchronize([issue])
        self.store.execute("UPDATE tasks SET updated=updated+1 WHERE source='issue'")
        self.config["github"]["labels"] = []
        self.synchronize([issue])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE source='issue'")["state"], "obsolete")

    def test_issue_revision_requeueing_a_task_drops_its_stale_reservation(self):
        issue = self.issue()
        self.synchronize([issue])
        task = self.store.one("SELECT * FROM tasks WHERE source='issue'")["id"]
        # 旧版本留下的预约键：任务已不可用，设置却还留着。
        self.store.put_setting("task-schedule:" + task, {"at": 4102444800.0, "timezone": "UTC",
                                                         "local_time": "2100-01-01T00:00:00", "fold": None})
        self.store.execute("UPDATE tasks SET state='failed',available=4102444800.0 WHERE id=?", (task,))
        self.synchronize([{**issue, "body": "revised body", "updated_at": "2026-10-21T00:00:00Z"}])
        refreshed = self.store.one("SELECT state,available FROM tasks WHERE id=?", (task,))
        self.assertEqual((refreshed["state"], refreshed["available"]), ("queued", 0))
        self.assertIsNone(self.store.setting("task-schedule:" + task))

    def test_issue_sync_page_budget_persists_and_resumes_without_losing_issues(self):
        pages = []
        def checked(args, **kwargs):
            page = int(next(value.split("=", 1)[1] for value in args if value.startswith("page=")))
            pages.append(page)
            start = (page - 1) * 100 + 1
            if start > 205:
                return "[]"
            return encode([{"number": number, "title": "Issue " + str(number), "body": "body",
                            "labels": [], "state": "open", "updated_at": "2026-10-20T00:00:00Z",
                            "html_url": "https://github.com/owner/project/issues/" + str(number)}
                           for number in range(start, min(start + 100, 206))])
        with patch("adaptive_rsi.discovery.ISSUE_SYNC_MAX_PAGES", 2):
            sync(self.runtime, self.project, self.config, checked)
            progress = self.store.setting("issue-sync:p")
            self.assertEqual(progress["page"], 3)
            self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 200)
            sync(self.runtime, self.project, self.config, checked)
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 205)
        self.assertEqual(self.store.setting("issue-sync:p")["page"], 1)

    def sync_event(self):
        return self.store.one("SELECT * FROM events WHERE kind='issues' ORDER BY seq DESC LIMIT 1")

    def test_issue_sync_counts_real_changes_instead_of_every_overlapping_scan(self):
        issue = self.issue(number=1)
        self.synchronize([issue])
        event = self.sync_event()
        data = json.loads(event["data"])
        self.assertEqual((data["scanned"], data["changed"], data["complete"]), (1, 1, True))
        self.assertEqual(event["message"], "Scanned 1 Issues; 1 updates synchronized")
        # The next cycle overlaps the previous `since` window; an unchanged scan is not an update.
        self.synchronize([issue])
        data = json.loads(self.sync_event()["data"])
        self.assertEqual((data["scanned"], data["changed"], data["complete"]), (1, 0, True))
        self.assertEqual(self.sync_event()["message"], "Scanned 1 Issues; 0 updates synchronized")
        # A real revision remains one update even when the row is read again.
        revised = {**issue, "body": "revised body", "updated_at": "2026-10-21T00:00:00Z"}
        self.synchronize([revised])
        self.synchronize([revised])
        events = self.store.query("SELECT * FROM events WHERE kind='issues' ORDER BY seq")
        self.assertEqual([json.loads(event["data"])["changed"] for event in events[-2:]], [1, 0])

    def test_issue_sync_marks_incomplete_scans_and_continues_without_false_completion(self):
        issues = [{"number": number, "title": "Issue " + str(number), "body": "body",
                   "labels": [], "state": "open", "updated_at": "2026-10-20T00:00:00Z",
                   "html_url": "https://github.com/owner/project/issues/" + str(number)}
                  for number in range(1, 102)]

        def checked(args, **kwargs):
            page = int(next(value.split("=", 1)[1] for value in args if value.startswith("page=")))
            return encode(issues[(page - 1) * 100:page * 100])

        with patch("adaptive_rsi.discovery.ISSUE_SYNC_MAX_PAGES", 1):
            sync(self.runtime, self.project, self.config, checked)
            first = self.sync_event()
            first_data = json.loads(first["data"])
            self.assertEqual((first_data["complete"], first_data["scanned"], first_data["changed"],
                              first_data["next_page"]), (False, 100, 100, 2))
            self.assertIn("sync continues next cycle", first["message"])
            # Retrying the persisted cursor completes the remaining page only.
            sync(self.runtime, self.project, self.config, checked)
            second = json.loads(self.sync_event()["data"])
            self.assertEqual((second["complete"], second["scanned"], second["changed"],
                              second["next_page"]), (True, 1, 1, 1))
            # A new cycle re-scans from the first page, but unchanged rows are not updates.
            sync(self.runtime, self.project, self.config, checked)
            third = self.sync_event()
            third_data = json.loads(third["data"])
            self.assertEqual((third_data["complete"], third_data["scanned"], third_data["changed"],
                              third_data["next_page"]), (False, 100, 0, 2))
            self.assertIn("sync continues next cycle", third["message"])
        self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE source='issue'")), 101)

    def test_skipped_issue_keeps_since_watermark_across_continuation_cycles(self):
        # 上一周期末页前跳过一行后按页预算退出；下一周期从中间页续扫到末页时，
        # progress["failures"] 必须把「有跳过」带进 since 判定，否则末页重建会用本轮
        # started 覆盖旧水印，被跳过行的 updated_at 早于新 since 而永久漏读。
        seed = [self.issue(number=1)]
        self.synchronize(seed)
        progress = self.store.setting("issue-sync:p")
        progress["since"] = "watermark-before-partial-scan"
        self.store.put_setting("issue-sync:p", progress)

        def make(number):
            return {"number": number, "title": "Issue " + str(number), "body": "body",
                    "labels": [], "state": "open", "updated_at": "2026-11-01T00:00:00Z",
                    "html_url": "https://github.com/owner/project/issues/" + str(number)}

        def bad(number):
            row = make(number)
            del row["labels"]          # apply() 内抛 KeyError → note_failure，行不完整不进 pending
            return row

        page1 = [make(n) for n in range(2, 52)] + [bad(52)] + [make(n) for n in range(53, 102)]
        page2 = [make(n) for n in range(102, 104)]

        def checked(args, **kwargs):
            page = int(next(v.split("=", 1)[1] for v in args if v.startswith("page=")))
            return encode(page1 if page == 1 else page2)

        key = "issue-sync:p"
        with patch("adaptive_rsi.discovery.ISSUE_SYNC_MAX_PAGES", 1):
            # 第一周期只读 page1(满页，非 complete):52 号跳过、failures=1 落库后 break 抛出。
            with self.assertRaises(KeyError):
                sync(self.runtime, self.project, self.config, checked)
            self.assertEqual(self.store.setting(key)["since"], "watermark-before-partial-scan")
            self.assertEqual(self.store.setting(key)["failures"], 1)
            self.assertEqual(self.store.setting(key)["page"], 2)
            # 第二周期从 page2 续扫，末页全好且本轮无新失败。prior_failures 必须保留旧水印。
            sync(self.runtime, self.project, self.config, checked)
            self.assertEqual(self.store.setting(key)["since"], "watermark-before-partial-scan")
            self.assertEqual(self.store.setting(key)["page"], 1)
            self.assertEqual(self.store.setting(key)["failures"], 0)
        self.assertIsNone(self.store.one("SELECT id FROM tasks WHERE fingerprint='issue:owner/project:52'"))

    def test_interrupted_continuation_page_preserves_prior_failures_for_since(self):
        # 区别于已有 complete-重建用例：本条钉住「中间页覆写」路径。周期1在末页前
        # 跳过一行后按页预算中断；周期2续扫到的仍是满页（非末页），本周期无新失败——
        # 此时 else 分支若无条件 progress[\"failures\"]=len(failures) 会把上周期带入的
        # prior_failures 覆写为0；周期3到末页时 has_failures 变假、since 被本周期
        # started 覆盖，周期1被跳过的行（updated_at 早于新水印）永久漏读。
        key = "issue-sync:p"
        self.synchronize([self.issue(number=1)])
        watermark = self.store.setting(key)["since"]  # 周期0完成时落库的水印

        def make(number):
            return {"number": number, "title": "Issue " + str(number), "body": "body",
                    "labels": [], "state": "open", "updated_at": "2026-11-01T00:00:00Z",
                    "html_url": "https://github.com/owner/project/issues/" + str(number)}
        bad = make(101)
        del bad["labels"]  # apply() 内 issue["labels"] 迭代 → KeyError；缺键不进 pending
        # 三个满页中夹一个末页：周期1页1(满+坏)、周期2页2(满+干净仍按预算中断)、周期3页3(末页)。
        pages = {1: [make(n) for n in range(2, 101)] + [bad],
                 2: [make(n) for n in range(102, 202)],
                 3: [make(n) for n in range(202, 204)]}
        def checked(args, **kwargs):
            page = int(next(v.split("=", 1)[1] for v in args if v.startswith("page=")))
            return encode(pages.get(page, []))
        with patch("adaptive_rsi.discovery.ISSUE_SYNC_MAX_PAGES", 1):
            with self.assertRaises(KeyError):
                sync(self.runtime, self.project, self.config, checked)  # 周期1：page1 满页中断
            self.assertEqual(self.store.setting(key)["failures"], 1)
            self.assertEqual(self.store.setting(key)["page"], 2)
            # 周期2：page2 是干净满页，仍按预算中断——prior_failures 必须带进下一周期。
            sync(self.runtime, self.project, self.config, checked)
            second = self.store.setting(key)
            self.assertEqual(second["failures"], 1)  # 回归前此处被覆写为 0
            self.assertEqual(second["page"], 3)
            # 周期3：末页 complete；has_failures 仍真 → since 保持旧水印而非本周期 started。
            sync(self.runtime, self.project, self.config, checked)
            third = self.store.setting(key)
            self.assertEqual(third["page"], 1)
            self.assertEqual(third["since"], watermark)

    def test_deterministically_broken_pending_entry_is_quarantined_after_cap(self):
        # pending 暂存行语义损坏但形状合法时，每周期 replay 必然再失败→failures 非空→
        # sync() 末抛 failures[0]，prs:/ci: 刷新与 summary event 永久 wedge。回归修复后：
        # 重试计入 attempts，超 ISSUE_SYNC_PENDING_MAX_ATTEMPTS 从 pending 移除并记
        # issue_sync_quarantined，不再让整个 sync 被单行楔死。
        from adaptive_rsi.discovery import ISSUE_SYNC_PENDING_MAX_ATTEMPTS
        from adaptive_rsi.config import fingerprint
        key = "issue-sync:p"
        # state 非 str → apply_existing/apply 内 issue[\"state\"].lower() AttributeError，确定性。
        broken = {"number": 42, "updated_at": "2026-11-01T00:00:00Z",
                  "html_url": "https://github.com/owner/project/issues/42",
                  "state": 123, "labels": [], "title": "broken", "body": "broken"}
        policy = fingerprint({"labels": self.config["github"]["labels"],
                              "exclude": self.config["github"]["exclude_labels"]})
        progress = self.store.setting(key, {})
        progress.update(repository="owner/project", policy=policy, page=1, pending={"42": broken})
        self.store.put_setting(key, progress)
        good = self.issue(number=7)
        # 前 MAX-1 周期：坏行 replay 失败计入 attempts，sync 仍抛首个失败但远端行照常处理。
        for cycle in range(ISSUE_SYNC_PENDING_MAX_ATTEMPTS - 1):
            with self.assertRaises(AttributeError):
                self.synchronize([good])
            self.assertIn("42", self.store.setting(key)["pending"])
            self.assertEqual(self.store.setting(key)["pending"]["42"]["attempts"], cycle + 1)
        # 达到上限的 replay 仍把本周期失败如实抛给调用方（证据不静默），同时把坏行
        # 从 pending 隔离并记 issue_sync_quarantined——楔死的是 entry 而不是整个 sync。
        with self.assertRaises(AttributeError):
            self.synchronize([good])
        self.assertNotIn("42", self.store.setting(key)["pending"])
        self.assertIsNotNone(self.store.one(
            "SELECT 1 FROM events WHERE kind='issue_sync_quarantined'"))
        # 下一周期坏行不再 replay：sync 完整走完远端读取并写 issues summary event。
        self.synchronize([good])
        self.assertIsNotNone(self.store.one(
            "SELECT 1 FROM tasks WHERE fingerprint='issue:owner/project:7'"))

    def test_remote_refetched_broken_issue_accumulates_attempts_toward_quarantine(self):
        # 与「pending replay 计数」互补：本条钉住「远端每周期重投同一条坏行」路径。
        # 坏行经远端扫描进来、apply() 失败、hold_failed() 暂存；若 hold_failed 把
        # attempts 覆写回 0，确定性坏行永远到不了隔离上限、sync 仍被 wedge。
        # 修复后 hold_failed 对相同 number+updated_at 保留既有 attempts 继续累加。
        from adaptive_rsi.discovery import ISSUE_SYNC_PENDING_MAX_ATTEMPTS
        broken = {"number": 42, "title": "broken", "body": "broken", "labels": [],
                  "state": 123, "updated_at": "2026-11-01T00:00:00Z",
                  "html_url": "https://github.com/owner/project/issues/42"}
        key = "issue-sync:p"
        # 远端每周期重投同一条坏行：apply 失败→hold_failed 暂存。hold_failed 只负责
        # 暂存、不计 attempts（自增在下一周期 pending replay 做）。回归前 hold_failed
        # 每次覆写 attempts=0，pending replay 拿到的永远是 0 → 永远到不了隔离上限。
        # 修复后 hold_failed 对相同 number+updated_at 保留既有 attempts，pending replay
        # 才能继续累加。第 1 周期首次暂存为 0；第 N 周期 pending replay 先累加到 N-1。
        for cycle in range(1, ISSUE_SYNC_PENDING_MAX_ATTEMPTS + 1):
            with self.assertRaises(AttributeError):
                self.synchronize([broken])
            self.assertEqual(self.store.setting(key)["pending"]["42"]["attempts"], cycle - 1)
        # attempts 已累到上限-1；下一周期 pending replay 推到上限即隔离。本周期远端不再
        # 重投该行（传 []），隔离的 pop 才不会被同一次远端扫描立即以 attempts=0 重新暂存。
        with self.assertRaises(AttributeError):
            self.synchronize([])
        self.assertNotIn("42", self.store.setting(key)["pending"])
        self.assertIsNotNone(self.store.one(
            "SELECT 1 FROM events WHERE kind='issue_sync_quarantined'"))
        # 隔离后远端再重投同一修订：作为新暂存重新计数（attempts 重置），不复活旧隔离计数。
        with self.assertRaises(AttributeError):
            self.synchronize([broken])
        self.assertEqual(self.store.setting(key)["pending"]["42"]["attempts"], 0)

    def test_malformed_pending_entry_is_removed_not_requarantined_each_cycle(self):
        # pending 条目缺 number 或不可转 int：除记 issue_sync_quarantined 外还必须把
        # 原键摘除，否则每周期重复进同一分支、无限追加 quarantine 事件。
        from adaptive_rsi.config import fingerprint
        key = "issue-sync:p"
        policy = fingerprint({"labels": self.config["github"]["labels"],
                              "exclude": self.config["github"]["exclude_labels"]})
        self.store.put_setting(key, {"repository": "owner/project", "policy": policy, "page": 1,
                                     "pending": {"bad": {"updated_at": "2026-11-01T00:00:00Z"}}})
        self.synchronize([self.issue(number=1)])
        self.assertNotIn("bad", self.store.setting(key)["pending"])
        # 第二周期远端不再返回坏条目时，pending 里也不应再出现它（只 quarantine 一次）。
        before = self.store.one("SELECT count(*) AS n FROM events WHERE kind='issue_sync_quarantined'")["n"]
        self.synchronize([self.issue(number=1)])
        after = self.store.one("SELECT count(*) AS n FROM events WHERE kind='issue_sync_quarantined'")["n"]
        self.assertEqual(after, before)
        self.assertNotIn("bad", self.store.setting(key)["pending"])

    def test_malformed_pending_shape_is_repaired_not_escaping(self):
        # 历史/损坏进度里 pending 可能是 list 等非 dict 形状；旧代码 .values() 直接
        # AttributeError 穿透出 sync()。回归修复后：重置为空、记 issue_sync_progress_repaired，
        # 本周期远端读取照常进行。
        from adaptive_rsi.config import fingerprint
        key = "issue-sync:p"
        policy = fingerprint({"labels": self.config["github"]["labels"],
                              "exclude": self.config["github"]["exclude_labels"]})
        self.store.put_setting(key, {"repository": "owner/project", "policy": policy,
                                     "page": 1, "pending": [{"number": 1}]})  # list 而非 dict
        self.synchronize([self.issue(number=1)])
        self.assertIsNotNone(self.store.one(
            "SELECT 1 FROM events WHERE kind='issue_sync_progress_repaired'"))
        # pending 已被规范化为 dict 并随本周期写回。
        self.assertIsInstance(self.store.setting(key)["pending"], dict)
        self.assertIsNotNone(self.store.one(
            "SELECT 1 FROM tasks WHERE fingerprint='issue:owner/project:1'"))

    def add_related(self, task_id, title, paths, result=None, updated=1, state="merged"):
        self.store.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,state,updated) "
                           "VALUES (?,'p','inspection',?,?,'',?,?,?)",
                           (task_id, task_id, title, encode([{"path": path} for path in paths]), state, updated))
        if result is not None:
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) "
                               "VALUES (?,?,'p','merged',?,1,2)", ("run-" + task_id, task_id, encode(result)))

    def test_related_work_ranking_preserves_ties_and_latest_results(self):
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": encode([{"path": "client.py"}])}
        self.add_related("first", "Handle client response", ["client.py"], updated=2)
        self.add_related("second", "Handle client response", ["client.py"], updated=2)
        self.add_related("newer", "Handle client response", ["client.py"], updated=3)
        self.add_related("result-path", "Timeout", [], {"files": ["client.py"], "summary": "old result"}, updated=4)
        self.add_related("word-match", "Handle client response", [], updated=9)
        self.add_related("unrelated", "Independent failure", [], updated=10)
        self.add_related("rejected", "Handle client response", ["client.py"], state="rejected")
        self.assertEqual([row["id"] for row in related_work(self.store, "p", task)],
                         ["newer", "first", "second", "result-path", "word-match"])
        self.assertEqual([row["id"] for row in related_work(self.store, "p", task, limit=2)], ["newer", "first"])
        self.assertEqual(related_work(self.store, "p", task, limit=0), [])
        self.assertEqual([row["id"] for row in related_work(self.store, "p", task, limit=-1)],
                         ["newer", "first", "second", "result-path"])
        self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) "
                           "VALUES ('new-result','result-path','p','merged',?,3,4)",
                           (encode({"files": ["other.py"], "summary": "new result"}),))
        self.assertNotIn("result-path", [row["id"] for row in related_work(self.store, "p", task)])

    def test_related_work_keeps_memory_bounded_with_large_run_evidence(self):
        count = 512
        evidence = encode([{"path": "client.py"}])
        result = encode({"files": ["client.py"], "summary": "Fixed timeout", "evaluation": {"stdout": "x" * 16384}})
        with self.store.connect() as db:
            db.executemany("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,state,updated) "
                           "VALUES (?,'p','inspection',?,'Handle client response','',?,'merged',?)",
                           (("task-" + str(i), "key-" + str(i), evidence, i) for i in range(count)))
            db.executemany("INSERT INTO runs(id,task,project,state,result,started,ended) "
                           "VALUES (?,?,'p','merged',?,1,2)",
                           (("run-" + str(i), "task-" + str(i), result) for i in range(count)))
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": evidence}
        tracemalloc.start()
        try:
            rows = related_work(self.store, "p", task)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual([row["id"] for row in rows], ["task-" + str(i) for i in range(count - 1, count - 13, -1)])
        self.assertLess(peak, 1024 * 1024, "Related-work selection retained the whole run history")

    def test_related_work_reuses_bounded_title_tokenization(self):
        evidence = [{"path": "client.py"}]
        for index in range(40):
            self.store.add_task("p", "inspection", "related-" + str(index),
                                "Handle client response " + str(index), evidence=evidence)
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": encode(evidence)}
        before = terms.cache_info()
        first = related_work(self.store, "p", task)
        second = related_work(self.store, "p", task)
        after = terms.cache_info()
        self.assertEqual(first, second)
        self.assertGreater(after.hits, before.hits)

    def test_related_work_closes_history_connection_after_invalid_evidence(self):
        self.add_related("broken", "Handle client response", ["client.py"])
        self.store.execute("UPDATE tasks SET evidence='invalid-json' WHERE id='broken'")
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": "[]"}
        opened = []
        connect = self.store.connect

        @contextlib.contextmanager
        def tracked_connection():
            with connect() as db:
                opened.append(db)
                yield db

        with patch.object(self.store, "connect", tracked_connection):
            with self.assertRaises(json.JSONDecodeError):
                related_work(self.store, "p", task)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


class IssueRequirementsTest(unittest.TestCase):
    """Real SQLite/task/run fixtures; no credentials, network or model calls."""

    def setUp(self):
        from adaptive_rsi.runtime import Runtime
        from tests.test_runtime import fixture
        self.temp = tempfile.TemporaryDirectory(prefix="issue-requirements-")
        self.root = Path(self.temp.name)
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, SimpleNamespace())
        self.project_id = self.runtime.add_project(self.repo)
        self.runtime.configure({"github": {"repository": "owner/project"},
                                "sources": {"inspection": False, "issues": False},
                                "evolution": {"enabled": False}}, self.project_id)
        self.project = self.runtime.project(self.project_id)
        self.config = self.runtime.config(self.project_id)
        self.issue = DiscoveryHardeningTest.issue()
        self.synchronize()
        self.task_id = self.store.one("SELECT id FROM tasks WHERE source='issue'")["id"]

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def synchronize(self, rows=None):
        sync(self.runtime, self.project, self.config,
             lambda *args, **kwargs: encode([self.issue] if rows is None else rows))

    def task(self):
        return self.store.one("SELECT * FROM tasks WHERE id=?", (self.task_id,))

    def update(self, body):
        self.issue.update(body=body, updated_at=self.issue["updated_at"] + "1")
        self.synchronize()

    def test_remote_updates_and_revisions_reach_actual_planning_input(self):
        from adaptive_rsi.gitops import git
        from adaptive_rsi.delivery import record_pr_feedback
        record_pr_feedback(self.store, {"id": "prior-run", "project": self.project_id, "task": self.task_id},
                           "https://example/pr/1", {"reviews": [{"id": "R1", "body": "Add regression coverage"}]})
        self.runtime.revise_task(self.task_id, "Mandatory: reject negative inputs")
        self.update("upstream v2")
        self.runtime.revise_task(self.task_id, "Keep zero valid")
        self.update("upstream v3")
        expected = "upstream v3\n\nMandatory: reject negative inputs\n\nKeep zero valid"
        self.assertEqual(self.task()["body"], expected)
        before = self.task()
        self.synchronize()
        self.assertEqual(self.task(), before)
        self.assertEqual(len(self.store.query("SELECT * FROM task_revisions")), 2)
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.assertEqual(run["task"], self.task_id)
        prompts = []

        def planning(role, prompt, *args, **kwargs):
            self.assertEqual(role, "planning")
            prompts.append(json.loads(prompt.split("\n")[-1]))
            return {"decision": "deferred", "reason": "Fixture stops after planning", "scope": [],
                    "acceptance": [], "risk": "low", "value": {
                        "impact": "medium", "effort": "low", "confidence": "high", "benefit": "Fixture",
                        "cost": "Fixture", "inaction": "Fixture", "evidence": ["User requirements"]}}

        # Execute the production process/pipeline through planning; replace only
        # external/environment probes and the model boundary.
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), git(self.repo, "rev-parse", "HEAD"), "main")), \
             patch.object(self.runtime, "issue_still_open", return_value=True), \
             patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
             patch("adaptive_rsi.discovery.issue_comment_context", return_value={"status": "loaded", "comments": []}), \
             patch.object(self.runtime, "invoke", side_effect=planning):
            self.runtime.process(run)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["task"]["body"], expected)
        self.assertEqual(prompts[0]["untrusted_pr_feedback"][0]["body"], "Add regression coverage")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "deferred")

    def test_legacy_intact_and_lost_additions_migrate_once_on_unchanged_source(self):
        # Legacy sync may have lost the first addition before another revise.
        for lost in [False, True]:
            with self.subTest(lost=lost):
                self.store.execute("DELETE FROM task_revisions")
                first_body = self.issue["body"]
                second_body = "legacy newer upstream" if lost else first_body + "\n\nfirst"
                current = second_body + "\n\nsecond"
                self.store.execute("UPDATE tasks SET body=?,evidence=?,state='deferred',updated=123,available=456 WHERE id=?",
                                   (current, encode([{"number": 7, "url": self.issue["html_url"],
                                                      "updated": self.issue["updated_at"], "state": "open"}]), self.task_id))
                with self.store.connect() as db:
                    db.executemany("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                                   [(self.task_id, first_body, "first"), (self.task_id, second_body, "second")])
                self.synchronize()
                expected_upstream = second_body if lost else first_body
                self.assertEqual(self.task()["body"], expected_upstream + "\n\nfirst\n\nsecond")
                self.assertEqual((self.task()["state"], self.task()["updated"], self.task()["available"]), ("deferred", 123, 456))
                self.store.execute("DELETE FROM task_revisions")
                self.synchronize()
                self.update("fresh upstream")
                self.assertEqual(self.task()["body"], "fresh upstream\n\nfirst\n\nsecond")

    def test_legacy_already_overwritten_body_recovers_and_revise_keeps_order(self):
        original = self.task()["body"]
        with self.store.connect() as db:
            db.execute("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                       (self.task_id, original, "lost local"))
        self.store.execute("UPDATE tasks SET body='remote after loss' WHERE id=?", (self.task_id,))
        self.runtime.revise_task(self.task_id, "new local")
        self.assertEqual(self.task()["body"], "remote after loss\n\nlost local\n\nnew local")
        self.update("latest")
        self.assertEqual(self.task()["body"], "latest\n\nlost local\n\nnew local")

    def test_legacy_sync_lost_latest_revision_but_kept_earlier_addition(self):
        original = self.task()["body"]
        with self.store.connect() as db:
            db.executemany("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                           [(self.task_id, original, "first"), (self.task_id, original + "\n\nfirst", "second")])
        self.store.execute("UPDATE tasks SET body=? WHERE id=?", (original + "\n\nfirst", self.task_id))
        self.synchronize()
        self.assertEqual(self.task()["body"], original + "\n\nfirst\n\nsecond")
        self.synchronize()
        self.assertEqual(self.task()["body"], original + "\n\nfirst\n\nsecond")

    def test_sync_and_local_revision_serialize_snapshot_updates(self):
        import concurrent.futures
        import threading
        from adaptive_rsi.discovery import _issue_task
        self.runtime.revise_task(self.task_id, "first")
        started = threading.Event()
        self.issue.update(body="concurrent upstream", updated_at="new revision")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            futures = []

            def revise():
                started.set()
                self.runtime.revise_task(self.task_id, "second")

            def lookup(db, *args):
                result = _issue_task(db, *args)
                futures.append(pool.submit(revise))
                self.assertTrue(started.wait(5))
                self.assertFalse(futures[0].done())
                return result

            with patch("adaptive_rsi.discovery._issue_task", side_effect=lookup):
                self.synchronize()
            futures[0].result(timeout=10)
        self.assertEqual(self.task()["body"], "concurrent upstream\n\nfirst\n\nsecond")
        self.update("following upstream")
        self.assertEqual(self.task()["body"], "following upstream\n\nfirst\n\nsecond")

    def test_upstream_matching_local_text_is_not_removed_or_deduplicated(self):
        self.runtime.revise_task(self.task_id, "same text")
        self.update("same text")
        self.assertEqual(self.task()["body"], "same text\n\nsame text")
        self.synchronize()
        self.assertEqual(self.task()["body"], "same text\n\nsame text")
        self.runtime.revise_task(self.task_id, "same text")
        self.update("replacement")
        self.assertEqual(self.task()["body"], "replacement\n\nsame text\n\nsame text")

    def test_active_issue_supplement_and_remote_revision_preserve_both_requirements(self):
        self.runtime.revise_task(self.task_id, "local")
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        before = self.task()
        self.update("new upstream")
        self.assertEqual(self.task(), before)
        receipt = self.runtime.revise_task(self.task_id, "added while active")
        self.assertTrue(receipt["pending"])
        self.assertEqual(self.task()["body"], before["body"] + "\n\nadded while active")
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (self.task_id,))
        failed = self.task()
        self.synchronize([])
        self.assertEqual(self.task(), failed)
        self.store.transition(run["id"], "failed", {}, True)
        self.synchronize([])
        self.assertEqual((self.task()["state"], self.task()["body"]),
                         ("queued", "new upstream\n\nlocal\n\nadded while active"))
        self.assertEqual(self.store.setting("issue-sync:" + self.project_id)["pending"], {})

    def test_parallel_issue_sync_is_consumed_after_running_issue_completes(self):
        import threading
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.issue.update(body="new feedback while working", updated_at=self.issue["updated_at"] + "1")
        sync_task = self.runtime.request(self.project_id, "issues")
        sync_run = self.store.claim(self.project_id, self.config)
        assert sync_run is not None
        self.assertEqual(sync_run["task"], sync_task)
        calls = []

        def remote(args, **kwargs):
            calls.append(args)
            return encode([self.issue]) if args[1] == "api" else "[]"

        with patch("adaptive_rsi.runtime.checked", side_effect=remote):
            worker = threading.Thread(target=self.runtime.process, args=(sync_run,))
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertTrue(calls)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (sync_run["id"],)),
                         {"state": "completed"})
        active_task = self.task()
        assert active_task is not None
        self.assertEqual(active_task["body"], "owner/project body")
        progress = self.store.setting("issue-sync:" + str(self.project_id))
        assert isinstance(progress, dict)
        self.assertIn("7", progress["pending"])
        self.store.transition(run["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        self.synchronize([])
        updated_task = self.task()
        assert updated_task is not None
        self.assertEqual((updated_task["state"], updated_task["body"]),
                         ("queued", "new feedback while working"))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                         {"state": "superseded"})

    def test_parallel_sync_finishing_after_candidate_applies_update_without_another_poll(self):
        import threading
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.issue.update(body="feedback received during candidate finish", updated_at=self.issue["updated_at"] + "1")
        self.runtime.request(self.project_id, "issues")
        sync_run = self.store.claim(self.project_id, self.config)
        assert sync_run is not None
        entered, resume = threading.Event(), threading.Event()

        def remote(args, **kwargs):
            if args[1] == "api":
                entered.set()
                self.assertTrue(resume.wait(10))
                return encode([self.issue])
            return "[]"

        try:
            with patch("adaptive_rsi.runtime.checked", side_effect=remote):
                worker = threading.Thread(target=self.runtime.process, args=(sync_run,))
                worker.start()
                self.assertTrue(entered.wait(5))
                self.store.transition(run["id"], "awaiting_approval", {
                    "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
                resume.set()
                worker.join(10)
                self.assertFalse(worker.is_alive())
        finally:
            resume.set()
        updated = self.task()
        assert updated is not None
        self.assertEqual((updated["state"], updated["body"]),
                         ("queued", "feedback received during candidate finish"))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                         {"state": "superseded"})

    def test_active_issue_finish_requeues_held_feedback_before_publish(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.issue.update(body="new pending requirement", updated_at=self.issue["updated_at"] + "1")
        self.synchronize()
        self.store.transition(run["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                         {"state": "superseded"})
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task_id,)),
                         {"state": "blocked"})
        self.synchronize([])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task_id,)),
                         {"state": "queued"})

    def test_held_feedback_waits_for_running_issue_before_replaying(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.issue.update(body="updated while working", updated_at=self.issue["updated_at"] + "1")
        self.synchronize()
        self.runtime.schedule(self.project_id, self.config)
        self.assertIsNone(self.store.one("SELECT id FROM tasks WHERE project=? AND source='system_issues'",
                                         (self.project_id,)))
        self.store.transition(run["id"], "awaiting_approval", {}, True)
        self.runtime.schedule(self.project_id, self.config)
        sync_run = self.store.claim(self.project_id, self.config)
        assert sync_run is not None
        with patch("adaptive_rsi.runtime.checked", return_value="[]"):
            self.runtime.process(sync_run)
        task = self.task()
        assert task is not None
        self.assertEqual((task["state"], task["body"]), ("queued", "updated while working"))
        progress = self.store.setting("issue-sync:" + str(self.project_id))
        assert isinstance(progress, dict)
        self.assertEqual(progress["pending"], {})

    def test_cancelling_active_issue_keeps_held_feedback_without_restarting_it(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.issue.update(body="new requirement", updated_at=self.issue["updated_at"] + "1")
        self.synchronize()
        self.runtime.cancel_task(self.task_id)
        self.store.transition(run["id"], "interrupted", {}, True)
        self.synchronize()
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task_id,)),
                         {"state": "cancelled"})
        progress = self.store.setting("issue-sync:" + str(self.project_id))
        assert isinstance(progress, dict)
        self.assertIn("7", progress["pending"])
        self.runtime.schedule(self.project_id, self.config)
        self.assertIsNone(self.store.one("SELECT id FROM tasks WHERE project=? AND source='system_issues'",
                                         (self.project_id,)))

    def test_pause_keeps_held_feedback_until_explicit_issue_retry(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.issue.update(body="updated requirement", updated_at=self.issue["updated_at"] + "1")
        self.synchronize()
        self.runtime.pause(self.project_id, True)
        self.store.transition(run["id"], "awaiting_approval", {}, True)
        self.runtime.pause(self.project_id, False)
        self.runtime.schedule(self.project_id, self.config)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task_id,)),
                         {"state": "interrupted"})
        self.assertIsNone(self.store.one("SELECT id FROM tasks WHERE project=? AND source='system_issues'",
                                         (self.project_id,)))
        self.runtime.retry(self.task_id)
        self.runtime.schedule(self.project_id, self.config)
        sync_run = self.store.claim(self.project_id, self.config)
        assert sync_run is not None
        with patch("adaptive_rsi.runtime.checked", return_value="[]"):
            self.runtime.process(sync_run)
        task = self.task()
        assert task is not None
        self.assertEqual((task["state"], task["body"]), ("queued", "updated requirement"))

    def test_updated_issue_replans_awaiting_candidate_and_revokes_queued_delivery(self):
        self.runtime.revise_task(self.task_id, "local requirement")
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.store.transition(run["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        publication = self.runtime.deliver(run["id"])
        self.update("updated issue requirements")
        updated = self.task()
        assert updated is not None
        self.assertEqual((updated["state"], updated["body"]),
                         ("queued", "updated issue requirements\n\nlocal requirement"))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                         {"state": "superseded"})
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (publication,)),
                         {"state": "cancelled"})
        fresh = self.store.claim(self.project_id, self.config)
        self.assertEqual(fresh["task"], self.task_id)
        self.assertNotEqual(fresh["id"], run["id"])

    def test_closed_or_excluded_issue_revokes_awaiting_candidate(self):
        self.runtime.pause(self.project_id, False)
        for index, change in enumerate(("closed", "excluded"), 8):
            with self.subTest(change=change):
                self.issue = DiscoveryHardeningTest.issue(number=index)
                self.synchronize()
                row = self.store.one("SELECT id FROM tasks WHERE fingerprint=?",
                                     ("issue:owner/project:" + str(index),))
                assert row is not None
                self.task_id = row["id"]
                # Leave other queued Issues out of the claim for this scenario.
                self.store.execute("UPDATE tasks SET state='cancelled' WHERE id<>? AND source='issue'", (self.task_id,))
                run = self.store.claim(self.project_id, self.config)
                self.store.transition(run["id"], "awaiting_approval", {
                    "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
                publication = self.runtime.deliver(run["id"])
                if change == "closed":
                    self.issue["state"] = "closed"
                else:
                    self.issue["labels"] = [self.config["github"]["exclude_labels"][0]]
                self.issue["updated_at"] += "1"
                self.synchronize()
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task_id,)),
                                 {"state": "obsolete"})
                self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                                 {"state": "superseded"})
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (publication,)),
                                 {"state": "cancelled"})
                if change == "excluded":
                    self.issue["labels"] = []
                    self.synchronize()
                    self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task_id,)),
                                     {"state": "queued"})
                    self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                                     {"state": "superseded"})

    def test_issue_update_rolls_back_candidate_revocation_if_task_write_fails(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.store.transition(run["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        publication = self.runtime.deliver(run["id"])
        original = self.task()
        self.store.execute("CREATE TRIGGER reject_issue_update BEFORE UPDATE OF body ON tasks "
                           "BEGIN SELECT RAISE(ABORT, 'Injected issue write failure'); END")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "Injected issue write failure"):
                self.update("changed upstream")
        finally:
            self.store.execute("DROP TRIGGER reject_issue_update")
        self.assertEqual(self.task(), original)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                         {"state": "awaiting_approval"})
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (publication,)),
                         {"state": "queued"})
        self.synchronize()
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],)),
                         {"state": "superseded"})
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task_id,)),
                         {"state": "queued"})

    def test_cancelled_run_waits_for_end_and_requires_explicit_revision(self):
        self.runtime.revise_task(self.task_id, "local")
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.runtime.cancel_task(self.task_id)
        before = self.task()
        self.update("upstream while stopping")
        self.assertEqual(self.task(), before)
        with self.assertRaisesRegex(ValueError, "active run"):
            self.runtime.revise_task(self.task_id, "new local")
        self.store.execute("UPDATE runs SET state='cancelled',ended=1 WHERE id=?", (run["id"],))
        self.synchronize([])
        self.assertEqual(self.task(), before)
        self.runtime.revise_task(self.task_id, "new local")
        self.synchronize([])
        self.assertEqual((self.task()["state"], self.task()["body"]),
                         ("queued", "upstream while stopping\n\nlocal\n\nnew local"))

    def test_cancelled_and_delivered_policy_preserves_local_additions(self):
        self.runtime.revise_task(self.task_id, "local")
        original = self.task()["body"]
        for state in ["cancelled", "completed", "merged", "pr_closed", "pr_open", "reverted", "superseded", "expired"]:
            with self.subTest(state=state):
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, self.task_id))
                self.update("edit for " + state)
                self.assertEqual((self.task()["state"], self.task()["body"]), (state, original))
        for state in ["completed", "merged", "pr_closed"]:
            with self.subTest(reopen=state):
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, self.task_id))
                self.issue["state"] = "closed"
                self.update("closed")
                self.issue["state"] = "open"
                self.update("reopened")
                self.assertEqual((self.task()["state"], self.task()["body"]), ("queued", "reopened\n\nlocal"))

    def test_label_policy_restores_decision_and_supplement(self):
        self.runtime.revise_task(self.task_id, "local")
        self.store.defer(self.task_id, {"reason": "held"})
        self.config["github"]["labels"] = ["security"]
        self.synchronize()
        self.assertEqual(self.task()["state"], "obsolete")
        self.config["github"]["labels"] = []
        self.synchronize()
        self.assertEqual((self.task()["state"], self.task()["body"]), ("deferred", self.issue["body"] + "\n\nlocal"))
        before = self.task()
        self.synchronize()
        self.assertEqual(self.task(), before)
        self.assertEqual(json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (self.task_id,))["data"]), {"reason": "held"})

    def test_pending_is_committed_before_the_next_row(self):
        from adaptive_rsi.discovery import _issue_task
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.issue.update(body="new", updated_at=self.issue["updated_at"] + "1")
        other = DiscoveryHardeningTest.issue(number=8)
        def lookup(db, project, repo, number):
            if number == 8:
                self.assertIn("7", self.store.setting("issue-sync:" + self.project_id)["pending"])
                self.assertIsNotNone(self.store.setting("issue-revision-pending:" + run["id"]))
            return _issue_task(db, project, repo, number)
        with patch("adaptive_rsi.discovery._issue_task", side_effect=lookup):
            self.synchronize([self.issue, other])

    def test_pending_survives_policy_change(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.update("held change")
        self.store.transition(run["id"], "awaiting_approval", {}, True)
        self.config["github"]["labels"] = ["new-policy"]
        self.synchronize([])
        self.assertEqual(self.task()["state"], "obsolete")

    def test_excluded_candidate_is_requeued_when_policy_restored(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.store.transition(run["id"], "awaiting_approval", {}, True)
        self.config["github"]["labels"] = ["new-policy"]
        self.synchronize()
        self.config["github"]["labels"] = []
        self.synchronize()
        self.assertEqual(self.task()["state"], "queued")

    def test_issue_edit_under_pr_open_does_not_leave_a_replaying_snapshot(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.store.transition(run["id"], "pr_open", {"pr": "https://example/pull/1"}, False)
        self.issue.update(body="edited while pr open", updated_at=self.issue["updated_at"] + "1")
        self.synchronize()
        progress = self.store.setting("issue-sync:" + str(self.project_id))
        self.assertEqual(progress["pending"], {})
        self.runtime.schedule(self.project_id, self.config)
        self.assertIsNone(self.store.one("SELECT id FROM tasks WHERE project=? AND source='system_issues'",
                                         (self.project_id,)))

    def test_failed_update_blocks_candidate_before_failure_is_reported(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.store.transition(run["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        publication = self.runtime.deliver(run["id"])
        self.store.execute("CREATE TRIGGER reject_issue_update BEFORE UPDATE OF body ON tasks "
                           "BEGIN SELECT RAISE(ABORT, 'Injected issue write failure'); END")
        original_event = self.store.event
        observed = []

        def event(kind, *args, **kwargs):
            if kind == "issue_sync_error":
                try:
                    self.runtime.deliver(run["id"])
                    approved = True
                except ValueError:
                    approved = False
                claimed = self.store.claim(self.project_id, self.config)
                observed.append((approved, claimed and claimed["task"] == publication))
            return original_event(kind, *args, **kwargs)

        try:
            with patch.object(self.store, "event", side_effect=event):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.update("changed")
        finally:
            self.store.execute("DROP TRIGGER reject_issue_update")
        self.assertEqual(observed, [(False, None)])

    def test_failed_snapshot_blocks_approval_and_publish_claim(self):
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.store.transition(run["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        publication = self.runtime.deliver(run["id"])
        self.store.execute("CREATE TRIGGER reject_issue_update BEFORE UPDATE OF body ON tasks "
                           "BEGIN SELECT RAISE(ABORT, 'Injected issue write failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.update("changed")
            with self.assertRaises(ValueError):
                self.runtime.deliver(run["id"])
            claimed = self.store.claim(self.project_id, self.config)
            self.assertTrue(claimed is None or claimed["task"] != publication)
        finally:
            self.store.execute("DROP TRIGGER reject_issue_update")

    def test_oversized_pending_keeps_snapshot_and_fetches_remote_recovery(self):
        self.runtime.revise_task(self.task_id, "local")
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.update("x" * 30000)
        key = "issue-sync:" + self.project_id
        held = self.store.setting(key)["pending"]["7"]
        self.store.transition(run["id"], "failed", {"reason": "fixture worker ended"}, True)
        before = self.task()
        unrelated = DiscoveryHardeningTest.issue(number=8)
        fetched = []

        def remote(*args, **kwargs):
            fetched.append(args)
            return encode([unrelated])

        # Repeated pending failures must not prevent another Issue from syncing.
        for _ in range(3):
            sync(self.runtime, self.project, self.config, remote)
            self.assertEqual(self.task(), before)
            self.assertEqual(self.store.setting(key)["pending"]["7"], held)
        self.assertEqual(len(fetched), 3)
        self.assertIsNotNone(self.store.one("SELECT id FROM tasks WHERE fingerprint='issue:owner/project:8'"))
        self.issue.update(body="shortened remote", updated_at="remote corrected")
        self.synchronize()
        self.assertEqual((self.task()["state"], self.task()["body"]), ("queued", "shortened remote\n\nlocal"))
        self.assertEqual(self.store.setting(key)["pending"], {})
        self.synchronize()
        self.assertEqual(self.task()["body"], "shortened remote\n\nlocal")

    def test_legacy_migration_reassesses_only_actual_input_changes(self):
        from adaptive_rsi.config import fingerprint
        original = self.task()
        for change in ["representation", "lost_supplement", "previous_input", "policy"]:
            with self.subTest(change=change):
                self.store.execute("DELETE FROM task_revisions")
                body = original["body"] if change == "lost_supplement" else original["body"] + "\n\nlocal"
                # Legacy metadata and additional evidence must survive migration.
                evidence = encode([{**json.loads(original["evidence"])[0], "legacy_metadata": "retain"},
                                   {"path": "calculator.py", "quote": "return a - b"}])
                self.store.execute("UPDATE tasks SET body=?,evidence=? WHERE id=?", (body, evidence, self.task_id))
                self.store.execute("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                                   (self.task_id, original["body"], "local"))
                decision = {"reason": "Explicit planner deferral", "base": "fixed-baseline", "extra": {"retain": True},
                            "triage_input": fingerprint({"body": body, "evidence": evidence,
                                                         "policy": self.config["triage"]["policy"]})}
                self.store.defer(self.task_id, decision)
                self.runtime.reassess_deferred(self.project_id, self.config, "fixed-baseline")
                self.assertEqual(self.task()["state"], "deferred")
                if change == "previous_input":
                    decision["triage_input"] = "earlier-input"
                    self.store.execute("UPDATE task_decisions SET data=? WHERE task=?", (encode(decision), self.task_id))
                elif change == "policy":
                    self.config["triage"]["policy"] += " updated"
                before_decision = self.store.one("SELECT * FROM task_decisions WHERE task=?", (self.task_id,))
                self.synchronize()
                migrated = self.task()
                self.assertEqual(migrated["body"], original["body"] + "\n\nlocal")
                migrated_evidence = json.loads(migrated["evidence"])
                migrated_evidence[0].pop("requirements")
                self.assertEqual(migrated_evidence, json.loads(evidence))
                after_decision = self.store.one("SELECT * FROM task_decisions WHERE task=?", (self.task_id,))
                if change == "representation":
                    expected = {**decision, "triage_input": fingerprint({"body": body, "evidence": migrated["evidence"],
                                                                         "policy": self.config["triage"]["policy"]})}
                    self.assertEqual(after_decision, {**before_decision, "data": encode(expected)})
                else:
                    self.assertEqual(after_decision, before_decision)
                self.synchronize()
                self.assertEqual(self.task(), migrated)
                self.runtime.reassess_deferred(self.project_id, self.config, "fixed-baseline")
                self.assertEqual(self.task()["state"], "deferred" if change == "representation" else "queued")

    def test_legacy_decision_migration_rolls_back_with_task_write(self):
        from adaptive_rsi.config import fingerprint
        original = self.task()
        body = original["body"] + "\n\nlocal"
        self.store.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.task_id))
        self.store.execute("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                           (self.task_id, original["body"], "local"))
        self.store.defer(self.task_id, {"base": "fixed-baseline", "reason": "held", "triage_input": fingerprint({
            "body": body, "evidence": original["evidence"], "policy": self.config["triage"]["policy"]})})
        before = self.task()
        decision = self.store.one("SELECT * FROM task_decisions WHERE task=?", (self.task_id,))
        self.store.execute("CREATE TRIGGER reject_migration BEFORE UPDATE OF body ON tasks "
                           "BEGIN SELECT RAISE(ABORT, 'Injected migration failure'); END")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "Injected migration failure"):
                self.synchronize()
        finally:
            self.store.execute("DROP TRIGGER reject_migration")
        self.assertEqual(self.task(), before)
        self.assertEqual(self.store.one("SELECT * FROM task_decisions WHERE task=?", (self.task_id,)), decision)

    def test_oversized_remote_composition_rolls_back_without_losing_local(self):
        self.runtime.revise_task(self.task_id, "local")
        before = self.task()
        progress = self.store.setting("issue-sync:" + self.project_id)
        with self.assertRaisesRegex(ValueError, "30000"):
            self.update("x" * 30000)
        self.assertEqual(self.task(), before)
        # The cursor stays put, while the failed snapshot remains sealed until
        # a later upstream revision can be applied.
        after = dict(self.store.setting("issue-sync:" + self.project_id))
        self.assertEqual(after.pop("failures"), progress.get("failures", 0) + 1)
        self.assertEqual(after.pop("pending")["7"]["body"], "x" * 30000)
        expected = dict(progress)
        expected.pop("failures", None)
        expected.pop("pending", None)
        self.assertEqual(after, expected)
        self.update("shortened")
        self.assertEqual(self.task()["body"], "shortened\n\nlocal")


if __name__ == "__main__":
    unittest.main()
