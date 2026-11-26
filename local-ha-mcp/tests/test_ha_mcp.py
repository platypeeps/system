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
import importlib.util
import os
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
  modes)
    # The modes of the files its stdout (fd 3 here) and stderr reach.
    "$TEST_PYTHON" -c 'import os; print("outmode=%o errmode=%o" % (os.fstat(3).st_mode & 0o777, os.fstat(2).st_mode & 0o777))' 3>&1 >> "$NPX_LOG" ;;
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
  stall)
    # A registry stall before mcp-remote reads stdin: EOF changes nothing,
    # and a grandchild holds on too.
    sleep 120 &
    echo $! > "$NPX_LOG.child"
    wait
    exit 0 ;;
  registry)
    echo "npm error network request to https://registry.npmjs.org/mcp-remote failed, reason: connect EHOSTUNREACH 192.0.2.20:443" >&2
    exit 1 ;;
  wrongid)
    # Both replies, under ids the probe never sent.
    printf '%s\n' '{"jsonrpc":"2.0","id":7,"result":{"serverInfo":{"name":"x","version":"1"}}}' \
      '{"jsonrpc":"2.0","id":8,"result":{"tools":[{"name":"a"}]}}'
    cat >/dev/null
    exit 0 ;;
  malformed)
    printf '%s\n' '[1, 2]' '"text"' '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":"not an object"}}' \
      '{"jsonrpc":"2.0","id":2,"result":["not", "an", "object"]}'
    cat >/dev/null
    exit 0 ;;
esac
SERVER_NAME=${SERVER_NAME:-home-assistant}
while IFS= read -r line; do
  case "$line" in
    *'"method":"initialize"'*)
      printf '%s\n' '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"'"$SERVER_NAME"'","version":"1.0"},"capabilities":{"tools":{}}}}' ;;
    *'"method":"tools/list"'*)
      echo '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"a"},{"name":"b"}]}}' ;;
  esac
done
"""

NODE = "#!/bin/sh\necho v26.0.0\n"

# Prints $CURL_BODY, unless -o sends it elsewhere, then $CURL_CODE as
# -w '\n%{http_code}' does; 000 is no answer, exit 7. The defaults are
# Home Assistant's answer to /api/ with no token.
CURL = r"""#!/bin/sh
case " $* " in *" -o "*) ;; *) printf '%s\n' "$CURL_BODY" ;; esac
printf '%s' "$CURL_CODE"
[ "$CURL_CODE" != 000 ] || exit 7
"""

# The real relay, after logging the mode of the file its stderr reaches.
MODE_RELAY = r"""#!/bin/sh
"$TEST_PYTHON" -c 'import os; print("relaymode=%o" % (os.fstat(2).st_mode & 0o777))' >> "$NPX_LOG"
exec "$TEST_PYTHON" "$@"
"""

# mktemp failing as on a full or read-only disk: no path, exit 1.
# MKTEMP_FAIL=file fails a file, dir a folder; anything else is mktemp.
FAILING_MKTEMP = r"""#!/bin/sh
case "$MKTEMP_FAIL:$1" in
  file:|file:-t|dir:-d) echo "mktemp: mkstemp failed: Read-only file system" >&2; exit 1 ;;
esac
exec /usr/bin/mktemp "$@"
"""

# Marks that something ran it, then fails: no relay may start.
NO_RELAY = r"""#!/bin/sh
echo "relay ran" > "$NPX_LOG.relay"
exit 1
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
            "CURL_CODE": "401",
            "CURL_BODY": "401: Unauthorized",
            **extra,
        }
        return {k: v for k, v in env.items() if v is not None}

    def run_script(self, *args, cwd=None, **extra):
        return subprocess.run(["sh", str(self.base / "local-ha-mcp/ha-mcp.sh"), *args], env=self.env(**extra),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
                              preexec_fn=lambda: os.umask(0o022), cwd=cwd)

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

    def test_status_names_local_network_when_curl_reaches_the_host(self):
        write_exec(self.base / "refused-python", REFUSED_RELAY)

        result = self.run_script("status", NPX_MODE="fetch", HA_MCP_PYTHON=str(self.base / "refused-python"))

        self.assertEqual(result.returncode, 1, self.out(result))
        first = self.first(result)
        self.assertIn("Local Network", first)
        self.assertIn("curl gets HTTP 401", first)
        self.assertNotIn(TOKEN, self.out(result))

    def test_status_of_a_bridge_that_never_answers_ends_inside_the_sweep_bound(self):
        started = time.monotonic()
        result = self.run_script("status", NPX_MODE="silent")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn(f"no handshake or no tool list from {self.url}", self.first(result))
        self.assertLess(time.monotonic() - started, 30)

    def test_status_ends_a_bridge_that_ignores_stdin_inside_the_sweep_bound(self):
        """A cold npm cache can stall npx before mcp-remote reads stdin (round 1)."""
        started = time.monotonic()
        result = self.run_script("status", NPX_MODE="stall")
        took = time.monotonic() - started

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertLess(took, 30)
        self.assertIn("did not finish", self.first(result))
        child = int((self.base / "npx.log.child").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            os.kill(child, signal.SIGKILL)
            self.fail("the stalled bridge's child outlived status")

    def test_an_answer_that_is_not_home_assistant_is_not_local_network(self):
        """A proxy or another server on the port answers curl too (round 3)."""
        write_exec(self.base / "refused-python", REFUSED_RELAY)
        for code, body in (("200", "<html>a proxy</html>"), ("502", "Bad Gateway")):
            with self.subTest(code):
                result = self.run_script("status", NPX_MODE="fetch", HA_MCP_PYTHON=str(self.base / "refused-python"),
                                         CURL_CODE=code, CURL_BODY=body)
                self.assertEqual(result.returncode, 1, self.out(result))
                self.assertNotIn("Local Network", self.out(result))
                self.assertIn("Home Assistant does not answer at 127.0.0.1", self.first(result))

    def test_replies_count_only_under_the_ids_the_probe_sent(self):
        result = self.run_script("status", NPX_MODE="wrongid")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("no handshake or no tool list", self.first(result))

    def test_a_failed_mktemp_fails_cleanly_and_spares_the_cwd(self):
        """A failed mktemp left an empty path; the trap then removed ./.err (round 3).

        macOS mktemp ignores an unwritable TMPDIR, so a stub fails instead.
        """
        write_exec(self.stubs / "mktemp", FAILING_MKTEMP)
        cwd = self.base / "cwd"
        cwd.mkdir()
        names = (".err", ".relay", ".done", "url")
        for verb, fail in (("check", "file"), ("status", "file"), ("check", "dir"), ("status", "dir"),
                           ("serve", "dir")):
            with self.subTest(verb=verb, fail=fail):
                for name in names:
                    (cwd / name).write_text("keep")
                result = self.run_script(verb, cwd=cwd, MKTEMP_FAIL=fail)
                self.assertEqual(result.returncode, 1, self.out(result))
                self.assertEqual([(cwd / n).read_text() if (cwd / n).exists() else None for n in names],
                                 ["keep"] * len(names))
                self.assertIn(f"cannot make a temporary {'file' if fail == 'file' else 'folder'}", self.out(result))
                self.assertFalse(self.log.exists(), "the bridge ran with no probe files")

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

    def test_a_registry_failure_is_not_called_local_network(self):
        result = self.run_script("status", NPX_MODE="registry", CURL_CODE="200")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertNotIn("Local Network", self.out(result))
        self.assertIn("npx could not fetch or start mcp-remote", self.first(result))

    def test_every_file_the_token_can_reach_is_private(self):
        """st_bounded names the command, bearer header too, in a timeout (round 2)."""
        write_exec(self.base / "mode-python", MODE_RELAY)

        result = self.run_script("check", NPX_MODE="modes", HA_MCP_PYTHON=str(self.base / "mode-python"))

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual((self.logged("outmode"), self.logged("errmode"), self.logged("relaymode")),
                         (["600"], ["600"], ["600"]))

    def test_an_https_url_goes_to_the_bridge_unchanged_with_no_relay(self):
        """A relay would rewrite the host, and TLS checks the name (round 2)."""
        write_exec(self.base / "no-relay", NO_RELAY)
        url = "https://ha.example.test/mcp_server/sse"
        for verb, mode in (("check", "ok"), ("serve", "silent")):
            with self.subTest(verb):
                self.log.unlink(missing_ok=True)
                result = self.run_script(verb, NPX_MODE=mode, HA_MCP_URL=url,
                                         HA_MCP_PYTHON=str(self.base / "no-relay"))
                self.assertEqual(result.returncode, 0, self.out(result))
                self.assertEqual(self.bridge_url(), url)
                self.assertFalse((self.base / "npx.log.relay").exists(), "an https URL started a relay")

    def test_a_malformed_reply_is_a_failed_probe_not_a_traceback(self):
        result = self.run_script("status", NPX_MODE="malformed")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertNotIn("Traceback", self.out(result))
        self.assertIn("no handshake or no tool list", self.first(result))

    def test_a_server_name_cannot_add_a_line(self):
        result = self.run_script("check", SERVER_NAME="evil\\nOK\\u001b[2J")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(result.stdout.count("\n"), 5, result.stdout)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("server : evil?OK?[2J 1.0", result.stdout)

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


def load_relay():
    spec = importlib.util.spec_from_file_location("ha_mcp_relay", FOLDER / "relay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Rewrite(unittest.TestCase):
    """relay.py's target and loopback URL for each URL shape."""

    def setUp(self):
        self.relay = load_relay()

    def test_the_target_takes_the_url_port_or_80(self):
        cases = {
            "http://ha.example.test:8123/mcp_server/sse": ("ha.example.test", 8123),
            "http://ha.example.test/mcp_server/sse": ("ha.example.test", 80),
        }
        for url, want in cases.items():
            with self.subTest(url):
                self.assertEqual(self.relay.target(url), want)

    def test_the_rewrite_keeps_scheme_path_and_query(self):
        self.assertEqual(self.relay.rewrite("http://ha.example.test:8123/mcp_server/sse?x=1", 40001),
                         "http://127.0.0.1:40001/mcp_server/sse?x=1")

    def test_a_url_with_no_host_or_another_scheme_is_refused(self):
        for url in ("https://ha.example.test/mcp_server/sse", "ws://ha.example.test/mcp_server/sse",
                    "http:///mcp_server/sse", "ha.example.test:8123"):
            with self.subTest(url):
                with self.assertRaises(ValueError):
                    self.relay.target(url)


class Handshake(unittest.TestCase):
    """relay.py binds port 0, prints its URL, and serves on that port."""

    def test_the_first_line_names_the_port_it_listens_on(self):
        upstream = free_port()  # loopback: the probe below opens no outbound connection
        proc = subprocess.Popen([sys.executable, str(FOLDER / "relay.py"),
                                 f"http://127.0.0.1:{upstream}/mcp_server/sse", str(os.getpid())],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)

        line = proc.stdout.readline().strip()

        parts = urlsplit(line)
        self.assertEqual((parts.scheme, parts.hostname, parts.path), ("http", "127.0.0.1", "/mcp_server/sse"))
        self.assertNotEqual(parts.port, upstream)
        self.assertTrue(listening(parts.port), line)

    def test_a_relay_whose_parent_is_already_gone_exits(self):
        result = subprocess.run([sys.executable, str(FOLDER / "relay.py"),
                                 "http://192.0.2.10:8123/mcp_server/sse", "1"],
                                capture_output=True, text=True, timeout=10)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
