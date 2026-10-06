"""The user-facing task workflow is local, atomic and revision checked."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, create_assignment, create_item, upsert_repo
from sd_db import contributions
from sd_db.database import transaction
from sd_db.migrate import initialise
from sd_db.reads import item_by_id
from sd_db.writes import add_note, set_item_fields
from sd_db.writes import TransitionRefused
from sd_db.workflow import (
    HAND_KINDS, REPO_LESS_KINDS, TASK_STATUSES, MissingItem, MissingNote, StaleItem, WorkflowError,
    add_item_note, allowed_statuses, capture_task, change_status, edit_item, item_state,
    resolve_item_note, schema_kinds,
)


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def capture(self, **values):
        return capture_task(self.db, title="A small task", who="operator", **values)

    def counts(self):
        return tuple(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("repo", "item", "note", "shadow"))


class OrdinaryTasks(WorkflowCase):
    def test_capture_edit_complete_needs_no_repository_or_external_issue(self):
        state = self.capture(body="Do this", priority=2, due="2026-09-10")
        item = state["item"]
        self.assertEqual((item["kind"], item["status"], item["repo"], item["external_id"]),
                         ("task", "planning", None, None))
        self.assertEqual(json.loads(item["body"]), {"text": "Do this"})
        edited = edit_item(self.db, item["id"], {"title": "Done today", "priority": 1},
                           expected_revision=state["revision"], who="operator")
        finished = change_status(self.db, item["id"], "done", who="user",
                                 expected_revision=edited["revision"])
        self.assertEqual(finished["item"]["status"], "done")
        self.assertEqual(finished["item"]["title"], "Done today")
        self.assertEqual(self.counts()[0::3], (0, 0))
        history = [n for n in finished["notes"] if n["kind"] == "status_change"]
        self.assertEqual(len(history), 2)
        self.assertIn("planning -> done by user", history[-1]["body"])
        self.assertTrue(history[-1]["timestamp"])

    def test_an_unknown_status_points_to_the_cancel_verbs(self):
        # sd:2741: 'dropped' listed the statuses and never named the verb that closes.
        item = self.capture()["item"]["id"]
        with self.assertRaises(TransitionRefused) as refused:
            change_status(self.db, item, "dropped", who="operator")
        self.assertIn("no status 'dropped'", str(refused.exception))
        self.assertIn(f"sd task cancel {item} --reason", str(refused.exception))
        self.assertIn("sd work cancel", str(refused.exception))

    def test_optional_repository_must_already_be_registered(self):
        with self.assertRaisesRegex(WorkflowError, "registered"):
            self.capture(repo="/repos/unknown")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        upsert_repo(self.db, "/repos/known")
        state = self.capture(repo="/repos/known")
        cleared = edit_item(self.db, state["item"]["id"], {"repo": None}, who="operator")
        self.assertIsNone(cleared["item"]["repo"])

    def test_invalid_user_fields_and_internal_metadata_cannot_be_written(self):
        state = self.capture()
        for changes in ({"status": "done"}, {"kind": "work"}, {"fields": {}},
                        {"external_id": "https://example.test/issue/1"},
                        {"title": "  "}, {"priority": True}, {"priority": 5},
                        {"due": "2026-02-30"}, {"due": "20260901"},
                        {"body": {"status_change": "done"}}):
            with self.subTest(changes=changes), self.assertRaises(WorkflowError):
                edit_item(self.db, state["item"]["id"], changes, who="operator")
            self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_missing_item_errors_are_exact_and_never_silent_successes(self):
        for call in (lambda: item_state(self.db, 999),
                     lambda: edit_item(self.db, 999, {"title": "x"}, who="operator"),
                     lambda: change_status(self.db, 999, "done", who="operator"),
                     lambda: add_item_note(self.db, 999, body="x", who="operator")):
            with self.assertRaisesRegex(MissingItem, "^no item 999$"):
                call()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_stale_edit_is_rejected_even_when_both_changes_share_a_second(self):
        with patch("sd_db.writes.now", return_value="2026-09-08T12:00:00+00:00"):
            state = self.capture()
            first = edit_item(self.db, state["item"]["id"], {"title": "First"},
                              expected_revision=state["revision"], who="operator")
            self.assertEqual(state["item"]["updated_at"], first["item"]["updated_at"])
            with self.assertRaises(StaleItem):
                edit_item(self.db, state["item"]["id"], {"title": "Lost update"},
                          expected_revision=state["revision"], who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), first)

    def test_failed_history_write_rolls_back_the_edited_row(self):
        state = self.capture()
        with patch("sd_db.workflow.add_note", side_effect=RuntimeError("disk error")):
            with self.assertRaisesRegex(RuntimeError, "disk error"):
                edit_item(self.db, state["item"]["id"], {"title": "Must roll back"}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_noop_status_does_not_add_history_or_change_revision(self):
        state = self.capture()
        self.assertEqual(change_status(self.db, state["item"]["id"], "planning", who="operator"), state)

    def test_readback_is_one_snapshot_while_another_connection_writes(self):
        state = self.capture()
        other = connect(self.path)
        self.addCleanup(other.close)

        def concurrent_edit(connection, item):
            row = item_by_id(connection, item)
            set_item_fields(other, item, title="Concurrent edit")
            add_note(other, item, "comment", "Concurrent note")
            return row

        with patch("sd_db.workflow.item_by_id", side_effect=concurrent_edit):
            observed = item_state(self.db, state["item"]["id"])
        self.assertEqual(observed, state)
        self.assertEqual(item_state(self.db, state["item"]["id"])["item"]["title"],
                         "Concurrent edit")


class StatusGuards(WorkflowCase):
    def test_unknown_status_is_refused_without_history(self):
        state = self.capture()
        with self.assertRaises(TransitionRefused):
            change_status(self.db, state["item"]["id"], "shipped", who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_work_done_cannot_bypass_delivery_checks(self):
        upsert_repo(self.db, "/repos/work", status_source="row")
        item = create_item(self.db, kind="work", title="Code delivery", repo="/repos/work")
        with self.assertRaisesRegex(TransitionRefused, "delivery"):
            change_status(self.db, item, "done", who="operator")
        self.assertNotIn("done", allowed_statuses(self.db, item))

    def test_work_status_stays_with_its_current_owner_until_cutover(self):
        for owner in ("file", "retiring", "row"):
            repo = f"/repos/{owner}"
            upsert_repo(self.db, repo, status_source=owner)
            item = create_item(self.db, kind="work", title="Work", repo=repo)
            if owner == "row":
                self.assertEqual(change_status(self.db, item, "ready", who="operator")["item"]["status"], "ready")
            else:
                before = item_state(self.db, item)
                with self.subTest(owner=owner), self.assertRaisesRegex(TransitionRefused, "owner"):
                    change_status(self.db, item, "ready", who="operator")
                self.assertEqual(allowed_statuses(self.db, item), [])
                self.assertEqual(item_state(self.db, item), before)

    def test_work_repository_cannot_be_changed_away_from_its_stable_source(self):
        upsert_repo(self.db, "/repos/one", status_source="row")
        upsert_repo(self.db, "/repos/two", status_source="row")
        item = create_item(self.db, kind="work", title="Work", repo="/repos/one",
                           source="docs/work", external_id="/repos/one::docs/work/x/prd.md")
        before = item_state(self.db, item)
        for repo in (None, "/repos/two"):
            with self.subTest(repo=repo), self.assertRaisesRegex(WorkflowError, "identity"):
                edit_item(self.db, item, {"repo": repo}, who="operator")
            self.assertEqual(item_state(self.db, item), before)

    def test_other_kinds_cannot_bypass_their_specialized_workflow(self):
        item = create_item(self.db, kind="idea", title="Writing")
        with self.assertRaises(TransitionRefused):
            change_status(self.db, item, "ready", who="operator")
        self.assertEqual(allowed_statuses(self.db, item), [])

    def test_allowed_statuses_takes_the_state_the_caller_read(self):
        # sd:2380: a caller that read the item's state passes it, and the history is not read again.
        item = create_item(self.db, kind="task", title="Listed")
        other = create_item(self.db, kind="idea", title="Another")
        state, others = item_state(self.db, item), item_state(self.db, other)
        with patch("sd_db.workflow.item_state", side_effect=AssertionError("the history was read again")):
            self.assertEqual(allowed_statuses(self.db, item, state=state), list(TASK_STATUSES))
            with self.assertRaisesRegex(WorkflowError, f"item {other}'s, not item {item}'s"):
                allowed_statuses(self.db, item, state=others)

    def assert_finishes_with_history(self, kind):
        item = create_item(self.db, kind=kind, title=f"A {kind} item")
        finished = change_status(self.db, item, "done", who="operator")
        self.assertEqual(finished["item"]["status"], "done")
        history = [n for n in finished["notes"] if n["kind"] == "status_change"]
        self.assertIn("planning -> done by operator", history[-1]["body"])

    def test_followup_item_moves_from_planning_to_done_with_history(self):
        # sd:768: a followup item used to have no status control at all.
        self.assert_finishes_with_history("followup")

    def test_personal_item_moves_from_planning_to_done_with_history(self):
        self.assert_finishes_with_history("personal")

    def test_followup_and_personal_offer_exactly_the_task_statuses(self):
        for kind in ("followup", "personal"):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            with self.subTest(kind=kind):
                self.assertEqual(allowed_statuses(self.db, item), list(TASK_STATUSES))
                self.assertEqual(change_status(self.db, item, "blocked", who="operator")["item"]["status"],
                                 "blocked")

    def test_a_done_task_status_item_reopens_to_planning_with_history(self):
        # sd:772: done is not terminal for these kinds; only work completion is.
        for kind in ("task", "followup", "personal"):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            change_status(self.db, item, "done", who="operator")
            with self.subTest(kind=kind):
                self.assertEqual(allowed_statuses(self.db, item), list(TASK_STATUSES))
                reopened = change_status(self.db, item, "planning", who="operator")
                self.assertEqual(reopened["item"]["status"], "planning")
                history = [n for n in reopened["notes"] if n["kind"] == "status_change"]
                self.assertIn("done -> planning by operator", history[-1]["body"])

    def test_a_stale_revision_on_a_followup_or_personal_item_leaves_its_status(self):
        for kind in ("followup", "personal"):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            seen = item_state(self.db, item)["revision"]
            finished = change_status(self.db, item, "done", who="operator", expected_revision=seen)
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(StaleItem, f"^item {item} changed"):
                    change_status(self.db, item, "planning", who="operator", expected_revision=seen)
                self.assertEqual(item_state(self.db, item), finished)
                self.assertEqual(finished["item"]["status"], "done")

    def test_kinds_with_their_own_workflow_still_refuse_status_controls(self):
        for kind in ("idea", "work-idea", "personal-idea", "report"):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            before = item_state(self.db, item)
            with self.subTest(kind=kind), self.assertRaisesRegex(TransitionRefused, "task controls"):
                change_status(self.db, item, "done", who="operator")
            self.assertEqual(allowed_statuses(self.db, item), [])
            self.assertEqual(item_state(self.db, item), before)

    def test_followup_with_active_assignment_refuses_until_it_ends(self):
        item = create_item(self.db, kind="followup", title="Come back to this")
        for status in ("queued", "running"):
            assignment = create_assignment(self.db, item=item, role="author", status=status)
            before = item_state(self.db, item)
            with self.subTest(status=status), self.assertRaisesRegex(TransitionRefused, "assignment"):
                change_status(self.db, item, "done", who="operator")
            self.assertEqual(allowed_statuses(self.db, item), [])
            self.assertEqual(item_state(self.db, item), before)
            self.db.execute("UPDATE assignment SET status = 'failed' WHERE id = ?", (assignment,))
            self.db.commit()
        self.assertEqual(change_status(self.db, item, "done", who="operator")["item"]["status"], "done")

    def test_queued_or_running_assignment_blocks_status_changes(self):
        for status in ("queued", "running"):
            state = self.capture()
            item = state["item"]["id"]
            create_assignment(self.db, item=item, role="author", status=status)
            with self.subTest(status=status), self.assertRaisesRegex(TransitionRefused, "assignment"):
                change_status(self.db, item, "done", who="operator")
            self.assertEqual(allowed_statuses(self.db, item), [])
            self.assertEqual(item_state(self.db, item), state)

    def test_work_body_cannot_be_replaced_by_task_editor(self):
        upsert_repo(self.db, "/repos/work", status_source="row")
        item = create_item(self.db, kind="work", title="Work", repo="/repos/work", body={"Goals": "Keep"})
        with self.assertRaisesRegex(WorkflowError, "task"):
            edit_item(self.db, item, {"body": "Replace"}, who="operator")
        self.assertEqual(json.loads(item_state(self.db, item)["item"]["body"]), {"Goals": "Keep"})


class BranchItemClose(WorkflowCase):
    """sd:2570: a row worked on its own branch needs its merge, or a reason,
    to close; the pack's `sd task status` rule (sd:1990), held by the library."""

    SHA = "a" * 40

    def on_branch(self, branch):
        item = self.capture()["item"]["id"]
        self.db.execute("UPDATE item SET branch = ? WHERE id = ?", (branch, item))
        self.db.commit()
        return item

    def test_a_reasonless_close_with_no_merge_recorded_is_refused_and_leaves_the_row(self):
        item = self.on_branch("fleet/fixture-sd1")
        before = item_state(self.db, item)
        with self.assertRaisesRegex(TransitionRefused,
                                    "worked on branch fleet/fixture-sd1, and no merge of it is recorded"):
            change_status(self.db, item, "done", who="dashboard")
        self.assertEqual(item_state(self.db, item), before)

    def test_a_reason_closes_it_and_the_transition_records_it(self):
        item = self.on_branch("fleet/fixture-sd1")
        finished = change_status(self.db, item, "done", who="dashboard", reason="superseded by another merge")
        self.assertEqual(finished["item"]["status"], "done")
        self.assertIn("superseded by another merge", finished["notes"][-1]["body"])

    def test_a_recorded_merge_closes_it_plainly(self):
        merged = self.on_branch("fleet/fixture-sd1")
        add_note(self.db, merged, "comment", f"Code delivery https://github.example.test/o/r/pull/7 at {self.SHA}")
        self.assertEqual(change_status(self.db, merged, "done", who="dashboard")["item"]["status"], "done")
        delivered = self.on_branch("fleet/fixture-sd2")
        change_status(self.db, delivered, "done", who="sd-ship", reason=f"delivered at {self.SHA} on refs/heads/main")
        change_status(self.db, delivered, "planning", who="operator")
        self.assertEqual(change_status(self.db, delivered, "done", who="dashboard")["item"]["status"], "done")

    def test_a_row_on_the_default_branch_or_none_still_closes_plainly(self):
        for branch in ("main", "origin/master", None):
            with self.subTest(branch=branch):
                item = self.on_branch(branch)
                self.assertEqual(change_status(self.db, item, "done", who="dashboard")["item"]["status"], "done")

    def test_other_moves_of_a_branch_row_are_unchanged(self):
        item = self.on_branch("fleet/fixture-sd1")
        self.assertEqual(change_status(self.db, item, "blocked", who="dashboard")["item"]["status"], "blocked")


class BranchClearing(WorkflowCase):
    """sd:2818: `edit_item` clears a stale branch and sets none."""

    def work_on(self, branch, status="done"):
        upsert_repo(self.db, "/repos/work", status_source="row")
        item = create_item(self.db, kind="work", status=status, title="Work",
                           repo="/repos/work", branch=branch)
        return item_state(self.db, item)

    def test_a_cleared_branch_is_written_noted_and_revision_checked(self):
        state = self.work_on("feat/gone")
        item = state["item"]["id"]
        with self.assertRaises(StaleItem):
            edit_item(self.db, item, {"branch": None}, who="operator", expected_revision="0" * 64)
        self.assertEqual(item_state(self.db, item), state)
        cleared = edit_item(self.db, item, {"branch": None}, who="operator",
                            expected_revision=state["revision"])
        self.assertIsNone(cleared["item"]["branch"])
        self.assertEqual(cleared["notes"][-1]["body"], "Updated branch by operator")

    def test_a_branch_value_is_refused_and_leaves_the_row(self):
        state = self.work_on("feat/gone", status="planning")
        with self.assertRaisesRegex(WorkflowError, "branch can only be cleared here"):
            edit_item(self.db, state["item"]["id"], {"branch": "feat/new"}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)


class KindEditing(WorkflowCase):
    """sd:743 -- a row's kind changes through `edit_item`, attributed and noted."""

    OLD = "2000-01-01T00:00:00+00:00"

    def backdate(self, item):
        self.db.execute("UPDATE item SET updated_at = ? WHERE id = ?", (self.OLD, item))
        self.db.commit()

    def refused(self, item, changes, pattern, **options):
        before, counts = item_state(self.db, item), self.counts()
        with self.assertRaisesRegex(WorkflowError, pattern):
            edit_item(self.db, item, changes, who="operator", **options)
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(self.counts(), counts)

    def test_hand_kinds_are_kinds_the_schema_allows(self):
        self.assertTrue(set(HAND_KINDS) <= set(schema_kinds(self.db)))
        self.assertIn("report", schema_kinds(self.db))

    def test_a_legal_change_writes_kind_updated_at_and_a_note_naming_both_kinds_and_who(self):
        state = self.capture()
        item = state["item"]["id"]
        self.backdate(item)
        state = item_state(self.db, item)
        edited = edit_item(self.db, item, {"kind": "personal"}, who="alex",
                           expected_revision=state["revision"])
        self.assertEqual(edited["item"]["kind"], "personal")
        self.assertNotEqual(edited["item"]["updated_at"], self.OLD)
        note = edited["notes"][-1]
        self.assertEqual((note["kind"], note["body"], note["session"]),
                         ("comment", "Changed kind task -> personal by alex", "alex"))
        # A row outside task and work is reclassified by the same verb, kind only.
        back = edit_item(self.db, item, {"kind": "task"}, who="alex")
        self.assertEqual(back["item"]["kind"], "task")
        self.assertEqual(back["notes"][-1]["body"], "Changed kind personal -> task by alex")
        with self.assertRaisesRegex(WorkflowError, "own editing workflow"):
            edit_item(self.db, create_item(self.db, kind="personal", title="Errand"),
                      {"kind": "task", "title": "Renamed"}, who="alex")

    def test_an_idea_without_a_piece_can_be_reclassified(self):
        item = create_item(self.db, kind="idea", title="Book idea")
        edited = edit_item(self.db, item, {"kind": "personal-idea"}, who="alex")
        self.assertEqual(edited["item"]["kind"], "personal-idea")
        self.assertEqual(edited["notes"][-1]["body"], "Changed kind idea -> personal-idea by alex")

    def test_a_vault_idea_keeps_its_kind_because_the_import_would_revert_it(self):
        # The vault lands its rows with `piece` NULL, so the piece rule
        # misses them. Its next import rewrote the kind back to idea.
        item = create_item(self.db, kind="idea", title="Vault note", source="vault",
                           external_id="notes/vault-note.md")
        self.refused(item, {"kind": "personal-idea"}, "vault note")

    def test_an_unknown_kind_is_refused_and_nothing_is_written(self):
        item = self.capture()["item"]["id"]
        for kind in ("nonsense", "", None, 3):
            with self.subTest(kind=kind):
                self.refused(item, {"kind": kind}, "no item kind")

    def test_an_idea_carrying_a_piece_keeps_its_kind(self):
        item = create_item(self.db, kind="idea", title="An article")
        set_item_fields(self.db, item, piece="pieces/an-article")
        self.db.commit()
        self.refused(item, {"kind": "work-idea"}, "writing piece")

    def test_a_produced_kind_cannot_be_set_or_left_by_hand(self):
        produced = [kind for kind in schema_kinds(self.db) if kind not in HAND_KINDS]
        self.assertTrue(produced)
        item = self.capture()["item"]["id"]
        for kind in produced:
            with self.subTest(target=kind):
                self.refused(item, {"kind": kind}, "own producer")
        upsert_repo(self.db, "/repos/work", status_source="row")
        for kind in ("work", "report", "dep", "skill-review", "proposal"):
            source = create_item(self.db, kind=kind, title=kind,
                                 repo="/repos/work" if kind == "work" else None)
            with self.subTest(source=kind):
                self.refused(source, {"kind": "task"}, "owned by their producer")

    def test_a_repository_less_kind_needs_the_repository_cleared_in_the_same_edit(self):
        upsert_repo(self.db, "/repos/one")
        for kind in sorted(REPO_LESS_KINDS):
            with self.subTest(kind=kind):
                item = self.capture(repo="/repos/one")["item"]["id"]
                self.refused(item, {"kind": kind}, "carries no repository")
                edited = edit_item(self.db, item, {"kind": kind, "repo": None}, who="alex")
                self.assertEqual((edited["item"]["kind"], edited["item"]["repo"]), (kind, None))
                self.assertEqual(edited["notes"][-1]["body"],
                                 f"Changed kind task -> {kind} by alex\nUpdated repo by alex")

    def test_the_repository_less_kinds_are_personal_and_the_two_ideas(self):
        """sd:809. `followup` left the set; a followup is about one repository."""
        self.assertEqual(REPO_LESS_KINDS, frozenset({"personal", "work-idea", "personal-idea"}))
        self.assertLess(REPO_LESS_KINDS, frozenset(HAND_KINDS))

    def test_a_queued_running_or_ending_assignment_blocks_a_kind_change(self):
        for status in ("queued", "running", "ending"):
            item = self.capture()["item"]["id"]
            create_assignment(self.db, item=item, role="author", status=status)
            with self.subTest(status=status):
                self.refused(item, {"kind": "personal"}, f"{status} assignment")

    def test_a_contribution_task_keeps_its_kind_so_a_round_trip_cannot_skip_its_checks(self):
        """Review of #332, B1: `contributions._registered` reads only `kind='task'`.

        Moving a contribution away and back let a second row take its pull
        request and let a dependency cycle form. The first move is refused.
        """
        pull = "https://github.com/o/r/pull/1"
        first = contributions.capture(self.db, title="A", changes={"pull_url": pull}, who="operator")["item"]["id"]
        self.refused(first, {"kind": "personal"}, "contribution")
        with self.assertRaisesRegex(WorkflowError, "already belongs"):
            contributions.capture(self.db, title="B", changes={"pull_url": pull}, who="operator")

        x = contributions.capture(self.db, title="X", changes={"pull_url": "https://github.com/o/r/pull/3"},
                                  who="operator")["item"]["id"]
        y = contributions.capture(self.db, title="Y", changes={"pull_url": "https://github.com/o/r/pull/4"},
                                  who="operator")["item"]["id"]
        contributions.configure(self.db, y, {"depends_on": [{"kind": "item", "item": x}]}, who="operator")
        self.refused(y, {"kind": "followup"}, "contribution")
        with self.assertRaisesRegex(WorkflowError, "cycle"):
            contributions.configure(self.db, x, {"depends_on": [{"kind": "item", "item": y}]}, who="operator")

    def test_a_skill_review_task_keeps_its_kind(self):
        item = create_item(self.db, kind="task", title="Apply proposals",
                           fields={"skill_review": {"action": "apply"}})
        self.refused(item, {"kind": "personal"}, "skill_review")

    def test_unreadable_fields_refuse_a_kind_change(self):
        item = self.capture()["item"]["id"]
        self.db.execute("UPDATE item SET fields = 'not json' WHERE id = ?", (item,))
        self.db.commit()
        self.refused(item, {"kind": "personal"}, "unreadable fields")

    def test_schema_kinds_reads_the_check_this_store_carries(self):
        narrow = sqlite3.connect(":memory:")
        self.addCleanup(narrow.close)
        narrow.row_factory = sqlite3.Row
        narrow.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, "
                       "kind TEXT NOT NULL CHECK (kind IN ('work', 'task')))")
        self.assertEqual(schema_kinds(narrow), ("work", "task"))
        narrow.execute("DROP TABLE item")
        narrow.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, kind TEXT NOT NULL)")
        with self.assertRaisesRegex(WorkflowError, "no CHECK"):
            schema_kinds(narrow)

    def test_an_unchanged_kind_beside_another_field_writes_only_that_field_s_note(self):
        item = self.capture()["item"]["id"]
        edited = edit_item(self.db, item, {"kind": "task", "title": "Renamed"}, who="alex")
        self.assertEqual((edited["item"]["kind"], edited["item"]["title"]), ("task", "Renamed"))
        self.assertEqual(edited["notes"][-1]["body"], "Updated title by alex")

    def test_an_idea_with_a_repository_reaches_a_repository_less_kind_in_one_edit(self):
        upsert_repo(self.db, "/repos/one")
        item = create_item(self.db, kind="idea", title="Side project", repo="/repos/one")
        # The clear rides only on a real reclassification.
        self.refused(item, {"kind": "idea", "repo": None}, "own editing workflow")
        self.refused(item, {"kind": "personal", "repo": "/repos/one"}, "own editing workflow")
        edited = edit_item(self.db, item, {"kind": "personal-idea", "repo": None}, who="alex")
        self.assertEqual((edited["item"]["kind"], edited["item"]["repo"]), ("personal-idea", None))
        self.assertEqual(edited["notes"][-1]["body"],
                         "Changed kind idea -> personal-idea by alex\nUpdated repo by alex")

    def test_who_is_required_and_has_no_default(self):
        item = self.capture()["item"]["id"]
        before = item_state(self.db, item)
        with self.assertRaises(TypeError):
            edit_item(self.db, item, {"kind": "personal"})
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(edit_item(self.db, item, {"kind": "personal"}, who="alex")["item"]["kind"],
                         "personal")

    def test_a_stale_revision_is_refused_and_a_current_one_applies(self):
        state = self.capture()
        item = state["item"]["id"]
        edit_item(self.db, item, {"title": "Moved on"}, who="operator")
        before, counts = item_state(self.db, item), self.counts()
        with self.assertRaises(StaleItem):
            edit_item(self.db, item, {"kind": "personal"}, who="operator",
                      expected_revision=state["revision"])
        self.assertEqual((item_state(self.db, item), self.counts()), (before, counts))
        edited = edit_item(self.db, item, {"kind": "personal"}, who="operator",
                           expected_revision=before["revision"])
        self.assertEqual(edited["item"]["kind"], "personal")


class FollowupRepository(WorkflowCase):
    """sd:809. A followup item carries a repository and edits like a task.

    Before this a followup was one of the repository-less kinds: a move into
    it had to clear `repo`, and `edit_item` refused its every field but `kind`,
    so a followup filed off its checkout could not be moved onto it.
    """

    def setUp(self):
        super().setUp()
        upsert_repo(self.db, "/repos/one")
        upsert_repo(self.db, "/repos/two")

    def refused(self, item, changes, pattern):
        before, counts = item_state(self.db, item), self.counts()
        with self.assertRaisesRegex(WorkflowError, pattern):
            edit_item(self.db, item, changes, who="operator")
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(self.counts(), counts)

    def test_a_task_with_a_repository_becomes_a_followup_and_keeps_it(self):
        item = self.capture(repo="/repos/one")["item"]["id"]
        edited = edit_item(self.db, item, {"kind": "followup"}, who="alex")
        self.assertEqual((edited["item"]["kind"], edited["item"]["repo"]), ("followup", "/repos/one"))
        self.assertEqual(edited["notes"][-1]["body"], "Changed kind task -> followup by alex")

    def test_a_followup_takes_a_repository_title_priority_and_due(self):
        item = create_item(self.db, kind="followup", title="Chase the review finding")
        state = item_state(self.db, item)
        edited = edit_item(self.db, item, {"repo": "/repos/one", "title": "Fix the finding",
                                           "priority": 2, "due": "2026-09-20"},
                           who="alex", expected_revision=state["revision"])
        row = edited["item"]
        self.assertEqual((row["kind"], row["repo"], row["title"], row["priority"], row["due"]),
                         ("followup", "/repos/one", "Fix the finding", 2, "2026-09-20"))
        self.assertEqual(edited["notes"][-1]["body"], "Updated due, priority, repo, title by alex")

        moved = edit_item(self.db, item, {"repo": "/repos/two"}, who="alex")
        self.assertEqual(moved["item"]["repo"], "/repos/two")
        cleared = edit_item(self.db, item, {"repo": None, "priority": None, "due": None}, who="alex")
        self.assertEqual((cleared["item"]["repo"], cleared["item"]["priority"], cleared["item"]["due"]),
                         (None, None, None))
        self.assertEqual(cleared["notes"][-1]["body"], "Updated due, priority, repo by alex")
        self.assertEqual(item_by_id(self.db, item)["repo"], None)

    def test_a_followup_refuses_an_unregistered_repository(self):
        item = create_item(self.db, kind="followup", title="Somewhere else")
        self.refused(item, {"repo": "/repos/unregistered"}, "not registered")

    def test_a_followup_with_a_repository_leaves_it_only_for_a_repository_less_kind(self):
        item = create_item(self.db, kind="followup", title="About one repository", repo="/repos/one")
        self.refused(item, {"kind": "personal"}, "carries no repository")
        back = edit_item(self.db, item, {"kind": "task"}, who="alex")
        self.assertEqual((back["item"]["kind"], back["item"]["repo"]), ("task", "/repos/one"))
        again = edit_item(self.db, item, {"kind": "followup"}, who="alex")
        personal = edit_item(self.db, item, {"kind": "personal", "repo": None}, who="alex",
                             expected_revision=again["revision"])
        self.assertEqual((personal["item"]["kind"], personal["item"]["repo"]), ("personal", None))

    def test_a_personal_item_becomes_a_followup_before_it_takes_a_repository(self):
        item = create_item(self.db, kind="personal", title="Turned out to be about a repository")
        self.refused(item, {"kind": "followup", "repo": "/repos/one"}, "personal items use their own editing workflow")
        followup = edit_item(self.db, item, {"kind": "followup"}, who="alex")
        moved = edit_item(self.db, item, {"repo": "/repos/one"}, who="alex", expected_revision=followup["revision"])
        self.assertEqual((moved["item"]["kind"], moved["item"]["repo"]), ("followup", "/repos/one"))

    def test_the_repository_less_kinds_still_refuse_their_other_fields(self):
        for kind in sorted(REPO_LESS_KINDS):
            item = create_item(self.db, kind=kind, title=f"A {kind}")
            with self.subTest(kind=kind):
                self.refused(item, {"title": "Renamed"}, "own editing workflow")
                self.refused(item, {"repo": "/repos/one"}, "own editing workflow")
                self.refused(item, {"priority": 1}, "own editing workflow")

    def test_a_followup_body_is_edited_as_a_task_body_is(self):
        item = create_item(self.db, kind="followup", title="Correct the finding")
        edited = edit_item(self.db, item, {"body": "The finding is at line 12"}, who="alex")
        self.assertEqual(json.loads(edited["item"]["body"]), {"text": "The finding is at line 12"})
        self.assertEqual(edited["notes"][-1]["body"], "Updated body by alex")
        # An equal submission writes nothing, as for a task.
        self.assertEqual(edit_item(self.db, item, {"body": "The finding is at line 12"}, who="alex"),
                         edited)
        for kind in ("personal", "work"):
            if kind == "work":
                upsert_repo(self.db, "/repos/work", status_source="row")
            other = create_item(self.db, kind=kind, title=f"A {kind}",
                                repo="/repos/work" if kind == "work" else None)
            with self.subTest(kind=kind):
                self.refused(other, {"body": "New text"},
                             "own editing workflow" if kind == "personal" else "task and followup items")


class TaskNotes(WorkflowCase):
    def test_add_and_resolve_note_are_reflected_in_revision_and_readback(self):
        state = self.capture()
        added = add_item_note(self.db, state["item"]["id"], body="Follow through",
                              kind="followup", who="user", expected_revision=state["revision"])
        self.assertNotEqual(added["revision"], state["revision"])
        self.assertEqual(added["note"]["session"], "user")
        resolved = resolve_item_note(self.db, added["note"]["id"], expected_revision=added["revision"], who="operator")
        self.assertTrue(resolved["note"]["resolved_at"])
        self.assertNotEqual(resolved["revision"], added["revision"])
        self.assertEqual(resolve_item_note(self.db, added["note"]["id"], who="operator"), resolved)

    def test_status_and_execution_history_cannot_be_injected_or_resolved(self):
        state = self.capture()
        for kind in ("status_change", "exec", "unknown"):
            with self.subTest(kind=kind), self.assertRaises(WorkflowError):
                add_item_note(self.db, state["item"]["id"], kind=kind, body="forged", who="operator")
        with self.assertRaises(WorkflowError):
            resolve_item_note(self.db, state["notes"][0]["id"], who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_missing_note_is_an_exact_error(self):
        with self.assertRaisesRegex(MissingNote, "^no note 999$"):
            resolve_item_note(self.db, 999, who="operator")

    def test_stale_note_resolution_leaves_note_unresolved(self):
        state = self.capture()
        added = add_item_note(self.db, state["item"]["id"], body="First", who="operator")
        current = add_item_note(self.db, state["item"]["id"], body="Second", who="operator")
        with self.assertRaises(StaleItem):
            resolve_item_note(self.db, added["note"]["id"], expected_revision=added["revision"], who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"])["revision"], current["revision"])


class NestedTransactions(WorkflowCase):
    def test_outer_failure_rolls_back_successful_inner_write(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.db):
                create_item(self.db, kind="task", title="Rolled back")
                raise RuntimeError("abort outer")
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_inner_failure_can_be_caught_without_discarding_outer_work(self):
        with transaction(self.db):
            create_item(self.db, kind="task", title="Kept")
            with self.assertRaises(RuntimeError):
                with transaction(self.db):
                    create_item(self.db, kind="task", title="Discarded")
                    raise RuntimeError("abort inner")
            create_item(self.db, kind="task", title="Also kept")
        self.assertEqual([row[0] for row in self.db.execute("SELECT title FROM item ORDER BY id")],
                         ["Kept", "Also kept"])


class BinaryColumns(WorkflowCase):
    """sd:874 -- `item_state` hashes a row a BLOB column makes unserialisable.

    `item.fields` and `item.body` are declared `TEXT`. TEXT affinity converts
    every numeric storage class to text before storing, so an INTEGER, a REAL,
    a CAST and a bound Python int or float all come back as `str` and
    `json.loads` never receives a number from these columns. BLOB is the one
    storage class affinity leaves alone: it comes back as `bytes`.

    `item_state` json.dumps-es the readback to compute the revision, and
    `json.JSONEncoder` has no rule for `bytes`. A BLOB carrying perfectly valid
    JSON died there too, before any caller's parse guard, with
    `TypeError: Object of type bytes is not JSON serializable`. The dashboard's
    `do_GET` catches neither, so the reader got no response at all.

    The fix belongs here rather than in a `CHECK` on the column: a constraint
    would reject the rows the store already holds and would not tell an
    existing reader why its revision changed. Encoding bytes for the hash
    leaves every text row's revision byte-identical, which the last test here
    is what proves.
    """

    def stored(self, value, column="fields"):
        item = create_item(self.db, kind="task", title="Binary")
        self.db.execute(f"UPDATE item SET {column} = ? WHERE id = ?", (value, item))
        self.db.commit()
        return item

    def test_affinity_leaves_only_a_blob_as_bytes(self):
        for value in (12345, 1.5, "12345"):
            with self.subTest(value=value):
                item = self.stored(value)
                self.assertIsInstance(item_by_id(self.db, item)["fields"], str)
        item = self.stored(b'{"report": {}}')
        self.assertIsInstance(item_by_id(self.db, item)["fields"], bytes)

    def test_a_blob_column_still_yields_a_revision(self):
        for column in ("fields", "body"):
            for value in (b'{"report": {}}', b"\xff\xfe not json"):
                with self.subTest(column=column, value=value):
                    state = item_state(self.db, self.stored(value, column))
                    self.assertRegex(state["revision"], r"\A[a-f0-9]{64}\Z")
                    self.assertEqual(state["item"][column], value)

    def swap(self, item, value, column="fields"):
        """Rewrite one column on one row and nothing else.

        The same row, so the revision cannot differ by `id` or `created_at`;
        a plain `UPDATE` rather than `set_item_fields`, so it cannot differ by
        `updated_at` either. What is left is the value, which is the point.
        """
        self.db.execute(f"UPDATE item SET {column} = ? WHERE id = ?", (value, item))
        self.db.commit()
        return item_state(self.db, item)["revision"]

    def test_different_blobs_do_not_share_a_revision(self):
        item = self.stored(b'{"a": 1}')
        before = item_state(self.db, item)["revision"]
        self.assertNotEqual(self.swap(item, b'{"a": 2}'), before)

    def test_a_blob_and_the_same_bytes_as_text_do_not_share_a_revision(self):
        item = self.stored(b'{"a": 1}')
        before = item_state(self.db, item)["revision"]
        self.assertNotEqual(self.swap(item, '{"a": 1}'), before)

    def test_a_text_row_revision_is_the_sha256_of_the_plain_dump(self):
        """The encoder runs only for a type `json.dumps` refuses, so no row
        that works today gets a new revision and no open edit goes stale."""
        item = self.stored('{"text": "ordinary"}', "body")
        state = item_state(self.db, item)
        plain = {"item": state["item"], "notes": state["notes"]}
        expected = hashlib.sha256(
            json.dumps(plain, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(state["revision"], expected)


if __name__ == "__main__":
    unittest.main()
