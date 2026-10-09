"""Tests for pinned checkouts: `sync` and `nightly` leave them alone, and
`repo-sync.sh refresh` moves them on the operator's command (sd:3097).

A checkout on a detached HEAD is pinned. The hub runs system and the command
pack from such checkouts, so they change only when the operator refreshes
them. Every fixture is a clone of a bare origin in a temporary directory, as
in `test_hygiene.py`; `make` is a stub that records its arguments.

Each case names its kind, as in `test_repo_sync.py`. The cases here are all
NEW: written before pinning existed and seen to fail against the script
without it.
"""

import pathlib
import shutil
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_hygiene import HygieneFixture  # noqa: E402
from test_repo_sync import FOLDER  # noqa: E402

MAKE_STUB = """#!/bin/sh
# Test stub for make: record the call, exit with MAKE_RC.
printf '%s\\n' "$*" >> "$MAKE_LOG"
eval "${MAKE_HOOK:-}"
exit "${MAKE_RC:-0}"
"""

# Refresh drains the lanes through `sd` (sd:3099). The stub answers the two
# calls it makes: an idle gate, unless SD_GATE_SCRIPT prints another answer,
# and an unset sd.lane_root, unless SD_LANE_ROOT_SETTING names one.
SD_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$SD_LOG"
case "$*" in
  "gate status --json")
    if [ -n "${SD_GATE_SCRIPT:-}" ]; then exec sh "$SD_GATE_SCRIPT"; fi
    echo '{"holders": [], "waiters": []}' ;;
  "config get sd.lane_root")
    if [ -n "${SD_CONFIG_BROKEN:-}" ]; then echo "sd: invalid sd.lane_root policy" >&2; exit 1; fi
    if [ -n "${SD_LANE_ROOT_SETTING:-}" ]; then echo "$SD_LANE_ROOT_SETTING"; exit 0; fi
    echo "sd: sd.lane_root is not set (the lane folder). Run:" >&2
    exit 1 ;;
  *) echo "sd stub: unexpected $*" >&2; exit 2 ;;
esac
"""


class PinFixture(HygieneFixture):
    def __init__(self):
        super().__init__()
        (self.bin / "make").write_text(MAKE_STUB)
        (self.bin / "make").chmod(0o755)
        self.make_log = self.tmp / "make.log"
        self.others = 0
        (self.bin / "sd").write_text(SD_STUB)
        (self.bin / "sd").chmod(0o755)
        self.sd_log = self.tmp / "sd.log"
        self.lanes = self.tmp / "lanes"
        self.lanes.mkdir()
        helper = FOLDER / "refresh_drain.py"
        if helper.exists():
            shutil.copy(helper, self.folder / "refresh_drain.py")

    def run(self, *args, expect=0, extra_env=None):
        env = {"MAKE_LOG": str(self.make_log), "SD_LOG": str(self.sd_log),
               "SD_LANE_ROOT": str(self.lanes), **(extra_env or {})}
        return super().run(*args, expect=expect, extra_env=env)

    def head(self, repo):
        return self.git(repo, "rev-parse", "HEAD")

    def short(self, repo, rev="HEAD"):
        return self.git(repo, "rev-parse", "--short", rev)

    def branch(self, repo):
        return self.git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)

    def pin(self, repo):
        self.git(repo, "switch", "-q", "--detach", "HEAD")

    def advance_origin(self, repo, path="later", content="later\n"):
        """Push one commit to `repo`'s origin from another clone, so only a
        fetch shows it here. Returns the new origin sha."""
        self.others += 1
        other = self.tmp / "other" / str(self.others)
        other.parent.mkdir(parents=True, exist_ok=True)
        bare = self.git(repo, "remote", "get-url", "origin")
        self.git(self.tmp, "clone", "-q", "-b", "main", bare, str(other))
        (other / path).parent.mkdir(parents=True, exist_ok=True)
        sha = self.commit(other, path, content, f"advance {path}")
        self.git(other, "push", "-q", "origin", "main")
        return sha

    def make_calls(self):
        if not self.make_log.exists():
            return []
        return self.make_log.read_text().splitlines()


class PinnedSyncTest(unittest.TestCase):
    def fixture(self):
        f = PinFixture()
        self.addCleanup(f.destroy)
        return f

    def test_sync_reports_a_pinned_checkout_and_does_not_pull_it(self):
        """NEW. A detached checkout is pinned: sync names it with its sha,
        runs no pull, and does not count it as failed."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        before = f.head(repo)

        result = f.run("sync", expect=0)

        self.assertIn(f"pinned at {f.short(repo)}", result.stdout)
        self.assertNotIn("failed", result.stdout + result.stderr)
        self.assertIn("all repos synced", result.stdout)
        self.assertEqual(before, f.head(repo))
        pulls = [c for c in f.git_verbs() if c.split()[:1] == ["pull"]]
        self.assertEqual([], pulls)

    def test_a_pinned_checkout_behind_origin_shows_the_count(self):
        """NEW. Sync fetches a pinned checkout, so the summary says how far
        behind origin's default branch it sits; the checkout stays put."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        before = f.head(repo)
        f.advance_origin(repo)
        f.advance_origin(repo, path="later2")

        result = f.run("sync", expect=0)

        self.assertIn(f"pinned at {f.short(repo)}, 2 behind origin/main", result.stdout)
        self.assertEqual(before, f.head(repo))

    def test_a_pinned_checkout_whose_fetch_fails_is_a_sync_failure(self):
        """NEW. Pinned is not a failure, but an unreachable origin still is,
        as it is for a checkout on a branch."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        f.git(repo, "remote", "set-url", "origin", str(f.tmp / "no-such-origin"))

        result = f.run("sync", expect=1)

        self.assertIn("failed: 1 repo(s)", result.stdout)
        self.assertIn(f"(fetch; pinned at {f.short(repo)})", result.stderr)

    def test_nightly_does_not_report_a_pinned_checkout(self):
        """NEW. A pinned checkout behind its origin is neither a sync failure
        nor a hygiene finding, so nightly exits 0 and sends nothing."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        f.advance_origin(repo)

        result = f.run("nightly", expect=0)

        self.assertIn("1 behind origin/main", result.stdout)
        self.assertFalse(f.notify_log.exists(),
                         f.notify_log.read_text() if f.notify_log.exists() else "")


class RefreshTest(unittest.TestCase):
    def fixture(self):
        f = PinFixture()
        self.addCleanup(f.destroy)
        return f

    def test_help_lists_refresh(self):
        """NEW. Every subcommand appears in `help`."""
        f = self.fixture()
        self.assertIn("refresh [path ...]", f.run("help").stdout)

    def test_refresh_moves_every_pinned_checkout_to_origin(self):
        """NEW. With no path, refresh moves each pinned conf checkout to
        origin's default branch, keeps it detached, and prints both shas."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        old = f.short(repo)
        new = f.advance_origin(repo)

        result = f.run("refresh", expect=0)

        self.assertEqual(new, f.head(repo))
        self.assertEqual("", f.branch(repo))
        self.assertIn(f"{old} -> {new[:len(old)]}", result.stdout)

    def test_refresh_takes_a_path(self):
        """NEW. A named path is refreshed even when no conf lists it."""
        f = self.fixture()
        repo = f.repo()
        f.write_conf("")
        f.pin(repo)
        new = f.advance_origin(repo)

        f.run("refresh", str(repo), expect=0)

        self.assertEqual(new, f.head(repo))

    def test_refresh_refuses_a_dirty_tree(self):
        """NEW. Uncommitted changes would ride along to the new commit, so
        refresh refuses, leaves the checkout where it was, and exits 1."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        before = f.head(repo)
        f.advance_origin(repo)
        (repo / "README").write_text("local edit\n")

        result = f.run("refresh", expect=1)

        self.assertIn("uncommitted changes", result.stdout)
        self.assertEqual(before, f.head(repo))
        self.assertEqual("local edit\n", (repo / "README").read_text())

    def test_refresh_leaves_a_branch_checkout_alone(self):
        """NEW. Only a pinned checkout moves: one on a branch is untouched,
        whether found in the conf or named as a path."""
        f = self.fixture()
        repo = f.repo()
        before = f.head(repo)
        f.advance_origin(repo)

        everything = f.run("refresh", expect=0)
        named = f.run("refresh", str(repo), expect=0)

        self.assertIn("no pinned checkouts", everything.stdout)
        self.assertIn("not pinned", named.stdout)
        self.assertEqual(before, f.head(repo))
        self.assertEqual("main", f.branch(repo))
        self.assertEqual([], f.make_calls())

    def test_refresh_runs_make_setup_in_the_pack_only(self):
        """NEW. The pack (it has bin/sd_install.py) installs its commands
        with `make setup` after the move; another checkout runs no make."""
        f = self.fixture()
        pack = f.repo("a/pack")
        (pack / "bin").mkdir()
        f.commit(pack, "bin/sd_install.py", "", "installer")
        f.git(pack, "push", "-q", "origin", "main")
        other = f.repo("a/other")
        for repo in (pack, other):
            f.pin(repo)
            f.advance_origin(repo)

        f.run("refresh", expect=0)

        self.assertEqual([f"-C {pack} setup"], f.make_calls())

    def test_a_failed_make_setup_fails_the_refresh(self):
        """NEW. The checkout has moved, so the failure names the command to
        rerun, and refresh exits 1."""
        f = self.fixture()
        pack = f.repo("a/pack")
        (pack / "bin").mkdir()
        f.commit(pack, "bin/sd_install.py", "", "installer")
        f.git(pack, "push", "-q", "origin", "main")
        f.pin(pack)
        new = f.advance_origin(pack)

        result = f.run("refresh", expect=1, extra_env={"MAKE_RC": "2"})

        self.assertEqual(new, f.head(pack))
        self.assertIn(f"make -C {pack} setup", result.stdout)

    def test_a_failed_fetch_fails_the_refresh(self):
        """NEW. An unreachable origin leaves the checkout where it was and
        exits 1."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        before = f.head(repo)
        f.git(repo, "remote", "set-url", "origin", str(f.tmp / "no-such-origin"))

        result = f.run("refresh", expect=1)

        self.assertIn("fetch", result.stdout)
        self.assertEqual(before, f.head(repo))

    def test_a_path_that_is_no_checkout_fails_the_refresh(self):
        """NEW. A mistyped path is a failure, not a silent skip."""
        f = self.fixture()

        result = f.run("refresh", str(f.tmp / "no-such-checkout"), expect=1)

        self.assertIn("not a git checkout", result.stdout)

    def test_no_origin_default_branch_fails_the_refresh(self):
        """NEW. Without origin/HEAD, origin/main or origin/master there is
        nothing to move to: the checkout stays and refresh exits 1."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        before = f.head(repo)
        bare = f.git(repo, "remote", "get-url", "origin")
        f.git(bare, "branch", "-m", "main", "trunk")
        f.git(repo, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        f.git(repo, "update-ref", "-d", "refs/remotes/origin/main")
        # git 2.48 and later recreate origin/HEAD on fetch unless told not to.
        f.git(repo, "config", "remote.origin.followRemoteHEAD", "never")

        result = f.run("refresh", expect=1)

        self.assertIn("no origin/HEAD, origin/main or origin/master", result.stdout)
        self.assertEqual(before, f.head(repo))

    def test_a_blocked_switch_fails_the_refresh_and_moves_nothing(self):
        """NEW. git refuses to overwrite an untracked file, and aborts the
        switch whole: the checkout stays where it was and refresh exits 1."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        before = f.head(repo)
        f.advance_origin(repo, path="later", content="from origin\n")
        (repo / "later").write_text("untracked here\n")

        result = f.run("refresh", expect=1)

        self.assertIn("switch to origin/main", result.stdout)
        self.assertEqual(before, f.head(repo))
        self.assertEqual("untracked here\n", (repo / "later").read_text())

    def test_an_ignored_file_origin_now_tracks_fails_the_refresh(self):
        """NEW. `git switch` overwrites an ignored file by default. Refresh
        must not: a local config or data file is often ignored, and origin
        may start tracking its path. The switch refuses, nothing moves, and
        refresh exits 1."""
        f = self.fixture()
        repo = f.repo()
        f.commit(repo, ".gitignore", "local.conf\n", "ignore local.conf")
        f.git(repo, "push", "-q", "origin", "main")
        f.pin(repo)
        before = f.head(repo)
        (repo / "local.conf").write_text("operator value\n")
        f.others += 1
        other = f.tmp / "other" / str(f.others)
        f.git(f.tmp, "clone", "-q", "-b", "main",
              f.git(repo, "remote", "get-url", "origin"), str(other))
        (other / "local.conf").write_text("from origin\n")
        f.git(other, "add", "-f", "local.conf")
        f.git(other, "commit", "-q", "-m", "track local.conf")
        f.git(other, "push", "-q", "origin", "main")

        result = f.run("refresh", expect=1)

        self.assertIn("switch to origin/main", result.stdout)
        self.assertEqual("operator value\n", (repo / "local.conf").read_text())
        self.assertEqual(before, f.head(repo))

    def test_a_checkout_with_submodules_is_refused(self):
        """NEW. `git submodule update` has no way to keep an ignored file a
        submodule checkout would overwrite, so refresh moves no checkout
        whose current or target commit holds a submodule."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        before = f.head(repo)
        f.others += 1
        other = f.tmp / "other" / str(f.others)
        f.git(f.tmp, "clone", "-q", "-b", "main",
              f.git(repo, "remote", "get-url", "origin"), str(other))
        f.git(other, "update-index", "--add", "--cacheinfo",
              f"160000,{before},sub")
        f.git(other, "commit", "-q", "-m", "add a submodule")
        f.git(other, "push", "-q", "origin", "main")

        result = f.run("refresh", expect=1)

        self.assertIn("submodule", result.stdout)
        self.assertEqual(before, f.head(repo))
        self.assertEqual([], [c for c in f.git_verbs() if c.startswith("submodule")])

    def test_refresh_prints_the_migrate_remedy_when_the_schema_changes(self):
        """NEW. A new local-sd-db SCHEMA_VERSION needs a migrate. Refresh
        names the steps and runs none of them."""
        f = self.fixture()
        repo = f.repo()
        schema = "local-sd-db/sd_db/schema.py"
        (repo / "local-sd-db" / "sd_db").mkdir(parents=True)
        f.commit(repo, schema, "SCHEMA_VERSION = 24\n", "schema 24")
        f.git(repo, "push", "-q", "origin", "main")
        f.pin(repo)
        f.advance_origin(repo, path=schema, content="SCHEMA_VERSION = 25\n")

        result = f.run("refresh", expect=0)

        self.assertIn("SCHEMA_VERSION 24 -> 25", result.stdout)
        self.assertIn("sd-db.sh migrate", result.stdout)
        self.assertIn("sd-db.sh backup", result.stdout)
        migrates = [c for c in f.git_verbs() if "migrate" in c]
        self.assertEqual([], migrates)

    def test_refresh_is_quiet_about_an_unchanged_schema(self):
        """NEW. No migrate remedy when SCHEMA_VERSION did not move."""
        f = self.fixture()
        repo = f.repo()
        schema = "local-sd-db/sd_db/schema.py"
        (repo / "local-sd-db" / "sd_db").mkdir(parents=True)
        f.commit(repo, schema, "SCHEMA_VERSION = 24\n", "schema 24")
        f.git(repo, "push", "-q", "origin", "main")
        f.pin(repo)
        f.advance_origin(repo)

        result = f.run("refresh", expect=0)

        self.assertNotIn("migrate", result.stdout)


if __name__ == "__main__":
    unittest.main()
