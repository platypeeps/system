"""What the entrypoint answers before `sd_plan.py` is ever reached (sd:1189).

`status`, `help` and the participation list are the shell's alone, and each
case here is a way the script once answered them wrongly: a duplicate path
counted twice, an unreadable list read as no list, a missing profile read as
`personal`, and a machine with only an old Python unable even to print help.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "sd-plan.sh"

#: A runner that answers `status` healthy and dispatching.
RUNNER = """\
#!/bin/sh
[ "$1" = status ] || exit 1
echo '{"healthy": true, "storage": {"dispatch_allowed": true}}'
"""


class EntrypointCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        # The conf sits beside the script, where a developer's own lists live,
        # so it is named for this case's temporary folder and nobody else's.
        self.profile = f"contract-{self.home.name}"
        self.conf = FOLDER / f"repos.{self.profile}.conf"
        self.assertFalse(self.conf.exists(), f"{self.conf} predates the case")
        self.addCleanup(self.remove_conf)
        self.runner = self.home / "runner.sh"
        self.runner.write_text(RUNNER, encoding="utf-8")
        self.runner.chmod(0o755)

    def remove_conf(self):
        if self.conf.is_dir():
            self.conf.rmdir()
        elif self.conf.exists():
            self.conf.chmod(0o644)
            self.conf.unlink()

    def plan(self, *args, expect=0, env=None, unset=()):
        environment = dict(os.environ)
        environment.update({
            "HOME": str(self.home),
            "SD_PLAN_PROFILE": self.profile,
            "MACHINE_SETUP_STATE": str(self.home / "absent"),
            "SD_PLAN_RUNNER": str(self.runner),
            "PYTHON": sys.executable,
        })
        for name in unset:
            environment.pop(name, None)
        environment.update(env or {})
        done = subprocess.run(["/bin/sh", str(ENTRYPOINT), *args],
                              capture_output=True, text=True, input="", env=environment)
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done


class TheParticipationList(EntrypointCase):
    def test_a_repeated_path_participates_once(self):
        self.conf.write_text("/Users/nobody/repos/one\n  /Users/nobody/repos/one  # again\n"
                             "/Users/nobody/repos/two\n", encoding="utf-8")
        done = self.plan("status")
        self.assertIn("2 repository(ies) participate", done.stdout)

    def test_a_directory_where_the_list_belongs_is_broken_not_absent(self):
        self.conf.mkdir()
        done = self.plan("status", expect=1)
        self.assertTrue(done.stdout.startswith("local-sd-plan: FAIL"), done.stdout)
        self.plan("nightly", "--dry-run", expect=1)

    def test_a_list_that_cannot_be_read_is_broken_not_absent(self):
        if os.geteuid() == 0:
            # Root reads a mode-000 file; the directory case covers the path.
            self.conf.mkdir()
        else:
            self.conf.write_text("/Users/nobody/repos/one\n", encoding="utf-8")
            self.conf.chmod(0)
        done = self.plan("status", expect=1)
        self.assertIn(self.conf.name, done.stdout)


class TheProfile(EntrypointCase):
    def test_no_recorded_profile_means_no_list_rather_than_personal(self):
        done = self.plan("status", expect=3, unset=("SD_PLAN_PROFILE",))
        self.assertIn("no machine-setup profile", done.stdout)
        self.assertNotIn("repos.personal.conf", done.stdout)


class TheInterpreter(EntrypointCase):
    def test_a_python_that_does_not_exist_is_not_called_too_old(self):
        done = self.plan("item", "1", "--dry-run", expect=1,
                         env={"PYTHON": str(self.home / "no-such-python")})
        self.assertNotIn("too old", done.stderr)
        self.assertIn("cannot be run", done.stderr)

    def test_help_usage_and_an_unconfigured_status_need_no_python(self):
        """Conventions 1 and 6 hold on a machine whose only Python is 3.9."""
        broken = {"PYTHON": str(self.home / "no-such-python")}
        self.assertIn("Usage: sd-plan.sh", self.plan("help", env=broken).stderr)
        self.assertIn("Usage: sd-plan.sh", self.plan(expect=1, env=broken).stderr)
        self.plan("status", expect=3, env=broken)

    def test_both_homebrew_prefixes_are_searched(self):
        """cron-jobs puts either prefix on PATH; the runner's PATH has neither."""
        text = ENTRYPOINT.read_text(encoding="utf-8")
        for prefix in ("/opt/homebrew", "/usr/local"):
            with self.subTest(prefix=prefix):
                # `assertTrue`, not `assertIn`: the failure would print the script.
                self.assertTrue(f"{prefix}/bin/python3" in text, f"{prefix} is not searched")


if __name__ == "__main__":
    unittest.main()
