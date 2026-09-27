"""Manual merge ownership follows repository identity across registered clones."""

import tempfile
import unittest
from pathlib import Path

from sd_db import runner, ship, workflow
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.workflow import WorkflowError
from sd_db.writes import create_assignment, create_item, upsert_repo

from . import support


class ManualMergeGuard(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        self.repo = str(self.root / "registered")
        upsert_repo(self.connection, self.repo, remote="https://github.com/fixture/repo.git")

    def assignment(self, repo, *, remote="git@github.com:Fixture/Repo.git", status="running", role="author"):
        upsert_repo(self.connection, repo, remote=remote)
        item = create_item(self.connection, kind="task", title="Other writer", repo=repo, status="in_progress")
        return create_assignment(self.connection, item=item, role=role, status=status)

    def checkout(self):
        checkout = support.repository(self.root / "checkout")
        support.git(checkout, "remote", "add", "origin", "git@github.com:Fixture/Repo.git")
        return checkout

    def claimed_run(self, *, remote="git@github.com:Fixture/Repo.git"):
        repo = str(self.root / "claimed")
        upsert_repo(self.connection, repo, remote=remote)
        item = create_item(self.connection, kind="task", title="Claimed writer", repo=repo,
                           branch="work/claimed", status="ready")
        assignment = create_assignment(self.connection, item=item, role="author", status="queued")
        claimed = runner.claim(self.connection, assignment, owner="fixture", work_root=self.root / "work",
                               retention_root=self.root / "retained")
        self.assertIsNotNone(claimed)
        return item, claimed["run"]

    def test_reassigned_item_cannot_hide_active_runner_ownership(self):
        item, run = self.claimed_run()
        unrelated = str(self.root / "unrelated")
        upsert_repo(self.connection, unrelated, remote="https://github.com/fixture/unrelated.git")
        with self.assertRaises(WorkflowError):
            ship.manual_merge_guard(self.connection, self.repo)
        workflow.edit_item(self.connection, item, {"repo": unrelated}, who="fixture")
        self.assertEqual(runner.run_state(self.connection, run["id"])["repo"], run["repo"])
        self.assertEqual(self.connection.execute("SELECT repo FROM runner_lease WHERE run=?", (run["id"],)).fetchone()[0],
                         run["repo"])
        with self.assertRaisesRegex(WorkflowError, "manual merge authority"):
            ship.manual_merge_guard(self.connection, self.repo)

    def test_cleared_item_cannot_hide_ending_runner_ownership(self):
        item, run = self.claimed_run()
        runner.begin_ending(self.connection, run["id"], outcome="blocked", detail="fixture ending")
        workflow.edit_item(self.connection, item, {"repo": None}, who="fixture")
        self.assertIsNone(workflow.item_state(self.connection, item)["item"]["repo"])
        self.assertIsNone(runner.run_state(self.connection, run["id"])["released_at"])
        self.assertIsNone(self.connection.execute("SELECT released_at FROM runner_lease WHERE run=?", (run["id"],)).fetchone()[0])
        with self.assertRaisesRegex(WorkflowError, "manual merge authority"):
            ship.manual_merge_guard(self.connection, self.repo)

    def test_unreleased_run_blocks_even_when_lease_and_assignment_look_released(self):
        item, run = self.claimed_run()
        workflow.edit_item(self.connection, item, {"repo": None}, who="fixture")
        # Simulate inconsistent recovery evidence. One release marker clears no other owner.
        self.connection.execute("UPDATE runner_lease SET released_at='fixture-release' WHERE run=?", (run["id"],))
        self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (run["assignment"],))
        with self.assertRaisesRegex(WorkflowError, f"unreleased runner run {run['id']}"):
            ship.manual_merge_guard(self.connection, self.repo)

    def test_unreleased_lease_blocks_even_when_run_and_assignment_look_released(self):
        item, run = self.claimed_run()
        workflow.edit_item(self.connection, item, {"repo": None}, who="fixture")
        self.connection.execute("UPDATE runner_run SET released_at='fixture-release' WHERE id=?", (run["id"],))
        self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (run["assignment"],))
        with self.assertRaisesRegex(WorkflowError, f"unreleased runner lease {run['id']}"):
            ship.manual_merge_guard(self.connection, self.repo)

    def test_released_durable_ownership_does_not_block(self):
        item, run = self.claimed_run()
        workflow.edit_item(self.connection, item, {"repo": None}, who="fixture")
        runner.begin_ending(self.connection, run["id"], outcome="blocked", detail="fixture complete")
        runner.update_run(self.connection, run["id"], end_step="retained")
        runner.release(self.connection, run["id"])
        self.assertIsNotNone(runner.run_state(self.connection, run["id"])["released_at"])
        self.assertIsNotNone(self.connection.execute("SELECT released_at FROM runner_lease WHERE run=?", (run["id"],)).fetchone()[0])
        ship.manual_merge_guard(self.connection, self.repo)

    def test_unrelated_durable_ownership_does_not_block(self):
        item, _ = self.claimed_run(remote="https://github.com/fixture/unrelated.git")
        workflow.edit_item(self.connection, item, {"repo": None}, who="fixture")
        ship.manual_merge_guard(self.connection, self.repo)

    def test_unknown_durable_repository_identity_fails_closed(self):
        item, run = self.claimed_run()
        workflow.edit_item(self.connection, item, {"repo": None}, who="fixture")
        self.connection.execute("UPDATE repo SET remote=NULL WHERE path=?", (run["repo"],))
        with self.assertRaisesRegex(WorkflowError, "unknown repository identity; manual merge authority is held"):
            ship.manual_merge_guard(self.connection, self.repo)

    def test_same_path_running_and_ending_assignments_remain_blocked(self):
        for status in ("running", "ending"):
            with self.subTest(status=status):
                assignment = self.assignment(self.repo, status=status)
                with self.assertRaisesRegex(WorkflowError, f"assignment {assignment} is {status}.*manual merge authority"):
                    ship.manual_merge_guard(self.connection, self.repo)
                self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))

    def test_same_remote_clones_block_all_existing_assignment_roles(self):
        for status in ("running", "ending"):
            for role in ("author", "merge", "reviewer"):
                with self.subTest(status=status, role=role):
                    assignment = self.assignment(str(self.root / "second"), status=status, role=role)
                    with self.assertRaisesRegex(WorkflowError, f"assignment {assignment} is {status}"):
                        ship.manual_merge_guard(self.connection, self.repo)
                    self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))

    def test_remote_protocol_case_and_git_suffix_share_one_identity(self):
        remotes = ("https://github.com/FIXTURE/REPO", "ssh://git@github.com/Fixture/Repo.git/",
                   "git@github.com:fixture/repo.git")
        for remote in remotes:
            with self.subTest(remote=remote):
                assignment = self.assignment(str(self.root / "second"), remote=remote)
                with self.assertRaisesRegex(WorkflowError, "repository fixture/repo"):
                    ship.manual_merge_guard(self.connection, self.repo)
                self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))

    def test_unrelated_remotes_do_not_block(self):
        self.assignment(str(self.root / "other-github"), remote="git@github.com:fixture/other.git")
        self.assignment(str(self.root / "other-host"), remote="git@gitlab.com:fixture/repo.git")
        self.assignment(str(self.root / "local"), remote=str(self.root / "bare.git"))
        ship.manual_merge_guard(self.connection, self.repo)

    def test_other_host_user_and_path_text_cannot_claim_github_ownership(self):
        remotes = (
            "https://gitlab.com/fixture/github.com-mirror.git",
            "https://github.com-bot@gitlab.com/fixture/repo.git",
            "git@gitlab.com:fixture/github.com-mirror.git",
            "github.com-bot@gitlab.com:fixture/repo.git",
            "ssh://github.com-bot@gitlab.com/fixture/github.com-mirror.git",
        )
        for remote in remotes:
            with self.subTest(remote=remote):
                assignment = self.assignment(str(self.root / "other-host"), remote=remote)
                ship.manual_merge_guard(self.connection, self.repo)
                self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))

    def test_ambiguous_github_hosts_still_hold_manual_authority(self):
        remotes = (
            "https://github.com/fixture", "git@github.com:fixture",
            "ssh://git@github.com/fixture", "https://gitlab.com@github.com/fixture",
            "gitlab.com@github.com:fixture", "https://[github.com]/fixture/repo",
            "https://github.com /fixture/repo", "https://github.com%2f.invalid/fixture/repo",
            "https://github.com.:443/fixture/repo", "https://gitlab.com:bad/fixture/github.com-mirror.git",
            "unknown://gitlab.com/fixture/github.com-mirror.git",
            "/local/github.com-mirror.git", "github.com-mirror.git",
        )
        for remote in remotes:
            with self.subTest(remote=remote):
                assignment = self.assignment(str(self.root / "unknown"), remote=remote)
                with self.assertRaisesRegex(WorkflowError, "unknown repository identity"):
                    ship.manual_merge_guard(self.connection, self.repo)
                self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))

    def test_queued_and_terminal_assignments_do_not_block(self):
        for status in ("queued", "done", "blocked"):
            self.assignment(str(self.root / status), status=status)
        ship.manual_merge_guard(self.connection, self.repo)

    def test_unregistered_worktree_checks_registered_clones_without_creating_rows(self):
        checkout = self.checkout()
        worktree = self.root / "worktree"
        support.git(checkout, "worktree", "add", "-qb", "topic", str(worktree))
        before = self.connection.total_changes
        ship.manual_merge_guard(self.connection, str(worktree), repository="fixture/repo")
        self.assertEqual(self.connection.total_changes, before)
        self.assertIsNone(self.connection.execute("SELECT 1 FROM repo WHERE path=?", (str(worktree),)).fetchone())
        assignment = self.assignment(self.repo, status="ending")
        with self.assertRaisesRegex(WorkflowError, f"assignment {assignment} is ending"):
            ship.manual_merge_guard(self.connection, str(worktree), repository="fixture/repo")

    def test_origin_transport_rewrite_does_not_change_repository_identity(self):
        checkout = self.checkout()
        support.git(checkout, "config", "url./fixture/transport/.insteadOf", "git@github.com:")
        self.assertEqual(support.git(checkout, "remote", "get-url", "origin"), "/fixture/transport/Fixture/Repo.git")
        ship.manual_merge_guard(self.connection, str(checkout), repository="FIXTURE/REPO")

    def test_unregistered_path_requires_explicit_identity_and_real_checkout(self):
        checkout = self.checkout()
        with self.assertRaisesRegex(WorkflowError, "explicit GitHub repository identity"):
            ship.manual_merge_guard(self.connection, str(checkout))
        with self.assertRaisesRegex(WorkflowError, "readable checkout root"):
            ship.manual_merge_guard(self.connection, str(self.root / "absent"), repository="fixture/repo")
        (checkout / "subdirectory").mkdir()
        with self.assertRaisesRegex(WorkflowError, "readable checkout root"):
            ship.manual_merge_guard(self.connection, str(checkout / "subdirectory"), repository="fixture/repo")

    def test_supplied_identity_cannot_override_registered_or_checkout_remote(self):
        for path in (self.repo, str(self.checkout())):
            with self.subTest(path=path):
                with self.assertRaisesRegex(WorkflowError, "identity does not match its remote"):
                    ship.manual_merge_guard(self.connection, path, repository="fixture/other")

    def test_missing_or_non_github_target_remote_refuses(self):
        for remote in (None, "", "git@gitlab.com:fixture/repo.git", "https://github.com/fixture"):
            with self.subTest(remote=remote):
                self.connection.execute("UPDATE repo SET remote=? WHERE path=?", (remote, self.repo))
                with self.assertRaisesRegex(WorkflowError, "no verified GitHub remote identity"):
                    ship.manual_merge_guard(self.connection, self.repo)
        checkout = self.checkout()
        support.git(checkout, "remote", "remove", "origin")
        with self.assertRaisesRegex(WorkflowError, "no verified GitHub remote identity"):
            ship.manual_merge_guard(self.connection, str(checkout), repository="fixture/repo")

    def test_unknown_active_repository_identity_fails_closed(self):
        for remote in (None, "", "https://github.com/fixture"):
            with self.subTest(remote=remote):
                assignment = self.assignment(str(self.root / "unknown"), remote=remote)
                with self.assertRaisesRegex(WorkflowError, f"assignment {assignment} has an unknown repository identity"):
                    ship.manual_merge_guard(self.connection, self.repo)
                self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))

    def test_repositoryless_assignments_keep_the_existing_scope(self):
        item = create_item(self.connection, kind="personal", title="No repository", status="in_progress")
        create_assignment(self.connection, item=item, role="author", status="running")
        create_assignment(self.connection, role="reviewer", status="running")
        ship.manual_merge_guard(self.connection, self.repo)

    def test_guard_works_on_a_read_only_connection_and_changes_nothing(self):
        before = self.connection.total_changes
        connection = connect(self.database, write=False)
        self.addCleanup(connection.close)
        ship.manual_merge_guard(connection, self.repo)
        self.assertEqual(self.connection.total_changes, before)
