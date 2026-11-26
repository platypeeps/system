"""ha-mcp.sh: the loopback relay and the `status` the health check sweeps (sd:10894).

macOS's Local Network privacy refused node, Homebrew's and nodejs.org's
alike, the Home Assistant host: every bridge call failed with EHOSTUNREACH
while Apple's curl reached the same host. Loopback is never gated, so the
bridge now runs relay.py under Apple's python and points mcp-remote at
127.0.0.1; the relay ends with the bridge. `status` tells the nightly
health check when the bridge fails, and names the cause.

Each case runs a copy of the folder against stubs: an npx that speaks the
MCP handshake, fetches through the URL it was given, or hangs, and a curl.
The relay is the real one, under this interpreter; the Home Assistant it
reaches is a local HTTP server. The token is a fake.
"""

import http.server
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

TOKEN = "example-token-not-real"
BODY = "home-assistant-example"

# Logs its URL argument and the node PATH resolves. NPX_MODE picks the rest:
# ok answers initialize and tools/list until stdin closes; fetch GETs the
# URL and logs the body; hang sleeps in this process; ehost fails as
# mcp-remote does when macOS refuses node the LAN.
NPX = r"""#!/bin/sh
echo "url=$3 node=$(command -v node)" >> "$NPX_LOG"
case "$NPX_MODE" in
  ehost)
    echo "fetch failed: connect EHOSTUNREACH 192.0.2.10:8123" >&2
    exit 1 ;;
  fetch)
    "$TEST_PYTHON" -c 'import sys, urllib.request; print("body=" + urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())' "$3" >> "$NPX_LOG" 2>&1
    cat >/dev/null
    exit 0 ;;
  hang)
    exec sleep 60 ;;
  silent)
    cat >/dev/null
    exit 0 ;;
esac
while IFS= read -r line; do
  case "$line" in
    *'"method":"initialize"'*)
      echo '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"home-assistant","version":"1.0"},"capabilities":{"tools":{}}}}' ;;
    *'"method":"tools/list"'*)
      echo '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"a"},{"name":"b"}]}}' ;;
  esac
done
"""

NODE = "#!/bin/sh\necho v26.0.0\n"

# Prints $CURL_CODE as -w '%{http_code}' does; 000 is no answer, exit 7.
CURL = r"""#!/bin/sh
printf '%s' "$CURL_CODE"
[ "$CURL_CODE" != 000 ] || exit 7
"""

# A relay that macOS refused the LAN: it hands out a URL, and its first
# connection upstream fails with No route to host.
REFUSED_RELAY = r"""#!/bin/sh
echo "http://127.0.0.1:9/mcp_server/sse"
echo "ha-mcp relay: cannot reach 192.0.2.10:8123: [Errno 65] No route to host" >&2
sleep 30
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = BODY.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Bridge(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        shutil.copytree(FOLDER, self.base / "local-ha-mcp", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, self.base / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.stubs = self.base / "stubs"
        write_exec(self.stubs / "node", NODE)
        write_exec(self.stubs / "npx", NPX)
        write_exec(self.base / "curl", CURL)
        self.log = self.base / "npx.log"
        self.ha = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.ha.serve_forever, daemon=True).start()
        self.addCleanup(self.ha.server_close)
        self.addCleanup(self.ha.shutdown)
        self.url = f"http://127.0.0.1:{self.ha.server_address[1]}/mcp_server/sse"

    def env(self, **extra):
        env = {
            "HOME": str(self.base / "home"),
            "SYSTEM_TOOLS_CONFIG": str(self.base / "config"),
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "TMPDIR": str(self.base),
            "HA_TOKEN": TOKEN,
            "HA_MCP_URL": self.url,
            "HA_MCP_CURL": str(self.base / "curl"),
            "HA_MCP_PYTHON": sys.executable,
            "TEST_PYTHON": sys.executable,
            "NPX_LOG": str(self.log),
            "NPX_MODE": "ok",
            "CURL_CODE": "200",
            **extra,
        }
        return {k: v for k, v in env.items() if v is not None}

    def run_script(self, *args, **extra):
        return subprocess.run(["sh", str(self.base / "local-ha-mcp/ha-mcp.sh"), *args], env=self.env(**extra),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def out(self, result):
        return result.stdout + result.stderr

    def first(self, result):
        lines = (result.stdout or result.stderr).splitlines()
        return lines[0] if lines else ""

    def logged(self, key):
        """Each value the npx stub logged as key=value."""
        text = self.log.read_text() if self.log.exists() else ""
        return [w.split("=", 1)[1] for w in text.split() if w.startswith(key + "=")]

    def bridge_url(self):
        urls = self.logged("url")
        self.assertEqual(len(urls), 1, urls)
        return urls[0]

    def assert_loopback_relay(self, url):
        parts = urlsplit(url)
        self.assertEqual(parts.hostname, "127.0.0.1")
        self.assertNotEqual(parts.port, self.ha.server_address[1], "the bridge went straight to the host")
        self.assertEqual(parts.path, "/mcp_server/sse")
        return parts.port

    def assert_closes(self, port, within=5):
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if not listening(port):
                return
            time.sleep(0.2)
        self.fail(f"the relay on 127.0.0.1:{port} outlived the bridge")

    def test_serve_runs_the_bridge_through_a_loopback_relay_to_the_host(self):
        result = self.run_script("serve", NPX_MODE="fetch")

        self.assertEqual(result.returncode, 0, self.out(result))
        port = self.assert_loopback_relay(self.bridge_url())
        self.assertEqual(self.logged("body"), [BODY], self.log.read_text())
        self.assert_closes(port)

    def test_the_relay_dies_with_a_killed_bridge(self):
        proc = subprocess.Popen(["sh", str(self.base / "local-ha-mcp/ha-mcp.sh")], env=self.env(NPX_MODE="hang"),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        deadline = time.monotonic() + 10
        while not self.logged("url") and time.monotonic() < deadline:
            time.sleep(0.1)
        port = self.assert_loopback_relay(self.bridge_url())
        self.assertTrue(listening(port), "no relay while the bridge runs")

        proc.send_signal(signal.SIGKILL)
        proc.wait()

        self.assert_closes(port)

    def test_check_goes_through_the_relay_and_leaves_none_behind(self):
        result = self.run_script("check")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn(f"node   : {self.stubs}/node v26.0.0", result.stdout)
        self.assert_closes(self.assert_loopback_relay(self.bridge_url()))

    def test_ha_mcp_node_picks_the_node(self):
        other = self.base / "other"
        write_exec(other / "node", NODE)
        write_exec(other / "npx", NPX)

        result = self.run_script("check", HA_MCP_NODE=str(other / "node"))

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.logged("node"), [f"{other}/node"])

    def test_a_relay_that_cannot_start_fails_without_running_the_bridge(self):
        result = self.run_script("status", HA_MCP_PYTHON=str(self.base / "no-such-python"))

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("relay did not start", self.first(result))
        self.assertFalse(self.log.exists(), "the bridge ran with no relay")

    def test_help_declares_the_status_contract_to_the_sweep(self):
        result = self.run_script("help")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn("local-health-check reads these codes", result.stdout)

    def test_status_of_a_working_bridge_exits_0_without_waiting(self):
        started = time.monotonic()
        result = self.run_script("status")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertTrue(self.first(result).startswith("ha-mcp: ok, 2 tools"), self.out(result))
        self.assertLess(time.monotonic() - started, 10)

    def test_status_names_a_host_that_refuses_the_relay(self):
        closed = free_port()

        result = self.run_script("status", NPX_MODE="fetch",
                                 HA_MCP_URL=f"http://127.0.0.1:{closed}/mcp_server/sse", CURL_CODE="000")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn(f"Home Assistant does not answer at 127.0.0.1:{closed}", self.first(result))
        self.assertNotIn("Local Network", self.out(result))

    def test_status_names_local_network_when_the_relay_is_refused_and_curl_is_not(self):
        write_exec(self.base / "refused-python", REFUSED_RELAY)

        result = self.run_script("status", NPX_MODE="fetch", HA_MCP_PYTHON=str(self.base / "refused-python"))

        self.assertEqual(result.returncode, 1, self.out(result))
        first = self.first(result)
        self.assertIn("Local Network", first)
        self.assertIn("curl gets HTTP 200", first)
        self.assertNotIn(TOKEN, self.out(result))

    def test_status_of_a_bridge_that_never_answers_ends_inside_the_sweep_bound(self):
        started = time.monotonic()
        result = self.run_script("status", NPX_MODE="silent")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn(f"no handshake or no tool list from {self.url}", self.first(result))
        self.assertLess(time.monotonic() - started, 30)

    def test_check_names_the_cause_on_stderr(self):
        result = self.run_script("check", NPX_MODE="ehost")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("ha-mcp check: FAILED -- ", result.stderr)

    def test_status_with_nothing_configured_exits_3(self):
        for name, extra in (("no url", {"HA_MCP_URL": None}),
                            ("example url", {"HA_MCP_URL": "http://change-me:8123/mcp_server/sse"}),
                            ("no token", {"HA_TOKEN": None})):
            with self.subTest(name):
                result = self.run_script("status", **extra)
                self.assertEqual(result.returncode, 3, self.out(result))
                self.assertFalse(self.log.exists(), "status ran the bridge with nothing to check")


if __name__ == "__main__":
    unittest.main()
