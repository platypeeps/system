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
import shutil
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# A tool that installs rather than runs a container: its `status` answers
# from FAKE_RC and FAKE_OUT, and `install` is logged.
TOOL_STUB = """#!/bin/sh
case "$1" in
  status) [ -z "$FAKE_OUT" ] || printf '%s\\n' "$FAKE_OUT"; exit "${FAKE_RC:-0}" ;;
  install) echo install >> "$FAKE_LOG"; echo "installed" ;;
esac
"""


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


class InstallTool(unittest.TestCase):
    """A `.service` line `<folder> install` (sd:9000): the stage reads the
    folder's `status` and `--apply` runs its `install`; docker plays no part.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        shutil.copytree(FOLDER, repo / "local-machine-setup",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.script = repo / "local-machine-setup/machine-setup.sh"
        tool = repo / "local-trellis/trellis.sh"
        tool.parent.mkdir()
        tool.write_text(TOOL_STUB)
        tool.chmod(0o755)
        self.config = fixture_config.copy_config(base)
        self.service = self.config / "machine-setup/profiles/personal.service"
        self.service.write_text("local-trellis install\n")
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.log = base / "install.log"
        # No docker on PATH at all: the tool must be checked without it.
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        fixture_config.seal(self, self.stubs)

    def stage(self, *extra, **env):
        e = {"HOME": str(self.home), "MACHINE_SETUP_STATE": str(self.state),
             "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
             "FAKE_LOG": str(self.log), **fixture_config.env(self.config), **env}
        result = subprocess.run([str(self.script), "update", "services", *extra], env=e,
                                capture_output=True, text=True, cwd=self.tmp.name)
        return result

    def test_an_installed_tool_is_ok_and_not_installed_again(self):
        r = self.stage(FAKE_RC="0", FAKE_OUT="ok      trellis-mac d58628f")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("  ok      local-trellis installed", r.stdout)
        self.assertNotIn("[dry-run]", r.stdout)
        self.assertNotIn("docker MISSING", r.stdout)

    def test_a_tool_with_nothing_installed_is_missing_and_install_runs(self):
        r = self.stage(FAKE_RC="3", FAKE_OUT="MISSING trellis-mac at x")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("  MISSING trellis-mac at x", r.stdout)
        self.assertRegex(r.stdout, r"\[dry-run\] \S+/local-trellis/trellis.sh install")

    def test_a_tool_that_drifted_names_each_gap_and_apply_installs(self):
        r = self.stage("--apply", FAKE_RC="1",
                       FAKE_OUT="DIFFERS no Hugging Face token -- run: hf auth login")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("  DIFFERS no Hugging Face token -- run: hf auth login", r.stdout)
        self.assertEqual(self.log.read_text(), "install\n")

    def test_a_drift_exit_with_no_drift_word_still_counts(self):
        r = self.stage(FAKE_RC="1", FAKE_OUT="something is off")
        self.assertIn("  DIFFERS local-trellis status exited 1", r.stdout)

    def test_a_status_that_fails_otherwise_is_failed_and_installs_nothing(self):
        r = self.stage("--apply", FAKE_RC="2", FAKE_OUT="")
        self.assertIn("  FAILED  local-trellis status exited 2", r.stdout)
        self.assertFalse(self.log.exists())

    def test_an_unknown_word_after_the_folder_is_skipped(self):
        self.service.write_text("local-trellis maybe\n")
        r = self.stage(FAKE_RC="3")
        self.assertIn("  SKIP    local-trellis maybe", r.stdout)
        self.assertNotIn("trellis.sh install", r.stdout)


if __name__ == "__main__":
    unittest.main()
