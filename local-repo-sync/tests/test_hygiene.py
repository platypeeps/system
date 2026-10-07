"""Tests for `repo-sync.sh hygiene` and the WORKTREE line in `reconcile` (sd:1987).

Every fixture repository lives in a temporary directory: a bare "origin" next
to the root and a clone under it, built by `Fixture.local_clone`. Its origin
is a path, so `git remote prune` works without a network, and nothing here
reads `~/repos` or the real config.

Each case names its kind, as in `test_repo_sync.py`. The cases here are all
NEW: written before `hygiene` existed and seen to fail against the script
without it. The README's test section says how to aim the suite at old code.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_repo_sync import Fixture  # noqa: E402

IDENTITY = [
    "-c", "user.name=t", "-c", "user.email=t@example.invalid",
    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
]

SQLITE_STUB = """#!/bin/sh
# Test stub for sqlite3: every item the query names is done.
echo done
"""


class HygieneFixture(Fixture):
    """A Fixture whose repositories have history, branches and worktrees."""

    def run(self, *args, expect=0, extra_env=None):
        # Every branch a case makes is seconds old, so the age guard is off
        # unless the case names it.
        env = {"REPO_SYNC_HYGIENE_MIN_AGE": "0", **(extra_env or {})}
        return super().run(*args, expect=expect, extra_env=env)

    def wrap_git(self, when, pattern, action):
        """Replace the logging git stub with one that runs the shell
        `action` `when` ("before" or "after") the real call, for arguments
        matching the `case` pattern `pattern`."""
        call = '"$REAL_GIT" "$@"; rc=$?'
        body = f"{action}\n{call}" if when == "before" else f"{call}\n{action}"
        (self.bin / "git").write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$GIT_LOG"\n'
            f'case "$*" in {pattern})\n{body}\nexit $rc ;;\nesac\n'
            'exec "$REAL_GIT" "$@"\n')

    def git(self, cwd, *args, check=True):
        result = subprocess.run(
            ["git", *IDENTITY, *args], cwd=str(cwd), capture_output=True,
            text=True, check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def repo(self, rel="a/proj"):
        """A clone at <root>/<rel> of a bare origin, on `main`, listed in
        the conf as `<subdir> owner/<name>`."""
        target = self.local_clone(rel)
        self.git(target, "remote", "set-head", "origin", "main")
        self.commit(target, "README", "seed\n", "readme")
        self.git(target, "push", "-q", "origin", "main")
        # A bare init without init.defaultBranch leaves HEAD at master, which
        # `ls-remote --symref` cannot report; hygiene would then delete nothing.
        bare = self.git(target, "remote", "get-url", "origin")
        self.git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
        subdir, name = rel.rsplit("/", 1)
        with (self.folder / f"repos.{self.profile}.conf").open("a") as conf:
            conf.write(f"{subdir} owner/{name}\n")
        return target

    def commit(self, cwd, path, content, message):
        (pathlib.Path(cwd) / path).write_text(content)
        self.git(cwd, "add", path)
        self.git(cwd, "commit", "-q", "-m", message)
        return self.git(cwd, "rev-parse", "HEAD")

    def branch_sha(self, repo, name):
        return self.git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{name}",
                        check=False)

    def worktree_paths(self, repo):
        out = self.git(repo, "worktree", "list", "--porcelain")
        return [line[len("worktree "):] for line in out.splitlines()
                if line.startswith("worktree ")]

    def add_worktree(self, repo, name, where=None):
        path = pathlib.Path(where) if where else self.tmp / "wt" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(repo, "worktree", "add", "-q", "-b", name, str(path))
        return path

    def merged_worktree(self, repo, name):
        """A worktree holding a branch whose content is already on main."""
        return self.add_worktree(repo, name)


def real(path):
    return str(pathlib.Path(path).resolve())


def lstart(pid):
    out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                         capture_output=True, text=True, check=True).stdout
    return " ".join(out.split())


class HygieneTest(unittest.TestCase):
    def fixture(self):
        f = HygieneFixture()
        self.addCleanup(f.destroy)
        return f

    def test_help_names_hygiene(self):
        """NEW. Every subcommand appears in `help`."""
        f = self.fixture()
        result = f.run("help")
        self.assertIn("hygiene", result.stdout)
        self.assertIn("--apply", result.stdout)

    def test_a_clean_fleet_reports_nothing_and_exits_0(self):
        """NEW. Requirement 9: without --apply, exit 0 when clean."""
        f = self.fixture()
        f.repo()
        result = f.run("hygiene", expect=0)
        self.assertIn("clean", result.stdout)

    def test_a_missing_worktree_directory_is_reported_then_pruned(self):
        """NEW. Criterion 1: report without --apply, prune with it."""
        f = self.fixture()
        repo = f.repo()
        wt = f.add_worktree(repo, "gone-dir")
        wt_real = real(wt)
        shutil.rmtree(wt)

        report = f.run("hygiene", expect=1)
        self.assertIn(wt_real, report.stdout)
        self.assertIn("directory gone", report.stdout)
        self.assertIn(wt_real, f.worktree_paths(repo))

        applied = f.run("hygiene", "--apply", expect=0)
        self.assertIn(wt_real, applied.stdout)
        self.assertNotIn(wt_real, f.worktree_paths(repo))

    def test_a_dead_lock_is_cleared_and_live_or_pidless_locks_are_kept(self):
        """NEW. Criterion 2. A pid that exited, and a live pid whose start
        time differs from the lock's, both count as not running. A live pid
        with the matching start time keeps its lock; so does a lock that
        names no pid."""
        f = self.fixture()
        repo = f.repo()

        exited = subprocess.Popen(["true"])
        exited.wait()
        live = subprocess.Popen(["sleep", "300"])
        self.addCleanup(live.kill)
        time.sleep(0.2)
        live_start = lstart(live.pid)

        # A reused pid shows in the seconds of its start time, the one field
        # no timezone offset changes.
        seconds = int(live_start.split(":")[2][:2])
        reused_start = f"Thu Jan  1 00:00:{(seconds + 30) % 60:02d} 1970"
        reasons = {
            "dead-pid": f"claude agent agent-a1 (pid {exited.pid} start Sat Sep 26 21:09:28 2026)",
            "reused-pid": f"claude agent agent-a2 (pid {live.pid} start {reused_start})",
            "live-pid": f"claude agent agent-a3 (pid {live.pid} start {live_start})",
            "no-pid": "claude agent agent-a4",
        }
        paths = {}
        for name, reason in reasons.items():
            wt = f.add_worktree(repo, name)
            f.git(repo, "worktree", "lock", "--reason", reason, str(wt))
            paths[name] = real(wt)
            shutil.rmtree(wt)

        f.run("hygiene", "--apply", expect=0)

        left = f.worktree_paths(repo)
        self.assertNotIn(paths["dead-pid"], left)
        self.assertNotIn(paths["reused-pid"], left)
        self.assertIn(paths["live-pid"], left)
        self.assertIn(paths["no-pid"], left)

    def test_branches_merged_three_ways_are_deleted_with_their_sha(self):
        """NEW. Criterion 3 and requirement 7: fast-forward, merge commit
        and squash all count as landed, and each deletion prints the tip."""
        f = self.fixture()
        repo = f.repo()
        tips = {}

        f.git(repo, "switch", "-q", "-c", "ff-branch")
        tips["ff-branch"] = f.commit(repo, "ff.txt", "ff\n", "ff")
        f.git(repo, "switch", "-q", "main")
        f.git(repo, "merge", "-q", "--ff-only", "ff-branch")

        f.git(repo, "switch", "-q", "-c", "merge-branch")
        tips["merge-branch"] = f.commit(repo, "merge.txt", "merge\n", "merge")
        f.git(repo, "switch", "-q", "main")
        f.commit(repo, "other.txt", "other\n", "main moves on")
        f.git(repo, "merge", "-q", "--no-ff", "-m", "merge it", "merge-branch")

        f.git(repo, "switch", "-q", "-c", "squash-branch")
        f.commit(repo, "sq1.txt", "one\n", "squash one")
        tips["squash-branch"] = f.commit(repo, "sq2.txt", "two\n", "squash two")
        f.git(repo, "switch", "-q", "main")
        f.commit(repo, "later.txt", "later\n", "main moves again")
        f.git(repo, "merge", "-q", "--squash", "squash-branch")
        f.git(repo, "commit", "-q", "-m", "squashed")
        f.git(repo, "push", "-q", "origin", "main")

        result = f.run("hygiene", "--apply", expect=0)

        for name, sha in tips.items():
            with self.subTest(branch=name):
                self.assertEqual("", f.branch_sha(repo, name))
                line = [l for l in result.stdout.splitlines() if name in l]
                self.assertTrue(line, result.stdout)
                self.assertIn(sha, line[0])
        self.assertNotEqual("", f.branch_sha(repo, "main"))

    def test_a_squash_with_different_content_is_not_deleted(self):
        """NEW. The squash probe compares patches, so a branch whose content
        differs from what landed stays."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "diverged")
        f.commit(repo, "x.txt", "branch version\n", "branch")
        f.git(repo, "push", "-q", "-u", "origin", "diverged")
        f.git(repo, "switch", "-q", "main")
        f.commit(repo, "x.txt", "main version\n", "main")
        f.git(repo, "push", "-q", "origin", "main")

        f.run("hygiene", "--apply", expect=0)

        self.assertNotEqual("", f.branch_sha(repo, "diverged"))

    def test_a_unique_commit_with_a_gone_upstream_is_reported_not_deleted(self):
        """NEW. Criterion 4, and remote-tracking refs are pruned."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "unique")
        sha = f.commit(repo, "u.txt", "unique\n", "unique")
        f.git(repo, "push", "-q", "-u", "origin", "unique")
        f.git(repo, "switch", "-q", "main")
        bare = f.git(repo, "remote", "get-url", "origin")
        f.git(bare, "branch", "-D", "unique")

        result = f.run("hygiene", "--apply", expect=0)

        self.assertEqual(sha, f.branch_sha(repo, "unique"))
        self.assertEqual("", f.git(repo, "rev-parse", "-q", "--verify",
                                   "refs/remotes/origin/unique", check=False))
        gone = [l for l in result.stdout.splitlines() if "unique" in l and "gone" in l]
        self.assertTrue(gone, result.stdout)

    def test_prune_accounting_does_not_read_translated_output(self):
        """NEW. Requirement 3.3: git translates the prune lines under a
        non-C locale; the counts must not depend on them. The stub
        translates whenever LC_ALL is not C."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "push", "-q", "origin", "main:elsewhere")
        f.git(repo, "fetch", "-q", "origin")
        bare = f.git(repo, "remote", "get-url", "origin")
        f.git(bare, "branch", "-D", "elsewhere")
        (f.bin / "git").write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$GIT_LOG"\n'
            'case "$*" in *"remote prune"*)\n'
            '  [ "$LC_ALL" = C ] || { "$REAL_GIT" "$@" 2>&1 | sed -e "s/would prune/wird entfernt/" -e "s/pruned/entfernt/"; exit 0; } ;;\n'
            'esac\n'
            'exec "$REAL_GIT" "$@"\n')
        env = {"LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8"}

        report = f.run("hygiene", expect=1, extra_env=env)
        applied = f.run("hygiene", "--apply", expect=0, extra_env=env)

        self.assertIn("would prune remote-tracking ref origin/elsewhere", report.stdout)
        self.assertIn("pruned remote-tracking ref origin/elsewhere", applied.stdout)
        self.assertIn("1 done", applied.stdout)

    def test_a_never_pushed_branch_is_reported_not_deleted(self):
        """NEW. Requirement 5: commits on no remote ref are listed."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "local-only")
        sha = f.commit(repo, "l.txt", "local\n", "local")
        f.git(repo, "switch", "-q", "main")

        report = f.run("hygiene", expect=1)
        self.assertIn("local-only", report.stdout)
        self.assertIn("no remote", report.stdout)
        f.run("hygiene", "--apply", expect=0)
        self.assertEqual(sha, f.branch_sha(repo, "local-only"))

    def test_a_branch_for_a_done_item_is_listed(self):
        """NEW. Requirement 5: a trailing -<n> names an item; a done item is a
        landing candidate, listed and never deleted."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "feat/thing-1234")
        sha = f.commit(repo, "t.txt", "thing\n", "thing")
        f.git(repo, "push", "-q", "-u", "origin", "feat/thing-1234")
        f.git(repo, "switch", "-q", "main")
        (f.bin / "sqlite3").write_text(SQLITE_STUB)
        (f.bin / "sqlite3").chmod(0o755)
        db = f.tmp / "sd.db"
        db.write_text("")

        result = f.run("hygiene", expect=1, extra_env={"REPO_SYNC_SD_DB": str(db)})

        self.assertIn("sd:1234", result.stdout)
        f.run("hygiene", "--apply", extra_env={"REPO_SYNC_SD_DB": str(db)})
        self.assertEqual(sha, f.branch_sha(repo, "feat/thing-1234"))

    def test_a_landed_branch_for_a_done_item_is_deleted(self):
        """NEW. Requirement 5: the report-only classes cover branches whose
        content is not on the default branch. A landed branch goes by 3.4,
        even when its item is done and its commits are on no remote (a
        squash landing whose remote branch was deleted)."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "feat/thing-1234")
        f.commit(repo, "t.txt", "thing\n", "thing")
        f.git(repo, "switch", "-q", "main")
        f.git(repo, "merge", "-q", "--squash", "feat/thing-1234")
        f.git(repo, "commit", "-q", "-m", "squashed")
        f.git(repo, "push", "-q", "origin", "main")
        (f.bin / "sqlite3").write_text(SQLITE_STUB)
        (f.bin / "sqlite3").chmod(0o755)
        db = f.tmp / "sd.db"
        db.write_text("")

        f.run("hygiene", "--apply", expect=0, extra_env={"REPO_SYNC_SD_DB": str(db)})

        self.assertEqual("", f.branch_sha(repo, "feat/thing-1234"))

    def test_a_stale_default_ref_deletes_no_landed_branch(self):
        """NEW. Requirement 3.4: a branch counts as landed against the local
        origin/<default>. When the remote's default no longer matches it (a
        force-push removed the content), no landed branch is deleted."""
        f = self.fixture()
        repo = f.repo()
        seed = f.git(repo, "rev-parse", "HEAD")
        f.git(repo, "switch", "-q", "-c", "rewound")
        sha = f.commit(repo, "r.txt", "rewound\n", "rewound")
        f.git(repo, "switch", "-q", "main")
        f.git(repo, "merge", "-q", "--ff-only", "rewound")
        f.git(repo, "push", "-q", "origin", "main")
        bare = f.git(repo, "remote", "get-url", "origin")
        f.git(bare, "update-ref", "refs/heads/main", seed)

        result = f.run("hygiene", "--apply", expect=0)

        self.assertEqual(sha, f.branch_sha(repo, "rewound"))
        self.assertIn("landed branches not deleted", result.stdout)

    def test_without_origin_default_no_landed_branch_is_deleted(self):
        """NEW. Requirement 3.4: with no origin/<default> to verify, the
        local main proves nothing is on origin, so nothing is deleted."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "local-only")
        f.git(repo, "remote", "set-head", "origin", "-d")
        f.git(repo, "update-ref", "-d", "refs/remotes/origin/main")

        result = f.run("hygiene", "--apply", expect=0)

        self.assertNotEqual("", f.branch_sha(repo, "local-only"))
        self.assertIn("landed branches not deleted", result.stdout)

    def test_a_checkout_behind_with_local_changes_is_listed(self):
        """NEW. Requirement 5: a checkout sync could not fast-forward."""
        f = self.fixture()
        repo = f.repo()
        other = f.tmp / "other"
        bare = f.git(repo, "remote", "get-url", "origin")
        f.git(f.tmp, "clone", "-q", "-b", "main", bare, str(other))
        f.commit(other, "README", "upstream\n", "upstream change")
        f.git(other, "push", "-q", "origin", "main")
        f.git(repo, "fetch", "-q", "origin")
        (repo / "README").write_text("local edit\n")

        result = f.run("hygiene", expect=1)

        self.assertIn("behind", result.stdout)

    def test_a_merged_branch_in_a_clean_worktree_goes_with_its_worktree(self):
        """NEW. Requirement 3.5: a worktree with nothing uncommitted,
        untracked or ignored is removed, then the branch is deleted."""
        f = self.fixture()
        repo = f.repo()
        wt = f.merged_worktree(repo, "clean-merged")

        f.run("hygiene", "--apply", expect=0)

        self.assertFalse(wt.exists())
        self.assertEqual("", f.branch_sha(repo, "clean-merged"))

    def test_a_merged_branch_in_a_worktree_with_ignored_files_keeps_both(self):
        """NEW. Requirement 3.5: ignored files (an .env, a local database)
        are not rebuildable in general, so they keep the worktree."""
        f = self.fixture()
        repo = f.repo()
        (repo / ".gitignore").write_text(".env\n")
        f.git(repo, "add", ".gitignore")
        f.git(repo, "commit", "-q", "-m", "ignore env")
        f.git(repo, "push", "-q", "origin", "main")
        wt = f.merged_worktree(repo, "ignored-merged")
        (wt / ".env").write_text("TOKEN=change-me\n")

        result = f.run("hygiene", "--apply", expect=0)

        self.assertTrue((wt / ".env").exists())
        self.assertNotEqual("", f.branch_sha(repo, "ignored-merged"))
        self.assertIn("KEEP     branch ignored-merged", result.stdout)

    def test_a_fresh_landed_branch_is_kept_by_default(self):
        """NEW. Requirement 3.4: a branch made a moment ago sits at the
        default tip and counts as landed; the default age guard keeps it."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "fresh")

        result = f.run("hygiene", "--apply", expect=0,
                       extra_env={"REPO_SYNC_HYGIENE_MIN_AGE": None})

        self.assertNotEqual("", f.branch_sha(repo, "fresh"))
        # A repo with only a note still prints it (design: notes print but
        # do not count).
        self.assertIn("note: kept branch fresh", result.stdout)

    def test_an_old_landed_branch_goes_under_the_default_age(self):
        """NEW. Requirement 3.4: the guard reads the newest reflog entry,
        so a branch last moved two days ago is deleted."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "old")
        log = repo / ".git" / "logs" / "refs" / "heads" / "old"
        old = str(int(time.time()) - 2 * 86400)
        log.write_text(re.sub(r"> \d+ ", f"> {old} ", log.read_text()))

        f.run("hygiene", "--apply", expect=0,
              extra_env={"REPO_SYNC_HYGIENE_MIN_AGE": None})

        self.assertEqual("", f.branch_sha(repo, "old"))

    def test_a_branch_whose_tip_moves_before_the_delete_is_kept(self):
        """NEW. Requirement 4: the delete compares the tip it classified,
        so a commit landing between the check and the delete survives."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "racing")
        moved = f.git(repo, "commit-tree", "-p", "HEAD", "-m", "late",
                      f.git(repo, "rev-parse", "HEAD^{tree}"))
        f.wrap_git("before", '*"-D racing"*|*"update-ref -d refs/heads/racing"*',
                   f'"$REAL_GIT" -C "{repo}" update-ref refs/heads/racing {moved}')

        f.run("hygiene", "--apply", expect=None)

        self.assertEqual(moved, f.branch_sha(repo, "racing"))

    def test_a_branch_checked_out_before_the_delete_is_kept(self):
        """NEW. Requirement 4: the worktree list is read again just before
        the delete, so a worktree added after classification keeps it."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "grabbed")
        wt = f.tmp / "wt" / "grabbed"
        f.wrap_git("after", '*for-each-ref*',
                   f'"$REAL_GIT" -C "{repo}" worktree add -q "{wt}" grabbed >/dev/null 2>&1')

        f.run("hygiene", "--apply", expect=None)

        self.assertNotEqual("", f.branch_sha(repo, "grabbed"))
        self.assertTrue(wt.exists())

    def test_a_branch_checked_out_during_the_delete_is_restored(self):
        """NEW. Requirement 4: git has no lock a checkout honours, so a
        checkout can land between the last worktree read and the delete.
        The list is read once more after it, and the ref comes back."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "late")
        sha = f.branch_sha(repo, "late")
        wt = f.tmp / "wt" / "late"
        f.wrap_git("before", '*"update-ref -d refs/heads/late"*',
                   f'"$REAL_GIT" -C "{repo}" worktree add -q "{wt}" late >/dev/null 2>&1')

        result = f.run("hygiene", "--apply", expect=1)

        self.assertEqual(sha, f.branch_sha(repo, "late"))
        self.assertEqual(sha, f.git(wt, "rev-parse", "HEAD"))
        self.assertIn("restored", result.stdout)

    def test_an_unreadable_worktree_list_deletes_no_branch(self):
        """NEW. Requirement 4: a worktree list git cannot produce proves
        nothing about who holds a branch, so no landed branch is deleted."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "landed")
        f.wrap_git("before", '*"worktree list"*', "exit 1")

        f.run("hygiene", "--apply", expect=1)

        self.assertNotEqual("", f.branch_sha(repo, "landed"))

    def test_a_worktree_list_failing_just_before_the_delete_keeps_the_branch(self):
        """NEW. Requirement 4: the re-read before the delete fails closed;
        an unreadable list counts as checked out."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "landed")
        mark = f.tmp / "classified"
        (f.bin / "git").write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$GIT_LOG"\n'
            f'case "$*" in *for-each-ref*) : > "{mark}" ;;\n'
            f'*"worktree list"*) [ -e "{mark}" ] && exit 1 ;;\nesac\n'
            'exec "$REAL_GIT" "$@"\n')

        result = f.run("hygiene", "--apply", expect=1)

        self.assertNotEqual("", f.branch_sha(repo, "landed"))
        self.assertIn("worktrees unreadable", result.stdout)

    def test_a_moved_remote_default_deletes_no_landed_branch(self):
        """NEW. Requirement 3.4: when origin makes another branch its
        default, the local origin/HEAD is stale even though origin/main is
        unchanged, so no landed branch is deleted."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "landed")
        bare = f.git(repo, "remote", "get-url", "origin")
        f.git(bare, "update-ref", "refs/heads/trunk", f.git(repo, "rev-parse", "HEAD"))
        f.git(bare, "symbolic-ref", "HEAD", "refs/heads/trunk")

        result = f.run("hygiene", "--apply", expect=0)

        self.assertNotEqual("", f.branch_sha(repo, "landed"))
        self.assertIn("origin/HEAD here names main, on origin refs/heads/trunk", result.stdout)

    def test_a_landed_branch_sharing_a_tag_name_is_deleted(self):
        """NEW. Requirement 3.4: a tag with the branch's name makes
        `refname:short` print `heads/<name>`; the name is taken whole."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "twin")
        f.git(repo, "tag", "twin")

        result = f.run("hygiene", "--apply", expect=0)

        self.assertEqual("", f.branch_sha(repo, "twin"))
        self.assertIn("deleted branch twin ", result.stdout)

    def test_a_merged_branch_in_a_dirty_worktree_keeps_both(self):
        """NEW. Criterion 5."""
        f = self.fixture()
        repo = f.repo()
        wt = f.merged_worktree(repo, "dirty-merged")
        (wt / "README").write_text("uncommitted\n")

        result = f.run("hygiene", "--apply", expect=0)

        self.assertTrue(wt.exists())
        self.assertNotEqual("", f.branch_sha(repo, "dirty-merged"))
        self.assertIn(real(wt), f.worktree_paths(repo))
        self.assertIn("dirty-merged", result.stdout)

    def test_a_merged_branch_in_a_live_worktree_keeps_both(self):
        """NEW. Requirement 3.5: a process with its cwd inside the worktree
        keeps it."""
        f = self.fixture()
        repo = f.repo()
        wt = f.merged_worktree(repo, "busy-merged")
        proc = subprocess.Popen(["sleep", "300"], cwd=str(wt / "."))
        self.addCleanup(proc.kill)
        time.sleep(0.2)

        f.run("hygiene", "--apply", expect=0)

        self.assertTrue(wt.exists())
        self.assertNotEqual("", f.branch_sha(repo, "busy-merged"))

    def test_a_live_agent_worktree_is_kept_quietly(self):
        """NEW. A fresh agent branch sits at the default tip and so counts as
        landed. A lock whose holder runs keeps it, and keeps it out of the
        count, so a nightly does not mail about a running agent."""
        f = self.fixture()
        repo = f.repo()
        live = subprocess.Popen(["sleep", "300"])
        self.addCleanup(live.kill)
        time.sleep(0.2)
        wt = f.merged_worktree(repo, "agent-branch")
        f.git(repo, "worktree", "lock", "--reason",
              f"claude agent agent-a5 (pid {live.pid} start {lstart(live.pid)})", str(wt))

        report = f.run("hygiene", expect=0)
        self.assertIn("clean", report.stdout)
        f.run("hygiene", "--apply", expect=0)
        self.assertTrue(wt.exists())
        self.assertNotEqual("", f.branch_sha(repo, "agent-branch"))

    def test_a_squash_that_differs_only_in_whitespace_is_not_deleted(self):
        """NEW (review round 1). `git cherry` ignores whitespace, so the squash
        match is confirmed verbatim; a branch whose content differs from what
        landed only in whitespace stays."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "ws-branch")
        sha = f.commit(repo, "w.txt", "hello world\n", "branch")
        f.git(repo, "switch", "-q", "main")
        f.commit(repo, "w.txt", "hello   world\n", "landed with other spacing")
        f.git(repo, "push", "-q", "origin", "main")

        f.run("hygiene", "--apply", expect=0)

        self.assertEqual(sha, f.branch_sha(repo, "ws-branch"))

    def test_report_mode_writes_no_object_into_the_checkout(self):
        """NEW (review round 1). The squash probe's synthetic commit goes to a
        throwaway object directory."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "probe-me")
        f.commit(repo, "p.txt", "probe\n", "probe")
        f.git(repo, "push", "-q", "-u", "origin", "probe-me")
        f.git(repo, "switch", "-q", "main")
        before = f.git(repo, "count-objects")

        f.run("hygiene", expect=None)

        self.assertEqual(before, f.git(repo, "count-objects"))

    def test_a_failing_process_scan_keeps_the_worktree(self):
        """NEW (review round 1). When lsof fails, nothing is known about who
        uses a worktree, so it counts as in use."""
        f = self.fixture()
        repo = f.repo()
        wt = f.merged_worktree(repo, "unknown-users")
        # Partial output and a failing status: the status decides.
        (f.bin / "lsof").write_text("#!/bin/sh\necho p1\necho n/nowhere\nexit 1\n")
        (f.bin / "lsof").chmod(0o755)

        f.run("hygiene", "--apply", expect=0,
              extra_env={"REPO_SYNC_PROC": str(f.tmp / "no-proc")})

        self.assertTrue(wt.exists())
        self.assertNotEqual("", f.branch_sha(repo, "unknown-users"))

    def test_a_live_pid_that_ps_cannot_read_keeps_its_lock(self):
        """NEW (review round 1). Only "No such process" means dead; a ps that
        prints nothing for a live pid keeps the lock."""
        f = self.fixture()
        repo = f.repo()
        live = subprocess.Popen(["sleep", "300"])
        self.addCleanup(live.kill)
        time.sleep(0.2)
        wt = f.add_worktree(repo, "ps-blind")
        f.git(repo, "worktree", "lock", "--reason",
              f"claude agent agent-a6 (pid {live.pid} start {lstart(live.pid)})", str(wt))
        shutil.rmtree(wt)
        (f.bin / "ps").write_text("#!/bin/sh\nexit 1\n")
        (f.bin / "ps").chmod(0o755)

        f.run("hygiene", "--apply", expect=0)

        self.assertIn(real(wt), f.worktree_paths(repo))

    def test_a_squash_reverted_on_main_is_not_deleted(self):
        """NEW (review round 2). The squash landed and was reverted later; the
        branch's content is no longer on main, so the branch stays."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "reverted-squash")
        sha = f.commit(repo, "r.txt", "reverted\n", "branch")
        f.git(repo, "switch", "-q", "main")
        f.commit(repo, "other.txt", "other\n", "main moves on")
        f.git(repo, "merge", "-q", "--squash", "reverted-squash")
        f.git(repo, "commit", "-q", "-m", "squashed")
        f.git(repo, "revert", "--no-edit", "HEAD")
        f.git(repo, "push", "-q", "origin", "main")

        f.run("hygiene", "--apply", expect=0)

        self.assertEqual(sha, f.branch_sha(repo, "reverted-squash"))

    def test_a_squash_whose_mode_changed_on_main_is_not_deleted(self):
        """NEW (review round 3). The squash landed an executable file and main
        dropped the bit later; same blob, different mode, so the branch
        stays."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "mode-squash")
        (repo / "run.sh").write_text("#!/bin/sh\n")
        (repo / "run.sh").chmod(0o755)
        f.git(repo, "add", "run.sh")
        f.git(repo, "commit", "-q", "-m", "branch")
        sha = f.git(repo, "rev-parse", "HEAD")
        f.git(repo, "switch", "-q", "main")
        f.commit(repo, "other.txt", "other\n", "main moves on")
        f.git(repo, "merge", "-q", "--squash", "mode-squash")
        f.git(repo, "commit", "-q", "-m", "squashed")
        f.git(repo, "update-index", "--chmod=-x", "run.sh")
        f.git(repo, "commit", "-q", "-m", "drop the bit")
        f.git(repo, "push", "-q", "origin", "main")

        f.run("hygiene", "--apply", expect=0)

        self.assertEqual(sha, f.branch_sha(repo, "mode-squash"))

    def test_a_lock_written_in_another_timezone_is_kept(self):
        """NEW (review round 2). The lock's start time was formatted in UTC;
        the sweep runs seven hours west. The live holder keeps its lock."""
        f = self.fixture()
        repo = f.repo()
        live = subprocess.Popen(["sleep", "300"])
        self.addCleanup(live.kill)
        time.sleep(0.2)
        utc_start = " ".join(subprocess.run(
            ["ps", "-p", str(live.pid), "-o", "lstart="], capture_output=True,
            text=True, check=True, env=dict(os.environ, TZ="UTC")).stdout.split())
        wt = f.add_worktree(repo, "other-tz")
        f.git(repo, "worktree", "lock", "--reason",
              f"claude agent agent-a7 (pid {live.pid} start {utc_start})", str(wt))
        shutil.rmtree(wt)

        f.run("hygiene", "--apply", expect=0, extra_env={"TZ": "Etc/GMT+7"})

        self.assertIn(real(wt), f.worktree_paths(repo))

    def test_a_failing_ps_keeps_the_lock_whatever_it_prints(self):
        """NEW (review round 2). A ps that exits non-zero proves nothing, even
        when it prints something that is not the lock's start time."""
        f = self.fixture()
        repo = f.repo()
        live = subprocess.Popen(["sleep", "300"])
        self.addCleanup(live.kill)
        time.sleep(0.2)
        wt = f.add_worktree(repo, "ps-fails")
        f.git(repo, "worktree", "lock", "--reason",
              f"claude agent agent-a8 (pid {live.pid} start {lstart(live.pid)})", str(wt))
        shutil.rmtree(wt)
        (f.bin / "ps").write_text("#!/bin/sh\necho 'Thu Jan  1 00:00:07 1970'\nexit 1\n")
        (f.bin / "ps").chmod(0o755)

        f.run("hygiene", "--apply", expect=0)

        self.assertIn(real(wt), f.worktree_paths(repo))

    def test_a_checkout_without_a_default_branch_still_prunes(self):
        """NEW (review round 1). The prunes need no default branch, so a
        checkout without one still reports its missing worktree."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "-q", "-m", "main", "trunk")
        f.git(repo, "remote", "set-head", "origin", "-d")
        wt = f.add_worktree(repo, "orphaned")
        shutil.rmtree(wt)

        report = f.run("hygiene", expect=1)

        self.assertIn(real(wt), report.stdout)

    def test_the_checked_out_branch_of_the_main_checkout_is_kept(self):
        """NEW. Requirement 4: never the checked-out branch of a worktree
        that stays, and never the default branch."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "current")

        f.run("hygiene", "--apply", expect=0)

        self.assertNotEqual("", f.branch_sha(repo, "current"))
        self.assertNotEqual("", f.branch_sha(repo, "main"))

    def test_a_stash_is_never_touched(self):
        """NEW. Criterion 6: the stash survives an --apply that deletes the
        branch it was made on."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "stashed-on")
        (repo / "README").write_text("work in progress\n")
        f.git(repo, "stash", "push", "-q", "-m", "keep me")
        f.git(repo, "switch", "-q", "main")
        before = f.git(repo, "rev-parse", "refs/stash")
        listing = f.git(repo, "stash", "list")

        f.run("hygiene", "--apply", expect=0)

        self.assertEqual(before, f.git(repo, "rev-parse", "refs/stash"))
        self.assertEqual(listing, f.git(repo, "stash", "list"))

    def test_hygiene_classifies_a_worktree_under_the_root_through_its_parent(self):
        """NEW. Criterion 7, the hygiene half: a linked worktree placed under
        the root, in a directory named after its branch, is found through
        the parent's registrations."""
        f = self.fixture()
        repo = f.repo()
        wt = f.add_worktree(repo, "feature-thing", where=f.root / "a" / "feature-thing")

        report = f.run("hygiene", expect=1)
        lines = [l for l in report.stdout.splitlines() if "feature-thing" in l]
        self.assertTrue(any("branch" in l for l in lines), report.stdout)

        f.run("hygiene", "--apply", expect=0)
        self.assertFalse(wt.exists())
        self.assertEqual("", f.branch_sha(repo, "feature-thing"))
        self.assertNotIn("feature-thing", (f.folder / "repos.terra.conf").read_text())

    def test_report_mode_lists_a_gone_upstream_before_the_prune(self):
        """NEW (sd:2071). Report mode prunes nothing, so the stale
        remote-tracking ref still exists and `%(upstream:track)` never reads
        [gone]; the dry run's list names the branch GONE all the same."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "switch", "-q", "-c", "unique")
        sha = f.commit(repo, "u.txt", "unique\n", "unique")
        f.git(repo, "push", "-q", "-u", "origin", "unique")
        f.git(repo, "switch", "-q", "main")
        bare = f.git(repo, "remote", "get-url", "origin")
        f.git(bare, "branch", "-D", "unique")

        # Report mode exits 1 when anything would be acted on.
        result = f.run("hygiene", expect=1)

        self.assertIn(f"GONE     branch unique {sha}", result.stdout)
        self.assertNotEqual("", f.git(repo, "rev-parse", "-q", "--verify",
                                      "refs/remotes/origin/unique", check=False))

    def test_a_min_age_the_shell_cannot_compare_is_refused(self):
        """NEW (sd:2074). A digits-only age past the shell's integer range
        made the age test false, which switched the guard off; it is
        refused, and nothing is deleted."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "fresh")

        result = f.run("hygiene", "--apply", expect=2,
                       extra_env={"REPO_SYNC_HYGIENE_MIN_AGE": "99999999999999999999"})

        self.assertIn("REPO_SYNC_HYGIENE_MIN_AGE", result.stderr)
        self.assertNotEqual("", f.branch_sha(repo, "fresh"))

    def test_a_min_age_with_leading_zeros_is_read_as_decimal(self):
        """NEW (sd:2074). Leading zeros do not count against the length
        limit, and the value is still decimal."""
        f = self.fixture()
        repo = f.repo()
        f.git(repo, "branch", "fresh")

        f.run("hygiene", "--apply", expect=0,
              extra_env={"REPO_SYNC_HYGIENE_MIN_AGE": "00000000000000000000086400"})

        self.assertNotEqual("", f.branch_sha(repo, "fresh"))

    def fake_proc(self, f, pids):
        """A /proc stand-in: `self/cwd` plus one `<pid>/cwd` per entry of
        `pids`, a path (a readable link) or None (a plain file, which
        readlink cannot read)."""
        proc = f.tmp / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "self" / "cwd").symlink_to(f.tmp)
        for pid, cwd in pids.items():
            (proc / str(pid)).mkdir()
            if cwd is None:
                (proc / str(pid) / "cwd").write_text("")
            else:
                (proc / str(pid) / "cwd").symlink_to(cwd)
        return proc

    def test_an_unreadable_proc_cwd_that_persists_keeps_the_worktree(self):
        """NEW (sd:2074). A process of this user whose cwd cannot be read
        may sit in the worktree, so the scan fails closed."""
        f = self.fixture()
        repo = f.repo()
        wt = f.merged_worktree(repo, "proc-blind")
        proc = self.fake_proc(f, {101: f.tmp, 102: None})

        f.run("hygiene", "--apply", expect=0, extra_env={"REPO_SYNC_PROC": str(proc)})

        self.assertTrue(wt.exists())
        self.assertNotEqual("", f.branch_sha(repo, "proc-blind"))

    def test_a_readable_proc_scan_removes_an_unused_worktree(self):
        """NEW (sd:2074). The /proc path still removes a worktree when every
        cwd reads and none is inside it."""
        f = self.fixture()
        repo = f.repo()
        wt = f.merged_worktree(repo, "proc-clear")
        proc = self.fake_proc(f, {101: f.tmp})

        f.run("hygiene", "--apply", expect=0, extra_env={"REPO_SYNC_PROC": str(proc)})

        self.assertFalse(wt.exists())
        self.assertEqual("", f.branch_sha(repo, "proc-clear"))

    def test_a_pid_that_exits_during_the_proc_scan_does_not_block(self):
        """NEW (sd:2074). A process that exits between the listing and the
        read leaves no entry; that is not an unreadable cwd."""
        f = self.fixture()
        repo = f.repo()
        wt = f.merged_worktree(repo, "proc-vanish")
        proc = self.fake_proc(f, {101: f.tmp, 103: f.tmp})
        # readlink finds pid 103 gone: the stub removes it, then fails.
        (f.bin / "readlink").write_text(
            "#!/bin/sh\n"
            'case "$1" in */103/cwd) rm -rf "${1%/cwd}"; exit 1 ;; esac\n'
            'exec /usr/bin/readlink "$@"\n')
        (f.bin / "readlink").chmod(0o755)

        f.run("hygiene", "--apply", expect=0, extra_env={"REPO_SYNC_PROC": str(proc)})

        self.assertFalse(wt.exists())
        self.assertEqual("", f.branch_sha(repo, "proc-vanish"))


class ReconcileWorktreeTest(unittest.TestCase):
    def test_a_worktree_under_the_root_is_listed_as_worktree(self):
        """NEW. Criterion 7, the reconcile half: WORKTREE, not MISMATCH, and
        the conf stays as it was."""
        f = HygieneFixture()
        self.addCleanup(f.destroy)
        parent = f.checkout("a/proj", "owner/proj")
        f.commit(parent, "README", "seed\n", "seed")
        f.write_conf("a owner/proj\n")
        f.git(parent, "worktree", "add", "-q", "-b", "sd-1987-thing",
              str(f.root / "a" / "sd-1987-thing"))

        result = f.run("reconcile")

        self.assertIn("WORKTREE a/sd-1987-thing (of a/proj)", result.stdout)
        self.assertNotIn("MISMATCH", result.stdout)
        self.assertEqual(f.entries(), ["a owner/proj"])


class NightlyHygieneTest(unittest.TestCase):
    def fixture(self):
        f = HygieneFixture()
        self.addCleanup(f.destroy)
        return f

    def test_nightly_runs_hygiene_apply_after_sync_and_mails_the_report(self):
        """NEW. Criterion 8, first half."""
        f = self.fixture()
        repo = f.repo()
        wt = f.add_worktree(repo, "gone-dir")
        shutil.rmtree(wt)

        f.run("nightly", expect=0)

        verbs = f.git_verbs()
        pulls = [i for i, v in enumerate(verbs) if v.startswith("pull")]
        prunes = [i for i, v in enumerate(verbs) if v.startswith("-C") and "worktree prune" in v]
        self.assertTrue(pulls, verbs)
        self.assertTrue(prunes, verbs)
        self.assertLess(pulls[0], prunes[0])
        self.assertNotIn(real(wt), f.worktree_paths(repo))
        log = f.notify_log.read_text()
        self.assertIn("hygiene", log)
        self.assertIn(real(wt), log)

    def test_nightly_is_silent_when_hygiene_finds_nothing(self):
        """NEW. Criterion 8, second half: a clean fleet mails nothing."""
        f = self.fixture()
        f.repo()

        f.run("nightly", expect=0)

        self.assertFalse(
            f.notify_log.exists(),
            f.notify_log.read_text() if f.notify_log.exists() else "",
        )


JEV_STUB = """#!/bin/sh
# Test stub for local-jev: log each call, and keep what `ask` was handed.
# `enabled STAGE` reads that stage's variable, as the real command does.
printf '%s run=%s\\n' "$*" "${JEV_RUN:-}" >> "$JEV_LOG"
case "$1" in
  enabled)
    eval "word=\\${$2:-}"
    case "$word" in 0|off|false|no|disabled) exit 3 ;; esac
    exit 0 ;;
  ask)
    while [ $# -gt 0 ]; do
      case "$1" in
        --questions) cp "$2" "$JEV_LOG.questions" ;;
        --state) cp "$2" "$JEV_LOG.state" ;;
      esac
      shift
    done
    exit "${JEV_STUB_EXIT:-0}" ;;
esac
exit 0
"""


class NightlyJevShadowTest(unittest.TestCase):
    """sd:2094: Jev classifies each listed line as live, abandoned or
    superseded, in shadow: the report keeps its order and its words."""

    def fixture(self, listed=True):
        f = HygieneFixture()
        self.addCleanup(f.destroy)
        jev = f.tmp / "local-jev"
        jev.mkdir()
        (jev / "jev.sh").write_text(JEV_STUB)
        self.log = f.tmp / "jev.log"
        repo = f.repo()
        if listed:
            f.git(repo, "checkout", "-q", "-b", "sd-2094-private-topic")
            self.sha = f.commit(repo, "notes", "x\n", "never pushed")
            f.git(repo, "checkout", "-q", "main")
        return f

    def nightly(self, f, **extra):
        return f.run("nightly", expect=0,
                     extra_env={"JEV_LOG": str(self.log), "JEV_RUN": "", **extra})

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_listed_line_is_asked_about_in_shadow(self):
        f = self.fixture()
        self.nightly(f)
        calls = self.calls()
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("enabled JEV_REPO_SYNC_HYGIENE --record --caller local-repo-sync",
                      calls[0])
        self.assertTrue(calls[1].startswith("ask "), calls)
        for flag in ("--shadow {}", "--caller local-repo-sync",
                     "--stage JEV_REPO_SYNC_HYGIENE", "--state-format json"):
            self.assertIn(flag, calls[1])
        questions = json.loads(pathlib.Path(f"{self.log}.questions").read_text())
        self.assertEqual(list(questions), ["h1"])
        self.assertEqual(questions["h1"]["type"], "choice")
        self.assertEqual(list(questions["h1"]["criteria"]),
                         ["live", "abandoned", "superseded"])
        state = json.loads(pathlib.Path(f"{self.log}.state").read_text())
        self.assertEqual(list(state["entries"]), ["h1"])
        self.assertIn("1 commit(s) on no remote", state["entries"]["h1"])
        self.assertIn("last commit 0 day(s) ago", state["entries"]["h1"])

    def test_the_ask_names_the_listing_by_hash_and_the_run_is_one_id(self):
        """`--subject` hashes the listed records; both calls share `JEV_RUN` (sd:2953)."""
        f = self.fixture()
        self.nightly(f)
        calls = self.calls()
        self.assertEqual(len(calls), 2, calls)
        found = re.search(r"--subject (\S+) ", calls[1])
        self.assertIsNotNone(found, calls[1])
        self.assertRegex(found.group(1), r"^repo-sync:[0-9a-f]{16}$")
        runs = {line.rsplit("run=", 1)[1] for line in calls}
        self.assertEqual(len(runs), 1, runs)
        self.assertRegex(runs.pop(), r"^repo-sync-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_each_listed_line_is_one_entry_in_report_order(self):
        f = self.fixture()
        repo = f.root / "a" / "proj"
        f.git(repo, "checkout", "-q", "-b", "sd-2094-pushed")
        f.commit(repo, "more", "y\n", "pushed then deleted upstream")
        f.git(repo, "push", "-q", "-u", "origin", "sd-2094-pushed")
        f.git(repo, "push", "-q", "origin", "--delete", "sd-2094-pushed")
        f.git(repo, "checkout", "-q", "main")
        out = self.nightly(f).stdout
        listed = [line.split()[0] for line in out.splitlines()
                  if re.match(r"^  (GONE|LOCAL|DONE|BEHIND|KEEP) ", line)]
        self.assertEqual(sorted(listed), ["GONE", "LOCAL"], out)
        state = json.loads(pathlib.Path(f"{self.log}.state").read_text())
        self.assertEqual(list(state["entries"]), ["h1", "h2"])
        kinds = ["upstream branch deleted" in text for text in state["entries"].values()]
        self.assertEqual(kinds, [kind == "GONE" for kind in listed])

    def test_nothing_private_leaves_the_machine(self):
        f = self.fixture()
        self.nightly(f)
        sent = pathlib.Path(f"{self.log}.state").read_text() + \
            pathlib.Path(f"{self.log}.questions").read_text()
        for private in ("sd-2094-private-topic", "private", self.sha, self.sha[:7],
                        "owner/proj", str(f.tmp), str(f.root)):
            self.assertNotIn(private, sent)

    def test_the_mailed_report_is_the_report_without_jev(self):
        f = self.fixture()
        self.nightly(f, JEV_STUB_EXIT="1")
        self.assertIn("LOCAL    branch sd-2094-private-topic", f.notify_log.read_text())
        self.assertNotIn("jev", f.notify_log.read_text().lower())

    def test_the_stage_switched_off_asks_nothing(self):
        f = self.fixture()
        self.nightly(f, JEV_REPO_SYNC_HYGIENE="0")
        self.assertEqual([c.split()[0] for c in self.calls()], ["enabled"])

    def test_a_clean_report_asks_nothing(self):
        f = self.fixture(listed=False)
        self.nightly(f)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
