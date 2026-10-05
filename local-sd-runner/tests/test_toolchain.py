"""sd:1762: the runner's environment blocks as `environment:`, never as a failing test.

Real clones and supervisors through `test_runtime.Fixture`. Every installer
and reviewer here is a stub script on a search path the test sets; no real
`npm` install and no real reviewer runs.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sd_db import runner as store
from sd_runner import cli, runtime, toolchain

from . import test_runtime

#: What assignment 111's ship adapter printed, verbatim apart from the indentation.
SHIP_REFUSAL = "ship adapter exited 3: " + json.dumps({
    "error": "local review readiness blocked: executable_missing; no provider pass was reserved",
    "manualRequired": True, "ok": False,
    "workflow": {"blocker": {"approval_required": False, "boundary": "runtime", "code": "executable_missing", "retryable": False},
                 "next_action": "Install the configured executable for claude.", "phase": "prepare",
                 "schema_version": 1, "state": "operator_decision"}})


def stub(directory: Path, name: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(0o755)
    return path


class Clone(unittest.TestCase):
    """The install step, run between a session that exits 0 and the check."""

    def setUp(self):
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("root", "db", "item", "runner", "provider"):
            setattr(self, name, getattr(self.fixture, name))
        self.bin = self.root / "operator-bin"
        self.bin.mkdir()
        # The installer resolves here only: /usr/bin and /bin hold no npm.
        self.runner.search_path = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        self.checked = self.root / "check-ran"
        # The check fails the way assignment 106's did when nothing was installed.
        self.check = [sys.executable, str(self.script("check.py", "import sys\nfrom pathlib import Path\n"
            f"Path({str(self.checked)!r}).write_text('ran')\n"
            "if not Path('node_modules/.installed').exists():\n"
            "    sys.stderr.write('sh: eslint: command not found\\n')\n    sys.exit(127)\n"))]

    def script(self, name, source):
        path = self.root / name
        path.write_text(source)
        return path

    def with_lockfile(self):
        self.provider.write_text("import subprocess\nfrom pathlib import Path\n"
            "Path('work.txt').write_text('authored work\\n')\nPath('package-lock.json').write_text('{}\\n')\n"
            "subprocess.run(['git','add','work.txt','package-lock.json'],check=True)\n"
            "subprocess.run(['git','commit','-qm','Implement fixture\\n\\nAuthored-with: fixture/fixture'],check=True)\n")

    def run_fixture(self):
        request = self.fixture.claim()
        result = self.runner.execute(self.db, request, command=[sys.executable, str(self.provider)],
                                     environment={"PATH": os.environ["PATH"], "HOME": str(self.root)}, check=self.check)
        return request, result

    def followups(self):
        return self.db.execute("SELECT * FROM note WHERE item=? AND kind='followup'", (self.item,)).fetchall()

    def assert_environment(self, request, result, *fragments):
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertEqual(result["end_step"], "released", result)
        self.assertTrue(result["detail"].startswith("environment: "), result["detail"])
        self.assertNotIn("hard stop", result["detail"])
        for fragment in fragments:
            self.assertIn(fragment, result["detail"])
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "blocked")
        self.assertEqual(self.followups(), [], "an environment block files no followup for the next session")

    def test_a_lockfile_is_installed_in_the_clone_before_the_check(self):
        self.with_lockfile()
        called = self.root / "npm-called"
        stub(self.bin, "npm", "import os,sys\nfrom pathlib import Path\nPath('node_modules').mkdir()\n"
             "Path('node_modules/.installed').write_text('')\n"
             f"Path({str(called)!r}).write_text(' '.join(sys.argv[1:]) + '\\n' + os.getcwd())\n")
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "done", result)
        args, cwd = called.read_text().splitlines()
        self.assertEqual(args, "ci")
        self.assertEqual(Path(cwd).resolve(), Path(result["work_path"]).resolve())

    def test_a_failed_install_is_an_environment_block_and_the_check_never_runs(self):
        self.with_lockfile()
        stub(self.bin, "npm", "import sys\nsys.stderr.write('npm error code EUSAGE: lock file out of sync\\n')\nsys.exit(1)\n")
        request, result = self.run_fixture()
        self.assert_environment(request, result, "dependency install `npm ci` exited 1", "lock file out of sync")
        self.assertFalse(self.checked.exists(), "the check does not run on a clone whose install failed")

    def test_a_missing_installer_is_an_environment_block_naming_it(self):
        self.with_lockfile()
        request, result = self.run_fixture()
        self.assert_environment(request, result, "npm not found for package-lock.json")
        self.assertFalse(self.checked.exists())

    def test_without_a_lockfile_nothing_is_installed(self):
        marker = self.root / "npm-ran"
        stub(self.bin, "npm", f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
        self.check = [sys.executable, str(self.script("pass.py", "pass\n"))]
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "done", result)
        self.assertFalse(marker.exists())

    def test_no_runnable_reviewer_blocks_before_the_session_starts(self):
        """Assignment 111: a codex author whose only other-vendor reviewer does not resolve."""
        started = self.root / "session-ran"
        self.provider.write_text(f"from pathlib import Path\nPath({str(started)!r}).write_text('ran')\n")
        reviewers = [SimpleNamespace(name="claude", vendor="anthropic", start="claude -p"),
                     SimpleNamespace(name="codex", vendor="openai", start="codex exec")]
        stub(self.bin, "codex", "pass\n")
        resolved = ([sys.executable, str(self.provider)], {"PATH": os.environ["PATH"], "HOME": str(self.root)},
                    {"provider": "codex", "vendor": "openai", "reviewers": reviewers})
        with patch.object(runtime, "provider_command", return_value=resolved):
            request = self.fixture.claim()
            result = self.runner.execute(self.db, request)
        self.assert_environment(request, result, "review provider executable not found: claude (claude)", str(self.bin))
        self.assertFalse(started.exists(), "no session is spent on work nothing here can review")

    def test_a_ship_readiness_refusal_for_a_missing_executable_is_an_environment_block(self):
        pack = self.root / "pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin/sd-ship").write_text(f"import sys\nsys.stdout.write({SHIP_REFUSAL.split(': ', 1)[1]!r})\nsys.exit(3)\n")
        resolved = ([sys.executable, str(self.provider)], {"PATH": os.environ["PATH"], "HOME": str(self.root)},
                    {"provider": "fixture", "vendor": "fixture"})
        self.runner.config = runtime.Config(**{**self.fixture.config.__dict__, "pack": pack})
        with patch.object(runtime, "provider_command", return_value=resolved):
            request = self.fixture.claim()
            result = self.runner.execute(self.db, request)
        self.assert_environment(request, result, "executable_missing", "Install the configured executable for claude.")


class SearchPath(unittest.TestCase):
    def test_configured_then_login_then_inherited_first_mention_wins(self):
        self.assertEqual(toolchain.search_path(["/opt/tools", "relative"], ["/home/u/.local/bin", "/usr/bin"], ["/usr/bin", "/bin"]),
                         os.pathsep.join(["/opt/tools", "/home/u/.local/bin", "/usr/bin", "/bin"]))

    def test_resolve_tools_puts_the_login_path_in_front_of_the_inherited_one(self):
        fixture = test_runtime.Fixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        login = fixture.root / "login-bin"
        stub(login, "claude", "pass\n")
        registry = SimpleNamespace(providers={"claude": SimpleNamespace(name="claude", vendor="anthropic", start="claude -p"),
                                              "gone": SimpleNamespace(name="gone", vendor="x", start="gone-program run"),
                                              "remote": SimpleNamespace(name="remote", vendor="y", start=None)})
        config = runtime.Config(**{**fixture.config.__dict__, "path": ("/opt/configured",)})
        runner = runtime.Runner(config, freezer=lambda path: None)
        module = SimpleNamespace(read_or_report=lambda **kwargs: (registry, ""))
        with patch.object(runtime, "registry_module", return_value=module), patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            runner.resolve_tools(probe=lambda: [str(login)])
            environment = runner.tool_environment({"id": "run-1", "provider": "claude"}, remote=True)
            self.assertEqual(runner.search_path, os.pathsep.join(["/opt/configured", str(login), "/usr/bin", "/bin"]))
        self.assertEqual(environment["PATH"], os.pathsep.join(["/opt/configured", str(login), "/usr/bin", "/bin"]),
                         "sd-ship's readiness searches what the runner resolved")
        self.assertEqual(runner.executables, {"claude": str(login / "claude"), "gone": None})
        # The install, the check and sd-ship commit as the run's provider (sd:2544).
        self.assertEqual((environment["SD_ASSIGNMENT"], environment["SD_AUTHOR"]), ("run-1", "claude"))

    def test_login_path_reads_between_the_markers_and_ignores_profile_noise(self):
        marker = toolchain.MARKER
        answer = subprocess.CompletedProcess([], 0, stdout=f"Using Node v22\n{marker}/a/bin:/b/bin{marker}", stderr="")
        self.assertEqual(toolchain.login_path(run=lambda *args, **kwargs: answer), ["/a/bin", "/b/bin"])

    def test_a_login_probe_that_fails_contributes_nothing(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="profile error")
        self.assertEqual(toolchain.login_path(run=lambda *args, **kwargs: failed), [])

        def hang(*args, **kwargs):
            raise subprocess.TimeoutExpired("sh", toolchain.LOGIN_PROBE_SECONDS)
        self.assertEqual(toolchain.login_path(run=hang), [])

    def test_runner_json_path_takes_a_list_or_a_path_string(self):
        self.assertEqual(cli.configured_path(["/a", "/b"]), ("/a", "/b"))
        self.assertEqual(cli.configured_path("/a:/b"), ("/a", "/b"))
        self.assertEqual(cli.configured_path(()), ())


class Classifiers(unittest.TestCase):
    def test_reviewer_check_needs_one_other_vendor_program_on_the_path(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reviewers = [SimpleNamespace(name="claude", vendor="anthropic", start="claude -p"),
                     SimpleNamespace(name="codex", vendor="openai", start="codex exec")]
        stub(root, "codex", "pass\n")
        with self.assertRaisesRegex(toolchain.EnvironmentBlock, r"^environment: review provider executable not found: claude \(claude\)"):
            toolchain.require_reviewer(reviewers, "openai", str(root))
        stub(root, "claude", "pass\n")
        toolchain.require_reviewer(reviewers, "openai", str(root))
        # No reviewer of another vendor at all is policy, and sd-ship names it.
        toolchain.require_reviewer(reviewers[1:], "openai", str(root))

    def test_ship_refusal_is_environment_only_for_executable_missing(self):
        block = toolchain.from_ship_refusal(SHIP_REFUSAL)
        self.assertEqual(str(block), "environment: sd-ship readiness: executable_missing: Install the configured executable for claude.")
        other = SHIP_REFUSAL.replace("executable_missing", "consent_missing")
        self.assertIsNone(toolchain.from_ship_refusal(other))
        self.assertIsNone(toolchain.from_ship_refusal("ship adapter exited 1: sd-ship: remote unreachable"))
        self.assertIsNone(toolchain.from_ship_refusal(SHIP_REFUSAL[:200]))

    def test_installer_picks_the_first_lockfile_and_makes_its_program_absolute(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        clone, tools = root / "clone", root / "bin"
        clone.mkdir()
        self.assertIsNone(toolchain.installer(clone, str(tools)))
        (clone / "pnpm-lock.yaml").write_text("")
        pnpm = stub(tools, "pnpm", "pass\n")
        self.assertEqual(toolchain.installer(clone, str(tools)), [str(pnpm), "install", "--frozen-lockfile"])
        (clone / "package-lock.json").write_text("{}")
        with self.assertRaisesRegex(toolchain.EnvironmentBlock, "npm not found for package-lock.json"):
            toolchain.installer(clone, str(tools))


if __name__ == "__main__":
    unittest.main()
