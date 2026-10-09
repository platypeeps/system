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
import shutil
import stat
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = pathlib.Path(os.environ.get("TASK_ACTIONS_TEST_SCRIPT") or FOLDER / "task-actions.sh")
LIB_CONFIG = FOLDER.parent / "lib" / "config.sh"

# launchd holds no label: `launchctl print` fails, anything else succeeds.
# Every call is logged so a case can assert the stub answered.
LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$TASK_ACTIONS_TEST_CALLS"
[ "$1" != print ] || exit 113
exit 0
"""
# No Funnel configured: `tailscale funnel status` prints nothing.
TAILSCALE = """#!/bin/sh
echo "tailscale $*" >> "$TASK_ACTIONS_TEST_CALLS"
exit 0
"""


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
        # Not loaded, per the stub: the documented exit 2.
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("launchctl print gui/", self.calls.read_text())
        self.assertIn("/example.test.task-actions", self.calls.read_text())

    def test_default_prefix_without_env_file(self):
        # PIN. No .env and nothing exported: the default prefix.
        result = self.run_script("status")
        self.assertIn("label:  local.system-tools.task-actions", result.stdout)
        self.assertIn("plist:  NOT installed", result.stdout)

    def test_port_from_env_file(self):
        # REGRESSION. TASK_ACTIONS_PORT was read before st_source_env too, so
        # the port help says .env may set was ignored.
        self.write_env('TASK_ACTIONS_PORT="18766"\n')
        result = self.run_script("base-url")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "http://127.0.0.1:18766")

    def test_base_url_does_not_wait_out_the_funnel_timeout(self):
        # REGRESSION (sd:1770). The watchdog's `sleep` inherited the stdout
        # of `$(funnel_url)`, so the substitution waited for the sleep to end:
        # every probe cost the full FUNNEL_TIMEOUT even when tailscale
        # answered at once, and a digest paid it once per button.
        import time
        start = time.monotonic()
        result = self.run_script("base-url",
                                 env={"TASK_ACTIONS_FUNNEL_TIMEOUT": "20"})
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "http://127.0.0.1:8766")
        self.assertLess(elapsed, 10, "base-url waited out the watchdog")

    def test_exported_values_win_over_env_file(self):
        # REGRESSION (Copilot cd43501067e5 on PR 12). st_source_env assigns
        # over the environment, and only OBSIDIAN_VAULT was put back, so .env
        # beat an exported prefix and port; .env.example says exported wins.
        self.write_env('SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n'
                       'TASK_ACTIONS_PORT="18766"\n')
        env = {"SYSTEM_TOOLS_LABEL_PREFIX": "other.example.test",
               "TASK_ACTIONS_PORT": "28766"}
        status = self.run_script("status", env=env)
        self.assertIn("label:  other.example.test.task-actions", status.stdout)
        base = self.run_script("base-url", env=env)
        self.assertEqual(base.returncode, 0, base.stderr)
        self.assertEqual(base.stdout.strip(), "http://127.0.0.1:28766")

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


if __name__ == "__main__":
    unittest.main()
