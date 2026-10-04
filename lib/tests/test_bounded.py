"""lib/bounded.sh: a step that hangs ends at its bound and names itself (sd:2660)."""

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]


def shell(script, **kwargs):
    return subprocess.run(["/bin/sh", "-c", f'. "{LIB}/bounded.sh"; {script}'],
                          capture_output=True, text=True, check=False, timeout=60, **kwargs)


def gone(pid, within=5.0):
    """True once no process has `pid`, waiting up to `within` seconds."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class Bounded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_command_in_time_keeps_its_output_and_code(self):
        result = shell("st_bounded 10 sh -c 'echo out; echo err >&2; exit 3'")

        self.assertEqual(result.returncode, 3)
        self.assertEqual((result.stdout, result.stderr), ("out\n", "err\n"))

    def test_an_expired_bound_exits_124_and_names_the_command(self):
        started = time.monotonic()
        result = shell("st_bounded 1 sleep 30")

        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stderr, "timed out after 1s: sleep 30\n")
        self.assertLess(time.monotonic() - started, 10)

    def test_a_command_that_ignores_term_is_killed_after_the_grace(self):
        started = time.monotonic()
        result = shell("ST_BOUNDED_GRACE=1 st_bounded 1 sh -c 'trap \"\" TERM; sleep 30'")

        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started, 10)

    def test_the_bound_ends_the_command_s_children_too(self):
        pidfile = self.dir / "child.pid"
        started = time.monotonic()
        result = shell(f"st_bounded 1 sh -c 'sleep 30 & echo $! > \"{pidfile}\"; wait'")

        self.assertEqual(result.returncode, 124)
        self.assertTrue(gone(int(pidfile.read_text())), "the child outlived the bound")
        # Ended by the TERM, not by the KILL ten seconds later.
        self.assertLess(time.monotonic() - started, 8)

    def test_a_term_to_the_caller_reaches_the_command(self):
        """The job's own limit TERMs the job's group; the bounded command is in
        a group of its own, so the TERM has to be passed on."""
        pidfile = self.dir / "child.pid"
        process = subprocess.Popen(
            ["/bin/sh", "-c", f'. "{LIB}/bounded.sh"; '
                              f"st_bounded 60 sh -c 'echo $$ > \"{pidfile}\"; exec sleep 60'"],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        deadline = time.monotonic() + 10
        while not pidfile.exists() or not pidfile.read_text().strip():
            self.assertLess(time.monotonic(), deadline, "the command never started")
            time.sleep(0.05)
        child = int(pidfile.read_text())
        self.assertNotEqual(os.getpgid(child), process.pid)

        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=20)

        self.assertTrue(gone(child), "the bounded command outlived the TERM")


class Step(unittest.TestCase):
    def test_the_step_is_logged_before_it_starts(self):
        result = shell("st_step 10 echo running 2>&1")

        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 0)
        self.assertRegex(lines[0], r"^\[step \d\d:\d\d:\d\d\] echo running \(bound 10s\)$")
        self.assertEqual(lines[1:], ["running"])

    def test_fd3_takes_the_start_line_and_a_failure_goes_to_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            result = shell(f'exec 3>"{log}"; ST_STEP_FD3=1; st_step 1 sleep 30')
            logged = log.read_text().splitlines()

        self.assertEqual(result.returncode, 124)
        self.assertEqual(len(logged), 2, logged)
        self.assertRegex(logged[0], r"\] sleep 30 \(bound 1s\)$")
        self.assertRegex(logged[1], r"\] sleep 30 exited 124$")
        self.assertEqual(result.stderr.splitlines()[0], "timed out after 1s: sleep 30")
        self.assertRegex(result.stderr.splitlines()[1], r"\] sleep 30 exited 124$")


if __name__ == "__main__":
    unittest.main()
