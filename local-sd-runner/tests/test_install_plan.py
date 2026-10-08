"""The runner's LaunchAgent passes SYSTEM_TOOLS_CONFIG (.claude/rules/services.md).

launchd gives an agent only the environment its plist names, so without it
the runner and every child it starts (`sd-ship`, the pre-push leak guard)
read the default config root instead of the one the operator installed with.
"""

from __future__ import annotations

import os
import plistlib
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_runner import cli, storage
from sd_runner.runtime import Config


def environment(**env):
    config = Config(Path("/db"), Path("/work"), Path("/retained"), Path("/pack"), Path("/home/example"))
    with patch.dict(os.environ, env, clear=True), patch.object(storage, "preflight", return_value={}):
        return plistlib.loads(cli.install_plan(config)["plist"].encode())["EnvironmentVariables"]


class InstallPlan(unittest.TestCase):
    def test_the_plist_passes_the_config_root_it_was_installed_with(self):
        self.assertEqual(environment(SYSTEM_TOOLS_CONFIG="/config/system")["SYSTEM_TOOLS_CONFIG"], "/config/system")

    def test_an_unset_config_root_stays_unset(self):
        self.assertNotIn("SYSTEM_TOOLS_CONFIG", environment())


if __name__ == "__main__":
    unittest.main()
