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
import subprocess
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


# launchd for refresh's library consumers (sd:3279): a file per loaded label
# under LAUNCHD_STATE; print exits 113, launchctl's "could not find service",
# for one that is not. bootout and bootstrap go to MAKE_LOG, so a case reads
# their order against make setup. LAUNCHCTL_FAIL names the one call that
# fails, "<verb> <label>"; LAUNCHCTL_PRINT_FAIL a label whose print fails
# otherwise. LAUNCHCTL_UNLOAD_DELAY keeps a booted-out label loaded for that
# many prints, as launchd can; LAUNCHCTL_NO_LOAD a label whose bootstrap
# exits 0 and loads nothing. A bootstrap of a loaded label fails, as it does.
LAUNCHCTL_STUB = """#!/bin/sh
case "$1" in
  print)
    label=${2##*/}
    [ "$label" != "${LAUNCHCTL_PRINT_FAIL:-}" ] || exit 5
    if [ -e "$LAUNCHD_STATE/$label.pending" ]; then
      n=$(cat "$LAUNCHD_STATE/$label.pending")
      if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$LAUNCHD_STATE/$label.pending"; exit 0; fi
      rm -f "$LAUNCHD_STATE/$label" "$LAUNCHD_STATE/$label.pending"
    fi
    [ -e "$LAUNCHD_STATE/$label" ] || exit 113
    exit 0 ;;
  bootout) label=${2##*/} ;;
  bootstrap) label=$(basename "$3" .plist) ;;
  *) echo "launchctl stub: unexpected $*" >&2; exit 2 ;;
esac
printf 'launchctl %s %s\\n' "$1" "$label" >> "$MAKE_LOG"
[ "$1 $label" != "${LAUNCHCTL_FAIL:-}" ] || exit 5
if [ "$1" = bootout ]; then
  if [ -n "${LAUNCHCTL_UNLOAD_DELAY:-}" ]; then
    echo "$LAUNCHCTL_UNLOAD_DELAY" > "$LAUNCHD_STATE/$label.pending"
  else
    rm -f "$LAUNCHD_STATE/$label"
  fi
else
  [ ! -e "$LAUNCHD_STATE/$label" ] || exit 5
  [ "$label" = "${LAUNCHCTL_NO_LOAD:-}" ] || : > "$LAUNCHD_STATE/$label"
fi
"""

SERVE = "local.system-tools.sd-serve"
DASHBOARD = "local.system-tools.sd-dashboard"


class PinFixture(HygieneFixture):
    def __init__(self):
        super().__init__()
        (self.bin / "make").write_text(MAKE_STUB)
        (self.bin / "make").chmod(0o755)
        self.make_log = self.tmp / "make.log"
        self.others = 0
        (self.bin / "sd").write_text(SD_STUB)
        (self.bin / "sd").chmod(0o755)
        (self.bin / "launchctl").write_text(LAUNCHCTL_STUB)
        (self.bin / "launchctl").chmod(0o755)
        self.launchd = self.tmp / "launchd"
        self.launchd.mkdir()
        self.database = self.tmp / "sd.db"
        # The drain reads the repo registry through the pack's bin/sd-db
        # (SD_PACK_ROOT); the stub prints REPO_LIST, or fails with REPO_LIST_RC.
        self.sd_pack = self.tmp / "sd-pack-stub"
        sd_db = self.sd_pack / "bin" / "sd-db"
        sd_db.parent.mkdir(parents=True)
        sd_db.write_text('#!/bin/sh\n[ "$1 $2" = "repo list" ] || exit 2\n'
                         '[ -z "${REPO_LIST_RC:-}" ] || { echo "sd-db: boom" >&2; exit "$REPO_LIST_RC"; }\n'
                         'cat "${REPO_LIST:-/dev/null}"\n')
        sd_db.chmod(0o755)
        self.sd_log = self.tmp / "sd.log"
        self.lanes = self.tmp / "lanes"
        self.lanes.mkdir()
        helper = FOLDER / "refresh_drain.py"
        if helper.exists():
            shutil.copy(helper, self.folder / "refresh_drain.py")

    def run(self, *args, expect=0, extra_env=None):
        env = {"MAKE_LOG": str(self.make_log), "SD_LOG": str(self.sd_log),
               "SD_LANE_ROOT": str(self.lanes), "LAUNCHD_STATE": str(self.launchd),
               "REPO_SYNC_SD_DB": str(self.database), "SD_PACK_ROOT": str(self.sd_pack), "SYSTEM_TOOLS_LABEL_PREFIX": "local.system-tools",
               "XDG_STATE_HOME": str(self.tmp / "state"), **(extra_env or {})}
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
        nor a hygiene finding, so nightly exits 0 and reports nothing."""
        f = self.fixture()
        repo = f.repo()
        f.pin(repo)
        f.advance_origin(repo)

        result = f.run("nightly", expect=0)

        self.assertIn("1 behind origin/main", result.stdout)
        self.assertNotIn("SD_REPORT_ATTENTION", result.stdout)
        self.assertFalse(f.notify_log.exists(),
                         f.notify_log.read_text() if f.notify_log.exists() else "")


class DivergedBranchSyncTest(unittest.TestCase):
    """A branch other than the default, with its own commits, that its
    upstream moved past cannot fast-forward: that is work in progress, not a
    failed sync (sd:3125). Every night `!!! failed` named such a checkout."""

    def fixture(self):
        f = PinFixture()
        self.addCleanup(f.destroy)
        return f

    def diverged(self, f):
        """A checkout on branch `pr` tracking origin/main, one commit ahead
        and one behind."""
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "pr", "--track", "origin/main")
        mine = f.commit(repo, "mine", "mine\n", "local work")
        f.advance_origin(repo)
        return repo, mine

    def test_a_diverged_feature_branch_is_listed_not_failed(self):
        """NEW. Listed with its counts, left where it is, and sync exits 0."""
        f = self.fixture()
        repo, mine = self.diverged(f)

        result = f.run("sync", expect=0)

        self.assertIn("diverged: 1 checkout(s), left alone; rebase or merge by hand", result.stdout)
        self.assertIn("owner/proj on pr, 1 ahead, 1 behind origin/main", result.stdout)
        self.assertNotIn("failed", result.stdout + result.stderr)
        self.assertEqual((mine, "pr"), (f.head(repo), f.branch(repo)))

    def test_a_diverged_default_branch_still_fails(self):
        """PIN. Commits on the default branch itself are not a side branch."""
        f = self.fixture()
        repo = f.repo()
        f.commit(repo, "mine", "mine\n", "local work on main")
        f.advance_origin(repo)

        result = f.run("sync", expect=1)

        self.assertIn("failed: 1 repo(s)", result.stdout)

    def test_a_feature_branch_whose_fetch_fails_still_fails(self):
        """PIN. An unreachable upstream is not a diverged branch."""
        f = self.fixture()
        repo, _ = self.diverged(f)
        f.git(repo, "remote", "set-url", "origin", str(f.tmp / "no-such-origin"))

        result = f.run("sync", expect=1)

        self.assertIn("failed: 1 repo(s)", result.stdout)
        self.assertNotIn("diverged:", result.stdout)


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
        """NEW. A new SCHEMA_VERSION in the pack's lib/sd_db needs a migrate
        (sd:3279: the pack owns sd_db). Refresh names the pack's bin/sd-db
        steps and runs none of them."""
        f = self.fixture()
        repo = f.repo()
        schema = "lib/sd_db/schema.py"
        (repo / "lib" / "sd_db").mkdir(parents=True)
        f.commit(repo, schema, "SCHEMA_VERSION = 24\n", "schema 24")
        f.git(repo, "push", "-q", "origin", "main")
        f.pin(repo)
        f.advance_origin(repo, path=schema, content="SCHEMA_VERSION = 25\n")

        result = f.run("refresh", expect=0)

        self.assertIn("SCHEMA_VERSION 24 -> 25", result.stdout)
        self.assertIn(f"{repo}/bin/sd-db backup", result.stdout)
        self.assertIn(f"{repo}/bin/sd-db migrate", result.stdout)
        migrates = [c for c in f.git_verbs() if "migrate" in c]
        self.assertEqual([], migrates)

    def test_refresh_is_quiet_about_an_unchanged_schema(self):
        """NEW. No migrate remedy when SCHEMA_VERSION did not move."""
        f = self.fixture()
        repo = f.repo()
        schema = "lib/sd_db/schema.py"
        (repo / "lib" / "sd_db").mkdir(parents=True)
        f.commit(repo, schema, "SCHEMA_VERSION = 24\n", "schema 24")
        f.git(repo, "push", "-q", "origin", "main")
        f.pin(repo)
        f.advance_origin(repo)

        result = f.run("refresh", expect=0)

        self.assertNotIn("migrate", result.stdout)


class ConsumersTest(unittest.TestCase):
    """sd:3279: refresh stops the hub's sd_db consumers before it moves the
    pack or system, and starts them once the database's schema is the pack
    lib's. Each imports lazily, so a running one would load new modules
    into an old process. Design D2 steps 1 and 5."""

    def setUp(self):
        self.f = PinFixture()
        self.addCleanup(self.f.destroy)
        self.pack = self.f.repo("a/pack")
        (self.pack / "bin").mkdir()
        (self.pack / "lib/sd_db").mkdir(parents=True)
        self.f.commit(self.pack, "bin/sd_install.py", "", "installer")
        self.f.commit(self.pack, "lib/sd_db/schema.py", "SCHEMA_VERSION = 5\n", "schema 5")
        self.f.git(self.pack, "push", "-q", "origin", "main")
        self.f.pin(self.pack)
        self.new = self.f.advance_origin(self.pack)
        self.schema(5)
        self.load(SERVE, DASHBOARD)
        self.stopped = self.f.tmp / "state/repo-sync/refresh-stopped"

    def schema(self, version):
        # The sqlite3 CLI, as refresh reads it: only sd_db opens the store from Python.
        subprocess.run(["sqlite3", str(self.f.database), f"PRAGMA user_version = {version}"], check=True)

    def load(self, *labels):
        for label in labels:
            (self.f.launchd / label).touch()

    def loaded(self):
        return sorted(p.name for p in self.f.launchd.iterdir() if p.suffix != ".pending")

    def test_consumers_stop_before_the_move_and_start_after_it(self):
        self.f.run("refresh", expect=0)
        self.assertEqual([f"launchctl bootout {SERVE}", f"launchctl bootout {DASHBOARD}",
                          f"-C {self.pack} setup",
                          f"launchctl bootstrap {SERVE}", f"launchctl bootstrap {DASHBOARD}"],
                         self.f.make_calls())
        self.assertEqual(self.new, self.f.head(self.pack))
        self.assertEqual([DASHBOARD, SERVE], self.loaded())
        self.assertFalse(self.stopped.exists())

    def test_a_database_at_another_schema_leaves_them_stopped_until_a_rerun(self):
        self.schema(4)
        result = self.f.run("refresh", expect=1)
        self.assertEqual([], self.loaded())
        self.assertIn("database schema 4, the pack's lib/ 5", result.stdout)
        self.assertIn(f"{self.pack}/bin/sd-db migrate", result.stdout)
        self.assertEqual(f"{SERVE}\n{DASHBOARD}\n", self.stopped.read_text())
        self.schema(5)
        self.f.run("refresh", expect=0)
        self.assertEqual([DASHBOARD, SERVE], self.loaded())
        self.assertFalse(self.stopped.exists())

    def test_a_stop_that_fails_moves_nothing_and_starts_the_stopped_one_again(self):
        before = self.f.head(self.pack)
        result = self.f.run("refresh", expect=1, extra_env={"LAUNCHCTL_FAIL": f"bootout {DASHBOARD}"})
        self.assertIn("nothing moved", result.stdout)
        self.assertEqual(before, self.f.head(self.pack))
        self.assertNotIn(f"-C {self.pack} setup", self.f.make_calls())
        self.assertEqual([DASHBOARD, SERVE], self.loaded())
        # Its bootout fails again on start, so it stays listed for the next refresh.
        self.assertEqual(f"{DASHBOARD}\n", self.stopped.read_text())

    def test_a_move_that_fails_still_starts_them(self):
        self.f.run("refresh", expect=1, extra_env={"MAKE_RC": "2"})
        self.assertEqual([DASHBOARD, SERVE], self.loaded())

    def test_a_start_that_fails_stays_listed_for_the_rerun(self):
        result = self.f.run("refresh", expect=1, extra_env={"LAUNCHCTL_FAIL": f"bootstrap {SERVE}"})
        self.assertIn(f"bootstrap {SERVE}", result.stdout)
        self.assertEqual([], self.loaded())
        self.assertEqual(f"{SERVE}\n{DASHBOARD}\n", self.stopped.read_text())
        self.f.run("refresh", expect=0)
        self.assertEqual([DASHBOARD, SERVE], self.loaded())

    def test_a_refresh_killed_between_stop_and_start_is_finished_by_the_next(self):
        # The state a kill after both bootouts leaves: both listed, neither loaded.
        for label in (SERVE, DASHBOARD):
            (self.f.launchd / label).unlink()
        self.stopped.parent.mkdir(parents=True)
        self.stopped.write_text(f"{SERVE}\n{DASHBOARD}\n")
        self.f.run("refresh", expect=0)
        self.assertEqual([DASHBOARD, SERVE], self.loaded())
        self.assertFalse(self.stopped.exists())

    # Round 2, class: a launchd state refresh assumed and never confirmed.

    def test_the_move_waits_until_launchd_has_unloaded_them(self):
        seen = self.f.tmp / "seen"
        self.f.run("refresh", expect=0, extra_env={
            "LAUNCHCTL_UNLOAD_DELAY": "3", "MAKE_HOOK": f'ls "$LAUNCHD_STATE" > {seen}'})
        self.assertEqual("", seen.read_text())
        self.assertEqual([DASHBOARD, SERVE], self.loaded())
        self.assertFalse(self.stopped.exists())

    def test_an_unload_that_never_comes_moves_nothing_and_keeps_the_record(self):
        before = self.f.head(self.pack)
        result = self.f.run("refresh", expect=1, extra_env={"LAUNCHCTL_UNLOAD_DELAY": "100000"})
        self.assertIn(f"{SERVE} did not unload", result.stdout)
        self.assertIn("nothing moved", result.stdout)
        self.assertEqual(before, self.f.head(self.pack))
        self.assertNotIn(f"-C {self.pack} setup", self.f.make_calls())
        self.assertIn(SERVE, self.stopped.read_text())

    def test_a_print_that_fails_is_no_answer_and_moves_nothing(self):
        before = self.f.head(self.pack)
        result = self.f.run("refresh", expect=1, extra_env={"LAUNCHCTL_PRINT_FAIL": DASHBOARD})
        self.assertIn(f"launchctl print {DASHBOARD}", result.stdout)
        self.assertEqual(before, self.f.head(self.pack))

    def test_a_listed_consumer_still_loaded_is_restarted_not_counted(self):
        # A registration that outlived its bootout, or a refresh killed after
        # the list write and before the bootout: still the old process.
        self.stopped.parent.mkdir(parents=True)
        self.stopped.write_text(f"{SERVE}\n{DASHBOARD}\n")
        other = self.f.repo("a/other")
        self.f.run("refresh", str(other), expect=0)
        self.assertEqual([f"launchctl bootout {SERVE}", f"launchctl bootstrap {SERVE}",
                          f"launchctl bootout {DASHBOARD}", f"launchctl bootstrap {DASHBOARD}"],
                         self.f.make_calls())
        self.assertFalse(self.stopped.exists())

    def test_a_bootstrap_that_loads_nothing_keeps_the_record(self):
        result = self.f.run("refresh", expect=1, extra_env={"LAUNCHCTL_NO_LOAD": SERVE})
        self.assertIn(f"{SERVE} did not load", result.stdout)
        self.assertIn(SERVE, self.stopped.read_text())

    def test_a_refresh_of_no_hub_checkout_stops_nothing(self):
        other = self.f.repo("a/other")
        self.f.pin(other)
        self.f.advance_origin(other)
        self.f.run("refresh", str(other), expect=0)
        self.assertEqual([], [c for c in self.f.make_calls() if c.startswith("launchctl")])


if __name__ == "__main__":
    unittest.main()
