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
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_refresh_drain import DrainFixture  # noqa: E402


# For the make stub: record the HEAD each `make -C <dir> setup` ran at, and
# fail the first call only, as a pack setup that breaks at the hub's pin.
FAIL_FIRST_SETUP = ('git -C "$2" rev-parse HEAD >> "$MAKE_LOG.heads"; '
                    'if [ -e "$MAKE_LOG.failed" ]; then MAKE_RC=0; '
                    'else : > "$MAKE_LOG.failed"; MAKE_RC=1; fi')


# For machine-setup.sh in the fixture's system checkout (sd:3168): log the
# arguments of each run, run UPDATE_HOOK, exit UPDATE_RC. A run of every
# stage, or of cron or agents, reinstalls the follow job's own LaunchAgent:
# launchd boots the job out, so the stub ends as a TERMed process does. A
# dry run ends with the line the real stage prints once it has checked the
# end state (PROOF); a hook that exits first leaves it out.
PROOF = {"bin": "  PATH         on PATH", "satellite": "  ok      sd_db 0.1 build 0123abcd matches the hub's"}
MACHINE_SETUP_STUB = ('[ -z "$UPDATE_LOG" ] || printf \'%s\\n\' "$*" >> "$UPDATE_LOG"\n'
                      'case "$2" in --apply|cron|agents) echo bootout >> "$UPDATE_LOG"; exit 143 ;; esac\n'
                      '[ -z "$UPDATE_HOOK" ] || eval "$UPDATE_HOOK"\n'
                      'if [ "$3" != --apply ]; then case "$2" in\n'
                      f'  bin) echo "{PROOF["bin"]}" ;;\n'
                      f'  satellite) echo "{PROOF["satellite"]}" ;;\n'
                      'esac; fi\n'
                      'exit "${UPDATE_RC:-0}"\n')


class FollowFixture(DrainFixture):
    def satellite(self):
        config = self.tmp / "home" / ".config" / "sd"
        config.mkdir(parents=True, exist_ok=True)
        (config / "hub.json").write_text(json.dumps({"hub": "hub.example.test", "port": 8769}))

    def system_repo(self, rel="a/system"):
        repo = self.repo(rel)
        bodies = {"local-sd-db/sd_db/schema.py": "SCHEMA_VERSION = 24\n",
                  "local-repo-sync/repo-sync.sh": "",
                  "local-machine-setup/machine-setup.sh": MACHINE_SETUP_STUB}
        for path, body in bodies.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            self.commit(repo, path, body, f"add {path}")
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
        # The state home holds follow's intent marker; it is the fixture's,
        # never the machine's.
        env = {"GIT_COMMITTER_NAME": "Hub", "GIT_COMMITTER_EMAIL": "hub@example.test",
               "XDG_STATE_HOME": str(self.tmp / "state"), "UPDATE_LOG": str(self.update_log),
               **(extra_env or {})}
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

    @property
    def intent(self):
        return self.tmp / "state" / "repo-sync" / "follow-intent"

    def leave_intent(self, system_sha, pack_sha):
        """The marker a follow killed after it wrote it leaves behind."""
        self.intent.parent.mkdir(parents=True, exist_ok=True)
        self.intent.write_text(f"system={system_sha}\npack={pack_sha}\n")

    def setup_heads(self):
        """The pack HEAD at each make setup FAIL_FIRST_SETUP saw."""
        heads = pathlib.Path(f"{self.make_log}.heads")
        return heads.read_text().splitlines() if heads.exists() else []

    @property
    def update_log(self):
        return self.tmp / "update.log"

    def update_calls(self):
        """The arguments of each machine-setup.sh run the stub logged."""
        return self.update_log.read_text().splitlines() if self.update_log.exists() else []

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
        # Only the update's proof deletes the marker (sd:3168 class pass).
        self.assertTrue(f.intent.exists())

        f.run("follow", expect=0)

        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertFalse(f.intent.exists())

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

    def test_the_intent_marker_holds_the_pair_while_it_moves(self):
        """NEW (round 6). The marker names the target pair before any move,
        is there while make setup runs, and is gone once setup succeeded."""
        f = self.fixture()
        system, pack, _ = self.pair(f)
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        f.set_hub_pin(system, pinned[0], pack=pinned[1])
        seen = f.tmp / "seen"

        f.run("follow", expect=0, extra_env={"MAKE_HOOK": f'cat "$XDG_STATE_HOME/repo-sync/follow-intent" > "{seen}"'})

        self.assertEqual(f"system={pinned[0]}\npack={pinned[1]}\n", seen.read_text())
        self.assertFalse(f.intent.exists())

    def test_kill_in_a_fetch_keeps_the_marker_for_the_next_run(self):
        """NEW (round 6, kill point 1). A run that cannot fetch the tag
        refuses and leaves an earlier marker; the next run sets up again."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        f.leave_intent(*old)
        f.wrap_git("before", "*'/a/system fetch'*hub-pin*", 'echo "fatal: unreachable" >&2; exit 128')

        f.run("follow", expect=1)

        self.assertTrue(f.intent.exists())
        f.wrap_git("before", "*no-such-call*", ":")
        f.run("follow", expect=0)
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertFalse(f.intent.exists())

    def test_kill_in_the_drain_keeps_the_marker_for_the_next_run(self):
        """NEW (round 6, kill point 2). A drain that refuses moves nothing and
        leaves the marker; the next run drains, sets up and deletes it."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        f.leave_intent(*old)
        proc = f.hold(f.lane("busy-repo"), 30)

        f.run("follow", expect=1, extra_env={"REPO_SYNC_DRAIN_WAIT": "1"})

        proc.kill(), proc.wait(), proc.stdout.close()
        self.assertTrue(f.intent.exists())
        self.assertEqual([], f.make_calls())
        f.run("follow", expect=0)
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertFalse(f.intent.exists())

    def test_kill_before_the_system_move_finishes_on_the_next_run(self):
        """NEW (round 6, kill point 3). Marker left, both checkouts still at
        the old pair: the next run moves both and deletes the marker."""
        f = self.fixture()
        system, pack, _ = self.pair(f)
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        f.set_hub_pin(system, pinned[0], pack=pinned[1])
        f.leave_intent(*pinned)

        f.run("follow", expect=0)

        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertFalse(f.intent.exists())

    def test_kill_between_the_moves_finishes_on_the_next_run(self):
        """NEW (round 6, kill point 4). Marker left, system at the pin, pack
        behind: the next run moves the pack alone and deletes the marker."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = f.advance_origin(pack)
        f.set_hub_pin(system, old[0], pack=pinned)
        f.leave_intent(old[0], pinned)

        result = f.run("follow", expect=0)

        self.assertIn("system already at the hub's pin", result.stdout)
        self.assertEqual((old[0], pinned), (f.head(system), f.head(pack)))
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertFalse(f.intent.exists())

    def test_kill_in_the_pack_setup_sets_up_again_at_the_pin(self):
        """NEW (round 6, kill points 5 to 7). Marker left with both HEADs at
        the pin, wherever make setup stopped: the next run drains, sets up
        again and deletes the marker; the run after does nothing."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        f.leave_intent(*old)

        result = f.run("follow", expect=0)

        self.assertIn("a follow stopped before its make setup finished", result.stdout)
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertTrue(f.gate_calls())
        self.assertFalse(f.intent.exists())

        f.run("follow", expect=0)

        self.assertEqual(1, len(f.make_calls()))

    def test_a_failed_setup_at_the_pin_keeps_the_marker(self):
        """NEW (round 6). make setup at the pin that fails again exits 1, names
        the command and keeps the marker, so the next run tries again."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        f.set_hub_pin(system, old[0], pack=old[1])
        f.leave_intent(*old)

        result = f.run("follow", expect=1, extra_env={"MAKE_RC": "1"})

        self.assertIn(f"by hand: make -C '{pack}' setup", result.stdout)
        self.assertTrue(f.intent.exists())
        f.run("follow", expect=0)
        self.assertFalse(f.intent.exists())

    def test_kill_in_the_rollback_finishes_on_the_next_run(self):
        """NEW (round 6, kill point 8). A rollback killed after it put system
        back leaves the marker with the pack at the pin: the next run moves
        system and sets the pack up again."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = f.advance_origin(system)
        f.set_hub_pin(system, pinned, pack=old[1])
        f.leave_intent(pinned, old[1])

        f.run("follow", expect=0)

        self.assertEqual((pinned, old[1]), (f.head(system), f.head(pack)))
        self.assertEqual([f"-C {pack} setup"], f.make_calls())
        self.assertFalse(f.intent.exists())

    def test_a_failed_rollback_keeps_the_marker(self):
        """NEW (round 6). When make setup fails at the pin and again at the
        old sha, the marker stays, and the next run finishes the move."""
        f = self.fixture()
        system, pack, old = self.pair(f)
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        f.set_hub_pin(system, pinned[0], pack=pinned[1])

        result = f.run("follow", expect=1, extra_env={"MAKE_RC": "1"})

        self.assertIn("follow-intent stays, so the next run finishes the move", result.stdout)
        self.assertEqual(f"system={pinned[0]}\npack={pinned[1]}\n", f.intent.read_text())
        f.run("follow", expect=0)
        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertFalse(f.intent.exists())

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


class FollowUpdateTest(unittest.TestCase):
    """follow runs `machine-setup.sh update --apply` inside the drain (sd:3168).

    The satellite job used to chain the update after follow, once the lane
    locks were released, so its satellite and bin stages could run under a
    live lane run.
    """

    def fixture(self):
        f = FollowFixture()
        self.addCleanup(f.destroy)
        f.satellite()
        return f

    def moved_pair(self, f):
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        f.pin(pack)
        old = (f.head(system), f.head(pack))
        pinned = (f.advance_origin(system), f.advance_origin(pack))
        f.set_hub_pin(system, pinned[0], pack=pinned[1])
        return system, pack, old, pinned

    def test_a_follow_that_moves_runs_the_update_while_the_lanes_are_held(self):
        """NEW. The update runs once, at the pin, with every lane lock held."""
        f = self.fixture()
        system, pack, _, pinned = self.moved_pair(f)
        lock = f.lane("some-repo")
        during = f.tmp / "during"
        hook = f'{sys.executable} {f.probe} {lock} >> {during}; git -C "{system}" rev-parse HEAD >> {during}'

        result = f.run("follow", expect=0, extra_env={"UPDATE_HOOK": hook})

        self.assertEqual(["update bin --apply", "update bin", "update satellite --apply", "update satellite"],
                         f.update_calls())
        self.assertEqual(["held", pinned[0]] * 4, during.read_text().splitlines())
        self.assertEqual("free", f.state(lock))
        self.assertIn("follow  : done", result.stdout)

    def test_the_update_never_reinstalls_the_follow_job_itself(self):
        """NEW (review round 1). A full update runs the cron and agents stages,
        which reinstall this job's own LaunchAgent; launchd then boots out the
        running follow mid-update. follow runs only the stages that need the
        locks, so it finishes and clears its marker."""
        f = self.fixture()
        system, pack, _, pinned = self.moved_pair(f)

        result = f.run("follow", expect=0)

        self.assertNotIn("bootout", f.update_calls())
        self.assertIn("follow  : done", result.stdout)
        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertFalse(f.intent.exists())

    def test_the_stages_follow_names_are_machine_setup_stages(self):
        """NEW. The stub accepts any name; the real script's STAGES must hold both."""
        script = pathlib.Path(__file__).resolve().parents[2] / "local-machine-setup" / "machine-setup.sh"
        line = next(l for l in script.read_text().splitlines() if l.startswith("STAGES="))
        for stage in ("bin", "satellite"):
            self.assertIn(stage, line.split('"')[1].split())

    def test_a_no_op_follow_runs_no_update(self):
        """PIN. At the hub's pair nothing moved, so nothing is updated."""
        f = self.fixture()
        system, pack = f.system_repo(), f.pack_repo()
        f.pin(system)
        f.pin(pack)
        f.set_hub_pin(system, f.head(system), pack=f.head(pack))

        f.run("follow", expect=0)

        self.assertEqual([], f.update_calls())

    def test_a_rollback_runs_no_update(self):
        """PIN. A failed move puts the old pair back and updates nothing."""
        f = self.fixture()
        system, pack, old, _ = self.moved_pair(f)

        f.run("follow", expect=1, extra_env={"MAKE_HOOK": FAIL_FIRST_SETUP})

        self.assertEqual(old, (f.head(system), f.head(pack)))
        self.assertEqual([], f.update_calls())

    def test_a_failed_update_exits_1_keeps_the_move_and_retries_next_run(self):
        """NEW. The move stays; one line names the command to run by hand.
        The marker stays too, so the next run sets up and updates again."""
        f = self.fixture()
        system, pack, _, pinned = self.moved_pair(f)

        result = f.run("follow", expect=1, extra_env={"UPDATE_RC": "1"})

        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertIn(f"!!! failed: machine-setup update bin; the checkouts stay at the hub's pin; "
                      f"by hand: sh '{system}/local-machine-setup/machine-setup.sh' update bin --apply",
                      result.stdout)
        self.assertNotIn("put ", result.stdout)
        self.assertTrue(f.intent.exists())

        f.run("follow", expect=0)

        self.assertEqual(["update bin --apply", "update bin --apply", "update bin",
                          "update satellite --apply", "update satellite"], f.update_calls())
        self.assertEqual(2, len(f.make_calls()))
        self.assertFalse(f.intent.exists())

    def test_an_update_that_exits_0_but_leaves_drift_is_a_failure(self):
        """NEW (review round 2). Neither stage's exit says its install worked:
        bin pipes bin-links through sed, and sd_db.satellite exits 0 when it
        could report. follow reads the stage's dry run after --apply, and a
        drift word there keeps the move and the marker, as a failed exit does."""
        f = self.fixture()
        system, pack, _, pinned = self.moved_pair(f)
        hook = '[ "$3" = --apply ] || [ "$2" != satellite ] || echo "  DIFFERS sd_db build: satellite 1, hub 2"'

        result = f.run("follow", expect=1, extra_env={"UPDATE_HOOK": hook})

        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertIn("  DIFFERS sd_db build: satellite 1, hub 2", result.stdout)
        self.assertIn(f"!!! failed: machine-setup update satellite left drift, named above; the checkouts stay "
                      f"at the hub's pin; by hand: sh '{system}/local-machine-setup/machine-setup.sh' "
                      f"update satellite --apply", result.stdout)
        self.assertNotIn("follow  : done", result.stdout)
        self.assertTrue(f.intent.exists())

        f.run("follow", expect=0)

        self.assertFalse(f.intent.exists())

    def test_a_dry_run_that_cannot_answer_is_a_failure(self):
        """NEW (review round 2). A failed check is an unknown result, not a pass."""
        f = self.fixture()
        system, pack, _, pinned = self.moved_pair(f)

        result = f.run("follow", expect=1, extra_env={"UPDATE_HOOK": '[ "$3" = --apply ] || exit 3'})

        self.assertIn("!!! failed: machine-setup update bin;", result.stdout)
        self.assertEqual(["update bin --apply", "update bin"], f.update_calls())
        self.assertTrue(f.intent.exists())

    def test_a_satellite_stage_that_skips_is_not_done(self):
        """NEW (class pass, review round 3). A SKIP is not drift, so no drift
        is no proof: a hub that did not answer left the build unchecked.
        follow wants the line the stage prints once the hub accepted this
        build; without it the move and the marker stay."""
        f = self.fixture()
        system, pack, _, pinned = self.moved_pair(f)
        skip = ("  SKIP    sd hub hub.example.test:8769 did not answer (timed out); "
                "the build and providers.yaml were not checked")
        hook = f'[ "$3" = --apply ] || [ "$2" != satellite ] || {{ echo "{skip}"; exit 0; }}'

        result = f.run("follow", expect=1, extra_env={"UPDATE_HOOK": hook})

        self.assertEqual(pinned, (f.head(system), f.head(pack)))
        self.assertIn(skip, result.stdout)
        self.assertIn("!!! failed: machine-setup update satellite did not prove its end state, printed above; "
                      "the checkouts stay at the hub's pin", result.stdout)
        self.assertNotIn("follow  : done", result.stdout)
        self.assertTrue(f.intent.exists())

        f.run("follow", expect=0)

        self.assertFalse(f.intent.exists())

    def test_a_bin_status_that_stops_part_way_is_not_done(self):
        """NEW (class pass). bin pipes bin-links status through sed, so a
        status that stops part way exits 0 with no drift word. Its last
        line, PATH, is the proof that it read every link."""
        f = self.fixture()
        self.moved_pair(f)
        row = "  repo-sync        linked                 local-repo-sync/repo-sync.sh"
        hook = f'[ "$3" = --apply ] || [ "$2" != bin ] || {{ echo "{row}"; exit 0; }}'

        result = f.run("follow", expect=1, extra_env={"UPDATE_HOOK": hook})

        self.assertIn("!!! failed: machine-setup update bin did not prove its end state", result.stdout)
        self.assertEqual(["update bin --apply", "update bin"], f.update_calls())
        self.assertTrue(f.intent.exists())

    def test_the_proof_lines_are_the_ones_the_stages_print(self):
        """NEW (class pass). follow's proof patterns match what the real
        stages print: bin-links status's last line through stage_bin's sed,
        and sd_db.satellite's line for a build the hub accepted."""
        root = pathlib.Path(__file__).resolve().parents[2]
        here = (root / "local-repo-sync" / "repo-sync.sh").read_text()
        proof = {}
        for stage in PROOF:
            found = re.search(rf"^FOLLOW_PROOF_{stage}='([^']*)'$", here, re.M)
            self.assertIsNotNone(found, f"repo-sync.sh holds no FOLLOW_PROOF_{stage}='...' line")
            proof[stage] = found.group(1)
            self.assertRegex(PROOF[stage], proof[stage])
        f = self.fixture()
        links = f.tmp / "bin"
        status = subprocess.run(
            ["sh", "-c", 'sh "$1" status | sed "s/^/  /"', "sh", str(root / "local-bin-links" / "bin-links.sh")],
            capture_output=True, text=True, timeout=60, check=True,
            env={"HOME": str(f.tmp / "home"), "BIN_LINKS_DIR": str(links), "PATH": f"{links}:/usr/bin:/bin"})
        self.assertRegex(status.stdout.splitlines()[-1], proof["bin"])
        library = (root / "local-sd-db" / "sd_db" / "satellite.py").read_text()
        self.assertIn('out.write(f"  {word:<7} {text}\\n")', library)
        self.assertIn('_line(out, "ok", f"sd_db {built[\'package\']} build {built[\'build\']} matches the hub\'s")',
                      library)
        self.assertRegex(f"  {'ok':<7} sd_db 0.1 build 0123abcd matches the hub's", proof["satellite"])

    def test_a_left_marker_with_no_pack_in_the_conf_still_updates(self):
        """NEW (class pass). With no pack checkout and system at the pin,
        nothing moves, and a marker an earlier failed update left is no
        proof the update ran: follow drains, runs both stages, and only
        then deletes it."""
        f = self.fixture()
        system = f.system_repo()
        f.pin(system)
        f.set_hub_pin(system, f.head(system), pack="e" * 40)
        f.leave_intent(f.head(system), "e" * 40)

        f.run("follow", expect=0)

        self.assertEqual(["update bin --apply", "update bin", "update satellite --apply", "update satellite"],
                         f.update_calls())
        self.assertTrue(f.gate_calls())
        self.assertFalse(f.intent.exists())

    def test_a_rollback_keeps_a_marker_an_earlier_update_left(self):
        """NEW (class pass). An update that failed at the old pair left the
        marker. A rollback from the next pair puts the checkouts back and
        runs make setup at the old sha, which proves nothing about that
        update, so the marker stays: when the hub pins the old pair again,
        follow sets up and updates instead of a no-op."""
        f = self.fixture()
        system, pack, old, pinned = self.moved_pair(f)
        f.leave_intent(*old)

        f.run("follow", expect=1, extra_env={"MAKE_HOOK": FAIL_FIRST_SETUP})

        self.assertEqual(old, (f.head(system), f.head(pack)))
        self.assertEqual([], f.update_calls())
        self.assertTrue(f.intent.exists())

        f.set_hub_pin(system, old[0], pack=old[1])
        f.run("follow", expect=0)

        self.assertEqual(["update bin --apply", "update bin", "update satellite --apply", "update satellite"],
                         f.update_calls())
        self.assertFalse(f.intent.exists())

    def test_follow_counts_the_drift_words_machine_setup_status_counts(self):
        """NEW. One vocabulary: follow's copy of status_stage's grep stays equal."""
        here = pathlib.Path(__file__).resolve().parents[1] / "repo-sync.sh"
        script = here.parents[1] / "local-machine-setup" / "machine-setup.sh"
        status = re.search(r"grep -cE '(DIFFERS[^']*)'", script.read_text()).group(1)
        follow = re.search(r"^FOLLOW_DRIFT='([^']*)'$", here.read_text(), re.M)
        self.assertIsNotNone(follow, "repo-sync.sh holds no FOLLOW_DRIFT='...' line")
        self.assertEqual(status, follow.group(1))


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
