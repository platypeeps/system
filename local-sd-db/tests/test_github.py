"""Two doors, one truth: the HTTP double and the `gh` shim."""

import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sd_db.testing import FixtureRemote, GitHubDouble, gh_environment, install_gh


def get(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        with error:
            raw = error.read()
        return error.code, (json.loads(raw) if raw else None)


class DoubleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = FixtureRemote(self.root)
        self.double = GitHubDouble(self.remote)
        self.double.__enter__()
        self.addCleanup(self.double.close)
        self.base = self.double.base_url

    def gh(self, *args, expect=0):
        environment = gh_environment(self.double, self.bin_dir)
        completed = subprocess.run(
            [str(self.shim), *args], capture_output=True, text=True, input="", env=environment
        )
        self.assertEqual(completed.returncode, expect, completed.stderr)
        return completed

    @property
    def bin_dir(self):
        return self.root / "bin"

    @property
    def shim(self):
        return install_gh(self.double, self.bin_dir)


class TheHttpDoor(DoubleCase):
    def test_the_repository_answers_with_its_default_branch(self):
        status, payload = get(f"{self.base}/repos/fixture/repo")
        self.assertEqual(status, 200)
        self.assertEqual(payload["default_branch"], "main")

    def test_an_unrouted_path_is_a_404_and_is_recorded(self):
        status, payload = get(f"{self.base}/repos/fixture/repo/nonsense")
        self.assertEqual(status, 404)
        self.assertIn("no route", payload["message"])
        self.assertEqual(self.remote.calls[-1].status, 404)

    def test_protection_is_a_404_until_it_is_set(self):
        status, _ = get(f"{self.base}/repos/fixture/repo/branches/main/protection")
        self.assertEqual(status, 404)
        self.remote.protection = {"required_pull_request_reviews": {}}
        status, payload = get(f"{self.base}/repos/fixture/repo/branches/main/protection")
        self.assertEqual(status, 200)
        self.assertIn("required_pull_request_reviews", payload)

    def test_compare_reports_behind_by(self):
        self.remote.commit_on("feature", "work")
        self.remote.commit_on("main", "moved on")
        _status, payload = get(f"{self.base}/repos/fixture/repo/compare/main...feature")
        self.assertEqual(payload["behind_by"], 1)

    def test_a_stale_sha_is_a_405_through_the_door_too(self):
        stale = self.remote.commit_on("feature", "work")
        pull = self.remote.open_pull_request("feature")
        self.remote.commit_on("feature", "one more")

        status, payload = get(
            f"{self.base}/repos/fixture/repo/pulls/{pull.number}/merge",
            method="PUT",
            body={"sha": stale, "merge_method": "squash"},
        )

        self.assertEqual(status, 405)
        self.assertIn("Head branch was modified", payload["message"])
        self.assertEqual(self.remote.calls[-1].status, 405)
        self.assertEqual(self.remote.calls[-1].door, "http")
        self.assertEqual(self.remote.pull(pull.number).state, "OPEN")

    def test_deleting_a_branch_removes_it(self):
        self.remote.commit_on("feature", "work")
        status, _ = get(
            f"{self.base}/repos/fixture/repo/git/refs/heads/feature", method="DELETE"
        )
        self.assertEqual(status, 204)
        self.assertNotIn("feature", self.remote.branches())


class TheGhDoor(DoubleCase):
    def test_the_shim_is_executable_and_first_on_path(self):
        shim = self.shim
        self.assertTrue(os.access(shim, os.X_OK))
        environment = gh_environment(self.double, self.bin_dir)
        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(self.bin_dir))

    def test_pr_view_reports_the_json_field_names(self):
        self.remote.commit_on("feature", "work")
        pull = self.remote.open_pull_request("feature", title="Work")
        payload = json.loads(self.gh("pr", "view", str(pull.number)).stdout)
        self.assertEqual(payload["headRefName"], "feature")
        self.assertEqual(payload["state"], "OPEN")
        self.assertEqual(payload["mergeStateStatus"], "CLEAN")

    def test_a_stale_sha_is_refused_through_gh_and_recorded_as_gh(self):
        stale = self.remote.commit_on("feature", "work")
        pull = self.remote.open_pull_request("feature")
        self.remote.commit_on("feature", "one more")

        completed = self.gh(
            "pr", "merge", str(pull.number), "--squash", "--match-head-commit", stale, expect=1
        )

        self.assertIn("Head branch was modified", completed.stderr)
        self.assertEqual(self.remote.calls[-1].status, 405)
        self.assertEqual(self.remote.calls[-1].door, "gh")
        self.assertEqual(self.remote.pull(pull.number).state, "OPEN")

    def test_both_doors_read_the_same_table(self):
        self.remote.commit_on("feature", "work")
        pull = self.remote.open_pull_request("feature", title="Work")
        self.gh("pr", "merge", str(pull.number), "--squash")

        _status, payload = get(f"{self.base}/repos/fixture/repo/pulls/{pull.number}")

        self.assertEqual(payload["state"], "merged")
        doors = [call.door for call in self.remote.calls]
        self.assertIn("gh", doors)
        self.assertIn("http", doors)

    def test_gh_api_reaches_an_arbitrary_path(self):
        payload = json.loads(self.gh("api", "/repos/fixture/repo").stdout)
        self.assertEqual(payload["default_branch"], "main")

    def test_gh_api_failure_exits_non_zero(self):
        completed = self.gh("api", "/repos/fixture/repo/nonsense", expect=1)
        self.assertIn("no route", completed.stderr)


if __name__ == "__main__":
    unittest.main()
