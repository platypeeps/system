"""Regression tests for task-actions.sh.

Each case runs a throwaway copy of the script, laid out beside a copy of
lib/config.sh the way the tree is, with HOME and SYSTEM_TOOLS_CONFIG pointed
at directories of its own. `launchctl` and `tailscale` are stubs on PATH, so
no case asks the real launchd or tailscaled anything, or registers anything.

REGRESSION means the case fails against the code from before its fix, which
is checkable with TASK_ACTIONS_TEST_SCRIPT:

    TASK_ACTIONS_TEST_SCRIPT=<(git show <sha>:local-task-actions/task-actions.sh) ...
"""

import os
import pathlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = pathlib.Path(os.environ.get("TASK_ACTIONS_TEST_SCRIPT") or FOLDER / "task-actions.sh")
LIB_CONFIG = FOLDER.parent / "lib" / "config.sh"

# launchd holds no label: `launchctl print` fails, anything else succeeds.
# TASK_ACTIONS_TEST_LOADED makes `print` answer, so `status` goes on to probe.
# Every call is logged so a case can assert the stub answered.
LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$TASK_ACTIONS_TEST_CALLS"
[ "$1" != print ] || [ -n "$TASK_ACTIONS_TEST_LOADED" ] || exit 113
exit 0
"""
# No Funnel configured: `tailscale funnel status` prints nothing.
TAILSCALE = """#!/bin/sh
echo "tailscale $*" >> "$TASK_ACTIONS_TEST_CALLS"
exit 0
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LabelPrefixTest(unittest.TestCase):
    """SYSTEM_TOOLS_LABEL_PREFIX and TASK_ACTIONS_PORT from <config>/task-actions/.env."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="task-actions-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        folder = self.tmp / "local-task-actions"
        folder.mkdir()
        shutil.copy(SCRIPT, folder / "task-actions.sh")
        shutil.copy(FOLDER / "task-actions.plist.template", folder)
        (self.tmp / "lib").mkdir()
        shutil.copy(LIB_CONFIG, self.tmp / "lib" / "config.sh")
        self.script = folder / "task-actions.sh"
        self.config = self.tmp / "config"
        (self.config / "task-actions").mkdir(parents=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.calls = self.tmp / "calls.txt"
        stubs = self.tmp / "stub-bin"
        stubs.mkdir()
        for name, text in (("launchctl", LAUNCHCTL), ("tailscale", TAILSCALE)):
            path = stubs / name
            path.write_text(text)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.path = f"{stubs}:/usr/bin:/bin"

    def write_env(self, text):
        (self.config / "task-actions" / ".env").write_text(text)

    def run_script(self, *args, env=None):
        base = {"PATH": self.path, "HOME": str(self.home),
                "SYSTEM_TOOLS_CONFIG": str(self.config),
                "TASK_ACTIONS_TEST_CALLS": str(self.calls)}
        base.update(env or {})
        return subprocess.run(["sh", str(self.script), *args], env=base,
                              capture_output=True, text=True, timeout=60)

    def serve(self, env=None):
        """Start `run` from the copy, on whatever port its config names, and wait until it answers."""
        base = {"PATH": self.path, "HOME": str(self.home),
                "SYSTEM_TOOLS_CONFIG": str(self.config)}
        base.update(env or {})
        server = subprocess.Popen(["sh", str(self.script), "run"], env=base,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        return server

    def answers(self, port):
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return True
            except OSError:
                time.sleep(0.1)
        return False

    def test_status_uses_the_prefix_from_env_file(self):
        # REGRESSION (sd:1944). The prefix was read before st_source_env, so
        # a prefix set only in .env was ignored and status looked for the
        # default label: "plist: NOT installed" for an installed agent.
        self.write_env('SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n')
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "example.test.task-actions.plist").write_text("<plist/>\n")
        result = self.run_script("status")
        self.assertIn("label:  example.test.task-actions", result.stdout)
        self.assertIn("plist:  installed", result.stdout)
        # Not loaded, per the stub: convention 6's "nothing to check" (sd:3552).
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("launchctl print gui/", self.calls.read_text())
        self.assertIn("/example.test.task-actions", self.calls.read_text())

    def test_status_uses_the_prefix_from_shared_env(self):
        # sd:3990. The prefix is kept once in <config>/shared.env.
        (self.config / "shared.env").write_text('SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n')
        result = self.run_script("status")
        self.assertIn("label:  example.test.task-actions", result.stdout)

    def test_help_declares_the_codes_to_the_health_check(self):
        # sd:3552. The sweep runs `status` only for a help text naming it.
        result = self.run_script("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local-health-check reads", result.stdout)

    def test_default_prefix_without_env_file(self):
        # PIN. No .env and nothing exported: the default prefix.
        result = self.run_script("status")
        self.assertIn("label:  local.system-tools.task-actions", result.stdout)
        self.assertIn("plist:  NOT installed", result.stdout)

    def test_port_from_env_file(self):
        # REGRESSION. TASK_ACTIONS_PORT was read before st_source_env too, so
        # the port help says .env may set was ignored.
        port = free_port()
        self.write_env(f'TASK_ACTIONS_PORT="{port}"\n')
        self.serve()
        self.assertTrue(self.answers(port), "run did not bind the port .env names")

    def test_status_does_not_wait_out_the_funnel_timeout(self):
        # REGRESSION (sd:1770). The watchdog's `sleep` inherited the stdout
        # of `$(funnel_url)`, so the substitution waited for the sleep to end:
        # every probe cost the full FUNNEL_TIMEOUT even when tailscale
        # answered at once.
        port = free_port()
        env = {"TASK_ACTIONS_PORT": str(port)}
        self.serve(env)
        self.assertTrue(self.answers(port))
        start = time.monotonic()
        result = self.run_script("status", env={**env, "TASK_ACTIONS_TEST_LOADED": "1",
                                                "TASK_ACTIONS_FUNNEL_TIMEOUT": "20"})
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("public: not configured", result.stdout)
        self.assertLess(elapsed, 10, "status waited out the watchdog")

    def test_exported_values_win_over_env_file(self):
        # REGRESSION (Copilot cd43501067e5 on PR 12). st_source_env assigns
        # over the environment, and only OBSIDIAN_VAULT was put back, so .env
        # beat an exported prefix and port; .env.example says exported wins.
        port = free_port()
        self.write_env('SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n'
                       f'TASK_ACTIONS_PORT="{free_port()}"\n')
        env = {"SYSTEM_TOOLS_LABEL_PREFIX": "other.example.test",
               "TASK_ACTIONS_PORT": str(port)}
        status = self.run_script("status", env=env)
        self.assertIn("label:  other.example.test.task-actions", status.stdout)
        self.serve(env)
        self.assertTrue(self.answers(port), "run bound the .env port over the exported one")

    def test_start_passes_the_config_root_to_the_agent(self):
        # REGRESSION (sd:1959). launchd passes only the environment the plist
        # names, and the plist named PATH and HOME, so an agent started under
        # a non-default SYSTEM_TOOLS_CONFIG ran `run` against the default root
        # and ignored its .env.
        import plistlib
        result = self.run_script("start")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("launchctl bootstrap gui/", self.calls.read_text())
        agent = (self.home / "Library" / "LaunchAgents"
                 / "local.system-tools.task-actions.plist")
        plist = plistlib.loads(agent.read_bytes())
        self.assertEqual(plist["EnvironmentVariables"]["SYSTEM_TOOLS_CONFIG"],
                         str(self.config))

    def test_the_agent_runs_at_standard_priority(self):
        # sd:3175. launchd's Background process type throttles the agent under
        # load, and it serves the dashboard's and the phone's actions. The
        # plist names no ProcessType, so launchd uses Standard.
        import plistlib
        result = self.run_script("start")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        agent = (self.home / "Library" / "LaunchAgents"
                 / "local.system-tools.task-actions.plist")
        self.assertNotIn("ProcessType", plistlib.loads(agent.read_bytes()))


class PageTest(unittest.TestCase):
    """The pages the server sends wear the foundation: its tokens, its theme control, no colour of their own.

    These run the real `run` from the real folder, because the stylesheets
    it serves sit beside it and in the dashboard's static folder.
    """

    RAW_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(|\bpurple\b|\bindigo\b|\bviolet\b")

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="task-actions-page."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "config" / "task-actions").mkdir(parents=True)
        home = self.tmp / "home"
        home.mkdir()
        # A decision note where the digest's links used to flip a status: it
        # must come through every case unchanged (sd:5134).
        vault = self.tmp / "vault"
        (vault / "System" / "Databases" / "Blog Ideas").mkdir(parents=True)
        self.idea = vault / "System" / "Databases" / "Blog Ideas" / "Idea.md"
        self.idea.write_text("---\nstatus: proposed\n---\nbody\n")
        self.addCleanup(lambda: self.assertEqual(
            self.idea.read_text(), "---\nstatus: proposed\n---\nbody\n", "a vault note changed"))
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        # An old brief's link to accept that note, as the digest built it.
        self.old_link = f"{self.base}/task?db=blog&f=Idea&a=accept&t=1&s=00"
        path = os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"])
        self.env = {"PATH": path, "HOME": str(home),
                    "SYSTEM_TOOLS_CONFIG": str(self.tmp / "config"),
                    "TASK_ACTIONS_PORT": str(self.port),
                    "OBSIDIAN_VAULT": str(vault)}
        self.server = subprocess.Popen(
            ["sh", str(SCRIPT), "run"], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.server.wait)
        self.addCleanup(self.server.terminate)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("the server did not start")

    def get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def page(self, url):
        status, headers, body = self.get(url)
        return status, headers, body.decode()

    def test_an_old_decision_link_changes_nothing_and_sends_you_to_tasks(self):
        # REGRESSION (sd:5134). The digest's links flipped a status in the
        # vault note; the decision moved to the dashboard's Tasks page, so an
        # old link, opened or posted, answers 410 and reads no note.
        for path in ("/task", "/open"):
            status, _, page = self.page(self.old_link.replace("/task", path))
            self.assertEqual(status, 410, path)
            self.assertIn("decide on Tasks", page)
        status, _, page = self.post(self.old_link)
        self.assertEqual(status, 410)
        self.assertIn("Nothing changed", page)

    def test_the_page_loads_the_foundation_and_carries_no_style_of_its_own(self):
        _, _, page = self.page(self.old_link)
        self.assertIn('href="/static/tokens.css"', page)
        self.assertIn('src="/static/theme.js"', page)
        self.assertNotIn("style=", page)
        self.assertNotIn("<style", page)
        self.assertNotRegex(page, self.RAW_COLOUR)

    def test_the_theme_control_is_on_the_page(self):
        _, _, page = self.page(self.old_link)
        for choice in ("dark", "light", "system"):
            self.assertIn(f'data-theme-choice="{choice}"', page)

    def test_the_pages_send_a_policy_that_runs_only_their_own_files(self):
        _, headers, _ = self.get(self.old_link)
        policy = headers["Content-Security-Policy"]
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                          "font-src 'self'", "form-action 'self'", "frame-ancestors 'none'"):
            self.assertIn(directive, policy)
        self.assertNotIn("unsafe-inline", policy)

    def test_every_stylesheet_and_script_the_page_names_is_served(self):
        _, _, page = self.page(self.old_link)
        wanted = re.findall(r'(?:href|src)="(/static/[^"]+)"', page)
        self.assertGreaterEqual(len(wanted), 3)
        for name in wanted:
            status, headers, body = self.get(self.base + name)
            self.assertEqual(status, 200, name)
            self.assertTrue(body, name)
            self.assertIn("Content-Security-Policy", headers, name)
        _, _, fonts = self.page(self.base + "/static/fonts.css")
        for face in re.findall(r"url\(([^)]+\.woff2)\)", fonts):
            if "ibm-plex-sans-var" in face:
                self.assertEqual(self.get(f"{self.base}/static/{face}")[0], 200, face)

    def test_status_finds_its_404_marker_on_the_404_page(self):
        # REGRESSION sd:3706: `status` grepped the old `<h2>not found</h2>` after the page moved to `<h1>`.
        marker = re.search(r'"\$pub/x" \| grep -q \'([^\']+)\'', SCRIPT.read_text()).group(1)
        status, _, body = self.page(self.base + "/x")
        self.assertEqual(status, 404)
        self.assertIn(marker, body)

    def test_nothing_outside_the_list_is_served(self):
        for name in ("/static/../task-actions.sh", "/static/%2e%2e/task-actions.sh",
                     "/static/secret", "/static/", "/static/fonts/OFL.txt", "/static/tokens.css/x"):
            self.assertEqual(self.get(self.base + name)[0], 404, name)

    def test_the_stylesheet_names_foundation_tokens_and_no_colour(self):
        _, _, css = self.page(self.base + "/static/confirm.css")
        _, _, tokens = self.page(self.base + "/static/tokens.css")
        self.assertNotRegex(css, self.RAW_COLOUR)
        self.assertNotRegex(css, r"var\([^)]*,", "a var() fallback is a second palette")
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", tokens))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", css))
        self.assertTrue(used)
        self.assertEqual(used - defined, set(), "a token the foundation does not define")

    def post(self, url):
        query = url.split("?", 1)[1].encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + "/task", data=query),
                                        timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read().decode()

    def test_the_answer_says_nothing_changed_with_a_glyph_and_words(self):
        _, _, page = self.page(self.old_link)
        self.assertIn("\u25a0", page)  # warning
        self.assertIn("Nothing changed", page)
        self.assertNotRegex(page, self.RAW_COLOUR)


if __name__ == "__main__":
    unittest.main()
