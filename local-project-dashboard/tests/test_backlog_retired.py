"""v1 /backlog retired into Tasks (sd:2356).

What this slice promises: a `/backlog` address answers 301 to `/tasks` with the
query v1 read (`tasks_screen.from_backlog`), so a bookmark or an old link keeps
its filters. v1's screen, kept at `/classic/backlog` until its run selection
went, was deleted in sd:2622: that address is a 404, and the palette no longer
offers it.
"""

from __future__ import annotations

import http.client
import unittest
from urllib.parse import parse_qs, urlsplit

from sd_db import upsert_repo
from sd_dashboard import tasks_screen, v2

from support import NOW, ScreenCase
from test_workflow_actions import BrowserSession


def moved(case, query: dict) -> str:
    return tasks_screen.from_backlog(case.connection, {key: [value] for key, value in query.items()}, now=NOW)


class TheAddress(ScreenCase):
    """`from_backlog` maps each name v1 read onto the one Tasks reads."""

    def test_the_query_v1_read_carries_over(self):
        self.assertEqual(moved(self, {}), "/tasks?view=list")
        self.assertEqual(moved(self, {"view": "board"}), "/tasks?view=board")
        self.assertEqual(moved(self, {"view": "../../etc"}), "/tasks?view=list")
        self.assertEqual(moved(self, {"view": "matrix", "kind": "task", "status": "ready", "age": "7", "active": "1",
                                      "q": "budget review", "page": "2", "skill": "sd-review"}),
                         "/tasks?view=matrix&kind=task&status=ready&age=7&active=1&q=budget%20review&page=2&skill=sd-review")

    def test_what_v1_would_not_take_does_not_carry_over(self):
        # v1 read active=1 only and a page of digits; page 1 is the default; its run picks (sel) are not a run chosen here.
        self.assertEqual(moved(self, {"active": "0", "page": "1", "sel": "4"}), "/tasks?view=list")
        self.assertEqual(moved(self, {"page": "two"}), "/tasks?view=list")

    def test_a_repository_path_becomes_the_label_tasks_filters_on(self):
        for path in ("/repos/team-a/shared", "/repos/team-b/shared"):
            upsert_repo(self.connection, path)
            self.item("Shared work", repo=path)
        self.assertEqual(parse_qs(urlsplit(moved(self, {"repo": "/repos/team-a/shared"})).query)["repo"], ["team-a/shared"])
        self.assertEqual(parse_qs(urlsplit(moved(self, {"repo": "none"})).query)["repo"], ["no repo"])
        # A path no open row has keeps its value: Tasks then lists nothing, as v1 did.
        self.assertEqual(parse_qs(urlsplit(moved(self, {"repo": "/repos/gone"})).query)["repo"], ["/repos/gone"])


class TheRoutes(BrowserSession):
    """The server answers /backlog with the move, and /classic/backlog with nothing."""

    def fetch(self, path: str):
        parts = urlsplit(self.base)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", path)
        answer = connection.getresponse()
        return answer.status, answer.getheader("Location"), answer.read().decode("utf-8")

    def test_backlog_moves_to_tasks_with_its_query(self):
        status, location, body = self.fetch("/backlog?view=board&status=ready&age=7&active=1&q=x&sel=3")
        self.assertEqual((status, location, body), (301, "/tasks?view=board&status=ready&age=7&active=1&q=x", ""))
        self.assertEqual(self.fetch("/backlog")[:2], (301, "/tasks?view=list"))

    def test_the_old_screen_is_gone_from_its_classic_address_and_the_palette(self):
        status, _, page = self.fetch("/classic/backlog?view=board")
        self.assertEqual(status, 404)
        self.assertNotIn('data-listing="backlog"', page)
        self.assertNotIn("/classic/backlog", v2.SCREENS.values())

if __name__ == "__main__":
    unittest.main()
