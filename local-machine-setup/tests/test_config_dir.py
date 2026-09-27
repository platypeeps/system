"""The profiles live outside the repository, in $SYSTEM_TOOLS_CONFIG/machine-setup.

A machine that has not been given its configuration yet must hear so once,
with the path it looked in, rather than run every stage against empty
manifests. The shipped examples/ folder is a working configuration: copied
into place, it is what a new operator starts from.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
SCRIPT = FOLDER / "machine-setup.sh"


class ConfigDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)

    def run_script(self, *args, config):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
            **fixture_config.env(config),
        }
        return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True,
                              cwd=self.tmp.name)

    def test_a_missing_config_dir_names_the_path_and_the_examples(self):
        config = self.base / "nowhere"
        for verb in (["profiles"], ["report"], ["setup"], ["compare", "a", "b"]):
            with self.subTest(verb=verb):
                result = self.run_script(*verb, config=config)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"no profile directory at {config}/machine-setup/profiles", result.stderr)
                self.assertIn(f"cp -R {FOLDER}/examples/.", result.stderr)

    def test_the_profiles_are_read_from_the_config(self):
        result = self.run_script("profiles", config=fixture_config.CONFIG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["personal", "work", "(plus common, which layers under every profile)"])

    def test_an_unknown_profile_lists_the_known_ones(self):
        result = self.run_script("report", "nosuch", config=fixture_config.CONFIG)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown profile 'nosuch' (want: personal, work)", result.stderr)

    def test_the_examples_are_a_working_config(self):
        config = self.base / "config"
        shutil.copytree(FOLDER / "examples", config / "machine-setup")
        result = self.run_script("profiles", config=config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "laptop")
        result = self.run_script("report", "laptop", config=config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local.system-tools.sd-dashboard", result.stdout)
        self.assertIn("497799835 Xcode", result.stdout)


if __name__ == "__main__":
    unittest.main()
