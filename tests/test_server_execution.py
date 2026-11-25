"""小时调度、无额度用量、OpenCode 和 Linux 容器执行的回归验证。"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent, SCHEMAS, validate_output
from adaptive_rsi.cli_worker import cli_home, run_cli, codex_home, sync_codex_oauth
from adaptive_rsi.config import DEFAULTS, merge, validate
from adaptive_rsi.credentials import Credentials
from adaptive_rsi.discovery import sync
from adaptive_rsi.gitops import git
from adaptive_rsi.native import WorkspaceTools
from adaptive_rsi.opencode import jsonc, parse_events, prepare_home, sync_oauth, check_provider_errors
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store, encode
from adaptive_rsi.usage import native_usage, opencode_usage
from tests.test_runtime import FakeAgent, fixture, CHECKS


class ServerExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.repo = fixture(self.root / "project")
        self.project = self.runtime.add_project(self.repo)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_only_two_hourly_schedules_and_legacy_deep_coalesces(self):
        self.runtime.configure({"github": {"repository": "owner/project"}}, self.project)
        self.runtime.schedule(self.project, self.runtime.config(self.project), now=100)
        rows = self.store.query("SELECT kind,due FROM schedules ORDER BY kind")
        self.assertEqual(rows, [{"kind": "issues", "due": 3700.0}, {"kind": "light", "due": 3700.0}])
        self.assertEqual(self.runtime.request(self.project, "deep"), self.runtime.request(self.project, "light"))
        self.runtime.schedule(self.project, self.runtime.config(self.project), now=3800)
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 2)
        self.store.add_task(self.project, "issue", "backlog", "Existing issue", priority=60)
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertIn(self.store.one("SELECT source FROM tasks WHERE id=?", (run["task"],))["source"],
                      ["system_light", "system_issues"])

    def test_issue_request_without_repository_is_rejected_before_queueing(self):
        self.runtime.configure({"github": {"repository": ""}, "sources": {"issues": True}},
                               self.project)
        with self.assertRaisesRegex(ValueError, "^No GitHub repository configured$"):
            self.runtime.request(self.project, "issues")
        self.assertEqual(self.store.query("SELECT id FROM tasks WHERE source='system_issues'"), [])

    def test_manual_requests_stay_available_while_sources_are_disabled(self):
        self.runtime.configure({"github": {"repository": "owner/project"},
                                "sources": {"inspection": False, "issues": False}}, self.project)
        light = self.runtime.request(self.project, "light")
        issues = self.runtime.request(self.project, "issues")
        self.assertEqual(
            {self.store.one("SELECT source FROM tasks WHERE id=?", (task,))["source"]
             for task in (light, issues)},
            {"system_light", "system_issues"})

    def test_unlimited_calls_and_legacy_tasks_recover(self):
        config = validate({"limits": {"daily_calls": 1, "run_calls": 1, "run_seconds": 1, "max_changed_files": 1}})
        task = self.runtime.goal(self.project, "record usage")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, config)
        for _ in range(105):
            call = self.store.reserve_call(self.project, run["id"], "planning", config["models"]["planning"], config)
            self.store.record_usage(call, run["id"], native_usage("responses", {"usage": {"input_tokens": 10, "output_tokens": 5}}), 0.1)
        self.assertEqual(self.store.one("SELECT calls,tokens FROM runs WHERE id=?", (run["id"],)), {"calls": 105, "tokens": 1575})
        self.store.transition(run["id"], "waiting_budget", {}, True)
        self.runtime.resume_budget(self.project, config)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    def test_inspection_repeats_attach_to_original_task_and_keep_distinct_causes(self):
        from adaptive_rsi.maintenance_evidence import task_observations

        project = self.runtime.project(self.project)
        config = encode(self.runtime.config(self.project))
        original = {"title": "Incorrect result", "body": "Arithmetic sign defect", "path": "calculator.py",
                    "quote": "return a - b", "acceptance": ["Addition is correct"], "priority": 50}
        distinct = {**original, "body": "String inputs are accepted incorrectly",
                    "acceptance": ["String inputs are rejected"]}
        with patch.object(self.runtime, "invoke", return_value={"findings": [original]}):
            self.runtime.inspect(project, self.repo, "light", {"id": "first", "config": config}, [])
        first = self.store.one("SELECT id FROM tasks WHERE source='inspection'")["id"]
        self.store.defer(first, {"reason": "Wait for a confirmed impact report"})
        (self.repo / "calculator.py").write_text("# version two\ndef add(a, b):\n    return a - b\n")
        git(self.repo, "add", "calculator.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "version two")
        with patch.object(self.runtime, "invoke", return_value={"findings": [original, original, distinct]}):
            run = {"id": "repeat", "config": config}
            self.runtime.inspect(project, self.repo, "light", run, [])
            self.runtime.inspect(project, self.repo, "light", run, [])
        observations = task_observations(self.store, first)
        self.assertEqual([(item["run"], item["reason"]) for item in observations],
                         [("repeat", "retained"), ("first", "discovered")])
        self.assertNotEqual(observations[0]["base"], observations[1]["base"])
        self.assertEqual(observations[0]["quote"], original["quote"])
        self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE source='inspection'")), 2)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (first,))["state"], "deferred")
        self.assertEqual(json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (first,))["data"])["reason"],
                         "Wait for a confirmed impact report")

    def test_main_retrospective_is_transactional_and_rules_match_exact_paths(self):
        from adaptive_rsi.maintenance_evidence import applicable_rules, confirm_retrospective, retrospective_preview
        from sqlite3 import IntegrityError

        project = self.runtime.project(self.project)
        start = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", "calculator.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "repair")
        head = git(self.repo, "rev-parse", "HEAD")
        preview = retrospective_preview(self.store, project, start)
        self.assertEqual((preview["main"], preview["end"], preview["cursor"]), (head, head, None))
        self.assertEqual([row["sha"] for row in preview["commits"]], [head])
        candidate = {"sha": head, "paths": ["calculator.py"], "rule": "Check signed arithmetic",
                     "reason": "Subtraction was used for addition", "validation": "Negative operands were untested"}
        self.assertEqual(applicable_rules(self.store, self.project, ["calculator.py"], self.repo, head), [])
        with self.assertRaises(ValueError):
            confirm_retrospective(self.store, project, start, preview["token"], [{**candidate, "paths": ["other.py"]}])
        self.store.execute("CREATE TRIGGER fail_confirm BEFORE INSERT ON maintenance_rules "
                           "BEGIN SELECT RAISE(ABORT, 'write failed'); END")
        with self.assertRaises(IntegrityError):
            confirm_retrospective(self.store, project, start, preview["token"], [candidate])
        self.assertEqual(self.store.query("SELECT sha FROM maintenance_reviews"), [])
        self.assertIsNone(self.store.setting("maintenance-cursor:" + self.project))
        self.store.execute("DROP TRIGGER fail_confirm")
        self.assertEqual(confirm_retrospective(self.store, project, start, preview["token"], [candidate])["cursor"], head)
        self.assertEqual(retrospective_preview(self.store, project)["commits"], [])
        self.assertEqual(applicable_rules(self.store, self.project, ["calculator.py"], self.repo, head)[0]["sha"], head)
        self.assertEqual(applicable_rules(self.store, self.project, ["other/calculator.py"], self.repo, head), [])
        self.assertEqual(applicable_rules(self.store, self.project, ["calculator.py"], self.repo, start), [])
        with self.assertRaises(ValueError):
            confirm_retrospective(self.store, project, start, preview["token"], [candidate])

    def test_weekly_retrospective_api_and_retry_keep_the_manual_cursor(self):
        import io
        from email.message import Message
        from types import SimpleNamespace
        from unittest.mock import Mock

        from adaptive_rsi.maintenance_evidence import (
            REVIEW_RETRY, REVIEW_WEEK, retrospective_preview, schedule_retrospectives,
        )
        from adaptive_rsi.server import Handler

        def request(body=None, query="", csrf="fixture-csrf"):
            handler = Handler.__new__(Handler)
            origin = "http://localhost"
            handler.server = SimpleNamespace(runtime=self.runtime, origin=origin, local_origin=origin,
                                             csrf="fixture-csrf", security_failure=Mock())
            handler.authenticated = Mock(return_value=True)
            handler.respond = Mock()
            handler.path = "/api/maintenance-review" + query
            handler.headers = Message()
            handler.headers["Origin"] = origin
            handler.headers["X-RSI-CSRF"] = csrf
            if body is None:
                handler.do_GET()
            else:
                payload = json.dumps({"project": self.project, **body}).encode()
                handler.headers["Content-Length"] = str(len(payload))
                handler.headers["Content-Type"] = "application/json"
                handler.rfile = io.BytesIO(payload)
                handler.do_POST()
            handler.authenticated.assert_called_once()
            response = handler.respond.call_args.args
            return response[0], response[1] if len(response) > 1 else 200

        def commit(subject):
            (self.repo / "calculator.py").write_text(f"# {subject}\n")
            git(self.repo, "add", "calculator.py")
            git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "commit", "--quiet", "-m", subject)
            return git(self.repo, "rev-parse", "HEAD")

        start = git(self.repo, "rev-parse", "HEAD")
        reviewed = commit("first change")
        self.runtime.pause(self.project, False)
        task = self.runtime.goal(self.project, "record merged evidence")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertEqual(run["task"], task)
        self.store.transition(run["id"], "merged", {"merged_sha": reviewed, "pr": 42, "evaluation": {
            "decision": "pass", "checks": {"candidate": [{"label": "record without outcome"}]},
        }}, True)
        query = "?project=" + self.project
        self.assertEqual(request({"schedule": True}, csrf="wrong")[1], 403)
        self.assertEqual(request({"schedule": True})[1], 400)
        self.assertEqual(request({"schedule": True, "start": "f" * 40})[1], 400)
        self.assertEqual(request({"schedule": True, "start": start, "rules": []})[1], 400)
        enabled, status = request({"schedule": True, "start": start})
        self.assertEqual(status, 200)
        self.assertIsNone(request(query=query)[0]["cursor"])
        due = enabled["next_at"]
        schedule_retrospectives(self.runtime, due - 1)
        self.assertEqual(request(query=query)[0]["schedule"], enabled)
        schedule_retrospectives(self.runtime, due)
        pending = request(query=query)[0]["schedule"]
        preview = request(query=query + "&preview=1&start=" + start)[0]
        self.assertEqual(pending["summary"], {**preview, "generated_at": due})
        self.assertEqual(preview["commits"][0]["paths"], ["calculator.py"])
        self.assertEqual(preview["commits"][0]["evidence"]["run"], run["id"])
        self.assertEqual(preview["commits"][0]["evidence"]["pr"], 42)
        self.assertEqual(preview["commits"][0]["review_clues"], [
            {"kind": "validation_gap", "status": "insufficient_evidence",
             "missing": ["recorded_candidate_checks", "conclusive_version_matched_postmerge_record"]},
        ])
        self.assertEqual(pending["next_at"], due + REVIEW_WEEK)
        schedule_retrospectives(self.runtime, due + REVIEW_WEEK)
        self.assertEqual(request(query=query)[0]["schedule"]["summary"], pending["summary"])
        self.assertEqual(request({"start": start, "token": preview["token"], "rules": []})[0]["cursor"], reviewed)

        head = commit("second change")
        schedule_retrospectives(self.runtime, due + 2 * REVIEW_WEEK)
        current = request(query=query)[0]["schedule"]
        self.assertEqual(current["summary"]["end"], head)
        self.assertEqual(current["summary"]["commits"][0]["review_clues"][0], {
            "kind": "possible_followup", "source_sha": reviewed, "paths": ["calculator.py"],
            "basis": "exact_path_overlap_only",
        })
        git(self.repo, "update-ref", "refs/heads/main", start)
        schedule_retrospectives(self.runtime, current["next_at"])
        failed = request(query=query)[0]
        self.assertEqual(failed["cursor"], reviewed)
        self.assertEqual(failed["schedule"]["summary"], current["summary"])
        self.assertIn("first-parent", failed["schedule"]["error"])
        retry = current["next_at"] + REVIEW_RETRY
        self.assertEqual(failed["schedule"]["next_at"], retry)
        git(self.repo, "update-ref", "refs/heads/main", head)
        schedule_retrospectives(self.runtime, retry)
        self.assertIsNone(request(query=query)[0]["schedule"]["error"])
        def disable_during_preview(*args):
            self.assertEqual(request({"schedule": False})[1], 200)
            return retrospective_preview(*args)

        with patch("adaptive_rsi.maintenance_evidence.retrospective_preview", side_effect=disable_during_preview) as preview_call:
            schedule_retrospectives(self.runtime, retry + REVIEW_WEEK)
        preview_call.assert_called_once()
        disabled = request(query=query)[0]["schedule"]
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["next_at"])
        self.assertEqual(disabled["summary"], current["summary"])
        schedule_retrospectives(self.runtime, retry + 2 * REVIEW_WEEK)
        self.assertEqual(request(query=query)[0]["schedule"], disabled)
        reenabled, status = request({"schedule": True})
        self.assertEqual(status, 200)
        self.assertIsNone(reenabled["start"])
        self.assertEqual(request(query=query)[0]["cursor"], reviewed)
        self.assertEqual(self.store.query("SELECT sha FROM maintenance_reviews"), [{"sha": reviewed}])
        self.assertEqual(self.store.query("SELECT id FROM maintenance_rules"), [])
        self.assertEqual(self.store.query("SELECT id FROM tasks"), [{"id": task}])

    def test_retrospective_clues_require_exact_commit_and_run_anchors(self):
        from adaptive_rsi.maintenance_evidence import retrospective_preview

        def commit_git(*args):
            git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", *args)
            return git(self.repo, "rev-parse", "HEAD")

        start = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "calculator.py").write_text("# changed\n")
        git(self.repo, "add", "calculator.py")
        unrelated = "f" * 40
        original = commit_git("commit", "-m", "Revert in subject only", "-m", f"This reverts commit {unrelated}.")
        rollback = commit_git("revert", "--no-edit", original)
        self.store.execute("INSERT INTO maintenance_reviews VALUES (?,?,?,?)",
                           (self.project, unrelated, encode({"paths": ["calculator.py"]}), 1))
        project = self.runtime.project(self.project)
        first, second = retrospective_preview(self.store, project, start)["commits"]
        gap = {"kind": "validation_gap", "status": "insufficient_evidence",
               "missing": ["linked_merged_run", "recorded_candidate_checks",
                           "conclusive_version_matched_postmerge_record"]}
        revert_clue = {"kind": "explicit_revert", "source_sha": original, "sources": {"git_message": True}}
        self.assertEqual(first["review_clues"], [gap])
        self.assertEqual(second["review_clues"], [revert_clue, gap])

        def record_merge(sha, **evidence):
            task = self.runtime.goal(self.project, sha)
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.assertEqual(run["task"], task)
            self.store.transition(run["id"], "merged", {"merged_sha": sha, **evidence}, True)
            return run["id"]

        self.runtime.pause(self.project, False)
        evaluation = {"checks": {"candidate_full": [{"passed": True}]}}
        original_run = record_merge(original, evaluation=evaluation, feedback={"state": "verified", "head": rollback})
        rollback_run = record_merge(rollback, evaluation=evaluation, feedback={"state": "regressed", "head": rollback})
        self.store.execute("UPDATE runs SET state='reverted',result=json_set(result,'$.reverted_by',?) WHERE id=?",
                           (rollback_run, original_run))
        first, second = retrospective_preview(self.store, project, start)["commits"]
        self.assertEqual(first["evidence"]["run"], original_run)
        self.assertEqual(first["review_clues"], [{**gap, "missing": ["conclusive_version_matched_postmerge_record"]}])
        self.assertEqual(second["review_clues"], [{**revert_clue, "sources": {
            "git_message": True, "delivery_runs": {"original": original_run, "rollback": rollback_run},
        }}])

        self.store.execute("INSERT INTO maintenance_reviews VALUES (?,?,?,?)",
                           (self.project, original, encode({"paths": ["calculator.py"]}), 2))
        (self.repo / "calculator.py").write_text("# another change\n")
        git(self.repo, "add", "calculator.py")
        commit_git("commit", "-m", "another change")
        with patch("adaptive_rsi.maintenance_evidence.REVIEW_BATCH", 1):
            final = retrospective_preview(self.store, project, rollback)["commits"][0]
        self.assertEqual(final["review_clues"], [gap])

    def test_fixed_check_signal_requires_exact_main_version(self):
        from adaptive_rsi.maintenance_evidence import fixed_check_signal

        repository = "owner/project"
        head = git(self.repo, "rev-parse", "HEAD")
        match = {"headSha": head, "headBranch": "main", "event": "push", "databaseId": 2,
                 "status": "completed", "conclusion": "failure"}
        self.store.put_setting("maintenance-ci:" + self.project,
                               {"repository": repository, "observed": 20,
                                "checks": [match, {**match, "databaseId": 3, "headSha": "a" * 40},
                                           {**match, "databaseId": 4, "event": "pull_request"}]})
        self.assertEqual(fixed_check_signal(self.store, self.project, repository, head)["checks"], [match])
        for given_repository, given_head in [(repository, None), (repository, "b" * 40), ("other/project", head)]:
            self.assertEqual(fixed_check_signal(self.store, self.project, given_repository, given_head)["status"], "investigate")
        self.assertEqual(self.store.query("SELECT id FROM tasks"), [])

    def test_inspection_keeps_more_than_five_evidenced_findings(self):
        findings = [{"title": "Defect " + str(i), "body": "A concrete defect", "path": "calculator.py",
                     "quote": "return a - b" + "\n" * i, "acceptance": ["Addition is correct"], "priority": 50}
                    for i in range(8)]
        # 不同源锚点保留为独立问题。
        (self.repo / "calculator.py").write_text("\n".join("def bug_%d(a,b): return a-b" % i for i in range(8)) + "\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "findings")
        for i, item in enumerate(findings):
            item["quote"] = "def bug_%d(a,b): return a-b" % i
        with patch.object(self.runtime, "invoke", return_value={"findings": findings}):
            self.runtime.inspect(self.runtime.project(self.project), self.repo, "light", {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
        self.assertEqual(len(self.store.query("SELECT * FROM tasks WHERE source='inspection'")), 8)
        validate_output({"findings": findings * 20}, SCHEMAS["analysis"])

    def test_inspection_batch_triage_keeps_evidence_and_only_queues_valuable_findings(self):
        findings = [
            {"title": "Cosmetic naming " + str(i), "body": "A low-value naming preference",
             "path": "calculator.py", "quote": "return a - b", "acceptance": ["Names are clearer"],
             "priority": priority}
            for i, priority in enumerate([20, 30, 40, 80])
        ]
        findings.append({**findings[0], "priority": 21})
        calls = []
        def invoke(role, prompt, workspace, run):
            calls.append(role)
            if role == "analysis":
                return {"findings": findings}
            if role == "triage":
                return {"findings": [
                    {"index": 0, "decision": "rejected", "confidence": "high", "reason": "Cosmetic churn"},
                    {"index": 1, "decision": "deferred", "confidence": "medium", "reason": "Weak current value"},
                    {"index": 2, "decision": "ready", "confidence": "high", "reason": "Concrete maintenance benefit"},
                    {"index": 3, "decision": "rejected", "confidence": "high", "reason": "Should be protected by priority"},
                ]}
            raise AssertionError(role)
        with patch.object(self.runtime, "invoke", side_effect=invoke):
            self.runtime.inspect(
                self.runtime.project(self.project), self.repo, "light",
                {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
        rows = self.store.query("SELECT id,title,state,evidence FROM tasks WHERE source='inspection' ORDER BY title")
        self.assertEqual(calls.count("analysis"), 1)
        self.assertEqual(calls.count("triage"), 1)
        states = {row["title"].split()[-1]: row["state"] for row in rows}
        self.assertEqual(states, {"0": "rejected", "1": "deferred", "2": "queued", "3": "queued"})
        self.assertTrue(all(json.loads(row["evidence"])[0]["quote"] == "return a - b" for row in rows))
        deferred = next(row for row in rows if row["title"].endswith("1"))
        config = self.runtime.config(self.project)
        head = git(self.repo, "rev-parse", "HEAD")
        self.runtime.reassess_deferred(self.project, config, head)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (deferred["id"],))["state"], "deferred")
        (self.repo / "calculator.py").write_text((self.repo / "calculator.py").read_text() + "\n# new baseline\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "new baseline")
        self.runtime.reassess_deferred(self.project, config, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (deferred["id"],))["state"], "queued")
        validate_output({"findings": [
            {"index": 0, "decision": "ready", "confidence": "high", "reason": "Concrete defect"}]},
            SCHEMAS["triage"])

    def test_issues_paginate_past_old_total_limit(self):
        config = merge(DEFAULTS, {"github": {"repository": "owner/project", "max_issues": 1}})
        pages = []
        def fetch(argv, **kwargs):
            page = int(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("page=")))
            pages.append(page)
            start, end = (page - 1) * 100 + 1, min(page * 100 + 1, 206)
            return encode([{"number": i, "title": str(i), "body": "", "state": "open", "labels": [],
                            "updated_at": "2026-01-01T00:00:00Z", "html_url": "https://github.com/owner/project/issues/" + str(i)}
                           for i in range(start, end)])
        sync(self.runtime, self.runtime.project(self.project), config, fetch)
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 205)

    def test_file_credentials_survive_restart_and_never_enter_config(self):
        provider = self.runtime.model_center.save_provider({"name": "Server", "protocol": "responses", "base_url": "https://example.com/v1", "env_key": ""}, {"mode": "file", "value": "server-test-secret"})
        item = self.runtime.model_center.provider(provider["id"])
        restarted = Credentials(self.store.root)
        self.assertEqual(restarted.get(item), "server-test-secret")
        self.assertEqual((self.store.root / "credentials" / item["secret_ref"]).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("server-test-secret", encode(self.runtime.config()))
        self.assertNotIn("server-test-secret", encode(self.runtime.model_center.snapshot()))

    def test_public_origin_is_explicit_and_health_remains_local(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for address in ["http://example.com", "https://example.com/path", "https://user:pass@example.com"]:
            with self.assertRaises(ValueError):
                Server(self.runtime, public_url=address)
        server = Server(self.runtime, public_url="https://rsi.example.com")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from scripts.service import live_session
            session = {"origin": server.origin, "local_origin": server.local_origin,
                       "cookie": server.cookie, "token": server.token, "pid": os.getpid()}
            (self.store.root / "server.json").write_text(encode(session))
            self.assertEqual(live_session(self.store.root)["pid"], os.getpid())
            session["pid"] = -1
            (self.store.root / "server.json").write_text(encode(session))
            with self.assertRaisesRegex(ValueError, "process changed"):
                live_session(self.store.root)
            headers = {"Host": "rsi.example.com", "Cookie": server.cookie + "=" + server.token}
            with opener.open(urllib.request.Request(server.local_origin + "/api/state", headers=headers)) as response:
                self.assertEqual(response.status, 200)
            headers["Host"] = "untrusted.example"
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                opener.open(urllib.request.Request(server.local_origin + "/api/state", headers=headers))
            self.assertEqual(rejected.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_state_poll_returns_without_waiting_for_dispatch_lock(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        server = Server(self.runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        headers = {"Cookie": server.cookie + "=" + server.token}

        def poll():
            with opener.open(urllib.request.Request(server.local_origin + "/api/state",
                                                    headers=headers), timeout=5) as response:
                return response.status, json.loads(response.read())

        try:
            self.assertEqual(poll()[0], 200)
            baseline = poll()[1]["update"]
            acquired, release = threading.Event(), threading.Event()

            def hold():
                with self.runtime.dispatch_lock:
                    acquired.set()
                    release.wait(10)

            holder = threading.Thread(target=hold, daemon=True)
            holder.start()
            self.assertTrue(acquired.wait(5))
            started = time.monotonic()
            try:
                status, payload = poll()
            finally:
                release.set()
                holder.join(5)
            # 读端点不得在 SQLite 连接内等待全局写锁：慢写入期间仍须及时返回。
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(status, 200)
            self.assertEqual(payload["update"], baseline)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_state_reports_busy_only_while_an_update_is_active(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        server = Server(self.runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        headers = {"Cookie": server.cookie + "=" + server.token}

        def busy():
            with opener.open(urllib.request.Request(server.local_origin + "/api/state",
                                                    headers=headers), timeout=5) as response:
                self.assertEqual(response.status, 200)
                return json.loads(response.read())["update"]["busy"]

        try:
            self.store.put_setting("update-status", {"state": "unchecked", "current": "0.0.0"})
            self.assertFalse(busy())
            for state in ("downloading", "restarting"):
                with self.subTest(state=state):
                    self.store.put_setting("update-status", {"state": state, "current": "0.0.0"})
                    self.assertTrue(busy())
            self.runtime.updates.installing = True
            self.store.put_setting("update-status", {"state": "available", "current": "0.0.0", "version": "9.9.9"})
            self.assertTrue(busy())
        finally:
            self.runtime.updates.installing = False
            server.shutdown()
            server.server_close()
            thread.join()


class UsageAndOpenCodeTest(unittest.TestCase):
    def test_codex_container_oauth_refresh_survives_the_temporary_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "login/auth.json"
            source.parent.mkdir()
            original = {"tokens": {"access_token": "old", "refresh_token": "old-refresh"}, "last_refresh": "old-date"}
            source.write_text(encode(original))
            with patch.dict(os.environ, {"CODEX_HOME": str(source.parent)}), cli_home(root) as home:
                copied = codex_home(home)
                target = home / ".codex/auth.json"
                target.write_text(encode({"tokens": {"access_token": "new", "refresh_token": "new-refresh", "extra": "bad"},
                                         "last_refresh": "new-date", "extra": "bad"}))
                sync_codex_oauth(home, copied)
                saved = json.loads(source.read_text())
                self.assertEqual(saved, {"tokens": {"access_token": "new", "refresh_token": "new-refresh"}, "last_refresh": "new-date"})
                self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                saved["tokens"]["access_token"] = "external-login"
                source.write_text(encode(saved))
                sync_codex_oauth(home, copied)
                self.assertEqual(json.loads(source.read_text())["tokens"]["access_token"], "external-login")

    def test_opencode_availability_error_is_classified_without_echoing_credentials(self):
        from adaptive_rsi.native import ProviderUnavailable
        from adaptive_rsi.provider_health import error_code
        with self.assertRaises(ProviderUnavailable) as failure:
            check_provider_errors(encode({"type": "error", "error": {"name": "APIError", "data": {
                "statusCode": 429, "message": "private-key-value"}}}))
        self.assertEqual(error_code(failure.exception), "rate_limit")
        self.assertNotIn("private-key-value", str(failure.exception))
        with self.assertRaises(RuntimeError) as failure:
            check_provider_errors(encode({"type": "error", "error": {"name": "ProviderAuthError"}}))
        self.assertEqual(error_code(failure.exception), "auth")

    def test_opencode_isolates_config_and_refreshes_only_selected_oauth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data/opencode/auth.json"
            source.parent.mkdir(parents=True)
            original = {"selected": {"type": "oauth", "access": "old-access-token", "refresh": "old-refresh-token", "expires": 1},
                        "other": {"type": "api", "key": "unrelated-secret"}}
            source.write_text(encode(original))
            config = root / "config/opencode/opencode.jsonc"
            config.parent.mkdir(parents=True)
            config.write_text(encode({"provider": {"selected": {"options": {"baseURL": "https://example.com"}}, "other": {}},
                                      "plugin": ["untrusted-plugin"], "mcp": {"untrusted": {}}, "instructions": ["untrusted"]}))
            home = root / "isolated"
            home.mkdir()
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
                                         "OPENCODE_CONFIG_CONTENT": "{}"}):
                with patch.dict(os.environ, clear=False):
                    os.environ.pop("OPENCODE_CONFIG", None)
                    environment, secrets = prepare_home(home, "selected/model")
                imported = json.loads((home / ".config/opencode/opencode.json").read_text())
                self.assertEqual(set(imported["provider"]), {"selected"})
                self.assertEqual(imported["plugin"], [])
                self.assertEqual(imported["mcp"], {})
                self.assertNotIn("unrelated-secret", secrets)
                self.assertEqual(environment["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
                target = home / ".local/share/opencode/auth.json"
                refreshed = {"selected": {**original["selected"], "access": "new-access-token", "expires": 2,
                                          "injected": "not-accepted"}, "injected": {"type": "api", "key": "bad"}}
                target.write_text(encode(refreshed))
                sync_oauth(home, "selected/model", original)
                saved = json.loads(source.read_text())
                self.assertEqual(saved["other"], original["other"])
                self.assertEqual(saved["selected"]["access"], "new-access-token")
                self.assertNotIn("injected", saved)
                self.assertNotIn("injected", saved["selected"])
                self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                saved["selected"]["access"] = "external-newer-token"
                source.write_text(encode(saved))
                sync_oauth(home, "selected/model", original)
                self.assertEqual(json.loads(source.read_text())["selected"]["access"], "external-newer-token")

    def test_cache_usage_is_not_counted_twice_and_missing_is_unknown(self):
        details = native_usage("responses", {"usage": {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 70}}})
        self.assertEqual((details["tokens"], details["input_tokens"], details["cache_read_tokens"]), (120, 30, 70))
        self.assertIsNone(native_usage("responses", {}))
        self.assertEqual(opencode_usage([{"input": 10, "output": 5, "cache": {"read": 20, "write": 2}}])["tokens"], 37)
        # 网关可能返回 null 或字符串计数；无法确认时按未知处理，不否决成功调用。
        self.assertIsNone(native_usage("chat_completions", {"usage": {"prompt_tokens": None, "completion_tokens": 3, "total_tokens": None}}))
        self.assertIsNone(native_usage("anthropic", {"usage": {"input_tokens": None, "output_tokens": 1}}))
        self.assertIsNone(opencode_usage([{"input": None, "output": 3}]))
        tolerated = native_usage("chat_completions", {"usage": {"prompt_tokens": "12", "completion_tokens": "5", "total_tokens": "17"}})
        self.assertEqual(tolerated["tokens"], 17)
        self.assertEqual(tolerated["input_tokens"], 12)
        self.assertEqual(tolerated["output_tokens"], 5)

    def test_opencode_jsonl_variants_and_jsonc_urls(self):
        data, steps = parse_events('\n'.join([encode({"type": "step_finish", "part": {"id": "step", "tokens": {"input": 5}}}),
                                              encode({"type": "text", "part": {"text": '```json\n{"summary":"done"}\n```'}})]))
        self.assertEqual(data, {"summary": "done"})
        self.assertEqual(len(steps), 1)
        self.assertEqual(jsonc('{/*c*/"url":"https://x/a//b", // hi\n"items":[1,],}'), {"url": "https://x/a//b", "items": [1]})
        config = validate({"models": {"implementation": {"provider": "opencode", "model": "provider/model", "effort": "custom_variant"}}})
        self.assertEqual(config["models"]["implementation"]["effort"], "custom_variant")


@unittest.skipUnless(os.environ.get("RSI_DOCKER_TESTS") == "1", "Enable RSI_DOCKER_TESTS=1 with the worker image built")
class DockerExecutionTest(unittest.TestCase):
    setUp = ServerExecutionTest.setUp
    tearDown = ServerExecutionTest.tearDown
    def test_linux_native_pipeline_uses_real_isolated_checks(self):
        self.runtime.configure({"sandbox": {"backend": "docker", "image": "adaptive-rsi-worker:local"},
                                "checks": CHECKS, "sources": {"inspection": False, "issues": False},
                                "evolution": {"enabled": False}}, self.project)
        self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        row = self.store.one("SELECT state,result FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "awaiting_approval", row["result"])
        self.assertIn("return a - b", (self.repo / "calculator.py").read_text())

    def test_native_command_creates_file_in_new_approved_test_directory(self):
        config = validate({"sandbox": {"backend": "docker", "image": "adaptive-rsi-worker:local"}})
        workspace = self.root / "scoped-workspace"
        (workspace / ".git").mkdir(parents=True)
        (workspace / "calculator.py").write_text("value = 1\n")
        tools = WorkspaceTools(workspace, "implementation", self.store.root, config, None, ())
        tools.load_scope({"paths": ["tests"], "directories": ["tests"]})
        self.assertFalse((workspace / "tests").exists())
        script = "\n".join([
            "import os",
            "from pathlib import Path",
            "for path in ('.', '.git', 'calculator.py', '/'):",
            "    assert os.statvfs(path).f_flag & os.ST_RDONLY, path",
            "test_dir = Path('tests')",
            "assert test_dir.is_dir() and not list(test_dir.iterdir())",
            "(test_dir / 'test_example.py').write_text('assert True\\n')",
        ])
        result = tools.execute("run_command", {"argv": ["python3", "-c", script], "cwd": "."})
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual((workspace / "tests" / "test_example.py").read_text(), "assert True\n")

    def test_container_hides_controller_and_other_projects_and_enforces_readonly(self):
        config = validate({"sandbox": {"backend": "docker"}})
        secret = self.store.root / "hidden.txt"
        secret.write_text("hidden evaluation data")
        began = time.monotonic()
        with cli_home(self.repo) as home:
            script = ("from pathlib import Path; import os; "
                      "assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '2147483648'; "
                      "assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '128'; "
                      "quota,period=map(int,Path('/sys/fs/cgroup/cpu.max').read_text().split()); assert quota == 2*period; "
                      "tmp=os.statvfs('/tmp'); assert tmp.f_blocks*tmp.f_frsize <= 256*1024*1024; "
                      "assert not Path(" + repr(str(secret)) + ").exists(); "
                      "assert not Path('/var/run/docker.sock').exists(); Path('calculator.py').write_text('forbidden')")
            result = run_cli(["python3", "-c", script], self.repo, home, config, lambda: time.monotonic() - began > 30,
                             read_only=True, state=self.store.root)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("Read-only file system", result["stderr"])
        self.assertIn("return a - b", (self.repo / "calculator.py").read_text())


if __name__ == "__main__":
    unittest.main()
