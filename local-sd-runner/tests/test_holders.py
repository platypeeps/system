"""Clone holders come from one bounded open-file table, and its warnings neither hide nor invent one."""

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
        # A directory walk hangs, as `+D` did over a large clone on a loaded
        # machine (sd:1775); the open-file table answers at once.
        # A clone whose names lsof escapes is walked instead; `walk` is that answer.
        lsof.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_LSOF/argv"\n'
                        'for word; do [ "$word" = +D ] || continue\n'
                        '  [ -e "$FAKE_LSOF/walk" ] || exec sleep 30\n'
                        '  cat "$FAKE_LSOF/walk"; cat "$FAKE_LSOF/stderr" >&2; exit "$(cat "$FAKE_LSOF/rc")"; done\n'
                        'cat "$FAKE_LSOF/stdout"\ncat "$FAKE_LSOF/stderr" >&2\nexit "$(cat "$FAKE_LSOF/rc")"\n')
        lsof.chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_LSOF": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def table(self) -> str:
        """Two holders, a working directory and an open file, and one process elsewhere."""
        return f"p4242\nfcwd\nn{self.clone}\np4343\nf6\nn{self.clone}/held\np5555\nf3\nn/dev/null\n"

    def lsof(self, *, stderr: str, stdout: str | bytes | None = None, rc: int = 0, walk: bool = False) -> set[int]:
        table = self.table() if stdout is None else stdout
        (self.root / "stdout").write_bytes(table if isinstance(table, bytes) else table.encode())
        (self.root / "stderr").write_text(stderr)
        (self.root / "rc").write_text(str(rc))
        with patch.object(processes, "LSOF_SECONDS", 5):
            held = processes.holders(self.clone)
        if not walk:
            self.assertNotIn("+D", self.argv())
        return held

    def argv(self) -> list[str]:
        return (self.root / "argv").read_text().split("\n")

    def refused(self, **kwargs) -> str:
        with self.assertRaises(RunnerRefused) as caught:
            self.lsof(**kwargs)
        self.assertIn("cannot verify clone holders", str(caught.exception))
        return str(caught.exception)

    def test_quiet_lsof_returns_the_parsed_pids(self):
        self.assertEqual(self.lsof(stderr=""), {4242, 4343})

    def test_a_directory_walk_that_hangs_does_not_hold_the_scan(self):
        # sd:1775: `+D` stats every file under the clone before it answers;
        # at load 71 it took 28 s over 105,000 files, past `LSOF_SECONDS`.
        self.assertEqual(self.lsof(stderr=""), {4242, 4343})

    def test_only_a_name_under_the_clone_is_a_holder(self):
        sibling = self.clone.parent / f"{self.clone.name}-2"
        table = (f"p4242\nfcwd\nn{self.clone}\np4343\nf6\nn{self.clone}/a/b\n"
                 f"p5555\nfcwd\nn{sibling}\np6666\nf4\nn{self.clone.parent}\np7777\nf5\nn\n")
        self.assertEqual(self.lsof(stderr="", stdout=table), {4242, 4343})

    def test_a_clone_named_through_a_symlink_matches_the_resolved_names(self):
        # The kernel names an open file by its resolved path, as `/var` is
        # `/private/var` on macOS.
        resolved = self.clone
        alias = self.root / "alias"
        alias.symlink_to(resolved.parent)
        self.clone = alias / resolved.name
        self.assertEqual(self.lsof(stderr="", stdout=f"p4242\nf7\nn{resolved}/held\n"), {4242})

    def test_warning_about_another_mount_returns_the_parsed_pids(self):
        self.assertEqual(self.lsof(stderr=SNAPSHOT), {4242, 4343})
        self.assertEqual(self.lsof(stderr=SNAPSHOT, stdout="p5555\nf3\nn/dev/null\n", rc=1), set())

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

    def test_an_error_lsof_prints_as_a_name_refuses(self):
        # sd:2769. lsof reports a process it could not read as a file whose
        # name is the error, on stdout with exit 0, so that process's files are unknown.
        for record in ("fcwd\nncwd|rtd info error: Operation not permitted",
                       "ferr\nnFD info error: Cannot allocate memory",
                       "ftxt\nnregion info error: Operation not permitted"):
            with self.subTest(record=record):
                self.assertIn("could not read process 4646", self.refused(stderr="", stdout=self.table() + f"p4646\n{record}\n"))

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


class HardLinkAliases(FakeLsof):
    """A process can open a file in the clone through a hard link outside it,
    and lsof then prints the outside name. The old `+D` selection matched by
    device and inode, so the alias was a holder; it still is (sd:1775 review 2)."""

    def alias(self, inside: Path) -> str:
        """The table line set for one process holding `inside` through an outside link."""
        outside = self.root / "outside"
        os.link(inside, outside)
        found = outside.stat()
        return (f"p4242\nf6\ntREG\nD0x{found.st_dev:x}\ni{found.st_ino}\nk{found.st_nlink}\nn{outside}\n"
                f"p5555\nf3\ntCHR\nD0x0\ni1\nk1\nn/dev/null\n")

    def test_an_outside_hard_link_into_the_clone_is_a_holder(self):
        inside = self.clone / "deep/held"
        inside.parent.mkdir()
        inside.write_text("x")
        self.assertEqual(self.lsof(stderr="", stdout=self.alias(inside)), {4242})

    def test_a_linked_file_with_no_name_in_the_clone_is_not_a_holder(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("x")
        self.assertEqual(self.lsof(stderr="", stdout=self.alias(elsewhere)), set())

    def test_an_outside_link_unlinked_after_open_is_a_holder(self):
        """The outside name is gone and the clone's name is the one link left (sd:1775 review 3).

        The open descriptor still names the outside path, now with nlink 1,
        so a filter on more than one link let a process that writes clone
        content go unnoticed. The inode is matched whatever the link count.
        """
        inside = self.clone / "deep/held"
        inside.parent.mkdir()
        inside.write_text("x")
        table = self.alias(inside)
        (self.root / "outside").unlink()
        self.assertEqual(inside.stat().st_nlink, 1)
        for printed in ("", " (deleted)"):
            with self.subTest(printed=printed):
                stdout = table.replace("\nk2\n", "\nk1\n").replace(f"n{self.root}/outside\n", f"n{self.root}/outside{printed}\n")
                self.assertIn("\nk1\n", stdout)
                self.assertEqual(self.lsof(stderr="", stdout=stdout), {4242})

    def test_a_single_link_file_with_no_name_in_the_clone_is_not_a_holder(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("x")
        found = elsewhere.stat()
        stdout = f"p4242\nf6\ntREG\nD0x{found.st_dev:x}\ni{found.st_ino}\nk1\nn{elsewhere}\n"
        self.assertEqual(self.lsof(stderr="", stdout=stdout), set())

    def test_a_linked_file_without_an_inode_refuses(self):
        found = self.clone.stat()
        self.refused(stderr="", stdout=f"p4242\nf6\ntREG\nD0x{found.st_dev:x}\nk2\nn{self.root}/outside\n")


class Entry:
    """A directory entry; a directory is removed between the listing and its stat."""

    def __init__(self, path: str, inode: int, *, directory: bool):
        self.path, self._inode, self.directory = path, inode, directory

    def is_dir(self, follow_symlinks=True):
        return self.directory

    def is_file(self, follow_symlinks=True):
        return not self.directory

    def inode(self):
        return self._inode

    def stat(self, follow_symlinks=True):
        raise FileNotFoundError(2, "No such file or directory", self.path)


class Listing(list):
    def __enter__(self):
        return iter(self)

    def __exit__(self, *exc):
        return False


class LinkedInsideWalk(unittest.TestCase):
    def test_a_directory_that_vanishes_mid_walk_skips_only_itself(self):
        """A child removed between the listing and its stat used to end the
        listing of its parent, so a later sibling's inode went unmatched
        (sd:1775 review 4)."""
        listing = Listing([Entry("/clone/gone", 7, directory=True), Entry("/clone/held", 42, directory=False)])
        with patch.object(processes.os, "scandir", lambda path: listing):
            self.assertEqual(processes._linked_inside(Path("/clone"), 1, {42}), {42})


class EscapedNames(FakeLsof):
    """lsof escapes a name it prints: a control character as `\\n` or `^A`, a
    backslash as `\\\\`, and a non-ASCII byte as `\\xNN` outside a UTF-8 locale.
    A clone whose own path holds such a character cannot be matched as text,
    so it is selected by filesystem with `+D`; any other clone's names start
    with its path exactly as lsof prints it (sd:1775 review)."""

    def escaped(self, name: str, printed: str) -> str:
        """Move the clone under `name`; the table names it as lsof prints it, `printed`."""
        self.clone = self.root / "work" / name
        self.clone.mkdir(parents=True)
        return f"p4242\nf6\nn{self.root}/work/{printed}/held\np5555\nf3\nn/dev/null\n"

    def walked(self, table: str) -> set[int]:
        """The holders, and that they came from a `+D` walk of the clone, not from the table."""
        (self.root / "walk").write_text("p4242\n")
        held = self.lsof(stderr="", stdout=table, walk=True)
        self.assertEqual(held, {4242})
        self.assertIn("+D", self.argv())
        self.assertIn(str(self.clone), (self.root / "argv").read_text())
        return held

    def test_a_clone_under_a_newline_is_walked(self):
        self.assertEqual(self.walked(self.escaped("a\nb", "a\\nb")), {4242})

    def test_a_non_ascii_clone_is_walked(self):
        self.assertEqual(self.walked(self.escaped("caf\u00e9", "caf\\xc3\\xa9")), {4242})

    def test_a_clone_with_a_caret_is_walked(self):
        """A literal `^A` prints as control-A does, so the text cannot tell them apart."""
        self.assertEqual(self.walked(self.escaped("car^Aet", "car^Aet")), {4242})

    def test_a_walk_that_does_not_finish_refuses(self):
        """Fail closed: a clone whose holders cannot be read is held."""
        self.escaped("a\nb", "a\\nb")
        with self.assertRaises(RunnerRefused) as caught:
            self.lsof(stderr="", walk=True)
        self.assertIn("cannot verify clone holders", str(caught.exception))

    def test_escaped_names_under_a_plain_clone_still_match(self):
        table = (f"p4242\nf6\nn{self.clone}/x\\ny\np4343\nf7\nn{self.clone}/caf\\xc3\\xa9\n"
                 f"p5555\nf3\nn{self.clone.parent}/a\\nb\n")
        self.assertEqual(self.lsof(stderr="", stdout=table), {4242, 4343})

    def test_a_name_that_is_not_utf8_does_not_break_the_read(self):
        table = f"p4242\nfcwd\nn{self.clone}\np5555\nf3\nn".encode() + b"/tmp/caf\xe9\n"
        self.assertEqual(self.lsof(stderr="", stdout=table), {4242})


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

    def test_real_holder_through_an_outside_hard_link_is_found(self):
        """lsof names the file by the link the process opened, outside the clone."""
        with tempfile.TemporaryDirectory(suffix=".noindex") as temporary:
            clone = Path(temporary).resolve() / "clone"
            clone.mkdir()
            (clone / "held").write_text("x")
            outside = Path(temporary).resolve() / "outside"
            os.link(clone / "held", outside)
            holder = subprocess.Popen(
                [sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); print('open', flush=True); time.sleep(60)",
                 str(outside)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "open")
                self.assertIn(holder.pid, processes.holders(clone))
            finally:
                holder.kill()
                holder.wait()
                holder.stdout.close()

    def test_real_holder_under_an_escaped_name_is_found(self):
        """lsof prints these names escaped; the walk selects them by filesystem (sd:1775 review)."""
        for name in ("a\nb", "caf\u00e9"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(suffix=".noindex") as temporary:
                clone = Path(temporary).resolve() / name
                clone.mkdir()
                holder = subprocess.Popen(
                    [sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'w'); print('open', flush=True); time.sleep(60)",
                     str(clone / "held")], stdout=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(holder.stdout.readline().strip(), "open")
                    self.assertIn(holder.pid, processes.holders(clone))
                finally:
                    holder.kill()
                    holder.wait()
                    holder.stdout.close()


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
