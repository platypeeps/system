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
  # STUB_LAUNCHD_PIDS: the pids launchd runs as jobs.
  list)
    [ -z "$STUB_LIST_FAILS" ] || exit 1
    printf 'PID\tStatus\tLabel\n-\t0\texample.idle\n'
    for p in $STUB_LAUNCHD_PIDS; do printf '%s\t0\texample.job\n' "$p"; done ;;
esac
""",
    "ps": """\
#!/bin/sh
# STUB_PS: the process table, one tab-separated line per process:
# pid, ppid, etime, the uv cache it holds the lock of, comm (the
# executable path) and args (the flattened argv).
[ -n "$STUB_PS" ] || exit 0
case "$*" in
  "-axo pid=,ppid=,comm=") awk -F'\t' '{print $1, $2, $5}' "$STUB_PS" ;;
  "-axo pid=,etime=,comm=") awk -F'\t' '{print $1, $3, $5}' "$STUB_PS" ;;
  "-o args= -p "*) awk -F'\t' -v p="$4" '$1 == p {print $6}' "$STUB_PS" ;;
esac
""",
    "lsof": """\
#!/bin/sh
# `lsof -t <file>`: the pids in STUB_PS that hold that file, exit 1 if none.
[ -z "$STUB_LSOF_FAILS" ] || exit 1
[ -n "$STUB_PS" ] || exit 1
for path; do :; done
awk -F'\t' -v f="$path" '$4 "/.lock" == f {print $1; n++} END {exit !n}' "$STUB_PS"
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
# A live, unreaped STUB_PS process whose executable has that name.
if [ -n "$STUB_PS" ]; then
  for pid in $(awk -F'\t' -v n="$2" '{c = $5; sub(/.*\//, "", c)} c == n {print $1}' "$STUB_PS"); do
    case "$(/bin/ps -o stat= -p "$pid" 2>/dev/null)" in ""|Z*) ;; *) exit 0 ;; esac
  done
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
            # kev is the default's second label; the stub's bootout above
            # unloaded every agent, so it is not loaded here.
            f"launchctl print gui/{uid}/local.system-tools.kev",
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

    def test_the_kev_agent_is_paused_by_default(self):
        """REGRESSION (sd:9460). kev serves through `uv run` around the
        clock, so it holds uv's cache lock like the Workspace MCP."""
        self.run_prune()
        uid = os.getuid()
        self.assertIn(f"launchctl print gui/{uid}/local.system-tools.kev",
                      self.calls())

    def processes(self, rows):
        """Start one real process per row and list it in STUB_PS.

        A row is (name, ppid, etime, cache, comm, args); a ppid of None
        names this test process, a live parent."""
        procs = {}
        lines = []
        for name, ppid, etime, cache, comm, args in rows:
            proc = subprocess.Popen(["sleep", "60"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
            procs[name] = proc
            ppid = os.getpid() if ppid is None else ppid
            lines.append(f"{proc.pid}\t{ppid}\t{etime}\t{cache}\t{comm}\t{comm} {args}\n")
        table = self.root / "ps"
        table.write_text("".join(lines))
        return procs, str(table)

    def test_an_orphaned_uvx_that_holds_this_cache_is_ended(self):
        """REGRESSION (sd:9460). A uvx whose parent is gone holds uv's
        cache lock with nothing left to serve; it is ended at any age,
        and the prune runs."""
        procs, table = self.processes([
            ("uvx", 1, "05:00:00", self.cache, "/opt/homebrew/bin/uvx", "mcp-proxy"),
            ("tool", 1, "02:00", self.cache, "/opt/homebrew/bin/uv",
             "tool uvx mcp-proxy-for-aws@latest"),
            # A space in the path: the executable is comm, never a word
            # of the flattened command line (review of sd:9460).
            ("spaced", 1, "01:00:00", self.cache, "/Volumes/Dev Tools/bin/uvx", "x"),
        ])
        result = self.run_prune(STUB_PS=table)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, proc in procs.items():
            self.assertIsNotNone(proc.wait(timeout=5), name)
            self.assertIn(f"ended uvx {proc.pid}: its parent is gone", result.stdout)
        self.assertIn("uv cache prune", self.calls())

    def test_a_uvx_whose_parent_lives_is_not_signalled_and_the_prune_skips(self):
        """REGRESSION (sd:9460 review). Age is no permission: a days-old
        MCP proxy of a live Claude session would lose its proxy with no
        restart path. It stays, and the prune skips as before."""
        procs, table = self.processes([
            ("live", None, "3-04:43:42", self.cache, "/opt/homebrew/bin/uv",
             "tool uvx mcp-proxy-for-aws@latest"),
        ])
        result = self.run_prune(STUB_PS=table)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(procs["live"].poll())
        self.assertNotIn("ended uvx", result.stdout)
        self.assertIn("another uv process holds the cache; skipped", result.stdout)
        self.assertNotIn("uv cache prune", self.calls())

    def test_an_orphaned_uvx_on_another_cache_is_not_signalled(self):
        """REGRESSION (sd:9460 review). A uvx with its own UV_CACHE_DIR
        holds another cache's lock, not the one this prune needs."""
        other = self.root / "other-cache"
        procs, table = self.processes([
            ("other", 1, "3-00:00:00", other, "/opt/homebrew/bin/uvx", "x"),
        ])
        result = self.run_prune(STUB_PS=table)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(procs["other"].poll())
        self.assertNotIn("ended uvx", result.stdout)

    def test_a_uvx_launchd_runs_as_a_job_is_not_signalled(self):
        """launchd is the parent of its jobs too: PPID 1 alone is not an
        orphan, and only the agents in UV_PRUNE_PAUSE come back."""
        procs, table = self.processes([
            ("job", 1, "4-00:00:00", self.cache, "/opt/homebrew/bin/uvx", "serve"),
        ])
        result = self.run_prune(STUB_PS=table, STUB_LAUNCHD_PIDS=str(procs["job"].pid))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(procs["job"].poll())
        self.assertNotIn("ended uvx", result.stdout)

    def test_a_failed_probe_signals_nothing(self):
        """A failed lsof or launchctl list is no answer, not a stale uvx."""
        for flag in ("STUB_LSOF_FAILS", "STUB_LIST_FAILS"):
            with self.subTest(flag=flag):
                procs, table = self.processes([
                    ("uvx", 1, "3-00:00:00", self.cache, "/opt/homebrew/bin/uvx", "x"),
                ])
                result = self.run_prune(STUB_PS=table, **{flag: "1"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(procs["uvx"].poll())
                self.assertNotIn("ended uvx", result.stdout)
                self.assertIn("skipped", result.stdout)

    def test_an_orphan_that_is_no_uvx_tool_run_is_not_signalled(self):
        """A `uv run` service and a program whose path names uvx stay."""
        procs, table = self.processes([
            ("service", 1, "4-07:05:25", self.cache, "uv", "run --extra serve kev"),
            ("other", 1, "2-00:00:00", self.cache, "/Volumes/uvx Tools/bin/other",
             "tool uvx x"),
        ])
        result = self.run_prune(STUB_PS=table)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, proc in procs.items():
            self.assertIsNone(proc.poll(), name)
        self.assertNotIn("ended uvx", result.stdout)

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
        self.assertEqual(plist["EnvironmentVariables"]["UV_PRUNE_PAUSE"],
                         f"{LABEL} local.system-tools.kev")
        self.assertEqual(plist["Label"], "local.system-tools.uv-cache-prune")

    def test_the_label_prefix_names_the_agent_and_the_paused_label(self):
        plist = self.render_plist(SYSTEM_TOOLS_LABEL_PREFIX="org.example")
        self.assertEqual(plist["Label"], "org.example.uv-cache-prune")
        self.assertEqual(plist["EnvironmentVariables"]["UV_PRUNE_PAUSE"],
                         "org.example.google-workspace-mcp org.example.kev")
        self.assertEqual(plist["StandardErrorPath"], "/tmp/org.example.uv-cache-prune.err")

    def test_the_agent_runs_at_standard_priority(self):
        # sd:3175. launchd's Background process type throttles the agent under
        # load. The plist names no ProcessType, so launchd uses Standard.
        self.assertNotIn("ProcessType", self.render_plist())

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
