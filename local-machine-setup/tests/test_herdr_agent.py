"""brew services' herdr is drift, and the bash launcher waits for it (sd:6427).

brew services' homebrew.mxcl.herdr starts herdr itself, so macOS charges every
session's file access to herdr, whose grant each upgrade drops. The
<prefix>.herdr agent starts it under Apple's /bin/bash. The agents stage
reports a loaded homebrew.mxcl.herdr, and starts no second server beside it.
It never stops herdr: that ends every session on the machine.

`launchctl` is a stub that logs its calls; `print` answers loaded for
homebrew.mxcl.herdr when BREW_HERDR is set, and not loaded for anything else.
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
FOLDER = HERE.parent
LABEL = f"{fixture_config.LABEL_PREFIX}.herdr"

LAUNCHCTL = r"""#!/bin/sh
echo "launchctl $*" >> "$LAUNCHCTL_LOG"
case "$1:$2" in
  print:*/homebrew.mxcl.herdr) [ -n "$BREW_HERDR" ] ;;
  print:*) exit 113 ;;
esac
"""


class HerdrAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.config = fixture_config.copy_config(base)
        agents = self.config / "machine-setup"
        (agents / "profiles/personal.agent").write_text(f"{LABEL}\n")
        shutil.copy(FOLDER / f"examples/launchagents/{LABEL}.plist", agents / f"launchagents/{LABEL}.plist")
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        stub = self.stubs / "launchctl"
        stub.write_text(LAUNCHCTL)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        fixture_config.seal(self, self.stubs)
        self.log = base / "launchctl.log"

    def run_stage(self, *flags, **extra):
        env = {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
               "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
               **fixture_config.env(self.config), "LAUNCHCTL_LOG": str(self.log), **extra}
        result = subprocess.run([str(FOLDER / "machine-setup.sh"), "update", "agents", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_loaded_brew_herdr_is_drift_and_holds_the_launcher(self):
        out = self.run_stage("--apply", BREW_HERDR="1")

        self.assertIn("DIFFERS homebrew.mxcl.herdr is loaded", out)
        self.assertIn(f"HELD    {LABEL}", out)
        self.assertFalse((self.home / f"Library/LaunchAgents/{LABEL}.plist").exists())
        self.assertEqual([call for call in self.calls() if not call.startswith("launchctl print")], [])

    def test_with_brew_herdr_stopped_the_launcher_installs(self):
        out = self.run_stage("--apply")

        self.assertNotIn("homebrew.mxcl.herdr", out)
        self.assertIn(f"MISSING {LABEL}", out)
        dst = self.home / f"Library/LaunchAgents/{LABEL}.plist"
        self.assertIn("<string>/opt/homebrew/opt/herdr/bin/herdr server; exit $?</string>", dst.read_text())
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} {dst}", self.calls())

    def test_a_profile_without_the_launcher_still_reports_brew_herdr(self):
        (self.config / "machine-setup/profiles/personal.agent").write_text("# none\n")
        out = self.run_stage(BREW_HERDR="1")

        self.assertIn("DIFFERS homebrew.mxcl.herdr is loaded", out)


if __name__ == "__main__":
    unittest.main()
