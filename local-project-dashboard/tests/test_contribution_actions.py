"""Contribution HTTP boundaries retain session, exact-event and revision checks."""

import json
from unittest.mock import patch

from sd_db import contributions, workflow

from tests.test_contribution_screen import contribution, seed_registered
from tests.test_workflow_actions import BrowserSession


class ContributionActions(BrowserSession):
    def test_real_ack_preserves_other_source_and_local_task_status(self):
        item = seed_registered(self.connection)
        row = contributions.projection(self.connection)[0]
        dependency, pull = row["attention_sources"]
        payload = {"key": dependency["key"], "revision": dependency["revision"], "event_ids": dependency["event_ids"]}
        original_status = workflow.item_state(self.connection, item)["item"]["status"]
        original_pull = contributions.snapshot(self.connection, pull["key"])
        status, _, _ = self.post("/api/contributions/acknowledge", payload)
        self.assertEqual(status, 200)
        self.assertEqual(contributions.snapshot(self.connection, pull["key"]), original_pull)
        remaining = contributions.projection(self.connection)[0]
        self.assertEqual(remaining["lane"], "awaiting_you")
        self.assertEqual([source["key"] for source in remaining["attention_sources"]], [pull["key"]])
        self.assertEqual(workflow.item_state(self.connection, item)["item"]["status"], original_status)
        before = self.snapshot()
        status, _, response = self.post("/api/contributions/acknowledge", payload)
        self.assertEqual(status, 409)
        self.assertTrue(response["reload"])
        self.assertEqual(self.snapshot(), before)

    def test_projection_api_matches_shared_rows_without_a_write(self):
        rows = [contribution(2, "newly_unblocked"), contribution(1, "merged")]
        before = self.snapshot()
        with patch.object(contributions, "projection", return_value=rows):
            status, _, body = self.request("/api/contributions", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"contributions": rows})
        self.assertEqual(self.snapshot(), before)

    def test_api_requires_session_and_refuses_unsupported_filters(self):
        before = self.snapshot()
        self.assertEqual(self.request("/api/contributions")[0], 403)
        self.assertEqual(self.request("/api/contributions?repo=x", headers={"Cookie": self.cookie})[0], 400)
        self.assertEqual(self.snapshot(), before)

    def test_ack_forwards_only_exact_rendered_checkpoint(self):
        payload = {"key": "item:14", "revision": "b" * 64, "event_ids": ["event-1", "event-2"]}
        with patch.object(contributions, "acknowledge", return_value={"ok": True}) as acknowledge:
            status, _, result = self.post("/api/contributions/acknowledge", payload)
        self.assertEqual((status, result), (200, {"ok": True}))
        args, kwargs = acknowledge.call_args
        self.assertEqual(args[1:], (payload["key"], payload["event_ids"]))
        self.assertEqual(kwargs, {"expected_revision": payload["revision"], "who": "dashboard"})

    def test_stale_ack_keeps_current_events_and_reports_reload(self):
        before = self.snapshot()
        with patch.object(contributions, "acknowledge", side_effect=workflow.StaleItem("Events changed")):
            status, _, result = self.post("/api/contributions/acknowledge",
                {"key": "item:14", "revision": "a" * 64, "event_ids": ["event-1"]})
        self.assertEqual(status, 409)
        self.assertTrue(result["reload"])
        self.assertEqual(self.snapshot(), before)

    def test_malformed_and_cross_origin_ack_never_reaches_shared_writer(self):
        good = {"key": "item:14", "revision": "a" * 64, "event_ids": ["event-1"]}
        before = self.snapshot()
        with patch.object(contributions, "acknowledge") as acknowledge:
            for changes in ({"event_ids": "event-1"}, {"event_ids": []}, {"event_ids": [1]},
                            {"key": 14}, {"revision": True}, {"all": True}):
                self.assertEqual(self.post("/api/contributions/acknowledge", {**good, **changes})[0], 400)
            self.assertEqual(self.post("/api/contributions/acknowledge", good, Origin="https://example.invalid")[0], 403)
        acknowledge.assert_not_called()
        self.assertEqual(self.snapshot(), before)
