"""Regression tests for cswap.sh.

Each case runs a throwaway copy of the script, laid out beside a copy of
lib/config.sh the way the tree is, with HOME pointed at a directory of its
own. `launchctl` is a stub on PATH, so no case asks the real launchd
anything, or registers anything.
"""

import pathlib
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB_CONFIG = FOLDER.parent / "lib" / "config.sh"

# launchd holds no label: `launchctl print` fails, anything else succeeds.
# Every call is logged so a case can assert the stub answered.
LAUNCHCTL = """#!/bin/sh
echo "launchctl $*" >> "$CSWAP_TEST_CALLS"
[ "$1" != print ] || exit 113
exit 0
"""


class StartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cswap-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        folder = self.tmp / "local-cswap"
        folder.mkdir()
        for name in ("cswap.sh", "cswap-auto.plist.template"):
            shutil.copy(FOLDER / name, folder / name)
        (self.tmp / "lib").mkdir()
        shutil.copy(LIB_CONFIG, self.tmp / "lib" / "config.sh")
        self.script = folder / "cswap.sh"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.calls = self.tmp / "calls.txt"
        stubs = self.tmp / "stub-bin"
        stubs.mkdir()
        path = stubs / "launchctl"
        path.write_text(LAUNCHCTL)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.path = f"{stubs}:/usr/bin:/bin"

    def start(self, **extra):
        env = {"PATH": self.path, "HOME": str(self.home),
               "CSWAP_TEST_CALLS": str(self.calls), **extra}
        result = subprocess.run(["bash", str(self.script), "start"], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("launchctl bootstrap gui/", self.calls.read_text())
        agent = (self.home / "Library" / "LaunchAgents"
                 / "local.system-tools.cswap-auto.plist")
        return plistlib.loads(agent.read_bytes())

    def test_start_passes_the_config_root_to_the_agent(self):
        # REGRESSION (sd:1959). launchd passes only the environment the plist
        # names, and the plist named PATH and HOME, so a non-default
        # SYSTEM_TOOLS_CONFIG never reached the agent.
        config = str(self.tmp / "config|x")
        plist = self.start(SYSTEM_TOOLS_CONFIG=config)
        self.assertEqual(plist["EnvironmentVariables"]["SYSTEM_TOOLS_CONFIG"], config)

    def test_start_passes_the_default_config_root_when_unset(self):
        # PIN. Unset, the agent gets the root lib/config.sh resolves.
        plist = self.start()
        self.assertEqual(plist["EnvironmentVariables"]["SYSTEM_TOOLS_CONFIG"],
                         str(self.home / ".config" / "system"))


if __name__ == "__main__":
    unittest.main()
