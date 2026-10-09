"""Ownership and delivery read one remote identity contract (sd:1025).

Each row is one remote spelling and the outcome both consumers must reach:
`github` with the repository it names, `other`, or `unknown`. Ownership is
`ship.manual_merge_guard` for a registered `fixture/repo`; delivery is the
verification clone's origin check in `progress._delivery_evidence` and the
receipt repository `ship._github_repository` reads.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import progress, ship
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.workflow import WorkflowError
from sd_db.writes import create_assignment, create_item, upsert_repo

REGISTERED = "https://github.com/fixture/repo.git"

#: (case, remote, outcome, repository for `github`)
FORMS = (
    ("https", "https://github.com/Fixture/Repo.git", "github", "fixture/repo"),
    ("https, another repository", "https://github.com/fixture/other/", "github", "fixture/other"),
    ("https, host in capitals", "https://GitHub.com/fixture/repo", "github", "fixture/repo"),
    ("scp-like", "git@github.com:fixture/repo.git", "github", "fixture/repo"),
    ("scp-like, another user", "deploy@github.com:fixture/repo", "github", "fixture/repo"),
    ("ssh user and port", "ssh://deploy@github.com:22/fixture/repo.git", "github", "fixture/repo"),
    ("known other host, https", "https://git.example.test/fixture/repo.git", "other", None),
    ("known other host, scp-like", "git@git.example.test:fixture/repo.git", "other", None),
    ("known other host, github.com in the user", "github.com-bot@git.example.test:fixture/repo", "other", None),
    ("known other host, github.com in the path", "ssh://git@git.example.test/fixture/github.com-repo", "other", None),
    ("missing", None, "unknown", None),
    ("empty", "", "unknown", None),
    ("ambiguous, owner only", "https://github.com/fixture", "unknown", None),
    ("ambiguous, deeper path", "https://github.com/fixture/repo/pull/1", "unknown", None),
    ("ambiguous, GitHub-looking host", "ssh://git@ssh.github.com:443/fixture/repo", "unknown", None),
    ("ambiguous, look-alike host", "https://github.com.example.test/fixture/repo", "unknown", None),
    ("ambiguous, unread transport", "git://github.com/fixture/repo", "unknown", None),
    ("ambiguous, fragment before the user", "https://git.example.test#@github.com/fixture/repo", "unknown", None),
    ("ambiguous, scp host before a GitHub path", "user:token@github.com/fixture/repo", "unknown", None),
    ("ambiguous, scheme glued to scp", "ssh://git@github.com:fixture/repo", "unknown", None),
    ("explicit local path", "/srv/git/repo.git", "other", None),
    ("explicit file URL", "file:///srv/git/repo.git", "other", None),
    ("local path naming github.com", "/srv/mirror/github.com/fixture/repo.git", "unknown", None),
    ("relative path naming github.com", "github.com/fixture/repo", "unknown", None),
)


class OwnershipReadsTheContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        initialise(self.root / "sd.db")
        self.connection = connect(self.root / "sd.db")
        self.addCleanup(self.connection.close)
        self.repo = str(self.root / "registered")
        upsert_repo(self.connection, self.repo, remote=REGISTERED)

    def test_each_form_holds_skips_or_compares(self):
        for index, (case, remote, outcome, repository) in enumerate(FORMS):
            with self.subTest(case, remote=remote):
                clone = str(self.root / f"clone-{index}")
                upsert_repo(self.connection, clone, remote=remote)
                item = create_item(self.connection, kind="task", title="Writer", repo=clone, status="in_progress")
                assignment = create_assignment(self.connection, item=item, role="author", status="running")
                try:
                    if outcome == "unknown":
                        with self.assertRaisesRegex(WorkflowError, "unknown repository identity"):
                            ship.manual_merge_guard(self.connection, self.repo)
                    elif repository == "fixture/repo":
                        with self.assertRaisesRegex(WorkflowError, "in repository fixture/repo"):
                            ship.manual_merge_guard(self.connection, self.repo)
                    else:
                        ship.manual_merge_guard(self.connection, self.repo)
                finally:
                    self.connection.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))

    def test_each_form_as_the_target_is_verified_only_on_github(self):
        for case, remote, outcome, repository in FORMS:
            with self.subTest(case, remote=remote):
                self.connection.execute("UPDATE repo SET remote=? WHERE path=?", (remote, self.repo))
                if outcome == "github":
                    ship.manual_merge_guard(self.connection, self.repo, repository=repository)
                else:
                    with self.assertRaisesRegex(WorkflowError, "no verified GitHub remote identity"):
                        ship.manual_merge_guard(self.connection, self.repo)


class DeliveryReadsTheContract(unittest.TestCase):
    def origin_check(self, origin, registered):
        """`_delivery_evidence` up to its origin check; the next git call ends the run."""
        passed = WorkflowError("origin check passed")

        def git(root, *args, input=None):
            if args == ("remote", "get-url", "origin"):
                return origin or ""
            raise passed

        with tempfile.TemporaryDirectory() as clone, patch.object(progress, "_git", git):
            try:
                progress._delivery_evidence({"id": 1, "repo": clone}, "0" * 40, verification_root=Path(clone),
                                            registered_remote=registered)
            except WorkflowError as error:
                if error is passed:
                    return True
                self.assertIn("origin does not match", str(error))
                return False
        self.fail("delivery evidence returned without reading the remote")

    def test_the_clone_origin_matches_the_registered_github_repository(self):
        for case, remote, _outcome, repository in FORMS:
            with self.subTest(case, remote=remote):
                self.assertEqual(self.origin_check(remote, REGISTERED), repository == "fixture/repo")

    def test_a_form_matches_itself_unless_its_identity_is_unknown(self):
        for case, remote, outcome, _repository in FORMS:
            with self.subTest(case, remote=remote):
                self.assertEqual(self.origin_check(remote, remote), outcome != "unknown")

    def test_the_receipt_repository_is_read_only_from_github(self):
        for case, remote, outcome, repository in FORMS:
            with self.subTest(case, remote=remote):
                if outcome == "github":
                    self.assertEqual(ship._github_repository(remote), repository)
                else:
                    with self.assertRaisesRegex(WorkflowError, "requires the registered GitHub remote"):
                        ship._github_repository(remote)


if __name__ == "__main__":
    unittest.main()
