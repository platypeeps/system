"""The runner journal and migration 014's repository keys (sd:1447).

014 rewrote `runner_run.repo` to the `~/` key and left the journal as
history. `runner_journal.canonical` gives both sides of a comparison the
row's spelling, so `persist` and the backup check agree with the row, while a
record for another repository still differs. `read` returns the file as it is.
"""

import hashlib
import importlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from sd_db import runner_journal
from sd_db.database import connect
from sd_db.errors import BackupError
from sd_db.migrate import initialise
from sd_db.writes import create_assignment, create_item, upsert_repo

STAMP = "2026-09-24T12:00:00+00:00"
#: `sd_db.backup` names the `backup` function the package re-exports.
backup = importlib.import_module("sd_db.backup")


def write_raw(database: Path, record: dict) -> Path:
    """A journal file with a valid digest, as a pre-014 runner wrote it."""
    root = runner_journal.directory(database)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{record['id']}.json"
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    envelope = {"record": record, "sha256": hashlib.sha256(body).hexdigest()}
    target.write_bytes(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    return target


class Canonical(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd1447-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    # Each record below carries `detached_from`, as one written after migration 018 does.

    def test_an_absolute_path_under_the_home_becomes_the_key(self):
        record = {"id": "a", "repo": str(self.home / "repos" / "x"), "detached_from": None}
        self.assertEqual(runner_journal.canonical(record), {"id": "a", "repo": "~/repos/x", "detached_from": None})
        self.assertEqual(record["repo"], str(self.home / "repos" / "x"), "the argument is not changed")

    def test_a_key_and_a_path_outside_the_home_are_kept(self):
        for repo in ("~/repos/x", "/srv/repos/x", "checkout"):
            record = {"id": "a", "repo": repo, "detached_from": None}
            self.assertIs(runner_journal.canonical(record), record)

    def test_no_home_keeps_the_record_as_it_is(self):
        record = {"id": "a", "repo": str(self.home / "repos" / "x"), "detached_from": None}
        with mock.patch.dict(os.environ, {"HOME": ""}):
            self.assertIs(runner_journal.canonical(record), record)

    def test_a_record_written_before_migration_018_reads_detached_from_as_null(self):
        """sd:2581: 018 added the column, so every older journal lacks the field and still matches its row."""
        record = {"id": "a", "repo": "~/repos/x"}
        self.assertEqual(runner_journal.canonical(record), {**record, "detached_from": None})
        self.assertNotIn("detached_from", record, "the argument is not changed")


class BackupCheck(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd1447-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        (self.home / "repos" / "x").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.database = self.root / "db" / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, "~/repos/x")
        item = create_item(self.db, kind="task", title="sd:1447 fixture", status="ready", repo="~/repos/x")
        work = create_assignment(self.db, role="author", status="running", item=item)
        self.ident = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.ident, work, 1, "~/repos/x", "sd/1447", "runner", str(self.root / "work"),
             str(self.root / "retained"), STAMP, STAMP))
        self.db.row_factory = sqlite3.Row
        self.row = dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (self.ident,)).fetchone())

    def test_an_absolute_record_for_a_keyed_row_passes(self):
        write_raw(self.database, {**self.row, "repo": str(self.home / "repos" / "x")})
        backup._check_runner_records(self.db, self.database.parent)

    def test_a_record_for_another_repository_still_fails(self):
        write_raw(self.database, {**self.row, "repo": str(self.home / "repos" / "other")})
        with self.assertRaisesRegex(BackupError, "differs from the backup journal"):
            backup._check_runner_records(self.db, self.database.parent)

    def test_read_returns_the_record_as_written(self):
        absolute = {**self.row, "repo": str(self.home / "repos" / "x")}
        path = write_raw(self.database, absolute)
        self.assertEqual(runner_journal.read(path), absolute)

    def test_persist_takes_the_row_over_an_absolute_record_and_writes_the_key(self):
        path = write_raw(self.database, {**self.row, "repo": str(self.home / "repos" / "x")})
        runner_journal.persist(self.database, self.row)
        self.assertEqual(json.loads(path.read_text())["record"], self.row)

    def released_journal(self) -> None:
        """Release the run and write its journal from the row, as the runner's release does."""
        self.db.execute("UPDATE runner_run SET released_at=? WHERE id=?", (STAMP, self.ident))
        write_raw(self.database, dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (self.ident,)).fetchone()))

    def test_a_detached_row_passes_against_the_journal_that_names_its_repository(self):
        """sd:2581: the removal moves the repo to `detached_from` in the row only; the check reads one run."""
        self.released_journal()
        self.db.execute("UPDATE runner_run SET repo=NULL, detached_from=repo WHERE id=?", (self.ident,))
        backup._check_runner_records(self.db, self.database.parent)

    def test_a_null_repo_without_provenance_or_from_another_repo_fails(self):
        """Review on sd:2581: only the repository the row was detached from reads as detached."""
        upsert_repo(self.db, "~/repos/other")
        for detached_from in (None, "~/repos/other"):
            with self.subTest(detached_from=detached_from):
                self.db.execute("UPDATE runner_run SET repo='~/repos/x', detached_from=NULL WHERE id=?", (self.ident,))
                self.released_journal()
                self.db.execute("UPDATE runner_run SET repo=NULL, detached_from=? WHERE id=?", (detached_from, self.ident))
                with self.assertRaisesRegex(BackupError, "differs from the backup journal"):
                    backup._check_runner_records(self.db, self.database.parent)

    def test_a_restore_reads_a_pre_018_saved_journal_as_the_live_one_at_its_version(self):
        """sd:2581: 018 added `detached_from`; a live rewrite at the same version carries it, the backup's copy does not."""
        def envelope(record: dict) -> bytes:
            body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            return json.dumps({"record": record, "sha256": hashlib.sha256(body).hexdigest()}).encode()
        old = {name: value for name, value in self.row.items() if name != "detached_from"}
        name = f"{self.ident}.json"
        backup._compatible_runner_records({name: envelope(old)}, {name: envelope(self.row)})
        with self.assertRaisesRegex(BackupError, "conflicts with live evidence"):
            backup._compatible_runner_records({name: envelope({**old, "detail": "other"})}, {name: envelope(self.row)})

    def test_persist_still_refuses_another_repository(self):
        write_raw(self.database, {**self.row, "repo": str(self.home / "repos" / "other")})
        with self.assertRaisesRegex(Exception, "identity changed: repo"):
            runner_journal.persist(self.database, {**self.row, "journal_version": 1})


class Detached(unittest.TestCase):
    """sd:2581: `repo remove` moves a released run's `repo` to `detached_from` in the row; its journal keeps the repo.

    The journal is never rewritten by the remove, so it cannot drift ahead of
    a transaction that rolled back. `against` reads it as the detached row
    only when the journal's repository is the row's `detached_from`; the
    journal itself still cannot lose its repository.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd2581-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name).resolve() / "sd.db"
        self.record = {"id": uuid.uuid4().hex, "assignment": 6, "run": 1, "repo": "/srv/repos/x", "branch": "sd/x",
                       "owner": "runner", "work_path": "/w", "retained_path": "/r", "created_at": STAMP,
                       "journal_version": 3, "released_at": STAMP, "detached_from": None}
        self.row = {**self.record, "repo": None, "detached_from": "/srv/repos/x"}

    def test_a_detached_row_reads_its_journal_as_the_move(self):
        self.assertEqual(runner_journal.against(self.record, self.row), self.row)

    def test_a_journal_written_before_018_reads_as_the_move_too(self):
        record = {name: value for name, value in self.record.items() if name != "detached_from"}
        self.assertEqual(runner_journal.against(record, self.row), self.row)

    def test_a_journal_naming_another_repository_still_differs(self):
        """Review on sd:2581: the journal names a repository the row never had."""
        other = {**self.record, "repo": "/srv/repos/other"}
        self.assertEqual(runner_journal.against(other, self.row)["repo"], "/srv/repos/other")

    def test_a_null_repo_without_provenance_still_differs(self):
        """Review on sd:2581: a repo lost by a hand edit or a partial restore is not a detach."""
        self.assertEqual(runner_journal.against(self.record, {**self.row, "detached_from": None})["repo"], "/srv/repos/x")
        self.assertFalse(runner_journal.detached({**self.row, "detached_from": None}))

    def test_an_open_row_still_differs(self):
        record, row = {**self.record, "released_at": None}, {**self.row, "released_at": None}
        self.assertEqual(runner_journal.against(record, row)["repo"], "/srv/repos/x")

    def test_an_attached_row_is_compared_as_written(self):
        """Review on sd:2581: nothing is nulled for a row that still names a repository."""
        attached = {**self.record, "repo": "/srv/repos/other"}
        self.assertEqual(runner_journal.against(self.record, attached)["repo"], "/srv/repos/x")

    def test_the_journal_itself_cannot_lose_its_repository(self):
        runner_journal.persist(self.database, self.record)
        with self.assertRaisesRegex(runner_journal.RunnerRefused, "identity changed: repo"):
            runner_journal.persist(self.database, {**self.row, "journal_version": 4})
        path = write_raw(self.database, {**self.record, "id": uuid.uuid4().hex, "repo": None})
        with self.assertRaisesRegex(runner_journal.RunnerRefused, "invalid repo"):
            runner_journal.read(path)


if __name__ == "__main__":
    unittest.main()
