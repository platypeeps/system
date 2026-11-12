"""fseventsd.sh's reader against the real pgrep and ps on this machine.

macOS only, so it lives here and tests/macos-only-suites.txt names it:
CLAUDE.md forbids a skipped test, and tests/ci-native.sh fails a suite whose
log reports one. tests/run-macos-only.sh runs this on a Mac and refuses to run
anywhere else, so nothing here skips: no fseventsd on a Mac is a failure, not
an excuse, and this suite exists to read that daemon.

The stub table in ../test_fseventsd.py cannot show what the real commands do
with a window width. Measured on Darwin 27.2.0: `ps -axo pid=,rss=,comm=`
writing straight to a terminal cuts each line to the window, 41 characters at
40 columns and 80 at 79, while the daemon's comm path is 134, so a basename
match on that field finds nothing there. Writing to a pipe or a file it cuts
nothing. These tests hold the reader to a form that asks for no comm field at
all, the form docs/work/2026-11-04-ramdisk-tmp/design.md:134 already used on
this daemon, and they cover both the terminal and the pipe.
"""

import fcntl
import os
import pty
import shutil
import struct
import subprocess
import tempfile
import termios
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[2]
ENTRYPOINT = FOLDER / "fseventsd.sh"


class TheLiveDaemonReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = subprocess.run(["uname", "-s"], capture_output=True,
                                    text=True, timeout=30).stdout.strip()
        cls.pgrep = subprocess.run(["pgrep", "-x", "fseventsd"],
                                   capture_output=True, text=True, timeout=30)

    def setUp(self):
        self.assertEqual("Darwin", self.system,
                         "this suite is macOS only; tests/run-macos-only.sh runs it")
        self.assertEqual(0, self.pgrep.returncode,
                         "no fseventsd is running, so the daemon cannot be read")

    def live_env(self):
        """The real PATH, a scratch HOME and config, and a budget no daemon
        reaches, so the run reports the size and exits 0."""
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home),
                "TMPDIR": str(home), "SYSTEM_TOOLS_CONFIG": str(home / "config"),
                "FSEVENTSD_RSS_MB": "1048576"}

    def test_the_reader_asks_for_no_comm_column(self):
        text = ENTRYPOINT.read_text()
        self.assertNotIn("comm=", text,
                         "a comm column is cut to the window width on a terminal")
        self.assertIn("pgrep -x fseventsd", text)

    def test_the_live_daemon_is_found_through_a_pipe(self):
        done = subprocess.run(["sh", str(ENTRYPOINT), "check"], env=self.live_env(),
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"(?m)^daemon: pid \d+, \d+ MB resident$")

    def test_the_live_daemon_is_found_on_a_narrow_terminal(self):
        """40 columns, well under the 134 the comm path needs."""
        for columns in (40, 80):
            with self.subTest(columns=columns):
                code, text = self.on_a_terminal(columns, self.live_env())
                self.assertEqual(0, code, text)
                self.assertRegex(text, r"daemon: pid \d+, \d+ MB resident")
                self.assertNotIn("no fseventsd process is running", text)

    def on_a_terminal(self, columns, env):
        """`check` with stdout on a pseudo-terminal of that window width."""
        pid, handle = pty.fork()
        if pid == 0:                                        # pragma: no cover
            try:
                os.execve("/bin/sh", ["sh", str(ENTRYPOINT), "check"], env)
            finally:
                os._exit(127)
        fcntl.ioctl(handle, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, columns, 0, 0))
        out = b""
        while True:
            try:
                chunk = os.read(handle, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.close(handle)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")


if __name__ == "__main__":
    unittest.main()
