"""`serve` reads the checkout's commit once and every heartbeat names it (sd:1952).

Python keeps the modules it loaded at start, so after a `git pull` the
daemon runs the old code until a restart. The heartbeat's `runner_commit`
is the checkout's HEAD when `serve` started, and `runner_checkout` the
checkout, which `heartbeat_state` compares with the HEAD on disk
(local-sd-db/tests/test_runner_commit.py).
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_runner import cli, runtime
from tests import test_runtime


class LoopFinished(Exception):
    pass


class RunnerCommit(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner

    def body(self):
        return self.fixture.db.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = 'runner'").fetchone()[0]

    def test_serve_reads_the_commit_once_and_every_pulse_writes_it(self):
        report = {"ok": True, "problems": [], "database_below_floor": False, "dispatch_allowed": False}
        with patch.object(runtime.storage, "preflight", return_value=report), \
                patch.object(runtime.gitops, "head", return_value="fixture-pack-head"), \
                patch.object(runtime, "checkout_commit", return_value="started-commit") as read, \
                patch.object(self.runner, "recover", return_value=[]), \
                patch.object(self.runner, "tick", side_effect=LoopFinished), \
                patch.object(self.runner, "refresh_archives"), patch.object(self.runner, "watch_deliveries"):
            with self.assertRaises(LoopFinished):
                self.runner.serve()
            read.return_value = "a later pull"
            for _ in range(2):
                state = self.runner.pulse(self.fixture.db)
        read.assert_called_once_with()
        self.assertEqual((state["runner_commit"], state["runner_checkout"]), ("started-commit", str(runtime.CHECKOUT)))

    def test_a_recovery_hold_heartbeat_names_the_commit(self):
        report = {"ok": True, "problems": [], "database_below_floor": False, "dispatch_allowed": False}
        with patch.object(runtime.storage, "preflight", return_value=report), \
                patch.object(runtime, "checkout_commit", return_value="started-commit"), \
                patch.object(self.runner, "recover", return_value=[{"reason": "fixture hold"}]), \
                self.assertRaises(runtime.store.RunnerRefused):
            self.runner.serve()
        self.assertIn('"runner_commit": "started-commit"', self.body())

    def test_checkout_commit_is_the_head_or_none(self):
        head = subprocess.run(["git", "-C", str(self.fixture.checkout), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(runtime.checkout_commit(self.fixture.checkout), head)
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(runtime.checkout_commit(Path(directory)))
        self.assertEqual(runtime.CHECKOUT, Path(runtime.__file__).resolve().parents[2])
        self.assertTrue((runtime.CHECKOUT / "local-sd-runner/runner.sh").is_file())

    def test_checkout_commit_starts_no_process(self):
        """sd:2254: a `git` child polled the tests' patched `time.sleep` under load."""
        head = subprocess.run(["git", "-C", str(self.fixture.checkout), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        with patch.object(subprocess, "run", side_effect=AssertionError("checkout_commit started a process")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("checkout_commit started a process")):
            self.assertEqual(runtime.checkout_commit(self.fixture.checkout), head)

    def test_status_warns_when_the_checkout_moved_and_still_exits_0(self):
        checkout = self.fixture.checkout
        started = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        store.heartbeat(self.fixture.db, {"healthy": True, "interval_seconds": 10, "runner_commit": started, "runner_checkout": str(checkout)})
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "--allow-empty", "-m", "pulled"], check=True)
        moved = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        config = self.fixture.root / "runner.json"
        config.write_text(json.dumps({"database": str(self.fixture.database)}))
        output = io.StringIO()
        with patch.object(cli, "agent_loaded", return_value=True), contextlib.redirect_stdout(output):
            code = cli.main(["status", "--config", str(config)])
        body = json.loads(output.getvalue())
        self.assertEqual(code, 0, body)
        self.assertEqual(body["deploy_warning"], f"runner started at {started[:7]}, checkout at {moved[:7]}; restart to deploy")


if __name__ == "__main__":
    unittest.main()
