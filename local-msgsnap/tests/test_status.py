"""`msgsnap.sh status` follows convention 6 (sd:3552).

The script runs from a copy in a temp folder, so `bin/msgsnap` is absent
unless the case makes one, and HOME points at a folder of its own. Nothing
here touches the real binary, whose cdhash holds the FDA grant.
"""

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

TOOL = pathlib.Path(__file__).resolve().parent.parent


class Status(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.folder = self.root / "local-msgsnap"
        self.folder.mkdir()
        shutil.copy(TOOL / "msgsnap.sh", self.folder / "msgsnap.sh")
        self.home = self.root / "home"
        self.home.mkdir()

    def status(self):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        return subprocess.run(["sh", str(self.folder / "msgsnap.sh"), "status"],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_an_unbuilt_binary_is_nothing_to_check(self):
        done = self.status()
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("binary:    not built", done.stdout)

    def test_a_built_binary_is_healthy(self):
        (self.folder / "bin").mkdir()
        binary = self.folder / "bin" / "msgsnap"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        done = self.status()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
