"""A verified work volume is trusted between `diskutil` calls (sd:1941).

`preflight` asked `diskutil info` and `diskutil apfs list` on every 10 s
pulse, about 17,000 calls a day, and a `diskutil` that stalled past its
timeout dropped `dispatch_allowed` for that pulse: `sd runner status` on
2026-09-28 read "cannot verify work volume is APFS (diskutil timed out
after 20s)" while a parallel test run loaded the machine. With the
runtime's `verified` dict, a mount identity answered once is repeated for
VERIFY_INTERVAL seconds without asking; a refresh with no answer keeps the
last answer and names the failure; an answer that names a problem replaces
the cache; and an answer VERIFY_STALE seconds old with no successful
refresh is forgotten. Without the dict nothing changes: `serve`'s first
preflight and the CLI verbs ask every time (tests/test_diskutil_timeout.py).
"""

from __future__ import annotations

import itertools
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_runner import runtime, storage
from tests import test_runtime

CAPACITIES = [{"device": 1, "free": 90e9, "total": 1e12},
              {"device": 2, "free": 60e9, "total": 1e12},
              {"device": 2, "free": 60e9, "total": 1e12}]
INFO = subprocess.CompletedProcess([], 0, plistlib.dumps({"FilesystemType": "apfs", "DeviceIdentifier": "disk9s1"}))
TIMEOUT = subprocess.TimeoutExpired(["diskutil", "info", "-plist", "/"], 20)


def inventory(quota):
    return subprocess.CompletedProcess([], 0, plistlib.dumps({"Containers": [{"Volumes": [{"DeviceIdentifier": "disk9s1", "CapacityQuota": quota}]}]}))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class VerifiedVolume(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = Clock()
        self.verified = {}

    def preflight(self, runs, *, verified="cache"):
        with patch("sd_runner.storage.sys.platform", "darwin"), \
                patch("sd_runner.storage.capacity", side_effect=itertools.cycle(CAPACITIES)), \
                patch("sd_runner.storage.subprocess.run", side_effect=runs) as calls:
            report = storage.preflight(self.root, self.root, self.root,
                                       verified=self.verified if verified == "cache" else verified, clock=self.clock)
        return report, calls.call_count

    def test_an_answer_is_repeated_without_diskutil_until_the_interval(self):
        first, asked = self.preflight([INFO, inventory(60_000_000_000)])
        self.assertEqual((first["ok"], first["dispatch_allowed"], first["quota_bytes"], asked), (True, True, 60_000_000_000, 2))
        self.assertEqual(first["verification"], {"cached": False, "age_seconds": 0.0, "refresh_problem": None})
        self.clock.now += storage.VERIFY_INTERVAL - 1
        second, asked = self.preflight([AssertionError("a repeated answer asks diskutil nothing")])
        self.assertEqual((second["ok"], second["dispatch_allowed"], second["quota_bytes"], asked), (True, True, 60_000_000_000, 0))
        self.assertEqual(second["verification"], {"cached": True, "age_seconds": storage.VERIFY_INTERVAL - 1, "refresh_problem": None})
        self.clock.now += 1
        third, asked = self.preflight([INFO, inventory(60_000_000_000)])
        self.assertEqual((third["ok"], asked, third["verification"]["cached"]), (True, 2, False))

    def test_a_refresh_with_no_answer_keeps_the_last_answer_and_names_the_failure(self):
        self.preflight([INFO, inventory(60_000_000_000)])
        self.clock.now += storage.VERIFY_INTERVAL
        report, asked = self.preflight([TIMEOUT])
        self.assertEqual((report["ok"], report["dispatch_allowed"], report["problems"], report["quota_bytes"], asked),
                         (True, True, [], 60_000_000_000, 1))
        self.assertEqual(report["verification"], {"cached": True, "age_seconds": float(storage.VERIFY_INTERVAL),
                                                  "refresh_problem": "cannot verify work volume is APFS (diskutil timed out after 20s)"})
        # The failed refresh is not asked again before the next interval, and stays named.
        self.clock.now += 1
        repeated, asked = self.preflight([AssertionError("asked again inside the interval")])
        self.assertEqual((repeated["ok"], asked, repeated["verification"]["refresh_problem"]),
                         (True, 0, "cannot verify work volume is APFS (diskutil timed out after 20s)"))
        # A refresh that answers clears the failure.
        self.clock.now += storage.VERIFY_INTERVAL
        cleared, asked = self.preflight([INFO, inventory(60_000_000_000)])
        self.assertEqual((cleared["ok"], asked, cleared["verification"]), (True, 2, {"cached": False, "age_seconds": 0.0, "refresh_problem": None}))

    def test_a_refresh_that_names_a_problem_replaces_the_answer(self):
        self.preflight([INFO, inventory(60_000_000_000)])
        self.clock.now += storage.VERIFY_INTERVAL
        report, asked = self.preflight([INFO, inventory(0)])
        self.assertEqual((report["ok"], report["dispatch_allowed"], asked), (False, False, 2))
        self.assertEqual(report["problems"], ["APFS CapacityQuota must be configured and exceed 40 GB; observed 0"])
        self.assertEqual(report["verification"], {"cached": False, "age_seconds": None, "refresh_problem": None})
        self.assertEqual(self.verified, {}, "a problem is not remembered; the next pulse asks again")
        after, asked = self.preflight([TIMEOUT])
        self.assertEqual((after["ok"], after["problems"], asked),
                         (False, ["cannot verify work volume is APFS (diskutil timed out after 20s)"], 1))

    def test_an_answer_with_no_successful_refresh_is_forgotten_when_stale(self):
        self.preflight([INFO, inventory(60_000_000_000)])
        for _ in range(storage.VERIFY_STALE // storage.VERIFY_INTERVAL - 1):
            self.clock.now += storage.VERIFY_INTERVAL
            held, _ = self.preflight([TIMEOUT])
            self.assertTrue(held["ok"], held)
        self.clock.now += storage.VERIFY_INTERVAL
        report, asked = self.preflight([TIMEOUT])
        self.assertEqual((report["ok"], report["dispatch_allowed"], asked), (False, False, 1))
        self.assertEqual(report["problems"], ["cannot verify work volume is APFS (diskutil timed out after 20s)"])
        self.assertEqual(self.verified, {})

    def test_a_different_mount_identity_is_its_own_answer(self):
        self.preflight([INFO, inventory(60_000_000_000)])
        (identity,) = self.verified
        self.verified[(identity[0], 3)] = self.verified.pop(identity)
        report, asked = self.preflight([INFO, inventory(60_000_000_000)])
        self.assertEqual((report["ok"], asked, report["verification"]["cached"]), (True, 2, False))
        self.assertIn(identity, self.verified)

    def test_without_a_dict_every_call_asks(self):
        first, asked = self.preflight([INFO, inventory(60_000_000_000)], verified=None)
        second, again = self.preflight([INFO, inventory(60_000_000_000)], verified=None)
        self.assertEqual((first["ok"], second["ok"], asked, again), (True, True, 2, 2))
        self.assertEqual(second["verification"], {"cached": False, "age_seconds": 0.0, "refresh_problem": None})
        self.assertEqual(self.verified, {})


class RuntimeCache(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner

    def test_pulse_and_serve_pass_the_runtime_dict(self):
        report = {"ok": True, "problems": [], "database_below_floor": False, "dispatch_allowed": False}
        with patch.object(runtime.storage, "preflight", return_value=report) as preflight, \
                patch.object(runtime.gitops, "head", return_value="fixture-head"), \
                patch.object(self.runner, "refresh_archives"), patch.object(self.runner, "watch_deliveries"):
            self.runner.pulse(self.fixture.db)
        self.assertIs(preflight.call_args.kwargs["verified"], self.runner.storage_verified)
        with patch.object(runtime.storage, "preflight", return_value={**report, "ok": False, "problems": ["fixture refusal"]}) as preflight, \
                self.assertRaisesRegex(runtime.store.RunnerRefused, "fixture refusal"):
            self.runner.serve(once=True)
        self.assertIs(preflight.call_args.kwargs["verified"], self.runner.storage_verified)
        self.assertEqual(self.runner.storage_verified, {}, "the fake answered nothing to remember")
