"""The run dialogs take a `budget_usd` beside `budget_minutes`, and a refusal
reaches the page. sd:235 criterion 4's creation clause, the dashboard half.

The fixture is `WorkflowControls`', whose registry has `claude`, a `start`
entry, first in the author list: what a budget is refused on. Its own tests
are not run again from here: `load_tests` names this module's one.
"""
import json
import re
import unittest

from sd_db import workflow

from .test_controls_actions import WorkflowControls


class RunBudget(WorkflowControls):
    def test_both_run_dialogs_offer_a_budget_usd_and_the_refusal_reaches_the_page(self):
        self.repo()
        item = self.item("Budgeted", kind="task", repo="/repos/system", branch="task/budget", status="ready")
        # The field renders on the item page and on the Backlog's selection dialog, optional and empty.
        for path in (f"/item/{item}", "/backlog"):
            with self.subTest(path=path):
                page = self.request(path)[2]
                found = re.search(r'<input[^>]*name="budget_usd"[^>]*>', page)
                self.assertIsNotNone(found, path)
                self.assertNotIn(" required", found.group(0))
                self.assertIn('type="number"', found.group(0))
                self.assertIn('name="budget_minutes"', page)
        revision = workflow.item_state(self.connection, item)["revision"]
        payload = {"items": [item], "revisions": {str(item): revision}, "budget_usd": 5.0}
        before = self.snapshot()
        status, _, result = self.post("/api/run", payload)
        self.assertEqual(status, 400)
        self.assertIn("author 'claude' is a 'start' entry", json.dumps(result))
        self.assertEqual(before, self.snapshot())
        # A bad number is refused in the neighbours' words, before the ledger sees it.
        for bad in ("5", -1, True, 10 ** 400):
            with self.subTest(bad=bad):
                status, _, result = self.post("/api/run", {**payload, "budget_usd": bad})
                self.assertEqual(status, 400)
                self.assertIn("choose a budget in US dollars of zero or more, or leave it empty", result["error"])
        self.assertEqual(before, self.snapshot())
        # No budget queues the row with NULL, as before.
        status, _, result = self.post("/api/run", {"items": [item], "revisions": {str(item): revision}})
        self.assertEqual(status, 200)
        self.assertIsNone(result["assignments"][0]["budget_usd"])


def load_tests(loader, tests, pattern):
    """Only this module's own test: the inherited ones already run from
    `test_controls_actions`, and the base class is imported here for its
    fixture, not its cases."""
    return unittest.TestSuite(loader.loadTestsFromNames(
        [f"{RunBudget.__module__}.RunBudget.{name}" for name in loader.getTestCaseNames(RunBudget)
         if name not in loader.getTestCaseNames(WorkflowControls)]))
