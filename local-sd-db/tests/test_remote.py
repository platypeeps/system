"""The fixture remote: a real repository, and the answers about it."""

import tempfile
import unittest
from pathlib import Path

from sd_db.testing import FixtureRemote, RemoteRefusal


class RemoteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.remote = FixtureRemote(Path(self.tmp.name))


class TheBareRepository(RemoteCase):
    def test_the_default_branch_exists(self):
        self.assertEqual(self.remote.branches(), ["main"])

    def test_a_commit_moves_the_branch(self):
        before = self.remote.rev_parse("main")
        after = self.remote.commit_on("main", "second")
        self.assertNotEqual(before, after)
        self.assertEqual(self.remote.rev_parse("main"), after)

    def test_behind_by_counts_what_the_branch_is_missing(self):
        self.remote.commit_on("feature", "work")
        self.assertEqual(self.remote.behind_by("main", "feature"), 0)
        self.remote.commit_on("main", "moved on")
        self.assertEqual(self.remote.behind_by("main", "feature"), 1)


class TheMergeRefusal(RemoteCase):
    """The behaviour criterion 23 names: a head that moved is refused."""

    def setUp(self):
        super().setUp()
        self.head = self.remote.commit_on("feature", "work")
        self.pull = self.remote.open_pull_request("feature", title="Work")

    def test_a_matching_sha_merges(self):
        merged = self.remote.merge(self.pull.number, sha=self.head)
        self.assertEqual(self.remote.pull(self.pull.number).state, "MERGED")
        self.assertEqual(self.remote.rev_parse("main"), merged)

    def test_no_sha_merges(self):
        self.remote.merge(self.pull.number)
        self.assertEqual(self.remote.pull(self.pull.number).state, "MERGED")

    def test_a_stale_sha_is_refused_and_the_branch_does_not_move(self):
        stale = self.head
        moved = self.remote.commit_on("feature", "one more")
        self.assertNotEqual(stale, moved)
        base_before = self.remote.rev_parse("main")

        with self.assertRaises(RemoteRefusal) as raised:
            self.remote.merge(self.pull.number, sha=stale)

        self.assertEqual(raised.exception.status, 405)
        self.assertIn("Head branch was modified", raised.exception.message)
        self.assertEqual(self.remote.pull(self.pull.number).state, "OPEN")
        self.assertEqual(self.remote.rev_parse("main"), base_before)

    def test_a_merged_pull_request_is_refused_a_second_time(self):
        self.remote.merge(self.pull.number)
        with self.assertRaises(RemoteRefusal) as raised:
            self.remote.merge(self.pull.number)
        self.assertEqual(raised.exception.status, 405)

    def test_an_unknown_pull_request_is_a_404(self):
        with self.assertRaises(RemoteRefusal) as raised:
            self.remote.pull(99)
        self.assertEqual(raised.exception.status, 404)


class TheCollaborators(RemoteCase):
    def setUp(self):
        super().setUp()
        self.remote.collaborators = [
            {"login": "alex", "permissions": {"admin": True, "push": True}},
            {"login": "bot", "permissions": {"admin": False, "push": True}},
            {"login": "reader", "permissions": {"admin": False, "push": False}},
        ]

    def test_admin_is_read_from_the_table(self):
        self.assertTrue(self.remote.can_administer("alex"))
        self.assertFalse(self.remote.can_administer("bot"))
        self.assertFalse(self.remote.can_administer("nobody"))

    def test_other_pushers_excludes_the_asker_and_the_read_only(self):
        self.assertEqual(self.remote.other_pushers("alex"), ["bot"])


class TheRecord(RemoteCase):
    def test_calls_arrive_in_order_with_the_door(self):
        self.remote.record("http", "GET", "/repos/fixture/repo")
        self.remote.record("gh", "PUT", "/repos/fixture/repo/pulls/1/merge", {"sha": "x"}, 405)
        self.assertEqual([call.door for call in self.remote.calls], ["http", "gh"])
        self.assertEqual(self.remote.calls[1].status, 405)


if __name__ == "__main__":
    unittest.main()
