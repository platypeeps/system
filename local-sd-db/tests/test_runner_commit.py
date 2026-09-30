"""The heartbeat names the checkout commit the daemon runs (sd:1952).

The daemon ran about 11 hours on code three merges old and nothing said so:
Python reads the runner's modules at start, and a `git pull` changes only
the files. `serve` reads `runner_commit` and `runner_checkout` once, at
start; `heartbeat_state` compares that commit with the checkout's on-disk
HEAD, with no fetch, and names a moved checkout in `deploy_warning`. A
heartbeat from a daemon that writes no commit reads `runner_commit unknown`.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner
from sd_db.database import connect
from sd_db.migrate import initialise


def git(root, *argv):
    return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, check=True).stdout.strip()


class RunnerCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        self.checkout = self.root / "checkout"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.checkout)], check=True)
        git(self.checkout, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture",
            "commit", "-q", "--allow-empty", "-m", "first")
        self.started = git(self.checkout, "rev-parse", "HEAD")

    def beat(self, **fields):
        return runner.heartbeat(self.db, {"healthy": True, "interval_seconds": 10, **fields})

    def test_a_checkout_that_moved_since_start_is_named(self):
        self.beat(runner_commit=self.started, runner_checkout=str(self.checkout))
        git(self.checkout, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture",
            "commit", "-q", "--allow-empty", "-m", "second")
        moved = git(self.checkout, "rev-parse", "HEAD")
        state = runner.heartbeat_state(self.db)
        self.assertEqual(state["runner_commit"], self.started)
        self.assertEqual(state["checkout_commit"], moved)
        self.assertEqual(state["deploy_warning"],
                         f"runner started at {self.started[:7]}, checkout at {moved[:7]}; restart to deploy")
        self.assertTrue(state["ok"], "a moved checkout warns; it does not make the runner unhealthy")

    def test_packed_refs_and_a_linked_worktree_are_read(self):
        git(self.checkout, "pack-refs", "--all")
        self.assertFalse((self.checkout / ".git/refs/heads/main").exists())
        self.assertEqual(runner.checkout_head(self.checkout), self.started)
        linked = self.root / "linked"
        git(self.checkout, "worktree", "add", "-q", "-b", "linked", str(linked))
        self.assertTrue((linked / ".git").is_file())
        self.assertEqual(runner.checkout_head(linked), self.started)
        git(self.checkout, "checkout", "-q", "--detach")
        self.assertEqual(runner.checkout_head(self.checkout), self.started)

    def test_a_read_starts_no_process(self):
        # Readers include the runtime's keepalive and tests that patch the
        # global time.sleep, which a subprocess timeout polls (sd:1778).
        self.beat(runner_commit=self.started, runner_checkout=str(self.checkout))
        with patch("subprocess.Popen", side_effect=AssertionError("a heartbeat read started a process")):
            self.assertEqual(runner.heartbeat_state(self.db)["checkout_commit"], self.started)

    def test_matching_commits_warn_nothing(self):
        self.beat(runner_commit=self.started, runner_checkout=str(self.checkout))
        state = runner.heartbeat_state(self.db)
        self.assertEqual(state["checkout_commit"], self.started)
        self.assertNotIn("deploy_warning", state)

    def test_an_old_daemon_without_the_field_reads_unknown(self):
        self.beat()
        state = runner.heartbeat_state(self.db)
        self.assertEqual(state["deploy_warning"], "runner_commit unknown")
        self.assertTrue(state["ok"])

    def test_a_daemon_started_outside_a_checkout_reads_unknown(self):
        self.beat(runner_commit=None, runner_checkout=str(self.root / "not-a-checkout"))
        self.assertEqual(runner.heartbeat_state(self.db)["deploy_warning"], "runner_commit unknown")

    def test_a_checkout_that_cannot_be_read_is_named(self):
        self.beat(runner_commit=self.started, runner_checkout=str(self.root / "gone"))
        state = runner.heartbeat_state(self.db)
        self.assertIsNone(state["checkout_commit"])
        self.assertEqual(state["deploy_warning"], f"runner started at {self.started[:7]}; checkout HEAD unreadable")

    def test_the_writer_stores_no_derived_field(self):
        # The runtime's long-preserve beat writes back what it read (`Runner.ending_keepalive`).
        self.beat(runner_commit=self.started, runner_checkout=str(self.checkout),
                  checkout_commit="stale", deploy_warning="stale")
        stored = self.db.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = 'runner'").fetchone()[0]
        self.assertNotIn("checkout_commit", stored)
        self.assertNotIn("deploy_warning", stored)


if __name__ == "__main__":
    unittest.main()
