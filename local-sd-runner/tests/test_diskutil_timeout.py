"""A `diskutil` that times out is a preflight problem, not a crash (sd:970).

The runner's err log ended with six `subprocess.TimeoutExpired` tracebacks
raised out of `storage.preflight` into `serve`: `diskutil info` and
`diskutil apfs list` each timed out after 20 seconds while the volume was
asleep or Spotlight held it. `preflight` already turns a non-zero exit into a
problem the operator reads; a timeout gets the same problem text, suffixed
with the timeout, and `preflight` returns so `serve` refuses the start the
way it does for every other problem.
"""

from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_runner import storage

CAPACITIES = [{"device": 1, "free": 90e9, "total": 1e12},
              {"device": 2, "free": 60e9, "total": 1e12},
              {"device": 2, "free": 60e9, "total": 1e12}]
INFO = subprocess.CompletedProcess([], 0, plistlib.dumps({"FilesystemType": "apfs", "DeviceIdentifier": "disk9s1"}))


class DiskutilTimeout(unittest.TestCase):
    def preflight(self, runs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sd_runner.storage.sys.platform", "darwin"), \
                    patch("sd_runner.storage.capacity", side_effect=CAPACITIES), \
                    patch("sd_runner.storage.subprocess.run", side_effect=runs) as calls:
                report = storage.preflight(root, root, root)
        return report, calls

    def test_info_timeout_is_the_apfs_problem_with_the_timeout_named(self):
        report, calls = self.preflight([subprocess.TimeoutExpired(["diskutil", "info", "-plist", "/"], 20)])
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"], ["cannot verify work volume is APFS (diskutil timed out after 20s)"])
        self.assertFalse(report["dispatch_allowed"])
        self.assertIsNone(report["quota_bytes"])
        # The inventory is not asked for a volume whose type is unknown.
        self.assertEqual(calls.call_count, 1)

    def test_apfs_list_timeout_is_the_quota_problem_with_the_timeout_named(self):
        report, calls = self.preflight([INFO, subprocess.TimeoutExpired(["diskutil", "apfs", "list", "-plist"], 20)])
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"], ["cannot verify configured APFS capacity quota (diskutil timed out after 20s)"])
        self.assertFalse(report["dispatch_allowed"])
        self.assertIsNone(report["quota_bytes"])
        self.assertEqual(calls.call_count, 2)
