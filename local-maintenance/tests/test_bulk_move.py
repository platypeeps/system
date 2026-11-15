"""`move <path>` puts a path on the bulk storage root, then links it (sd:8797).

The copy is checked by size and content per file before the source is
replaced by a symlink; a mismatch keeps the source and removes the copy.
Tracked git paths, build output, a path a process holds, and a missing or
full bulk root are refused. `sd` and `lsof` are stubs; git is real.
"""

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_report import FOLDER, LIB, _stub

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
                    "SD_PACK_ROOT": str(self.pack), "FAKE_BULK": str(self.bulk)}

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
