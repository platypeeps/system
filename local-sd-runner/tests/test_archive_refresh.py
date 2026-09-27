"""Disposable databases, real archives, and native holders test nightly refresh."""

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.writes import create_item, upsert_repo
from sd_runner import archive_refresh, reconciliation, storage
from sd_runner.runtime import Config


class ArchiveRefresh(unittest.TestCase):
    def setUp(self):
        # `.noindex` keeps Spotlight out of the fixture: the clone below is
        # rewritten mid-test, and an indexer holding the rewritten file is a
        # holder `survivors` reports and the cadence test fails on -- once, in
        # CI, on 2026-09-12 (sd:554). The workflow gives TMPDIR the same suffix.
        self.temp = tempfile.TemporaryDirectory(suffix=".noindex")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        database = self.root / "db/sd.db"
        initialise(database)
        self.db = connect(database)
        self.addCleanup(self.db.close)
        checkout = self.root / "checkout"
        upsert_repo(self.db, str(checkout), remote=str(self.root / "remote.git"))
        item = create_item(self.db, kind="task", title="kept fixture", repo=str(checkout),
                           branch="work/fixture", status="ready")
        self.config = Config(database, self.root / "work", self.root / "retained", self.root / "pack",
                             self.root, floor_gb=.001)
        assignment = store.enqueue(self.db, [item], who="operator")[0]
        request = store.claim(self.db, assignment["id"], owner="fixture", work_root=self.config.work,
                              retention_root=self.config.retention)
        run = request["run"]
        self.clone = Path(run["work_path"])
        self.clone.mkdir(parents=True)
        (self.clone / "precious").write_text("initial bytes")
        (self.clone / "nested").mkdir()
        (self.clone / "nested/ignored").write_text("ignored bytes")
        (self.clone / "external").symlink_to(self.root / "outside")
        store.begin_ending(self.db, run["id"], outcome="blocked", detail="kept fixture")
        self.run = store.update_run(self.db, run["id"], end_step="kept")
        journal.persist(database, self.run)
        self.original = Path(self.run["retained_path"]).parent / "kept.tar"
        storage.archive(self.clone, self.original)
        self.original_hash = self.digest(self.original)
        self.now = datetime(2026, 9, 8, tzinfo=UTC)

    @staticmethod
    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def refresh(self, days=0):
        return archive_refresh.refresh(self.config, now=self.now + timedelta(days=days))

    def test_changed_bytes_preserve_original_and_every_generation_and_restore(self):
        database_before = list(self.db.iterdump())
        first = self.refresh()
        self.assertEqual(first["status"], "complete", first)
        entry = first["entries"][0]
        first_hash = self.digest(Path(entry["archive"]))
        (self.clone / "precious").write_text("newer operator bytes")
        second = self.refresh(days=1)
        self.assertEqual(second["status"], "complete", second)
        self.assertEqual(self.digest(self.original), self.original_hash)
        self.assertEqual(self.digest(Path(entry["archive"])), first_hash)
        self.assertNotEqual(entry["archive"], second["entries"][0]["archive"])
        selected = archive_refresh.latest(self.run)
        self.assertEqual(selected["retained_path"], second["entries"][0]["restore_run"]["retained_path"])
        target = self.root / "restored-latest"
        storage.restore(self.run, target)
        self.assertEqual((target / "precious").read_text(), "newer operator bytes")
        self.assertEqual(os.readlink(target / "external"), str(self.root / "outside"))
        self.assertEqual((target / "nested/ignored").read_text(), "ignored bytes")
        older = archive_refresh.latest(self.run, expected_sha256=entry["inventory_sha256"])
        old_target = self.root / "restored-first"
        storage.restore(older, old_target)
        self.assertEqual((old_target / "precious").read_text(), "initial bytes")
        self.assertEqual(list(self.db.iterdump()), database_before)

    def test_interrupted_restore_pins_its_generation_after_newer_refresh(self):
        first = self.refresh()
        self.assertEqual(first["status"], "complete", first)
        destination = self.root / "interrupted-restore"
        with (patch("sd_runner.restoration.os.rename", side_effect=OSError("crash before final rename")),
              self.assertRaisesRegex(OSError, "crash before final rename")):
            storage.restore(self.run, destination)
        self.assertFalse(destination.exists())
        self.assertTrue(list(self.root.glob(".sd-restore-*.working")))
        (self.clone / "precious").write_text("newer operator bytes")
        second = self.refresh(days=1)
        self.assertEqual(second["status"], "complete", second)
        storage.restore(self.run, destination)
        self.assertEqual((destination / "precious").read_text(), "initial bytes")
        latest_destination = self.root / "fresh-restore"
        storage.restore(self.run, latest_destination)
        self.assertEqual((latest_destination / "precious").read_text(), "newer operator bytes")

    def test_daily_cadence_survives_new_call_and_unchanged_clone_has_no_new_tar(self):
        first = self.refresh()
        self.assertEqual(first["status"], "complete", first)
        self.assertEqual(self.refresh()["status"], "not-due")
        (self.clone / "precious").write_text("changed inside the day")
        self.assertEqual(self.refresh()["status"], "not-due")
        second = self.refresh(days=1)
        self.assertEqual(second["status"], "complete", second)
        third = self.refresh(days=2)
        self.assertEqual(third["entries"][0]["status"], "unchanged", third)
        self.assertEqual(len(list(self.original.parent.glob("archives/*/kept.tar"))), 2)
        self.assertEqual(len(list((self.config.retention / ".archive-refresh").glob("*.json"))), 3)

    def test_schedule_is_read_only(self):
        before = sorted(str(path) for path in self.root.rglob("*"))
        report = archive_refresh.schedule(self.config, now=self.now)
        self.assertTrue(report["due"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["cadence_seconds"], 86400)
        self.assertEqual(sorted(str(path) for path in self.root.rglob("*")), before)

    def test_real_holder_is_skipped_without_signalling(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                                 cwd=self.clone, stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            report = self.refresh()
            self.assertEqual(report["status"], "held", report)
            self.assertIn("process holders", report["skipped"][0]["reason"])
            self.assertIn(f"{child.pid} (holder)", report["skipped"][0]["reason"])
            self.assertIsNone(child.poll())
            self.assertFalse((self.original.parent / "archives").exists())
        finally:
            child.terminate()
            child.wait(timeout=10)
            child.stdout.close()

    def test_a_holder_that_arrives_after_the_archive_is_named_and_the_generation_kept(self):
        # sd:1221. The post-archive check has its own wording and its own
        # consequence -- the generation stays, unselected -- and only the
        # pre-archive check was ever exercised.
        holders = [[], [{"pid": 4242, "ownership": "holder", "command": "indexer"}]]
        with patch.object(archive_refresh.processes, "survivors", side_effect=holders):
            report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        reason = report["skipped"][0]["reason"]
        self.assertIn("gained process holders", reason)
        self.assertIn("4242 (holder)", reason)
        generations = sorted((self.original.parent / "archives").glob("*/kept.tar"))
        self.assertEqual(len(generations), 1, generations)
        self.assertIn(str(generations[0].parent), reason)
        # Nothing is selected: the manifest that names a generation is absent.
        self.assertFalse((generations[0].parent / "manifest.json").exists())
        self.assertEqual(self.digest(self.original), self.original_hash)

    def test_ending_and_archive_locks_exclude_refresh(self):
        paths = (self.config.database.parent / "runner-ending" / f"{self.run['id']}.lock",
                 self.original.parent / ".archive.lock")
        for path in paths:
            with self.subTest(path=path), journal.lock(path):
                report = self.refresh()
                self.assertEqual(report["status"], "held", report)
                self.assertIn("ownership lock held", report["skipped"][0]["reason"])
        self.assertFalse((self.original.parent / "archives").exists())

    def test_restore_uncertainty_and_missing_journal_hold_all_writes(self):
        marker = self.config.database.parent / "runner-restore-intent.json"
        marker.write_text("{}")
        report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        self.assertIn("unresolved restore", report["reason"])
        marker.rename(self.root / "saved-restore-intent.json")
        record = journal.directory(self.config.database) / f"{self.run['id']}.json"
        record.rename(self.root / "saved-run-journal.json")
        report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        self.assertIn("ownership evidence", report["reason"])
        self.assertFalse((self.original.parent / "archives").exists())

    def test_journal_issue_beside_no_other_run_holds_refresh(self):
        # A hard link gives a second writable path to the run's durable ownership
        # record, so `_journal_view` blocks both names. `plan` withholds the run
        # each blocked name identifies (sd:779, #362), which empties `entries`:
        # the issue is then the only evidence that anything is wrong.
        record = journal.directory(self.config.database) / f"{self.run['id']}.json"
        stray = record.parent / f"{'a' * 32}.json"
        os.link(record, stray)
        plan = reconciliation.plan(self.config)
        self.assertEqual(plan["entries"], [])
        self.assertFalse(plan["restore_pending"])
        self.assertEqual(sorted(issue["entry"] for issue in plan["journal_issues"]),
                         sorted([record.name, stray.name]))
        self.assertTrue(all(issue["blocked"] for issue in plan["journal_issues"]))
        report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        # `reconciliation.quarantine` refuses a blocked issue, and only the
        # restore-linked shape has a repair verb at all, so naming quarantine
        # here would send the operator at an action the library will refuse.
        self.assertIn(f"recovery for {', '.join(sorted([record.name, stray.name]))}", report["reason"])
        self.assertNotIn("quarantine for", report["reason"])
        self.assertFalse((self.original.parent / "archives").exists())
        self.assertEqual(self.digest(self.original), self.original_hash)

    def test_interrupted_partial_journal_write_holds_refresh(self):
        # An issue that is not blocked holds too: `reconcile` refuses on any
        # issue, not only on one whose entry could not be opened at all.
        stray = journal.directory(self.config.database) / f"{'b' * 32}.partial"
        stray.write_bytes(b'{"run": "half-written')
        plan = reconciliation.plan(self.config)
        self.assertEqual(plan["entries"], [])
        self.assertFalse(plan["restore_pending"])
        self.assertEqual([issue["entry"] for issue in plan["journal_issues"]], [stray.name])
        self.assertIsNone(plan["journal_issues"][0]["blocked"])
        report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        self.assertIn(f"quarantine for {stray.name}", report["reason"])
        self.assertNotIn("recovery for", report["reason"])
        self.assertFalse((self.original.parent / "archives").exists())

    def test_low_capacity_skips_without_new_archive(self):
        with patch("sd_runner.archive_refresh.storage.capacity", return_value={"free": 1}):
            report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        self.assertIn("free-space floor", report["reason"])
        self.assertFalse((self.original.parent / "archives").exists())
        self.assertEqual(self.digest(self.original), self.original_hash)

    def test_mutation_during_real_tar_creation_preserves_unselected_partial(self):
        original_add = tarfile.TarFile.add
        def change_after_add(archive, name, *args, **kwargs):
            result = original_add(archive, name, *args, **kwargs)
            if Path(name) == self.clone:
                (self.clone / "precious").write_text("changed during tar creation")
            return result
        with patch.object(tarfile.TarFile, "add", change_after_add):
            report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        self.assertIn("changed during archive", report["skipped"][0]["reason"])
        self.assertEqual(self.digest(self.original), self.original_hash)
        self.assertEqual(len(list(self.original.parent.glob("archives/*/kept.partial"))), 1)
        self.assertEqual(archive_refresh.latest(self.run), self.run)
        self.assertEqual(self.refresh()["status"], "complete")

    def test_preserved_metadata_mutation_is_caught_by_content_verification(self):
        original_add = tarfile.TarFile.add
        precious = self.clone / "precious"
        details = precious.stat()
        def change_after_add(archive, name, *args, **kwargs):
            result = original_add(archive, name, *args, **kwargs)
            if Path(name) == self.clone:
                precious.write_text("altered bytes")
                os.utime(precious, ns=(details.st_atime_ns, details.st_mtime_ns))
            return result
        with patch.object(tarfile.TarFile, "add", change_after_add):
            report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        self.assertIn("changed during refresh", report["skipped"][0]["reason"])
        self.assertEqual(self.digest(self.original), self.original_hash)
        self.assertEqual(archive_refresh.latest(self.run), self.run)
        self.assertEqual(len(list(self.original.parent.glob("archives/*/kept.tar"))), 1)

    def test_tampered_generation_or_run_binding_is_refused(self):
        report = self.refresh()
        self.assertEqual(report["status"], "complete", report)
        path = Path(report["entries"][0]["generation"]) / "manifest.json"
        record = json.loads(path.read_text())
        record["run"] = "f" * 32
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(store.RunnerRefused, "owned run"):
            archive_refresh.latest(self.run)
        self.assertEqual(self.digest(self.original), self.original_hash)

    def test_archive_root_symlink_cannot_redirect_writes(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.original.parent / "archives").symlink_to(outside)
        report = self.refresh()
        self.assertEqual(report["status"], "held", report)
        self.assertIn("root is linked", report["skipped"][0]["reason"])
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
