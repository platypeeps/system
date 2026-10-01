"""Tests for kev.sh.

Each case runs a throwaway copy of the script beside a copy of lib/config.sh,
the way the tree is, with HOME and SYSTEM_TOOLS_CONFIG pointed at directories
of its own. `launchctl`, `uv` and `git` are stubs on PATH that log their
arguments, so no case asks the real launchd anything or downloads anything.
`status` runs against a loopback stub server on a free port.
"""

import http.server
import os
import pathlib
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = FOLDER / "kev.sh"
TEMPLATE = FOLDER / "kev.plist.template"
LIB_CONFIG = FOLDER.parent / "lib" / "config.sh"

# launchd holds no label: `launchctl print` fails, anything else succeeds.
LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$KEV_TEST_CALLS"
[ "$1" != print ] || exit 113
exit 0
"""
# Logs its working directory and arguments; `git clone` makes the target.
STUB = """#!/bin/sh
echo "$(basename "$0") [$(pwd)] $*" >> "$KEV_TEST_CALLS"
if [ "$(basename "$0")" = git ] && [ "$1" = clone ]; then
  mkdir -p "$3/.git"
  : > "$3/pyproject.toml"
fi
exit 0
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Answer(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b'{"object": "list", "models": [{"id": "kev-latest"}]}'
    seen = []

    def do_GET(self):  # noqa: N802 - the handler's API
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args):
        pass


class KevCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kev-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        folder = self.tmp / "local-kev"
        folder.mkdir()
        shutil.copy(SCRIPT, folder / "kev.sh")
        shutil.copy(TEMPLATE, folder / "kev.plist.template")
        (self.tmp / "lib").mkdir()
        shutil.copy(LIB_CONFIG, self.tmp / "lib" / "config.sh")
        self.folder = folder
        self.script = folder / "kev.sh"
        self.config = self.tmp / "config"
        (self.config / "kev").mkdir(parents=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.checkout = self.tmp / "kev-checkout"
        self.calls = self.tmp / "calls.txt"
        self.calls.write_text("")
        stubs = self.tmp / "stub-bin"
        stubs.mkdir()
        for name, text in (("launchctl", LAUNCHCTL), ("uv", STUB), ("git", STUB)):
            path = stubs / name
            path.write_text(text)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.path = f"{stubs}:/usr/bin:/bin"
        self.port = free_port()

    def installed(self):
        (self.checkout / ".git").mkdir(parents=True)
        (self.checkout / "pyproject.toml").write_text("")

    def serve(self, status=200, body=None):
        handler = type("Handler", (Answer,), {"status": status, "seen": []})
        if body is not None:
            handler.body = body
        server = http.server.HTTPServer(("127.0.0.1", self.port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return handler

    def run_script(self, *args, env=None):
        base = {"PATH": self.path, "HOME": str(self.home),
                "SYSTEM_TOOLS_CONFIG": str(self.config),
                "KEV_TEST_CALLS": str(self.calls),
                "KEV_DIR": str(self.checkout),
                "KEV_PORT": str(self.port)}
        base.update(env or {})
        base = {key: value for key, value in base.items() if value is not None}
        return subprocess.run(["sh", str(self.script), *args], env=base,
                              capture_output=True, text=True, timeout=60)


class TheEntrypoint(KevCase):
    def test_help_exits_0_and_names_every_subcommand(self):
        result = self.run_script("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for word in ("install", "serve", "agent-install", "agent-uninstall",
                     "status", "test"):
            self.assertIn(word, result.stdout)
        self.assertIn("local-health-check reads these codes", result.stdout)

    def test_no_argument_prints_usage_and_exits_1(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stderr)

    def test_unknown_subcommand_exits_1(self):
        self.assertEqual(self.run_script("bogus").returncode, 1)


class TheDefaultCheckout(KevCase):
    def test_unset_kev_dir_runs_the_checkout_beside_the_wrapper(self):
        beside = self.folder / "kev"
        beside.mkdir()
        (beside / "pyproject.toml").write_text("")
        result = self.run_script("serve", env={"KEV_DIR": None})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"uv [{beside}] run", self.calls.read_text())


class TheStatus(KevCase):
    def test_no_checkout_is_nothing_to_check(self):
        result = self.run_script("status")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("not installed", result.stdout)

    def test_nothing_listening_is_nothing_to_check(self):
        self.installed()
        result = self.run_script("status")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("not running", result.stdout)

    def test_a_model_list_is_healthy(self):
        self.installed()
        handler = self.serve()
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(handler.seen, [("/v1/models", None)])

    def test_a_listener_that_never_answers_is_broken(self):
        # Accepted but never answered: curl times out (exit 28), and a hung
        # server is a broken one, not one that is not running.
        self.installed()
        hung = socket.socket()
        hung.bind(("127.0.0.1", self.port))
        hung.listen(1)
        self.addCleanup(hung.close)
        result = self.run_script("status")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("did not answer", result.stdout)

    def test_an_error_answer_is_broken(self):
        self.installed()
        self.serve(status=500, body=b'{"error": "loading"}')
        result = self.run_script("status")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("HTTP 500", result.stdout)

    def test_something_else_on_the_port_is_broken(self):
        self.installed()
        self.serve(body=b"<html>not kev</html>")
        self.assertEqual(self.run_script("status").returncode, 1)

    def test_the_key_goes_as_a_bearer_token(self):
        self.installed()
        handler = self.serve()
        self.run_script("status", env={"KEV_API_KEY": "test-key"})
        self.assertEqual(handler.seen, [("/v1/models", "Bearer test-key")])

    def test_a_placeholder_key_is_no_key(self):
        self.installed()
        handler = self.serve()
        self.run_script("status", env={"KEV_API_KEY": "change-me"})
        self.assertEqual(handler.seen, [("/v1/models", None)])

    def test_the_port_comes_from_the_config_file(self):
        self.installed()
        handler = self.serve()
        (self.config / "kev" / ".env").write_text(f'KEV_PORT="{self.port}"\n')
        env = {"PATH": self.path, "HOME": str(self.home),
               "SYSTEM_TOOLS_CONFIG": str(self.config),
               "KEV_TEST_CALLS": str(self.calls), "KEV_DIR": str(self.checkout)}
        result = subprocess.run(["sh", str(self.script), "status"], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(handler.seen), 1)


class TheAgent(KevCase):
    def test_agent_install_renders_the_template_and_bootstraps(self):
        self.installed()
        (self.config / "kev" / ".env").write_text(
            'SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n')
        result = self.run_script("agent-install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plist = self.home / "Library" / "LaunchAgents" / "example.test.kev.plist"
        text = plist.read_text()
        self.assertIn("<string>example.test.kev</string>", text)
        self.assertIn(f"<string>{self.folder}/kev.sh</string>", text)
        self.assertIn(f"<string>{self.home}</string>", text)
        self.assertNotIn("@", text.replace("<?xml", ""))
        calls = self.calls.read_text()
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} {plist}", calls)
        self.assertIn(f"launchctl kickstart gui/{os.getuid()}/example.test.kev", calls)
        self.assertTrue((self.folder / "logs").is_dir())

    def test_the_agent_reads_the_config_root_agent_install_read(self):
        # run_script points SYSTEM_TOOLS_CONFIG at a root of its own; launchd
        # passes the agent only what the plist names, so the plist names it.
        self.installed()
        result = self.run_script("agent-install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = (self.home / "Library" / "LaunchAgents"
                / "local.system-tools.kev.plist").read_text()
        self.assertIn("<key>SYSTEM_TOOLS_CONFIG</key>\n"
                      f"        <string>{self.config}</string>", text)

    def test_agent_install_needs_a_checkout(self):
        result = self.run_script("agent-install")
        self.assertEqual(result.returncode, 3)
        self.assertIn("kev.sh install", result.stderr)
        self.assertNotIn("bootstrap", self.calls.read_text())

    def test_agent_uninstall_removes_the_plist(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        plist = agents / "local.system-tools.kev.plist"
        plist.write_text("<plist/>\n")
        result = self.run_script("agent-uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(plist.exists())


class TheInstallAndServe(KevCase):
    def test_install_clones_then_syncs_the_serve_extra(self):
        result = self.run_script("install",
                                 env={"KEV_REPO_URL": "https://example.test/kev.git"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertTrue(calls[0].startswith("git "), calls)
        self.assertIn(f"clone https://example.test/kev.git {self.checkout}", calls[0])
        self.assertEqual(calls[1], f"uv [{self.checkout}] sync --extra serve")

    def test_install_fast_forwards_an_existing_checkout(self):
        self.installed()
        self.assertEqual(self.run_script("install").returncode, 0)
        calls = self.calls.read_text().splitlines()
        self.assertIn(f"-C {self.checkout} pull --ff-only", calls[0])

    def test_serve_binds_loopback_with_the_pinned_model(self):
        self.installed()
        result = self.run_script("serve")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.calls.read_text().strip(),
            f"uv [{self.checkout}] run --extra serve python -m kev.serve "
            f"--run jaredpalmer/kev-4b@v1.0 --host 127.0.0.1 --port {self.port}")

    def test_serve_takes_another_checkpoint(self):
        self.installed()
        self.run_script("serve", env={"KEV_MODEL": "example/kev-other"})
        self.assertIn("--run example/kev-other ", self.calls.read_text())

    def test_serve_needs_a_checkout(self):
        self.assertEqual(self.run_script("serve").returncode, 3)
        self.assertEqual(self.calls.read_text(), "")


if __name__ == "__main__":
    unittest.main()
