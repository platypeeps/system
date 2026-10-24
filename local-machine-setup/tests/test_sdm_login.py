"""~/.local/bin/sdm-login, a dotfile the work profile carries (sd:5919).

StrongDM's `sdm` signs out now and then, which blocks every tunnel through
it. The helper signs it in only when `sdm status` says signed out (exit 9),
then prints `sdm status`. The dotfiles stage installs it from
dotfiles/<profile>/, so a profile without a copy gets none. The committed
copy is examples/dotfiles/work/.local/bin/sdm-login; each case puts it in a
copy of the fixture config. The email comes from SDM_EMAIL or the config
folder's machine-setup.env, never from the repository. A stub `sdm` records
each call; the home is bare.
"""

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"
HELPER = HERE.parent / "examples/dotfiles/work/.local/bin/sdm-login"
NAME = ".local/bin/sdm-login"

# `sdm status` answers with $SDM_STUB_STATUS until a login, then 0.
STUB = """#!/bin/sh
echo "sdm $*" >> "$SDM_STUB_LOG"
case "$1" in
  status)
    if [ -f "$SDM_STUB_LOG.in" ]; then echo "signed in"; exit 0; fi
    echo "status says $SDM_STUB_STATUS"; exit "$SDM_STUB_STATUS" ;;
  login) touch "$SDM_STUB_LOG.in"; exit "${SDM_STUB_LOGIN:-0}" ;;
esac
exit 64
"""


def executable(path):
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class SdmLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.config = fixture_config.copy_config(base)
        self.tracked = self.config / "machine-setup/dotfiles/work" / NAME
        self.tracked.parent.mkdir(parents=True)
        shutil.copyfile(HELPER, self.tracked)
        executable(self.tracked)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        self.installed = self.home / NAME
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        (self.stubs / "sdm").write_text(STUB)
        executable(self.stubs / "sdm")
        self.log = base / "sdm.log"

    def env(self, **extra):
        return {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
                "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "LANG": "en_US.UTF-8", "SDM_STUB_LOG": str(self.log),
                **fixture_config.env(self.config), **extra}

    def run_stage(self, profile, *flags):
        (self.state / "profile").write_text(f"{profile}\n")
        result = subprocess.run([str(SCRIPT), "update", "dotfiles", *flags], env=self.env(),
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def helper(self, status, *args, **extra):
        """Runs the installed helper with `sdm status` answering `status`; returns the result and the calls."""
        self.run_stage("work", "--apply")
        result = subprocess.run([str(self.installed), *args], env=self.env(SDM_STUB_STATUS=str(status), **extra),
                                capture_output=True, text=True, cwd=self.tmp.name, stdin=subprocess.DEVNULL)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def write_email(self, line):
        (self.config / "machine-setup/machine-setup.env").write_text(line + "\n")

    def test_a_work_apply_installs_the_helper(self):
        self.assertIn(f"MISSING {NAME}", self.run_stage("work"))
        self.assertFalse(self.installed.exists())
        self.run_stage("work", "--apply")
        self.assertEqual(self.installed.read_bytes(), HELPER.read_bytes())
        self.assertTrue(os.access(self.installed, os.X_OK))
        self.assertIn(f"ok      {NAME}", self.run_stage("work"))

    def test_a_copy_that_lost_its_mode_still_installs_executable(self):
        # A config folder moved through a share mounted noowners drops the bit.
        self.tracked.chmod(0o644)
        self.run_stage("work", "--apply")
        self.assertTrue(os.access(self.installed, os.X_OK))

    def test_a_copy_whose_chmod_failed_is_not_ok_and_is_made_executable(self):
        # sd:5940: the content matches, so a compare of content alone read ok.
        self.run_stage("work", "--apply")
        self.installed.chmod(0o644)
        self.assertIn(f"DIFFERS {NAME} — mode is not 755", self.run_stage("work"))
        self.assertFalse(os.access(self.installed, os.X_OK))
        self.run_stage("work", "--apply")
        self.assertEqual(stat.S_IMODE(self.installed.stat().st_mode), 0o755)
        self.assertIn(f"ok      {NAME}", self.run_stage("work"))

    def test_a_symlinked_helper_is_judged_by_its_target(self):
        # sd:5960: the probe read the link's own mode (0755 on macOS) while cmp and chmod follow it.
        target = pathlib.Path(self.tmp.name) / "elsewhere" / "sdm-login"
        target.parent.mkdir()
        shutil.copyfile(HELPER, target)
        target.chmod(0o644)
        self.installed.parent.mkdir(parents=True)
        self.installed.symlink_to(target)
        self.assertIn(f"DIFFERS {NAME} — mode is not 755", self.run_stage("work"))
        self.run_stage("work", "--apply")
        self.assertTrue(self.installed.is_symlink())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertIn(f"ok      {NAME}", self.run_stage("work"))

    def test_a_hand_written_helper_is_kept_until_force(self):
        # A machine that wrote its own helper before this one: nothing recorded says which moved.
        self.installed.parent.mkdir(parents=True)
        self.installed.write_text("#!/bin/sh\nsdm login\n")
        out = self.run_stage("work", "--apply")
        self.assertIn(f"DIFFERS {NAME} — machine and repo disagree", out)
        self.assertEqual(self.installed.read_text(), "#!/bin/sh\nsdm login\n")
        self.run_stage("work", "--apply", "--force")
        self.assertEqual(self.installed.read_bytes(), HELPER.read_bytes())
        self.assertTrue(os.access(self.installed, os.X_OK))

    def test_a_profile_without_a_copy_installs_none(self):
        out = self.run_stage("personal", "--apply")
        self.assertIn(f"--      {NAME} not captured for personal yet", out)
        self.assertFalse(self.installed.exists())

    def test_signed_in_prints_status_and_logs_nothing_in(self):
        result, calls = self.helper(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["sdm status"])
        self.assertIn("status says 0", result.stdout)

    def test_signed_out_logs_in_with_the_configured_email_then_prints_status(self):
        self.write_email('SDM_EMAIL="ops@example.test"')
        result, calls = self.helper(9)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["sdm status", "sdm login --email ops@example.test", "sdm status"])
        self.assertIn("signed in", result.stdout)

    def test_an_exported_email_wins_over_the_file(self):
        self.write_email("SDM_EMAIL=file@example.test")
        result, calls = self.helper(9, SDM_EMAIL="env@example.test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sdm login --email env@example.test", calls)

    def test_signed_out_with_no_email_names_both_remedies_and_logs_nothing_in(self):
        for line in (None, "SDM_EMAIL=change-me"):
            with self.subTest(line=line):
                if line:
                    self.write_email(line)
                self.log.unlink(missing_ok=True)
                result, calls = self.helper(9)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, ["sdm status"])
                self.assertIn("SDM_EMAIL", result.stderr)
                self.assertIn("export SDM_EMAIL", result.stderr)
                self.assertIn("machine-setup.env.example", result.stderr)
                self.assertIn(str(self.config / "machine-setup/machine-setup.env"), result.stderr)

    def test_another_status_failure_is_not_signed_out(self):
        # Exit 9 is the one signed-out answer; any other failure is unknown, so nothing logs in.
        self.write_email("SDM_EMAIL=ops@example.test")
        result, calls = self.helper(1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["sdm status"])
        self.assertIn("status says 1", result.stderr)

    def test_a_failed_login_exits_with_its_code(self):
        self.write_email("SDM_EMAIL=ops@example.test")
        result, calls = self.helper(9, SDM_STUB_LOGIN="3")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(calls, ["sdm status", "sdm login --email ops@example.test"])


if __name__ == "__main__":
    unittest.main()
