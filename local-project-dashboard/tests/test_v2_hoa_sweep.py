"""Run sweep on the HOA page (sd:5095).

What this slice promises: `POST /api/hoa/sweep` starts the hoa checkout's `tools/sweep.py run` in the background and
answers 202 at once; a second POST while that run holds the lock is a 409 and starts nothing; once the run ends, a new
POST starts again. `GET /api/hoa/sweep/status` reads the checkout's `tools/sweep.py status --json` and says whether a run holds
the lock. A run that ended without a record of its own reads failed, naming the start log, never the older run.

The checkout's `tools/sweep.py` here is a stub that counts its runs and waits for a release file. No test calls
claude, Gmail or launchctl.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import sweep

from test_workflow_actions import BrowserSession

#: The stub: `run` counts itself and waits for `release` (or dies at once when `die` exists); `status --json` prints
#: the record a finished run wrote, else that no sweep has run.
STUB = r'''import json, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent.parent
record = here / "record.json"
if sys.argv[1:] == ["status", "--json"]:
    print(record.read_text() if record.exists() else json.dumps({"running": False, "state": "never"}))
elif sys.argv[1:] == ["run"]:
    if (here / "die").exists():
        print("boom: the stub died before its record")
        sys.exit(1)
    (here / "path.txt").write_text(__import__("os").environ.get("PATH", ""))
    with open(here / "runs.txt", "a") as runs:
        runs.write("run\n")
    started = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    for _ in range(600):
        if (here / "release").exists():
            break
        time.sleep(0.05)
    record.write_text(json.dumps({"running": False, "state": "done", "exit": 0, "started": started,
                                  "finished": started, "reason": "RESULT: ok", "log": str(here / "x.log")}))
else:
    sys.exit(1)
'''


def wait(test, until, what):
    for _ in range(200):
        if until():
            return
        time.sleep(0.05)
    test.fail(f"timed out waiting for {what}")


class RunSweep(BrowserSession):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.hoa = root / "hoa"
        (self.hoa / "tools").mkdir(parents=True)
        (self.hoa / "tools" / "sweep.py").write_text(STUB)
        self.repo(str(self.hoa))
        patch = mock.patch.object(sweep, "state_dir", return_value=root / "state")
        patch.start()
        self.addCleanup(patch.stop)
        # A run still waiting when a test ends is released, so no stub outlives its folder.
        self.addCleanup(self.finish)

    def finish(self):
        (self.hoa / "release").touch()
        wait(self, lambda: not sweep.status(self.hoa)["running"], "the run to end")

    def runs(self):
        try:
            return len((self.hoa / "runs.txt").read_text().splitlines())
        except FileNotFoundError:
            return 0

    def read(self):
        status, _, body = self.request("/api/hoa/sweep/status", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_a_post_starts_one_run_and_a_second_post_while_it_runs_is_refused(self):
        status, _, body = self.post("/api/hoa/sweep", {})
        self.assertEqual(status, 202, body)
        self.assertEqual((body["repo"], body["command"]), (str(self.hoa), "python3 tools/sweep.py run"))
        wait(self, lambda: self.runs() == 1, "the stub's run")
        # The run calls sd and sd-ship, which install in ~/.local/bin, a folder the LaunchAgent's PATH may lack.
        self.assertIn(str(Path.home() / ".local" / "bin"), (self.hoa / "path.txt").read_text().split(":"))
        self.assertTrue(self.read()["running"])

        status, _, body = self.post("/api/hoa/sweep", {})
        self.assertEqual(status, 409, body)
        self.assertIn("A hoa sweep is already running", body["error"])
        time.sleep(0.3)
        self.assertEqual(self.runs(), 1)

        self.finish()
        doc = self.read()
        self.assertFalse(doc["running"])
        self.assertEqual((doc["last"]["state"], doc["last"]["exit"]), ("done", 0))
        status, _, body = self.post("/api/hoa/sweep", {})
        self.assertEqual(status, 202, body)
        wait(self, lambda: self.runs() == 2, "the second run")

    def test_a_run_that_dies_before_its_record_reads_failed_and_names_the_start_log(self):
        (self.hoa / "die").touch()
        status, _, _ = self.post("/api/hoa/sweep", {})
        self.assertEqual(status, 202)
        wait(self, lambda: not sweep.status(self.hoa)["running"], "the stub to die")
        doc = self.read()
        self.assertEqual(doc["last"]["state"], "failed")
        self.assertIn("ended without a record of its own", doc["last"]["reason"])
        self.assertIn("boom: the stub died", Path(doc["start_log"]).read_text())

    def test_a_post_with_fields_is_refused_before_anything_starts(self):
        status, _, body = self.post("/api/hoa/sweep", {"repo": "/elsewhere"})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.runs(), 0)

    def test_without_the_script_in_the_checkout_nothing_starts_and_the_read_says_why(self):
        (self.hoa / "tools" / "sweep.py").unlink()
        status, _, body = self.post("/api/hoa/sweep", {})
        self.assertEqual(status, 400, body)
        self.assertIn("tools/sweep.py is not in the hoa checkout", body["error"])
        self.assertEqual(self.read()["error"], "tools/sweep.py is not in the checkout")

    def test_the_hoa_document_carries_the_sweep(self):
        status, _, body = self.request("/api/hoa", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        sweep = json.loads(body)["sweep"]
        self.assertEqual((sweep["running"], sweep["last"]["state"]), (False, "never"))


if __name__ == "__main__":
    unittest.main()
