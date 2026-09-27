"""lsof warnings about another file system do not hide or invent clone holders."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db.runner import RunnerRefused
from sd_runner import processes

# The warning a Carbon Copy Cloner snapshot left mounted put on every lsof call
# on one machine, 2026-09-13 (sd:759).
SNAPSHOT = (
    "lsof: WARNING: can't stat() apfs file system /private/tmp/16@113EA6DC/c\n"
    "      Output information may be incomplete.\n"
    '      assuming "dev=3200000a" from mount table\n'
)


class FakeLsof(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(suffix=".noindex")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.clone = self.root / "work/clone"
        self.clone.mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        lsof = bin_dir / "lsof"
        lsof.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_LSOF/argv"\n'
                        'cat "$FAKE_LSOF/stdout"\ncat "$FAKE_LSOF/stderr" >&2\nexit "$(cat "$FAKE_LSOF/rc")"\n')
        lsof.chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_LSOF": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def lsof(self, *, stderr: str, stdout: str = "p4242\nf5\np4343\nf6\n", rc: int = 0) -> set[int]:
        (self.root / "stdout").write_text(stdout)
        (self.root / "stderr").write_text(stderr)
        (self.root / "rc").write_text(str(rc))
        held = processes.holders(self.clone)
        self.assertEqual((self.root / "argv").read_text().split("\n")[-2], str(self.clone))
        return held

    def refused(self, **kwargs) -> str:
        with self.assertRaises(RunnerRefused) as caught:
            self.lsof(**kwargs)
        self.assertIn("cannot verify clone holders", str(caught.exception))
        return str(caught.exception)

    def test_quiet_lsof_returns_the_parsed_pids(self):
        self.assertEqual(self.lsof(stderr=""), {4242, 4343})

    def test_warning_about_another_mount_returns_the_parsed_pids(self):
        self.assertEqual(self.lsof(stderr=SNAPSHOT), {4242, 4343})
        self.assertEqual(self.lsof(stderr=SNAPSHOT, stdout="", rc=1), set())

    def test_the_warned_mount_is_never_looked_up(self):
        # A mount lsof could not stat -- a dead network share -- can hang a
        # lookup, outside lsof's timeout.
        lstat, stat = os.lstat, os.stat

        def guarded(real):
            def call(target, *args, **kwargs):
                if not isinstance(target, int) and os.fsdecode(target).startswith("/private/tmp/16@113EA6DC"):
                    raise AssertionError(f"looked up the warned mount: {target}")
                return real(target, *args, **kwargs)
            return call

        with patch("os.lstat", guarded(lstat)), patch("os.stat", guarded(stat)):
            self.assertEqual(self.lsof(stderr=SNAPSHOT), {4242, 4343})

    def test_warnings_about_several_other_mounts_return_the_parsed_pids(self):
        other = "lsof: WARNING: can't stat() smbfs file system /Volumes/share\n      Output information may be incomplete.\n"
        self.assertEqual(self.lsof(stderr=SNAPSHOT + other), {4242, 4343})

    def test_warning_about_the_clones_own_path_refuses(self):
        self.refused(stderr=f"lsof: WARNING: can't stat() apfs file system {self.clone}\n      Output information may be incomplete.\n")

    def test_warning_about_a_mount_holding_the_clone_refuses(self):
        for mount in (self.root / "work", self.root, Path("/")):
            with self.subTest(mount=str(mount)):
                self.refused(stderr=f"lsof: WARNING: can't stat() apfs file system {mount}\n      Output information may be incomplete.\n")

    def test_warning_about_the_resolved_mount_of_an_unresolved_clone_refuses(self):
        # The mount table holds resolved paths; a clone can be named through
        # a symlink, as `/var` is on macOS.
        alias = self.root / "alias"
        alias.symlink_to(self.clone.parent)
        self.clone = alias / "clone"
        self.refused(stderr=f"lsof: WARNING: can't stat() apfs file system {self.root / 'work'}\n      Output information may be incomplete.\n")

    def test_warning_about_a_mount_inside_the_clone_refuses(self):
        self.refused(stderr=f"lsof: WARNING: can't stat() nfs file system {self.clone / 'vendor'}\n      Output information may be incomplete.\n")

    def test_warning_about_the_clones_own_device_refuses(self):
        device = format(self.clone.stat().st_dev, "x")
        self.refused(stderr=(
            "lsof: WARNING: can't stat() apfs file system /private/tmp/16@113EA6DC/c\n"
            "      Output information may be incomplete.\n"
            f'      assuming "dev={device}" from mount table\n'
        ))

    def test_own_volume_warning_beside_an_unrelated_one_refuses(self):
        self.refused(stderr=SNAPSHOT + f"lsof: WARNING: can't stat() apfs file system {self.clone}\n      Output information may be incomplete.\n")

    def test_real_lsof_error_refuses(self):
        message = self.refused(stderr=f"lsof: status error on {self.clone}: No such file or directory\n", stdout="", rc=1)
        self.assertIn("status error", message)

    def test_unrecognised_line_inside_a_warning_refuses(self):
        self.refused(stderr=SNAPSHOT + "      some other detail lsof may add\n")

    def test_other_warning_refuses(self):
        self.refused(stderr="lsof: WARNING: compiled for macOS release 15; this is 26\n")

    def test_unexpected_exit_status_refuses(self):
        self.refused(stderr="", rc=2)

    def test_a_blank_line_between_warnings_refuses(self):
        # sd:1221. A blank line was dropped before the fail-close check, so a
        # stream it could not account for read as two recognised warnings.
        self.refused(stderr=SNAPSHOT + "\n" + SNAPSHOT)

    def test_a_whitespace_line_before_any_warning_refuses(self):
        # The same drop from the other side: with no block open yet, a
        # whitespace-only line attached to nothing and vanished.
        self.refused(stderr="   \n" + SNAPSHOT)


class NativeLsof(unittest.TestCase):
    def test_real_holder_is_found_whatever_else_is_mounted(self):
        with tempfile.TemporaryDirectory(suffix=".noindex") as temporary:
            clone = Path(temporary).resolve()
            holder = subprocess.Popen(
                [sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'w'); print('open', flush=True); time.sleep(60)",
                 str(clone / "held")], stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.wait)
            self.addCleanup(holder.kill)
            self.assertEqual(holder.stdout.readline().strip(), "open")
            holder.stdout.close()
            self.assertIn(holder.pid, processes.holders(clone))


class FakeProc:
    """A /proc whose environ is unreadable (not dumpable) and whose boot time is fixed."""

    BOOTED = 1_790_000_000

    def __call__(self, path):
        class Entry:
            def read_bytes(self):
                raise PermissionError(13, "Permission denied", path)

            def read_text(self):
                return f"cpu 1 2 3\nbtime {FakeProc.BOOTED}\n"

        return Entry()


class UnreadableEnviron(unittest.TestCase):
    SUPERVISED = {"supervisor_start": "linux:boot-a:5000"}

    def marked(self, started, run):
        with patch.object(processes.sys, "platform", "linux"), \
                patch.object(processes, "Path", FakeProc()), \
                patch.object(processes, "start_identity", return_value=started), \
                patch.object(processes.os, "sysconf", return_value=100):
            return processes.marked(4242, "run-1", run)

    def refuses(self, started, run):
        with self.assertRaisesRegex(RunnerRefused, "cannot inspect owned user process 4242"):
            self.marked(started, run)

    def created(self, seconds_after_boot):
        stamp = datetime.fromtimestamp(FakeProc.BOOTED + seconds_after_boot, timezone.utc)
        return {"created_at": stamp.isoformat()}

    def test_process_older_than_the_supervisor_reads_as_unmarked(self):
        # GitHub's Ubuntu runner: a same-user process from boot, not dumpable.
        self.assertFalse(self.marked("linux:boot-a:120", self.SUPERVISED))

    def test_process_younger_than_the_supervisor_still_refuses(self):
        self.refuses("linux:boot-a:5001", self.SUPERVISED)

    def test_without_a_supervisor_the_run_row_creation_decides(self):
        # 120 ticks at 100 Hz is 1.2 s after boot.
        self.assertFalse(self.marked("linux:boot-a:120", self.created(60)))
        self.refuses("linux:boot-a:120", self.created(3))
        self.refuses("linux:boot-a:6000", self.created(60))

    def test_other_boot_unknown_start_or_no_reference_still_refuses(self):
        for started, run in (("linux:boot-b:120", self.SUPERVISED), (None, self.SUPERVISED),
                             ("linux:boot-a:120", {}), ("linux:boot-a:x", self.SUPERVISED),
                             ("linux:boot-a:120", None)):
            with self.subTest(started=started, run=run):
                self.refuses(started, run)


if __name__ == "__main__":
    unittest.main()
