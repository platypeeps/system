"""The services stage finds a running service by its container name (sd:2846).

`local-genai-traces` runs as `local-genai-collector` and `local-genai-phoenix`,
not `genai-traces`. Read as the folder suffix, it looked stopped on every
run, so `update --apply` ran its `start` again each night. It counts as
running only while both are up: a stopped Phoenix is a stopped service
(sd:2852). A `docker` stub lists the compose containers; the stage runs as a
dry run, so no entrypoint starts anything.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"


class ServicesStage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.config = fixture_config.copy_config(base)
        (self.config / "machine-setup/profiles/personal.service").write_text("local-genai-traces\n")
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        self.running("local-genai-collector", "local-genai-phoenix")
        fixture_config.seal(self, self.stubs)

    def running(self, *names):
        """A `docker` stub whose `ps` lists `names`."""
        docker = self.stubs / "docker"
        listed = "".join(name + "\\n" for name in names)
        docker.write_text(f'#!/bin/sh\n[ "$1" = ps ] && printf "{listed}"\nexit 0\n')
        docker.chmod(0o755)

    def stage(self):
        env = {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
               "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}", "LANG": "en_US.UTF-8",
               **fixture_config.env(self.config)}
        result = subprocess.run([str(SCRIPT), "update", "services"], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_a_running_genai_traces_is_ok_and_not_started_again(self):
        result = self.stage()
        self.assertIn("  ok      local-genai-traces running", result.stdout)
        self.assertNotIn("[dry-run]", result.stdout)

    def test_a_stopped_phoenix_is_a_stopped_service_and_is_started(self):
        self.running("local-genai-collector")
        result = self.stage()
        self.assertNotIn("local-genai-traces running", result.stdout)
        self.assertIn("[dry-run]", result.stdout)
        self.assertIn("genai-traces.sh start", result.stdout)


if __name__ == "__main__":
    unittest.main()
