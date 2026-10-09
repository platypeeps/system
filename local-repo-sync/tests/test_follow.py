"""Tests for `repo-sync.sh follow` and the hub-pin tag a hub refresh pushes (sd:3100).

After a refresh with no failure, the hub pushes one annotated tag, `hub-pin`,
to the system origin: it names the pinned system sha, and its message carries
`pack=<sha>`. A satellite, which `$HOME/.config/sd/hub.json` marks, fetches
that tag with `follow`, fetches the pack sha from the pack origin, drains its
own lanes, and moves both checkouts to exactly that pair, never to origin's
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


# For the make stub: record the HEAD each `make -C <dir> setup` ran at, and
# fail the first call only, as a pack setup that breaks at the hub's pin.
FAIL_FIRST_SETUP = ('git -C "$2" rev-parse HEAD >> "$MAKE_LOG.heads"; '
                    'if [ -e "$MAKE_LOG.failed" ]; then MAKE_RC=0; '
                    'else : > "$MAKE_LOG.failed"; MAKE_RC=1; fi')


# The --serve help line of a pack that serves its own HEAD (pack sd:3111).
SERVES_HEAD = ("#  --serve          in the serving tree, render it; elsewhere, clone the tree if it is\n"
               "#                   missing, detach it at this checkout's HEAD and run its\n")


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

    def run(self, *args, expect=0, extra_env=None):
        # The hub's refresh makes an annotated tag, which needs a committer.
        # The data home holds the serving tree follow asks --verify of; it is
        # the fixture's, never the machine's.
        env = {"GIT_COMMITTER_NAME": "Hub", "GIT_COMMITTER_EMAIL": "hub@example.test",
               "XDG_DATA_HOME": str(self.tmp / "data"), **(extra_env or {})}
        return super().run(*args, expect=expect, extra_env=env)

    def set_hub_pin(self, system, sha, pack=None, body=None):
        """The hub-pin tag on system's origin, as the hub's refresh makes it:
        naming `sha`, with `pack=<pack>` in its message unless `body` is given."""
        message = body if body is not None else f"hub-pin\n\npack={pack}"
        self.git(self.bare(system), "tag", "-f", "-a", "-m", message, "hub-pin", sha)

    def hub_pin(self, system):
        """(system sha, pack sha) from the hub-pin tag on system's origin, or None."""
        bare = self.bare(system)
        if not self.git(bare, "rev-parse", "-q", "--verify", "refs/tags/hub-pin", check=False):
            return None
        body = self.git(bare, "cat-file", "tag", "hub-pin")
        packs = [line[len("pack="):] for line in body.splitlines() if line.startswith("pack=")]
        return self.git(bare, "rev-parse", "hub-pin^{commit}"), (packs[0] if packs else None)

    def refs(self, repo, pattern="*hub-pin*"):
        return self.git(self.bare(repo), "for-each-ref", "--format=%(refname)", f"refs/**/{pattern}")

    def serving_verify(self, code):
        """A serving tree whose installer's --verify --json reports `code`
        for the source check, as the pack's sd_install.py does."""
        installer = self.tmp / "data" / "sd-ai-command-pack" / "serving" / "bin" / "sd_install.py"
        installer.parent.mkdir(parents=True, exist_ok=True)
        check = {"component": "source", "status": "passed" if code == "ok" else "failed", "code": code}
        installer.write_text("import json, sys\n"
                             "assert sys.argv[1:] == ['--verify', '--json'], sys.argv\n"
                             f"print(json.dumps({{'checks': [{check!r}], 'status': 'failed'}}))\n")

    def serving_clone(self, pack, rev):
        """The serving tree, a clone of pack's origin detached at `rev`."""
        tree = self.tmp / "data" / "sd-ai-command-pack" / "serving"
        tree.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.tmp, "clone", "-q", self.bare(pack), str(tree))
        self.git(tree, "checkout", "-q", "--detach", rev)
        return tree

    def setup_heads(self):
        """The pack HEAD at each make setup FAIL_FIRST_SETUP saw."""
        heads = pathlib.Path(f"{self.make_log}.heads")
        return heads.read_text().splitlines() if heads.exists() else []

    def gate_calls(self):
        if not self.sd_log.exists():
            return []
        return [line for line in self.sd_log.read_text().splitlines() if line.startswith("gate")]


class HubPushTest(unittest.TestCase):
    def fixture(self):
        f = FollowFixture()
        self.addCleanup(f.destroy)
        return f

    def pinned_pair(self, f):
        system, pack = f.system_repo(), f.pack_repo()
        for repo in (system, pack):
            f.pin(repo)
            f.advance_origin(repo)
        return system, pack

    def test_help_lists_follow(self):
        """NEW. Every subcommand appears in `help`."""
        f = self.fixture()
        self.assertIn("follow", f.run("help").stdout)

    def test_a_hub_refresh_pushes_one_tag_naming_both_pins(self):
        """NEW (round 1). One push publishes both: the hub-pin tag on the
        system origin names the system sha and carries pack=<sha>. The pack
        origin gets no hub-pin ref, and neither main is touched."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        mains = []
        for repo in (system, pack):
            f.pin(repo)
            mains.append(f.advance_origin(repo))

        result = f.run("refresh", expect=0)

        self.assertEqual((f.head(system), f.head(pack)), f.hub_pin(system))
        self.assertEqual("", f.refs(pack))
        self.assertEqual("refs/tags/hub-pin", f.refs(system))
        self.assertEqual(mains, [f.git(f.bare(r), "rev-parse", "refs/heads/main") for r in (system, pack)])
        self.assertIn("hub-pin : system", result.stdout)

    def test_hub_pin_may_move_backwards(self):
        """NEW. A hub-pin tag on a commit the new pin does not descend from is
        replaced: that one tag may move backwards."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        f.pin(pack)
        side = f.advance_origin(system, path="side")
        f.set_hub_pin(system, side, pack=f.head(pack))
        f.git(f.bare(system), "update-ref", "refs/heads/main", f.head(system))

        f.run("refresh", expect=0)

        self.assertEqual((f.head(system), f.head(pack)), f.hub_pin(system))

    def test_a_failed_push_exits_1_and_says_satellites_keep_the_old_pins(self):
        """NEW. A push the origin refuses fails the refresh by name."""
        f = self.fixture()
        system, _ = self.pinned_pair(f)
        f.wrap_git("before", "*push*hub-pin*", 'echo "push refused" >&2; exit 1')

        result = f.run("refresh", expect=1)

        self.assertIn("satellites keep the old pins; run refresh again", result.stdout)
        self.assertIsNone(f.hub_pin(system))

    def test_a_pinned_system_without_a_pinned_pack_publishes_nothing(self):
        """NEW (round 1). A pair the hub cannot name is not published: the
        refresh exits 1 and the old tag stays."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        f.advance_origin(system)

        result = f.run("refresh", expect=1)

        self.assertIn("hub-pin not published", result.stdout)
        self.assertIsNone(f.hub_pin(system))
        self.assertEqual("main", f.branch(pack))

    def test_a_failed_hub_refresh_pushes_nothing(self):
        """PIN. A satellite must not follow a refresh that did not finish."""
        f = self.fixture()
        system, _ = self.pinned_pair(f)
        (system / "README").write_text("local edit\n")

        f.run("refresh", expect=1)

        self.assertIsNone(f.hub_pin(system))

    def test_follow_on_the_hub_is_a_no_op_that_says_so(self):
        """NEW. The hub moves its checkouts with refresh, never follow."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        before = f.head(system)
        f.set_hub_pin(system, f.advance_origin(system), pack=f.head(pack))

        result = f.run("follow", expect=0)

        self.assertIn("this machine is the hub", result.stdout)
        self.assertEqual(before, f.head(system))


class FollowTest(unittest.TestCase):
    def fixture(self):
        f = FollowFixture()
        self.addCleanup(f.destroy)
        f.satellite()
        return f

    def pair(self, f, pin=True):
        """A system and a pack checkout, each with a newer commit on origin;
        returns them and their old heads."""
        system, pack = f.system_repo(), f.pack_repo()
        if pin:
            f.pin(system)
            f.pin(pack)
        return system, pack, (f.head(system), f.head(pack))

    def test_follow_moves_both_to_the_tag_not_origin_main(self):
        """NEW. The satellite drains first, then moves system and pack to the
        tag's pair and never past it to origin/main."""
        f = self.fixture()
        system, pack, _ = self.pair(f)
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        later = f.advance_origin(system, path="later2")
        f.advance_origin(pack, path="later2")
        f.set_hub_pin(system, pinned[0], pack=pinned[1])

        result = f.run("follow", expect=0)

        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertNotEqual(later, f.head(system))
        self.assertEqual(("", ""), (f.branch(system), f.branch(pack)))
        self.assertIn("lanes held", result.stdout)
        self.assertTrue(f.gate_calls())
        self.assertEqual([f"-C {pack} setup"], f.make_calls())

    def test_follow_pins_a_branch_checkout(self):
        """NEW. A satellite's system on `main` is detached by the move, so a
        later hand `git pull` refuses."""
        f = self.fixture()
        system, pack, old = self.pair(f, pin=False)
        pinned = f.advance_origin(system)
        f.advance_origin(system, path="later2")
        f.set_hub_pin(system, pinned, pack=old[1])

        f.run("follow", expect=0)

        self.assertEqual(pinned, f.head(system))
        self.assertEqual("", f.branch(system))

    def test_a_failed_tag_fetch_refuses_and_moves_nothing(self):
        """NEW. Without the tag nothing is known: refuse, nothing moved."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, f.advance_origin(system), pack=f.advance_origin(pack))
        f.wrap_git("before", "*'/a/system fetch'*hub-pin*", 'echo "fatal: unreachable" >&2; exit 128')

        result = f.run("follow", expect=1)

        self.assertIn(f"cannot fetch the hub-pin tag in {system}", result.stdout)
        self.assertIn("nothing moved", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))

    def test_a_pack_sha_it_cannot_fetch_refuses_and_moves_nothing(self):
        """NEW (round 1). The pack sha is fetched before any move: one the
        pack origin does not have leaves system unmoved too."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, f.advance_origin(system), pack="f" * 40)

        result = f.run("follow", expect=1)

        self.assertIn(f"cannot fetch the hub's pack pin {'f' * 40}", result.stdout)
        self.assertIn("nothing moved", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))
        self.assertEqual([], f.gate_calls())

    def test_a_tag_without_a_pack_line_refuses_and_moves_nothing(self):
        """NEW (round 1). A tag that names system alone is half a pair."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, f.advance_origin(system), body="hub-pin")

        result = f.run("follow", expect=1)

        self.assertIn("carries no pack=<sha> line", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))

    def test_a_failed_pack_move_puts_system_back_and_the_next_run_recovers(self):
        """NEW (round 1). System moves first; when the pack's make setup then
        fails, both go back to the old pair, and the next run moves both."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        f.set_hub_pin(system, pinned[0], pack=pinned[1])

        result = f.run("follow", expect=1, extra_env={"MAKE_HOOK": FAIL_FIRST_SETUP})

        self.assertIn("back at its old sha, so a migrate note above does not apply; the next run retries",
                      result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))

        f.run("follow", expect=0)

        self.assertEqual(pinned, (f.head(system), f.head(pack)))

    def test_the_rollback_reinstalls_the_pack_commands_at_the_old_sha(self):
        """NEW (round 2). A make setup that failed part way may have installed
        commands from the hub's pin: the rollback runs make setup again once
        the pack is back, so the commands match its HEAD."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        f.set_hub_pin(system, pinned[0], pack=pinned[1])

        result = f.run("follow", expect=1, extra_env={"MAKE_HOOK": FAIL_FIRST_SETUP})

        self.assertEqual([pinned[1], old[1]], f.setup_heads())
        self.assertNotIn("by hand", result.stdout)

    def test_a_failed_reinstall_prints_the_command_to_run_by_hand(self):
        """NEW (round 2). When make setup fails at the old sha too, follow
        names the one command that puts the commands back."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, f.advance_origin(system), pack=f.advance_origin(pack))

        result = f.run("follow", expect=1, extra_env={"MAKE_RC": "1"})

        self.assertIn(f"make setup at the old sha; the pack's commands may be from the hub's pin; "
                      f"by hand: make -C '{pack}' setup", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))
        self.assertEqual(2, len(f.make_calls()))

    def test_the_rollback_keeps_a_checkout_path_with_a_space(self):
        """NEW (round 2). The rollback list holds whole paths: under a root
        with a space, both checkouts still go back to the old pair."""
        f = self.fixture()
        f.root = f.tmp / "checkout root"
        f.root.mkdir()
        system, pack, old = self.pair(f)
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        f.set_hub_pin(system, pinned[0], pack=pinned[1])

        result = f.run("follow", expect=1, extra_env={"MAKE_HOOK": FAIL_FIRST_SETUP})

        self.assertIn(f"put {system} back", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))
        self.assertEqual([pinned[1], old[1]], f.setup_heads())

        f.run("follow", expect=0)

        self.assertEqual(pinned, (f.head(system), f.head(pack)))

    def test_a_setup_cut_during_the_venv_build_runs_again_at_the_pin(self):
        """NEW (round 3, kill point 5). A follow killed while the pack's make
        setup built the venv leaves HEAD at the pin and .venv/sd-provisioning
        behind: the next run drains and runs make setup again, and once the
        marker is gone it is a no-op."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        (pack / ".venv").mkdir()
        (pack / ".venv" / "sd-provisioning").write_text("make setup is building this environment\n")

        result = f.run("follow", expect=0, extra_env={"MAKE_HOOK": 'rm -f "$2/.venv/sd-provisioning"'})

        self.assertIn("its last make setup did not finish", result.stdout)
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertTrue(f.gate_calls())
        self.assertEqual(old, (f.head(system), f.head(pack)))

        f.run("follow", expect=0)

        self.assertEqual(1, len(f.make_calls()))

    def test_a_setup_cut_during_the_serving_step_runs_again_at_the_pin(self):
        """NEW (round 3, kill point 6). A follow killed while --serve moved the
        serving tree leaves its receipt behind it: --verify says
        source_commit_changed, and the next run drains and sets up again."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        f.serving_verify("source_commit_changed")

        result = f.run("follow", expect=0)

        self.assertIn("sd_install.py --verify reports source_commit_changed", result.stdout)
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertTrue(f.gate_calls())

    def test_a_setup_cut_before_the_serving_tree_moved_runs_again_at_the_pin(self):
        """NEW (round 5, kill point 7). A pack that serves its own HEAD leaves
        the serving tree there after a completed setup: a tree elsewhere means
        a follow killed after the venv build, and the next run sets up again
        until the tree is at the pack's HEAD."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = f.commit(pack, "bin/sd_install.py", SERVES_HEAD, "serve the checkout's HEAD")
        f.git(pack, "push", "-q", "origin", "HEAD:main")
        f.set_hub_pin(system, old[0], pack=pinned)
        tree = f.serving_clone(pack, old[1])
        serve = f'git -C "{tree}" fetch -q "$2" HEAD && git -C "{tree}" checkout -q --detach FETCH_HEAD'

        result = f.run("follow", expect=0, extra_env={"MAKE_HOOK": serve})

        self.assertIn("the serving tree is not at the pack's HEAD", result.stdout)
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertTrue(f.gate_calls())
        self.assertEqual(pinned, f.head(tree))

        f.run("follow", expect=0)

        self.assertEqual(1, len(f.make_calls()))

    def test_a_pack_that_serves_origin_main_is_not_set_up_on_every_run(self):
        """PIN (round 5). A pack from before sd:3111 serves origin/main, so its
        serving tree is rarely at the pack's HEAD: follow does not compare
        them there, and two runs set up nothing."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        f.serving_clone(pack, f.git(pack, "rev-parse", "HEAD~1"))

        for _ in range(2):
            result = f.run("follow", expect=0)
            self.assertIn("pack already at the hub's pin", result.stdout)
        self.assertEqual([], f.make_calls())
        self.assertEqual([], f.gate_calls())

    def test_a_verified_serving_tree_at_the_pin_is_a_no_op(self):
        """PIN (round 3). A --verify that names no source mismatch, even a
        failed one, does not set up again."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        f.serving_verify("receipt_malformed")

        result = f.run("follow", expect=0)

        self.assertIn("pack already at the hub's pin", result.stdout)
        self.assertEqual([], f.make_calls())

    def test_a_failed_setup_at_the_pin_exits_1_and_names_the_command(self):
        """NEW (round 3). make setup at the pin that fails again exits 1 and
        names the command; the marker it leaves makes the next run retry."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        (pack / ".venv").mkdir()
        (pack / ".venv" / "sd-provisioning").write_text("building\n")

        result = f.run("follow", expect=1, extra_env={"MAKE_RC": "1"})

        self.assertIn(f"by hand: make -C '{pack}' setup", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))

    def test_system_at_the_pin_and_pack_behind_moves_the_pack(self):
        """PIN (round 3, kill point 4). A follow killed between the two moves
        leaves system at the pin: the next run moves the pack alone."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = f.advance_origin(pack)
        f.set_hub_pin(system, old[0], pack=pinned)

        result = f.run("follow", expect=0)

        self.assertIn("system already at the hub's pin", result.stdout)
        self.assertEqual((old[0], pinned), (f.head(system), f.head(pack)))
        self.assertEqual([f"-C {pack} setup"], f.make_calls())

    def test_a_dirty_tree_refuses_and_moves_nothing(self):
        """NEW. Uncommitted changes in one checkout stop the whole follow,
        before the drain."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, f.advance_origin(system), pack=f.advance_origin(pack))
        (pack / "README").write_text("local edit\n")

        result = f.run("follow", expect=1)

        self.assertIn("uncommitted changes", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))
        self.assertEqual([], f.gate_calls())

    def test_the_drain_runs_first(self):
        """NEW. A busy lane past the bound refuses before anything moves."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, f.advance_origin(system), pack=old[1])
        proc = f.hold(f.lane("busy-repo"), 30)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))

        result = f.run("follow", expect=1, extra_env={"REPO_SYNC_DRAIN_WAIT": "1"})

        self.assertIn("busy-repo", result.stderr)
        self.assertIn("repo-sync.sh follow: refused, nothing moved", result.stderr)
        self.assertEqual(old, (f.head(system), f.head(pack)))

    def test_follow_is_a_no_op_when_already_there(self):
        """NEW. At the hub's pair, follow drains nothing and runs no make."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])

        result = f.run("follow", expect=0)

        self.assertIn("system already at the hub's pin", result.stdout)
        self.assertIn("pack already at the hub's pin", result.stdout)
        self.assertEqual([], f.gate_calls())
        self.assertEqual([], f.make_calls())

    def test_no_hub_pin_tag_is_a_no_op(self):
        """NEW. Until the hub pushes hub-pin, a satellite moves nothing."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.advance_origin(system)

        result = f.run("follow", expect=0)

        self.assertIn("no hub-pin tag yet", result.stdout)
        self.assertEqual(old, (f.head(system), f.head(pack)))
        self.assertEqual([], f.gate_calls())

    def test_follow_does_not_read_the_workflow_database(self):
        """NEW. A satellite's sd_db may be a build the hub refuses until follow
        moves it: follow and its drain read no registry, so it still moves."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = f.advance_origin(system)
        f.set_hub_pin(system, pinned, pack=old[1])

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
        self.assertIsNone(f.hub_pin(system))


if __name__ == "__main__":
    unittest.main()
