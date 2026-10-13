"""`status` names a missing scheduled wake as drift (sd:3541).

A job in the 02:xx slot runs only if the Mac wakes for it
(.claude/rules/cron-wake.md). `pmset` is a stub on PATH that prints a
fixture schedule, so no run reads this Mac's.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"

WAKE = "Repeating power events:\n  wakepoweron at 2:40AM every day\n"
# A one-off wake is not the nightly one.
ONE_OFF = "Scheduled power events:\n [0]  wake at 10/13/2026 02:40:00 by 'pmset'\n"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class WakeScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.folder = base / "repo" / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, base / "repo" / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = base / "stubs"
        self.sched = base / "sched"
        write_exec(self.stubs / "pmset", f'#!/bin/sh\n[ "$*" = "-g sched" ] || exit 2\ncat "{self.sched}"\n')
        write_exec(self.stubs / "brew", '#!/bin/sh\n[ "$1" != --prefix ] || echo /usr/local\nexit 0\n')
        for name in ("defaults", "sudo", "launchctl", "mas"):
            write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")
        fixture_config.seal(self, self.stubs)

    def status(self, schedule):
        if schedule is None:
            write_exec(self.stubs / "pmset", "#!/bin/sh\nexit 1\n")
        else:
            self.sched.write_text(schedule)
        env = {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
               "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
               **fixture_config.env(self.config_root)}
        run = subprocess.run([str(self.folder / "machine-setup.sh"), "status"], env=env,
                             capture_output=True, text=True, cwd=self.tmp.name,
                             stdin=subprocess.DEVNULL, timeout=120)
        self.assertIn("drift   : ", run.stdout, run.stdout + run.stderr)
        return run.stdout, int(run.stdout.split("drift   : ")[1].split()[0])

    def test_a_missing_nightly_wake_is_drift(self):
        out, drift = self.status(ONE_OFF)
        clean_out, clean = self.status(WAKE)
        self.assertIn("wake    : MISSING (sudo pmset repeat wakeorpoweron MTWRFSU 02:40:00)", out)
        self.assertNotIn("wake    :", clean_out)
        self.assertEqual(drift - clean, 1)

    def test_an_unreadable_schedule_is_unknown_not_clean(self):
        _, clean = self.status(WAKE)
        out, drift = self.status(None)
        self.assertIn("wake    : UNKNOWN (pmset -g sched failed)", out)
        self.assertEqual(drift - clean, 1)


if __name__ == "__main__":
    unittest.main()
