"""satellite-stale.sh against a stub sd-db.sh and a temp config dir.

The alarm's logic is tested in local-sd-db (`tests/test_satellite_stale.py`).
Here the stub records the argv and the variables it was handed, and exits
with the code the test asks for, so each verb's wiring and the convention 6
pass-through are checked without a database.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "satellite-stale.sh"
STUB = """#!/bin/sh
python3 - "$@" <<'PY'
import json, os, sys
keys = ("SD_NOTIFY", "SD_SATELLITE_STALE_HOURS", "SD_SATELLITE_STALE_WINDOW")
with open(os.environ["STUB_RECORD"], "w") as out:
    json.dump({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in keys}}, out)
PY
exit "${STUB_EXIT:-0}"
"""


class SatelliteStaleSh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.config = self.tmp / "config"
        self.config.mkdir()
        self.stub = self.tmp / "sd-db.sh"
        self.stub.write_text(STUB)
        self.record = self.tmp / "record.json"

    def run_sh(self, *args, extra=None):
        env = {"PATH": "/usr/bin:/bin:" + os.path.dirname(shutil.which("python3") or "/usr/bin"),
               "HOME": str(self.tmp), "SYSTEM_TOOLS_CONFIG": str(self.config),
               "SATELLITE_STALE_SD_DB": str(self.stub), "STUB_RECORD": str(self.record)}
        env.update(extra or {})
        return subprocess.run(["sh", str(ENTRYPOINT), *args], env=env, capture_output=True, text=True,
                              timeout=60)

    def recorded(self):
        return json.loads(self.record.read_text())

    def test_help_exits_zero_and_declares_the_status_codes(self):
        for word in ("help", "-h", "--help"):
            done = self.run_sh(word)
            self.assertEqual(done.returncode, 0, word)
            self.assertIn("local-health-check reads these", done.stdout)
        self.assertFalse(self.record.exists(), "help ran the stub")

    def test_no_argument_prints_usage_to_stderr_and_exits_one(self):
        done = self.run_sh()
        self.assertEqual(done.returncode, 1)
        self.assertIn("Usage:", done.stderr)

    def test_an_unknown_command_exits_one(self):
        self.assertEqual(self.run_sh("frobnicate").returncode, 1)

    def test_status_passes_each_code_through(self):
        for code in (0, 1, 3):
            done = self.run_sh("status", extra={"STUB_EXIT": str(code)})
            self.assertEqual(done.returncode, code)
            self.assertEqual(self.recorded()["argv"], ["satellite-stale", "status"])

    def test_check_sends_nothing(self):
        self.assertEqual(self.run_sh("check").returncode, 0)
        self.assertEqual(self.recorded()["argv"], ["satellite-stale"])

    def test_run_notifies_through_local_notify(self):
        self.assertEqual(self.run_sh("run").returncode, 0)
        record = self.recorded()
        self.assertEqual(record["argv"], ["satellite-stale", "--notify"])
        notifier = Path(record["env"]["SD_NOTIFY"])
        self.assertEqual(notifier.resolve(), (FOLDER.parent / "local-notify" / "notify.sh").resolve())
        self.assertTrue(notifier.is_file())

    def test_an_exported_notifier_wins(self):
        self.run_sh("run", extra={"SD_NOTIFY": "/somewhere/else.sh"})
        self.assertEqual(self.recorded()["env"]["SD_NOTIFY"], "/somewhere/else.sh")

    def test_the_config_file_reaches_the_verb(self):
        folder = self.config / "satellite-stale"
        folder.mkdir()
        (folder / ".env").write_text("SD_SATELLITE_STALE_HOURS=5\nSD_SATELLITE_STALE_WINDOW=8-20\n")
        self.run_sh("status")
        self.assertEqual(self.recorded()["env"]["SD_SATELLITE_STALE_HOURS"], "5")
        self.assertEqual(self.recorded()["env"]["SD_SATELLITE_STALE_WINDOW"], "8-20")

    def test_a_missing_config_file_is_fine(self):
        self.assertEqual(self.run_sh("status").returncode, 0)

    def test_the_example_holds_no_live_value(self):
        for line in (FOLDER / ".env.example").read_text().splitlines():
            self.assertTrue(line == "" or line.startswith("#"), line)


if __name__ == "__main__":
    unittest.main()
