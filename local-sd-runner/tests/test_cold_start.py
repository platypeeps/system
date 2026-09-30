"""A cold start waits out a `diskutil` with no answer, for a bounded window (sd:1950).

`serve`'s first preflight has no remembered answer (sd:1941), so a
`diskutil` that timed out under load refused the start, and launchd's
KeepAlive relaunched the daemon every 30 s into the same timeout: on
2026-09-28 no heartbeat was written for over five minutes. No answer (a
timeout or a non-zero exit) is now "not yet verified": `serve` writes an
unhealthy heartbeat naming the problem, dispatches nothing, and asks again
each interval until COLD_START_WINDOW has passed. A definitive answer (not
APFS, no quota) still refuses at once.
"""

from __future__ import annotations

import itertools
import plistlib
import subprocess
import unittest
from unittest.mock import patch

from sd_runner import runtime, storage
from sd_db import runner as store
from tests import test_runtime

CAPACITIES = [{"device": 1, "free": 90e9, "total": 1e12},
              {"device": 2, "free": 60e9, "total": 1e12},
              {"device": 2, "free": 60e9, "total": 1e12}]
INFO = subprocess.CompletedProcess([], 0, plistlib.dumps({"FilesystemType": "apfs", "DeviceIdentifier": "disk9s1"}))
HFS = subprocess.CompletedProcess([], 0, plistlib.dumps({"FilesystemType": "hfs", "DeviceIdentifier": "disk9s1"}))
FAILED = subprocess.CompletedProcess([], 1, b"")
TIMED_OUT = "cannot verify work volume is APFS (diskutil timed out after 20s)"


def inventory(quota):
    return subprocess.CompletedProcess([], 0, plistlib.dumps({"Containers": [{"Volumes": [{"DeviceIdentifier": "disk9s1", "CapacityQuota": quota}]}]}))


class LoopFinished(Exception):
    pass


class ColdStart(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.runner.config.work.mkdir()
        self.runner.config.retention.mkdir()
        self.now = 1000.0
        self.sleeps = []
        self.heartbeats = []

    def serve(self, answers, *, until=None):
        """`serve` against `diskutil` answers; `until` ends the loop at the first tick."""
        def pause(seconds):
            self.sleeps.append(seconds)
            self.heartbeats.append(store.heartbeat_state(self.fixture.db))
            self.now += 60

        with patch("sd_runner.storage.sys.platform", "darwin"), \
                patch("sd_runner.storage.capacity", side_effect=itertools.cycle(CAPACITIES)), \
                patch("sd_runner.storage._diskutil", side_effect=answers) as diskutil, \
                patch.object(runtime.time, "monotonic", side_effect=lambda: self.now), \
                patch.object(runtime.time, "sleep", side_effect=pause), \
                patch.object(self.runner, "recover", return_value=[]) as recover, \
                patch.object(self.runner, "tick", side_effect=LoopFinished) as tick:
            try:
                self.runner.serve()
            finally:
                self.diskutil, self.recover, self.tick = diskutil, recover, tick

    def test_no_answer_waits_with_an_unhealthy_heartbeat_then_starts(self):
        with self.assertRaises(LoopFinished):
            self.serve([None, FAILED, INFO, inventory(60_000_000_000)])
        self.assertEqual(self.sleeps, [self.runner.config.interval] * 2)
        first, second = self.heartbeats
        self.assertIs(first["healthy"], False)
        self.assertEqual(first["storage"]["problems"], [TIMED_OUT])
        self.assertIs(first["storage"]["dispatch_allowed"], False)
        self.assertEqual(first["pid"], runtime.os.getpid())
        self.assertEqual(first["starting"]["window_seconds"], runtime.COLD_START_WINDOW)
        # A non-zero exit is no answer either, and is retried the same way.
        self.assertEqual(second["storage"]["problems"], ["cannot verify work volume is APFS"])
        self.assertIs(second["healthy"], False)
        self.recover.assert_called_once()
        self.tick.assert_called_once()
        # The answer that came is remembered, as a first preflight's is (sd:1941).
        self.assertEqual(len(self.runner.storage_verified), 1)

    def test_no_answer_for_the_whole_window_refuses(self):
        with self.assertRaisesRegex(store.RunnerRefused, "diskutil timed out after 20s"):
            self.serve(itertools.repeat(None))
        self.assertEqual(len(self.sleeps), runtime.COLD_START_WINDOW // 60)
        self.assertTrue(all(beat["healthy"] is False for beat in self.heartbeats))
        self.recover.assert_not_called()
        self.tick.assert_not_called()

    def test_a_definitive_answer_refuses_at_once(self):
        for answers, problem in (([HFS], "work volume must be APFS"),
                                 ([INFO, inventory(0)], "APFS CapacityQuota must be configured")):
            with self.subTest(problem=problem):
                self.sleeps.clear()
                with self.assertRaisesRegex(store.RunnerRefused, problem):
                    self.serve(answers)
                self.assertEqual(self.sleeps, [])
                self.recover.assert_not_called()
                self.assertEqual(store.heartbeat_state(self.fixture.db)["reason"], "runner has never reported a heartbeat")

    def test_a_definitive_answer_while_waiting_refuses(self):
        with self.assertRaisesRegex(store.RunnerRefused, "work volume must be APFS"):
            self.serve([None, HFS])
        self.assertEqual(len(self.sleeps), 1)
        self.recover.assert_not_called()

    def test_a_definitive_problem_beside_no_answer_refuses_at_once(self):
        report = {"ok": False, "problems": ["worktrees and retained clones must share one device for atomic rename", TIMED_OUT],
                  "unverified": [TIMED_OUT], "dispatch_allowed": False, "database_below_floor": False}
        with patch.object(runtime.storage, "preflight", return_value=report), \
                patch.object(runtime.time, "sleep") as sleep, \
                self.assertRaisesRegex(store.RunnerRefused, "atomic rename"):
            self.runner.serve()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
