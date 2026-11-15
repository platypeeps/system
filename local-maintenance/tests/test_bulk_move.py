"""`move <path>` puts a path on the bulk storage root, then links it (sd:8797).

The copy is checked by size and content per file before the source is
replaced by a symlink; a mismatch keeps the source and removes the copy.
Tracked git paths, build output, a path a process holds, and a missing or
full bulk root are refused. Writers are quiesced as machine-setup's
bulkmove does it: the move holds the cron jobs' locks and checks the
database for an open run before the copy and before the link. `sd`,
`lsof`, `launchctl` and the database read are stubs; git is real.
"""

import fcntl
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_report import FOLDER, LIB, _stub

# launchctl: a label is loaded while LOADED/<label> exists, and names
# its log under LOGS. Any verb but print fails.
LAUNCHCTL = """[ "$1" = print ] || exit 1
[ -e "$LOADED/${2##*/}" ] || exit 113
echo "	stdout path = $LOGS/${2##*.cron.}.log"
"""

# The database read under SD_DB_PYTHON: the Nth call prints RUNS_N;
# DB_FAIL fails it.
DB_READ = """[ -z "$DB_FAIL" ] || exit 1
n=$(( $(cat "$DB_CALLS" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$DB_CALLS"
eval "out=\\${RUNS_$n:-}"
[ -z "$out" ] || printf '%s\\n' "$out"
"""

_SPEC = importlib.util.spec_from_file_location("bulk_move", FOLDER / "bulk_move.py")


def _module():
    module = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(module)
    return module


def _set_xattr(path, name, value):
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/xattr", "-s", "-wx", name, value.hex(), str(path)], check=True)
    else:
        os.setxattr(path, name, value, follow_symlinks=False)


def _get_xattr(path, name):
    if sys.platform != "darwin":
        return os.getxattr(path, name, follow_symlinks=False)
    run = subprocess.run(["/usr/bin/xattr", "-s", "-px", name, str(path)],
                         check=False, capture_output=True, text=True)
    return bytes.fromhex(run.stdout.replace(" ", "").replace("\n", "")) if run.returncode == 0 else None


# A resource fork and a Finder tag on macOS; a user attribute elsewhere.
XATTRS = ({"com.apple.ResourceFork": b"fork " * 100, "com.apple.metadata:_kMDItemUserTags": b"tag"}
          if sys.platform == "darwin" else {"user.example.tag": b"tag"})


class BulkMoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bulk-move.")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        tool = self.tmp / "local-maintenance"
        tool.mkdir()
        for name in ("maintenance.sh", "in_use.py", "bulk_move.py"):
            shutil.copy(FOLDER / name, tool)
        shutil.copytree(LIB, self.tmp / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.bin = self.tmp / "bin"
        self.bulk = self.tmp / "bulk"
        self.bulk.mkdir()
        self.pack = self.tmp / "pack"
        _stub(self.pack / "bin" / "sd",
              '[ "$1 $2 $3" = "config get sd.bulk_storage_root" ] || exit 2\n'
              '[ -n "$FAKE_BULK" ] || { echo "sd.bulk_storage_root is not set" >&2; exit 1; }\n'
              'echo "$FAKE_BULK"\n')
        # lsof lists FAKE_HELD as open, or fails when FAKE_LSOF_FAIL is set.
        # FAKE_LSOF_PARTIAL: an unrelated listing, then exit 1.
        _stub(self.bin / "lsof", '[ -z "$FAKE_LSOF_FAIL" ] || exit 1\n'
              'printf "p1\\nn/dev/null\\n"\n[ -z "$FAKE_HELD" ] || printf "n%s\\n" "$FAKE_HELD"\n'
              '[ -z "$FAKE_LSOF_PARTIAL" ] || exit 1\n')
        _stub(self.bin / "launchctl", LAUNCHCTL)
        _stub(self.bin / "python-db", DB_READ)
        self.loaded = self.tmp / "loaded"
        self.loaded.mkdir()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.repo = self.tmp / "home" / "repos" / "widget"
        self.repo.mkdir(parents=True)
        self.git("init", "-q")
        (self.repo / "tracked.txt").write_text("tracked\n")
        (self.repo / "mixed").mkdir()
        (self.repo / "mixed" / "kept.txt").write_text("kept\n")
        self.git("add", "tracked.txt", "mixed/kept.txt")
        self.git("-c", "user.name=operator", "-c", "user.email=operator@example.test",
                 "commit", "-q", "-m", "init")
        (self.repo / "mixed" / "new.txt").write_text("new\n")
        self.data = self.repo / "runs" / "data"
        (self.data / "sub").mkdir(parents=True)
        (self.data / "a.bin").write_bytes(os.urandom(4096))
        (self.data / "sub" / "b.txt").write_text("b\n")
        os.symlink("a.bin", self.data / "link")
        self.env = {"HOME": str(self.tmp / "home"), "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "SYSTEM_TOOLS_CONFIG": str(self.tmp / "config"), "MAINTENANCE_PROFILE": "none",
                    "SD_PACK_ROOT": str(self.pack), "FAKE_BULK": str(self.bulk),
                    "SD_DB_PYTHON": str(self.bin / "python-db"), "DB_CALLS": str(self.tmp / "db-calls"),
                    "LOADED": str(self.loaded), "LOGS": str(self.logs),
                    "SYSTEM_TOOLS_LABEL_PREFIX": "local.system-tools"}
        # main() in this process reads the same stubs.
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def load(self, job):
        (self.loaded / f"local.system-tools.cron.{job}").touch()
        return self.logs / f".{job}.flock"

    def db_reads(self):
        path = self.tmp / "db-calls"
        return int(path.read_text()) if path.exists() else 0

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def move(self, *args, **extra):
        return subprocess.run(["sh", str(self.tmp / "local-maintenance" / "maintenance.sh"), "move", *args],
                              check=False, capture_output=True, text=True, env={**self.env, **extra}, timeout=60)

    def assert_refused(self, run, path, why):
        self.assertEqual(1, run.returncode, run.stdout + run.stderr)
        self.assertIn(why, run.stderr)
        self.assertFalse(os.path.islink(path), path)
        self.assertTrue(os.path.exists(path), path)

    def test_moves_an_untracked_folder_and_links_it(self):
        before = (self.data / "a.bin").read_bytes()
        run = self.move(str(self.data))
        self.assertEqual(0, run.returncode, run.stderr)
        dst = self.bulk / "widget" / "runs" / "data"
        self.assertTrue(self.data.is_symlink())
        self.assertEqual(str(dst), os.readlink(self.data))
        self.assertEqual(before, (dst / "a.bin").read_bytes())
        self.assertEqual("b\n", (dst / "sub" / "b.txt").read_text())
        self.assertEqual("a.bin", os.readlink(dst / "link"))
        self.assertEqual([self.data.name], [p.name for p in self.data.parent.iterdir()])

    def test_extended_attributes_and_resource_forks_move(self):
        for path in (self.data / "a.bin", self.data / "sub", self.data):
            for name, value in XATTRS.items():
                if path.is_file() or name != "com.apple.ResourceFork":
                    _set_xattr(path, name, value)
        run = self.move(str(self.data))
        self.assertEqual(0, run.returncode, run.stderr)
        dst = self.bulk / "widget" / "runs" / "data"
        for path in (dst / "a.bin", dst / "sub", dst):
            for name, value in XATTRS.items():
                if path.is_file() or name != "com.apple.ResourceFork":
                    self.assertEqual(value, _get_xattr(path, name), f"{name} on {path}")

    def test_a_copy_missing_an_attribute_keeps_the_source(self):
        bulk_move = _module()
        name, value = next(iter(XATTRS.items()))
        _set_xattr(self.data / "a.bin", name, value)
        with mock.patch.object(bulk_move, "copy", lambda src, dst: shutil.copytree(src, dst, symlinks=True,
                                                                                copy_function=shutil.copyfile)), \
                mock.patch.object(bulk_move, "held_open", lambda path: False):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertFalse(self.data.is_symlink())
        self.assertEqual(value, _get_xattr(self.data / "a.bin", name))
        self.assertFalse((self.bulk / "widget" / "runs" / "data").exists())

    def test_a_destination_the_lane_sweep_reads_is_refused(self):
        # repo-sync's hyg_lane deletes old files under <root>/*/lane/.
        lane = self.repo / "lane"
        lane.mkdir()
        (lane / "old.log").write_text("old\n")
        self.assert_refused(self.move(str(lane)), lane, "lane")
        loose = self.tmp / "home" / "Downloads" / "lane"
        loose.parent.mkdir()
        loose.write_text("x\n")
        for folder in ("widget", "notes/lane"):
            self.assert_refused(self.move(str(loose), folder), loose, "lane")
        self.assertEqual(0, self.move(str(self.data), "notes").returncode)
        self.assertFalse((self.bulk / "widget").exists())

    def test_a_lane_folder_inside_the_moved_folder_is_refused(self):
        # With folder `.` the copy is <root>/archive, so its lane/ child
        # is <root>/archive/lane, which the lane sweep empties.
        archive = self.tmp / "home" / "archive"
        (archive / "Lane").mkdir(parents=True)
        (archive / "Lane" / "old.log").write_text("old\n")
        self.assert_refused(self.move(str(archive), "."), archive, "lane")
        self.assertFalse((self.bulk / "archive").exists())

    def test_a_tracked_file_is_refused(self):
        run = self.move(str(self.repo / "tracked.txt"))
        self.assert_refused(run, self.repo / "tracked.txt", "tracked")
        self.assertFalse((self.bulk / "widget").exists())

    def test_a_folder_holding_a_tracked_file_is_refused(self):
        self.assert_refused(self.move(str(self.repo / "mixed")), self.repo / "mixed", "tracked")

    def test_a_checkout_is_refused(self):
        self.assert_refused(self.move(str(self.repo)), self.repo, "checkout")

    def test_build_output_is_refused(self):
        for name in ("target", "node_modules"):
            out = self.repo / name / "x"
            out.mkdir(parents=True)
            self.assert_refused(self.move(str(out)), out, "build output")
            (self.data / name).mkdir()
            self.assert_refused(self.move(str(self.data)), self.data, "build output")
            (self.data / name).rmdir()

    def test_a_path_a_process_holds_is_refused(self):
        run = self.move(str(self.data), FAKE_HELD=str(self.data / "a.bin"))
        self.assert_refused(run, self.data, "open")

    def test_a_write_during_the_last_database_read_keeps_the_original(self):
        """NEW (sd:8797 review). A writer outside the locked jobs updates
        the source after the verify, during the last database read; the
        original with that write stays, and the stale copy is not linked."""
        bulk_move = _module()
        real_idle, calls = bulk_move.check_idle, []

        def writer(when):
            calls.append(when)
            if len(calls) == 2:
                (self.data / "sub" / "b.txt").write_text("late\n")
            real_idle(when)
        with mock.patch.object(bulk_move, "check_idle", writer):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertEqual(2, len(calls))
        self.assertFalse(self.data.is_symlink())
        self.assertEqual("late\n", (self.data / "sub" / "b.txt").read_text())
        self.assertEqual([self.data.name], [p.name for p in self.data.parent.iterdir()])
        self.assertFalse((self.bulk / "widget" / "runs" / "data").exists())

    def test_a_process_holding_the_source_before_the_link_keeps_it(self):
        """NEW (sd:8797 review). lsof runs again just before the link,
        with the source in place; a handle on it refuses."""
        bulk_move = _module()
        answers = iter([False, True])
        with mock.patch.object(bulk_move, "held_open", lambda path: next(answers)):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertFalse(self.data.is_symlink())
        self.assertEqual("b\n", (self.data / "sub" / "b.txt").read_text())
        self.assertEqual([self.data.name], [p.name for p in self.data.parent.iterdir()])

    def test_a_write_through_the_link_after_publication_stays_live(self):
        """NEW (sd:8797 review round 2). Once the link is published the
        copy is the live data: a write through src lands in dst, and no
        rollback may put the older original back over it."""
        bulk_move = _module()
        real_held, calls = bulk_move.held_open, []

        def writer(path):
            calls.append(path)
            if os.path.islink(self.data):
                (self.data / "sub" / "b.txt").write_text("through the link\n")
            return real_held(path)
        with mock.patch.object(bulk_move, "held_open", writer):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertTrue(self.data.is_symlink())
        self.assertEqual("through the link\n", (self.data / "sub" / "b.txt").read_text())
        self.assertEqual(1, rc)  # the original differs now, so it is kept, not deleted
        aside = [p for p in self.data.parent.iterdir() if p.name.startswith("data.bulk-move-")]
        self.assertEqual(1, len(aside))
        self.assertEqual("b\n", (aside[0] / "sub" / "b.txt").read_text())

    def test_a_handle_on_the_original_after_publication_keeps_both(self):
        """NEW (sd:8797 review round 2). lsof shows a handle on the
        original after the link: the link stays, the original is kept
        beside it and named, and nothing is deleted."""
        bulk_move = _module()
        answers = iter([False, False, True])
        with mock.patch.object(bulk_move, "held_open", lambda path: next(answers)), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertTrue(self.data.is_symlink())
        aside = [p for p in self.data.parent.iterdir() if p.name.startswith("data.bulk-move-")]
        self.assertEqual(1, len(aside))
        self.assertIn(str(aside[0]), err.getvalue())

    def test_a_failed_lsof_before_the_link_keeps_the_source(self):
        """NEW (sd:8797 review round 2, class pass). An lsof that fails
        just before the link refuses: the copy goes, the source stays."""
        bulk_move = _module()
        answers = iter([False])

        def lsof(path):
            for answer in answers:
                return answer
            raise bulk_move.Refused("lsof gave no full answer (exit 1)")
        with mock.patch.object(bulk_move, "held_open", lsof):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertFalse(self.data.is_symlink())
        self.assertFalse((self.bulk / "widget" / "runs" / "data").exists())

    def test_an_interrupt_during_the_swap_restores_the_source(self):
        bulk_move = _module()
        with mock.patch.object(bulk_move, "held_open", lambda path: False), \
                mock.patch.object(bulk_move.os, "symlink", side_effect=KeyboardInterrupt):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertFalse(self.data.is_symlink())
        self.assertEqual("b\n", (self.data / "sub" / "b.txt").read_text())
        self.assertEqual([self.data.name], [p.name for p in self.data.parent.iterdir()])

    def test_a_source_recreated_during_the_swap_is_not_overwritten(self):
        bulk_move = _module()

        def writer_wins(target, link):
            pathlib.Path(link).write_text("new\n")
            raise FileExistsError(17, "File exists", link)
        with mock.patch.object(bulk_move, "held_open", lambda path: False), \
                mock.patch.object(bulk_move.os, "symlink", writer_wins), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertEqual("new\n", self.data.read_text())
        aside = [p for p in self.data.parent.iterdir() if p.name.startswith("data.bulk-move-")]
        self.assertEqual(1, len(aside))
        self.assertEqual("b\n", (aside[0] / "sub" / "b.txt").read_text())
        self.assertIn(str(aside[0]), err.getvalue())

    def test_a_job_run_in_progress_keeps_the_source(self):
        """NEW (sd:8797, ruling #55829). A cron run holding its job's
        lock may write the source; the move refuses and copies nothing."""
        self.load("build-dispatch")
        with open(self.load("lane-run"), "a") as run:
            fcntl.flock(run, fcntl.LOCK_EX)
            self.assert_refused(self.move(str(self.data)), self.data, "a run of lane-run is in progress")
        self.assertFalse((self.bulk / "widget").exists())

    def test_job_slots_skip_while_the_move_copies(self):
        """NEW (sd:8797). Every loaded job's lock is held from before the
        copy to the end, so no slot starts a run that writes the source."""
        locks = [self.load(job) for job in ("build-dispatch", "lane-run", "night-build")]
        bulk_move = _module()
        real_copy, seen = bulk_move.copy, []

        def probe(src, dst):
            for lock in locks:
                with open(lock, "a") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        seen.append("free")
                    except BlockingIOError:
                        seen.append("held")
            real_copy(src, dst)
        with mock.patch.object(bulk_move, "copy", probe):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(0, rc)
        self.assertEqual(["held"] * 3, seen)
        self.assertTrue(self.data.is_symlink())

    def test_the_database_is_read_before_the_copy_and_before_the_link(self):
        run = self.move(str(self.data))
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(2, self.db_reads())

    def test_an_open_run_before_the_copy_keeps_the_source(self):
        """NEW (sd:8797). An open pack run may write the source."""
        run = self.move(str(self.data), RUNS_1="run 7")
        self.assert_refused(run, self.data, "open run(s) before the copy: run 7")
        self.assertFalse((self.bulk / "widget").exists())

    def test_an_open_run_after_the_copy_removes_the_copy(self):
        """NEW (sd:8797). A run that opened during the copy may have
        written the source after it was read; the source stays."""
        run = self.move(str(self.data), RUNS_2="lane 8797 3")
        self.assert_refused(run, self.data, "open run(s) after the copy: lane 8797 3")
        self.assertEqual("b\n", (self.data / "sub" / "b.txt").read_text())
        self.assertFalse((self.bulk / "widget" / "runs" / "data").exists())

    def test_an_unreadable_database_keeps_the_source(self):
        run = self.move(str(self.data), DB_FAIL="1")
        self.assert_refused(run, self.data, "could not read the open runs")
        self.assertFalse((self.bulk / "widget").exists())

    def test_the_jobs_are_machine_setups_bulkmove_jobs(self):
        """PIN (sd:8797). One list of the jobs that start pack runs."""
        setup = (FOLDER.parent / "local-machine-setup" / "machine-setup.sh").read_text()
        jobs = re.search(r'^BULK_JOBS="([^"]*)"', setup, re.M).group(1).split()
        self.assertEqual(jobs, list(_module().JOBS))

    def test_a_failed_lsof_is_refused(self):
        self.assert_refused(self.move(str(self.data), FAKE_LSOF_FAIL="1"), self.data, "lsof")

    def test_a_partial_lsof_listing_is_refused(self):
        self.assert_refused(self.move(str(self.data), FAKE_LSOF_PARTIAL="1"), self.data, "lsof")

    def test_a_relative_link_out_of_the_folder_is_refused(self):
        (self.repo / "runs" / "shared").write_text("shared\n")
        os.symlink("../shared", self.data / "out")
        run = self.move(str(self.data))
        self.assert_refused(run, self.data, "link")
        self.assertEqual("shared\n", (self.data / "out").read_text())
        self.assertFalse((self.bulk / "widget").exists())

    def test_links_inside_the_folder_and_absolute_links_move(self):
        os.symlink("../sub/b.txt", self.data / "sub" / "back")
        os.symlink(str(self.repo / "tracked.txt"), self.data / "abs")
        run = self.move(str(self.data))
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual("b\n", (self.data / "sub" / "back").read_text())
        self.assertEqual("tracked\n", (self.data / "abs").read_text())

    def test_a_bulk_root_inside_the_source_is_refused(self):
        inner = self.data / "bulk"
        inner.mkdir()
        run = self.move(str(self.data), FAKE_BULK=str(inner))
        self.assert_refused(run, self.data, "inside")
        self.assertEqual([], list(inner.iterdir()))

    def test_a_folder_that_leaves_the_bulk_root_is_refused(self):
        loose = self.tmp / "home" / "Downloads" / "big.iso"
        loose.parent.mkdir()
        loose.write_bytes(b"iso")
        elsewhere = self.tmp / "elsewhere"
        for folder in ("../elsewhere", str(elsewhere)):
            self.assert_refused(self.move(str(loose), folder), loose, "bulk root")
        self.assertFalse(elsewhere.exists())

    def test_a_missing_bulk_root_is_refused(self):
        run = self.move(str(self.data), FAKE_BULK=str(self.tmp / "unmounted"))
        self.assert_refused(run, self.data, "bulk root")

    def test_an_unset_bulk_root_is_refused(self):
        self.assert_refused(self.move(str(self.data), FAKE_BULK=""), self.data, "sd.bulk_storage_root")

    def test_an_existing_destination_is_refused(self):
        (self.bulk / "widget" / "runs" / "data").mkdir(parents=True)
        self.assert_refused(self.move(str(self.data)), self.data, "exists")

    def test_a_symlink_is_refused(self):
        link = self.repo / "runs" / "alias"
        os.symlink("data", link)
        run = self.move(str(link))
        self.assertEqual(1, run.returncode)
        self.assertIn("symlink", run.stderr)

    def test_outside_a_checkout_the_folder_is_named(self):
        loose = self.tmp / "home" / "Downloads" / "big.iso"
        loose.parent.mkdir()
        loose.write_bytes(b"iso" * 1000)
        self.assert_refused(self.move(str(loose)), loose, "folder")
        run = self.move(str(loose), "downloads")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(str(self.bulk / "downloads" / "big.iso"), os.readlink(loose))
        self.assertEqual(b"iso" * 1000, loose.read_bytes())

    def test_a_mismatch_keeps_the_source_and_removes_the_copy(self):
        bulk_move = _module()
        real_copy = bulk_move.copy

        def bad_copy(src, dst):
            real_copy(src, dst)
            with open(os.path.join(dst, "a.bin"), "r+b") as f:
                first = f.read(1)
                f.seek(0)
                f.write(bytes([first[0] ^ 0xFF]))

        before = (self.data / "a.bin").read_bytes()
        with mock.patch.object(bulk_move, "copy", bad_copy), \
                mock.patch.object(bulk_move, "held_open", lambda path: False):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertFalse(self.data.is_symlink())
        self.assertEqual(before, (self.data / "a.bin").read_bytes())
        self.assertFalse((self.bulk / "widget" / "runs" / "data").exists())

    def test_a_full_bulk_root_is_refused(self):
        bulk_move = _module()
        with mock.patch.object(bulk_move, "free_bytes", lambda path: 10), \
                mock.patch.object(bulk_move, "held_open", lambda path: False):
            rc = bulk_move.main([str(self.bulk), str(self.data)])
        self.assertEqual(1, rc)
        self.assertFalse(self.data.is_symlink())
        self.assertFalse((self.bulk / "widget").exists())


if __name__ == "__main__":
    unittest.main()
