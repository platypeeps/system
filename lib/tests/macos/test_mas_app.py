"""lib/mas_app.sh reads a running app by its executable's full path
(sd:8580). On a terminal macOS ps cuts each line to the window width,
so a path longer than the window never matched and a running app read
as not running. Real ps and pgrep, so macOS only."""

import fcntl
import os
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "mas_app.sh"


def on_pty(script, *args, columns=40):
    """SCRIPT's output under sh, its stdout a COLUMNS-wide terminal."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
    try:
        subprocess.run(["sh", "-c", script, "sh", *args], stdout=slave,
                       stdin=subprocess.DEVNULL, check=True, timeout=30)
        out = b""
        while select.select([master], [], [], 0.5)[0]:
            chunk = os.read(master, 4096)
            if not chunk:
                break
            out += chunk
    finally:
        os.close(slave)
        os.close(master)
    return out.decode().replace("\r\n", "\n")


class ExecutablePathTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.bundle = tmp / "An-app-with-a-long-name-example.app"
        exe = self.bundle / "Contents" / "MacOS" / "examplesleeper"
        exe.parent.mkdir(parents=True)
        shutil.copy("/bin/sleep", exe)
        # A copied platform binary is killed at exec until signed anew.
        subprocess.run(["codesign", "-s", "-", "-f", str(exe)], check=True,
                       capture_output=True, timeout=30)
        self.exe = str(exe)
        self.assertGreater(len(self.exe), 40)
        proc = subprocess.Popen([self.exe, "60"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.pid = str(proc.pid)

    def test_the_path_is_whole_on_a_narrow_terminal(self):
        out = on_pty('. "$1"; _st_mas_exe "$2"', str(LIB), self.pid)
        self.assertEqual(out, self.exe + "\n")

    def test_a_running_app_is_found_on_a_narrow_terminal(self):
        out = on_pty('. "$1"; _st_mas_pids "$2" "$3"', str(LIB),
                     str(self.bundle), "examplesleeper")
        self.assertEqual(out, self.pid + "\n")


if __name__ == "__main__":
    unittest.main()
