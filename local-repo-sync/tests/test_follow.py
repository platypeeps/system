"""Tests for `repo-sync.sh follow` and the hub-pin branch a hub refresh pushes (sd:3100).

After a refresh with no failure, the hub pushes each pinned system and pack
sha to `refs/heads/hub-pin` on that checkout's origin. A satellite, which
`$HOME/.config/sd/hub.json` marks, fetches that branch with `follow`, drains
its own lanes, and moves the checkout to exactly that sha, never to origin's
default branch. Each origin is a local bare repository; the lane root, `sd`
and `sd-db.sh` are the fixtures of `test_refresh_drain.py`.

Each case names its kind, as in `test_repo_sync.py`. A NEW case was written
before `follow` and the push existed and seen to fail against the script
without them; a PIN case keeps behaviour the change had to preserve.
"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_refresh_drain import DrainFixture  # noqa: E402


class FollowFixture(DrainFixture):
    def satellite(self):
        config = self.tmp / "home" / ".config" / "sd"
        config.mkdir(parents=True, exist_ok=True)
        (config / "hub.json").write_text(json.dumps({"hub": "hub.example.test", "port": 8769}))

    def system_repo(self, rel="a/system"):
        repo = self.repo(rel)
        for path in ("local-sd-db/sd_db/schema.py", "local-repo-sync/repo-sync.sh"):
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            self.commit(repo, path, "SCHEMA_VERSION = 24\n" if path.endswith(".py") else "",
                        f"add {path}")
        self.git(repo, "push", "-q", "origin", "main")
        return repo

    def pack_repo(self, rel="a/pack"):
        pack = self.repo(rel)
        (pack / "bin").mkdir()
        self.commit(pack, "bin/sd_install.py", "", "installer")
        self.git(pack, "push", "-q", "origin", "main")
        return pack

    def bare(self, repo):
        return self.git(repo, "remote", "get-url", "origin")

    def set_hub_pin(self, repo, sha):
        """Point origin's hub-pin at `sha`, as the hub's refresh would."""
        self.git(self.bare(repo), "update-ref", "refs/heads/hub-pin", sha)

    def hub_pin(self, repo):
        """origin's hub-pin sha, or "" when it has none."""
        return self.git(self.bare(repo), "rev-parse", "-q", "--verify", "refs/heads/hub-pin", check=False)

    def gate_calls(self):
        if not self.sd_log.exists():
            return []
        return [line for line in self.sd_log.read_text().splitlines() if line.startswith("gate")]


class HubPushTest(unittest.TestCase):
    def fixture(self):
        f = FollowFixture()
        self.addCleanup(f.destroy)
        return f

    def test_help_lists_follow(self):
        """NEW. Every subcommand appears in `help`."""
        f = self.fixture()
        self.assertIn("follow", f.run("help").stdout)

    def test_a_hub_refresh_pushes_hub_pin_in_system_and_pack(self):
        """NEW. After a refresh with no failure, each pinned hub checkout's
        sha is origin's hub-pin; origin's main is not touched."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        mains = []
        for repo in (system, pack):
            f.pin(repo)
            mains.append(f.advance_origin(repo))

        result = f.run("refresh", expect=0)

        self.assertEqual(f.head(system), f.hub_pin(system))
        self.assertEqual(f.head(pack), f.hub_pin(pack))
        self.assertEqual(mains, [f.git(f.bare(r), "rev-parse", "refs/heads/main") for r in (system, pack)])
        self.assertIn("hub-pin : system at", result.stdout)

    def test_hub_pin_may_move_backwards(self):
        """NEW. A hub-pin that is not an ancestor of the new pin is replaced:
        that one ref may move backwards."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        side = f.advance_origin(system, path="side")
        f.git(f.bare(system), "update-ref", "refs/heads/hub-pin", side)
        f.git(f.bare(system), "update-ref", "refs/heads/main", f.head(system))

        f.run("refresh", expect=0)

        self.assertEqual(f.head(system), f.hub_pin(system))
        self.assertNotEqual(side, f.head(system))

    def test_a_failed_push_exits_1_and_says_satellites_keep_the_old_pin(self):
        """NEW. A push the origin refuses fails the refresh by name."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        f.advance_origin(system)
        f.wrap_git("before", "*push*hub-pin*", 'echo "push refused" >&2; exit 1')

        result = f.run("refresh", expect=1)

        self.assertIn("satellites keep the old system pin; run refresh again", result.stdout)
        self.assertEqual("", f.hub_pin(system))

    def test_a_failed_hub_refresh_pushes_nothing(self):
        """PIN. A satellite must not follow a refresh that did not finish."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        f.advance_origin(system)
        (system / "README").write_text("local edit\n")

        f.run("refresh", expect=1)

        self.assertEqual("", f.hub_pin(system))

    def test_follow_on_the_hub_is_a_no_op_that_says_so(self):
        """NEW. The hub moves its checkouts with refresh, never follow."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        before = f.head(system)
        f.set_hub_pin(system, f.advance_origin(system))

        result = f.run("follow", expect=0)

        self.assertIn("this machine is the hub", result.stdout)
        self.assertEqual(before, f.head(system))


class FollowTest(unittest.TestCase):
    def fixture(self):
        f = FollowFixture()
        self.addCleanup(f.destroy)
        f.satellite()
        return f

    def test_follow_moves_to_hub_pin_not_origin_main(self):
        """NEW. The satellite drains first, then moves to the hub's pin and
        never past it to origin/main."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        pinned = f.advance_origin(system)
        later = f.advance_origin(system, path="later2")
        f.set_hub_pin(system, pinned)

        result = f.run("follow", expect=0)

        self.assertEqual(pinned, f.head(system))
        self.assertNotEqual(later, f.head(system))
        self.assertEqual("", f.branch(system))
        self.assertIn("lanes held", result.stdout)
        self.assertTrue(f.gate_calls())

    def test_follow_pins_a_branch_checkout(self):
        """NEW. A satellite's system on `main` is detached by the move, so a
        later hand `git pull` refuses."""
        f = self.fixture()
        system = f.system_repo()
        pinned = f.advance_origin(system)
        f.advance_origin(system, path="later2")
        f.set_hub_pin(system, pinned)

        f.run("follow", expect=0)

        self.assertEqual(pinned, f.head(system))
        self.assertEqual("", f.branch(system))

    def test_follow_runs_make_setup_in_the_pack(self):
        """NEW. The pack installs its commands after the move."""
        f = self.fixture()
        pack = f.pack_repo()
        f.pin(pack)
        pinned = f.advance_origin(pack)
        f.set_hub_pin(pack, pinned)

        f.run("follow", expect=0)

        self.assertEqual(pinned, f.head(pack))
        self.assertEqual([f"-C {pack} setup"], f.make_calls())

    def test_a_failed_fetch_refuses_and_moves_nothing(self):
        """NEW. Every check runs before any move: a system hub-pin it cannot
        fetch leaves the pack unmoved too."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        f.pin(pack)
        before = (f.head(system), f.head(pack))
        f.set_hub_pin(system, f.advance_origin(system))
        f.set_hub_pin(pack, f.advance_origin(pack))
        f.wrap_git("before", "*'/a/system fetch'*hub-pin*", 'echo "fatal: unreachable" >&2; exit 128')

        result = f.run("follow", expect=1)

        self.assertIn(f"cannot fetch hub-pin in {system}", result.stdout)
        self.assertIn("nothing moved", result.stdout)
        self.assertEqual(before, (f.head(system), f.head(pack)))
        self.assertEqual([], f.make_calls())

    def test_a_dirty_tree_refuses_and_moves_nothing(self):
        """NEW. Uncommitted changes in one checkout stop the whole follow,
        before the drain."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        f.pin(pack)
        before = (f.head(system), f.head(pack))
        f.set_hub_pin(system, f.advance_origin(system))
        f.set_hub_pin(pack, f.advance_origin(pack))
        (system / "README").write_text("local edit\n")

        result = f.run("follow", expect=1)

        self.assertIn("uncommitted changes", result.stdout)
        self.assertEqual(before, (f.head(system), f.head(pack)))
        self.assertEqual([], f.gate_calls())

    def test_the_drain_runs_first(self):
        """NEW. A busy lane past the bound refuses before anything moves."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        before = f.head(system)
        f.set_hub_pin(system, f.advance_origin(system))
        proc = f.hold(f.lane("busy-repo"), 30)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))

        result = f.run("follow", expect=1, extra_env={"REPO_SYNC_DRAIN_WAIT": "1"})

        self.assertIn("busy-repo", result.stderr)
        self.assertIn("repo-sync.sh follow: refused, nothing moved", result.stderr)
        self.assertEqual(before, f.head(system))

    def test_follow_is_a_no_op_when_already_there(self):
        """NEW. At the hub's pin, follow drains nothing and runs no make."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        f.pin(pack)
        f.set_hub_pin(system, f.head(system))
        f.set_hub_pin(pack, f.head(pack))

        result = f.run("follow", expect=0)

        self.assertIn("system already at the hub's pin", result.stdout)
        self.assertIn("pack already at the hub's pin", result.stdout)
        self.assertEqual([], f.gate_calls())
        self.assertEqual([], f.make_calls())

    def test_no_hub_pin_branch_is_a_no_op(self):
        """NEW. Until the hub pushes hub-pin, a satellite moves nothing."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        before = f.head(system)
        f.advance_origin(system)

        result = f.run("follow", expect=0)

        self.assertIn("no hub-pin branch yet", result.stdout)
        self.assertEqual(before, f.head(system))
        self.assertEqual([], f.gate_calls())

    def test_follow_does_not_read_the_workflow_database(self):
        """NEW. A satellite's sd_db may be a build the hub refuses until follow
        moves it: follow and its drain read no registry, so it still moves."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        pinned = f.advance_origin(system)
        f.set_hub_pin(system, pinned)

        f.run("follow", expect=0, extra_env={"REPO_LIST_RC": "3"})

        self.assertEqual(pinned, f.head(system))


class SatelliteTest(unittest.TestCase):
    def fixture(self):
        f = FollowFixture()
        self.addCleanup(f.destroy)
        f.satellite()
        return f

    def test_sync_on_a_satellite_never_pulls_system_or_pack(self):
        """NEW. Only follow moves them there, even on a branch."""
        f = self.fixture()
        system = f.system_repo()
        before = f.head(system)
        f.advance_origin(system)

        result = f.run("sync", expect=0)

        self.assertEqual(before, f.head(system))
        self.assertEqual("main", f.branch(system))
        self.assertIn("follow moves it", result.stdout)
        self.assertEqual([], [c for c in f.git_verbs() if c.split()[:1] == ["pull"]])

    def test_nightly_on_a_satellite_never_pulls_pack(self):
        """NEW. nightly syncs too, and leaves a branch checkout of the pack
        where it was while origin moves."""
        f = self.fixture()
        pack = f.pack_repo()
        before = f.head(pack)
        f.advance_origin(pack)

        f.run("nightly", expect=None)

        self.assertEqual(before, f.head(pack))
        self.assertEqual("main", f.branch(pack))

    def test_sync_on_a_satellite_still_pulls_other_repos(self):
        """PIN. Every other checkout syncs as before."""
        f = self.fixture()
        other = f.repo("a/other")
        new = f.advance_origin(other)

        f.run("sync", expect=0)

        self.assertEqual(new, f.head(other))

    def test_refresh_on_a_satellite_refuses_and_names_follow(self):
        """NEW. A satellite never moves to origin/main on its own."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        before = f.head(system)
        f.advance_origin(system)

        result = f.run("refresh", expect=1)

        self.assertIn("only follow moves", result.stderr)
        self.assertEqual(before, f.head(system))
        self.assertEqual("", f.hub_pin(system))


if __name__ == "__main__":
    unittest.main()
