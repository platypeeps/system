"""worldmonitor.sh resolves its checkout from config, never a fixed path."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "worldmonitor.sh"


class WorldmonitorTest(unittest.TestCase):
    def run_script(self, *args, **env):
        base = {k: v for k, v in os.environ.items() if k != "WORLDMONITOR_DIR"}
        base.update(env)
        return subprocess.run(["sh", str(SCRIPT), *args], env=base,
                              capture_output=True, text=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config"

    def test_help_exits_zero(self):
        result = self.run_script("help", SYSTEM_TOOLS_CONFIG=str(self.config))
        self.assertEqual(result.returncode, 0)
        self.assertIn("WORLDMONITOR_DIR", result.stdout)

    def test_no_args_prints_usage_and_fails(self):
        result = self.run_script(SYSTEM_TOOLS_CONFIG=str(self.config))
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stderr)

    def test_missing_dir_names_variable_and_remedies(self):
        result = self.run_script("lint", SYSTEM_TOOLS_CONFIG=str(self.config))
        self.assertEqual(result.returncode, 1)
        self.assertIn("WORLDMONITOR_DIR is not set", result.stderr)
        self.assertIn("Export it", result.stderr)
        self.assertIn("local-worldmonitor/.env.example", result.stderr)

    def test_dir_from_config_env(self):
        app = Path(self.tmp.name) / "app"
        app.mkdir()
        # A stub npm on PATH records the directory it ran in.
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        npm = bin_dir / "npm"
        npm.write_text('#!/bin/sh\necho "npm $* in $(pwd -P)"\n')
        npm.chmod(0o755)
        (self.config / "worldmonitor").mkdir(parents=True)
        (self.config / "worldmonitor" / ".env").write_text(f"WORLDMONITOR_DIR={app}\n")
        result = self.run_script("lint", SYSTEM_TOOLS_CONFIG=str(self.config),
                                 PATH=f"{bin_dir}:{os.environ['PATH']}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"npm run lint in {app.resolve()}", result.stdout)


if __name__ == "__main__":
    unittest.main()
