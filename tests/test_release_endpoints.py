import os
import unittest
from unittest.mock import patch

from adaptive_rsi import release
from scripts import release as release_script


class ReleaseEndpointTest(unittest.TestCase):
    def test_repository_root_and_nonempty_paths_are_passed_exactly_to_gh(self):
        for endpoint in ["", "commits/main", "releases?per_page=100", "contents/.github/workflows/release.yml?ref=main"]:
            expected = "repos/example/project" + ("/" + endpoint if endpoint else "")
            with self.subTest(helper="script", endpoint=endpoint), patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/project"}), patch.object(release_script, "run", return_value='{"default_branch":"main"}') as command:
                self.assertEqual(release_script.gh(endpoint), {"default_branch": "main"})
                command.assert_called_once_with("gh", "api", expected, "--method", "GET")
            with self.subTest(helper="product", endpoint=endpoint), patch.object(release, "checked", return_value='{"default_branch":"main"}') as command:
                self.assertEqual(release.api("example/project", endpoint), {"default_branch": "main"})
                command.assert_called_once_with(["gh", "api", expected, "--method", "GET"])

    def test_nonempty_write_endpoint_retains_method_and_fields_without_executing(self):
        expected = ["gh", "api", "repos/example/project/git/refs", "--method", "POST", "-f", "ref=refs/tags/v1.0.0", "-f", "sha=" + "a" * 40]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/project"}), patch.object(release_script, "run", return_value="") as command:
            self.assertEqual(release_script.gh("git/refs", "POST", ref="refs/tags/v1.0.0", sha="a" * 40), {})
            command.assert_called_once_with(*expected)
        with patch.object(release, "checked", return_value="") as command:
            self.assertEqual(release.api("example/project", "git/refs", "POST", {"ref": "refs/tags/v1.0.0", "sha": "a" * 40}), {})
            command.assert_called_once_with(expected)


if __name__ == "__main__":
    unittest.main()
