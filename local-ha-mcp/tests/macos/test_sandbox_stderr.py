"""ha-mcp.sh serve under sandbox-exec, which only macOS has (sd:10894).

The portable suite, ../test_ha_mcp.py, runs in tests/ci-native.sh on Linux
too; this case needs sandbox-exec to deny /dev/fd, so it runs from
tests/macos-only-suites.txt. It reuses that suite's Bridge fixture and runs
only its own case.
"""

import pathlib
import socket
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import test_ha_mcp  # noqa: E402 - after the path
from test_ha_mcp import BODY  # noqa: E402 - after the path


class SocketStderr(test_ha_mcp.Bridge):
    def test_serve_starts_when_stderr_is_a_socket_it_cannot_reopen(self):
        """An MCP launcher can hand fd 2 over as a socket (round 3).

        macOS reopens /dev/fd/2 on a socket; Linux refuses it, and so does
        a sandboxed launcher, with Operation not permitted. sandbox-exec
        denies /dev/fd here to stand in for both: serve must inherit fd 2.
        """
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        deny = '(version 1)(allow default)(deny file-read* file-write* (regex #"^/dev/fd/"))'
        with theirs:
            result = subprocess.run(["sandbox-exec", "-p", deny, "sh", str(self.base / "local-ha-mcp/ha-mcp.sh"),
                                     "serve"], env=self.env(NPX_MODE="fetch"), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=theirs.fileno(), timeout=60)
        ours.settimeout(5)
        said = ours.recv(65536).decode(errors="replace")

        self.assertEqual(result.returncode, 0, said)
        port = self.assert_loopback_relay(self.bridge_url())
        self.assertEqual(self.logged("body"), [BODY], self.log.read_text())
        self.assert_closes(port)


def load_tests(loader, tests, pattern):
    """Only this file's case: the inherited Bridge cases run in the portable suite."""
    return unittest.TestSuite([SocketStderr("test_serve_starts_when_stderr_is_a_socket_it_cannot_reopen")])


if __name__ == "__main__":
    unittest.main()
