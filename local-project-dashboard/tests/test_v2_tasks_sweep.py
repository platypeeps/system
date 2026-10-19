"""Run sweep on the Tasks page's Personal and Work views (sd:5096, sd:5097).

What this slice promises: `POST /api/tasks/sweep {"realm"}` starts that realm's sweep in the background and answers 202
at once; a second POST for the same realm while its run holds the lock is a 409 and starts nothing, while the other
realm still starts; any other body is a 400. `GET /api/tasks/sweep/status` and `/api/tasks`' `sweeps` read each realm's
record and whether a run holds its lock.

The runner here is a stub (`sweep.REALM_RUNNER`) that counts its runs, waits for a release file and writes the record a
finished run writes. No test calls claude, Gmail or launchctl.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import sweep

from test_workflow_actions import BrowserSession

STUB = r'''import json, os, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent
realm = sys.argv[2]
assert sys.argv[1] == "run"
with open(here / f"runs-{realm}.txt", "a") as runs:
    runs.write("run\n")
started = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
for _ in range(600):
    if (here / f"release-{realm}").exists():
        break
    time.sleep(0.05)
state = pathlib.Path(os.environ["XDG_STATE_HOME"]) / "system" / f"{realm}-sweep"
(state / "last.json").write_text(json.dumps({"state": "done", "exit": 0, "started": started, "finished": started,
                                             "reason": "filed brief; RESULT: ok", "log": "x.log"}))
'''


def wait(test, until, what):
    for _ in range(200):
        if until():
            return
        time.sleep(0.05)
    test.fail(f"timed out waiting for {what}")


class RunRealmSweep(BrowserSession):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "stub.py").write_text(STUB)
        for patch in (mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}),
                      mock.patch.object(sweep, "REALM_RUNNER", (sys.executable, str(self.root / "stub.py")))):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.finish)

    def finish(self):
        for realm in ("work", "personal"):
            (self.root / f"release-{realm}").touch()
            wait(self, lambda: not sweep.status(None, realm)["running"], f"the {realm} run to end")

    def runs(self, realm):
        try:
            return len((self.root / f"runs-{realm}.txt").read_text().splitlines())
        except FileNotFoundError:
            return 0

    def read(self, path="/api/tasks/sweep/status"):
        status, _, body = self.request(path, headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_one_run_per_realm_and_the_other_realm_still_starts(self):
        status, _, body = self.post("/api/tasks/sweep", {"realm": "work"})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["command"], "python3 -m sd_dashboard.realm_sweep run work")
        wait(self, lambda: self.runs("work") == 1, "the work run")
        self.assertTrue(self.read()["work"]["running"])

        status, _, body = self.post("/api/tasks/sweep", {"realm": "work"})
        self.assertEqual(status, 409, body)
        self.assertIn("A work sweep is already running", body["error"])
        status, _, body = self.post("/api/tasks/sweep", {"realm": "personal"})
        self.assertEqual(status, 202, body)
        wait(self, lambda: self.runs("personal") == 1, "the personal run")
        time.sleep(0.3)
        self.assertEqual(self.runs("work"), 1)

        self.finish()
        doc = self.read()
        self.assertEqual([(doc[r]["running"], doc[r]["last"]["state"]) for r in ("work", "personal")],
                         [(False, "done"), (False, "done")])
        self.assertEqual(self.read("/api/tasks")["sweeps"]["work"]["last"]["state"], "done")
        status, _, body = self.post("/api/tasks/sweep", {"realm": "work"})
        self.assertEqual(status, 202, body)
        wait(self, lambda: self.runs("work") == 2, "the second work run")

    def test_any_other_body_is_refused_before_anything_starts(self):
        for payload in ({}, {"realm": "hoa"}, {"realm": "elsewhere"}, {"realm": "work", "repo": "/elsewhere"}):
            status, _, body = self.post("/api/tasks/sweep", payload)
            self.assertEqual(status, 400, (payload, body))
        self.assertEqual((self.runs("work"), self.runs("personal")), (0, 0))

    def test_before_any_run_each_realm_reads_never(self):
        doc = self.read("/api/tasks")["sweeps"]
        self.assertEqual({r: (w["running"], w["last"]["state"]) for r, w in doc.items()},
                         {"work": (False, "never"), "personal": (False, "never")})


if __name__ == "__main__":
    unittest.main()
