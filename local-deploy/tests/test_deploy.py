"""Tests for deploy.sh (sd:2725).

Each case builds a throwaway git checkout with deploy.sh in `local-deploy/`
and stub `dashboard.sh` and `runner.sh` entrypoints beside it, commits a
change, and runs `plan` or `apply` over that commit. `launchctl`, `lsof` and
`plutil` are stubs on PATH, and the interpreter the plist names is a stub
that answers the library check, so no case asks the real launchd, the real
network or a real virtualenv.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent

# `print` fails for a label named in DEPLOY_TEST_UNLOADED; `kickstart` marks
# the restart so the lsof stub can answer with a new listener.
LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$DEPLOY_TEST_CALLS"
case "$1" in
  print) case " $DEPLOY_TEST_UNLOADED " in *" ${2##*/} "*) exit 113 ;; esac ;;
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

# Answers every plist variable with the stub interpreter.
PLUTIL = """#!/bin/sh
echo "$DEPLOY_TEST_PYTHON"
"""

# The consumer's interpreter: DEPLOY_TEST_LAG makes the library check refuse.
PYTHON = """#!/bin/sh
echo "lag-check" >> "$DEPLOY_TEST_CALLS"
[ -z "$DEPLOY_TEST_LAG" ] || { echo "$DEPLOY_TEST_LAG" >&2; exit 1; }
"""

LIBRARY_REPORT = ("report needs sd_db install: local-sd-db/sd-db.sh install <venv> for the dashboard's"
                  " and the runner's interpreters, before apply")

ENTRYPOINT = """#!/bin/sh
echo "{name} $*" >> "$DEPLOY_TEST_CALLS"
exit "${{{status}:-0}}"
"""


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="deploy-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.write("local-deploy/deploy.sh", (FOLDER / "deploy.sh").read_text())
        self.write("local-project-dashboard/dashboard.sh",
                   ENTRYPOINT.format(name="dashboard.sh", status="DEPLOY_TEST_HEALTH"))
        self.write("local-sd-runner/runner.sh",
                   ENTRYPOINT.format(name="runner.sh", status="DEPLOY_TEST_RUNNER"))
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
                    "DEPLOY_TEST_PYTHON": str(self.python),
                    "DEPLOY_WAIT": "1"}

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
                                    "local-herdr/herdr.sh": "x\n"}), [])

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
        self.assertIn("dashboard.sh health", calls)
        self.assertIn("runner.sh restart", calls)
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
        self.assertNotIn("runner.sh", self.calls.read_text())

    def test_apply_stops_at_a_failed_dashboard_health_check(self):
        self.change({"local-project-dashboard/sd_dashboard/app.py": "x\n",
                     "local-sd-runner/sd_runner/cli.py": "x\n"})
        result = self.run_deploy("apply", DEPLOY_TEST_HEALTH="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy: dashboard health failed; stopped", result.stderr)
        self.assertNotIn("runner.sh", self.calls.read_text())

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

    def test_usage(self):
        script = str(FOLDER / "deploy.sh")
        bare = subprocess.run(["sh", script], capture_output=True, text=True)
        self.assertEqual((bare.returncode, bare.stdout), (1, ""))
        self.assertIn("usage: deploy.sh", bare.stderr)
        self.assertEqual(subprocess.run(["sh", script, "help"], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
