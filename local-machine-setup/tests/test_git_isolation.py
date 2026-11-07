"""Fixture git calls ignore the operator's git config (system sd:7972).

A global core.hooksPath or commit.gpgsign made a fixture's `git commit`
fail, hang or run the operator's own hooks. `fixture_config.git_env` is the
one environment every fixture git call uses; these cases run it against a
hostile global config: a hook that exits 1 and a signing program that fails.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import fixture_config


class HostileGitConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        self.hook_ran = self.tmp / "hook-ran"
        for name in ("pre-commit", "commit-msg"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\necho {name} >> '{self.hook_ran}'\nexit 1\n")
            hook.chmod(0o755)
        config = self.tmp / "hostile.gitconfig"
        config.write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n"
                          "[gpg]\n\tprogram = /usr/bin/false\n")
        patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_AUTHOR_NAME": "operator"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_hostile_config_breaks_an_unisolated_commit(self):
        """The control: the same call with the inherited environment fails."""
        repo = self.tmp / "control"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test",
                                 "commit", "-q", "--allow-empty", "-m", "x"],
                                capture_output=True, stdin=subprocess.DEVNULL)
        self.assertNotEqual(result.returncode, 0)
        self.hook_ran.unlink(missing_ok=True)

    def test_git_env_drops_the_operator_config_and_git_variables(self):
        env = fixture_config.git_env()
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertNotIn("GIT_AUTHOR_NAME", env)

    def test_dispatch_pack_builds_its_repository_under_a_hostile_config(self):
        pack = fixture_config.dispatch_pack(self.tmp / "pack", new=False)
        self.assertEqual(len(pack["SD_DISPATCH_PACK_MIN"]), 40)
        self.assertFalse(self.hook_ran.exists(), "the operator's hook ran")


if __name__ == "__main__":
    unittest.main()
