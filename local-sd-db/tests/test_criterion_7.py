"""Criterion 7's transition clauses 7.17, 7.19 and 7.20, the library half.

The clauses (`prd.md:1321-1339` of the one-database item) name five
surfaces; this module is the one they all reach,
`source:local-sd-db/sd_db/workflow.py::change_status`, and the two cancels
the owner's 2026-09-16 decision (a) named: `sd runner cancel` over
`source:local-sd-db/sd_db/runner_controls.py::control` and `sd assignments
cancel` over `source:local-sd-db/sd_db/operations.py::cancel_assignment`.
The dashboard's four surfaces are `local-project-dashboard/tests/
test_criterion_7.py`; the pack CLI's `sd task status` is the pack's to test.

The grep the 7.17 clause asks for -- a second `UPDATE item SET status` --
is already `test_no_second_writer_of_item_status` in
`local-project-dashboard/tests/test_status_history.py`, so it is cited here
and not written twice.
"""

from __future__ import annotations

import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import (
    connect,
    create_assignment,
    create_item,
    operations,
    runner,
    runner_controls,
    upsert_repo,
)
from sd_db.migrate import initialise
from sd_db.reads import item_by_id, status_changes
from sd_db.workflow import change_status, item_state
from sd_db.writes import STATUSES, TransitionRefused

#: A cancel that needs no runner is a cancel that returns at once. Two
#: transactions on an empty database take milliseconds; a heartbeat
#: interval is ten seconds. The bound sits far from both.
CANCEL_BOUND_SECONDS = 2.0


class Criterion7(unittest.TestCase):
    """A `work` item with a triad on a row-owned repository, and no runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.repo = "/repos/system"
        upsert_repo(self.db, self.repo, remote="git@example.invalid:system.git", status_source="row")
        self.item = create_item(
            self.db, kind="work", title="An unmerged slice", repo=self.repo,
            branch="feat/sd-1-slice", status="in_progress", source="docs/work",
            path="docs/work/2026-09-05-slice/prd.md",
            external_id=f"{self.repo}::docs/work/2026-09-05-slice/prd.md",
        )

    # -- readback ----------------------------------------------------------

    def row(self) -> dict:
        return dict(item_by_id(self.db, self.item))

    def history(self) -> list[str]:
        return [note["body"] for note in status_changes(self.db, self.item)]

    def refusal(self, target: str) -> str:
        """The refusal `change_status` makes for `target`, with nothing written."""
        before, notes = self.row(), self.history()
        with self.assertRaises(TransitionRefused) as caught:
            change_status(self.db, self.item, target, who="operator")
        self.assertEqual(self.row(), before, "the item row is unchanged")
        self.assertEqual(self.history(), notes, "and no status_change note was written")
        return str(caught.exception)

    def no_runner(self):
        """No runner process, no heartbeat read, no sleep: the cancel is a write.

        `test_runner.py` asserts the heartbeat row's shape and nothing about
        waiting on it, so this is the assertion the 7.20 clause adds: patch
        the two ways a caller could wait and let either raise.
        """
        stack = contextlib.ExitStack()
        for guard in (
            patch("subprocess.Popen", side_effect=AssertionError("a cancel started a process")),
            patch("subprocess.run", side_effect=AssertionError("a cancel ran a process")),
            patch("time.sleep", side_effect=AssertionError("a cancel slept")),
            patch.object(runner, "heartbeat_state", side_effect=AssertionError("a cancel read the heartbeat")),
        ):
            stack.enter_context(guard)
        return stack

    # -- 7.17 ---------------------------------------------------------------

    def test_7_17_done_on_unmerged_work_is_refused_naming_the_delivery_and_offering_cancel(self):
        message = self.refusal("done")
        self.assertRegex(message, r"delivery|merge")
        self.assertRegex(message, r"cancel")
        self.assertEqual(self.row()["status"], "in_progress")
        self.assertIsNone(self.row()["shipped_at"])

    # -- 7.19 ---------------------------------------------------------------

    def test_7_19_a_running_assignment_refuses_every_status_write_naming_the_row(self):
        """Every target in `STATUSES`, the current one included: a write of
        the status the row already has is a write, and the refusal comes
        before the same-status no-op while the runner owns the row."""
        running = create_assignment(self.db, item=self.item, role="author", status="running")
        self.assertEqual(self.row()["status"], "in_progress")
        for target in STATUSES:
            with self.subTest(target=target):
                self.assertRegex(self.refusal(target), rf"assignment {running}\b")

    def end(self, assignment: int) -> None:
        self.db.execute("UPDATE assignment SET status = 'failed' WHERE id = ?", (assignment,))
        self.db.commit()

    def test_7_19_a_queued_row_names_cancel_and_a_running_row_names_the_control_entry(self):
        """The clause's second half: the refusal says what would satisfy it.

        A `queued` row has no process, so the way out is the library's own
        cancel; a `running` row with an owned run has one, so the way out is
        the runner's control entry, `sd runner cancel <id>` on the item
        screen; and a `running` row with no `runner_run` has no process to
        stop, so the way out is the same verb, which ends the row (sd:991).
        """
        queued = runner.enqueue(self.db, [self.item], who="operator")[0]["id"]
        message = self.refusal("ready")
        self.assertRegex(message, rf"assignment {queued}\b")
        self.assertIn(f"cancel it with `sd runner cancel {queued}`", message)
        self.assertNotIn("control entry", message)
        self.end(queued)

        owned = runner.enqueue(self.db, [self.item], who="operator")[0]["id"]
        root = Path(self.tmp.name)
        held = runner.claim(self.db, owned, owner="fixture", work_root=root / "work", retention_root=root / "retained")
        self.assertEqual(held["status"], "running")
        self.assertIsNotNone(held["run"])
        message = self.refusal("ready")
        self.assertRegex(message, rf"assignment {owned}\b")
        self.assertIn(f"stop it from the runner's control entry, `sd runner cancel {owned}`", message)
        self.end(owned)

        by_hand = create_assignment(self.db, item=self.item, role="author", status="running")
        self.assertIsNone(runner.queue_state(self.db, by_hand)["run"])
        message = self.refusal("ready")
        self.assertRegex(message, rf"assignment {by_hand}\b")
        self.assertIn(f"running assignment without a runner run; end it with `sd runner cancel {by_hand}`", message)
        self.assertNotIn("control entry", message)

    def test_7_19_a_running_row_whose_only_attempt_was_released_names_no_control_entry(self):
        """A released `runner_run` is history, not an active attempt.

        `runner_controls.control` filters on `released_at IS NULL`, so it
        refuses this row. The bare existence test sent it to that entry.
        """
        released = runner.enqueue(self.db, [self.item], who="operator")[0]["id"]
        root = Path(self.tmp.name)
        runner.claim(self.db, released, owner="fixture", work_root=root / "work", retention_root=root / "retained")
        self.db.execute("UPDATE runner_run SET released_at = '2026-01-01T00:00:00Z' WHERE assignment = ?",
                        (released,))
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE id = ?",
                                         (released,)).fetchone()[0], "running")
        message = self.refusal("ready")
        self.assertRegex(message, rf"assignment {released}\b")
        self.assertIn(f"running assignment without a runner run; end it with `sd runner cancel {released}`", message)
        self.assertNotIn("control entry", message)
        self.end(released)

    # -- 7.19 recovery (sd:991) ----------------------------------------------

    def ended_running_row_frees_the_item(self, cancel, *, released=False):
        """A `running` row no attempt owns: the cancel ends it, with the note.

        The owner's decision (note 2706): `sd runner cancel <id>` ends a
        `running` row with no `runner_run` `cancelled`, with a `cancelled by
        <who>` note in the cancel's own write. It waits on no runner.
        """
        if released:
            assignment = runner.enqueue(self.db, [self.item], who="operator")[0]["id"]
            root = Path(self.tmp.name)
            runner.claim(self.db, assignment, owner="fixture", work_root=root / "work", retention_root=root / "retained")
            self.db.execute("UPDATE runner_run SET released_at = '2026-01-01T00:00:00Z' WHERE assignment = ?", (assignment,))
            self.db.commit()
        else:
            assignment = create_assignment(self.db, item=self.item, role="author", status="running")
        self.assertRegex(self.refusal("ready"), rf"assignment {assignment}\b")
        with self.no_runner():
            result = cancel(assignment)
        self.assertEqual((result["id"], result["status"], result["result"]), (assignment, "cancelled", "cancelled by operator"))
        self.assertTrue(result["ended"])
        freed = change_status(self.db, self.item, "ready", who="operator")
        self.assertEqual(freed["item"]["status"], "ready")

    def control_cancel(self, assignment):
        current = runner.queue_state(self.db, assignment)
        return runner_controls.control(self.db, assignment, "cancel", expected_revision=current["revision"], who="operator")

    def request_cancel(self, assignment):
        current = runner.queue_state(self.db, assignment)
        return runner.request_cancel(self.db, assignment, expected_revision=current["revision"], who="operator")

    def test_7_19_the_runner_control_cancel_ends_a_running_row_with_no_run(self):
        self.ended_running_row_frees_the_item(self.control_cancel)

    def test_7_19_the_library_cancel_ends_a_running_row_with_no_run(self):
        self.ended_running_row_frees_the_item(self.request_cancel)

    def test_7_19_the_runner_control_cancel_ends_a_running_row_whose_only_attempt_was_released(self):
        self.ended_running_row_frees_the_item(self.control_cancel, released=True)

    def test_7_19_the_library_cancel_ends_a_running_row_whose_only_attempt_was_released(self):
        self.ended_running_row_frees_the_item(self.request_cancel, released=True)

    def test_7_19_a_running_row_with_an_owned_attempt_is_only_asked_to_stop(self):
        owned = runner.enqueue(self.db, [self.item], who="operator")[0]["id"]
        root = Path(self.tmp.name)
        runner.claim(self.db, owned, owner="fixture", work_root=root / "work", retention_root=root / "retained")
        result = self.request_cancel(owned)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["run"]["cancel_requested"], "cancelled by operator")
        self.assertIsNone(result["ended"])

    # -- 7.20 ---------------------------------------------------------------

    def cancelled_row_frees_the_item(self, cancel):
        """Enqueue with no runner, see the refusal, cancel, write again."""
        queued = runner.enqueue(self.db, [self.item], who="operator")[0]
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["run"], "no runner claimed it")
        self.assertRegex(self.refusal("ready"), rf"assignment {queued['id']}\b")

        started = time.perf_counter()
        with self.no_runner():
            cancel(queued["id"])
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, CANCEL_BOUND_SECONDS, "the cancel waited on nothing")

        # The clause says `blocked`; the schema's terminal status for a
        # cancelled row is `cancelled`, which `writes.py` guards from being
        # undone and `requeue` accepts. The drift is recorded on the item.
        row = self.db.execute("SELECT status, result, ended FROM assignment WHERE id = ?",
                              (queued["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertTrue(row["ended"])
        notes = [note["body"] for note in item_state(self.db, self.item)["notes"]]
        self.assertTrue(
            row["result"] == "cancelled by operator" or any("cancelled by operator" in note for note in notes),
            f"the row or its note says `cancelled by operator`: result={row['result']!r}, notes={notes!r}",
        )
        freed = change_status(self.db, self.item, "ready", who="operator")
        self.assertEqual(freed["item"]["status"], "ready")
        self.assertIn("in_progress -> ready by operator", self.history()[-1])

    def test_7_20_the_runner_control_cancel_needs_no_runner(self):
        def cancel(assignment):
            current = runner.queue_state(self.db, assignment)
            result = runner_controls.control(self.db, assignment, "cancel",
                                             expected_revision=current["revision"], who="operator")
            self.assertEqual(result["id"], assignment)
        self.cancelled_row_frees_the_item(cancel)

    def test_7_20_the_operations_cancel_needs_no_runner(self):
        def cancel(assignment):
            current = operations.assignment_state(self.db, assignment)
            self.assertTrue(current["capabilities"]["cancel"]["allowed"])
            result = operations.cancel_assignment(self.db, assignment,
                                                  expected_revision=current["revision"], who="operator")
            self.assertEqual(result["id"], assignment)
        self.cancelled_row_frees_the_item(cancel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
