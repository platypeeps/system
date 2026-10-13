"""One batched revision read per page (sd:3531).

Reports, Today's work rows, Today's Decisions and Review each took one `workflow.item_state` per row for its revision.
They read every row's revision with one `reads.revisions` call now. Each test checks two things on a fixture database:
the values equal the old per-row `item_state` result, and a statement trace on the connection counts the revision reads
the page makes. An `item_state` call opens the savepoint `sd_workflow_state`; `reads.revisions` opens
`sd_reads_revisions`, once for any number of items.
"""

from __future__ import annotations

from sd_db import reporting, workflow
from sd_dashboard import decisions, now_screen, reports_screen, review_screen

from support import NOW, ScreenCase
from test_v2_review import seed as seed_review

PER_ROW = "SAVEPOINT sd_workflow_state"
BATCHED = "SAVEPOINT sd_reads_revisions"


class OneRevisionReadPerPage(ScreenCase):

    def traced(self, read):
        """The page's result, and how many per-row and batched revision reads it made."""
        seen = []
        self.connection.set_trace_callback(seen.append)
        try:
            result = read()
        finally:
            self.connection.set_trace_callback(None)
        return result, {"per_row": sum(s.startswith(PER_ROW) for s in seen),
                        "batched": sum(s.startswith(BATCHED) for s in seen)}

    def revision(self, item):
        return workflow.item_state(self.connection, item)["revision"]

    def test_reports_read_every_revision_and_followup_at_once(self):
        ids = []
        for n, attention in enumerate((True, False, True, False)):
            ids.append(reporting.ingest(self.connection, job=f"job-{n}", run_id=f"r{n}", started="2026-09-08T08:15:00Z",
                                        ended="2026-09-08T08:16:00Z", exit_code=7 if attention else 0, text="out",
                                        source_path="/fixture/log", attention=attention)["item"]["id"])
        # A second open followup, a resolved one and a question: only the open followups count, in history order.
        self.note(ids[0], "Check the mirror")
        resolved = self.note(ids[1], "Already handled")
        workflow.resolve_item_note(self.connection, resolved, who="test")
        self.note(ids[2], "Keep it?\nOption: Yes\nOption: No", kind="question")
        expected = {}
        for item in ids:
            state = workflow.item_state(self.connection, item)
            expected[item] = (state["revision"], [n["id"] for n in state["notes"]
                                                  if n["kind"] == "followup" and not n["resolved_at"]])
        rows, reads = self.traced(lambda: reports_screen._reports(self.connection))
        self.assertEqual({row["id"]: (row["revision"], row["followups"]) for row in rows}, expected)
        self.assertEqual(len(expected[ids[0]][1]), 2)
        self.assertEqual(reads, {"per_row": 0, "batched": 1})

    def test_today_work_rows_read_every_revision_at_once(self):
        items = [self.item(f"Work {n}", status="in_progress") for n in range(4)]
        self.note(items[1], "A later note moves the revision", kind="comment")
        rows, reads = self.traced(lambda: now_screen.work_rows(self.connection, NOW))
        self.assertEqual([(row["item"], row["revision"]) for row in rows], [(item, self.revision(item)) for item in items])
        self.assertEqual(reads, {"per_row": 0, "batched": 1})

    def test_decisions_read_every_revision_at_once(self):
        first, second = self.item("First"), self.item("Second")
        self.note(first, "Pick one\nOption: A\nOption: B", kind="question")
        self.note(first, "And one more\nOption: C\nOption: D", kind="question")
        self.note(second, "Ship it?\nOption: Yes\nOption: No", kind="question")
        document, reads = self.traced(lambda: decisions.document(self.connection))
        self.assertEqual(sorted((row["item"], row["revision"]) for row in document["decisions"]),
                         sorted([(first, self.revision(first))] * 2 + [(second, self.revision(second))]))
        self.assertEqual(reads, {"per_row": 0, "batched": 1})

    def test_review_reads_every_batch_revision_at_once(self):
        seed_review(self)
        document, reads = self.traced(lambda: review_screen.document(self.connection))
        batches = document["batches"]
        self.assertEqual([row.get("revision") for row in batches if row["error"] is None],
                         [self.revision(row["item"]) for row in batches if row["error"] is None])
        self.assertGreaterEqual(len([row for row in batches if row["error"] is None]), 3)
        # `review.read` still reads each batch's own notes; the revision no longer takes a second read per row.
        self.assertEqual(reads, {"per_row": len(review_screen._rows(self.connection)), "batched": 1})


if __name__ == "__main__":
    import unittest

    unittest.main()
