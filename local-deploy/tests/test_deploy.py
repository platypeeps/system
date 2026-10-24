"""Tests for deploy.sh (sd:2725, sd:2812).

Each case builds a throwaway git checkout with deploy.sh in `local-deploy/`
and a stub `dashboard.sh` entrypoint beside it,
commits a change, and runs `plan` or `apply` over that commit. An `upgrade`
case gives the checkout a bare repository as its origin and lands the change
there, so its fetch reads a local path. `launchctl` and `lsof`
are stubs on PATH, so no case asks the real launchd or the real network.
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

# `print` fails for a label named in DEPLOY_TEST_UNLOADED or stopped: a file
# in DEPLOY_TEST_OUT, which `bootout` writes and `bootstrap` removes; like
# launchd, `bootstrap` refuses an agent that is loaded. `kickstart` and `bootstrap` mark the restart so the
# lsof stub can answer with a new listener.
LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$DEPLOY_TEST_CALLS"
case "$1" in
  print) case " $DEPLOY_TEST_UNLOADED " in *" ${2##*/} "*) exit 113 ;; esac
         [ ! -e "$DEPLOY_TEST_OUT/${2##*/}" ] || exit 113 ;;
  bootout) touch "$DEPLOY_TEST_OUT/${2##*/}" ;;
  bootstrap) label=$(basename "$3" .plist)
             [ -e "$DEPLOY_TEST_OUT/$label" ] || { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
             rm -f "$DEPLOY_TEST_OUT/$label"; touch "$DEPLOY_TEST_KICKED" ;;
  kickstart) touch "$DEPLOY_TEST_KICKED" ;;
esac
exit 0
"""

# LISTEN answers pid 100 before a kickstart and 200 after it, unless
# DEPLOY_TEST_NO_LISTENER is set. Like `lsof -t`, it prints nothing and exits 1
# for no match.
LSOF = """#!/bin/sh
case "$*" in
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

# The dashboard's interpreter, as the old library check ran it: DEPLOY_TEST_LAG
# makes that check refuse; no case may reach it now (sd:3279).
PYTHON = """#!/bin/sh
echo "lag-check" >> "$DEPLOY_TEST_CALLS"
[ -z "$DEPLOY_TEST_LAG" ] || { echo "$DEPLOY_TEST_LAG" >&2; exit 1; }
"""

ENTRYPOINT = """#!/bin/sh
echo "{name} $*" >> "$DEPLOY_TEST_CALLS"
exit "${{{status}:-0}}"
"""

AGENTS = ("test.example.sd-dashboard", "test.example.sd-serve")


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="deploy-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.write("local-deploy/deploy.sh", (FOLDER / "deploy.sh").read_text())
        self.write("lib/config.sh", (FOLDER.parent / "lib" / "config.sh").read_text())
        self.git("init", "-q")
        self.commit()
        self.base = self.head()
        stubs = self.tmp / "stub-bin"
        stubs.mkdir()
        self.python = self.tmp / "venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        for path, body in ((stubs / "launchctl", LAUNCHCTL), (stubs / "lsof", LSOF),
                           (self.python, PYTHON)):
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        # The dashboard is the command pack's since pack sd:5987.
        dashboard = self.tmp / "pack" / "dashboard" / "dashboard.sh"
        dashboard.parent.mkdir(parents=True)
        dashboard.write_text(ENTRYPOINT.format(name="dashboard.sh", status="DEPLOY_TEST_HEALTH"))
        self.calls = self.tmp / "calls.txt"
        self.calls.touch()
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(self.tmp),
                    "SYSTEM_TOOLS_LABEL_PREFIX": "test.example",
                    "DEPLOY_TEST_CALLS": str(self.calls),
                    "DEPLOY_TEST_KICKED": str(self.tmp / "kicked"),
                    "DEPLOY_TEST_OUT": str(self.tmp / "out"),
                    "DEPLOY_TEST_PYTHON": str(self.python),
                    "DEPLOY_WAIT": "1", "PYTHON": sys.executable, "SD_PACK_ROOT": str(self.tmp / "pack"), "XDG_STATE_HOME": str(self.tmp / "state")}
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

    def test_plan_restarts_the_changed_dashboard(self):
        self.assertEqual(self.plan({"local-project-dashboard/sd_dashboard/app.py": "x\n"}),
                         ["restart dashboard"])

    def test_plan_restarts_and_installs_nothing_for_the_retired_sd_db_folder(self):
        """sd:3281: sd_db, its schema and sd-serve are the pack's, which refresh moves; a
        range that touches the retired folder, or deletes it, plans no sd-serve restart."""
        self.assertEqual(self.plan({"local-sd-db/sd-db": "x\n", "local-sd-db/sd_db/remote.py": "x\n",
                                    "local-sd-db/sd_db/schema/002_x.sql": "x\n"}), [])

    def test_plan_is_empty_for_tests_docs_and_other_folders(self):
        self.assertEqual(self.plan({"local-herdr/tests/test_x.py": "x\n",
                                    "local-project-dashboard/README.md": "x\n",
                                    "local-herdr/herdr.sh": "x\n",
                                    "local-deploy/other.sh": "SD_DASHBOARD_PYTHON\n"}), [])

    def test_plan_reports_a_plist_template_and_restarts_nothing_for_it(self):
        path = "local-machine-setup/examples/launchagents/test.example.sd-serve.plist"
        self.assertEqual(self.plan({path: "<plist/>\n"}),
                         [f"report needs local-machine-setup install ({path})"])

    def test_plan_reports_plist_code_and_still_restarts_the_service(self):
        path = "local-project-dashboard/sd_dashboard/runtime.py"
        self.assertEqual(self.plan({path: '"EnvironmentVariables": {}\n'}),
                         [f"report needs local-project-dashboard install ({path})", "restart dashboard"])

    def test_apply_of_reports_only_calls_nothing(self):
        path = "local-machine-setup/examples/launchagents/test.example.sd-serve.plist"
        self.change({path: "<plist/>\n"})
        result = self.run_deploy("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"report needs local-machine-setup install ({path})", result.stdout)
        self.assertEqual(self.calls.read_text(), "")

    def test_apply_restarts_the_dashboard_in_its_safe_form(self):
        self.change({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        result = self.run_deploy("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        kicks = [call for call in calls if "kickstart" in call]
        self.assertEqual(len(kicks), 1, calls)
        self.assertTrue(kicks[0].startswith("launchctl kickstart -k gui/")
                        and kicks[0].endswith("/test.example.sd-dashboard"), kicks)
        self.assertIn("dashboard.sh health --wait 1", calls)

    def test_apply_restarts_the_dashboard_whatever_copy_its_interpreter_holds(self):
        """sd:3279: the dashboard runs the pack's lib/ and refuses any other copy itself, so a
        copy in its venv that differs from this checkout is no reason to refuse."""
        self.change({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_LAG="installed sd_db differs from this checkout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restarted dashboard", result.stdout)
        self.assertNotIn("lag-check", self.calls.read_text())

    def test_apply_stops_at_a_failed_dashboard_health_check(self):
        self.change({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_HEALTH="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: dashboard health failed; stopped", result.stderr)

    def test_apply_skips_an_agent_that_is_not_loaded(self):
        self.change({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_UNLOADED="test.example.sd-dashboard")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip dashboard: test.example.sd-dashboard is not loaded", result.stdout)
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
        self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        self.git("checkout", "-q", "-b", "topic")
        result = self.run_upgrade("--from", self.base)
        self.assertIn("is not on main", result.stderr)
        self.assert_nothing_ran(result)

    def test_upgrade_refuses_a_checkout_with_untracked_files(self):
        self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        self.write("local-herdr/stray.py", "x\n")
        result = self.run_upgrade("--from", self.base)
        self.assertIn("has uncommitted or untracked files", result.stderr)
        self.assert_nothing_ran(result)

    def test_upgrade_refuses_a_main_that_diverged_from_origin(self):
        self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        self.change({"local-herdr/herdr.sh": "x\n"})
        local = self.head()
        result = self.run_upgrade("--from", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("main and origin/main have diverged", result.stderr)
        self.assertEqual(self.head(), local)
        self.assertEqual(self.calls.read_text(), "")

    def test_upgrade_runs_one_at_a_time(self):
        landed = self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
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
        landed = self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no deployed sha recorded in {self.state}; give --from SHA", result.stderr)
        self.assertEqual(self.calls.read_text(), "")
        self.assertIsNone(self.recorded())
        self.assertEqual(self.head(), landed)

    def test_upgrade_records_the_sha_and_the_next_run_reads_it(self):
        landed = self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        result = self.run_upgrade("--from", self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restarted dashboard", result.stdout)
        self.assertIn(f"deployed {landed}", result.stdout)
        self.assertEqual((self.head(), self.recorded()), (landed, landed))
        self.calls.write_text("")
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"nothing to deploy from {landed} to {landed}", result.stdout)
        self.assertEqual(self.calls.read_text(), "")

    def test_upgrade_acts_on_its_own_checkout_from_any_working_directory(self):
        """The allow rule matches `sh <abs>/deploy.sh upgrade` alone, with no `cd` before it."""
        landed = self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
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
                self.assertIn("restarted dashboard", result.stdout)
                self.assertEqual((self.head(), self.recorded()), (landed, landed))

    def test_upgrade_over_the_retired_sd_db_folder_restarts_and_stops_nothing(self):
        """sd:3281: a change to, or the deletion of, the retired local-sd-db folder
        restarts nothing, stops no agent and needs no migration."""
        self.origin()
        self.change({"local-sd-db/sd_db/remote.py": "x\n"})
        self.git("rm", "-q", "-r", "local-sd-db/sd_db")
        self.commit()
        landed = self.head()
        self.git("push", "-q", "origin", "main")
        self.git("reset", "-q", "--hard", self.base)
        result = self.run_upgrade("--from", self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text()
        self.assertEqual(calls, "")
        self.assertNotIn("migration", result.stdout + result.stderr)
        self.assertEqual(self.recorded(), landed)

    def starts(self):
        """The plists bootstrapped, in call order."""
        return [call.split()[-1].rsplit("/", 1)[-1] for call in self.calls.read_text().splitlines()
                if "bootstrap" in call]

    def fail_a_start_then_rerun(self, label, change, **failing):
        """A start whose wait fails boots its agent out; the rerun starts each agent once.

        The agent is one an older upgrade stopped to install sd_db and left in `stopped`
        (older state, sd:3279): this version stops nothing, but it still finishes that run."""
        landed = self.land({change: "x\n"})
        self.record(self.base)
        self.stopped.parent.mkdir(parents=True, exist_ok=True)
        self.stopped.write_text(label + "\n")
        (self.tmp / "out" / label).touch()
        result = self.run_upgrade(**failing)
        self.assertEqual(result.returncode, 1, result.stdout)
        calls = self.calls.read_text().splitlines()
        self.assertTrue(calls[-1].startswith("launchctl bootout") and calls[-1].endswith(label), calls)
        self.assertIn(label, self.stopped.read_text().splitlines())
        self.assertEqual(self.recorded(), self.base)
        self.calls.write_text("")
        (self.tmp / "kicked").unlink(missing_ok=True)
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        starts = self.starts()
        self.assertEqual(starts, [label + ".plist"])
        self.assertFalse(list((self.tmp / "out").iterdir()))
        self.assertFalse(self.stopped.exists())
        self.assertEqual(self.recorded(), landed)

    def test_a_failed_dashboard_health_after_bootstrap_boots_it_out_for_the_rerun(self):
        self.fail_a_start_then_rerun("test.example.sd-dashboard", "local-project-dashboard/sd_dashboard/app.py",
                                     DEPLOY_TEST_HEALTH="1")

    def test_a_missing_sd_serve_listener_after_bootstrap_boots_it_out_for_the_rerun(self):
        self.fail_a_start_then_rerun("test.example.sd-serve", "local-project-dashboard/sd_dashboard/app.py",
                                     DEPLOY_TEST_NO_LISTENER="1")

    def test_a_replay_bootstraps_no_marked_agent_that_launchd_holds(self):
        self.origin()
        self.record(self.base)
        # Both marked agents are loaded: no file in out/.
        self.stopped.write_text("test.example.sd-dashboard\ntest.example.sd-serve\n")
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.starts(), [])
        self.assertIn("dashboard.sh health --wait 1", self.calls.read_text().splitlines())
        self.assertFalse(self.stopped.exists())

    def test_upgrade_keeps_the_record_when_a_step_fails_so_a_rerun_replays_it(self):
        landed = self.land({"local-project-dashboard/sd_dashboard/app.py": "x\n"})
        self.record(self.base)
        result = self.run_upgrade(DEPLOY_TEST_HEALTH="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: dashboard health failed; stopped", result.stderr)
        self.assertEqual(self.recorded(), self.base)
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restarted dashboard", result.stdout)
        self.assertEqual(self.recorded(), landed)

    def test_upgrade_applies_but_does_not_record_a_pending_launchagent_install(self):
        path = "local-project-dashboard/sd_dashboard/runtime.py"
        self.land({path: '"EnvironmentVariables": {}\n'})
        self.record(self.base)
        result = self.run_upgrade()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"report needs local-project-dashboard install ({path})", result.stdout)
        self.assertIn("restarted dashboard", result.stdout)
        self.assertIn("a LaunchAgent install is pending", result.stderr)
        self.assertEqual(self.recorded(), self.base)

    def test_usage(self):
        script = str(FOLDER / "deploy.sh")
        bare = subprocess.run(["sh", script], capture_output=True, text=True)
        self.assertEqual((bare.returncode, bare.stdout), (1, ""))
        self.assertIn("usage: deploy.sh", bare.stderr)
        self.assertEqual(subprocess.run(["sh", script, "help"], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
