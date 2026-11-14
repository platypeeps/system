"""`run` clears what filled a hub's disk with no job to clean it (sd:8746).

A hub at 97% full held 25.7 GB of exited gate test containers (the docker
prune runs Sundays only), 8.6 GB of Homebrew cache, 5.5 GB of npm cache and
82 GB of uv cache: uv-prune ran only from the login agent, and an always-on
hub never logs in again. Each tool is a stub on PATH that logs its argv.
"""

from tests.test_report import Harness, _stub


class CacheCleanupTest(Harness):
    def setUp(self):
        super().setUp()
        self.calls = self.tmp / "calls"
        self.calls.write_text("")
        self.env["CALLS"] = str(self.calls)

    def logged(self):
        return self.calls.read_text().splitlines()

    def docker(self, exited):
        _stub(self.bin / "docker", 'echo "docker $*" >> "$CALLS"\n'
              f'case "$1" in ps) printf "%s\\n" {exited} ;; esac\n')

    def test_check_lists_the_exited_containers_and_removes_none(self):
        self.docker("example-test-1 example-test-2")
        run = self.maintenance("# nothing to check\n", "check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("- [dry-run] would remove 2 exited docker container(s): "
                      "example-test-1 example-test-2", run.stdout)
        self.assertFalse([c for c in self.logged() if c.startswith("docker rm")])

    def test_run_removes_only_exited_containers_every_night(self):
        self.docker("example-test-1")
        run = self.maintenance("# nothing to check\n", FAKE_WEEKDAY="3")
        self.assertIn("docker ps -a --filter status=exited --format {{.Names}}", self.logged())
        self.assertIn("docker rm example-test-1", self.logged())
        self.assertIn("- removed 1 exited docker container(s): example-test-1", run.stdout)

    def test_no_exited_container_is_no_action(self):
        self.docker("")
        run = self.maintenance("# nothing to check\n")
        self.assertIn("all checks clean", run.stdout)

    def test_run_cleans_the_brew_cache_bounded(self):
        _stub(self.bin / "brew", 'echo "brew $*" >> "$CALLS"\n'
              'echo "==> This operation has freed approximately 8.6GB of disk space."\n')
        run = self.maintenance("# nothing to check\n")
        self.assertIn("brew cleanup -s", self.logged())
        self.assertIn("- brew cleanup -s: freed approximately 8.6GB", run.stdout)
        self.assertIn("[step ", run.stderr)
        self.assertNotIn("brew cleanup -s", self.maintenance("# x\n", "check").stdout.replace(
            "[dry-run] would run brew cleanup -s", ""))

    def test_run_cleans_the_npm_cache_unless_npm_runs(self):
        cache = self.tmp / "npm-cache"
        (cache / "_cacache").mkdir(parents=True)
        (cache / "_cacache" / "blob").write_bytes(b"\0" * 2 * 1024 * 1024)
        _stub(self.bin / "npm", 'echo "npm $*" >> "$CALLS"\n'
              f'case "$1 $2" in "config get") echo "{cache}" ;;\n'
              f'  "cache clean") rm -rf "{cache}/_cacache" ;; esac\n')
        _stub(self.bin / "pgrep", "exit 1\n")
        run = self.maintenance("# nothing to check\n")
        self.assertIn("npm cache clean --force", self.logged())
        self.assertIn("- npm cache: 2 MB -> 0 MB", run.stdout)
        (cache / "_cacache").mkdir()
        (cache / "_cacache" / "blob").write_bytes(b"\0" * 1024)
        self.calls.write_text("")
        _stub(self.bin / "pgrep", "exit 0\n")
        run = self.maintenance("# nothing to check\n")
        self.assertNotIn("npm cache clean --force", self.logged())
        self.assertIn("npm cache clean skipped: npm runs", run.stdout)

    def uv(self, foreign):
        cache = self.tmp / "uv-cache"
        cache.mkdir(exist_ok=True)
        _stub(self.bin / "uv", 'echo "uv $*" >> "$CALLS"\n'
              f'case "$1 $2" in "cache dir") echo "{cache}" ;; esac\n')
        # Not loaded: no agent is paused, so the real launchd is never asked.
        _stub(self.bin / "launchctl", 'echo "launchctl $*" >> "$CALLS"\nexit 1\n')
        _stub(self.bin / "pgrep", "exit 0\n" if foreign else "exit 1\n")

    def test_run_prunes_the_uv_cache(self):
        self.uv(foreign=False)
        run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertIn("uv cache prune", self.logged())
        self.assertRegex(run.stdout, r"- uv-prune: .*uv-cache 0 MB -> 0 MB \(prune exit 0\)")

    def test_a_prune_skipped_three_nights_running_is_a_finding(self):
        self.uv(foreign=True)
        for night in (1, 2):
            run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
            self.assertIn("- uv-prune: another uv process holds the cache; skipped", run.stdout)
            self.assertEqual(self.attention(run), [], night)
        run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertNotIn("uv cache prune", self.logged())
        self.assertEqual(self.attention(run), [
            "SD_REPORT_ATTENTION: maintenance on fixturehost: uv cache prune skipped "
            "3 or more nights running: another uv process holds the cache"])
        # A prune that runs ends the streak.
        self.uv(foreign=False)
        self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.uv(foreign=True)
        run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertEqual(self.attention(run), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
