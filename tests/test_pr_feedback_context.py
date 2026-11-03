import tempfile
import unittest
from pathlib import Path

from adaptive_rsi.store import Store
from adaptive_rsi.delivery import record_pr_feedback, pr_feedback_context


class PRFeedbackContextTest(unittest.TestCase):
    def test_gh_shapes_and_missing_ids_preserve_human_feedback_ignore_bot_comments(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root))
            run = {"id": "r", "task": "t", "project": "p"}
            pr = {"comments": [
                {"author": {"login": "alice"}, "body": "Please fix", "createdAt": "2026-11-02T04:49:25Z", "url": "https://github.com/o/r/pull/1#issuecomment-1"},
                {"author": {"login": "github-actions"}, "body": "Automated notice", "id": "IC_bot"}],
                "reviews": [{"author": {"login": "bob"}, "body": "Fix this", "state": "COMMENTED", "submittedAt": "2026-11-02T04:49:17Z", "commit": {"oid": "abc"}},
                            {"author": {"login": "copilot-pull-request-reviewer"}, "body": "Automated review", "state": "CHANGES_REQUESTED"}]}
            keys = record_pr_feedback(store, run, "pr", pr)
            # 评论只是上下文；只有人类明确请求修改才会驱动重排。
            self.assertEqual(keys, [])
            self.assertEqual({row["body"] for row in pr_feedback_context(store, "p", "t")}, {"Please fix", "Fix this"})
            store.close()

    def test_ci_identity_ignores_pr_run_and_check_url_but_changes_for_new_failure(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root))
            run = {"id": "r", "task": "t", "project": "p"}
            def observe(pr, head, state, url):
                return record_pr_feedback(store, run, pr, {"headRefOid": head, "statusCheckRollup": [
                    {"name": "verify", "conclusion": state, "detailsUrl": url}]})
            first = observe("pr1", "a", "FAILURE", "check1")
            self.assertEqual(first, observe("pr2", "b", "FAILURE", "check2"))
            self.assertNotEqual(first, observe("pr2", "b", "TIMED_OUT", "check2"))
            store.close()

    def test_feedback_is_durable_deduplicated_and_scoped_to_original_task(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root))
            run = {"id": "run-1", "task": "task-1", "project": "project-1"}
            pr = {"headRefOid": "abc", "comments": [{"id": "C1", "body": "Please fix", "url": "https://example/c"}],
                  "reviews": [{"id": "R1", "body": "Needs tests", "state": "CHANGES_REQUESTED"}],
                  "statusCheckRollup": [{"name": "verify", "conclusion": "FAILURE", "detailsUrl": "https://example/check"}]}
            record_pr_feedback(store, run, "https://example/pr/1", pr)
            record_pr_feedback(store, run, "https://example/pr/1", pr)
            rows = pr_feedback_context(store, "project-1", "task-1")
            self.assertEqual({row["kind"] for row in rows}, {"comment", "review", "ci"})
            self.assertEqual(len(rows), 3)
            self.assertEqual(next(row for row in rows if row["kind"] == "comment")["body"], "Please fix")
            self.assertEqual(pr_feedback_context(store, "project-1", "other"), [])
            self.assertEqual(pr_feedback_context(store, "other", "task-1"), [])
            pr["comments"][0]["body"] = "Edited comment"
            record_pr_feedback(store, run, "https://example/pr/1", pr)
            self.assertEqual(len(pr_feedback_context(store, "project-1", "task-1")), 4)
            store.close()

    def test_planning_context_bounds_external_content_without_discarding_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root))
            run = {"id": "r", "task": "t", "project": "p"}
            record_pr_feedback(store, run, "pr", {"comments": [
                {"id": str(i), "body": "x" * 5000} for i in range(25)]})
            rows = pr_feedback_context(store, "p", "t")
            self.assertEqual(len(rows), 20)
            self.assertTrue(all(len(row["body"]) == 4000 and row["truncated"] for row in rows))
            self.assertEqual(len(store.query("SELECT value FROM settings WHERE key LIKE 'pr-feedback:%'")), 25)
            store.close()

    def test_change_request_superseded_by_approval_is_not_actionable(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root))
            run = {"id": "r", "task": "t", "project": "p"}
            pr = {"reviewDecision": "APPROVED", "reviews": [
                {"id": "R1", "body": "", "state": "CHANGES_REQUESTED", "author": {"login": "alice"}},
                {"id": "R2", "body": "ok", "state": "APPROVED", "author": {"login": "alice"}}]}
            self.assertEqual(record_pr_feedback(store, run, "pr", pr), [])
            pr["reviewDecision"] = "CHANGES_REQUESTED"
            self.assertTrue(record_pr_feedback(store, run, "pr", pr))
            store.close()


if __name__ == "__main__":
    unittest.main()
