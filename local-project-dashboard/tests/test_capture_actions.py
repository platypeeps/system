"""Capture context preserves identity, revision and the existing action boundary."""

import json
from unittest.mock import patch

from sd_db import reads, upsert_repo, workflow, writes
from test_workflow_actions import BrowserSession


class CaptureActions(BrowserSession):
    def test_context_is_read_only_and_exposes_only_selected_identity(self):
        first = workflow.capture_task(self.connection, title="Same title", body="Private details", who="operator")
        second = workflow.capture_task(self.connection, title="Same title", who="operator")
        first = workflow.add_item_note(self.connection, first["item"]["id"], body="Private note", who="operator")
        before = self.snapshot()
        status, headers, body = self.request(f"/api/items/{first['item']['id']}/capture-context")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        context = json.loads(body)
        self.assertEqual(set(context), {"item", "revision"})
        self.assertEqual(context["item"], {key: first["item"][key]
                         for key in ("id", "title", "kind", "status", "repo", "parked_at")})
        self.assertEqual(context["revision"], first["revision"])
        self.assertNotEqual(context["item"]["id"], second["item"]["id"])
        self.assertNotIn("Private", body)
        self.assertEqual(self.snapshot(), before)

    def test_all_note_kinds_attach_to_selected_parent_without_creating_tasks(self):
        upsert_repo(self.connection, "/repos/one")
        upsert_repo(self.connection, "/repos/two")
        first = workflow.capture_task(self.connection, title="Same title", repo="/repos/one", who="operator")
        second = workflow.capture_task(self.connection, title="Same title", repo="/repos/two", who="operator")
        item = second["item"]["id"]
        count = self.connection.execute("SELECT count(*) FROM item").fetchone()[0]
        for kind in workflow.NOTE_KINDS:
            with self.subTest(kind=kind):
                status, _, body = self.request(f"/api/items/{item}/capture-context")
                self.assertEqual(status, 200)
                context = json.loads(body)
                status, _, result = self.post(f"/api/items/{item}/notes", {
                    "revision": context["revision"], "kind": kind, "body": f"Captured {kind}"})
                self.assertEqual(status, 200)
                self.assertEqual(result["note"]["item"], item)
                self.assertEqual(result["note"]["kind"], kind)
                self.assertEqual(result["item"], second["item"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM item").fetchone()[0], count)
        self.assertEqual(workflow.item_state(self.connection, first["item"]["id"]), first)
        self.assertEqual([row["kind"] for row in reads.open_followups(self.connection)], ["followup"])

    def test_changed_parent_or_note_refuses_captured_revision_without_writes(self):
        for change in ("title", "note"):
            with self.subTest(change=change):
                state = workflow.capture_task(self.connection, title="Original", who="operator")
                item = state["item"]["id"]
                status, _, body = self.request(f"/api/items/{item}/capture-context")
                self.assertEqual(status, 200)
                revision = json.loads(body)["revision"]
                if change == "title":
                    workflow.edit_item(self.connection, item, {"title": "Changed elsewhere"}, who="operator")
                else:
                    workflow.add_item_note(self.connection, item, body="Concurrent note", who="operator")
                before = self.snapshot()
                status, _, result = self.post(f"/api/items/{item}/notes", {
                    "revision": revision, "kind": "followup", "body": "Old selection"})
                self.assertEqual(status, 409)
                self.assertTrue(result["reload"])
                self.assertEqual(self.snapshot(), before)

    def test_note_does_not_reactivate_a_completed_parked_parent(self):
        state = workflow.capture_task(self.connection, title="Archived context", who="operator")
        item = state["item"]["id"]
        self.connection.execute("UPDATE item SET status='done', parked_at=? WHERE id=?",
                                (self.now, item))
        state = workflow.item_state(self.connection, item)
        status, _, body = self.request(f"/api/items/{item}/capture-context")
        self.assertEqual(status, 200)
        context = json.loads(body)
        self.assertEqual(context["item"]["parked_at"], self.now)
        status, _, result = self.post(f"/api/items/{item}/notes", {
            "revision": context["revision"], "kind": "followup", "body": "Retained for later"})
        self.assertEqual(status, 200)
        self.assertEqual(result["item"], state["item"])
        self.assertEqual(reads.open_followups(self.connection), [])

    def test_missing_invalid_contexts_and_notes_are_refused_without_writes(self):
        before = self.snapshot()
        for identity in ("999999", "0", "-1", "nope", "01", "9223372036854775808", "1" * 100):
            with self.subTest(identity=identity):
                self.assertEqual(self.request(f"/api/items/{identity}/capture-context")[0], 404)
        self.assertEqual(self.request("/api/items/1/capture-context?revision=fake")[0], 404)
        self.assertEqual(self.post("/api/items/999999/notes", {
            "revision": "0" * 64, "kind": "followup", "body": "Missing"})[0], 404)
        self.assertEqual(self.post("/api/items/1/notes", {"kind": "followup", "body": "No revision"})[0], 400)
        self.assertEqual(self.snapshot(), before)

    def test_context_uses_existing_host_boundary_and_notes_keep_csrf_boundary(self):
        state = workflow.capture_task(self.connection, title="Keep", who="operator")
        item = state["item"]["id"]
        before = self.snapshot()
        status, _, _ = self.request(f"/api/items/{item}/capture-context", headers={"Host": "evil.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(self.post(f"/api/items/{item}/notes", {
            "revision": state["revision"], "kind": "followup", "body": "Denied"},
            **{"X-SD-CSRF": "0" * 64})[0], 403)
        self.assertEqual(self.snapshot(), before)


class FollowupItemCapture(BrowserSession):
    """sd:719 step 6a: the form files a followup ITEM through the write `sd task add --kind followup` makes.

    The pack's verb is `workflow.capture_task` and then the library's own
    `writes.set_item_fields(kind="followup")` inside one transaction
    (`bin/sd_work.py::_capture` at a7572f14); the control here calls the
    same two functions. The note-kind path is untouched: `followup` on
    `/api/items/<id>/notes` is still a note.
    """

    def test_a_followup_item_is_filed_with_the_cli_write_and_linked_to_its_parent(self):
        upsert_repo(self.connection, "/repos/one")
        parent = workflow.capture_task(self.connection, title="The parent", repo="/repos/one", who="operator")
        parent_id = parent["item"]["id"]
        notes = len(reads.item_notes(self.connection, parent_id))
        followups = list(reads.open_followups(self.connection))
        with patch("sd_db.writes.set_item_fields", wraps=writes.set_item_fields) as reclassify, \
                patch("sd_db.workflow.capture_task", wraps=workflow.capture_task) as capture:
            status, _, state = self.post("/api/items", {
                "title": "Chase the reviewer", "kind": "followup", "followup_of": parent_id})
        self.assertEqual(status, 201)
        capture.assert_called_once()
        reclassify.assert_called_once()
        self.assertEqual(reclassify.call_args.kwargs["kind"], "followup")
        child = state["item"]
        self.assertEqual(child["kind"], "followup")
        self.assertEqual(child["title"], "Chase the reviewer")
        self.assertEqual(child["status"], "planning")
        self.assertEqual(child["repo"], "/repos/one")
        self.assertEqual(json.loads(child["fields"]), {"followup_of": parent_id})
        self.assertEqual(state, workflow.item_state(self.connection, child["id"]))
        # The link is visible from the parent: one comment note naming the child.
        added = reads.item_notes(self.connection, parent_id)[notes:]
        self.assertEqual([(note["kind"], note["body"]) for note in added],
                         [("comment", f"Followup item #{child['id']} filed: Chase the reviewer")])
        # An item, not a note: Today's open followups did not grow.
        self.assertEqual(list(reads.open_followups(self.connection)), followups)
        self.assertIn(child["id"], [row["id"] for row in reads.backlog_items(self.connection)])

    def test_a_followup_posted_with_a_null_repo_still_takes_its_parents(self):
        # `"repo" not in values` read an explicit `null` as a choice, so a
        # caller serialising an optional field filed the child with no
        # repository under a parent that had one (PR #428 review). A named
        # repository still wins over the parent's.
        upsert_repo(self.connection, "/repos/one")
        upsert_repo(self.connection, "/repos/two")
        parent = workflow.capture_task(self.connection, title="The parent", repo="/repos/one", who="operator")["item"]["id"]
        status, _, state = self.post("/api/items", {
            "title": "Chase", "kind": "followup", "followup_of": parent, "repo": None})
        self.assertEqual((status, state["item"]["repo"]), (201, "/repos/one"))
        status, _, state = self.post("/api/items", {
            "title": "Chase elsewhere", "kind": "followup", "followup_of": parent, "repo": "/repos/two"})
        self.assertEqual((status, state["item"]["repo"]), (201, "/repos/two"))

    def test_a_followup_item_may_stand_alone(self):
        status, _, state = self.post("/api/items", {"title": "Ask about the budget", "kind": "followup"})
        self.assertEqual(status, 201)
        self.assertEqual(state["item"]["kind"], "followup")
        self.assertIsNone(state["item"]["repo"])
        self.assertIsNone(state["item"]["fields"])

    def test_the_note_kind_path_still_produces_a_note_and_no_item(self):
        parent = workflow.capture_task(self.connection, title="The parent", who="operator")
        count = self.connection.execute("SELECT count(*) FROM item").fetchone()[0]
        status, _, result = self.post(f"/api/items/{parent['item']['id']}/notes", {
            "revision": parent["revision"], "kind": "followup", "body": "Still a note"})
        self.assertEqual(status, 200)
        self.assertEqual(result["note"]["kind"], "followup")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM item").fetchone()[0], count)
        self.assertEqual([row["body"] for row in reads.open_followups(self.connection)], ["Still a note"])

    def test_a_kind_the_form_does_not_offer_and_a_bad_parent_are_refused_without_writes(self):
        parent = workflow.capture_task(self.connection, title="The parent", who="operator")["item"]["id"]
        before = self.snapshot()
        for payload, expected in (
            ({"title": "x", "kind": "personal"}, 400),
            ({"title": "x", "kind": "task", "followup_of": parent}, 400),
            ({"title": "x", "kind": "followup", "followup_of": str(parent)}, 400),
            ({"title": "x", "kind": "followup", "followup_of": 0}, 400),
            ({"title": "x", "kind": "followup", "followup_of": parent + 1000}, 404),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/items", payload)[0], expected)
        self.assertEqual(self.snapshot(), before)
