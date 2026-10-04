"""Retention deletes only complete, unchanged backups with recorded ownership."""
import json
from importlib import import_module
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from datetime import UTC, datetime, timedelta

from sd_db.errors import BackupError
from sd_db.jobs.backup import retention

from .test_backup import BackupCase, ENTRYPOINT

backup = import_module("sd_db.backup")

class Retention(BackupCase):
    def snapshot(self, day=0, **kwargs):
        return backup.run(home=self.home, when=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day), **kwargs)

    def test_no_delete_mode_never_calls_rmtree(self):
        with patch.object(backup.shutil, "rmtree", side_effect=AssertionError("deletion reached")):
            first = self.snapshot()
            second = self.snapshot(1, keep=None)
            self.assertEqual(backup.prune(backup.backup_root(self.home), None), [])
        self.assertTrue(first.directory.exists())
        self.assertTrue(second.directory.exists())

    def test_explicit_retention_removes_only_verified_owned_backups(self):
        snapshots = [self.snapshot(day) for day in range(3)]
        latest = self.snapshot(3, keep=2)
        self.assertEqual(latest.removed, [snapshot.directory for snapshot in snapshots[:2]])
        self.assertTrue(snapshots[2].directory.exists())
        self.assertTrue(latest.directory.exists())

    def test_unrelated_and_unowned_dated_directories_are_preserved(self):
        owned = self.snapshot()
        root = backup.backup_root(self.home)
        protected = [root / name for name in ("personal", "worktrees", "2025-01-01", "2025-02-30")]
        for path in protected:
            path.mkdir()
            (path / "keep.txt").write_text("unowned data")
        self.assertEqual(backup.prune(root, 0), [owned.directory])
        for path in protected:
            self.assertEqual((path / "keep.txt").read_text(), "unowned data")

    def test_runner_retention_and_dated_symlinks_are_preserved(self):
        owned = self.snapshot()
        root = backup.backup_root(self.home)
        target = self.home / "retained"
        target.mkdir()
        (target / "work").write_text("retained output")
        links = [root / "worktrees", root / "2025-01-01"]
        for path in links:
            path.symlink_to(target, target_is_directory=True)
        self.assertEqual(backup.prune(root, 0), [owned.directory])
        self.assertTrue(all(path.is_symlink() for path in links))
        self.assertEqual((target / "work").read_text(), "retained output")

    def test_malformed_or_wrong_checkpoint_manifests_are_preserved(self):
        invalid = self.snapshot()
        (invalid.directory / backup.BACKUP_MANIFEST).write_text("not json")
        wrong_checkpoint = self.snapshot(1)
        path = wrong_checkpoint.directory / backup.BACKUP_MANIFEST
        manifest = json.loads(path.read_text())
        manifest["run_id"] = "0" * 32
        path.write_text(json.dumps(manifest))
        with patch.object(backup.shutil, "rmtree", side_effect=AssertionError("unowned deletion")):
            self.assertEqual(backup.prune(backup.backup_root(self.home), 0), [])
        self.assertTrue(invalid.directory.exists())
        self.assertTrue(wrong_checkpoint.directory.exists())

    def test_changed_contents_and_added_empty_directories_are_preserved(self):
        changed = self.snapshot()
        with (changed.directory / "sd.db").open("ab") as stream:
            stream.write(b"changed")
        extended = self.snapshot(1)
        (extended.directory / "unrelated").mkdir()
        self.assertEqual(backup.prune(backup.backup_root(self.home), 0), [])

    def test_moved_backup_cannot_claim_its_new_path(self):
        original = self.snapshot()
        moved = original.directory.with_name("2025-01-01")
        original.directory.rename(moved)
        self.assertEqual(backup.prune(backup.backup_root(self.home), 0), [])
        self.assertTrue(moved.exists())

    def test_linked_manifest_or_internal_entry_blocks_deletion(self):
        linked = self.snapshot()
        manifest = linked.directory / backup.BACKUP_MANIFEST
        saved = self.home / "manifest.json"
        manifest.rename(saved)
        manifest.symlink_to(saved)
        internal = self.snapshot(1)
        (internal.directory / "external").symlink_to(self.home / "missing")
        self.assertEqual(backup.prune(backup.backup_root(self.home), 0), [])

    def test_same_day_retention_sorts_numeric_suffixes(self):
        snapshots = [self.snapshot() for _ in range(12)]
        self.assertEqual(backup.prune(backup.backup_root(self.home), 2),
                         [snapshot.directory for snapshot in snapshots[:-2]])
        self.assertTrue(snapshots[-1].directory.exists())
        self.assertTrue(snapshots[-2].directory.exists())

    def test_linked_root_refuses_numeric_retention(self):
        self.snapshot()
        link = self.home / "backup-link"
        link.symlink_to(backup.backup_root(self.home), target_is_directory=True)
        with self.assertRaises(BackupError):
            backup.prune(link, 0)

    def test_invalid_count_refuses_before_checkpoint(self):
        before = self.database.read_bytes()
        for value in (-1, True, "30"):
            with self.assertRaises(BackupError):
                self.snapshot(keep=value)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(backup.backup_root(self.home).exists())

    def test_cli_forwards_explicit_no_delete_mode(self):
        result = subprocess.run(["sh", str(ENTRYPOINT), "backup", "--keep", "all"],
                                env={**os.environ, "HOME": str(self.home), "PYTHON": sys.executable},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 old snapshot(s) removed", result.stdout)
        root = backup.backup_root(self.home)
        self.assertEqual(len(list(root.glob("*/" + backup.BACKUP_MANIFEST))), 1)

    def test_cli_forwards_explicit_destination(self):
        destination = self.home / "external" / "sd-backups"
        result = subprocess.run(
            ["sh", str(ENTRYPOINT), "backup", "--destination", str(destination)],
            env={**os.environ, "HOME": str(self.home), "PYTHON": sys.executable},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(destination), result.stdout)
        self.assertFalse(backup.backup_root(self.home).exists())
        self.assertEqual(len(list(destination.glob("*/" + backup.BACKUP_MANIFEST))), 1)

    def test_cli_retention_parser_accepts_only_explicit_supported_values(self):
        self.assertIsNone(retention("all"))
        self.assertEqual(retention("30"), 30)
        import argparse
        # 0 is refused: the count includes the backup the run takes (sd:2599).
        for value in ("0", "-1", "yes", "", "٣٠", "3.0"):
            with self.assertRaises(argparse.ArgumentTypeError):
                retention(value)

    def test_cli_forwards_explicit_numeric_retention(self):
        for day in range(3):
            self.snapshot(day)
        result = subprocess.run(["sh", str(ENTRYPOINT), "backup", "--keep", "2"],
                                env={**os.environ, "HOME": str(self.home), "PYTHON": sys.executable},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 old snapshot(s) removed", result.stdout)
        self.assertEqual(len(list(backup.backup_root(self.home).iterdir())), 2)

    def test_changed_ownership_at_final_check_preserves_backup(self):
        snapshot = self.snapshot()
        identity = backup._owned_backup(snapshot.directory)
        with patch.object(backup, "_owned_backup", side_effect=[identity, None]), \
                patch.object(backup.shutil, "rmtree", side_effect=AssertionError("deletion reached")):
            with self.assertRaisesRegex(BackupError, "changed before retention"):
                backup.prune(backup.backup_root(self.home), 0)
        self.assertTrue(snapshot.directory.exists())


if __name__ == "__main__":
    unittest.main()
