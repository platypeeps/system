"""Recovery uses real Git history and captured sources, with atomic refusals."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sd_db import (
    connect,
    create_assignment,
    create_item,
    initialise,
    record_state,
    set_item_fields,
    upsert_repo,
)
from sd_db.backup import restore, run
from sd_db.errors import SdDbError
from sd_db.recovery import RecoveryRefused, reimport
from sd_db.writes import add_note
from sd_db.writing import import_piece


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Recovery fixture")
        self.git("config", "commit.gpgsign", "false")
        self.home = self.root / "home"
        initialise(home=self.home)
        self.db = connect(home=self.home)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, str(self.repo))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture source")
        return self.git("rev-parse", "HEAD")

    def work(self, slug="one", *, row=True):
        relative = f"docs/work/{slug}/prd.md"
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {slug}\nstatus: ready\ncreated: 2026-08-01\n---\nDetails.\n")
        commit = self.commit()
        if not row:
            return path, None
        item = create_item(self.db, kind="work", title=slug, repo=str(self.repo),
                           status="ready", path=relative, source="docs/work", source_commit=commit,
                           external_id=f"{self.repo}::{relative}", session="docs/work migration")
        return path, item

    def apply(self):
        preview = reimport(self.db, str(self.repo), dry_run=True)
        return reimport(self.db, str(self.repo), expected_fingerprint=preview["fingerprint"])

    def retire_work(self):
        for path in self.repo.glob("docs/work/*/prd.md"):
            path.write_text(path.read_text().replace("status: ready\n", ""))
        (self.repo / "docs/work/.status-source").write_text("row\n")
        self.commit()
        upsert_repo(self.db, str(self.repo), status_source="retiring")
        record_state(self.db, "restore", key="old-snapshot")

    def piece(self):
        path = self.repo / "content/2026/piece/index.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\ntitle: A piece\nstatus: review\n---\n## Draft\nUncommitted original.\n")
        item = import_piece(self.db, str(self.repo), "2026/piece", who="operator")["item"]["id"]
        path.write_text(path.read_text().replace("status: review\n", ""))
        (self.repo / "content/.status-source").write_text("row\n")
        upsert_repo(self.db, str(self.repo), pieces_source="retiring")
        record_state(self.db, "restore", key="old-snapshot")
        return path, item

    def test_pinned_history_rebuilds_rows_and_does_not_rewrite_checkout(self):
        path, item = self.work()
        self.retire_work()
        before = path.read_bytes()
        result = self.apply()
        self.assertEqual(result["authorities"], {"status_source": 1})
        row = self.db.execute("SELECT * FROM item WHERE id=?", (item,)).fetchone()
        self.assertEqual((row["title"], row["status"]), ("one", "ready"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.db.execute("SELECT status_source FROM repo").fetchone()[0], "row")
        self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE kind='restore' AND resolved_at IS NULL").fetchone()[0], 1)
        with self.assertRaisesRegex(RecoveryRefused, "not awaiting"):
            reimport(self.db, str(self.repo))

    def test_inventory_finds_missing_rows_and_restores_from_history(self):
        self.work()
        self.work("two", row=False)
        self.retire_work()
        result = self.apply()
        self.assertEqual(result["authorities"]["status_source"], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 2)
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_missing_commit_refuses_before_any_row_or_authority_changes(self):
        _, item = self.work()
        _, other = self.work("two")
        self.retire_work()
        set_item_fields(self.db, other, source_commit="0" * 40)
        before = list(self.db.execute("SELECT title, status FROM item"))
        with self.assertRaises(SdDbError):
            reimport(self.db, str(self.repo))
        self.assertEqual(list(self.db.execute("SELECT title, status FROM item")), before)
        self.assertEqual(self.db.execute("SELECT status_source FROM repo").fetchone()[0], "retiring")
        self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE kind='verified'").fetchone()[0], 0)

    def test_writing_recovery_uses_hash_bound_capture_without_a_commit(self):
        path, item = self.piece()
        before = path.read_bytes()
        result = self.apply()
        self.assertEqual(result["authorities"], {"pieces_source": 1})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.db.execute("SELECT stage FROM item WHERE id=?", (item,)).fetchone()[0], "review")

    def test_corrupt_captured_source_keeps_writing_held(self):
        _, item = self.piece()
        set_item_fields(self.db, item, body={"source": "forged"})
        with self.assertRaisesRegex(RecoveryRefused, "hash and path"):
            reimport(self.db, str(self.repo))
        self.assertEqual(self.db.execute("SELECT pieces_source FROM repo").fetchone()[0], "retiring")

    def test_later_progress_is_not_overwritten_by_migration_replay(self):
        _, item = self.work()
        self.retire_work()
        self.db.execute("UPDATE item SET status='in_progress' WHERE id=?", (item,))
        add_note(self.db, item, "comment", "Implemented the first step", session="user")
        with self.assertRaisesRegex(RecoveryRefused, "later progress"):
            reimport(self.db, str(self.repo))
        self.assertEqual(self.db.execute("SELECT status FROM item").fetchone()[0], "in_progress")

    def test_restore_blocks_old_assignments_and_marks_unproven_cutover(self):
        self.work()
        self.retire_work()
        self.db.execute("DELETE FROM state WHERE kind='restore'")
        upsert_repo(self.db, str(self.repo), status_source="file")
        assignment = create_assignment(self.db, role="worker", status="queued")
        snapshot = run(home=self.home)
        restore(snapshot.directory, home=self.home)
        self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE id=?", (assignment,)).fetchone()[0], "blocked")
        self.assertEqual(self.db.execute("SELECT status_source FROM repo").fetchone()[0], "retiring")
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_preview_writes_no_rows_and_a_stale_preview_is_refused(self):
        _, item = self.work()
        self.retire_work()
        before = "\n".join(self.db.iterdump())
        preview = reimport(self.db, str(self.repo), dry_run=True)
        self.assertEqual("\n".join(self.db.iterdump()), before)
        set_item_fields(self.db, item, title="Concurrent edit")
        with self.assertRaisesRegex(RecoveryRefused, "changed since preview"):
            reimport(self.db, str(self.repo), expected_fingerprint=preview["fingerprint"])
        self.assertEqual(self.db.execute("SELECT title FROM item").fetchone()[0], "Concurrent edit")

    def test_killed_reimport_rolls_back_rows_and_authority_and_can_restart(self):
        _, item = self.work()
        self.retire_work()
        before = "\n".join(self.db.iterdump())
        script = """import os,sys
from sd_db import connect
from sd_db import recovery
db = connect(home=sys.argv[1])
recovery.add_note = lambda *args, **kwargs: os._exit(73)
preview = recovery.reimport(db, sys.argv[2], dry_run=True)
recovery.reimport(db, sys.argv[2], expected_fingerprint=preview["fingerprint"])
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.home), str(self.repo)],
                                env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual("\n".join(self.db.iterdump()), before)
        self.assertEqual(self.apply()["authorities"], {"status_source": 1})

    def test_same_stage_metadata_and_title_changes_are_not_overwritten(self):
        _, item = self.piece()
        row = self.db.execute("SELECT * FROM item WHERE id=?", (item,)).fetchone()
        fields = json.loads(row["fields"])
        fields["writing"].update({"tip": "new destination", "published_urls": {"gdrive": "https://example.invalid/new"}})
        fields["user_context"] = {"decision": "retain this"}
        set_item_fields(self.db, item, title="Revised title", fields=fields)
        add_note(self.db, item, "comment", "Updated metadata while remaining in review", session="user")
        before = "\n".join(self.db.iterdump())
        with self.assertRaisesRegex(RecoveryRefused, "later progress in title, fields"):
            reimport(self.db, str(self.repo))
        self.assertEqual("\n".join(self.db.iterdump()), before)

    def test_same_stage_metadata_with_an_unattributed_note_is_protected(self):
        _, item = self.piece()
        fields = json.loads(self.db.execute("SELECT fields FROM item WHERE id=?", (item,)).fetchone()[0])
        fields["writing"]["tip"] = "new metadata"
        set_item_fields(self.db, item, fields=fields)
        add_note(self.db, item, "comment", "Metadata was edited")
        before = "\n".join(self.db.iterdump())
        with self.assertRaisesRegex(RecoveryRefused, "later progress in fields"):
            reimport(self.db, str(self.repo))
        self.assertEqual("\n".join(self.db.iterdump()), before)

    def test_same_stage_metadata_without_any_note_is_protected(self):
        _, item = self.piece()
        fields = json.loads(self.db.execute("SELECT fields FROM item WHERE id=?", (item,)).fetchone()[0])
        fields["writing"]["tip"] = "preserve public-API metadata edit"
        set_item_fields(self.db, item, fields=fields)
        before = "\n".join(self.db.iterdump())
        with self.assertRaisesRegex(RecoveryRefused, "later progress in fields"):
            reimport(self.db, str(self.repo), dry_run=True)
        self.assertEqual("\n".join(self.db.iterdump()), before)

    def test_a_rehearsal_label_is_not_immutable_proof_that_an_edit_is_disposable(self):
        _, item = self.work()
        self.retire_work()
        set_item_fields(self.db, item, title="rehearsal title")
        before = "\n".join(self.db.iterdump())
        with self.assertRaisesRegex(RecoveryRefused, "later progress in title"):
            reimport(self.db, str(self.repo), dry_run=True)
        self.assertEqual("\n".join(self.db.iterdump()), before)

    def test_apply_requires_a_fingerprint_and_changes_nothing_without_one(self):
        self.work()
        self.retire_work()
        before = "\n".join(self.db.iterdump())
        with self.assertRaisesRegex(RecoveryRefused, "requires the preview fingerprint"):
            reimport(self.db, str(self.repo))
        self.assertEqual("\n".join(self.db.iterdump()), before)
        self.assertEqual(self.apply()["authorities"], {"status_source": 1})
