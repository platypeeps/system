"""`POST /api/snooze`: the write both Today and Health post (sd:1896)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sd_db import writes

from test_workflow_actions import BrowserSession


def later(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat(timespec="seconds")


class TheSnoozeRoute(BrowserSession):
    def held(self):
        return writes.snoozed(self.connection, now=datetime.now(UTC).isoformat())

    def test_a_snooze_holds_the_row_and_a_null_time_shows_it_again(self):
        until = later(hours=1)
        status, _, body = self.post("/api/snooze", {"page": "today", "row": "ahead:pushy:1", "until": until})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"key": "today:ahead:pushy:1", "until": writes.stamp(until)})
        self.assertEqual(self.held(), {"today:ahead:pushy:1": writes.stamp(until)})
        status, _, body = self.post("/api/snooze", {"page": "today", "row": "ahead:pushy:1", "until": None})
        self.assertEqual((status, body), (200, {"key": "today:ahead:pushy:1", "until": None}))
        self.assertEqual(self.held(), {})

    def test_a_health_row_keys_on_its_own_page(self):
        self.assertEqual(self.post("/api/snooze", {"page": "health", "row": "br:system", "until": later(days=7)})[0], 200)
        self.assertEqual(list(self.held()), ["health:br:system"])

    def test_a_request_it_cannot_judge_writes_nothing(self):
        good = {"page": "today", "row": "ahead:pushy:1", "until": later(hours=1)}
        for bad in ({**good, "page": "tasks"}, {**good, "row": ""}, {**good, "row": 7}, {**good, "until": 3600},
                    {"page": "today", "row": "ahead:pushy:1"}, {**good, "extra": 1},
                    {**good, "until": later(hours=-1)}, {**good, "until": later(days=32)},
                    {**good, "until": "next tuesday"}):
            with self.subTest(bad=bad):
                before = self.snapshot()
                status, _, body = self.post("/api/snooze", bad)
                self.assertEqual(status, 400, body)
                self.assertTrue(body["error"])
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.post("/api/snooze", good, Origin="https://example.invalid")[0], 403)
        self.assertEqual(self.held(), {})
