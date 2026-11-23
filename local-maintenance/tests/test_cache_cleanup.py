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

    def npm(self):
        cache = self.tmp / "npm-cache"
        _stub(self.bin / "npm", 'echo "npm $*" >> "$CALLS"\n'
              f'case "$1 $2" in "config get") echo "{cache}" ;;\n'
              f'  "cache clean") rm -rf "{cache}/_cacache" ;; esac\n')
        return cache

    def run_with_processes(self, *commands):
        """A run while `ps` lists `commands`, as npm's process.title shows them."""
        cache = self.npm()
        (cache / "_cacache").mkdir(parents=True, exist_ok=True)
        (cache / "_cacache" / "blob").write_bytes(b"\0" * 2 * 1024 * 1024)
        listing = "".join(f"echo '{c}'\n" for c in commands)
        _stub(self.bin / "ps", listing)
        self.calls.write_text("")
        return self.maintenance("# nothing to check\n")

    def test_run_cleans_the_npm_cache_when_no_npm_runs(self):
        run = self.run_with_processes(
            "/sbin/launchd",
            "node /srv/npm-example/server.js",
            "vim /home/operator/.npmrc",
            "grep npm install.log",
            "node /opt/example/lib/node_modules/npm-check/bin/cli.js")
        self.assertIn("npm cache clean --force", self.logged())
        self.assertIn("- npm cache: 2 MB -> 0 MB", run.stdout)

    def test_a_running_npm_keeps_the_cache(self):
        # npm sets process.title, so a running install shows as "npm
        # install", not as node running npm-cli.js (sd:8746 review).
        for title in ("npm install", "npm exec --yes example-tool", "npm ci",
                      "/opt/example/bin/npm run build", "npx example-tool",
                      "node /opt/example/lib/node_modules/npm/bin/npm-cli.js i",
                      "node /opt/example/lib/node_modules/npm/bin/npx-cli.js x"):
            with self.subTest(title=title):
                run = self.run_with_processes("/sbin/launchd", title)
                self.assertNotIn("npm cache clean --force", self.logged())
                self.assertIn("npm cache clean skipped: npm runs", run.stdout)

    def test_an_unreadable_process_list_keeps_the_cache(self):
        self.npm()
        cache = self.tmp / "npm-cache"
        (cache / "_cacache").mkdir(parents=True)
        _stub(self.bin / "ps", "exit 1\n")
        run = self.maintenance("# nothing to check\n")
        self.assertNotIn("npm cache clean --force", self.logged())
        self.assertIn("npm cache clean skipped: could not list processes", run.stdout)

    def uv(self, foreign, prune_rc=0):
        cache = self.tmp / "uv-cache"
        cache.mkdir(exist_ok=True)
        _stub(self.bin / "uv", 'echo "uv $*" >> "$CALLS"\n'
              f'case "$1 $2" in "cache dir") echo "{cache}" ;;\n'
              f'  "cache prune") exit {prune_rc} ;; esac\n')
        # Not loaded: no agent is paused, so the real launchd is never asked.
        _stub(self.bin / "launchctl", 'echo "launchctl $*" >> "$CALLS"\nexit 1\n')
        # `lsof -t <cache>/.lock`: the pids that hold this cache's lock.
        _stub(self.bin / "lsof", 'printf "%s\\n" 101 102 3\n' if foreign else "exit 1\n")
        # The real process list may hold a uvx: list none for uv-prune's
        # holder scan. A test that wants holders stubs ps again.
        _stub(self.bin / "ps", 'case "$*" in "-A -o pid= -o comm=") exit 0 ;; '
              'esac\nexec /bin/ps "$@"\n')

    def test_run_prunes_the_uv_cache(self):
        self.uv(foreign=False)
        run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertIn("uv cache prune", self.logged())
        self.assertRegex(run.stdout, r"- uv-prune: .*uv-cache 0 MB -> 0 MB \(prune exit 0\)")

    def test_a_failed_prune_is_a_finding_and_keeps_the_skips(self):
        # sd:8809: the size line printed beside a failed prune read as
        # success and ended the skip streak.
        self.uv(foreign=True)
        self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        skips = self.tmp / "home/.local/state/maintenance/uv-prune-skips"
        self.assertEqual("1", skips.read_text().strip())
        self.uv(foreign=False, prune_rc=2)
        run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertIn("uv cache prune", self.logged())
        self.assertEqual(self.attention(run), [
            "SD_REPORT_ATTENTION: maintenance on fixturehost: "
            "uv cache prune failed"])
        self.assertIn("- uv cache prune failed (exit 2)", run.stdout)
        self.assertEqual("1", skips.read_text().strip())

    def test_a_skip_names_its_holders_and_keeps_its_alert_open(self):
        # sd:9377: the finding fired every night on a skip the operator
        # accepted, and named no holder.
        self.uv(foreign=True)
        # `ps -o pid= -o comm=` names the executable only, never the
        # arguments; a stub that sees `command=` prints a secret argument.
        _stub(self.bin / "ps", 'case "$*" in *command*) echo "4 uv run --token s3cret" ;; '
              '*) printf "%s\\n" "  101 uvx" "  102 uvx" "  3 /opt/example/bin/uv" "  4 /bin/zsh" ;; esac\n')
        holders = "(uvx pid 101 102; uv pid 3)"
        for night in (1, 2):
            run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
            self.assertIn(f"holds the cache; skipped {holders}", run.stdout)
        run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertIn(f"- uv cache prune skipped 3 nights running: another uv process holds the cache {holders}",
                      run.stdout)
        # Bundle review: night 4 reported healthy and closed night 3's
        # alert. Each later night raises the same reason line, so the
        # pack folds it into the open report.
        for night in (4, 5):
            run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
            self.assertEqual(self.attention(run), [
                "SD_REPORT_ATTENTION: maintenance on fixturehost: uv cache prune skipped "
                "3 or more nights running: another uv process holds the cache"], night)
            self.assertIn(f"- uv cache prune skipped {night} nights running: another uv process holds the cache "
                          f"{holders}", run.stdout)
            self.assertNotIn("s3cret", run.stdout)

    def test_long_lived_uvx_runs_skip_quietly(self):
        # sd:9998: uvx MCP proxies in open sessions hold the cache lock
        # for days; that skip is expected, so it never raises attention
        # and never counts toward the streak.
        self.uv(foreign=True)
        _stub(self.bin / "ps", 'printf "%s\\n" "  101 uvx" "  102 uvx" "  4 /bin/zsh"\n')
        skips = self.tmp / "home/.local/state/maintenance/uv-prune-skips"
        for night in (1, 2, 3, 4):
            run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
            self.assertEqual(self.attention(run), [], night)
            self.assertIn("- uv-prune: only uvx tool runs hold the cache; "
                          "skipped (uvx pid 101 102)", run.stdout)
        self.assertNotIn("uv cache prune", self.logged())
        self.assertFalse(skips.exists())

    def test_a_third_night_that_dies_before_its_report_alerts_next_night(self):
        # Lead re-review: the count was written before the report, so a
        # run that died between them made night 4 an action, never alerted.
        self.uv(foreign=True)
        for _ in (1, 2):
            self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        # `hostname` runs only for the attention lines: here it kills the
        # run there, as a crash would. It kills only maintenance.sh shells.
        _stub(self.bin / "hostname",
              'for p in $PPID $(ps -o ppid= -p $PPID); do\n'
              '  ps -o command= -p "$p" | grep -q maintenance.sh && kill -KILL "$p"\n'
              'done\nexit 1\n')
        died = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertEqual((died.returncode, self.attention(died)), (-9, []))
        _stub(self.bin / "hostname", 'echo "fixturehost"\n')
        run = self.maintenance("# nothing to check\n", UV_PRUNE_WAIT="0")
        self.assertEqual(self.attention(run), [
            "SD_REPORT_ATTENTION: maintenance on fixturehost: uv cache prune skipped "
            "3 or more nights running: another uv process holds the cache"])

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
