import base64
import concurrent.futures
import datetime
import hashlib
import json
import threading
import time
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from adaptive_rsi import __version__
from adaptive_rsi.config import fingerprint
from adaptive_rsi.pipeline import _issue_comment_cache_evidence
from adaptive_rsi.release import ApiRejected, advance, deployment_health, key, owner_key, retry, save
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import fixture
from adaptive_rsi.gitops import git


TRUSTED = "a" * 40
MERGED = "b" * 40
PREVIOUS = "c" * 40
WORKFLOW = """name: Trusted delivery
run-name: ${{ inputs.rsi_operation_id }}
on:
  workflow_dispatch:
    inputs:
      rsi_operation_id:
        required: true
        type: string
      rsi_sha:
        required: true
        type: string
jobs:
  deliver:
    runs-on: ubuntu-latest
    steps:
      - run: echo configured-by-maintainer
"""


class BackendHardeningTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, object())
        self.store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                           ("project", "Project", str(self.root / "project"), "{}", 0, time.time()))

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_claim_blocks_invalid_overrides_and_claims_following_valid_task(self):
        bad_json = self.store.add_task("project", "goal", "bad-json", "Bad JSON", priority=100)
        bad_null = self.store.add_task("project", "goal", "bad-null", "Null overrides", priority=99)
        bad_list = self.store.add_task("project", "goal", "bad-list", "List overrides", priority=98)
        bad_timezone = self.store.add_task("project", "goal", "bad-timezone", "Bad timezone", priority=97)
        good = self.store.add_task("project", "goal", "good", "Valid task", priority=1)
        self.store.execute("UPDATE tasks SET overrides='{' WHERE id=?", (bad_json,))
        self.store.execute("UPDATE tasks SET overrides='null' WHERE id=?", (bad_null,))
        self.store.execute("UPDATE tasks SET overrides='[]' WHERE id=?", (bad_list,))
        self.store.execute("UPDATE tasks SET overrides=? WHERE id=?",
                           (json.dumps({"schedule": {"timezone": "No/Such_Zone"}}), bad_timezone))

        run = self.store.claim("project", self.runtime.config("project"))

        self.assertEqual(run["task"], good)
        states = self.store.query("SELECT id,state FROM tasks WHERE id IN (?,?,?,?)", (bad_json, bad_null, bad_list, bad_timezone))
        self.assertEqual({row["state"] for row in states}, {"blocked"})
        events = self.store.query("SELECT message,data FROM events WHERE kind='task_configuration_error' ORDER BY seq")
        self.assertEqual(len(events), 4)
        self.assertIn("Unknown schedule timezone", events[-1]["message"])
        self.assertNotIn("No/Such_Zone", events[-1]["message"])
        self.assertEqual(json.loads(events[-1]["data"])["task"], bad_timezone)

    def test_claim_blocks_invalid_evidence_and_claims_following_valid_task(self):
        bad_json = self.store.add_task("project", "system_light", "bad-json", "Bad evidence JSON", priority=100)
        bad_shape = self.store.add_task("project", "system_light", "bad-shape", "Non-array evidence", priority=99)
        auto = self.store.add_task("project", "system_light", "auto-scan", "Automatic scan", priority=98,
                                   evidence=[{"scan_trigger": "automatic"}])
        good = self.store.add_task("project", "goal", "good", "Valid task", priority=1)
        self.store.execute("UPDATE tasks SET evidence='not-json' WHERE id=?", (bad_json,))
        self.store.execute("UPDATE tasks SET evidence='null' WHERE id=?", (bad_shape,))

        run = self.store.claim("project", {"sources": {"inspection": False, "issues": False}})

        self.assertEqual(run["task"], good)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (bad_json,))["state"], "blocked")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (bad_shape,))["state"], "blocked")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (auto,))["state"], "obsolete")
        events = self.store.query("SELECT message,data FROM events WHERE kind='task_configuration_error' ORDER BY seq")
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["message"].startswith("Task was blocked because its evidence is invalid: "))
        self.assertEqual([json.loads(row["data"])["task"] for row in events], [bad_json, bad_shape])

    def test_claim_evidence_guard_matches_overrides_semantics_with_sources_enabled(self):
        bad = self.store.add_task("project", "system_light", "bad-evidence", "Bad evidence", priority=100)
        good = self.store.add_task("project", "goal", "good", "Valid task", priority=1)
        self.store.execute("UPDATE tasks SET evidence='not-json' WHERE id=?", (bad,))

        run = self.store.claim("project", self.runtime.config("project"))

        self.assertEqual(run["task"], good)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (bad,))["state"], "blocked")
        events = self.store.query("SELECT data FROM events WHERE kind='task_configuration_error'")
        self.assertEqual([json.loads(row["data"])["task"] for row in events], [bad])

    def test_run_directory_creation_error_closes_planning_run(self):
        task = self.store.add_task("project", "goal", "mkdir-error", "A task")
        run = self.store.claim("project", self.runtime.config("project"))
        directory = self.store.root / "runs" / run["id"]
        original_mkdir = Path.mkdir

        def fail_run_directory(path, *args, **kwargs):
            if path == directory:
                raise OSError("run directory is not writable")
            return original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", fail_run_directory):
            self.runtime.process(run)

        ended = self.store.one("SELECT state,ended FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(ended["state"], "blocked")
        self.assertIsNotNone(ended["ended"])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "blocked")

    def test_next_effective_schedule_respects_timezone_and_working_hours(self):
        self.runtime.configure({"schedule": {"working_hours": [9, 17], "timezone": "America/New_York"}}, "project")
        zone = ZoneInfo("America/New_York")

        before_open = datetime.datetime(2026, 3, 8, 8, 0, tzinfo=zone).timestamp()
        self.assertEqual(self.runtime.next_effective_schedule("project", before_open, before_open),
                         datetime.datetime(2026, 3, 8, 9, 0, tzinfo=zone).timestamp())
        after_close = datetime.datetime(2026, 3, 7, 17, 30, tzinfo=zone).timestamp()
        self.assertEqual(self.runtime.next_effective_schedule("project", after_close, after_close),
                         datetime.datetime(2026, 3, 8, 9, 0, tzinfo=zone).timestamp())
        due_inside_window = datetime.datetime(2026, 3, 9, 11, 0, tzinfo=zone).timestamp()
        now = datetime.datetime(2026, 3, 9, 9, 0, tzinfo=zone).timestamp()
        self.assertEqual(self.runtime.next_effective_schedule("project", due_inside_window, now), due_inside_window)

    def test_update_check_preserves_only_same_target_manual_install_intent(self):
        major, minor, _ = map(int, __version__.split("."))
        target = f"{major}.{minor + 1}.0"

        def release(version):
            name = f"adaptive-rsi-{version}-plugin.zip"
            prefix = f"https://github.com/Haoog/Adaptive-RSI/releases/download/v{version}/"
            return {"tag_name": "v" + version, "draft": False, "prerelease": False,
                    "assets": [{"name": name, "browser_download_url": prefix + name},
                               {"name": name + ".sha256", "browser_download_url": prefix + name + ".sha256"}]}

        self.store.put_setting("update-status", {"state": "waiting_idle", "current": __version__,
                                                   "version": target, "requested": True, "checked": 0})
        with patch("adaptive_rsi.updates.download", return_value=json.dumps(release(target)).encode()):
            self.runtime.updates.check()
        same = self.runtime.updates.status()
        self.assertEqual((same["state"], same["version"], same["requested"]), ("waiting_idle", target, True))

        newer = f"{major}.{minor + 2}.0"
        with patch("adaptive_rsi.updates.download", return_value=json.dumps(release(newer)).encode()):
            self.runtime.updates.check()
        changed = self.runtime.updates.status()
        self.assertEqual(changed["state"], "available")
        self.assertEqual(changed["version"], newer)
        self.assertNotIn("requested", changed)

    def test_issue_comment_cache_ignores_refresh_watermark_but_tracks_evidence(self):
        context = {"status": "loaded", "comments": [{"id": 7, "body": "Keep this behavior",
                    "updated_at": "2026-10-20T12:00:00Z"}], "complete": True,
                    "pages_complete": True, "omitted_count": 0, "warning": None,
                    "failure_code": None, "updated_at": "2026-10-20T12:01:00Z",
                    "fetched_at": 100}
        refreshed = {**context, "status": "cached", "updated_at": "2026-10-20T12:06:00Z", "fetched_at": 400}
        key = lambda value: fingerprint(_issue_comment_cache_evidence(value))
        self.assertEqual(key(context), key(refreshed))

        changed_comment = {**refreshed, "comments": [{**context["comments"][0], "body": "Remove this behavior"}]}
        self.assertNotEqual(key(refreshed), key(changed_comment))
        incomplete = {**refreshed, "complete": False, "warning": "Older comments were omitted"}
        self.assertNotEqual(key(refreshed), key(incomplete))


class ConfigRepairHTTPTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime = Runtime(Store(self.root / "state"), object())
        self.repo = fixture(self.root / "project")
        self.project = self.runtime.add_project(self.repo, enabled=True)
        self.server = Server(self.runtime)
        self.server_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(3)
        self.runtime.close()
        self.temp.cleanup()

    def post(self, path, payload, origin=None):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            body = json.dumps(payload).encode()
            connection.request("POST", path, body=body, headers={
                "Content-Type": "application/json", "Content-Length": str(len(body)),
                "Origin": origin or self.server.origin, "X-RSI-CSRF": self.server.csrf,
                "Cookie": self.server.cookie + "=" + self.server.token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def get(self, path):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            connection.request("GET", path, headers={"Cookie": self.server.cookie + "=" + self.server.token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_explicit_repair_backs_up_invalid_overrides_and_requires_manual_enable(self):
        from adaptive_rsi.store import encode
        invalid = ["{", "[]", json.dumps({"schedule": {"light_minutes": 1}}),
                   json.dumps({"schedule": {"timezone": "Invalid/Zone"}})]
        expected_backups = 0

        self.runtime.store.execute("UPDATE projects SET config=?,paused=0 WHERE id=?", (invalid[0], self.project))
        status, _ = self.post("/api/config/repair", {"project": self.project, "confirmation": "reset"})
        self.assertEqual(status, 400)
        self.assertEqual(self.runtime.project(self.project)["config"], invalid[0])
        self.assertEqual(self.runtime.project(self.project)["paused"], 0)

        for raw in invalid:
            with self.subTest(raw=raw):
                self.runtime.store.execute("UPDATE projects SET config=?,paused=0 WHERE id=?", (raw, self.project))
                status, state = self.get("/api/state")
                self.assertEqual(status, 200)
                shown = next(project for project in state["projects"] if project["id"] == self.project)
                self.assertTrue(shown["configuration_error"])
                self.assertTrue(shown["paused"])
                self.assertFalse(shown["configured_paused"])
                status, result = self.post("/api/config/repair", {
                    "project": self.project,
                    "confirmation": "reset-invalid-overrides-to-global-defaults"})
                self.assertEqual(status, 200, result)
                self.assertEqual(result, {"saved": True, "repaired": True, "paused": True})
                repaired = self.runtime.project(self.project)
                self.assertEqual((repaired["config"], repaired["paused"]), ("{}", 1))
                self.runtime.config(self.project)
                status, state = self.get("/api/state")
                self.assertEqual(status, 200)
                shown = next(project for project in state["projects"] if project["id"] == self.project)
                self.assertNotIn("configuration_error", shown)
                self.assertTrue(shown["paused"])
                expected_backups += 1

                backups = self.runtime.store.query(
                    "SELECT key,value FROM settings WHERE key LIKE ? ORDER BY key",
                    ("project-config-repair-backup:" + self.project + ":%",))
                self.assertEqual(len(backups), expected_backups)
                backup = next(json.loads(item["value"]) for item in backups
                              if json.loads(item["value"])["sha256"] == hashlib.sha256(raw.encode()).hexdigest())
                self.assertEqual(backup["raw"], raw)
                self.assertEqual(backup["sha256"], hashlib.sha256(raw.encode()).hexdigest())
                event = self.runtime.store.one(
                    "SELECT message,data FROM events WHERE kind='config_repaired' AND project=? ORDER BY seq DESC LIMIT 1",
                    (self.project,))
                self.assertNotIn(raw, event["message"])
                self.assertEqual(set(json.loads(event["data"])), {"backup_key", "sha256"})

                status, result = self.post("/api/config", {
                    "project": self.project, "values": {"schedule": {"light_minutes": 120}}})
                self.assertEqual(status, 200, result)
                self.assertEqual(self.runtime.project(self.project)["paused"], 1)
                status, result = self.post("/api/pause", {"project": self.project, "paused": False})
                self.assertEqual(status, 200, result)
                self.assertEqual(self.runtime.project(self.project)["paused"], 0)

        valid = encode({"schedule": {"light_minutes": 90}})
        self.runtime.store.execute("UPDATE projects SET config=?,paused=1 WHERE id=?", (valid, self.project))
        backup_count = len(self.runtime.store.query(
            "SELECT key FROM settings WHERE key LIKE ?", ("project-config-repair-backup:" + self.project + ":%",)))
        status, _ = self.post("/api/config/repair", {
            "project": self.project, "confirmation": "reset-invalid-overrides-to-global-defaults"})
        self.assertEqual(status, 400)
        self.assertEqual(self.runtime.project(self.project)["config"], valid)
        self.assertEqual(self.runtime.project(self.project)["paused"], 1)
        self.assertEqual(len(self.runtime.store.query(
            "SELECT key FROM settings WHERE key LIKE ?", ("project-config-repair-backup:" + self.project + ":%",))), backup_count)

    def test_repair_requires_valid_global_configuration(self):
        raw = "{"
        self.runtime.store.execute("UPDATE projects SET config=?,paused=0 WHERE id=?", (raw, self.project))
        self.runtime.store.put_setting("config", {"invalid": True})
        status, _ = self.post("/api/config/repair", {
            "project": self.project, "confirmation": "reset-invalid-overrides-to-global-defaults"})
        self.assertEqual(status, 400)
        self.assertEqual(self.runtime.project(self.project)["config"], raw)
        self.assertEqual(self.runtime.project(self.project)["paused"], 0)
        self.assertEqual(self.runtime.store.query(
            "SELECT key FROM settings WHERE key LIKE ?", ("project-config-repair-backup:" + self.project + ":%",)), [])

    def test_post_accepts_local_origin_besides_configured_public_origin(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(3)
        self.server = Server(self.runtime, public_url="https://rsi.example.com")
        self.server_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.server_thread.start()
        self.assertNotEqual(self.server.origin, self.server.local_origin)
        status, _ = self.post("/api/pause", {"project": self.project, "paused": True})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/pause", {"project": self.project, "paused": False}, origin=self.server.local_origin)
        self.assertEqual(status, 200)
        status, body = self.post("/api/pause", {"project": self.project, "paused": True}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(body, {"error": "Invalid origin or CSRF token"})


class ImportAdmissionTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime = Runtime(Store(self.root / "state"), object())
        self.repo = fixture(self.root / "project")
        self.project = self.runtime.add_project(self.repo, enabled=True)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_http_import_does_not_hold_global_lock_and_dispatch_waits_for_clone(self):
        self.runtime.goal(self.project, "Existing queued work")
        server = Server(self.runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        clone_entered, release_clone = threading.Event(), threading.Event()
        import_response, import_done = {}, threading.Event()
        operation_done, dispatch_done = threading.Event(), threading.Event()
        operations, submitted = [], []

        def post(path, payload):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
            try:
                body = json.dumps(payload).encode()
                connection.request("POST", path, body=body, headers={
                    "Content-Type": "application/json", "Content-Length": str(len(body)),
                    "Origin": server.origin, "X-RSI-CSRF": server.csrf,
                    "Cookie": server.cookie + "=" + server.token})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        def clone(argv, **kwargs):
            target = Path(argv[4])
            repo = fixture(target)
            git(repo, "remote", "add", "origin", "https://github.com/example/imported.git")
            clone_entered.set()
            release_clone.wait(3)

        def import_request():
            try:
                import_response["result"] = post("/api/projects/import", {
                    "url": "https://github.com/example/imported", "directory": str(self.root / "clones")})
            finally:
                import_done.set()

        def change_settings():
            operations.append(post("/api/config", {"project": self.project,
                                                    "values": {"schedule": {"light_minutes": 120}}}))
            operations.append(post("/api/pause", {"project": self.project, "paused": True}))
            operations.append(post("/api/pause", {"project": self.project, "paused": False}))
            operation_done.set()

        class Pool:
            def submit(self, fn, run):
                submitted.append(run)
                future = concurrent.futures.Future()
                future.set_result(None)
                return future

        def dispatch_work():
            try:
                self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
            finally:
                dispatch_done.set()

        import_thread = threading.Thread(target=import_request)
        settings_thread = threading.Thread(target=change_settings)
        dispatch_thread = threading.Thread(target=dispatch_work)
        original_pool = self.runtime.pool
        try:
            with patch("adaptive_rsi.runtime.checked", side_effect=clone):
                import_thread.start()
                self.assertTrue(clone_entered.wait(2))
                settings_thread.start()
                completed_before_clone = operation_done.wait(1)
                task = self.runtime.goal(self.project, "Should wait behind import")
                self.runtime.pool = Pool()
                dispatch_thread.start()
                dispatch_before_clone = dispatch_done.wait(1)
                self.assertTrue(completed_before_clone, "pause/config should not wait for the GitHub clone")
                self.assertTrue(dispatch_before_clone, "dispatch should promptly observe the import admission flag")
                self.assertEqual(submitted, [])
                release_clone.set()
                settings_thread.join(3)
                dispatch_thread.join(3)
                import_thread.join(3)
            self.assertEqual(self.store_active_runs(), [])
            self.assertTrue(completed_before_clone, "pause/config should not wait for the GitHub clone")
            self.assertTrue(import_done.is_set())
            self.assertEqual(import_response["result"][0], 200)
            self.assertFalse(self.runtime.import_in_progress)
            self.assertEqual(self.runtime.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
        finally:
            release_clone.set()
            for thread in [settings_thread, dispatch_thread, import_thread]:
                if thread.is_alive():
                    thread.join(3)
            self.runtime.pool = original_pool
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def store_active_runs(self):
        return self.runtime.store.query("SELECT id FROM runs WHERE ended IS NULL")

    def test_import_rechecks_project_overlap_and_releases_admission_flag(self):
        entered, release = threading.Event(), threading.Event()
        parent = self.root / "clones"
        target = parent / "example--imported"
        child = target / "nested"
        errors = []

        def clone(argv, **kwargs):
            repository = fixture(Path(argv[4]))
            git(repository, "remote", "add", "origin", "https://github.com/example/imported.git")
            fixture(child)
            entered.set()
            release.wait(3)

        def importing():
            try:
                self.runtime.import_project("https://github.com/example/imported", str(parent))
            except Exception as exc:
                errors.append(exc)

        with patch("adaptive_rsi.runtime.checked", side_effect=clone):
            thread = threading.Thread(target=importing)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.runtime.add_project(child)
            release.set()
            thread.join(3)

        self.assertTrue(errors)
        self.assertIn("must not contain", str(errors[0]))
        self.assertTrue((child / ".git").exists())
        self.assertTrue((target / ".git").exists())
        self.assertFalse(self.runtime.import_in_progress)

    def test_failed_clone_clears_admission_flag(self):
        with patch("adaptive_rsi.runtime.checked", side_effect=RuntimeError("clone failed")):
            with self.assertRaisesRegex(RuntimeError, "clone failed"):
                self.runtime.import_project("https://github.com/example/imported", str(self.root / "clones"))
        self.assertFalse(self.runtime.import_in_progress)


class ReleaseDispatchRejectionTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime = Runtime(Store(self.root / "state"), object())
        self.repo = fixture(self.root / "repo")
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.project = self.runtime.add_project(self.repo, enabled=True)
        self.policy = {"enabled": True, "workflow": ".github/workflows/publish.yml", "ref": "trusted-v1", "sha": TRUSTED}
        self.runtime.configure({"github": {"repository": "example/project"},
                                "delivery": {"mode": "pr", "release": self.policy}}, self.project)
        self.run = self.make_merged_run("rejected dispatch")

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def make_merged_run(self, title):
        task = self.runtime.goal(self.project, title)
        run = self.runtime.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.store.transition(run["id"], "merged", {"merged_sha": MERGED, "feedback": {"state": "verified", "head": MERGED}}, True)
        run = self.runtime.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.runtime.store.put_setting("delivery:" + run["id"], {"repository": "example/project"})
        return run

    def api(self, failure=None, pin=TRUSTED):
        calls = {"dispatch": 0}

        def call(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("git/ref/tags/"):
                return {"object": {"type": "commit", "sha": pin}}
            if endpoint.startswith("contents/"):
                return {"type": "file", "encoding": "base64", "content": base64.b64encode(WORKFLOW.encode()).decode()}
            if "/dispatches" in endpoint:
                calls["dispatch"] += 1
                if calls["dispatch"] == 1 and failure is not None:
                    raise failure
                return {}
            if "/runs?" in endpoint:
                return {"workflow_runs": []}
            raise AssertionError(endpoint)

        return calls, call

    def test_403_and_422_dispatch_rejections_are_retryable_after_pin_check(self):
        for code in [403, 422]:
            with self.subTest(code=code):
                run = self.run if code == 403 else self.make_merged_run("Rejected dispatch " + str(code))
                calls, api = self.api(ApiRejected("GitHub rejected dispatch", code))
                with patch("adaptive_rsi.release.api", side_effect=api):
                    result = advance(self.runtime, self.runtime.project(self.project), run,
                                     {"merged_sha": MERGED, "feedback": {"state": "verified", "head": MERGED}})["release"]
                    self.assertEqual((result["state"], result["dispatch_receipt"], result["dispatch_http_status"]),
                                     ("failed", "rejected", code))
                    self.assertEqual(calls["dispatch"], 1)

                    _, drifted_api = self.api(pin="d" * 40)
                    with patch("adaptive_rsi.release.api", side_effect=drifted_api), self.assertRaisesRegex(ValueError, "tag changed"):
                        retry(self.runtime, self.project, run["id"], "release")
                    self.assertEqual(calls["dispatch"], 1)

                    calls, retry_api = self.api()
                    with patch("adaptive_rsi.release.api", side_effect=retry_api):
                        retried = retry(self.runtime, self.project, run["id"], "release")
                    self.assertEqual(retried["state"], "submitted")
                    self.assertEqual(retried["dispatch_receipt"], "accepted")
                    self.assertEqual(calls["dispatch"], 1)

    def test_uncertain_dispatch_remains_unknown_and_never_reposts(self):
        calls, api = self.api(RuntimeError("network disconnected after request"))
        with patch("adaptive_rsi.release.api", side_effect=api):
            result = advance(self.runtime, self.runtime.project(self.project), self.run,
                             {"merged_sha": MERGED, "feedback": {"state": "verified", "head": MERGED}})["release"]
            self.assertEqual((result["state"], result["dispatch_receipt"]), ("dispatch_unknown", "unknown"))
            with self.assertRaisesRegex(ValueError, "confirmed failed"):
                retry(self.runtime, self.project, self.run["id"], "release")
        self.assertEqual(calls["dispatch"], 1)

    def test_rollback_rejection_is_retryable_without_repeating_ambiguous_dispatch(self):
        deploy = {"enabled": True, "workflow": ".github/workflows/publish.yml", "ref": "trusted-v1", "sha": TRUSTED,
                  "health_url": "https://service.example/health", "automatic_rollback": True,
                  "rollback_data_policy": "backward_compatible"}
        self.runtime.configure({"delivery": {"deploy": deploy}}, self.project)
        run = self.make_merged_run("Rollback dispatch")
        self.runtime.store.execute("UPDATE runs SET config=? WHERE id=?",
                                   (json.dumps(self.runtime.config(self.project)), run["id"]))
        run = self.runtime.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.runtime.store.put_setting(owner_key(self.project, "example/project", deploy),
                                       {"run": run["id"], "sha": MERGED})
        data = {"state": "health_failed", "repository": "example/project", "sha": MERGED, "kind": "deploy",
                "workflow": deploy["workflow"], "ref": deploy["ref"], "workflow_sha": TRUSTED,
                "operation_id": "rsi-deploy-operation", "rollback_target": {"sha": PREVIOUS}}
        save(self.runtime, run, "deploy", data)
        calls, api = self.api(ApiRejected("GitHub rejected rollback", 422))
        with patch("adaptive_rsi.release.api", side_effect=api), self.assertRaises(ApiRejected):
            deployment_health(self.runtime, self.runtime.project(self.project), run, data, deploy, None)
        saved = self.runtime.store.setting(key(run["id"], "deploy"))
        self.assertEqual((saved["state"], saved["rollback"]["state"], saved["rollback"]["dispatch_receipt"]),
                         ("rollback_failed", "failed", "rejected"))
        self.assertEqual(calls["dispatch"], 1)

        calls, retry_api = self.api()
        with patch("adaptive_rsi.release.api", side_effect=retry_api):
            retried = retry(self.runtime, self.project, run["id"], "deploy")
        self.assertEqual((retried["state"], retried["rollback"]["state"], retried["rollback"]["dispatch_receipt"]),
                         ("rolling_back", "submitted", "accepted"))
        self.assertEqual(calls["dispatch"], 1)


class CliAndRepositoryErrorTest(unittest.TestCase):
    """固定业务校验必须以 400 与原文到达 CLI 和面板，而不是内部错误。"""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_repository_rejects_unusable_roots_with_fixed_message(self):
        from adaptive_rsi.gitops import repository
        plain = self.root / "plain"
        plain.mkdir()
        commitless = self.root / "empty-repo"
        commitless.mkdir()
        git(commitless, "init", "--quiet")
        for path in (plain, commitless):
            with self.assertRaises(ValueError) as caught:
                repository(path)
            self.assertEqual(str(caught.exception), "Select the root of an existing Git repository")
        usable = fixture(self.root / "usable")
        self.assertEqual(repository(usable), usable)

    def test_add_retry_cancel_and_goal_conflicts_map_to_fixed_validation_messages(self):
        runtime = Runtime(Store(self.root / "state"), object())
        self.addCleanup(runtime.close)
        plain = self.root / "plain"
        plain.mkdir()
        repo = fixture(self.root / "project")
        project = runtime.add_project(repo)
        runtime.goal(project, "First requirement text", request_id="probe-1")
        with self.assertRaises(ValueError) as goal_conflict:
            runtime.goal(project, "Different text entirely", request_id="probe-1")
        self.assertEqual(str(goal_conflict.exception), "Submission ID was already used with different content")
        with self.assertRaises(ValueError) as retry_missing:
            runtime.retry("missing-task")
        self.assertEqual(str(retry_missing.exception), "This task cannot be retried")
        with self.assertRaises(ValueError) as cancel_missing:
            runtime.cancel_task("missing-task")
        self.assertEqual(str(cancel_missing.exception), "Task not found")
        runtime.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                              "VALUES ('manual-ready',?,'strategy','ready',?,?,?)",
                              (project, json.dumps({"guidance": "Check arithmetic boundaries first",
                                                    "triggers": ["add"]}),
                               json.dumps({"reason": "verified"}), time.time()))

        server = Server(runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(server_thread.join, 3)

        import http.client

        def post(path, payload):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
            try:
                body = json.dumps(payload).encode()
                connection.request("POST", path, body=body, headers={
                    "Content-Type": "application/json", "Content-Length": str(len(body)),
                    "Origin": server.origin, "X-RSI-CSRF": server.csrf,
                    "Cookie": server.cookie + "=" + server.token})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        status, payload = post("/api/projects", {"path": str(plain)})
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "Select the root of an existing Git repository",
                                   "error_code": "validation_error"})
        status, payload = post("/api/retry", {"task": "missing-task"})
        self.assertEqual((status, payload["error"], payload["error_code"]),
                         (400, "This task cannot be retried", "validation_error"))
        status, payload = post("/api/cancel", {"task": "missing-task"})
        self.assertEqual((status, payload["error"], payload["error_code"]),
                         (400, "Task not found", "validation_error"))
        status, payload = post("/api/evolution/promote", {"candidate": "manual-ready"})
        self.assertEqual((status, payload), (200, {"candidate": "manual-ready", "status": "stable"}))
        status, payload = post("/api/evolution/promote", {"candidate": "manual-ready"})
        self.assertEqual((status, payload["error"], payload["error_code"]),
                         (400, "Only a verified ready candidate can be promoted", "validation_error"))
        status, payload = post("/api/evolution/promote", {"candidate": "missing-candidate"})
        self.assertEqual((status, payload["error"], payload["error_code"]),
                         (400, "Candidate not found", "validation_error"))
        runtime.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                              "VALUES ('incompatible-ready',?,'strategy','ready',?,?,?)",
                              (project, json.dumps({"guidance": "Check arithmetic boundaries first",
                                                    "triggers": ["add"]}),
                               json.dumps({"compatibility": "obsolete-fingerprint"}), time.time()))
        before = {table: runtime.store.query("SELECT * FROM " + table)
                  for table in ("candidates", "skills", "events")}
        status, payload = post("/api/evolution/promote", {"candidate": "incompatible-ready"})
        self.assertEqual((status, payload),
                         (400, {"error": "Candidate evidence is incompatible with current configuration",
                                "error_code": "validation_error"}))
        for table, rows in before.items():
            self.assertEqual(runtime.store.query("SELECT * FROM " + table), rows, table)
        self.assertEqual([item["id"] for item in runtime.active_strategies(project)], ["manual-ready"])

    def test_revision_validation_errors_reach_the_console_verbatim(self):
        from adaptive_rsi import server as server_module
        messages = [
            "Supplement must contain 1..10000 characters",
            "Pause or finish active work before supplementing this task",
            "Combined requirements exceed 30000 characters",
        ]
        for message in messages:
            self.assertIn(message, server_module.SAFE_VALIDATION_MESSAGES)

        runtime = Runtime(Store(self.root / "state"), object())
        self.addCleanup(runtime.close)
        project = runtime.add_project(fixture(self.root / "project"))
        too_long = runtime.goal(project, "First requirement")
        inactive = runtime.goal(project, "Second requirement")
        oversized = runtime.goal(project, "Third requirement")
        # 已完成任务不能补充；超长正文用于触发合并后的需求上限。
        runtime.store.execute("UPDATE tasks SET state='completed' WHERE id=?", (inactive,))
        runtime.store.execute("UPDATE tasks SET body=? WHERE id=?", ("x" * 30000, oversized))

        server = Server(runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(server_thread.join, 3)

        import http.client

        def post(payload):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
            try:
                body = json.dumps(payload).encode()
                connection.request("POST", "/api/task/revise", body=body, headers={
                    "Content-Type": "application/json", "Content-Length": str(len(body)),
                    "Origin": server.origin, "X-RSI-CSRF": server.csrf,
                    "Cookie": server.cookie + "=" + server.token})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        cases = [
            ({"task": too_long, "supplement": "x" * 10001},
             "Supplement must contain 1..10000 characters"),
            ({"task": inactive, "supplement": "A valid supplement"},
             "Pause or finish active work before supplementing this task"),
            ({"task": oversized, "supplement": "y"},
             "Combined requirements exceed 30000 characters"),
        ]
        for payload, message in cases:
            status, body = post(payload)
            self.assertEqual((status, body["error"], body["error_code"]),
                             (400, message, "validation_error"))

    def test_cli_api_surfaces_fixed_server_messages(self):
        import io
        import urllib.error
        from adaptive_rsi import cli
        state = self.root / "cli-state"
        state.mkdir()
        (state / "server.json").write_text(json.dumps(
            {"origin": "https://127.0.0.1:1", "cookie": "rsi", "token": "t", "csrf": "c"}))

        def error_for(payload, code=400):
            body = json.dumps(payload).encode()
            return urllib.error.HTTPError("https://127.0.0.1:1/api/retry", code, "Bad Request",
                                          {}, io.BytesIO(body))

        def error_for_json(raw, code=400):
            return urllib.error.HTTPError("https://127.0.0.1:1/api/retry", code, "Bad Request",
                                          {}, io.BytesIO(raw.encode()))

        with patch("urllib.request.urlopen", side_effect=error_for(
                {"error": "This task cannot be retried", "error_code": "validation_error"})):
            with self.assertRaises(RuntimeError) as caught:
                cli.api(state, "/api/retry", {"task": "missing-task"})
        self.assertEqual(str(caught.exception), "This task cannot be retried")
        with patch("urllib.request.urlopen", side_effect=error_for(
                {"error": "Request could not be completed", "error_code": "internal_error"}, 500)):
            with self.assertRaises(RuntimeError) as caught:
                cli.api(state, "/api/retry", {"task": "missing-task"})
        self.assertEqual(str(caught.exception), "Request could not be completed (internal_error)")
        with patch("urllib.request.urlopen", side_effect=error_for({}, 502)):
            with self.assertRaises(RuntimeError) as caught:
                cli.api(state, "/api/retry", {"task": "missing-task"})
        self.assertEqual(str(caught.exception), "HTTP Error 502")
        # 合规代理可能返回合法 JSON 但不是对象；一行固定错误，不打印堆栈。
        for body in ("null", "[]", '"gateway"'):
            with patch("urllib.request.urlopen", side_effect=error_for_json(body, 502)):
                with self.assertRaises(RuntimeError) as caught:
                    cli.api(state, "/api/retry", {"task": "missing-task"})
            self.assertEqual(str(caught.exception), "HTTP Error 502")


if __name__ == "__main__":
    unittest.main()
