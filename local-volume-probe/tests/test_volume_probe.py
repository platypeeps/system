"""volume-probe.sh against temp paths and a temp config dir.

A read that waits is made real, not stubbed: opening a FIFO for reading
blocks in open() until a writer appears, which is the shape of the sd:2537
stall, where launchd jobs waited in open() on the external volume. Every
test points SYSTEM_TOOLS_CONFIG at a temp dir, so nothing reads the real
config, and PATH holds a launchctl stub that fails the test if called.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "volume-probe.sh"


class VolumeProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.config = self.tmp / "config"
        self.bin = self.tmp / "bin"
        self.volume = self.tmp / "volume"
        for d in (self.config, self.bin, self.volume):
            d.mkdir()
        self.touched = self.tmp / "launchctl-called"
        stub = self.bin / "launchctl"
        stub.write_text(f"#!/bin/sh\ntouch '{self.touched}'\nexit 1\n")
        stub.chmod(0o755)

    def tearDown(self):
        self.assertFalse(self.touched.exists(), "volume-probe.sh called launchctl")

    def env_file(self, text):
        d = self.config / "volume-probe"
        d.mkdir(exist_ok=True)
        (d / ".env").write_text(text)

    def run_vp(self, *args, extra=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp),
               "TMPDIR": str(self.tmp), "SYSTEM_TOOLS_CONFIG": str(self.config)}
        env.update(extra or {})
        return subprocess.run(["sh", str(ENTRYPOINT), *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def readable(self):
        (self.volume / "dir").mkdir(exist_ok=True)
        (self.volume / "file").write_text("x")
        return f"{self.volume}/dir:{self.volume}/file"

    def test_a_read_that_waits_past_the_bound_exits_1_naming_the_path(self):
        fifo = self.volume / "Backup Local" / "waits"
        fifo.parent.mkdir()
        os.mkfifo(fifo)
        self.env_file(f'VOLUME_PROBE_PATHS="{self.readable()}:{fifo}"\nVOLUME_PROBE_TIMEOUT=1\n')
        started = time.monotonic()
        done = self.run_vp("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn(f"blocked: {fifo} still waiting after 1s", done.stdout)
        self.assertNotIn("survived KILL", done.stdout)
        self.assertEqual("", done.stderr)
        self.assertLess(time.monotonic() - started, 15)

    def test_unconfigured_exits_3(self):
        done = self.run_vp("status")
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertIn("not configured (VOLUME_PROBE_PATHS is not set)", done.stdout)

    def test_paths_that_answer_exit_0(self):
        self.env_file(f'VOLUME_PROBE_PATHS="{self.readable()}"\n')
        done = self.run_vp("status")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual("volume-probe: ok: 2 path(s) answered within 5s\n", done.stdout)

    def test_an_exported_value_counts_without_a_config_file(self):
        done = self.run_vp("status", extra={"VOLUME_PROBE_PATHS": self.readable()})
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def test_a_missing_path_exits_1_naming_it(self):
        gone = self.volume / "gone"
        self.env_file(f'VOLUME_PROBE_PATHS="{gone}"\n')
        done = self.run_vp("status")
        self.assertEqual(1, done.returncode)
        self.assertIn(f"missing: {gone} does not exist", done.stdout)

    def test_a_refused_read_exits_1_naming_it(self):
        reader = self.bin / "refuse"
        reader.write_text("#!/bin/sh\necho \"$1: Operation not permitted\" >&2\nexit 1\n")
        reader.chmod(0o755)
        self.env_file(f'VOLUME_PROBE_PATHS="{self.readable()}"\nVOLUME_PROBE_READER="{reader}"\n')
        done = self.run_vp("status")
        self.assertEqual(1, done.returncode)
        self.assertIn(f"refused: {self.volume}/dir (exit 1: {self.volume}/dir: Operation not permitted)",
                      done.stdout)

    def test_check_prints_one_line_per_path(self):
        self.env_file(f'VOLUME_PROBE_PATHS="{self.readable()}"\n')
        done = self.run_vp("check")
        self.assertEqual(0, done.returncode)
        self.assertEqual([f"ok: {self.volume}/dir", f"ok: {self.volume}/file",
                          "volume-probe: ok: 2 path(s) answered within 5s"],
                         done.stdout.splitlines())

    def test_a_bad_timeout_is_broken_not_unconfigured(self):
        for value in ("0", "61", "5s"):
            with self.subTest(value=value):
                extra = {"VOLUME_PROBE_PATHS": self.readable(), "VOLUME_PROBE_TIMEOUT": value}
                done = self.run_vp("status", extra=extra)
                self.assertEqual(1, done.returncode)
                self.assertIn("VOLUME_PROBE_TIMEOUT must be whole seconds from 1 to 60", done.stdout)

    def test_help_declares_the_health_check_codes_and_exits_0(self):
        for flag in ("help", "-h", "--help"):
            done = self.run_vp(flag)
            self.assertEqual(0, done.returncode)
            self.assertIn("local-health-check reads these codes", " ".join(done.stdout.split()))

    def test_no_argument_prints_usage_to_stderr_and_exits_1(self):
        done = self.run_vp()
        self.assertEqual(1, done.returncode)
        self.assertEqual("", done.stdout)
        self.assertIn("Usage: volume-probe.sh", done.stderr)


if __name__ == "__main__":
    unittest.main()
