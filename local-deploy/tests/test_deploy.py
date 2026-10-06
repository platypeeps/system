"""Tests for deploy.sh (sd:2725, sd:2812).

Each case builds a throwaway git checkout with deploy.sh in `local-deploy/`
and stub `dashboard.sh`, `runner.sh` and `sd-db.sh` entrypoints beside it,
commits a change, and runs `plan` or `apply` over that commit. An `upgrade`
case gives the checkout a bare repository as its origin and lands the change
there, so its fetch reads a local path. `launchctl`, `lsof` and
`plutil` are stubs on PATH, and the interpreter the plist names is a stub
that answers the library check, so no case asks the real launchd, the real
network or a real virtualenv.
"""

import fcntl
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
RUNTIME = FOLDER.parent / "local-project-dashboard" / "sd_dashboard" / "runtime.py"

# `print` fails for a label named in DEPLOY_TEST_UNLOADED or stopped: a file
# in DEPLOY_TEST_OUT, which `bootout` and `runner.sh stop` write and
# `bootstrap` and `runner.sh start` remove. `kickstart` and `bootstrap` mark
# the restart so the lsof stub can answer with a new listener.
LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$DEPLOY_TEST_CALLS"
case "$1" in
  print) case " $DEPLOY_TEST_UNLOADED " in *" ${2##*/} "*) exit 113 ;; esac
         [ ! -e "$DEPLOY_TEST_OUT/${2##*/}" ] || exit 113 ;;
  bootout) touch "$DEPLOY_TEST_OUT/${2##*/}" ;;
  bootstrap) rm -f "$DEPLOY_TEST_OUT/$(basename "$3" .plist)"; touch "$DEPLOY_TEST_KICKED" ;;
  kickstart) touch "$DEPLOY_TEST_KICKED" ;;
esac
exit 0
"""

# ESTABLISHED answers DEPLOY_TEST_SESSIONS; LISTEN answers pid 100 before a
# kickstart and 200 after it, unless DEPLOY_TEST_NO_LISTENER is set. Like
# `lsof -t`, it prints nothing and exits 1 for no match. DEPLOY_TEST_LSOF_FAIL
# makes it fail with that code and a message, as a missing or broken lsof does.
LSOF = """#!/bin/sh
if [ -n "$DEPLOY_TEST_LSOF_FAIL" ]; then
  echo "lsof: cannot answer" >&2
  exit "$DEPLOY_TEST_LSOF_FAIL"
fi
case "$*" in
  *ESTABLISHED*) pids="$DEPLOY_TEST_SESSIONS" ;;
  *LISTEN*)
    if [ -e "$DEPLOY_TEST_KICKED" ]; then
      [ -n "$DEPLOY_TEST_NO_LISTENER" ] || pids=200
    else
      pids=100
    fi ;;
esac
[ -n "$pids" ] || exit 1
echo "$pids"
"""

# Answers every plist variable with the stub interpreter; the runner's can
# differ. `-convert json` answers as the real one does, slashes escaped; the
# sd-serve plist names an interpreter only when DEPLOY_TEST_SERVE_PYTHON is set.
PLUTIL = """#!/bin/sh
case "$*" in
  *-convert*)
    case "$*" in
      *.sd-runner.plist*) key=SD_RUNNER_PYTHON; value="${DEPLOY_TEST_RUNNER_PYTHON:-$DEPLOY_TEST_PYTHON}" ;;
      *.sd-dashboard.plist*) key=SD_DASHBOARD_PYTHON; value="$DEPLOY_TEST_PYTHON" ;;
      *) key=SD_DB_PYTHON; value="$DEPLOY_TEST_SERVE_PYTHON" ;;
    esac
    if [ -n "$value" ]; then
      printf '{"EnvironmentVariables":{"PATH":"\\/usr\\/bin","%s":"%s"}}\\n' "$key" "$(printf '%s' "$value" | sed 's#/#\\\\/#g')"
    else
      printf '{"EnvironmentVariables":{"PATH":"\\/usr\\/bin"}}\\n'
    fi ;;
  *SD_RUNNER_PYTHON*) echo "${DEPLOY_TEST_RUNNER_PYTHON:-$DEPLOY_TEST_PYTHON}" ;;
  *) echo "$DEPLOY_TEST_PYTHON" ;;
esac
"""

# The consumer's interpreter: DEPLOY_TEST_LAG makes the library check refuse,
# and DEPLOY_TEST_LOAD the runner's load limit (load.py), as its JSON does.
PYTHON = """#!/bin/sh
case "$*" in
  *load.py*)
    echo "load-check" >> "$DEPLOY_TEST_CALLS"
    [ -z "$DEPLOY_TEST_LOAD" ] || { printf '{"ok": false, "reason": "%s", "load": 169.0}\\n' "$DEPLOY_TEST_LOAD"; exit 1; }
    echo '{"ok": true}' ;;
  *)
    echo "lag-check" >> "$DEPLOY_TEST_CALLS"
    [ -z "$DEPLOY_TEST_LAG" ] || { echo "$DEPLOY_TEST_LAG" >&2; exit 1; } ;;
esac
"""

LIBRARY_REPORT = ("report needs sd_db install: local-sd-db/sd-db.sh install <venv> for the dashboard's"
                  " and the runner's interpreters, before apply")

ENTRYPOINT = """#!/bin/sh
echo "{name} $*" >> "$DEPLOY_TEST_CALLS"
exit "${{{status}:-0}}"
"""

# `stop` marks the runner stopped, which `launchctl print` then reports, and
# `start` clears it; `restart` exits DEPLOY_TEST_RUNNER.
RUNNER = """#!/bin/sh
echo "runner.sh $*" >> "$DEPLOY_TEST_CALLS"
case "$1" in
  stop) touch "$DEPLOY_TEST_OUT/$SYSTEM_TOOLS_LABEL_PREFIX.sd-runner" ;;
  start) rm -f "$DEPLOY_TEST_OUT/$SYSTEM_TOOLS_LABEL_PREFIX.sd-runner" ;;
  *) exit "${DEPLOY_TEST_RUNNER:-0}" ;;
esac
"""

# Logs which agents were stopped while sd_db was replaced.
SDDB = """#!/bin/sh
echo "sd-db.sh $* stopped=$(ls "$DEPLOY_TEST_OUT" | tr '\\n' ' ' | sed 's/ $//')" >> "$DEPLOY_TEST_CALLS"
exit "${DEPLOY_TEST_INSTALL:-0}"
"""

AGENTS = ("test.example.sd-dashboard", "test.example.sd-runner", "test.example.sd-serve")

LOAD = "the 1-minute load average 169.0 is at or above 16; a cold start under load can stall on diskutil"


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="deploy-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.write("local-deploy/deploy.sh", (FOLDER / "deploy.sh").read_text())
        self.write("local-project-dashboard/dashboard.sh",
                   ENTRYPOINT.format(name="dashboard.sh", status="DEPLOY_TEST_HEALTH"))
        self.write("local-sd-runner/runner.sh", RUNNER)
        self.write("local-sd-db/sd-db.sh", SDDB)
        self.write("local-sd-db/sd_db/schema.py", "SCHEMA_VERSION = 1\n")
        self.git("init", "-q")
        self.commit()
        self.base = self.head()
        stubs = self.tmp / "stub-bin"
        stubs.mkdir()
        self.python = self.tmp / "venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        for path, body in ((stubs / "launchctl", LAUNCHCTL), (stubs / "lsof", LSOF),
                           (stubs / "plutil", PLUTIL), (self.python, PYTHON)):
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.calls = self.tmp / "calls.txt"
        self.calls.touch()
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(self.tmp),
                    "SYSTEM_TOOLS_LABEL_PREFIX": "test.example",
                    "DEPLOY_TEST_CALLS": str(self.calls),
                    "DEPLOY_TEST_KICKED": str(self.tmp / "kicked"),
                    "DEPLOY_TEST_OUT": str(self.tmp / "out"),
                    "DEPLOY_TEST_PYTHON": str(self.python),
                    "DEPLOY_WAIT": "1", "PYTHON": sys.executable, "XDG_STATE_HOME": str(self.tmp / "state")}
        self.state = self.tmp / "state" / "system" / "deploy" / "deployed"
        self.stopped = self.state.with_name("stopped")
        (self.tmp / "out").mkdir()
        agents = self.tmp / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        for label in AGENTS:
            (agents / f"{label}.plist").write_text("<plist/>\n")

    def write(self, relative, text):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.test",
                 "commit", "-q", "-m", "change")

    def head(self):
        return self.git("rev-parse", "HEAD")

    def change(self, files):
        for relative, text in files.items():
            self.write(relative, text)
        self.commit()

    def run_deploy(self, verb, **extra):
        return subprocess.run(["sh", str(self.repo / "local-deploy/deploy.sh"), verb, self.base, self.head()],
                              env={**self.env, **extra}, capture_output=True, text=True, timeout=60)

    def plan(self, files):
        self.change(files)
        result = self.run_deploy("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_plan_restarts_each_changed_service(self):
        self.assertEqual(self.plan({"local-sd-db/sd-db.sh": "x\n",
                                    "local-project-dashboard/sd_dashboard/app.py": "x\n",
                                    "local-sd-runner/sd_runner/cli.py": "x\n"}),
                         ["restart sd-serve", "restart dashboard", "restart runner"])

    def test_plan_maps_a_library_change_to_every_consumer_after_its_install(self):
        self.assertEqual(self.plan({"local-sd-db/sd_db/remote.py": "x\n"}),
                         [LIBRARY_REPORT, "restart sd-serve", "restart dashboard", "restart runner"])

    def test_plan_is_empty_for_tests_docs_and_other_folders(self):
        self.assertEqual(self.plan({"local-sd-db/tests/test_x.py": "x\n",
                                    "local-project-dashboard/README.md": "x\n",
                                    "local-herdr/herdr.sh": "x\n",
                                    "local-deploy/other.sh": "SD_RUNNER_PYTHON\n"}), [])

    def test_plan_reports_a_plist_template_and_restarts_nothing_for_it(self):
        path = "local-machine-setup/examples/launchagents/test.example.sd-serve.plist"
        self.assertEqual(self.plan({path: "<plist/>\n"}),
                         [f"report needs local-machine-setup install ({path})"])

    def test_plan_reports_plist_code_and_still_restarts_the_service(self):
        path = "local-project-dashboard/sd_dashboard/runtime.py"
        self.assertEqual(self.plan({path: '"EnvironmentVariables": {}\n'}),
                         [f"report needs local-project-dashboard install ({path})", "restart dashboard"])

    def test_plan_reports_a_schema_change_and_holds_every_restart(self):
        self.assertEqual(self.plan({"local-sd-db/sd_db/schema/002_x.sql": "x\n",
                                    "local-sd-db/sd_db/remote.py": "x\n",
                                    "local-project-dashboard/sd_dashboard/app.py": "x\n",
                                    "local-sd-runner/sd_runner/cli.py": "x\n"}),
                         ["report needs migration (local-sd-db/sd_db/schema/002_x.sql); restart after migrate",
                          LIBRARY_REPORT])

    def test_apply_of_reports_only_calls_nothing(self):
        self.change({"local-sd-db/sd_db/schema.py": "SCHEMA_VERSION = 2\n",
                     "local-project-dashboard/sd_dashboard/app.py": "x\n"})
        result = self.run_deploy("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("report needs migration (local-sd-db/sd_db/schema.py); restart after migrate", result.stdout)
        self.assertEqual(self.calls.read_text(), "")

    def test_apply_restarts_each_service_in_its_safe_form(self):
        self.change({"local-sd-db/sd_db/remote.py": "x\n",
                     "local-project-dashboard/sd_dashboard/app.py": "x\n",
                     "local-sd-runner/sd_runner/cli.py": "x\n"})
        result = self.run_deploy("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restarted sd-serve: listener pid 200", result.stdout)
        calls = self.calls.read_text().splitlines()
        kicks = [call for call in calls if "kickstart" in call]
        self.assertEqual(len(kicks), 2, calls)
        self.assertTrue(kicks[0].startswith("launchctl kickstart -k gui/")
                        and kicks[0].endswith("/test.example.sd-serve"), kicks)
        self.assertTrue(kicks[1].endswith("/test.example.sd-dashboard"), kicks)
        self.assertIn("dashboard.sh health --wait 1", calls)
        self.assertIn("runner.sh restart", calls)
        self.assertLess(calls.index("load-check"), calls.index(kicks[0]))
        self.assertEqual(calls.count("lag-check"), 2, calls)
        self.assertLess(calls.index("lag-check"), calls.index(kicks[0]))

    def test_apply_refuses_a_consumer_whose_installed_library_lags(self):
        self.change({"local-sd-db/sd_db/remote.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_LAG="RuntimeRefused: installed sd_db abc lacks def")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: refused before any restart: sd-dashboard: RuntimeRefused: installed sd_db abc lacks def;"
                      f" provision: local-sd-db/sd-db.sh install {self.tmp / 'venv'}", result.stderr)
        self.assertTrue(result.stdout.startswith(LIBRARY_REPORT), result.stdout)
        calls = self.calls.read_text()
        self.assertNotIn("kickstart", calls)
        self.assertNotIn("runner.sh", calls)

    def real_consumer(self, edit_installed=None):
        """A real interpreter whose installed sd_db is a copy of the checkout's,
        with no dist-info at all: no provenance, so only content can tell."""
        self.change({"local-project-dashboard/sd_dashboard/__init__.py": "",
                     "local-project-dashboard/sd_dashboard/runtime.py": RUNTIME.read_text(),
                     "local-sd-db/sd_db/__init__.py": "",
                     # The library check is under test, not this machine's load.
                     "local-sd-runner/sd_runner/load.py": 'print(\'{"ok": true}\')\n'})
        venv = self.tmp / "consumer"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        python = venv / "bin" / "python"
        purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                 check=True, capture_output=True, text=True).stdout.strip()
        installed = pathlib.Path(purelib) / "sd_db"
        shutil.copytree(self.repo / "local-sd-db" / "sd_db", installed)
        if edit_installed:
            (installed / edit_installed).write_text("SCHEMA_VERSION = 0\n")
        return venv

    def test_apply_restarts_consumers_whose_installed_library_matches(self):
        venv = self.real_consumer()
        result = self.run_deploy("apply", DEPLOY_TEST_PYTHON=str(venv / "bin" / "python"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restarted dashboard", result.stdout)
        self.assertIn("restarted runner", result.stdout)

    def test_apply_refuses_an_installed_library_without_provenance_that_differs(self):
        venv = self.real_consumer(edit_installed="schema.py")
        result = self.run_deploy("apply", DEPLOY_TEST_PYTHON=str(venv / "bin" / "python"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: refused before any restart: sd-dashboard: installed sd_db differs from this checkout"
                      f" in 1 file(s): schema.py; provision: local-sd-db/sd-db.sh install {venv}", result.stderr)
        self.assertNotIn("kickstart", self.calls.read_text())

    def test_apply_refuses_when_lsof_cannot_answer(self):
        self.change({"local-sd-db/sd-db.sh": "x\n"})
        for code in ("127", "1", "2"):
            with self.subTest(code=code):
                self.calls.write_text("")
                result = self.run_deploy("apply", DEPLOY_TEST_LSOF_FAIL=code)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"lsof exited {code}: lsof: cannot answer", result.stderr)
                self.assertIn("deploy: refused before any restart: sd-serve: lsof cannot tell", result.stderr)
                self.assertNotIn("kickstart", self.calls.read_text())

    def test_apply_skips_sd_serve_while_a_satellite_session_is_open(self):
        self.change({"local-sd-db/sd-db.sh": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_SESSIONS="4242")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("report sd-serve not restarted: a session is open on port 8769", result.stdout)
        self.assertNotIn("kickstart", self.calls.read_text())

    def test_apply_fails_when_sd_serve_listener_does_not_return(self):
        self.change({"local-sd-db/sd-db.sh": "x\n",
                     "local-sd-runner/sd_runner/cli.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_NO_LISTENER="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sd-serve listener check", result.stderr)
        self.assertNotIn("runner.sh restart", self.calls.read_text().splitlines())

    def test_apply_stops_at_a_failed_dashboard_health_check(self):
        self.change({"local-project-dashboard/sd_dashboard/app.py": "x\n",
                     "local-sd-runner/sd_runner/cli.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_HEALTH="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: dashboard health failed; stopped", result.stderr)
        self.assertNotIn("runner.sh restart", self.calls.read_text().splitlines())

    def test_apply_fails_when_the_runner_restart_fails(self):
        self.change({"local-sd-runner/sd_runner/cli.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_RUNNER="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: runner restart failed; stopped", result.stderr)
        self.assertNotIn("kickstart", self.calls.read_text())

    def test_apply_skips_an_agent_that_is_not_loaded(self):
        self.change({"local-sd-db/sd-db.sh": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_UNLOADED="test.example.sd-serve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip sd-serve: test.example.sd-serve is not loaded", result.stdout)
        self.assertNotIn("kickstart", self.calls.read_text())

    def origin(self):
        """Put the checkout on main, with a bare repository as its origin."""
        self.git("branch", "-M", "main")
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-q", "origin", "main")

    def land(self, files):
        """Land `files` on origin/main and leave the checkout one commit behind it."""
        self.origin()
        self.change(files)
        landed = self.head()
        self.git("push", "-q", "origin", "main")
        self.git("reset", "-q", "--hard", self.base)
        return landed

    def record(self, sha):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(sha + "\n")

    def recorded(self):
        return self.state.read_text().strip() if self.state.exists() else None

    def run_upgrade(self, *args, cwd=None, **extra):
        return subprocess.run(["sh", str(self.repo / "local-deploy/deploy.sh"), "upgrade", *args], cwd=cwd,
                              env={**self.env, **extra}, capture_output=True, text=True, timeout=60)

    def assert_nothing_ran(self, result):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.head(), self.base)
        self.assertEqual(self.calls.read_text(), "")
        self.assertIsNone(self.recorded())

    def test_upgrade_refuses_a_checkout_off_main(self):
        self.land({"local-sd-runner/sd_runner/cli.py": "x\n"})
        self.git("checkout", "-q", "-b", "topic")
        result = self.run_upgrade("--from", self.base)
        self.assertIn("is not on main", result.stderr)
        self.assert_nothing_ran(result)

    def test_upgrade_refuses_a_checkout_with_untracked_files(self):
        self.land({"local-sd-runner/sd_runner/cli.py": "x\n"})
        self.write("local-sd-db/sd_db/stray.py", "x\n")
        result = self.run_upgrade("--from", self.base)
        self.assertIn("has uncommitted or untracked files", result.stderr)
        self.assert_nothing_ran(result)

    def test_upgrade_refuses_a_main_that_diverged_from_origin(self):
        self.land({"local-sd-runner/sd_runner/cli.py": "x\n"})
        self.change({"local-herdr/herdr.sh": "x\n"})
        local = self.head()
        result = self.run_upgrade("--from", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("main and origin/main have diverged", result.stderr)
        self.assertEqual(self.head(), local)
        self.assertEqual(self.calls.read_text(), "")

    def test_upgrade_runs_one_at_a_time(self):
        landed = self.land({"local-sd-runner/sd_runner/cli.py": "x\n"})
        lock = self.state.parent / "upgrade.lock"
        lock.parent.mkdir(parents=True)
        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_upgrade("--from", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"another upgrade holds {lock}", result.stderr)
        self.assertEqual((self.head(), self.calls.read_text()), (self.base, ""))
        result = self.run_upgrade("--from", self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), landed)

    def test_upgrade_with_no_record_requires_from(self):
        landed = self.land({"local-sd-runner/sd_runner/cli.py": "x\n"})
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no deployed sha recorded in {self.state}; give --from SHA", result.stderr)
        self.assertEqual(self.calls.read_text(), "")
        self.assertIsNone(self.recorded())
        self.assertEqual(self.head(), landed)

    def test_upgrade_records_the_sha_and_the_next_run_reads_it(self):
        landed = self.land({"local-sd-runner/sd_runner/cli.py": "x\n"})
        result = self.run_upgrade("--from", self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restarted runner", result.stdout)
        self.assertIn(f"deployed {landed}", result.stdout)
        self.assertEqual((self.head(), self.recorded()), (landed, landed))
        self.calls.write_text("")
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"nothing to deploy from {landed} to {landed}", result.stdout)
        self.assertEqual(self.calls.read_text(), "")

    def test_upgrade_acts_on_its_own_checkout_from_any_working_directory(self):
        """The allow rule matches `sh <abs>/deploy.sh upgrade` alone, with no `cd` before it."""
        landed = self.land({"local-sd-runner/sd_runner/cli.py": "x\n"})
        plain = self.tmp / "plain"
        plain.mkdir()
        other = self.tmp / "other-repo"
        subprocess.run(["git", "init", "-q", "-b", "topic", str(other)], check=True)
        (other / "stray.txt").write_text("x\n")
        for cwd in (plain, other):
            with self.subTest(cwd=cwd.name):
                self.git("reset", "-q", "--hard", self.base)
                (self.tmp / "kicked").unlink(missing_ok=True)
                self.state.unlink(missing_ok=True)
                result = self.run_upgrade("--from", self.base, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("restarted runner", result.stdout)
                self.assertEqual((self.head(), self.recorded()), (landed, landed))

    def test_upgrade_installs_sd_db_once_per_venv_before_any_restart(self):
        self.land({"local-sd-db/sd_db/remote.py": "x\n"})
        other = self.tmp / "other venv" / "bin" / "python"
        other.parent.mkdir(parents=True)
        shutil.copy(self.python, other)
        for runner_python, venvs in ((self.python, [self.tmp / "venv"]),
                                     (other, sorted([self.tmp / "venv", self.tmp / "other venv"]))):
            with self.subTest(runner_python=runner_python):
                self.calls.write_text("")
                (self.tmp / "kicked").unlink(missing_ok=True)
                result = self.run_upgrade("--from", self.base, DEPLOY_TEST_RUNNER_PYTHON=str(runner_python))
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls.read_text().splitlines()
                installs = [call for call in calls if call.startswith("sd-db.sh")]
                stopped = "stopped=test.example.sd-dashboard test.example.sd-runner"
                self.assertEqual(installs, [f"sd-db.sh install {venv} {stopped}" for venv in venvs])
                self.assertLess(calls.index("runner.sh stop"), calls.index(installs[0]))
                self.assertEqual(calls[-1], "runner.sh start")
                self.assertFalse(list((self.tmp / "out").iterdir()))
                kick = next(call for call in calls if "kickstart" in call)
                self.assertLess(calls.index(installs[-1]), calls.index(kick))

    def test_a_failure_after_the_stop_leaves_the_runner_stopped_until_a_rerun(self):
        landed = self.land({"local-sd-db/sd_db/remote.py": "x\n"})
        self.record(self.base)
        result = self.run_upgrade(DEPLOY_TEST_INSTALL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"deploy: sd_db install into {self.tmp / 'venv'} failed; stopped", result.stderr)
        self.assertIn("left stopped: test.example.sd-runner test.example.sd-dashboard; finish the upgrade with: sh "
                      f"{self.repo / 'local-deploy/deploy.sh'} upgrade --from {self.base}", result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertIn("runner.sh stop", calls)
        self.assertFalse([call for call in calls if "kickstart" in call or "bootstrap" in call
                          or call.startswith("runner.sh start")], calls)
        self.assertEqual(sorted(path.name for path in (self.tmp / "out").iterdir()),
                         ["test.example.sd-dashboard", "test.example.sd-runner"])
        self.assertEqual(self.recorded(), self.base)
        self.calls.write_text("")
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls[-1], "runner.sh start")
        self.assertNotIn("runner.sh stop", calls)
        self.assertFalse(list((self.tmp / "out").iterdir()))
        self.assertFalse(self.stopped.exists())
        self.assertEqual(self.recorded(), landed)

    def test_upgrade_stops_sd_serve_too_when_it_runs_from_the_venv(self):
        self.land({"local-sd-db/sd_db/remote.py": "x\n"})
        self.record(self.base)
        result = self.run_upgrade(DEPLOY_TEST_SERVE_PYTHON=str(self.python))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        install = next(call for call in calls if call.startswith("sd-db.sh"))
        self.assertTrue(install.endswith("stopped=test.example.sd-dashboard test.example.sd-runner test.example.sd-serve"),
                        install)
        starts = [call.split()[-1].rsplit("/", 1)[-1] for call in calls
                  if "bootstrap" in call or call == "runner.sh start"]
        self.assertEqual(starts, ["test.example.sd-serve.plist", "test.example.sd-dashboard.plist", "start"])
        self.assertIn("started test.example.sd-serve", result.stdout)

    def test_upgrade_refuses_on_the_runner_load_before_any_restart(self):
        self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n",
                   "local-sd-runner/sd_runner/cli.py": "x\n"})
        self.record(self.base)
        result = self.run_upgrade(DEPLOY_TEST_LOAD=LOAD)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"deploy: refused before any restart: runner: {LOAD}", result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls, ["launchctl print gui/" + calls[0].split("gui/")[1], "load-check"])
        self.assertEqual(self.recorded(), self.base)

    def test_upgrade_keeps_the_record_when_a_step_fails_so_a_rerun_replays_it(self):
        landed = self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n",
                            "local-sd-runner/sd_runner/cli.py": "x\n"})
        self.record(self.base)
        result = self.run_upgrade(DEPLOY_TEST_HEALTH="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: dashboard health failed; stopped", result.stderr)
        self.assertEqual(self.recorded(), self.base)
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restarted dashboard", result.stdout)
        self.assertIn("restarted runner", result.stdout)
        self.assertEqual(self.recorded(), landed)

    def test_upgrade_refuses_an_open_sd_serve_session_before_any_stop(self):
        self.land({"local-sd-db/serve.conf": "x\n"})
        self.record(self.base)
        result = self.run_upgrade(DEPLOY_TEST_SESSIONS="4242")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sd-serve: a session is open on port 8769; rerun upgrade when it closes", result.stderr)
        self.assertNotIn("kickstart", self.calls.read_text())
        self.assertEqual(self.recorded(), self.base)

    def test_upgrade_applies_but_does_not_record_a_pending_launchagent_install(self):
        path = "local-sd-runner/sd_runner/cli.py"
        self.land({path: '"EnvironmentVariables": {}\n'})
        self.record(self.base)
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"report needs local-sd-runner install ({path})", result.stdout)
        self.assertIn("restarted runner", result.stdout)
        self.assertIn("a LaunchAgent install is pending", result.stderr)
        self.assertEqual(self.recorded(), self.base)

    def test_upgrade_refuses_a_migration_and_keeps_the_record(self):
        landed = self.land({"local-sd-db/sd_db/schema.py": "SCHEMA_VERSION = 2\n"})
        self.record(self.base)
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"then record it: deploy.sh upgrade --from {landed}", result.stderr)
        self.assertEqual(self.calls.read_text(), "")
        self.assertEqual(self.recorded(), self.base)

    def test_usage(self):
        script = str(FOLDER / "deploy.sh")
        bare = subprocess.run(["sh", script], capture_output=True, text=True)
        self.assertEqual((bare.returncode, bare.stdout), (1, ""))
        self.assertIn("usage: deploy.sh", bare.stderr)
        self.assertEqual(subprocess.run(["sh", script, "help"], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
