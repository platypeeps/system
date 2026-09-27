"""A rejected restore must leave the operator's current database usable."""

import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, create_item
from sd_db.backup import restore, run
from sd_db.errors import BackupError
from sd_db.migrate import initialise
from sd_db.schema import SCHEMA_VERSION, migrations

backup_module = importlib.import_module("sd_db.backup")


class RestoreSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        initialise(home=self.home)
        self.state = self.home / ".local/share/sd"
        self.database = self.state / "sd.db"
        self.writer = connect(home=self.home)
        self.addCleanup(self.writer.close)
        create_item(self.writer, kind="task", title="Saved before backup")
        self.snapshot = run(home=self.home).directory
        self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.writer.execute("PRAGMA wal_autocheckpoint = 0")
        self.pending = create_item(self.writer, kind="task", title="Committed only in WAL")
        self.config = self.state / "providers.yaml"
        self.config.write_text("current providers\n", encoding="utf-8")

    def bytes(self):
        return {
            path.name: path.read_bytes() for path in self.state.iterdir() if path.is_file()
        }

    def assert_preserved(self, before):
        self.assertEqual(self.bytes(), before)
        self.assertEqual(self.writer.execute("SELECT title FROM item WHERE id = ?", (self.pending,)).fetchone()[0], "Committed only in WAL")
        self.assertEqual(self.writer.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_corrupt_or_truncated_snapshot_does_not_touch_current_database_or_wal(self):
        original = (self.snapshot / "sd.db").read_bytes()
        for content in (b"not a database", original[:len(original) // 2]):
            (self.snapshot / "sd.db").write_bytes(content)
            before = self.bytes()
            with self.assertRaises(BackupError):
                restore(self.snapshot, home=self.home)
            self.assert_preserved(before)

    def test_incompatible_snapshot_is_refused_before_replacing_configuration(self):
        (self.snapshot / "providers.yaml").write_text("old providers\n", encoding="utf-8")
        # The older label is 1, not `SCHEMA_VERSION - 1`: a current-shape
        # snapshot claiming the version one behind is only detectable when
        # the last migration changed a table set or a column list, and 007
        # changed a CHECK constraint -- the shape it leaves is the shape it
        # found, so such a snapshot reads as a real one and is upgraded.
        # Version 1 has half the tables and stays refusable.
        for version in (1, SCHEMA_VERSION + 1):
            raw = sqlite3.connect(self.snapshot / "sd.db")
            raw.execute(f"PRAGMA user_version = {version}")
            raw.close()
            before = self.bytes()
            with self.assertRaises(BackupError):
                restore(self.snapshot, home=self.home)
            self.assert_preserved(before)

    def test_matching_version_with_missing_table_is_refused(self):
        raw = sqlite3.connect(self.snapshot / "sd.db")
        raw.execute("DROP TABLE trial")
        raw.close()
        before = self.bytes()
        with self.assertRaises(BackupError):
            restore(self.snapshot, home=self.home)
        self.assert_preserved(before)

    def test_matching_version_with_wrong_columns_is_refused(self):
        raw = sqlite3.connect(self.snapshot / "sd.db")
        raw.execute("ALTER TABLE trial ADD COLUMN not_in_schema TEXT")
        raw.close()
        before = self.bytes()
        with self.assertRaises(BackupError):
            restore(self.snapshot, home=self.home)
        self.assert_preserved(before)

    def test_failed_configuration_staging_keeps_current_database_and_configuration(self):
        (self.snapshot / "commands.yaml").mkdir()
        before = self.bytes()
        with self.assertRaises(BackupError):
            restore(self.snapshot, home=self.home)
        self.assert_preserved(before)

    def test_successful_restore_uses_sqlite_and_remains_visible_to_existing_connection(self):
        target = restore(self.snapshot, home=self.home)
        self.assertEqual(target, self.database)
        self.assertEqual(self.writer.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual([row[0] for row in self.writer.execute("SELECT title FROM item")], ["Saved before backup"])
        rows = self.writer.execute("SELECT key FROM state WHERE kind = 'restore' AND resolved_at IS NULL").fetchall()
        self.assertEqual([row[0] for row in rows], [self.snapshot.name])

    def test_busy_destination_times_out_and_rolls_back_configuration(self):
        (self.snapshot / "providers.yaml").write_text("snapshot providers\n", encoding="utf-8")
        self.writer.execute("BEGIN IMMEDIATE")
        before = self.bytes()
        try:
            with patch.object(backup_module, "RESTORE_SECONDS", 0), self.assertRaises(BackupError) as raised:
                restore(self.snapshot, home=self.home)
            self.assertIn("timed out", str(raised.exception))
            self.assert_preserved(before)
        finally:
            self.writer.execute("ROLLBACK")

    def test_partial_sqlite_copy_abort_rolls_back_the_database(self):
        source = connect(self.snapshot / "sd.db")
        create_item(source, kind="task", title="Large source", body={"text": "a" * 1_000_000})
        self.assertGreater(source.execute("PRAGMA page_count").fetchone()[0], 128)
        source.close()
        before = list(self.writer.iterdump())
        with patch.object(backup_module.time, "monotonic", side_effect=[0, 31]), self.assertRaises(BackupError):
            restore(self.snapshot, home=self.home)
        self.assertEqual(list(self.writer.iterdump()), before)
        self.assertEqual(self.writer.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_nonempty_source_wal_is_not_silently_discarded(self):
        source = connect(self.snapshot / "sd.db")
        self.addCleanup(source.close)
        create_item(source, kind="task", title="Snapshot is actually live")
        before = self.bytes()
        with self.assertRaises(BackupError) as raised:
            restore(self.snapshot, home=self.home)
        self.assertIn("WAL", str(raised.exception))
        self.assert_preserved(before)

    def test_postcommit_failure_is_explicit_and_keeps_matching_configuration(self):
        (self.snapshot / "providers.yaml").write_text("snapshot providers\n", encoding="utf-8")
        check = backup_module._check_restore
        attempts = 0

        def fail_after_commit(connection):
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                raise sqlite3.OperationalError("injected post-check failure")
            return check(connection)

        with patch.object(backup_module, "_check_restore", side_effect=fail_after_commit), self.assertRaises(BackupError) as raised:
            restore(self.snapshot, home=self.home)
        self.assertIn("was installed but its verification failed", str(raised.exception))
        self.assertEqual(self.config.read_text(encoding="utf-8"), "snapshot providers\n")
        self.assertEqual([row[0] for row in self.writer.execute("SELECT title FROM item")], ["Saved before backup"])

    def legacy_snapshot(self):
        directory = self.home.parent / "legacy-schema-3"
        directory.mkdir()
        raw = sqlite3.connect(directory / "sd.db")
        try:
            for version, source in migrations():
                if version <= 3:
                    raw.executescript(source.read_text())
            raw.execute("PRAGMA user_version=3")
            raw.execute("INSERT INTO item(kind,title,status,created_at,updated_at) VALUES ('task','Legacy task','planning','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')")
            raw.commit()
        finally:
            raw.close()
        return directory

    def test_schema_three_backup_upgrades_only_staged_copy_then_restores(self):
        snapshot = self.legacy_snapshot()
        original = (snapshot / "sd.db").read_bytes()
        restore(snapshot, home=self.home)
        self.assertEqual((snapshot / "sd.db").read_bytes(), original)
        self.assertEqual(self.writer.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.writer.execute("SELECT title FROM item").fetchone()[0], "Legacy task")
        self.assertEqual(self.writer.execute("SELECT count(*) FROM item").fetchone()[0], 1)
        self.assertEqual(self.writer.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.writer.execute("SELECT count(*) FROM state WHERE kind='restore' AND resolved_at IS NULL").fetchone()[0], 1)

    def test_failed_staged_upgrade_preserves_live_wal_configuration_and_old_backup(self):
        snapshot = self.legacy_snapshot()
        original = (snapshot / "sd.db").read_bytes()
        (snapshot / "providers.yaml").write_text("old configuration")
        before = self.bytes()
        migrate = backup_module.migrate_database

        def fail_after_migration(candidate):
            migrate(candidate)
            raise sqlite3.OperationalError("injected migration verification failure")

        with patch.object(backup_module, "migrate_database", side_effect=fail_after_migration):
            with self.assertRaisesRegex(BackupError, "migration verification failure"):
                restore(snapshot, home=self.home)
        self.assertEqual((snapshot / "sd.db").read_bytes(), original)
        self.assert_preserved(before)


if __name__ == "__main__":
    unittest.main()
