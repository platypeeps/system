"""`uv-prune` empties uv's cache at login without breaking a running uv.

sd:1494: `~/.cache/uv` held 45G, and `uv cache prune` waited an hour for the
cache lock, because long-lived uv servers hold it while they run. At login
only one of them is up: the Google Workspace MCP, a KeepAlive LaunchAgent.
`uv-prune` pauses that agent, prunes only when no other uv process is left,
and starts the agent again whatever the prune did. It never passes --force:
uv's own lock is what keeps a concurrent uv safe.

Every outside command is a stub on PATH that appends its argv to one log, so
the order of pause, prune and resume is read from that log. `pgrep` answers
"a uv is running" while the paused agent is loaded or while a second, foreign
uv is declared. Nothing here touches the real launchd or the real cache.
"""

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "maintenance.sh"
# The default pause label: SYSTEM_TOOLS_LABEL_PREFIX is unset in these runs.
LABEL = "local.system-tools.google-workspace-mcp"

STUBS = {
    "uv": """\
#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
case "$1 $2" in
  "cache dir") echo "$STUB_CACHE" ;;
  "cache prune") exit "${STUB_PRUNE_RC:-0}" ;;
esac
exit 0
""",
    "launchctl": """\
#!/bin/sh
echo "launchctl $*" >> "$STUB_LOG"
case "$1" in
  print) [ -f "$STUB_LOADED" ] ;;
  bootout) rm -f "$STUB_LOADED" ;;
  bootstrap) [ -z "$STUB_BOOTSTRAP_FAILS" ] || exit 5; : > "$STUB_LOADED" ;;
esac
""",
    "pgrep": """\
#!/bin/sh
# STUB_UVX_POLLS: a paused agent's `uvx` answers that many polls, then exits.
if [ "$2" = uvx ] && [ -n "$STUB_UVX_POLLS" ]; then
  n=$(cat "$STUB_UVX_COUNT" 2>/dev/null || echo "$STUB_UVX_POLLS")
  [ "$n" -gt 0 ] || exit 1
  echo $((n - 1)) > "$STUB_UVX_COUNT"
  exit 0
fi
[ -f "$STUB_LOADED" ] || [ -n "$STUB_FOREIGN_UV" ]
""",
}


class UvPrune(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.log = self.root / "log"
        self.log.write_text("")
        self.loaded = self.root / "loaded"
        self.state = self.root / ".local" / "state" / "uv-prune" / "paused"
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STUB_LOG": str(self.log),
            "STUB_CACHE": str(self.cache),
            "STUB_LOADED": str(self.loaded),
            "UV_PRUNE_WAIT": "1",
        }

    def run_prune(self, **extra):
        env = dict(self.env, **extra)
        return subprocess.run(["sh", str(ENTRYPOINT), "uv-prune"], env=env,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_pauses_the_agent_prunes_then_resumes_it(self):
        self.loaded.touch()
        result = self.run_prune()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [c for c in self.calls() if c != "uv cache dir"]
        uid = os.getuid()
        self.assertEqual(calls, [
            f"launchctl print gui/{uid}/{LABEL}",
            f"launchctl bootout gui/{uid}/{LABEL}",
            "uv cache prune",
            f"launchctl print gui/{uid}/{LABEL}",
            f"launchctl bootstrap gui/{uid} {self.root}/Library/LaunchAgents/{LABEL}.plist",
        ])
        self.assertTrue(self.loaded.exists())
        self.assertIn("MB ->", result.stdout)
        self.assertFalse(self.state.exists())

    def test_a_paused_uvx_is_waited_out_like_uv(self):
        self.loaded.touch()
        result = self.run_prune(STUB_UVX_POLLS="2", STUB_UVX_COUNT=str(self.root / "uvx-count"),
                                UV_PRUNE_WAIT="5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uv cache prune", self.calls())

    def test_a_foreign_uv_skips_the_prune_and_still_resumes_the_agent(self):
        self.loaded.touch()
        result = self.run_prune(STUB_FOREIGN_UV="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("uv cache prune", self.calls())
        self.assertTrue(any(c.startswith("launchctl bootstrap") for c in self.calls()))
        self.assertIn("skipped", result.stdout)

    def test_a_failed_prune_still_resumes_the_agent_and_reports_the_failure(self):
        # Exit 0: the agent's exit status drives launchd's relaunch, and a
        # relaunch after a failed prune would pause the MCP again in a loop.
        self.loaded.touch()
        result = self.run_prune(STUB_PRUNE_RC="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prune exit 2", result.stdout)
        self.assertTrue(any(c.startswith("launchctl bootstrap") for c in self.calls()))
        self.assertFalse(self.state.exists())

    def test_an_agent_that_does_not_start_again_fails_the_run_and_names_it(self):
        # A failed bootstrap leaves the agent unloaded, and KeepAlive cannot
        # bring back an agent launchd no longer knows. That outage must not
        # pass as a successful cleanup: retry, then fail naming the remedy.
        self.loaded.touch()
        result = self.run_prune(STUB_BOOTSTRAP_FAILS="1", UV_PRUNE_RESUME_DELAY="0")
        self.assertEqual(result.returncode, 1)
        attempts = [c for c in self.calls() if c.startswith("launchctl bootstrap")]
        self.assertEqual(len(attempts), 3)
        self.assertIn(LABEL, result.stderr)
        self.assertIn("launchctl bootstrap", result.stderr)
        # The unloaded agent stays on record for the next run to restore.
        self.assertEqual(self.state.read_text().split(), [LABEL])

    def test_a_recorded_agent_is_restored_first_and_nothing_is_pruned(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(LABEL + "\n")
        result = self.run_prune()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.loaded.exists())
        self.assertNotIn("uv cache prune", self.calls())
        self.assertFalse(any(c.startswith("launchctl bootout") for c in self.calls()))
        self.assertFalse(self.state.exists())

    def test_a_recorded_agent_that_still_does_not_start_keeps_failing(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(LABEL + "\n")
        result = self.run_prune(STUB_BOOTSTRAP_FAILS="1", UV_PRUNE_RESUME_DELAY="0")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("uv cache prune", self.calls())
        self.assertEqual(self.state.read_text().split(), [LABEL])

    def test_a_recorded_agent_that_is_already_loaded_counts_as_restored(self):
        self.loaded.touch()
        self.state.parent.mkdir(parents=True)
        self.state.write_text(LABEL + "\n")
        result = self.run_prune()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("launchctl bootstrap") for c in self.calls()))
        self.assertFalse(self.state.exists())

    def test_an_unwritable_record_pauses_nothing(self):
        # Without a record a failed restart could never be retried, so no
        # agent is unloaded unless its label is on disk first.
        self.loaded.touch()
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        result = self.run_prune(UV_PRUNE_STATE=str(locked / "paused"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith("launchctl bootout") for c in self.calls()))
        self.assertNotIn("uv cache prune", self.calls())
        self.assertIn("cannot record", result.stderr)

    def test_a_failed_update_keeps_the_existing_record(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(LABEL + "\n")
        self.state.parent.chmod(0o500)
        self.addCleanup(self.state.parent.chmod, 0o700)
        result = self.run_prune(STUB_BOOTSTRAP_FAILS="1", UV_PRUNE_RESUME_DELAY="0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state.read_text().split(), [LABEL])

    def render_plist(self, **extra):
        env = dict(self.env, **extra)
        result = subprocess.run(["sh", str(ENTRYPOINT), "uv-prune-plist"], env=env,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return plistlib.loads(result.stdout)

    def test_launchd_runs_it_again_until_it_succeeds(self):
        plist = self.render_plist()
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["ProgramArguments"],
                         ["/bin/sh", str(ENTRYPOINT), "uv-prune"])
        # The record lives under $HOME; set it as the Workspace MCP plist does.
        self.assertEqual(plist["EnvironmentVariables"]["HOME"], str(self.root))
        self.assertEqual(plist["EnvironmentVariables"]["UV_PRUNE_PAUSE"], LABEL)
        self.assertEqual(plist["Label"], "local.system-tools.uv-cache-prune")

    def test_the_label_prefix_names_the_agent_and_the_paused_label(self):
        plist = self.render_plist(SYSTEM_TOOLS_LABEL_PREFIX="org.example")
        self.assertEqual(plist["Label"], "org.example.uv-cache-prune")
        self.assertEqual(plist["EnvironmentVariables"]["UV_PRUNE_PAUSE"],
                         "org.example.google-workspace-mcp")
        self.assertEqual(plist["StandardErrorPath"], "/tmp/org.example.uv-cache-prune.err")

    def test_the_agent_reads_the_config_root_it_was_rendered_under(self):
        # REGRESSION (sd:1959). launchd passes only the environment the plist
        # names, so a non-default SYSTEM_TOOLS_CONFIG was lost and uv-prune
        # read the default root's .env.
        config = str(self.root / "elsewhere")
        plist = self.render_plist(SYSTEM_TOOLS_CONFIG=config)
        self.assertEqual(plist["EnvironmentVariables"]["SYSTEM_TOOLS_CONFIG"], config)
        # Unset, the agent gets the default root lib/config.sh resolves.
        plist = self.render_plist()
        self.assertEqual(plist["EnvironmentVariables"]["SYSTEM_TOOLS_CONFIG"],
                         str(self.root / ".config" / "system"))

    def test_an_agent_that_is_not_loaded_is_neither_paused_nor_started(self):
        result = self.run_prune()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("uv cache prune", calls)
        self.assertFalse(any(c.startswith(("launchctl bootout", "launchctl bootstrap")) for c in calls))
        self.assertFalse(self.loaded.exists())

    def test_the_prune_never_forces_past_the_lock(self):
        self.loaded.touch()
        self.run_prune()
        self.assertFalse(any("--force" in c for c in self.calls()))

    def test_help_names_the_verb(self):
        result = subprocess.run(["sh", str(ENTRYPOINT), "help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("uv-prune", result.stdout)



class ConfigLocation(unittest.TestCase):
    def test_a_missing_conf_names_the_config_path_and_the_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ["PATH"], "HOME": tmp,
                   "SYSTEM_TOOLS_CONFIG": str(Path(tmp) / "config"),
                   "MACHINE_SETUP_STATE": str(Path(tmp) / "no-state")}
            result = subprocess.run(["sh", str(ENTRYPOINT), "check"], env=env,
                                    capture_output=True, text=True, timeout=60)
            want = Path(tmp) / "config" / "maintenance" / "maintenance.conf"
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"copy local-maintenance/maintenance.conf.example to {want}",
                          result.stderr)

if __name__ == "__main__":
    unittest.main()
