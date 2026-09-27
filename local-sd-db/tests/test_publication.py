"""A real SQLite claim with connector-shaped failures and native readback."""

import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import paths
from sd_db import publication as pub
from sd_db.publication_render import md_to_html
from sd_db.schema import SCHEMA_DIR
from sd_db.workflow import WorkflowError
from sd_db.writes import record_state, resolve_state
from sd_db.writing import change_stage, piece_state, update_piece_metadata

from .test_writing import WritingCase


class MarkdownFences(unittest.TestCase):
    def test_shorter_or_mismatched_fences_remain_literal_code(self):
        for opening, inner, closing in (
            ("````text", "```", "````"),
            ("~~~~", "~~~", "~~~~~"),
            ("```", "~~~", "````"),
            ("~~~", "~~~~ trailing text", "~~~"),
        ):
            with self.subTest(opening=opening, inner=inner):
                rendered = md_to_html(f"{opening}\nalpha\n{inner}\nbeta\n{closing}\nafter", ".")
                self.assertIn(f"<pre><code>alpha\n{inner}\nbeta</code></pre>", rendered)
                self.assertRegex(rendered, r'</pre>\s*<p[^>]*>after</p>')

    def test_fence_after_paragraph_keeps_code_out_of_prose(self):
        rendered = md_to_html("before\n```text\n<a>& b\n```\nafter", ".")
        self.assertRegex(rendered, r'<p[^>]*>before</p>\s*<pre><code>&lt;a&gt;&amp; b</code></pre>')
        self.assertRegex(rendered, r'</pre>\s*<p[^>]*>after</p>')

    def test_short_fence_cannot_close_unterminated_long_fence(self):
        with self.assertRaisesRegex(WorkflowError, "unclosed fenced code block"):
            md_to_html("````\nalpha\n```", ".")


class Publication(WritingCase):
    def setUp(self):
        super().setUp()
        self.cutover()
        self.gates()
        change_stage(self.db, self.item, "ready", who="operator")
        self.context = {"profile": {"email": "writer@example.test"},
                        "drafts": {"id": "drafts", "title": "Drafts", "mime_type": pub.FOLDER},
                        "published": {"id": "published", "title": "Published", "mime_type": pub.FOLDER},
                        "expected": {"account": "writer@example.test", "drafts": "drafts", "published": "published"}}
        self.payload = pub.render_payload(self.db, self.item)

    def claim(self):
        return pub.create_claim(self.db, self.item, self.payload, self.context, who="operator")["claim"]

    def dispatch(self, claim):
        return pub.dispatch(self.db, self.item, claim, self.context, confirmed=True, who="operator")

    def imported(self, claim):
        operation = self.dispatch(claim)
        return pub.receipt(self.db, self.item, claim, operation["operation"],
                           {"success": True, "converted": True, "mimeType": pub.NATIVE, "fileId": "doc-1"})

    def evidence(self, claim, published=False):
        state = pub.claim_state(self.db, self.item, claim, include_payload=True)
        return {"metadata": {"id": "doc-1", "mime_type": pub.NATIVE,
                             "title": "A piece" if published else state["payload"]["staging_title"],
                             "parent_ids": ["published" if published else "root-id"],
                             "url": "https://docs.google.com/document/d/doc-1/edit"},
                "document": {"documentId": "doc-1", "tabs": [{"tabId": "t.0", "body": {"content": [
                    {"paragraph": {"elements": [{"textRun": {"content": "A piece\n"}}]}},
                    {"paragraph": {"elements": [{"textRun": {"content": "A claim.\n"}}]}}]}}]}}

    def verify(self, claim, published=False):
        return pub.reconcile(self.db, self.item, claim, self.context, self.evidence(claim, published), who="operator")

    def test_complete_publish_requires_readback_after_move(self):
        claim = self.claim()
        self.imported(claim)
        self.verify(claim)
        move = self.dispatch(claim)
        self.assertEqual(move["action"]["arguments"]["removeParents"], "root-id")
        pub.receipt(self.db, self.item, claim, move["operation"], {"success": True, "id": "doc-1"})
        self.assertEqual(piece_state(self.db, self.item)["item"]["stage"], "ready")
        result = self.verify(claim, published=True)
        self.assertEqual(result["phase"], "published")
        self.assertEqual(piece_state(self.db, self.item)["item"]["stage"], "published")
        self.assertFalse(result["active"])
        self.assertEqual(self.verify(claim, published=True), result)

    def test_lost_import_never_automatically_creates_again(self):
        claim = self.claim()
        self.assertTrue(self.dispatch(claim)["execute"])
        repeat = self.dispatch(claim)
        self.assertFalse(repeat["execute"])
        with self.assertRaisesRegex(WorkflowError, "uncertain"):
            pub.reconcile(self.db, self.item, claim, self.context, {"search_pages": [{"results": [], "next_page_token": None}]}, who="operator")
        self.assertFalse(self.dispatch(claim)["execute"])
        title = pub.claim_state(self.db, self.item, claim, include_payload=True)["payload"]["staging_title"]
        pub.reconcile(self.db, self.item, claim, self.context, {"search_pages": [{"results": [{"id": "doc-1", "title": title, "mime_type": pub.NATIVE}]}]}, who="operator")
        self.verify(claim)
        self.assertEqual(self.dispatch(claim)["action"]["tool"], "google_drive_update_file")

    def test_lost_move_is_resolved_by_parent_and_content_readback(self):
        claim = self.claim()
        self.imported(claim)
        self.verify(claim)
        self.dispatch(claim)
        self.assertFalse(self.dispatch(claim)["execute"])
        with self.assertRaisesRegex(WorkflowError, "move remains uncertain"):
            self.verify(claim)
        self.assertEqual(self.verify(claim, published=True)["phase"], "published")

    def test_forged_html_cannot_borrow_the_current_source_hashes(self):
        self.payload["html"] = '<h1>A piece</h1><p>A claim.</p><img src="data:image/png;base64,YQ==" width="10" height="10"><a href="https://example.test">Source</a>'
        with self.assertRaisesRegex(WorkflowError, "canonical"):
            self.claim()

    def test_truncated_native_text_cannot_verify_or_publish(self):
        claim = self.claim()
        self.imported(claim)
        evidence = self.evidence(claim)
        evidence["document"]["tabs"][0]["body"]["content"].pop()
        with self.assertRaisesRegex(WorkflowError, "text"):
            pub.reconcile(self.db, self.item, claim, self.context, evidence, who="operator")
        self.assertEqual(pub.claim_state(self.db, self.item, claim)["phase"], "created")
        self.assertEqual(piece_state(self.db, self.item)["item"]["stage"], "ready")

    def test_source_changes_before_dispatch_and_move_are_refused(self):
        claim = self.claim()
        self.imported(claim)
        self.verify(claim)
        self.index.write_text(self.index.read_text() + "Edited.\n")
        with self.assertRaises(WorkflowError):
            self.dispatch(claim)
        self.assertEqual(pub.claim_state(self.db, self.item, claim)["phase"], "verified")

    def test_change_after_move_never_fabricates_current_published_state(self):
        claim = self.claim()
        self.imported(claim)
        self.verify(claim)
        self.dispatch(claim)
        self.index.write_text(self.index.read_text() + "Edited.\n")
        result = self.verify(claim, published=True)
        self.assertEqual(result["phase"], "published")
        self.assertFalse(result["current_draft_published"])
        self.assertEqual(piece_state(self.db, self.item)["item"]["stage"], "review")

    def test_active_claim_blocks_writers_and_database_enforces_uniqueness(self):
        claim = self.claim()
        with self.assertRaisesRegex(WorkflowError, "owns"):
            update_piece_metadata(self.db, self.item, {"tip": "new tip"}, who="operator")
        with self.assertRaisesRegex(WorkflowError, "owns"):
            self.claim()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO publication_claim SELECT 'duplicate',item,active_item,payload,state,created_at,updated_at FROM publication_claim WHERE id=?", (claim,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.db.execute("UPDATE publication_claim SET payload='{}' WHERE id=?", (claim,))

    def test_restore_blocks_old_claim(self):
        claim = self.claim()
        record_state(self.db, "restore", body={"from": "older backup"})
        with self.assertRaisesRegex(WorkflowError, "restore"):
            self.dispatch(claim)

    def test_claim_context_and_payload_must_be_exact(self):
        self.context["published"]["title"] = "Drafts"
        with self.assertRaisesRegex(WorkflowError, "Published"):
            self.claim()
        self.context["published"]["title"] = "Published"
        self.payload["source_hashes"] = {}
        with self.assertRaisesRegex(WorkflowError, "canonical"):
            self.claim()

    def test_concrete_confirmation_and_receipt_identity(self):
        claim = self.claim()
        with self.assertRaisesRegex(WorkflowError, "confirm"):
            pub.dispatch(self.db, self.item, claim, self.context, who="operator")
        dispatch = self.dispatch(claim)
        with self.assertRaisesRegex(WorkflowError, "pending"):
            pub.receipt(self.db, self.item, claim, "wrong", {"success": True})
        response = {"success": True, "converted": True, "mimeType": pub.NATIVE, "fileId": "doc-1"}
        first = pub.receipt(self.db, self.item, claim, dispatch["operation"], response)
        self.assertEqual(pub.receipt(self.db, self.item, claim, dispatch["operation"], response)["phase"], first["phase"])
        with self.assertRaisesRegex(WorkflowError, "different receipt"):
            pub.receipt(self.db, self.item, claim, dispatch["operation"], {**response, "fileId": "other"})

    def test_abandon_before_dispatch_releases_and_uncertain_leave_retains_barrier(self):
        claim = self.claim()
        pub.abandon(self.db, self.item, claim, reason="Revise before sending", who="operator")
        next_claim = self.claim()
        self.dispatch(next_claim)
        with self.assertRaisesRegex(WorkflowError, "leave"):
            pub.abandon(self.db, self.item, next_claim, reason="Interrupted", who="operator")
        pub.abandon(self.db, self.item, next_claim, reason="Leave document for operator", leave=True, who="operator")
        with self.assertRaisesRegex(WorkflowError, "uncertain"):
            self.claim()

    def test_journal_survives_database_restore_and_recovers_known_publication(self):
        old = sqlite3.connect(":memory:")
        self.addCleanup(old.close)
        self.db.backup(old)
        claim = self.claim()
        self.imported(claim)
        self.verify(claim)
        self.dispatch(claim)
        self.verify(claim, published=True)
        # Restored into the file, as `restore` does, and not into the
        # library's connection: a remote connection cannot be a backup target.
        restoring = sqlite3.connect(self.path, isolation_level=None)
        old.backup(restoring)
        restoring.close()
        restored = record_state(self.db, "restore", body={"snapshot": "before-publication"})
        resolve_state(self.db, restored)
        with self.assertRaisesRegex(WorkflowError, "newer evidence"):
            self.claim()
        self.assertEqual(pub.recover_journal(self.db, self.item, who="operator")["recovered"], [claim])
        self.assertFalse(self.dispatch(claim)["execute"])
        self.assertEqual(self.verify(claim, published=True)["phase"], "published")

    def test_a_journal_written_before_migration_014_still_guards_the_keyed_row(self):
        """sd:1450: the journal names the absolute path, 014 keyed the row."""
        old = sqlite3.connect(":memory:")
        self.addCleanup(old.close)
        self.db.backup(old)
        claim = self.claim()
        self.imported(claim)
        self.verify(claim)
        self.dispatch(claim)
        self.verify(claim, published=True)
        # Restored into the file, as `restore` does, and not into the
        # library's connection: a remote connection cannot be a backup target.
        restoring = sqlite3.connect(self.path, isolation_level=None)
        old.backup(restoring)
        restoring.close()
        restored = record_state(self.db, "restore", body={"snapshot": "before-publication"})
        resolve_state(self.db, restored)
        # The repository now sits under `$HOME`, and 014 runs on the store.
        # It keys every repository column and leaves the journal as written.
        home = patch.dict(os.environ, {"HOME": str(self.root)})
        home.start()
        self.addCleanup(home.stop)
        # On the file, as `migrate` runs it: the two functions 014 calls are
        # Python on this side and cannot be registered on a remote connection.
        migrating = sqlite3.connect(self.path, isolation_level=None)
        paths.install(migrating)
        migrating.executescript((SCHEMA_DIR / "014_home_relative_repo_paths.sql").read_text(encoding="utf-8"))
        migrating.close()
        self.assertEqual(piece_state(self.db, self.item)["item"]["repo"], "~/writing")
        with self.assertRaisesRegex(WorkflowError, "newer evidence"):
            self.claim()
        self.assertEqual(pub.recover_journal(self.db, self.item, who="operator")["recovered"], [claim])
        self.assertFalse(self.dispatch(claim)["execute"])
        self.assertEqual(self.verify(claim, published=True)["phase"], "published")

    def test_write_ahead_journal_recovers_database_commit_failure_without_reimport(self):
        claim = self.claim()
        with patch.object(pub, "_save", side_effect=lambda conn, cid, state, **kw: (
                pub.journal.append(conn, cid, state), (_ for _ in ()).throw(RuntimeError("crash")))), self.assertRaisesRegex(RuntimeError, "crash"):
            self.dispatch(claim)
        with self.assertRaisesRegex(WorkflowError, "ahead"):
            self.dispatch(claim)
        pub.recover_journal(self.db, self.item, who="operator")
        self.assertFalse(self.dispatch(claim)["execute"])

    def test_missing_or_corrupt_journal_cannot_clear_with_confirmation(self):
        claim = self.claim()
        path = pub.journal.root(self.db) / claim / "00000001.json"
        path.write_text("broken")
        with self.assertRaisesRegex(WorkflowError, "corrupt"):
            self.dispatch(claim)

    def test_missing_journal_after_restore_refuses_new_claim(self):
        restored = record_state(self.db, "restore", body={})
        resolve_state(self.db, restored)
        with self.assertRaisesRegex(WorkflowError, "journal is missing"):
            self.claim()

    def test_native_flattened_tab_readback_preserves_image_order_and_captions(self):
        html = '<h1>Title</h1><p>Evidence.</p><p><img src="data:image/png;base64,YQ==" width="20" height="10"><br><em>Caption.</em></p>'
        native = {"documentId": "doc", "tabs": [{"tabId": "t.0", "body": {"content": [
            {"paragraph": {"elements": [{"textRun": {"content": "Title\n"}}]}},
            {"paragraph": {"elements": [{"textRun": {"content": "Evidence.\n"}}]}},
            {"paragraph": {"elements": [{"inlineObjectElement": {"inlineObjectId": "image1"}},
                                         {"textRun": {"content": "\nCaption.\n"}}]}}]},
            "inlineObjects": {"image1": {"inlineObjectProperties": {"embeddedObject": {
                "imageProperties": {"contentUri": "https://lh3.googleusercontent.com/fixture"},
                "size": {"width": {"magnitude": 15, "unit": "PT"}, "height": {"magnitude": 7.5, "unit": "PT"}}}}}}}]}
        expected = pub.html_inventory(html)
        actual = pub._document_inventory(native)
        self.assertEqual(actual["text"], "Title Evidence. Caption.")
        for key in ("text", "images", "image_positions", "image_aspects", "links"):
            self.assertEqual(actual[key], expected[key])
        native["tabs"][0]["inlineObjects"] = {}
        with self.assertRaisesRegex(WorkflowError, "missing or unrendered"):
            pub._document_inventory(native)

    def test_malformed_table_line_renders_with_bounded_runtime(self):
        result = subprocess.run([sys.executable, "-c", "from sd_db.publication_render import md_to_html; print(md_to_html('| ordinary prose', '/tmp'))"],
                                capture_output=True, text=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| ordinary prose", result.stdout)

    def test_interrupted_journal_initialization_or_claim_creation_has_explicit_repair(self):
        original = pub.journal._write_new
        for fail_after in (1, 2, 3, 4):
            with self.subTest(fail_after=fail_after):
                # Each case uses a separate DB and its own journal directory.
                folder = self.root / ("fault-" + str(fail_after))
                folder.mkdir()
                database = folder / "sd.db"
                other = sqlite3.connect(database, isolation_level=None)
                other.row_factory = sqlite3.Row
                self.db.backup(other)
                count = 0

                def fail(path, data, stop=fail_after):
                    nonlocal count
                    original(path, data)
                    count += 1
                    if count == stop:
                        raise RuntimeError("killed after durable write")

                with patch.object(pub.journal, "_write_new", side_effect=fail), self.assertRaisesRegex(RuntimeError, "killed"):
                    pub.create_claim(other, self.item, self.payload, self.context, who="operator")
                result = pub.recover_journal(other, self.item, repair_incomplete=True, who="operator")
                self.assertTrue(result["archived"] or fail_after == 2)
                for preserved in result["archived"]:
                    self.assertTrue(Path(preserved).exists())
                pub.create_claim(other, self.item, self.payload, self.context, who="operator")
                other.close()

    def test_incomplete_dispatched_journal_remains_held(self):
        claim = self.claim()
        self.dispatch(claim)
        (pub.journal.root(self.db) / claim / "00000002.json").write_text("partial event")
        with self.assertRaisesRegex(WorkflowError, "corrupt"):
            pub.recover_journal(self.db, self.item, repair_incomplete=True, who="operator")

    def test_partial_catalog_create_repairs_without_erasing_evidence(self):
        original = pub.journal._write_new

        def interrupted(path, data):
            original(path, data)
            if path.name == "00000001.json":
                (path.parent.parent / "claims.jsonl").write_bytes(b'{"claim":')
                raise RuntimeError("catalog write interrupted")

        with patch.object(pub.journal, "_write_new", side_effect=interrupted), self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.claim()
        result = pub.recover_journal(self.db, self.item, repair_incomplete=True, who="operator")
        self.assertEqual(len(result["archived"]), 2)
        preserved_catalog = next(Path(path) for path in result["archived"] if path.endswith("claims.jsonl"))
        self.assertEqual(preserved_catalog.read_bytes(), b'{"claim":')
        self.assertEqual(pub.journal.validate_path(pub.journal.root(self.db)), {})
        self.claim()

    def test_restore_merge_intent_blocks_dispatch_and_incomplete_prefix_repair(self):
        claim = self.claim()
        intent = pub.journal.root(self.db).parent / "publication-restore-intent.json"
        intent.write_text('{"snapshot": "must-resume-this-restore"}')
        with self.assertRaisesRegex(WorkflowError, "restore is incomplete"):
            self.dispatch(claim)
        with self.assertRaisesRegex(WorkflowError, "restore is incomplete"):
            pub.recover_journal(self.db, self.item, repair_incomplete=True, who="operator")
        self.assertTrue(intent.exists())
        self.assertEqual(pub.claim_state(self.db, self.item, claim)["phase"], "claimed")

    def test_malformed_journal_shapes_are_integrity_refusals(self):
        claim = self.claim()
        base = pub.journal.root(self.db)
        targets = {base / "claims.jsonl": b'[]\n',
                   base / claim / "manifest.json": b'[]',
                   base / claim / "00000001.json": b'[]'}
        for target, malformed in targets.items():
            with self.subTest(target=target.name):
                original = target.read_bytes()
                target.write_bytes(malformed)
                with self.assertRaises(WorkflowError):
                    pub.journal.validate_path(base)
                target.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
