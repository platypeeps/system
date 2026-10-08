"""Tests for codebase-memory-mcp.sh run, with a stub binary.

The script runs from a copy in a temporary folder, so the checkout's log is
untouched. HOME holds a stub `~/.local/bin/codebase-memory-mcp` that reads
nothing and exits, so `run` returns at once. mkfifo exists on Linux too.
"""

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "codebase-memory-mcp.sh"


class TheStdinFifo(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = self.tmp / "checkout"
        self.folder.mkdir()
        shutil.copy(SCRIPT, self.folder / SCRIPT.name)
        binary = self.tmp / "home" / ".local" / "bin" / "codebase-memory-mcp"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.tmp / "home"), XDG_STATE_HOME=str(self.tmp / "state"))

    def run_script(self):
        return subprocess.run(["sh", str(self.folder / SCRIPT.name), "run"], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_the_fifo_lives_under_the_state_home_not_the_checkout(self):
        """A FIFO in ~/repos blocked `grep -r` for hours."""
        self.assertEqual(self.run_script().returncode, 0)
        fifo = self.tmp / "state" / "system" / "codebase-memory-mcp" / "stdin.fifo"
        self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode), fifo)
        self.assertFalse((self.folder / ".stdin.fifo").exists())

    def test_the_old_fifo_in_the_checkout_is_removed(self):
        os.mkfifo(self.folder / ".stdin.fifo")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse((self.folder / ".stdin.fifo").exists())


if __name__ == "__main__":
    unittest.main()
