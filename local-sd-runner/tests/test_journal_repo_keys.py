"""A journal written before migration 014 names its repository absolute (sd:1447).

Migration 014 (sd:1439) rewrote `runner_run.repo` to the `~/` key and left the
runner journal alone, as history. A record that still says `$HOME/repos/x`
while its row says `~/repos/x` is the same run, not a conflict. On the live
store that difference held every run: `restore_holds` reported a same-version
conflict, and `persist` refused the next write as a changed identity, so the
runner could not start and could not heal itself.

These tests build that store in a temporary home. The control keeps the
identity check whole: a record naming a different repository still holds.
"""

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.writes import create_assignment, create_item, upsert_repo
from sd_runner import reconciliation
from sd_runner.runtime import Config, Runner

STAMP = "2026-09-24T12:00:00+00:00"


def write_raw(database: Path, record: dict) -> Path:
    """A journal file with a valid digest, as a pre-014 runner wrote it."""
    root = journal.directory(database)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{record['id']}.json"
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    envelope = {"record": record, "sha256": hashlib.sha256(body).hexdigest()}
    target.write_bytes(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    return target


class JournalRepoKeys(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd1447-", suffix=".noindex")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        (self.home / "repos" / "x").mkdir(parents=True)
        (self.home / "repos" / "other").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.database = self.root / "db" / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        (self.root / "retained").mkdir()
        self.config = Config(self.database, self.root / "work", self.root / "retained", self.root / "pack",
                             self.home, floor_gb=.001)
        upsert_repo(self.db, "~/repos/x")
        item = create_item(self.db, kind="task", title="sd:1447 fixture", status="ready", repo="~/repos/x")
        work = create_assignment(self.db, role="author", status="running", item=item)
        self.ident = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.ident, work, 1, "~/repos/x", "sd/1447", "runner", str(self.root / "work" / f"{work}-1-{self.ident}"),
             str(self.root / "retained" / str(work) / "1" / "clone"), STAMP, STAMP))
        self.row = store.run_state(self.db, self.ident)
        self.assertEqual(self.row["repo"], "~/repos/x")

    def journal_with(self, repo: str) -> Path:
        return write_raw(self.database, {**self.row, "repo": repo})

    def holds(self):
        runner = Runner(self.config, freezer=lambda path: None, observer=lambda run: [])
        return runner.restore_holds(self.db)

    # -- the live incident ---------------------------------------------------

    def test_absolute_journal_for_a_keyed_row_is_not_a_hold(self):
        self.journal_with(str(self.home / "repos" / "x"))
        self.assertEqual(self.holds(), [])

    def test_absolute_journal_for_a_keyed_row_is_not_a_reconciliation_entry(self):
        self.journal_with(str(self.home / "repos" / "x"))
        self.assertEqual(reconciliation.plan(self.config)["entries"], [])

    def test_the_next_write_heals_the_record_to_the_key(self):
        path = self.journal_with(str(self.home / "repos" / "x"))
        self.db.execute("UPDATE runner_run SET journal_version=journal_version+1, detail='next' WHERE id=?",
                        (self.ident,))
        journal.persist(self.database, store.run_state(self.db, self.ident))
        self.assertEqual(json.loads(path.read_text())["record"]["repo"], "~/repos/x")
        self.assertEqual(self.holds(), [])

    # -- the identity check stays whole --------------------------------------

    def test_a_different_repository_still_holds(self):
        self.journal_with(str(self.home / "repos" / "other"))
        reasons = [hold["reason"] for hold in self.holds()]
        self.assertEqual(reasons, ["same-version database and ownership journal conflict"])
        self.assertTrue(reconciliation.plan(self.config)["entries"][0]["blocked"])

    def test_a_different_repository_is_still_a_changed_identity(self):
        self.journal_with(str(self.home / "repos" / "other"))
        self.db.execute("UPDATE runner_run SET journal_version=journal_version+1 WHERE id=?", (self.ident,))
        with self.assertRaisesRegex(store.RunnerRefused, "identity changed: repo"):
            journal.persist(self.database, store.run_state(self.db, self.ident))

    # -- a row `repo remove` detached (sd:2581) -------------------------------

    def detach(self, *, released: bool = True) -> None:
        """Release the run (or not), write its journal from the row, then null the row's repo as the remove does."""
        if released:
            self.db.execute("UPDATE runner_run SET released_at=? WHERE id=?", (STAMP, self.ident))
        write_raw(self.database, store.run_state(self.db, self.ident))
        self.db.execute("UPDATE runner_run SET repo=NULL WHERE id=?", (self.ident,))

    def test_a_detached_row_and_the_journal_that_names_its_repository_are_not_a_hold(self):
        self.detach()
        self.assertEqual(self.holds(), [])
        self.assertEqual(reconciliation.plan(self.config)["entries"], [])

    def test_an_open_row_with_no_repository_still_holds(self):
        self.detach(released=False)
        self.assertIn("same-version database and ownership journal conflict", [hold["reason"] for hold in self.holds()])

    def test_a_record_with_a_bad_digest_is_refused_and_left_untouched(self):
        path = self.journal_with(str(self.home / "repos" / "x"))
        envelope = json.loads(path.read_text())
        envelope["sha256"] = "0" * 64
        path.write_text(json.dumps(envelope))
        before = path.read_bytes()
        with self.assertRaisesRegex(store.RunnerRefused, "checksum mismatch"):
            self.holds()
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
