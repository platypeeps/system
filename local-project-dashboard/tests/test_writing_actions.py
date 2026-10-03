"""Writing HTTP controls use the shared gates and never modify piece files."""

from pathlib import Path

from sd_db import upsert_repo, writing

from .test_workflow_actions import BrowserSession


class WritingActions(BrowserSession):
    def test_park_and_revive_preserve_files_stage_and_gate_evidence(self):
        folder, state = self.seed_piece(gates=True)
        item = state["item"]["id"]
        before_files = self.files(folder)
        revision = state["revision"]
        status, _, parked = self.post(f"/api/items/{item}/park", {"revision": revision})
        self.assertEqual(status, 200)
        self.assertTrue(parked["writing"]["parked"])
        self.assertEqual(parked["writing"]["stage"], state["writing"]["stage"])
        self.assertEqual(parked["writing"]["gates"], state["writing"]["gates"])
        self.assertIn(f'action="/api/items/{item}/revive"', self.request(f"/item/{item}")[2])
        self.assertEqual(self.post(f"/api/items/{item}/revive", {"revision": revision})[0], 409)
        status, _, revived = self.post(f"/api/items/{item}/revive", {"revision": parked["revision"]})
        self.assertEqual(status, 200)
        self.assertFalse(revived["writing"]["parked"])
        self.assertEqual(self.files(folder), before_files)

    def test_file_owned_park_refuses_without_any_write(self):
        folder, state = self.seed_piece(owner="file")
        item = state["item"]["id"]
        before_files, before_rows = self.files(folder), self.snapshot()
        self.assertNotIn(f'action="/api/items/{item}/park"', self.request(f"/item/{item}")[2])
        self.assertEqual(self.post(f"/api/items/{item}/park", {"revision": state["revision"]})[0], 400)
        self.assertEqual(self.files(folder), before_files)
        self.assertEqual(self.snapshot(), before_rows)

    def seed_piece(self, *, slug="fixture", stage="review", owner="row", gates=False):
        root = Path(self.tmp.name) / "writing"
        folder = root / "content" / "2026" / slug
        folder.mkdir(parents=True, exist_ok=True)
        index = folder / "index.md"
        index.write_text(f"---\ntitle: {slug.capitalize()} piece\nstatus: {stage}\n---\n\n## Draft\n\nA carefully scoped fixture argument.\n")
        upsert_repo(self.connection, str(root), pieces_source="file")
        state = writing.import_piece(self.connection, str(root), f"2026/{slug}", who="operator")
        item = state["item"]["id"]
        digest = state["writing"]["gates"]["digest"]
        (folder / "research.md").write_text(f"Research fixture\n<!-- reconciled-with-draft: {digest} gen=0 -->\n")
        (folder / "fact-check.md").write_text(f"Fact-check fixture report\n<!-- reconciled-with-draft: {digest} gen=0 -->\n")
        (folder / "adversarial.md").write_text(f"Adversarial fixture report\n<!-- reconciled-with-draft: {digest} gen=0 -->\n")
        upsert_repo(self.connection, str(root), pieces_source=owner)
        if gates:
            for artifact in ("fact-check", "adversarial"):
                writing.record_gate(self.connection, item, artifact, verdict="pass", findings=[], reason="Explicit fixture verdict", who="operator")
        return folder, writing.piece_state(self.connection, item)

    def files(self, folder):
        return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}

    def test_readiness_refusal_and_success_share_gates_and_preserve_files(self):
        folder, state = self.seed_piece()
        item = state["item"]["id"]
        before_files, before_rows = self.files(folder), self.snapshot()
        status, _, refused = self.post(f"/api/items/{item}/stage", {
            "revision": state["revision"], "stage": "ready"})
        self.assertEqual(status, 400)
        self.assertIn("gate", refused["error"])
        self.assertEqual(self.snapshot(), before_rows)
        self.assertEqual(self.files(folder), before_files)
        for artifact in ("fact-check", "adversarial"):
            state = writing.record_gate(self.connection, item, artifact, verdict="pass", findings=[], reason="Explicit fixture verdict", who="operator")
        status, _, state = self.post(f"/api/items/{item}/stage", {
            "revision": state["revision"], "stage": "ready"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["stage"], "ready")
        self.assertEqual(state["item"]["status"], writing.STAGE_STATUS["ready"])
        self.assertTrue(state["item"]["ready_digest"])
        self.assertEqual(self.files(folder), before_files)
        page = self.request(f"/item/{item}")[2]
        self.assertIn("Review checks pass", page)
        self.assertIn("A carefully scoped fixture argument.", page)
        artifact = page.split('<article class="artifact">', 1)[1].split("</article>", 1)[0]
        self.assertNotIn("status: review", artifact)
        self.assertNotIn("External context", page)
        self.assertNotIn('value="published"', page)

    def test_correction_requires_reason_and_invalidates_readiness(self):
        folder, state = self.seed_piece(gates=True)
        item = state["item"]["id"]
        state = writing.change_stage(self.connection, item, "ready", who="operator")
        before_files, before_rows = self.files(folder), self.snapshot()
        self.assertEqual(self.post(f"/api/items/{item}/stage", {
            "revision": state["revision"], "stage": "drafting", "correct": True})[0], 400)
        self.assertEqual(self.snapshot(), before_rows)
        status, _, state = self.post(f"/api/items/{item}/stage", {
            "revision": state["revision"], "stage": "drafting", "correct": True,
            "reason": "Revisit the central claim"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["gate_generation"], 1)
        self.assertIsNone(state["item"]["ready_digest"])
        self.assertFalse(state["writing"]["gates"]["ok"])
        self.assertEqual(self.files(folder), before_files)

    def test_forged_file_owned_stage_request_never_writes_a_file(self):
        folder, state = self.seed_piece(stage="idea", owner="file")
        item = state["item"]["id"]
        before_files, before_rows = self.files(folder), self.snapshot()
        page = self.request(f"/item/{item}")[2]
        self.assertIn("read-only until its database migration is verified", page)
        self.assertNotIn(f'action="/api/items/{item}/stage"', page)
        self.assertEqual(self.post(f"/api/items/{item}/stage", {
            "revision": state["revision"], "stage": "researching"})[0], 400)
        self.assertEqual(self.snapshot(), before_rows)
        self.assertEqual(self.files(folder), before_files)

    def test_changed_report_stale_tab_and_generic_publish_are_refused(self):
        folder, state = self.seed_piece(gates=True)
        item = state["item"]["id"]
        (folder / "fact-check.md").write_text("Changed evidence")
        self.assertEqual(self.post(f"/api/items/{item}/stage", {
            "revision": state["revision"], "stage": "ready"})[0], 400)
        fresh = writing.change_stage(self.connection, item, "drafting", correct=True, reason="New evidence", who="operator")
        self.assertEqual(self.post(f"/api/items/{item}/stage", {
            "revision": state["revision"], "stage": "ready"})[0], 409)
        before = self.snapshot()
        self.assertEqual(self.post(f"/api/items/{item}/stage", {
            "revision": fresh["revision"], "stage": "published"})[0], 400)
        self.assertEqual(self.snapshot(), before)

    def test_writing_list_filters_stage_saved_readiness_and_parked(self):
        _, first = self.seed_piece(slug="review", gates=True)
        first = writing.change_stage(self.connection, first["item"]["id"], "ready", who="operator")
        _, second = self.seed_piece(slug="draft", stage="drafting")
        _, third = self.seed_piece(slug="parked", stage="idea")
        writing.park_piece(self.connection, third["item"]["id"], who="operator")
        page = self.request("/classic/writing?stage=ready&readiness=recorded")[2]
        self.assertIn(f'href="/item/{first["item"]["id"]}"', page)
        self.assertNotIn(f'href="/item/{second["item"]["id"]}"', page)
        self.assertNotIn(f'href="/item/{third["item"]["id"]}"', page)
        page = self.request("/classic/writing?parked=parked")[2]
        self.assertIn(f'href="/item/{third["item"]["id"]}"', page)
        self.assertNotIn(f'href="/item/{first["item"]["id"]}"', page)

    def test_current_draft_and_checks_are_read_from_the_same_snapshot(self):
        folder, state = self.seed_piece(gates=True)
        item = state["item"]["id"]
        index = folder / "index.md"
        index.write_text(index.read_text() + "\nNew uncommitted paragraph <script>bad</script>.\n")
        page = self.request(f"/item/{item}")[2]
        self.assertIn("New uncommitted paragraph", page)
        self.assertNotIn("<script>bad</script>", page)
        self.assertIn("Review needs attention", page)
        self.assertIn("stale against this draft", page)
