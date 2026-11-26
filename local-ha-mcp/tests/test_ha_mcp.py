"""ha-mcp.sh: the node it runs and the `status` the health check sweeps (sd:10894).

macOS keys a Local Network grant to a binary's signer. Homebrew's node is
ad-hoc signed, so each rebuild lost the bridge's grant and every call failed
with EHOSTUNREACH while curl to the same host answered. The bridge now runs
~/.local/bin/node, the nodejs.org build machine-setup links, unless
HA_MCP_NODE names another; and `status` tells the nightly health check when
the bridge fails, naming Local Network when node is refused and curl is not.

Each case runs a copy of the script against stubs: an npx that speaks the
MCP handshake or fails as mcp-remote does, and a curl. Nothing reaches a
network; the token is a fake.
"""

import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

URL = "http://192.0.2.10:8123/mcp_server/sse"
TOKEN = "example-token-not-real"

# Logs the node PATH resolves and its own path, then answers initialize and
# tools/list until stdin closes; NPX_MODE=ehost fails as mcp-remote does
# when macOS refuses node the LAN, and NPX_MODE=silent answers nothing.
NPX = r"""#!/bin/sh
echo "npx=$0 node=$(command -v node)" >> "$NPX_LOG"
if [ "$NPX_MODE" = ehost ]; then
  echo "fetch failed: connect EHOSTUNREACH 192.0.2.10:8123" >&2
  exit 1
fi
if [ "$NPX_MODE" = silent ]; then
  cat >/dev/null
  exit 0
fi
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


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


class Bridge(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        shutil.copytree(FOLDER, self.base / "local-ha-mcp", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, self.base / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.home = self.base / "home"
        self.official = self.home / ".local/bin"
        self.stubs = self.base / "stubs"
        for folder in (self.official, self.stubs):
            write_exec(folder / "node", NODE)
            write_exec(folder / "npx", NPX)
        write_exec(self.base / "curl", CURL)
        self.log = self.base / "npx.log"

    def run_script(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "SYSTEM_TOOLS_CONFIG": str(self.base / "config"),
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "TMPDIR": str(self.base),
            "HA_TOKEN": TOKEN,
            "HA_MCP_URL": URL,
            "HA_MCP_CURL": str(self.base / "curl"),
            "NPX_LOG": str(self.log),
            "NPX_MODE": "ok",
            "CURL_CODE": "200",
            **extra,
        }
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(["sh", str(self.base / "local-ha-mcp/ha-mcp.sh"), *args], env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def out(self, result):
        return result.stdout + result.stderr

    def first(self, result):
        lines = (result.stdout or result.stderr).splitlines()
        return lines[0] if lines else ""

    def test_the_default_node_is_the_linked_nodejs_org_node(self):
        result = self.run_script("check")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn(f"node   : {self.official}/node v26.0.0", result.stdout)
        self.assertEqual(self.log.read_text().split(), [f"npx={self.official}/npx", f"node={self.official}/node"])

    def test_path_node_serves_a_machine_without_the_link(self):
        shutil.rmtree(self.official)

        result = self.run_script("check")

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertIn(f"node   : {self.stubs}/node v26.0.0", result.stdout)

    def test_ha_mcp_node_wins_over_the_link(self):
        result = self.run_script("check", HA_MCP_NODE=str(self.stubs / "node"))

        self.assertEqual(result.returncode, 0, self.out(result))
        self.assertEqual(self.log.read_text().split(), [f"npx={self.stubs}/npx", f"node={self.stubs}/node"])

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

    def test_status_names_local_network_when_node_is_refused_and_curl_is_not(self):
        started = time.monotonic()
        result = self.run_script("status", NPX_MODE="ehost", CURL_CODE="200")

        self.assertEqual(result.returncode, 1, self.out(result))
        first = self.first(result)
        self.assertIn("Local Network", first)
        self.assertIn(f"{self.official}/node", first)
        self.assertIn("curl gets HTTP 200", first)
        self.assertNotIn(TOKEN, self.out(result))
        self.assertLess(time.monotonic() - started, 10)

    def test_status_names_a_silent_host_when_curl_gets_no_answer_either(self):
        result = self.run_script("status", NPX_MODE="ehost", CURL_CODE="000")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("Home Assistant does not answer at http://192.0.2.10:8123", self.first(result))
        self.assertNotIn("Local Network", self.out(result))

    def test_status_of_a_bridge_that_never_answers_ends_inside_the_sweep_bound(self):
        started = time.monotonic()
        result = self.run_script("status", NPX_MODE="silent")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn(f"no handshake or no tool list from {URL}", self.first(result))
        self.assertLess(time.monotonic() - started, 30)

    def test_check_names_the_cause_too(self):
        result = self.run_script("check", NPX_MODE="ehost")

        self.assertEqual(result.returncode, 1, self.out(result))
        self.assertIn("Local Network", result.stderr)

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
