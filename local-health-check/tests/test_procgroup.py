"""The cleanup the suites share: `kill_group` in `tests/procgroup.py`.

REGRESSION (sd:2261). A full gate failed in the cleanup of
`StatusBoundIsValidated`: `os.killpg` raised `PermissionError`. The script had
exited between the last poll and the kill, and on macOS `killpg` answers EPERM,
not ESRCH, while the exited leader is an unreaped zombie. The cleanup caught
`ProcessLookupError` only, so a test that had passed was reported as an error.
"""

import errno
import os
import signal
import subprocess
import time
import unittest
from unittest import mock

from .procgroup import kill_group


class GroupAlreadyGone(unittest.TestCase):

    def test_a_zombie_leader_is_no_error(self):
        # The flake's state, made on purpose: the leader has exited and is
        # not reaped yet, so it is a zombie and the group has no live member.
        proc = subprocess.Popen(["sh", "-c", "exit 0"],
                                start_new_session=True)
        try:
            # Wait for the exit without reaping it (WNOWAIT).
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            kill_group(proc.pid)
        finally:
            proc.wait()

    def test_a_reaped_leader_is_no_error(self):
        proc = subprocess.Popen(["sh", "-c", "exit 0"],
                                start_new_session=True)
        proc.wait()
        kill_group(proc.pid)

    def test_eperm_and_esrch_from_killpg_are_swallowed(self):
        # The same claim on any platform: Linux signals a zombie without
        # complaint, so the test above cannot catch a regression there.
        for exc in (PermissionError(errno.EPERM, "Operation not permitted"),
                    ProcessLookupError(errno.ESRCH, "No such process")):
            with self.subTest(errno=exc.errno):
                with mock.patch("os.killpg", side_effect=exc) as killpg:
                    kill_group(12345)
                killpg.assert_called_once_with(12345, signal.SIGKILL)

    def test_any_other_error_is_raised(self):
        with mock.patch("os.killpg",
                        side_effect=OSError(errno.EINVAL, "Invalid argument")):
            with self.assertRaises(OSError) as raised:
                kill_group(12345)
        self.assertEqual(raised.exception.errno, errno.EINVAL)


class LiveGroupIsKilled(unittest.TestCase):

    def test_leader_and_child_both_die(self):
        # The leader prints its child's pid, then both sleep; one SIGKILL to
        # the group must end both, not just the process Popen knows about.
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 60 & echo $!; wait"],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            child = int(proc.stdout.readline())
            kill_group(proc.pid)
            self.assertEqual(proc.wait(timeout=10), -signal.SIGKILL)
            # The child is reparented and reaped by init once it dies.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"child {child} of the group outlived kill_group")
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
