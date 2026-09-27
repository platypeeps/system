"""A home the test owns, and one environment that points every door inside it."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sd_db.testing import (
    FixtureHome,
    FixtureRemote,
    GitHubDouble,
    gh_environment,
    install_gh,
    install_start_command,
    provider_environment,
)


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = FixtureHome(self.root)


class TheDirectories(HomeCase):
    def test_the_places_the_system_reads_exist(self):
        self.assertTrue(self.home.state.is_dir())
        self.assertTrue(self.home.backups.is_dir())
        self.assertTrue(self.home.env_sh.is_file())

    def test_nothing_resolves_outside_the_fixture(self):
        environment = self.home.environment(base={"PATH": "/usr/bin:/bin", "HOME": "/Users/real"})
        for key in ("HOME", "TMPDIR", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
            self.assertTrue(
                environment[key].startswith(str(self.root)), f"{key} = {environment[key]}"
            )

    def test_the_state_directory_is_where_a_child_process_finds_it(self):
        environment = self.home.environment(base={"PATH": "/usr/bin:/bin"})
        out = subprocess.run(
            ["/bin/sh", "-c", 'printf %s "$HOME/.local/share/sd"'],
            capture_output=True, text=True, input="", env=environment,
        ).stdout
        self.assertEqual(out, str(self.home.state))


class TheEnvironment(HomeCase):
    def test_the_stub_directory_is_first_and_the_shell_still_resolves(self):
        environment = self.home.environment(base={"PATH": "/usr/bin:/bin"})
        entries = environment["PATH"].split(os.pathsep)
        self.assertEqual(entries[0], str(self.home.stubs.bin))
        self.assertIn("/bin", entries)

    def test_merging_two_doors_keeps_both_path_prefixes(self):
        remote = FixtureRemote(self.root / "remote")
        with GitHubDouble(remote) as github:
            install_gh(github, self.home.bin)
            provider = install_start_command(self.root / "providers", "claude")
            self.home.merge(
                gh_environment(github, self.home.bin, base={"PATH": ""}),
                provider_environment(self.root / "providers", provider, base={"PATH": ""}),
            )
            environment = self.home.environment(base={"PATH": "/usr/bin:/bin"})

        entries = environment["PATH"].split(os.pathsep)
        self.assertEqual(entries[0], str(self.home.stubs.bin))
        self.assertIn(str(self.home.bin), entries)
        self.assertIn(str(self.root / "providers"), entries)
        self.assertEqual(environment["SD_FIXTURE_SLUG"], "fixture/repo")

    def test_gh_resolves_from_path_alone(self):
        remote = FixtureRemote(self.root / "remote")
        with GitHubDouble(remote) as github:
            install_gh(github, self.home.bin)
            self.home.merge(gh_environment(github, self.home.bin, base={"PATH": ""}))
            environment = self.home.environment(base={"PATH": "/usr/bin:/bin"})
            completed = subprocess.run(
                ["gh", "api", "/repos/fixture/repo"],
                capture_output=True, text=True, input="", env=environment,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("main", completed.stdout)
        self.assertEqual(remote.calls[-1].door, "gh")


if __name__ == "__main__":
    unittest.main()
