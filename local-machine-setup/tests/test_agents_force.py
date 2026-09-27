"""A launch agent plist nothing was recorded for still yields to --force.

The agents stage's no-record DIFFERS line tells the reader to review, then
run 'capture --apply' or --force. Until the dotfiles stage was fixed (#420)
the same line there offered --force and --force did nothing; this is the
same branch in the agents stage. Runs are dry runs, so `run` prints the cp
and launchctl calls instead of making them.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
SCRIPT = FOLDER / "machine-setup.sh"
PROFILE = "personal"


def first_agent():
    for line in (fixture_config.PROFILES / f"{PROFILE}.agent").read_text().splitlines():
        label = line.strip()
        if label and not label.startswith("#") and (fixture_config.AGENTS / f"{label}.plist").is_file():
            return label
    raise AssertionError(f"no {PROFILE}.agent label has a plist in launchagents/")


class AgentsForceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text(f"{PROFILE}\n")
        self.label = first_agent()
        plist = self.home / "Library/LaunchAgents" / f"{self.label}.plist"
        plist.parent.mkdir(parents=True)
        # Installed as the stage would render it, then edited by hand.
        rendered = fixture_config.render(fixture_config.AGENTS / f"{self.label}.plist",
                                         self.label, self.home, FOLDER.parent)
        plist.write_text(rendered + "<!-- edited -->\n")

    def run_stage(self, *flags):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
        }
        result = subprocess.run([str(SCRIPT), "update", "agents", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.splitlines()

    def verdict(self, lines):
        return [line for line in lines if f" {self.label} " in line and not line.startswith("  [dry-run]")]

    def test_without_force_the_stage_only_reports(self):
        lines = self.run_stage()
        self.assertEqual(self.verdict(lines),
                         [f"  DIFFERS {self.label} — machine and repo disagree, nothing recorded says which moved; "
                          "review, then 'capture --apply' or --force"])
        self.assertFalse([line for line in lines if line.startswith("  [dry-run]") and self.label in line])

    def test_force_overwrites_a_plist_nothing_was_recorded_for(self):
        lines = self.run_stage("--force")
        self.assertEqual(self.verdict(lines),
                         [f"  DIFFERS {self.label} — nothing recorded; --force overwrites (backup kept)"])
        planned = [line for line in lines if line.startswith("  [dry-run]")]
        self.assertTrue(any(" cp " in line and self.label in line for line in planned), lines)
        self.assertTrue(any("launchctl bootstrap" in line for line in planned), lines)


if __name__ == "__main__":
    unittest.main()
