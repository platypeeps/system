"""Stored JSON repository values and migration 014's keys (sd:1450).

014 rewrote the repository columns to the `~/` key and left JSON values as
written: a runner delivery proof in `state.body` and a publication journal
manifest name the absolute path when written before it. `paths.same_key`
matches such a value to the keyed column; a different repository still
differs, and without `$HOME` a legacy value fails to match.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect, create_item, paths, publication_journal, ship, upsert_repo
from sd_db.database import transaction
from sd_db.migrate import initialise
from sd_db.workflow import WorkflowError

KEY = "~/repos/x"


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd1450-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.repo = self.home / "repos" / "x"
        self.repo.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def others(self):
        """Values that name another repository, in every spelling."""
        return (str(self.home / "repos" / "y"), "~/repos/y", "/srv/repos/x", str(self.repo) + "x", "~/repos/xx")


class SameKey(HomeCase):
    def test_the_absolute_path_under_the_home_matches_its_key(self):
        self.assertTrue(paths.same_key(str(self.repo), KEY))
        self.assertTrue(paths.same_key(KEY, str(self.repo)))
        self.assertTrue(paths.same_key(KEY, KEY))
        self.assertTrue(paths.same_key("/srv/repos/x", "/srv/repos/x"))

    def test_another_repository_differs(self):
        for other in self.others():
            with self.subTest(other=other):
                self.assertFalse(paths.same_key(other, KEY))

    def test_without_home_a_legacy_value_fails_closed(self):
        with mock.patch.dict(os.environ, {"HOME": ""}):
            self.assertFalse(paths.same_key(str(self.repo), KEY))
            self.assertTrue(paths.same_key(KEY, KEY))

    def test_nothing_touches_the_disk(self):
        missing = self.home / "repos" / "gone"
        self.assertTrue(paths.same_key(str(missing), "~/repos/gone"))
        self.assertFalse(missing.exists())


class StoreCase(HomeCase):
    def setUp(self):
        super().setUp()
        self.path = Path(self.temp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, KEY, status_source="row")
        self.item = create_item(self.db, kind="work", title="Keyed work", repo=KEY)


class DeliveryProof(StoreCase):
    """`ship.finalize_delivery` binds a proof to its run by repository."""

    COMMIT = "c" * 40

    def finalize(self, proof_repo):
        proof = {"run": "r1", "assignment": 5, "item": self.item, "repo": proof_repo,
                 "evidence": {"commit": self.COMMIT}}
        receipt = self.db.execute(
            "INSERT INTO state(kind, key, timestamp, body, resolved_at) VALUES ('verified', ?, ?, ?, ?)",
            ("runner-delivery:r1", "2026-09-24T12:00:00+00:00", json.dumps(proof), "2026-09-24T12:00:01+00:00"),
        ).lastrowid
        # The run and assignment as a released merge run: its row, which 014
        # keyed. The proof is the value under test.
        run = {"assignment": 5, "repo": KEY, "end_step": "released", "released_at": "2026-09-24T12:00:00+00:00",
               "retained_path": "/retained", "quarantine": 0}
        assignment = {"id": 5, "item": self.item, "status": "done", "phase": "merged"}
        with mock.patch("sd_db.runner.run_state", return_value=run), \
                mock.patch("sd_db.runner.queue_state", return_value=assignment), transaction(self.db):
            return ship.finalize_delivery(self.db, "r1", {"run": "r1", "receipt": receipt, "commit": self.COMMIT})

    def test_a_pre_014_proof_finalizes_against_the_keyed_run(self):
        self.assertEqual(self.finalize(str(self.repo))["item"]["id"], self.item)

    def test_a_proof_for_another_repository_is_refused(self):
        for other in self.others():
            with self.subTest(other=other):
                self.db.execute("DELETE FROM state")
                with self.assertRaisesRegex(WorkflowError, "retained, successfully released merge run"):
                    self.finalize(other)


class PublicationJournal(StoreCase):
    """`publication_journal.for_piece` finds a manifest by repository and piece."""

    def journal(self, repo, claim="a" * 32, piece="essay"):
        publication_journal.create(self.db, claim, self.item, {"repo": repo, "piece": piece}, {"phase": "claimed"})

    def test_a_pre_014_manifest_is_found_for_the_keyed_row(self):
        self.journal(str(self.repo))
        found = publication_journal.for_piece(self.db, KEY, "essay")
        self.assertEqual([manifest["claim"] for manifest, _ in found], ["a" * 32])
        self.assertEqual(found[0][0]["payload"]["repo"], str(self.repo), "the journal is returned as written")

    def test_another_repository_or_piece_is_not_found(self):
        self.journal(str(self.repo))
        for other in self.others():
            with self.subTest(other=other):
                self.assertEqual(publication_journal.for_piece(self.db, other, "essay"), [])
        self.assertEqual(publication_journal.for_piece(self.db, KEY, "other"), [])


if __name__ == "__main__":
    unittest.main()
