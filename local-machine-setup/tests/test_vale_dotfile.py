"""~/.vale.ini is a tracked dotfile.

Vale reads the first .vale.ini above the current directory, so one at $HOME
governs every checkout without its own. It names style packages and no
secret, which makes it a dotfile the stage can carry between machines like
.gitconfig. The fixture config is copied so a case can add the tracked copy;
the home is bare.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"

TRACKED = "StylesPath = .vale/styles\nMinAlertLevel = suggestion\n\n[*.{md,txt}]\nBasedOnStyles = Vale, write-good\n"


class ValeDotfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.config = fixture_config.copy_config(base)
        self.tracked = self.config / "machine-setup/dotfiles/common/.vale.ini"
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.installed = self.home / ".vale.ini"

    def run_stage(self, *flags):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
        }
        result = subprocess.run([str(SCRIPT), "update", "dotfiles", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_an_uncaptured_vale_ini_is_named_and_not_drift(self):
        out = self.run_stage()

        self.assertIn("--      .vale.ini not captured for personal yet", out)
        self.assertNotIn("MISSING .vale.ini", out)

    def test_a_missing_vale_ini_is_drift_and_apply_installs_the_tracked_copy(self):
        self.tracked.write_text(TRACKED)

        dry = self.run_stage()
        self.assertIn("MISSING .vale.ini", dry)
        self.assertFalse(self.installed.exists())

        self.run_stage("--apply")
        self.assertEqual(self.installed.read_text(), TRACKED)
        self.assertIn("ok      .vale.ini", self.run_stage())


if __name__ == "__main__":
    unittest.main()
