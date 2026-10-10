"""Criterion 7's transition clauses 7.17, 7.19 and 7.20, the dashboard half.

The clauses (`prd.md:1321-1339` of the one-database item) name five
surfaces for a status write: the board's drag, a bulk action, the item
screen, `sd status` and the palette's status entry. Four are this
dashboard's, and this module measures each against the one route that
writes a status, `/api/items/<id>/status`; the route table of `server.py`
calls `source:local-sd-db/sd_db/workflow.py::change_status` there and
nowhere else:

* the **item screen** posts that route from its `Change status` form;
* the **palette's status entry** is that same form, re-listed by
  `localActions` in `dashboard.js` from `main [data-workflow-form][data-cli]`,
  so it posts the same route -- the registered-command palette
  (`commands.yaml`) carries no status command of its own;
* the **board** was a view on the classic Backlog, deleted with it in
  sd:2622; `dashboard.js` still carries no drag handler;
* a **bulk action** is a `BulkAction` in `listing.py`, and no section
  constructs one, so no bulk status write exists.

The fifth surface, the pack's `sd task status <item> <status>`, is the
pack's to test. The library half, `change_status` itself and the two
cancels, is `local-sd-db/tests/test_criterion_7.py`; the grep for a second
`UPDATE item SET status` is `test_status_history.py`'s.
"""

from __future__ import annotations

import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import create_assignment, reads, runner, upsert_repo, workflow
from sd_db.writes import STATUSES

from .test_workflow_actions import BrowserSession

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
STATIC = HERE / "sd_dashboard" / "static"

#: See the library half: the cancel is two transactions, not a wait.
CANCEL_BOUND_SECONDS = 2.0


class Criterion7(BrowserSession):
    """An unmerged `work` item on a row-owned repository, behind the session."""

    def setUp(self):
        super().setUp()
        self.checkout = "/repos/system"
        upsert_repo(self.connection, self.checkout, remote="git@example.invalid:system.git")
        self.work = self.item(
            "An unmerged slice", repo=self.checkout, branch="feat/sd-1-slice", status="in_progress",
            source="docs/work", path="docs/work/2026-09-05-slice/prd.md",
            external_id=f"{self.checkout}::docs/work/2026-09-05-slice/prd.md",
        )
        self.status_route = f"/api/items/{self.work}/status"

    # -- readback ----------------------------------------------------------

    def row(self) -> dict:
        return dict(reads.item_by_id(self.connection, self.work))

    def history(self) -> list[str]:
        return [note["body"] for note in reads.status_changes(self.connection, self.work)]

    def page(self) -> str:
        status, _, body = self.request(f"/item/{self.work}")
        self.assertEqual(status, 200)
        return body

    def ask(self, target: str):
        """POST the status route with the current revision; (status, body)."""
        revision = workflow.item_state(self.connection, self.work)["revision"]
        status, _, result = self.post(self.status_route, {"revision": revision, "status": target})
        return status, result

    def refused(self, target: str) -> str:
        """The route's refusal for `target`: 400, row unchanged, no note."""
        before, notes = self.row(), self.history()
        status, result = self.ask(target)
        self.assertEqual(status, 400, result)
        self.assertEqual(self.row(), before, "the item row is unchanged")
        self.assertEqual(self.history(), notes, "and no status_change note was written")
        return result["error"]

    def status_forms(self, page: str) -> list[str]:
        return re.findall(r'<form[^>]*action="/api/items/\d+/status"[^>]*>', page)

    # -- 7.17 ---------------------------------------------------------------

    def test_7_17_the_item_screen_refuses_done_naming_the_delivery_and_offering_cancel(self):
        page = self.page()
        forms = self.status_forms(page)
        self.assertEqual(len(forms), 1, "one status form on the item screen")
        self.assertIn(f'data-cli="sd task status {self.work} STATUS"', forms[0])
        # The form offers no `done`; asked anyway, the route refuses.
        select = re.search(r'<select name="status"[^>]*>(.*?)</select>', page, re.DOTALL).group(1)
        self.assertNotIn('value="done"', select)
        self.assertIn('value="ready"', select)
        message = self.refused("done")
        self.assertRegex(message, r"delivery|merge")
        self.assertRegex(message, r"cancel")
        self.assertEqual(self.row()["status"], "in_progress")

    def test_7_17_the_palette_status_entry_is_the_item_form_and_posts_the_same_route(self):
        script = (STATIC / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('main [data-workflow-form][data-cli]:not([data-palette-form])', script,
                      "the palette lists the page's command-bearing forms as its local entries")
        page = self.page()
        self.assertIn('id="command-palette"', page)
        entries = re.findall(r'<form[^>]*data-workflow-form[^>]*data-cli="(sd task status [^"]*)"[^>]*>', page)
        self.assertEqual(entries, [f"sd task status {self.work} STATUS"])
        source = (HERE / "sd_dashboard" / "server.py").read_text(encoding="utf-8")
        calls = re.findall(r"workflow\.change_status\(", source)
        self.assertEqual(len(calls), 1, "one route writes a status, and the palette's entry reaches it")
        self.assertRegex(self.refused("done"), r"delivery|merge")

    def test_7_17_no_drag_and_no_bulk_action_writes_a_status(self):
        # v1's board went with the classic Backlog (sd:2622); the script it loaded still has no drag.
        script = (STATIC / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"dragstart|dragend|ondrop|\.drop\b|draggable")
        # A bulk action exists only where a section constructs one, and none does.
        found = subprocess.run(
            ["git", "-C", str(ROOT), "grep", "-n", "-I", "-e", r"BulkAction(",
             "--", "local-project-dashboard/sd_dashboard/*.py"],
            capture_output=True, text=True, check=False,
        )
        self.assertIn(found.returncode, (0, 1), found.stderr)
        self.assertEqual([line for line in found.stdout.splitlines() if line.strip()], [])
        # And a status route that takes several items is not a route.
        revision = workflow.item_state(self.connection, self.work)["revision"]
        self.assertEqual(self.post("/api/items/status", {"revision": revision, "status": "done",
                                                          "items": [self.work]})[0], 404)
        self.assertEqual(self.row()["status"], "in_progress")

    # -- 7.19 ---------------------------------------------------------------

    def test_7_19_a_running_row_takes_no_status_write_from_the_item_screen_or_the_palette(self):
        running = create_assignment(self.connection, item=self.work, role="author", status="running")
        page = self.page()
        self.assertEqual(self.status_forms(page), [], "no status form is offered")
        self.assertEqual(re.findall(r'data-cli="sd task status[^"]*"', page), [], "so the palette lists none")
        # Every target in `STATUSES`, the current `in_progress` included: the
        # refusal comes before the library's same-status no-op.
        for target in STATUSES:
            with self.subTest(target=target):
                self.assertRegex(self.refused(target), rf"assignment {running}\b")

    def end(self, assignment: int) -> None:
        self.connection.execute("UPDATE assignment SET status = 'failed' WHERE id = ?", (assignment,))
        self.connection.commit()

    def test_7_19_the_refusal_names_cancel_for_queued_and_no_verb_for_running(self):
        queued = runner.enqueue(self.connection, [self.work], who="operator")[0]["id"]
        message = self.refused("ready")
        self.assertRegex(message, rf"assignment {queued}\b")
        self.assertIn(f"cancel it with `sd assignments cancel {queued}`", message)
        self.assertNotIn("sd runner", message)
        self.assertIn(f'action="/api/assignments/{queued}/cancel"', self.page(), "and the item screen offers it")
        self.end(queued)

        owned = runner.enqueue(self.connection, [self.work], who="operator")[0]["id"]
        root = Path(self.tmp.name)
        runner.claim(self.connection, owned, owner="fixture", work_root=root / "work", retention_root=root / "retained")
        message = self.refused("ready")
        self.assertRegex(message, rf"assignment {owned}\b")
        self.assertIn("no verb cancels a running assignment now that the runner is gone", message)
        # sd:3041: the runner's stop went with the runner; `operations.cancel_assignment` refuses a running row.
        self.assertNotIn(f'/{owned}/cancel"', self.page())
        self.end(owned)

        # A running row with no `runner_run`: the item screen renders no
        # control, and the refusal says no verb cancels it (sd:3041).
        by_hand = create_assignment(self.connection, item=self.work, role="author", status="running")
        self.assertNotIn(f'/{by_hand}/cancel"', self.page())
        message = self.refused("ready")
        self.assertRegex(message, rf"assignment {by_hand}\b")
        self.assertIn("no verb cancels a running assignment now that the runner is gone", message)
        self.assertNotIn("sd runner", message)

    # -- 7.20 ---------------------------------------------------------------

    def test_7_20_the_item_screen_cancels_a_queued_row_with_no_runner_and_frees_the_item(self):
        with patch("subprocess.Popen", side_effect=AssertionError("enqueue started a process")):
            queued = runner.enqueue(self.connection, [self.work], who="operator")[0]
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["run"])
        page = self.page()
        self.assertEqual(self.status_forms(page), [])
        control = re.search(rf'<form[^>]*action="/api/assignments/{queued["id"]}/cancel"[^>]*>.*?</form>', page)
        self.assertIsNotNone(control, "the item screen offers the cancel")
        self.assertIn(f'data-cli="sd assignments cancel {queued["id"]}"', control.group(0))
        revision = re.search(r'name="revision" value="([0-9a-f]+)"', control.group(0))[1]
        self.assertRegex(self.refused("ready"), rf"assignment {queued['id']}\b")

        started = time.perf_counter()
        with patch("subprocess.Popen", side_effect=AssertionError("the cancel started a process")), \
                patch("subprocess.run", side_effect=AssertionError("the cancel ran a process")), \
                patch("time.sleep", side_effect=AssertionError("the cancel slept")), \
                patch.object(runner, "heartbeat_state", side_effect=AssertionError("the cancel read the heartbeat")):
            status, _, result = self.post(f"/api/assignments/{queued['id']}/cancel", {"revision": revision})
        elapsed = time.perf_counter() - started
        self.assertEqual(status, 200, result)
        self.assertLess(elapsed, CANCEL_BOUND_SECONDS)
        row = self.connection.execute("SELECT status, result, ended FROM assignment WHERE id = ?",
                                      (queued["id"],)).fetchone()
        # `cancelled`, the schema's terminal status, where the clause says `blocked` (recorded on the item).
        self.assertEqual(row["status"], "cancelled")
        self.assertTrue(row["ended"])
        status, result = self.ask("ready")
        self.assertEqual(status, 200, result)
        self.assertEqual(self.row()["status"], "ready")
        self.assertIn("in_progress -> ready by dashboard", self.history()[-1])
        self.assertEqual(len(self.status_forms(self.page())), 1, "the status control is back")

    def test_7_20_the_operations_cancel_route_frees_the_item_too(self):
        queued = runner.enqueue(self.connection, [self.work], who="operator")[0]["id"]
        self.assertRegex(self.refused("ready"), rf"assignment {queued}\b")
        state = self.connection.execute("SELECT * FROM assignment WHERE id = ?", (queued,)).fetchone()
        self.assertEqual(state["status"], "queued")
        from sd_db import operations

        revision = operations.assignment_state(self.connection, queued)["revision"]
        with patch("time.sleep", side_effect=AssertionError("the cancel slept")), \
                patch.object(runner, "heartbeat_state", side_effect=AssertionError("the cancel read the heartbeat")):
            status, _, result = self.post(f"/api/assignments/{queued}/cancel", {"revision": revision})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "cancelled")
        self.assertIn(f"Queued assignment {queued} cancelled by dashboard",
                      [note["body"] for note in workflow.item_state(self.connection, self.work)["notes"]][-1])
        self.assertEqual(self.ask("ready")[0], 200)
        self.assertEqual(self.row()["status"], "ready")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
