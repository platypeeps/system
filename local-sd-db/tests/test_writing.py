"""One writing lifecycle, snapshot-bound reviews, and reversible cutover."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, upsert_repo
from sd_db.migrate import initialise
from sd_db.testing.wire import hub_only
from sd_db.workflow import StaleItem, WorkflowError
from sd_db.writing import (
    change_stage, cutover_pieces, cutover_preview, import_piece, list_pieces,
    piece_for_key, piece_state, preflight, record_gate, update_piece_metadata,
    verify_pieces, readiness, recover_cutover, park_piece, checkout, piece_files, promote,
)
from sd_db.writes import create_item, now


class WritingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "writing"
        self.piece = "2026/a-piece"
        self.folder = self.repo / "content" / self.piece
        self.folder.mkdir(parents=True)
        self.index = self.folder / "index.md"
        self.index.write_text('---\ntitle: "A piece"\nstatus: review\nupdated: 2026-09-08\npublished: null\n'
                              'published_urls:\n  gdrive: null\nreview_urls:\n  gdocs: null\n---\n\n## Draft\nA claim.\n')
        self.digest = hashlib.sha1("A claim.".encode()).hexdigest()[:12]
        (self.folder / "research.md").write_text(f"<!-- reconciled-with-draft: {self.digest} on 2026-09-08 -->\nSource.\n")
        (self.folder / "fact-check.md").write_text("Full claim ledger.\n")
        (self.folder / "adversarial.md").write_text("Full adversarial report.\n")
        self.path = self.root / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, str(self.repo))
        self.item = import_piece(self.db, str(self.repo), self.piece, who="operator")["item"]["id"]

    def gates(self):
        for artifact in ("fact-check", "adversarial"):
            record_gate(self.db, self.item, artifact, verdict="pass", findings=[], reason="Reviewed full report", reviewed_digest=self.digest, who="operator")

    def cutover(self):
        preview = cutover_preview(self.db, str(self.repo))
        return cutover_pieces(self.db, str(self.repo), expected_fingerprint=preview["fingerprint"], who="operator")


class PieceFiles(WritingCase):
    def row(self):
        return self.db.execute("SELECT * FROM item WHERE id = ?", (self.item,)).fetchone()

    def test_each_file_gives_its_size_and_an_absent_one_none(self):
        (self.folder / "fact-check.md").unlink()
        found = piece_files(self.row())
        self.assertEqual(sorted(found), ["adversarial.md", "fact-check.md", "index.md", "research.md"])
        self.assertIsNone(found["fact-check.md"])
        self.assertEqual(found["adversarial.md"]["bytes"], len("Full adversarial report.\n"))
        self.assertRegex(found["index.md"]["mtime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_a_report_linked_out_of_the_repository_is_not_listed(self):
        outside = self.root / "outside.md"
        outside.write_text("elsewhere\n")
        (self.folder / "research.md").unlink()
        (self.folder / "research.md").symlink_to(outside)
        self.assertIsNone(piece_files(self.row())["research.md"])


class SharedTransitions(WritingCase):
    def test_backfill_accepts_null_defaults_and_identical_metadata_has_no_churn(self):
        first = update_piece_metadata(self.db, self.item, {
            "published_urls": {"gdrive": None, "blog": None},
            "review_urls": {"gdocs_digest": None},
        }, who="operator")
        second = update_piece_metadata(self.db, self.item, {
            "published_urls": {"gdrive": None, "blog": None},
            "review_urls": {"gdocs_digest": None},
        }, who="operator")
        self.assertEqual(first, second)

    def title_notes(self):
        return [row[0] for row in self.db.execute(
            "SELECT body FROM note WHERE item = ? AND body LIKE 'Title changed%' ORDER BY id", (self.item,))]

    def test_a_title_change_reaches_the_row_the_file_and_the_history(self):
        """sd:2368: the title was not editable metadata. A file-owned piece
        carries it in the frontmatter, so the file and the row move together
        and verification still matches them; the note keeps the old title."""
        state = update_piece_metadata(self.db, self.item, {"title": "A # better: piece"}, who="operator")
        self.assertEqual(state["item"]["title"], "A # better: piece")
        self.assertEqual(state["writing"]["metadata"]["title"], "A # better: piece")
        self.assertIn('title: "A # better: piece"\n', self.index.read_text())
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])
        self.assertEqual(self.title_notes(), ["Title changed from 'A piece' to 'A # better: piece' by operator"])
        # The same title again is no change and writes no note.
        self.assertEqual(update_piece_metadata(self.db, self.item, {"title": "A # better: piece"}, who="operator"), state)
        self.assertEqual(len(self.title_notes()), 1)

    def test_a_blank_or_multiline_title_is_refused_and_writes_nothing(self):
        before, source = piece_state(self.db, self.item), self.index.read_bytes()
        for bad in ("", "   ", None, 7, "two\nlines", "tab\tinside"):
            with self.subTest(title=bad), self.assertRaises(WorkflowError):
                update_piece_metadata(self.db, self.item, {"title": bad}, who="operator")
        self.assertEqual(piece_state(self.db, self.item), before)
        self.assertEqual(self.index.read_bytes(), source)

    def test_gate_recording_requires_the_actual_reviewed_digest_and_deduplicates(self):
        before = piece_state(self.db, self.item)
        with self.assertRaisesRegex(WorkflowError, "reviewed draft"):
            record_gate(self.db, self.item, "fact-check", verdict="pass", findings=[], reason="Reviewed", who="operator")
        self.assertEqual(piece_state(self.db, self.item), before)
        first = record_gate(self.db, self.item, "fact-check", verdict="pass", findings=[], reason="Reviewed", reviewed_digest=self.digest, who="operator")
        self.assertEqual(record_gate(self.db, self.item, "fact-check", verdict="pass", findings=[], reason="Reviewed", reviewed_digest=self.digest, who="operator"), first)
        (self.folder / "fact-check.md").write_text("<!-- reconciled-with-draft: 000000000000 on 2026-09-08 -->\nOld report")
        with self.assertRaisesRegex(WorkflowError, "stale"):
            record_gate(self.db, self.item, "fact-check", verdict="pass", findings=[], reason="Reviewed", reviewed_digest=self.digest, who="operator")

    def test_readiness_reports_problems_before_ready_and_document_matches_checked_draft(self):
        self.assertFalse(readiness(self.db, self.item)["ok"])
        self.index.write_text(self.index.read_text().replace("A claim.", "Current uncommitted prose."))
        state = piece_state(self.db, self.item)
        self.assertIn("Current uncommitted prose", state["writing"]["document"])
        self.assertFalse(state["writing"]["gates"]["ok"])

    def test_template_comments_quoted_hashes_and_quoted_commas_survive_import(self):
        source = self.index.read_text().replace('title: "A piece"', 'title: "A # piece"')
        source = source.replace("published_urls:", "published_urls: # actual targets")
        source = source.replace("published: null", "published: null # unknown date")
        source = source.replace("review_urls:", 'tip: null # not attached\ncustom: ["a,b", "c"]\nreview_urls:')
        self.index.write_text(source)
        state = import_piece(self.db, str(self.repo), self.piece, who="operator")
        metadata = state["writing"]["metadata"]
        self.assertEqual(metadata["title"], "A # piece")
        self.assertIsNone(metadata["tip"])
        self.assertEqual(metadata["custom"], ["a,b", "c"])
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])

    def test_dashboard_cannot_use_file_owner_as_a_write_bypass(self):
        before = self.index.read_bytes()
        with self.assertRaisesRegex(WorkflowError, "ownership"):
            change_stage(self.db, self.item, "drafting", correct=True, reason="Correction", require_row=True, who="operator")
        with self.assertRaisesRegex(WorkflowError, "ownership"):
            park_piece(self.db, self.item, require_row=True, who="operator")
        self.assertIsNone(piece_state(self.db, self.item)["item"]["parked_at"])
        self.assertEqual(self.index.read_bytes(), before)

    def test_import_is_idempotent_and_piece_identity_unique(self):
        again = import_piece(self.db, str(self.repo), self.piece, who="operator")
        self.assertEqual(again["item"]["id"], self.item)
        self.assertEqual(len(list_pieces(self.db, str(self.repo))), 1)
        self.assertEqual(piece_for_key(self.db, str(self.repo), self.piece)["id"], self.item)

    def test_ready_needs_current_explicit_gate_results(self):
        with self.assertRaisesRegex(WorkflowError, "gate|review"):
            change_stage(self.db, self.item, "ready", who="operator")
        self.gates()
        state = change_stage(self.db, self.item, "ready", who="operator")
        self.assertEqual((state["item"]["stage"], state["item"]["status"]), ("ready", "ready_to_send"))
        self.assertEqual(state["item"]["ready_digest"], self.digest)
        self.assertIn("status: ready", self.index.read_text())

    def test_ready_refuses_failed_or_unresolved_reviews(self):
        self.gates()
        record_gate(self.db, self.item, "adversarial", verdict="pass",
                    findings=[{"id": "A1", "confidence": "CERTAIN", "disposition": "open"}], reason="Unresolved issue", reviewed_digest=self.digest, who="operator")
        with self.assertRaisesRegex(WorkflowError, "A1"):
            change_stage(self.db, self.item, "ready", who="operator")
        record_gate(self.db, self.item, "adversarial", verdict="pass",
                    findings=[{"id": "A1", "confidence": "CERTAIN", "disposition": "rebutted", "evidence": "Source explains it"}], reason="Verified source", reviewed_digest=self.digest, who="operator")
        record_gate(self.db, self.item, "fact-check", verdict="fail", findings=[], reason="NO verdict", reviewed_digest=self.digest, who="operator")
        with self.assertRaisesRegex(WorkflowError, "fact-check"):
            change_stage(self.db, self.item, "ready", who="operator")

    def test_report_or_draft_edits_invalidate_gate_results(self):
        self.gates()
        (self.folder / "fact-check.md").write_text("New findings")
        with self.assertRaisesRegex(WorkflowError, "changed|stale"):
            change_stage(self.db, self.item, "ready", who="operator")
        self.gates()
        self.index.write_text(self.index.read_text().replace("A claim.", "A revised claim."))
        with self.assertRaises(WorkflowError):
            change_stage(self.db, self.item, "ready", who="operator")

    def test_correction_is_human_owned_and_invalidates_prior_generation(self):
        self.gates()
        change_stage(self.db, self.item, "ready", who="operator")
        with self.assertRaises(WorkflowError):
            change_stage(self.db, self.item, "drafting", actor="session", correct=True, reason="Correction", who="operator")
        with self.assertRaises(WorkflowError):
            change_stage(self.db, self.item, "drafting", correct=True, who="operator")
        state = change_stage(self.db, self.item, "drafting", correct=True, reason="Research changed", who="operator")
        self.assertEqual(state["item"]["gate_generation"], 1)
        self.assertIsNone(state["item"]["ready_digest"])
        change_stage(self.db, self.item, "review", who="operator")
        with self.assertRaises(WorkflowError):
            change_stage(self.db, self.item, "ready", who="operator")

    def test_publication_requires_ready_digest_confirmation_and_actual_url(self):
        self.gates()
        change_stage(self.db, self.item, "ready", who="operator")
        with self.assertRaises(WorkflowError):
            change_stage(self.db, self.item, "published", who="operator")
        with self.assertRaises(WorkflowError):
            change_stage(self.db, self.item, "published", confirmed=True, who="operator")
        update_piece_metadata(self.db, self.item, {"published_urls": {"gdrive": "https://docs.google.com/document/d/actual"}}, who="operator")
        with self.assertRaisesRegex(WorkflowError, "verified claim"):
            change_stage(self.db, self.item, "published", confirmed=True, who="operator")
        self.assertEqual(piece_state(self.db, self.item)["writing"]["metadata"]["publication_url_provenance"]["gdrive"]["kind"], "manual-unverified")
        update_piece_metadata(self.db, self.item, {"published_urls": {"blog": "https://blog.example.com/actual"}}, who="operator")
        state = change_stage(self.db, self.item, "published", confirmed=True, who="operator")
        self.assertEqual(state["writing"]["metadata"]["publication_confirmation"]["kind"], "manual-acknowledgement")
        self.assertEqual(state["item"]["status"], "done")
        self.assertTrue(state["item"]["shipped_at"])

    def test_preflight_rechecks_after_ready(self):
        self.gates()
        change_stage(self.db, self.item, "ready", who="operator")
        self.assertTrue(preflight(self.db, self.item)["writing"]["gates"]["ok"])
        self.index.write_text(self.index.read_text().replace("A claim.", "Changed."))
        with self.assertRaises(WorkflowError):
            preflight(self.db, self.item)


class Cutover(WritingCase):
    @hub_only
    def test_registration_after_cutover_retires_new_template_bookkeeping_once(self):
        self.cutover()
        new = self.repo / "content/2026/new/index.md"
        new.parent.mkdir(parents=True)
        original = b'---\ntitle: New piece\nstatus: idea\npublished: null\n---\n\n## Draft\nFuture prose.\n'
        new.write_bytes(original)
        result = import_piece(self.db, str(self.repo), "2026/new", who="operator")
        self.assertEqual(result["item"]["stage"], "accepted")
        self.assertNotIn(b"status:", new.read_bytes())
        self.assertIn(b"Future prose", new.read_bytes())
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])
        self.assertEqual(json.loads(result["item"]["body"])["source"].encode(), original)

    def test_duplicate_piece_keys_are_enforced_by_the_database(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO item(kind,repo,piece,title,status,created_at,updated_at) VALUES('idea',?,?,?,'planning','','')",
                            (str(self.repo), self.piece, "Duplicate"))

    def test_verification_reports_metadata_and_body_loss_with_actual_row_count(self):
        self.db.execute("UPDATE item SET fields = '{}', body = '{}' WHERE id = ?", (self.item,))
        report = verify_pieces(self.db, str(self.repo))
        self.assertFalse(report["ok"])
        self.assertEqual(report["rows"], 1)
        self.assertTrue(any("metadata" in difference for difference in report["differences"]))
        self.assertTrue(any("body" in difference for difference in report["differences"]))

    @hub_only
    def test_killed_cutover_restores_from_durable_journal(self):
        before = self.index.read_bytes()
        script = """import os, sys
from sd_db import connect
from sd_db import writing
c = connect(sys.argv[1])
p = writing.cutover_preview(c, sys.argv[2])
writing._write_marker = lambda repo: os._exit(86)
writing.cutover_pieces(c, sys.argv[2], expected_fingerprint=p['fingerprint'], who='operator')
"""
        package = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "-c", script, str(self.path), str(self.repo)],
                                env={**os.environ, "PYTHONPATH": str(package)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 86, result.stderr)
        self.assertNotEqual(self.index.read_bytes(), before)
        with self.assertRaisesRegex(WorkflowError, "unfinished"):
            cutover_preview(self.db, str(self.repo))
        recovered = recover_cutover(self.db, str(self.repo), who="operator")
        self.assertEqual(recovered["recovered"], "rolled_back")
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(piece_state(self.db, self.item)["writing"]["owner"], "file")

    @hub_only
    def test_failed_cutover_preserves_concurrent_edit_and_names_recovery_backup(self):
        preview = cutover_preview(self.db, str(self.repo))
        changed = b"Concurrent user edit\n"

        def concurrent_change(repo):
            self.index.write_bytes(changed)
            raise OSError("stop")

        with patch("sd_db.writing._write_marker", side_effect=concurrent_change):
            with self.assertRaisesRegex(WorkflowError, "preserved concurrent edits.*backup"):
                cutover_pieces(self.db, str(self.repo), expected_fingerprint=preview["fingerprint"], who="operator")
        self.assertEqual(self.index.read_bytes(), changed)

    @hub_only
    def test_cutover_records_verified_authority_receipt(self):
        self.cutover()
        receipt = self.db.execute("SELECT * FROM state WHERE kind='verified' AND key=?", (f"{self.repo}:pieces_source",)).fetchone()
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt["resolved_at"])
        self.assertEqual(json.loads(receipt["body"])["rows"], 1)

    @hub_only
    def test_retired_status_and_metadata_edits_leave_all_source_bytes_unchanged(self):
        self.gates()
        change_stage(self.db, self.item, "ready", who="operator")
        self.cutover()
        before = {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        update_piece_metadata(self.db, self.item, {"tip": "Approved tip", "review_urls": {"gdocs": "https://docs.google.com/document/d/review"},
                                                 "published_urls": {"blog": "https://blog.example.com/live"}}, who="operator")
        renamed = update_piece_metadata(self.db, self.item, {"title": "Row-owned title"}, who="operator")
        self.assertEqual(renamed["item"]["title"], "Row-owned title")
        change_stage(self.db, self.item, "published", confirmed=True, who="operator")
        after = {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])

    @hub_only
    def test_preview_writes_nothing_and_cutover_status_writes_only_database(self):
        before = self.index.read_bytes()
        state = piece_state(self.db, self.item)
        preview = cutover_preview(self.db, str(self.repo))
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(piece_state(self.db, self.item), state)
        result = cutover_pieces(self.db, str(self.repo), expected_fingerprint=preview["fingerprint"], who="operator")
        self.assertTrue(result["ok"])
        retired = self.index.read_bytes()
        self.assertNotIn(b"status:", retired)
        self.assertIn(b"A claim.", retired)
        change_stage(self.db, self.item, "drafting", correct=True, reason="Rework", who="operator")
        self.assertEqual(self.index.read_bytes(), retired)
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])

    @hub_only
    def test_cutover_refuses_changed_files_without_changing_row_owner(self):
        preview = cutover_preview(self.db, str(self.repo))
        self.index.write_text(self.index.read_text() + "Changed after preview\n")
        with self.assertRaises(StaleItem):
            cutover_pieces(self.db, str(self.repo), expected_fingerprint=preview["fingerprint"], who="operator")
        self.assertEqual(piece_state(self.db, self.item)["writing"]["owner"], "file")

    @hub_only
    def test_cutover_failure_restores_exact_files_and_database(self):
        before = self.index.read_bytes()
        preview = cutover_preview(self.db, str(self.repo))
        with patch("sd_db.writing._write_marker", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cutover_pieces(self.db, str(self.repo), expected_fingerprint=preview["fingerprint"], who="operator")
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(piece_state(self.db, self.item)["writing"]["owner"], "file")

    @hub_only
    def test_row_owner_import_never_overwrites_current_database_stage(self):
        self.cutover()
        change_stage(self.db, self.item, "drafting", correct=True, reason="Rework", who="operator")
        with self.assertRaises(WorkflowError):
            import_piece(self.db, str(self.repo), self.piece, who="operator")
        self.assertEqual(piece_state(self.db, self.item)["item"]["stage"], "drafting")

    @hub_only
    def test_parked_pieces_are_preserved_and_excluded_from_default_list(self):
        parked = self.repo / "content-parked/2026/parked/index.md"
        parked.parent.mkdir(parents=True)
        parked.write_bytes(self.index.read_bytes())
        import_piece(self.db, str(self.repo), "2026/parked", path="content-parked/2026/parked/index.md", who="operator")
        self.assertEqual(len(list_pieces(self.db, str(self.repo))), 1)
        self.assertEqual(len(list_pieces(self.db, str(self.repo), include_parked=True)), 2)
        self.cutover()
        self.assertTrue(parked.exists())


TEMPLATE = ('---\ntitle: ""\ntype: blog        # blog | research | article\nstatus: idea       # idea | drafting\n'
            'created: YYYY-MM-DD\nupdated: YYYY-MM-DD\npublished: null\npublished_urls:      # filled on publish\n'
            '  gdrive: null\ntags: []\n---\n\n## Notes / angle\n\n## Draft\n')


class Promote(WritingCase):
    """An idea row becomes a piece in place, scaffolded from the repository's template (sd:1994)."""

    def setUp(self):
        super().setUp()
        (self.repo / "templates").mkdir()
        (self.repo / "templates/piece-template.md").write_text(TEMPLATE)
        self.idea = create_item(self.db, kind="idea", title="An idea: Über café!", source="vault",
                                external_id="Blog Ideas/an-idea.md", body={"text": "The angle."})
        self.year = now()[:4]

    def file(self, slug="an-idea-uber-cafe", root=None):
        return (root or self.repo) / "content" / self.year / slug / "index.md"

    def test_the_idea_row_becomes_the_piece_scaffolded_from_the_template(self):
        state = promote(self.db, self.idea, who="operator")
        row = state["item"]
        self.assertEqual(row["id"], self.idea)
        self.assertEqual(row["piece"], f"{self.year}/an-idea-uber-cafe")
        self.assertEqual((row["stage"], row["status"], row["source"]), ("accepted", "ready", "writing-piece"))
        text = self.file().read_text()
        self.assertIn('title: "An idea: Über café!"\n', text)
        self.assertIn(f"created: {now()[:10]}\n", text)
        self.assertIn("type: blog        # blog | research | article\n", text)
        self.assertTrue(text.endswith("## Notes / angle\n\n## Draft\n"))
        self.assertEqual(json.loads(row["body"])["text"], "The angle.")
        self.assertEqual(json.loads(row["fields"])["promoted_from"],
                         {"source": "vault", "external_id": "Blog Ideas/an-idea.md"})
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])

    def test_a_slug_override_names_the_piece(self):
        self.assertEqual(promote(self.db, self.idea, slug="short", who="operator")["item"]["piece"], f"{self.year}/short")
        self.assertTrue(self.file("short").is_file())

    def test_refusals_change_neither_the_row_nor_the_files(self):
        promote(self.db, create_item(self.db, kind="idea", title="Taken"), who="operator")
        taken = self.file("taken").read_bytes()
        parked = self.repo / "content-parked" / self.year / "parked" / "index.md"
        parked.parent.mkdir(parents=True)
        parked.write_text("held\n")
        before = piece_state(self.db, self.item)
        task = create_item(self.db, kind="task", title="Not an idea")
        declined = create_item(self.db, kind="idea", title="Declined", status="done")
        for item, slug, message in ((self.idea, "../escape", "YEAR/SLUG"), (self.idea, "taken", "already"),
                                    (self.idea, "parked", "already"), (task, None, "not an idea"),
                                    (self.item, None, "already"), (declined, None, "done")):
            with self.subTest(slug=slug, message=message), self.assertRaisesRegex(WorkflowError, message):
                promote(self.db, item, slug=slug, who="operator")
        self.assertIsNone(self.db.execute("SELECT piece FROM item WHERE id = ?", (self.idea,)).fetchone()[0])
        self.assertEqual(self.file("taken").read_bytes(), taken)
        self.assertEqual(parked.read_text(), "held\n")
        self.assertEqual(piece_state(self.db, self.item), before)

    def test_a_title_with_no_slug_text_needs_an_explicit_slug(self):
        idea = create_item(self.db, kind="idea", title="???")
        with self.assertRaisesRegex(WorkflowError, "slug"):
            promote(self.db, idea, who="operator")

    def test_a_missing_template_is_refused_by_name(self):
        (self.repo / "templates/piece-template.md").unlink()
        with self.assertRaisesRegex(WorkflowError, "templates/piece-template.md"):
            promote(self.db, self.idea, who="operator")
        self.assertFalse(self.file().parent.exists())

    def test_a_content_folder_linked_out_of_the_repository_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / "content" / self.year).mkdir(exist_ok=True)
        (self.repo / "content" / self.year).rename(outside / self.year)
        (self.repo / "content" / self.year).symlink_to(outside / self.year)
        with self.assertRaisesRegex(WorkflowError, "escapes"):
            promote(self.db, self.idea, who="operator")
        self.assertFalse((outside / self.year / "an-idea-uber-cafe").exists())
        self.assertIsNone(self.db.execute("SELECT piece FROM item WHERE id = ?", (self.idea,)).fetchone()[0])

    def orphan(self, extra=""):
        """The untouched scaffold a promote stopped before its commit leaves, dated another day."""
        text = TEMPLATE.replace('title: ""', 'title: "An idea: Über café!"').replace("YYYY-MM-DD", "2026-01-02") + extra
        self.file().parent.mkdir(parents=True, exist_ok=True)
        self.file().write_text(text)
        return text

    def test_a_retry_adopts_the_untouched_scaffold_a_stopped_promote_left(self):
        text = self.orphan()
        state = promote(self.db, self.idea, who="operator")
        self.assertEqual(self.file().read_text(), text)
        self.assertEqual(state["writing"]["metadata"]["created"], "2026-01-02")
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])

    def test_an_edited_scaffold_is_not_adopted(self):
        text = self.orphan("Prose someone wrote.\n")
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            promote(self.db, self.idea, who="operator")
        self.assertEqual(self.file().read_text(), text)

    def test_an_untouched_scaffold_with_a_parked_twin_is_not_adopted(self):
        self.orphan()
        parked = self.repo / "content-parked" / self.year / "an-idea-uber-cafe" / "index.md"
        parked.parent.mkdir(parents=True)
        parked.write_text("held\n")
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            promote(self.db, self.idea, who="operator")
        self.assertIsNone(self.db.execute("SELECT piece FROM item WHERE id = ?", (self.idea,)).fetchone()[0])

    def test_an_open_outer_transaction_is_refused(self):
        self.db.execute("BEGIN")
        try:
            with self.assertRaisesRegex(WorkflowError, "own transaction"):
                promote(self.db, self.idea, who="operator")
        finally:
            self.db.execute("ROLLBACK")
        self.assertFalse(self.file().parent.exists())

    def test_a_failure_after_the_scaffold_removes_it_and_keeps_the_row(self):
        with patch("sd_db.writing.piece_state", side_effect=RuntimeError("late")), self.assertRaises(RuntimeError):
            promote(self.db, self.idea, who="operator")
        self.assertFalse(self.file().parent.exists())
        row = self.db.execute("SELECT piece, source FROM item WHERE id = ?", (self.idea,)).fetchone()
        self.assertEqual(tuple(row), (None, "vault"))

    def test_the_target_is_the_one_repository_that_registers_pieces(self):
        other = self.root / "other"
        (other / "templates").mkdir(parents=True)
        (other / "templates/piece-template.md").write_text(TEMPLATE)
        upsert_repo(self.db, str(other), pieces_source="row")
        with self.assertRaisesRegex(WorkflowError, "several"):
            promote(self.db, self.idea, who="operator")
        promote(self.db, self.idea, repo=str(other), who="operator")
        self.assertTrue(self.file(root=other).is_file())
        self.assertFalse(self.file().exists())

    def test_no_repository_that_registers_pieces_is_refused(self):
        self.db.execute("UPDATE item SET piece = NULL WHERE id = ?", (self.item,))
        with self.assertRaisesRegex(WorkflowError, "no repository"):
            promote(self.db, self.idea, who="operator")

    @hub_only
    def test_a_row_owned_repository_gets_no_status_bookkeeping(self):
        self.cutover()
        promote(self.db, self.idea, who="operator")
        text = self.file().read_text()
        self.assertNotIn("status:", text)
        self.assertNotIn("published:", text)
        self.assertTrue(verify_pieces(self.db, str(self.repo))["ok"])


class WorktreeCheckout(WritingCase):
    """A writer in a linked worktree reads its own files under the registered row (sd:2024)."""

    def setUp(self):
        super().setUp()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        def git(*args, cwd=self.repo):
            subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-q", "-m", "seed")
        self.worktree = self.root / "worktree"
        git("worktree", "add", "-q", "-b", "gate", str(self.worktree))
        self.linked = self.worktree / "content" / self.piece / "index.md"

    def test_state_reads_the_worktree_prose_under_the_registered_row(self):
        self.linked.write_text(self.linked.read_text().replace("A claim.", "Worktree prose."))
        with checkout(str(self.repo), self.worktree):
            state = piece_state(self.db, self.item)
        self.assertIn("Worktree prose", state["writing"]["document"])
        self.assertNotIn("Worktree prose", piece_state(self.db, self.item)["writing"]["document"])

    def test_gate_hashes_the_worktree_report_not_the_main_checkout(self):
        (self.worktree / "content" / self.piece / "fact-check.md").write_text("Worktree ledger.\n")
        with checkout(str(self.repo), self.worktree):
            record_gate(self.db, self.item, "fact-check", verdict="pass", findings=[], reason="Reviewed",
                        reviewed_digest=self.digest, who="operator")
            fresh = piece_state(self.db, self.item)["writing"]["gates"]
        stale = piece_state(self.db, self.item)["writing"]["gates"]
        self.assertNotEqual(fresh, stale)

    def test_a_path_outside_the_repository_is_refused(self):
        stranger = self.root / "stranger"
        stranger.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=stranger, check=True)
        for path in (stranger, self.root):
            with self.assertRaisesRegex(WorkflowError, "not a worktree"):
                with checkout(str(self.repo), path):
                    pass

    @hub_only
    def test_cutover_and_recovery_refuse_a_worktree(self):
        with checkout(str(self.repo), self.worktree):
            with self.assertRaisesRegex(WorkflowError, "registered checkout"):
                cutover_pieces(self.db, str(self.repo), expected_fingerprint="x", who="operator")
            with self.assertRaisesRegex(WorkflowError, "registered checkout"):
                recover_cutover(self.db, str(self.repo), who="operator")
            with self.assertRaisesRegex(WorkflowError, "registered checkout"):
                import_piece(self.db, str(self.repo), self.piece, who="operator")

    def test_another_repository_keeps_its_own_files_inside_a_checkout(self):
        other = self.root / "other"
        folder = other / "content" / self.piece
        folder.mkdir(parents=True)
        (folder / "index.md").write_text(self.index.read_text().replace("A claim.", "Other repository prose."))
        upsert_repo(self.db, str(other))
        item = import_piece(self.db, str(other), self.piece, who="operator")["item"]["id"]
        self.linked.write_text(self.linked.read_text().replace("A claim.", "Worktree prose."))
        with checkout(str(self.repo), self.worktree):
            document = piece_state(self.db, item)["writing"]["document"]
        self.assertIn("Other repository prose", document)
        self.assertNotIn("Worktree prose", document)

    @hub_only
    def test_registration_refuses_a_worktree_and_leaves_its_file_alone(self):
        self.cutover()
        new = self.worktree / "content/2026/new/index.md"
        new.parent.mkdir(parents=True)
        original = b'---\ntitle: New piece\nstatus: idea\npublished: null\n---\n\n## Draft\nFuture prose.\n'
        new.write_bytes(original)
        with checkout(str(self.repo), self.worktree):
            with self.assertRaisesRegex(WorkflowError, "registered checkout"):
                import_piece(self.db, str(self.repo), "2026/new", who="operator")
        self.assertEqual(new.read_bytes(), original)
        self.assertIsNone(piece_for_key(self.db, str(self.repo), "2026/new"))

    def test_promote_scaffolds_in_the_checkout_under_the_registered_row(self):
        template = self.worktree / "templates/piece-template.md"
        template.parent.mkdir()
        template.write_text(TEMPLATE)
        idea = create_item(self.db, kind="idea", title="Worktree idea")
        with checkout(str(self.repo), self.worktree):
            state = promote(self.db, idea, who="operator")
        self.assertEqual(state["item"]["repo"], str(self.repo))
        relative = f"content/{state['item']['piece']}/index.md"
        self.assertTrue((self.worktree / relative).is_file())
        self.assertFalse((self.repo / relative).exists())

    def test_the_registered_checkout_is_its_own_checkout(self):
        with checkout(str(self.repo), self.repo):
            self.assertIn("A claim", piece_state(self.db, self.item)["writing"]["document"])


if __name__ == "__main__":
    unittest.main()
