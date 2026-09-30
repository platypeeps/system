"""`capture` refreshes the values in <profile>.macos and keeps a key this
machine has never set (sd:1432).

`defaults read` exits 1 for a key that is unset. capture_macos read each key
in a plain assignment under `set -e`, so the first unset key ended the whole
capture: no summary, no manifest written, and the stages after it not run.

capture writes into the checkout it runs from, so it runs from a copy of this
folder under a temporary root that is not a git checkout. brew, git and
defaults are stubs, so nothing on this machine is read.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent

MANIFEST = (
    "# Fixture manifest.\n"
    "com.example.fixture SetKey int 1\n"
    "com.example.fixture UnsetKey string before\n"
    "com.example.fixture Flag bool false\n"
)

# What real `defaults` does: print a set value and exit 0; for an unset key,
# complain on stderr and exit 1. Every other read is unset too.
DEFAULTS_STUB = """#!/bin/sh
case "$1 $2 $3" in
  "read com.example.fixture SetKey") echo 42; exit 0 ;;
  "read com.example.fixture Flag") echo 1; exit 0 ;;
esac
echo "The domain/default pair of ($2, $3) does not exist" >&2
exit 1
"""


def write_stub(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class CaptureMacosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.folder = base / "repo/local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        # capture lists host cron jobs through lib/, and stops when it cannot.
        shutil.copytree(FOLDER.parent / "lib", self.folder.parent / "lib",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        self.manifest = self.profiles / "personal.macos"
        self.manifest.write_text(MANIFEST)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        write_stub(self.stubs, "brew", "#!/bin/sh\nexit 0\n")
        write_stub(self.stubs, "git", "#!/bin/sh\nexit 1\n")
        write_stub(self.stubs, "defaults", DEFAULTS_STUB)

    def capture(self, *flags):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "capture", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_an_unset_key_is_kept_and_capture_goes_on(self):
        out = self.capture("--apply")
        self.assertIn("    keep    com.example.fixture UnsetKey (unset on this machine)\n", out)
        self.assertIn("    refreshed 2 value(s)\n", out)
        # The stages after macos still ran.
        self.assertIn("  services are not auto-captured", out)
        self.assertEqual(self.manifest.read_text(),
                         "# Fixture manifest.\n"
                         "com.example.fixture SetKey int 42\n"
                         "com.example.fixture UnsetKey string before\n"
                         "com.example.fixture Flag bool true\n")

    def test_a_dry_run_writes_nothing(self):
        out = self.capture()
        self.assertIn("    keep    com.example.fixture UnsetKey (unset on this machine)\n", out)
        self.assertEqual(self.manifest.read_text(), MANIFEST)


if __name__ == "__main__":
    unittest.main()
