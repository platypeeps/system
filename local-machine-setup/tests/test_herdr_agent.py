"""No agent starts herdr, and brew services' herdr is drift (sd:6466).

A server launchd starts is charged to herdr, whose grant each upgrade drops;
one the first herdr client starts from a terminal is covered by the
terminal's grant. The agents stage reports a loaded homebrew.mxcl.herdr and
never stops it: that ends every session on the machine.

`launchctl` is a stub that logs its calls; `print` answers loaded for
homebrew.mxcl.herdr when BREW_HERDR is set, and not loaded for anything else.
BREW_HERDR=silent exits 0 and prints nothing, as test_backbone's stub does.
"""

import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent

LAUNCHCTL = r"""#!/bin/sh
echo "launchctl $*" >> "$LAUNCHCTL_LOG"
case "$1:$2" in
  print:*/homebrew.mxcl.herdr)
    [ -n "$BREW_HERDR" ] || exit 113
    [ "$BREW_HERDR" = silent ] || echo "$2 = {" ;;
  print:*) exit 113 ;;
esac
"""


class HerdrAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.config = fixture_config.copy_config(base)
        (self.config / "machine-setup/profiles/personal.agent").write_text("# none\n")
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

    def test_no_herdr_agent_ships(self):
        """sd:6466: under Apple's bash, herdr was still charged to itself."""
        self.assertEqual(list((FOLDER / "examples/launchagents").glob("*herdr*")), [])

    def test_a_loaded_brew_herdr_is_drift_and_is_not_stopped(self):
        out = self.run_stage("--apply", BREW_HERDR="1")

        self.assertIn("DIFFERS homebrew.mxcl.herdr is loaded", out)
        self.assertEqual([call for call in self.calls() if not call.startswith("launchctl print")], [])

    def test_a_profile_that_still_names_a_herdr_agent_is_told_to_retire_it(self):
        """A profile and template from before sd:6466 are neither installed
        nor started; the stage names the manual steps."""
        label = "local.system-tools.herdr"
        (self.config / "machine-setup/profiles/personal.agent").write_text(label + "\n")
        agents = self.config / "machine-setup/launchagents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{label}.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            f"<key>Label</key><string>{label}</string></dict></plist>\n")

        out = self.run_stage("--apply")

        self.assertIn(f"DIFFERS {label} — retired (sd:6466)", out)
        self.assertFalse((self.home / f"Library/LaunchAgents/{label}.plist").exists())
        self.assertEqual([call for call in self.calls() if not call.startswith("launchctl print")], [])

    def test_an_exit_0_that_names_no_service_is_not_brew_herdr(self):
        """Evidence: a print that names no service, as a stub's bare
        `exit 0`, reports no drift."""
        out = self.run_stage(BREW_HERDR="silent")

        self.assertNotIn("homebrew.mxcl.herdr", out)

if __name__ == "__main__":
    unittest.main()
