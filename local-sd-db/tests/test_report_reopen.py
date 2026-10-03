"""`reporting.reopen` undoes an acknowledgement (sd:2395).

The dashboard's Reports page asked before every Acknowledge and offered no
Undo, because nothing in sd-db moved a report back out of `done` (sd:2121).
These tests hold the verb to what an Undo needs: the report returns to the
status it was acknowledged from, the history names who reopened it, and a
stale or wrong request moves nothing.
"""

import inspect
import tempfile
import unittest
from pathlib import Path

from sd_db import create_item, reporting, workflow
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.writes import transition


class ReopenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)

    def report(self, status="planning"):
        return create_item(self.db, kind="report", title="nightly: run report", status=status,
                           source="cron-report", external_id=f"nightly:{self.db.total_changes}",
                           fields={"attention": False}, body={"text": "all quiet"}, session="cron")

    def revision(self, item):
        return workflow.item_state(self.db, item)["revision"]

    def acknowledged(self, status="planning"):
        item = self.report(status)
        reporting.acknowledge(self.db, item, expected_revision=self.revision(item), who="dashboard")
        return item

    def statuses(self, item):
        return [note["body"] for note in workflow.item_state(self.db, item)["notes"]
                if note["kind"] == "status_change"]


class AReopenUndoesTheAcknowledgement(ReopenCase):
    def test_an_acknowledged_report_returns_to_planning_and_names_who(self):
        item = self.acknowledged()
        state = reporting.reopen(self.db, item, expected_revision=self.revision(item), who="dashboard")
        self.assertEqual(state["item"]["status"], "planning")
        self.assertEqual(self.statuses(item)[-1], "done -> planning by dashboard: reopened")
        again = reporting.acknowledge(self.db, item, expected_revision=state["revision"], who="dashboard")
        self.assertEqual(again["item"]["status"], "done")

    def test_it_returns_to_the_status_the_acknowledgement_left(self):
        """A report a runner had moved acknowledges from there, and an Undo puts it back there."""
        item = self.report()
        transition(self.db, item, "in_progress", who="runner")
        reporting.acknowledge(self.db, item, expected_revision=self.revision(item), who="dashboard")
        state = reporting.reopen(self.db, item, expected_revision=self.revision(item), who="dashboard",
                                 reason="acknowledged by mistake")
        self.assertEqual(state["item"]["status"], "in_progress")
        self.assertEqual(self.statuses(item)[-1], "done -> in_progress by dashboard: reopened: acknowledged by mistake")

    def test_a_report_with_no_readable_history_returns_to_planning(self):
        item = self.report(status="done")
        state = reporting.reopen(self.db, item, expected_revision=self.revision(item), who="dashboard")
        self.assertEqual(state["item"]["status"], "planning")

    def test_an_open_report_is_left_as_it_is(self):
        """An Undo that races a reload finds the report open already, and changes nothing."""
        item = self.report()
        before = workflow.item_state(self.db, item)
        self.assertEqual(reporting.reopen(self.db, item, expected_revision=before["revision"], who="dashboard"), before)


class AWrongReopenMovesNothing(ReopenCase):
    def test_a_stale_revision_is_refused(self):
        item = self.acknowledged()
        with self.assertRaises(workflow.StaleItem):
            reporting.reopen(self.db, item, expected_revision="old", who="dashboard")
        self.assertEqual(workflow.item_state(self.db, item)["item"]["status"], "done")

    def test_only_a_report_reopens_here(self):
        task = create_item(self.db, kind="task", title="A task", status="done")
        with self.assertRaisesRegex(workflow.WorkflowError, "only report items"):
            reporting.reopen(self.db, task, expected_revision=self.revision(task), who="dashboard")
        self.assertEqual(workflow.item_state(self.db, task)["item"]["status"], "done")

    def test_who_has_no_default(self):
        who = inspect.signature(reporting.reopen).parameters["who"]
        self.assertIs(who.default, inspect.Parameter.empty)
        self.assertIs(who.kind, inspect.Parameter.KEYWORD_ONLY)
        item = self.acknowledged()
        with self.assertRaises(TypeError):
            reporting.reopen(self.db, item, expected_revision=self.revision(item))
        with self.assertRaises(workflow.WorkflowError):
            reporting.reopen(self.db, item, expected_revision=self.revision(item), who=" ")
        self.assertEqual(workflow.item_state(self.db, item)["item"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
