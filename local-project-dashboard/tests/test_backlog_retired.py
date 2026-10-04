"""v1 /backlog retired into Tasks (sd:2356).

What this slice promises: a `/backlog` address answers 301 to `/tasks` with the
query v1 read (`tasks_screen.from_backlog`), so a bookmark or an old link keeps
its filters; Tasks, loaded at that address, lists the rows v1 listed for it;
v1's screen stays reachable at `/classic/backlog`, in the palette, until its
run selection is deleted.
"""

from __future__ import annotations

import http.client
import re
import unittest
from urllib.parse import parse_qs, urlsplit

from sd_db import upsert_repo, workflow
from sd_dashboard import tasks_screen, v2

import test_v2_tasks
from support import NOW, ScreenCase
from test_screens import listing_content
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


class TheRoundTrip(ScreenCase):
    """Tasks at the moved address lists the rows v1 listed for the old one."""

    run_page = test_v2_tasks.TheScript.run_page

    def setUp(self):
        super().setUp()
        for path in ("/repos/team-a/shared", "/repos/team-b/shared"):
            upsert_repo(self.connection, path, remote="git@example.invalid:x.git")
        self.ready = self.item("Budget review", kind="task", repo="/repos/team-a/shared")
        workflow.change_status(self.connection, self.ready, "ready", who="test")
        self.work = self.item("Port the page", kind="work", repo="/repos/team-b/shared", path="docs/work/port/prd.md")
        self.loose = self.item("Answer the question", kind="followup")
        self.closed = self.item("Closed this week", kind="task", repo="/repos/team-a/shared")
        workflow.change_status(self.connection, self.closed, "done", who="test")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {}

    def v1(self, query: dict) -> set[int]:
        page = self.render("/classic/backlog", {key: [value] for key, value in query.items()})
        return {int(found) for found in re.findall(r'href="/item/(\d+)"', listing_content(page, "backlog"))}

    def tasks(self, address: str) -> set[int]:
        out = self.run_page("R.list = ELS['view-list'].html;", search="?" + urlsplit(address).query)
        return test_v2_tasks.keys(out["R"]["list"])

    def test_every_filter_lists_the_same_rows(self):
        for query in ({}, {"status": "ready"}, {"status": "done"}, {"kind": "followup"}, {"kind": "work"},
                      {"repo": "/repos/team-a/shared"}, {"repo": "none"}, {"active": "1"}, {"q": "budget"},
                      {"active": "1", "repo": "/repos/team-a/shared"}):
            with self.subTest(query=query):
                expected = self.v1(query)
                self.assertEqual(self.tasks(moved(self, query)), expected)
        self.assertEqual(self.v1({"repo": "/repos/team-a/shared"}), {self.ready, self.closed}, "the fixture filters nothing")


class TheRoutes(BrowserSession):
    """The server answers /backlog with the move, and v1's screen at /classic/backlog."""

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

    def test_the_old_screen_answers_at_its_classic_address_and_stays_there(self):
        status, _, page = self.fetch("/classic/backlog?view=board")
        self.assertEqual(status, 200)
        self.assertIn('data-listing="backlog"', page)
        self.assertEqual(v2.SCREENS.get("Backlog (classic)"), "/classic/backlog")
        # Its own links (view toggle, pager, filter) stay on the classic screen rather than moving to Tasks.
        self.assertNotRegex(page, r'href="/backlog[?"]')
        self.assertIn('href="/classic/backlog?view=list"', page)


if __name__ == "__main__":
    unittest.main()
