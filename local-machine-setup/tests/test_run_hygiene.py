"""Three ways a run ended wrongly or silently (sd:3545).

decommission --apply uninstalls the cron jobs, then read the profile with a
call that dies: no sign-off list, no word why. Five verbs removed their
temporary folder on TERM and returned into the run, so a cron limit left
`capture --apply` writing manifests from empty bodies. A flag nobody matched
fell into the catch-all arm, so `status --fail-on-drft` never failed.

Every case runs a copy of this folder under a temporary root, with stubs on
PATH; nothing reads or changes this Mac.
"""

import os
import pathlib
import pty
import select
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

# Records its pid, then waits for the signal. The test signals the shell, then
# this pid: a signal to the whole group would also kill the pipeline's other
# commands, whose exit 143 would pass for the trap's.
HANG_STUB = """#!/bin/sh
[ ! -e "$HANG_STARTED" ] || exit 0
echo $$ > "$HANG_STARTED.tmp"
mv "$HANG_STARTED.tmp" "$HANG_STARTED"
exec sleep 60
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class RunHygiene(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        repo = self.base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config = fixture_config.copy_capture_config(self.base)
        self.profiles = self.config / "machine-setup/profiles"
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        self.started = self.base / "hang-started"
        fixture_config.seal(self, self.stubs)

    def env(self, **extra):
        return {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "HANG_STARTED": str(self.started),
            **extra,
        }

    def run_verb(self, *args, **extra):
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args],
                              env=self.env(**extra), capture_output=True, text=True,
                              cwd=self.tmp.name, stdin=subprocess.DEVNULL, timeout=60)

    # -------------------------------------------------------- decommission ----

    def decommission(self, **extra):
        """`decommission --apply` on a terminal, typing the confirmation."""
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.tmp.name)
            script = str(self.folder / "machine-setup.sh")
            os.execve(script, [script, "decommission", "--apply"], self.env(**extra))
        out = b""
        sent = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 1)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and b'type "decommission"' in out:
                os.write(fd, b"decommission\n")
                sent = True
        _, status = os.waitpid(pid, 0)
        return out.decode(errors="replace").replace("\r\n", "\n"), os.waitstatus_to_exitcode(status)

    def assert_agents_skipped(self, out, rc):
        # The cron step ran (its script is absent from the copy, so it failed),
        # and the run still reached the list the operator signs off.
        self.assertIn("== cron jobs", out)
        self.assertIn("  FAILED  launch agents not removed: no usable profile", out)
        self.assertIn("== manual sign-offs and exports", out)
        self.assertEqual(rc, 1, out)

    def test_decommission_without_a_recorded_profile_says_so_and_prints_the_sign_offs(self):
        out, rc = self.decommission()
        self.assert_agents_skipped(out, rc)

    def test_decommission_with_an_unknown_recorded_profile_skips_the_agents(self):
        (self.state / "profile").write_text("no-such-profile\n")
        out, rc = self.decommission()
        self.assert_agents_skipped(out, rc)
        self.assertIn("no-such-profile", out)

    def test_decommission_without_a_profile_directory_skips_the_agents(self):
        (self.state / "profile").write_text("personal\n")
        out, rc = self.decommission(MACHINE_SETUP_PROFILE_DIR=str(self.base / "no-such-dir"))
        self.assert_agents_skipped(out, rc)

    # ------------------------------------------------------------- traps ----

    def signal_during(self, signum, stub, *args, **extra):
        """Run the verb; once `stub` hangs, send `signum` to the shell and then
        the stub, so the shell can act on it. Returns the exit code and output."""
        write_exec(self.stubs / stub, HANG_STUB)
        process = subprocess.Popen(
            [str(self.folder / "machine-setup.sh"), *args],
            env=self.env(MACHINE_SETUP_PROFILE="personal", **extra), cwd=self.tmp.name,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
            # A shell cannot trap a signal that was ignored when it started,
            # and a harness may start this test with INT ignored.
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        deadline = time.monotonic() + 30
        while not self.started.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(self.started.exists(), f"{stub} never started")
        stub_pid = int(self.started.read_text())
        os.kill(process.pid, signum)
        # The same signal ends the stub: bash runs an INT trap only when the
        # command it waits on died of INT too.
        os.kill(stub_pid, signum)
        stdout, stderr = process.communicate(timeout=30)
        return process.returncode, stdout + stderr

    def term_during(self, stub, *args, **extra):
        return self.signal_during(signal.SIGTERM, stub, *args, **extra)

    def test_a_term_ends_status(self):
        rc, out = self.term_during("brew", "status")
        self.assertEqual(rc, 143, out)

    def test_a_term_ends_compare(self):
        rc, out = self.term_during("comm", "compare", "personal", "work")
        self.assertEqual(rc, 143, out)

    def test_a_term_ends_triage(self):
        rc, out = self.term_during("brew", "triage", "brew")
        self.assertEqual(rc, 143, out)

    def test_a_term_ends_apps(self):
        rc, out = self.term_during("brew", "apps")
        self.assertEqual(rc, 143, out)

    def test_a_term_ends_capture_before_it_writes_a_manifest(self):
        before = {p.name: p.read_text() for p in self.profiles.iterdir()}
        rc, out = self.term_during("brew", "capture", "--apply")
        self.assertEqual(rc, 143, out)
        self.assertEqual({p.name: p.read_text() for p in self.profiles.iterdir()}, before)

    def test_an_int_ends_status_too(self):
        rc, out = self.signal_during(signal.SIGINT, "brew", "status")
        self.assertEqual(rc, 130, out)

    # ------------------------------------------------------------- flags ----

    def test_an_unknown_option_is_refused_and_named(self):
        for flag in ("--fail-on-drft", "--bogus"):
            result = self.run_verb("status", flag, MACHINE_SETUP_PROFILE="personal")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn(f"unknown option {flag}", result.stderr)
            self.assertNotIn("profile :", result.stdout, "the verb ran")

    def test_a_known_option_is_still_accepted(self):
        result = self.run_verb("profiles", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_test_verb_still_passes_its_own_options_on(self):
        result = self.run_verb("test", "--failfast")
        self.assertNotIn("unknown option", result.stderr)


if __name__ == "__main__":
    unittest.main()
