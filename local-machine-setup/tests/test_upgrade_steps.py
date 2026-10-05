"""A brew or mas step that hangs names itself and ends at its bound (sd:2660).

On some nights macOS stops answering permission checks for launchd jobs, and
a brew call then hangs. upgrade-report captures the sweep's output for its
mail, so the job log said nothing until the job's two-hour limit killed it.

The script runs from a copy of this folder under a temporary root, beside a
copy of lib/ and a stub notify.sh that only logs, so no mail leaves. `brew`
and `mas` are stubs on PATH.
"""

import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# One formula is outdated, so upgrade-report runs the sweep. With BREW_HANG
# set, `brew upgrade` never finishes.
BREW_STUB = r"""#!/bin/sh
echo "$*" >> "$BREW_LOG"
case "$*" in
  "outdated --formula --quiet") echo example-formula ;;
  upgrade) [ -z "$BREW_STARTED" ] || : > "$BREW_STARTED"
           [ -z "$BREW_HANG" ] || exec sleep 60 ;;
esac
exit 0
"""

MAS_STUB = r"""#!/bin/sh
echo "$*" >> "$MAS_LOG"
exit 0
"""

NOTIFY_STUB = r"""#!/bin/sh
printf '%s\n' "$@" > "$NOTIFY_LOG"
exit 0
"""

STEP = r"\[step \d\d:\d\d:\d\d\] {} \(bound {}s\)"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class UpgradeStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        write_exec(repo / "local-notify/notify.sh", NOTIFY_STUB)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        self.brew_log = base / "brew.log"
        self.mas_log = base / "mas.log"
        self.notify_log = base / "notify.log"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "mas", MAS_STUB)
        fixture_config.seal(self, self.stubs)

    def env(self, **extra):
        return {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "BREW_LOG": str(self.brew_log),
            "MAS_LOG": str(self.mas_log),
            "NOTIFY_LOG": str(self.notify_log),
            "ST_BOUNDED_GRACE": "1",
            **extra,
        }

    def run_verb(self, *args, **extra):
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args],
                              env=self.env(**extra), capture_output=True, text=True,
                              cwd=self.tmp.name, stdin=subprocess.DEVNULL, timeout=30)

    def brew_calls(self):
        return self.brew_log.read_text().splitlines() if self.brew_log.exists() else []

    def test_a_hung_step_is_logged_stopped_and_the_sweep_goes_on(self):
        result = self.run_verb("upgrade", "--apply",
                               MACHINE_SETUP_STEP_TIMEOUT="2", BREW_HANG="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stderr, STEP.format("brew upgrade", 2))
        self.assertIn("timed out after 2s: brew upgrade", result.stderr)
        self.assertEqual(self.brew_calls(),
                         ["update", "upgrade", "upgrade --cask", "cleanup --prune=all"])
        self.assertEqual(self.mas_log.read_text(), "upgrade\n")
        self.assertIn("failed steps: brew upgrade", result.stdout)

    def test_each_step_has_its_own_default_bound(self):
        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command, bound in (("brew update", 600), ("brew upgrade", 1800),
                               ("brew upgrade --cask", 1800),
                               ("brew cleanup --prune=all", 600), ("mas upgrade", 1200)):
            self.assertRegex(result.stderr, STEP.format(re.escape(command), bound))

    def test_the_report_names_a_hung_step_in_the_job_log_and_mails_it(self):
        result = self.run_verb("upgrade-report", MACHINE_SETUP_STEP_TIMEOUT="2", BREW_HANG="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # stderr is the job log; the sweep's own output goes into the mail.
        self.assertRegex(result.stderr, STEP.format("brew upgrade", 2))
        mail = self.notify_log.read_text().splitlines()
        self.assertIn("-F", mail)
        self.assertIn("timed out after 2s: brew upgrade", "\n".join(mail))

    def test_a_term_mid_step_exits_without_writing_into_the_removed_temp_dir(self):
        """The job's limit ends the run; the TERM trap used to remove the
        temporary folder and return, and the run then read and wrote into it."""
        started = pathlib.Path(self.tmp.name) / "brew-started"
        # A group of its own, as cron-jobs' run_bounded gives a job: its limit
        # sends TERM to the whole group.
        process = subprocess.Popen(
            [str(self.folder / "machine-setup.sh"), "upgrade-report"],
            env=self.env(BREW_HANG="1", BREW_STARTED=str(started)), cwd=self.tmp.name,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(started.exists(), "brew upgrade never started")

        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)

        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertNotIn("No such file or directory", stdout + stderr)
        self.assertFalse(self.notify_log.exists(), "a killed run mailed a report")

    def test_a_dry_run_runs_no_step(self):
        result = self.run_verb("upgrade")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  [dry-run] brew upgrade --cask", result.stdout)
        self.assertEqual(self.brew_calls(), [])
        self.assertNotIn("[step", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
