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

    def test_an_absolute_path_under_the_home_becomes_the_key(self):
        record = {"id": "a", "repo": str(self.home / "repos" / "x")}
        self.assertEqual(runner_journal.canonical(record), {"id": "a", "repo": "~/repos/x"})
        self.assertEqual(record["repo"], str(self.home / "repos" / "x"), "the argument is not changed")

    def test_a_key_and_a_path_outside_the_home_are_kept(self):
        for repo in ("~/repos/x", "/srv/repos/x", "checkout"):
            record = {"id": "a", "repo": repo}
            self.assertIs(runner_journal.canonical(record), record)

    def test_no_home_keeps_the_record_as_it_is(self):
        record = {"id": "a", "repo": str(self.home / "repos" / "x")}
        with mock.patch.dict(os.environ, {"HOME": ""}):
            self.assertIs(runner_journal.canonical(record), record)


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

    def test_persist_still_refuses_another_repository(self):
        write_raw(self.database, {**self.row, "repo": str(self.home / "repos" / "other")})
        with self.assertRaisesRegex(Exception, "identity changed: repo"):
            runner_journal.persist(self.database, {**self.row, "journal_version": 1})

if __name__ == "__main__":
    unittest.main()


class Detached(unittest.TestCase):
    """sd:2581: `repo remove` detaches a released run, and its journal holds `repo` NULL.

    Only a released run may lose its repository, and only that one field may
    change: an open run with none, or a detached run given a repository back,
    is still refused.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd2581-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name).resolve() / "sd.db"
        self.record = {"id": uuid.uuid4().hex, "assignment": 6, "run": 1, "repo": "/srv/repos/x", "branch": "sd/x",
                       "owner": "runner", "work_path": "/w", "retained_path": "/r", "created_at": STAMP,
                       "journal_version": 3, "released_at": STAMP}

    def test_a_released_run_may_lose_its_repository_once(self):
        runner_journal.persist(self.database, self.record)
        detached = {**self.record, "repo": None, "journal_version": 4}
        path = runner_journal.persist(self.database, detached)
        self.assertEqual(runner_journal.read(path), detached)
        with self.assertRaisesRegex(runner_journal.RunnerRefused, "identity changed: repo"):
            runner_journal.persist(self.database, {**detached, "repo": "/srv/repos/y", "journal_version": 5})

    def test_an_open_run_with_no_repository_is_still_invalid(self):
        path = write_raw(self.database, {**self.record, "repo": None, "released_at": None})
        with self.assertRaisesRegex(runner_journal.RunnerRefused, "invalid repo"):
            runner_journal.read(path)
        other = {**self.record, "id": uuid.uuid4().hex, "released_at": None}
        runner_journal.persist(self.database, other)
        with self.assertRaisesRegex(runner_journal.RunnerRefused, "identity changed: repo"):
            runner_journal.persist(self.database, {**other, "repo": None, "journal_version": 4})
