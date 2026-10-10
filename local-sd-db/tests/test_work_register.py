"""`work register`, and the row it makes.

The row owns a `docs/work` item's status, so a folder with no row has no
status at all -- which `sd-status` reports as `status-unreadable`, and which
cost one item a hand-written `INSERT` on 2026-09-11. These cases pin the verb that
replaces that `INSERT`, at both levels: the function that writes the row, and
the entrypoint a person actually types.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import SdDbError, connect, upsert_repo
from sd_db.jobs import cli
from sd_db.repos import registered_for, remote_identity, same_remote
from sd_db.migrate import initialise
from sd_db.workflow import WorkflowError, register_work_item

from . import support
from .test_cli import CliCase


class RegisterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        path = self.root / "sd.db"
        initialise(path)
        self.db = connect(path)
        self.addCleanup(self.db.close)
        self.repo = "/Users/nobody/repos/system"
        upsert_repo(self.db, self.repo)

    def register(self, **overrides):
        values = {
            "repo": self.repo,
            "path": "docs/work/2026-09-11-a-folder/prd.md",
            "title": "A folder",
            "created_at": "2026-09-11",
            "who": "operator",
        }
        return register_work_item(self.db, **{**values, **overrides})


class TheRowItWrites(RegisterCase):
    def test_a_folder_becomes_one_planning_item_the_readers_can_key_on(self):
        state = self.register(branch="plan/2026-09-11-a-folder", source_commit="a" * 40)
        self.assertTrue(state["created"])
        row = state["item"]
        self.assertEqual(row["kind"], "work")
        self.assertEqual(row["status"], "planning")
        self.assertEqual(row["repo"], self.repo)
        self.assertEqual(row["path"], "docs/work/2026-09-11-a-folder/prd.md")
        self.assertEqual(row["source"], "docs/work")
        # The readers find the row by this identity and nothing else; the
        # hand-written repair of 2026-09-11 had to compose it by hand.
        self.assertEqual(
            row["external_id"],
            f"{self.repo}::docs/work/2026-09-11-a-folder/prd.md",
        )
        # The branch is the caller's to name and the row records it as given:
        # the column means "the branch to do the work on", which is the only
        # way `runner.py:_item`, `configure_item` and `sd_plan.py` read it.
        self.assertEqual(row["branch"], "plan/2026-09-11-a-folder")
        self.assertEqual(row["source_commit"], "a" * 40)
        # Not the clock: an item whose idle age starts at its registration is
        # one the first sweep offers as fresh work.
        self.assertTrue(str(row["created_at"]).startswith("2026-09-11"))

    def test_a_caller_that_cannot_name_a_branch_leaves_the_column_null(self):
        """NULL is what a task row starts with and what `runner.py:_item`
        refuses loudly; a value that only looks like a branch is neither."""
        row = self.register()["item"]
        self.assertIsNone(row["branch"])

    def test_registering_twice_reports_the_row_rather_than_making_a_second(self):
        first = self.register()
        again = self.register(title="A different title")
        self.assertFalse(again["created"])
        self.assertEqual(again["item"]["id"], first["item"]["id"])
        # The second call does not get to rename the item either -- it is a
        # report, not an edit.
        self.assertEqual(again["item"]["title"], "A folder")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM item").fetchone()[0], 1
        )


class WhatItRefuses(RegisterCase):
    def assertRefused(self, fragment, **overrides):
        with self.assertRaises(WorkflowError) as caught:
            self.register(**overrides)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0,
            "a refusal must leave no row behind",
        )

    def test_a_repository_nobody_registered(self):
        self.assertRefused("is not registered", repo="/Users/nobody/repos/other")

    def test_a_path_that_is_not_the_file_the_readers_key_on(self):
        self.assertRefused("docs/work/<item>/prd.md", path="docs/work/a-folder/design.md")
        self.assertRefused("docs/work/<item>/prd.md", path="docs/work/prd.md")
        self.assertRefused("docs/work/<item>/prd.md", path="notes/a-folder/prd.md")

    def test_a_nested_prd_is_not_the_file_the_readers_key_on(self):
        # The readers key on `docs/work/<item>/prd.md`. The old `< 4` filed this
        # row, and no reader ever saw it again.
        self.assertRefused("docs/work/<item>/prd.md", path="docs/work/a/nested/prd.md")

    def test_a_path_reaching_outside_the_repository(self):
        self.assertRefused("without `..`", path="/docs/work/a-folder/prd.md")
        self.assertRefused("without `..`", path="docs/work/../../../etc/prd.md")

    def test_a_date_no_age_can_be_measured_from(self):
        self.assertRefused("YYYY-MM-DD", created_at="11 September 2026")
        self.assertRefused("YYYY-MM-DD", created_at="2026-09-11T05:00:00Z")
        self.assertRefused("YYYY-MM-DD", created_at="2026-13-01")


class TheVerb(CliCase):
    """End to end, through `sd-db.sh`, standing in the checkout."""

    def setUp(self):
        super().setUp()
        self.repo = support.repository(self.home / "repos" / "system")
        self.sd_db("init")
        self.sd_db("repo", "add", str(self.repo))
        connection = connect(self.home / ".local/share/sd/sd.db")
        self.addCleanup(connection.close)
        upsert_repo(connection, str(self.repo.resolve()))
        connection.commit()

    def rows(self):
        connection = connect(self.home / ".local/share/sd/sd.db")
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM item WHERE kind = 'work' ORDER BY id"
            )]
        finally:
            connection.close()

    def test_it_reads_the_title_and_the_date_out_of_the_frontmatter(self):
        support.write_item(
            self.repo, "2026-09-11-a-folder",
            title="A folder worth a shape", created="2026-09-11",
        )
        done = self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        self.assertIn("registered #", done.stdout)
        row, = self.rows()
        self.assertEqual(row["title"], "A folder worth a shape")
        self.assertEqual(row["status"], "planning")
        self.assertTrue(str(row["created_at"]).startswith("2026-09-11"))
        # Uncommitted is the ordinary case, and the verb says so rather than
        # recording a commit that does not carry the file.
        self.assertIsNone(row["source_commit"])
        self.assertIn("not committed yet", done.stdout)

    def test_a_committed_folder_records_the_commit_that_carries_it(self):
        support.write_item(self.repo, "2026-09-11-a-folder")
        head = support.commit(self.repo, "docs: a folder")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        row, = self.rows()
        self.assertEqual(row["source_commit"], head)

    def test_a_committed_file_with_uncommitted_edits_records_no_commit(self):
        """`log -1` reads history only; the commit it names does not carry
        the edited contents being registered, so the row records none."""
        prd = support.write_item(self.repo, "2026-09-11-a-folder")
        support.commit(self.repo, "docs: a folder")
        prd.write_text(prd.read_text(encoding="utf-8") + "\nan edit\n", encoding="utf-8")
        done = self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        row, = self.rows()
        self.assertIsNone(row["source_commit"])
        self.assertIn("not committed yet", done.stdout)

    def test_a_prd_that_is_not_utf8_is_refused_in_a_sentence(self):
        prd = self.repo / "docs/work/2026-09-11-a-folder/prd.md"
        prd.parent.mkdir(parents=True)
        prd.write_bytes(b"---\ntitle: caf\xe9\ncreated: 2026-09-11\n---\n")
        done = self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md",
            cwd=self.repo, expect=1,
        )
        self.assertIn("sd-db work register:", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.rows(), [])

    def test_on_the_default_branch_the_row_records_no_branch(self):
        """`origin/main` is what this wrote until sd:462: a remote-tracking
        name that passes `runner.py:_item`'s shape check and names no head,
        so the row read as runnable and failed only inside the clone. On
        the default the honest answer is NULL, the state a task row starts
        in and `sd work register` fills."""
        support.git(self.repo, "remote", "add", "origin", str(self.home / "bare.git"))
        support.git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        support.git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/main")
        support.write_item(self.repo, "2026-09-11-a-folder")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        row, = self.rows()
        self.assertIsNone(row["branch"])

    def test_without_a_remote_main_is_still_the_default_and_not_a_branch_to_record(self):
        support.write_item(self.repo, "2026-09-11-a-folder")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        row, = self.rows()
        self.assertIsNone(row["branch"])

    def test_without_a_remote_master_is_the_default_too(self):
        support.git(self.repo, "checkout", "-q", "-b", "master")
        support.write_item(self.repo, "2026-09-11-a-folder")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        row, = self.rows()
        self.assertIsNone(row["branch"])

    def test_a_local_main_is_a_working_branch_when_the_default_is_dev(self):
        """sd:653, the library's copy of the pack's sd:639. The `{main, master}`
        pair belongs inside the no-remote guess. Every other case here has
        `main` as its default, so none can tell "not the default" from "not
        named main or master". This one reads `origin/HEAD` as `dev`, stands
        on a local `main` and expects `main` back; on `dev`, the control, it
        expects NULL."""
        support.git(self.repo, "remote", "add", "origin", str(self.home / "bare.git"))
        support.git(self.repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        support.git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/dev")
        self.assertEqual(
            support.git(self.repo, "symbolic-ref", "--short", "HEAD").strip(), "main")
        support.write_item(self.repo, "2026-09-11-on-main")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-on-main/prd.md", cwd=self.repo,
        )
        support.git(self.repo, "checkout", "-q", "-b", "dev")
        support.write_item(self.repo, "2026-09-11-on-dev")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-on-dev/prd.md", cwd=self.repo,
        )
        on_main, on_dev = self.rows()
        self.assertEqual(on_main["branch"], "main")
        self.assertIsNone(on_dev["branch"])

    def test_on_a_branch_of_its_own_the_row_records_that_branch(self):
        """A runner clone on `plan/<slug>` is where `sd-plan` registers the
        folder it just wrote, and that branch is the one the clone's pre-push
        hook accepts: the honest working branch, and a local head."""
        support.git(self.repo, "checkout", "-q", "-b", "plan/2026-09-11-a-folder")
        support.write_item(self.repo, "2026-09-11-a-folder")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        row, = self.rows()
        self.assertEqual(row["branch"], "plan/2026-09-11-a-folder")

    def test_a_detached_checkout_names_no_branch(self):
        support.git(self.repo, "checkout", "-q", "--detach")
        support.write_item(self.repo, "2026-09-11-a-folder")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        row, = self.rows()
        self.assertIsNone(row["branch"])

    def test_a_worktree_cloned_over_ssh_registers_against_an_https_row(self):
        """sd:1436: the worktree's path is not a row, so its origin decides.

        The row records the https spelling and the checkout the ssh one; both
        name one repository, and the verb refused until they compared equal.
        """
        support.git(self.repo, "remote", "add", "origin",
                    "git@github.com:platypeeps/system.git")
        connection = connect(self.home / ".local/share/sd/sd.db")
        self.addCleanup(connection.close)
        upsert_repo(connection, str(self.repo.resolve()),
                    remote="https://github.com/platypeeps/system")
        connection.commit()
        worktree = self.home / "worktrees" / "agent-1"
        support.git(self.repo, "worktree", "add", "-q", "-b", "plan/a-folder", str(worktree))
        support.write_item(worktree, "2026-09-11-a-folder")
        done = self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=worktree,
        )
        self.assertIn("registered #", done.stdout)
        row, = self.rows()
        registered = [r["path"] for r in connection.execute("SELECT path FROM repo")]
        self.assertIn(row["repo"], registered)
        self.assertNotIn("worktrees", row["repo"])
        self.assertEqual(row["branch"], "plan/a-folder")

    def test_a_second_run_reports_the_row_and_adds_nothing(self):
        support.write_item(self.repo, "2026-09-11-a-folder")
        self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        again = self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md", cwd=self.repo,
        )
        self.assertIn("already registered as #", again.stdout)
        self.assertEqual(len(self.rows()), 1)

    def test_a_path_the_working_directory_does_not_contain_is_refused(self):
        outside = support.repository(self.home / "repos" / "elsewhere")
        support.write_item(outside, "2026-09-11-theirs")
        done = self.sd_db(
            "work", "register", "../elsewhere/docs/work/2026-09-11-theirs/prd.md",
            cwd=self.repo, expect=1,
        )
        # R10-D6: the repository is the one enclosing the working directory,
        # and a path that leaves it would give a row a file nobody standing
        # here can read.
        self.assertIn("is outside", done.stderr)
        self.assertEqual(self.rows(), [])

    def test_a_missing_file_says_so_instead_of_inventing_a_row(self):
        done = self.sd_db(
            "work", "register", "docs/work/2026-09-11-nothing/prd.md",
            cwd=self.repo, expect=1,
        )
        self.assertIn("no file at", done.stderr)
        self.assertEqual(self.rows(), [])

    def test_frontmatter_without_a_date_is_refused_by_name(self):
        path = support.write_item(self.repo, "2026-09-11-a-folder")
        path.write_text("---\ntitle: A folder\n---\n\n# PRD\n", encoding="utf-8")
        done = self.sd_db(
            "work", "register", "docs/work/2026-09-11-a-folder/prd.md",
            cwd=self.repo, expect=1,
        )
        self.assertIn("`title:` and `created:`", done.stderr)
        self.assertEqual(self.rows(), [])

    def test_the_usage_line_names_the_shape_it_wants(self):
        done = self.sd_db("work", cwd=self.repo, expect=1)
        self.assertIn("register <docs/work/<item>/prd.md>", done.stderr)

    def test_help_describes_the_verb_and_the_argument_it_takes(self):
        done = self.sd_db("help")
        self.assertIn("work register PATH", done.stderr)


if __name__ == "__main__":
    unittest.main()


class WhichRepositoryACloneSpeaksFor(RegisterCase):
    """R10-D6 selects a repository by the working directory, not by its path.

    The runner clones a repository from its remote into a work path under
    `/Volumes/sd-work/worktrees/...` and runs there. Resolving by path alone
    refuses every run it makes -- and does so only after the command has been
    queued, dispatched and cloned, which is how it was found.
    """

    def setUp(self):
        super().setUp()
        upsert_repo(self.db, self.repo, remote="git@github.com:platypeeps/system.git")

    def test_a_registered_path_answers_for_itself(self):
        self.assertEqual(registered_for(self.db, self.repo, None), self.repo)

    def test_a_clone_resolves_to_the_repository_it_was_cloned_from(self):
        self.assertEqual(
            registered_for(self.db, "/Volumes/sd-work/worktrees/442/5-1-abc",
                           "git@github.com:platypeeps/system.git"),
            self.repo,
        )

    def test_a_clone_of_something_else_resolves_to_itself_and_is_refused_there(self):
        """Resolving to itself is what makes the caller's own refusal the message."""
        elsewhere = "/Volumes/sd-work/worktrees/9/1-def"
        self.assertEqual(
            registered_for(self.db, elsewhere, "git@github.com:platypeeps/other.git"),
            elsewhere,
        )

    def test_a_checkout_with_no_origin_resolves_to_itself(self):
        self.assertEqual(registered_for(self.db, "/tmp/loose", None), "/tmp/loose")

    def test_a_registered_path_wins_over_a_remote_match(self):
        """An ordinary checkout costs one indexed lookup and never scans."""
        upsert_repo(self.db, "/Users/nobody/repos/clone",
                    remote="git@github.com:platypeeps/system.git")
        self.assertEqual(
            registered_for(self.db, "/Users/nobody/repos/clone",
                           "git@github.com:platypeeps/system.git"),
            "/Users/nobody/repos/clone",
        )

    def test_an_origin_two_registered_paths_share_resolves_to_itself(self):
        """sd:1219 (941f61d80ffc, a09df0750b08): the first path in path order was a guess."""
        upsert_repo(self.db, "/srv/example.test/repos/clone",
                    remote="git@github.com:platypeeps/system.git")
        clone = "/Volumes/sd-work/worktrees/442/5-1-abc"
        self.assertEqual(registered_for(self.db, clone, "git@github.com:platypeeps/system.git"), clone)


class WhatCountsAsOneRemote(unittest.TestCase):
    def test_the_git_suffix_and_a_trailing_slash_are_not_differences(self):
        self.assertTrue(same_remote("git@github.com:platypeeps/system.git",
                                    "git@github.com:platypeeps/system"))
        self.assertTrue(same_remote("https://example.invalid/a/b/",
                                    "https://example.invalid/a/b"))

    def test_a_different_repository_is_a_difference(self):
        self.assertFalse(same_remote("git@github.com:platypeeps/system.git",
                                     "git@github.com:platypeeps/other.git"))

    def test_absent_remotes_are_never_equal(self):
        """Two repositories with no remote are not thereby the same repository."""
        self.assertFalse(same_remote(None, None))
        self.assertFalse(same_remote("", ""))
        self.assertFalse(same_remote(None, "git@github.com:platypeeps/system.git"))

    def test_an_ssh_and_an_https_url_for_one_repository_match(self):
        """sd:1436: a worktree cloned over ssh against an https row was refused."""
        self.assertTrue(same_remote("git@github.com:platypeeps/system.git",
                                    "https://github.com/platypeeps/system"))
        self.assertTrue(same_remote("https://github.com/platypeeps/system/",
                                    "git@github.com:platypeeps/system"))

    def test_the_ssh_url_form_matches_the_scp_and_https_forms(self):
        self.assertTrue(same_remote("ssh://git@github.com/platypeeps/system.git",
                                    "git@github.com:platypeeps/system.git"))
        self.assertTrue(same_remote("ssh://git@github.com:22/platypeeps/system",
                                    "https://github.com/platypeeps/system.git"))

    def test_the_host_is_compared_without_case(self):
        self.assertTrue(same_remote("https://GitHub.com/platypeeps/system",
                                    "git@github.com:platypeeps/system.git"))

    def test_a_different_repository_is_refused_across_url_forms(self):
        """Normalising the spelling must not merge two repositories."""
        self.assertFalse(same_remote("git@github.com:platypeeps/system.git",
                                     "https://github.com/platypeeps/other"))
        self.assertFalse(same_remote("ssh://git@github.com/platypeeps/system",
                                     "https://github.com/otherowner/system"))
        self.assertFalse(same_remote("git@github.com:platypeeps/system.git",
                                     "https://gitlab.com/platypeeps/system"))

    def test_the_identity_is_host_owner_and_name(self):
        """One normaliser: delivery verification and ship read this too."""
        for spelling in ("git@github.com:Platypeeps/System.git",
                         "ssh://git@github.com:22/platypeeps/system/",
                         "https://user@GITHUB.com/platypeeps/system.git"):
            self.assertEqual(remote_identity(spelling), "github.com/platypeeps/system")
        self.assertEqual(remote_identity(None), "")

    def test_another_host_is_compared_as_written_but_for_host_case_and_suffix(self):
        """Only github.com is known to serve one repository at every spelling."""
        self.assertTrue(same_remote("https://Git.Example.com/Team/Repo.git",
                                    "https://git.example.com/Team/Repo/"))
        self.assertFalse(same_remote("https://git.example.com/Team/Repo",
                                     "https://git.example.com/team/repo"))
        self.assertFalse(same_remote("https://git.example.com/team/repo",
                                     "git@git.example.com:team/repo"))

    def test_another_hosts_port_and_ssh_user_are_part_of_its_identity(self):
        """sd:1436 review: two ports can be two servers, two users two homes."""
        self.assertFalse(same_remote("ssh://git@host.example:2222/srv/repo.git",
                                     "ssh://git@host.example:2223/srv/repo.git"))
        self.assertFalse(same_remote("alice@host.example:repo.git",
                                     "bob@host.example:repo.git"))
        self.assertTrue(same_remote("alice@Host.Example:repo.git",
                                    "alice@host.example:repo"))

    def test_a_local_path_remote_still_compares_as_a_path(self):
        """Test fixtures clone from a bare directory; that is not a host."""
        self.assertTrue(same_remote("/tmp/x/bare.git", "/tmp/x/bare"))
        self.assertFalse(same_remote("/tmp/x/bare.git", "/tmp/y/bare.git"))


class ResolvingAnSshWorktreeAgainstAnHttpsRow(RegisterCase):
    def test_a_worktree_with_an_ssh_origin_resolves_to_the_https_row(self):
        """The exact refusal sd:1436 records, at the resolver `work register` uses."""
        upsert_repo(self.db, self.repo,
                    remote="https://github.com/platypeeps/system")
        worktree = f"{self.repo}/.claude/worktrees/agent-1"
        self.assertEqual(
            registered_for(self.db, worktree, "git@github.com:platypeeps/system.git"),
            self.repo,
        )



class ReadingTheCommitThatCarriesTheFile(unittest.TestCase):
    """`_last_commit` against a stubbed git: a failed read is not "uncommitted"."""

    def last_commit(self, answers):
        calls = []

        def git(_root, *args):
            calls.append(args[0])
            return answers[args[0]]

        with mock.patch.object(cli.docs_work, "_git", side_effect=git):
            return cli._last_commit(Path("/nowhere"), "docs/work/x/prd.md"), calls

    def test_a_clean_tracked_file_records_its_commit(self):
        commit, _ = self.last_commit({"status": (0, "", ""), "log": (0, "a" * 40 + "\n", "")})
        self.assertEqual(commit, "a" * 40)

    def test_an_edited_file_records_none_without_reading_history(self):
        commit, calls = self.last_commit({"status": (0, " M docs/work/x/prd.md\n", "")})
        self.assertIsNone(commit)
        self.assertEqual(calls, ["status"])

    def test_a_failed_history_read_refuses_rather_than_recording_null(self):
        with self.assertRaisesRegex(SdDbError, "bad object"):
            self.last_commit({"status": (0, "", ""), "log": (128, "", "fatal: bad object HEAD")})

    def test_a_failed_status_read_refuses_rather_than_recording_null(self):
        with self.assertRaisesRegex(SdDbError, "index file corrupt"):
            self.last_commit({"status": (128, "", "fatal: index file corrupt")})
