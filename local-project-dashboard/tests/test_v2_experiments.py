"""An item's experiments row on Tasks (sd:4639, slice E3 of pack sd:3982).

The Details link `fields.experiments.url` in Published and read the item's
designation, with the designated experiments a task can join. The Experiments
field posts `POST /api/items/<n>/experiment`, whose `experiment` list holds the
values `sd task edit <n> --experiment` takes, to `projection.designate`.
Nothing here reaches Notion: the pack's projection only writes the item row.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

from sd_db import projection, workflow
from sd_dashboard import tasks_screen

from support import NOW, ScreenCase
from test_workflow_actions import BrowserSession

ROW = "3c9f52b1-5782-81a7-a466-fb0e2df4d928"
URL = "https://example.test/3c9f52b1578281a7a466fb0e2df4d928"


class Seeded:
    def setUp(self):
        super().setUp()
        self.experiment = self.item("An experiment", kind="task")
        self.task = self.item("A task", kind="task")
        self.other = self.item("Another experiment", kind="task")
        self.connection.commit()

    def designate(self, item, *specs):
        projection.designate(self.connection, item, list(specs), who="test")
        self.connection.commit()

    def held(self, item):
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()[0] or "{}")
        return fields.get("experiments")

    def recorded(self, item, url=URL):
        """The row a drain recorded: `record_row` needs an intent, so the fixture writes the field `record_row` leaves."""
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()[0] or "{}")
        fields["experiments"] = dict(fields["experiments"], row=ROW, url=url)
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), item))
        self.connection.commit()


class TheDetails(Seeded, ScreenCase):
    def test_published_links_the_experiments_row_once_a_drain_recorded_it(self):
        self.designate(self.experiment, "yes")
        self.assertEqual(tasks_screen.details(self.connection, self.experiment, now=NOW)["published"], [])
        self.recorded(self.experiment)
        self.assertEqual(tasks_screen.details(self.connection, self.experiment, now=NOW)["published"], [
            {"title": "Experiments row", "where": "experiments database · sd keeps Status",
             "links": [{"channel": "notion-row", "url": URL, "href": URL}]}])
        # Off keeps the row but drops its database and stops the Status writes; Published must not claim either.
        self.designate(self.experiment, "off")
        self.assertEqual(tasks_screen.details(self.connection, self.experiment, now=NOW)["published"][0]["where"],
                         "projection off · sd no longer writes it")
        self.designate(self.other, "yes")
        self.designate(self.task, f"of:{self.other}")
        self.recorded(self.task)
        self.assertEqual(tasks_screen.details(self.connection, self.task, now=NOW)["published"][0]["where"],
                         "tasks database · sd keeps Status")
        self.recorded(self.experiment, "javascript:alert(1)")
        self.assertIsNone(tasks_screen.details(self.connection, self.experiment, now=NOW)["published"][0]["links"][0]["href"])

    def test_the_designation_and_the_experiments_a_task_can_join(self):
        self.assertEqual(tasks_screen.details(self.connection, self.task, now=NOW)["experiment"],
                         {"value": None, "row": None, "choices": [], "reason": None})
        self.designate(self.experiment, "yes")
        self.designate(self.other, "yes")
        self.designate(self.task, f"of:{self.experiment}")
        self.assertEqual(tasks_screen.details(self.connection, self.task, now=NOW)["experiment"], {
            "value": f"of:{self.experiment}", "row": None, "reason": None,
            "choices": [{"value": f"of:{self.experiment}", "label": f"task of #{self.experiment} An experiment"},
                        {"value": f"of:{self.other}", "label": f"task of #{self.other} Another experiment"}]})
        # One level: an experiment with a task can join none; without, any but itself.
        self.recorded(self.experiment)
        got = tasks_screen.details(self.connection, self.experiment, now=NOW)["experiment"]
        self.assertEqual((got["value"], got["row"], got["choices"]), ("yes", ROW, []))
        self.designate(self.task, "off")
        self.assertEqual(tasks_screen.details(self.connection, self.task, now=NOW)["experiment"]["value"], "off")
        got = tasks_screen.details(self.connection, self.experiment, now=NOW)["experiment"]
        self.assertEqual([c["value"] for c in got["choices"]], [f"of:{self.other}"])

    def test_a_designation_that_cannot_be_read_is_named_not_read_as_none(self):
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?",
                                (json.dumps({"experiments": {"as": "sideways"}}), self.task))
        self.connection.commit()
        got = tasks_screen.details(self.connection, self.task, now=NOW)["experiment"]
        self.assertEqual(got["value"], None)
        self.assertIn("cannot be read", got["reason"])

    def test_a_pack_without_the_projection_says_to_update_it(self):
        with patch.dict(sys.modules, {"sd_db.projection": None}):
            got = tasks_screen.details(self.connection, self.task, now=NOW)["experiment"]
        self.assertEqual(got["reason"], "this sd_db has no projection; update the command pack")


class TheRoute(Seeded, BrowserSession):
    def revision(self, item):
        return workflow.item_state(self.connection, item)["revision"]

    def post_experiment(self, item, specs, revision=None):
        return self.post(f"/api/items/{item}/experiment",
                         {"experiment": specs, "revision": revision or self.revision(item)})

    def test_the_route_designates_as_sd_task_edit_does(self):
        status, _, out = self.post_experiment(self.experiment, ["yes"])
        self.assertEqual(status, 200, out)
        self.assertEqual(self.held(self.experiment)["as"], "experiment")
        self.assertEqual(out["revision"], self.revision(self.experiment))
        status, _, out = self.post_experiment(self.task, [f"of:{self.experiment}"])
        self.assertEqual(status, 200, out)
        self.assertEqual((self.held(self.task)["as"], self.held(self.task)["of"]), ("task", self.experiment))
        status, _, out = self.post_experiment(self.task, [f"row:{URL}"])
        self.assertEqual((status, self.held(self.task)["row"]), (200, ROW), out)
        status, _, out = self.post_experiment(self.task, ["new"])
        self.assertEqual(status, 200, out)
        self.assertNotIn("row", self.held(self.task))

    def test_a_refused_value_answers_400_and_writes_nothing(self):
        before = self.revision(self.task)
        for specs in ([f"of:{self.other}"], ["of:" + str(self.task)], ["sideways"], ["yes", "off"], [], "yes", [1],
                      ["yes"] * 3):
            with self.subTest(specs=specs):
                status, _, out = self.post_experiment(self.task, specs, before)
                self.assertEqual(status, 400, out)
                self.assertEqual(self.revision(self.task), before)
        status, _, out = self.post(f"/api/items/{self.task}/experiment",
                                   {"experiment": ["yes"], "title": "x", "revision": before})
        self.assertEqual(status, 400, out)
        self.assertIsNone(self.held(self.task))

    def test_a_stale_revision_answers_409(self):
        status, _, out = self.post_experiment(self.task, ["yes"], "0" * 64)
        self.assertEqual(status, 409, out)
        self.assertIsNone(self.held(self.task))

    def test_a_pack_without_the_projection_refuses_naming_the_update(self):
        with patch.dict(sys.modules, {"sd_db.projection": None}):
            status, _, out = self.post_experiment(self.task, ["yes"])
        self.assertEqual(status, 400, out)
        self.assertIn("update the command pack", out["error"])
        self.assertIsNone(self.held(self.task))
