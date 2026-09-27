"""A journal entry that vanishes during a reconciliation read (review concern C-50).

`removal.apply` renames runner-journal files into quarantine, and that rename
can land while `reconciliation._journal_view` walks the listing it took. Each
test below removes one run's `.json` at a different point of that walk and
asks `plan` to return with the entry in neither the records nor the issues,
its sibling's record intact, and the vanished run still named by `plan` as a
`journal-from-database` entry, which is what makes skipping it safe.

The pins at the end keep the catch narrow: an entry swapped for a symlink
still lstats, so it is still blocked, and a symlink met by `O_NOFOLLOW` still
raises instead of being skipped.
"""

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from sd_db import runner_journal as journal
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.writes import create_assignment, create_item, upsert_repo
from sd_runner import reconciliation
from sd_runner.runtime import Config

STAMP = "2026-09-23T12:00:00+00:00"


class JournalVanish(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="c50-", suffix=".noindex")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / "db" / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        (self.root / "retained").mkdir()
        self.config = Config(self.database, self.root / "work", self.root / "retained", self.root / "pack",
                             self.root / "home", floor_gb=.001)
        source = str(self.root / "checkout")
        upsert_repo(self.db, source)
        item = create_item(self.db, kind="task", title="c50 fixture", status="ready", repo=source)
        self.runs = []
        for _ in range(2):
            work = create_assignment(self.db, role="author", status="running", item=item)
            ident = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ident, work, 1, source, "sd/c50", "runner", str(self.root / "work" / f"{work}-1-{ident}"),
                 str(self.root / "retained" / str(work) / "1" / "clone"), STAMP, STAMP))
            row = dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (ident,)).fetchone())
            journal.persist(self.database, row)
            self.runs.append(ident)
        self.vanished, self.sibling = self.runs
        self.journal = journal.directory(self.database)
        self.target = self.journal / f"{self.vanished}.json"
        # The control: with nothing vanishing, both runs are healthy records.
        records, issues = reconciliation._journal_view(self.database)
        self.assertEqual(sorted(record["id"] for record in records), sorted(self.runs))
        self.assertEqual(issues, [])
        self.assertEqual(reconciliation.plan(self.config)["entries"], [])

    def plan(self):
        """Run `plan` once, keeping what its one `_journal_view` call returned."""
        seen = []
        real = reconciliation._journal_view

        def view(database):
            seen.append(real(database))
            return seen[-1]

        with mock.patch.object(reconciliation, "_journal_view", side_effect=view):
            report = reconciliation.plan(self.config)
        self.assertEqual(len(seen), 1)
        return report, seen[0]

    def assert_skipped(self, report, view):
        records, issues = view
        self.assertFalse(self.target.exists())
        self.assertEqual([record["id"] for record in records], [self.sibling])
        self.assertEqual(issues, [])
        self.assertEqual(report["journal_issues"], [])
        # Skipping hides nothing: the active run without a journal is still named.
        self.assertEqual([(entry["run"], entry["operation"]) for entry in report["entries"]],
                         [(self.vanished, "journal-from-database")])

    def test_an_entry_gone_after_the_listing_is_skipped(self):
        real = Path.iterdir

        def listing(path):
            names = list(real(path))
            if path == self.journal and self.target.exists():
                self.target.unlink()
            return iter(names)

        with mock.patch.object(Path, "iterdir", autospec=True, side_effect=listing):
            report, view = self.plan()
        self.assert_skipped(report, view)

    def test_an_entry_gone_before_its_open_is_skipped(self):
        real = os.open

        def opener(path, flags, *args):
            if Path(path) == self.target and self.target.exists():
                self.target.unlink()
            return real(path, flags, *args)

        with mock.patch.object(reconciliation.os, "open", side_effect=opener):
            report, view = self.plan()
        self.assert_skipped(report, view)

    def test_an_entry_gone_while_it_is_open_is_skipped(self):
        real = os.open

        def opener(path, flags, *args):
            descriptor = real(path, flags, *args)
            if Path(path) == self.target and self.target.exists():
                self.target.unlink()
            return descriptor

        with mock.patch.object(reconciliation.os, "open", side_effect=opener):
            report, view = self.plan()
        self.assert_skipped(report, view)

    def test_an_entry_gone_before_the_journal_read_is_skipped(self):
        real = journal.read

        def read(path):
            if path == self.target and self.target.exists():
                self.target.unlink()
            return real(path)

        with mock.patch.object(reconciliation.journal, "read", side_effect=read):
            report, view = self.plan()
        self.assert_skipped(report, view)

    # -- pins: the catch is FileNotFoundError, not "anything that moved" ----

    def test_pin_an_entry_swapped_for_a_symlink_is_still_a_blocked_issue(self):
        real = Path.iterdir
        decoy = self.root / "decoy.json"
        decoy.write_bytes(self.target.read_bytes())

        def listing(path):
            names = list(real(path))
            if path == self.journal and not self.target.is_symlink():
                self.target.unlink()
                self.target.symlink_to(decoy)
            return iter(names)

        with mock.patch.object(Path, "iterdir", autospec=True, side_effect=listing):
            report, (records, issues) = self.plan()
        self.assertTrue(self.target.is_symlink())
        self.assertEqual([record["id"] for record in records], [self.sibling])
        self.assertEqual([(issue["entry"], bool(issue["blocked"])) for issue in issues],
                         [(self.target.name, True)])
        self.assertEqual(report["journal_issues"], issues)

    def test_pin_a_symlink_met_by_the_open_still_raises(self):
        real = os.open
        decoy = self.root / "decoy.json"
        decoy.write_bytes(self.target.read_bytes())

        def opener(path, flags, *args):
            if Path(path) == self.target and not self.target.is_symlink():
                self.target.unlink()
                self.target.symlink_to(decoy)
            return real(path, flags, *args)

        with mock.patch.object(reconciliation.os, "open", side_effect=opener):
            with self.assertRaises(OSError) as raised:
                reconciliation.plan(self.config)
        self.assertNotIsInstance(raised.exception, FileNotFoundError)


if __name__ == "__main__":
    unittest.main()
