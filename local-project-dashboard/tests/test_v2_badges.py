"""Every rail badge on every page (sd:3446).

What this slice promises: `/ui/sections.js` is written on each request, unversioned and never cached, and carries each
page's badge as `SHELL_COUNTS`, from the one rule the page's server read holds (`Page.count`). A `live` page's read runs
again on each request; any other page shows what its own last read carried, with that read's time, so the rail never
waits on the network. A failed or shapeless read is `unknown`, an older read never replaces a newer one, and a request
without a session gets the map and no badge. The pages' own rules are each page's test; this one names no real page.
"""

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from sd_dashboard import v2
from sd_dashboard.v2 import pages as registry
from sd_dashboard.v2.pages import Api, Page, Read, attention

from test_workflow_actions import BrowserSession

WHAT = {"warning": "rows broken", "caution": "rows to watch"}


def counts(body: str) -> dict:
    declared = dict(re.findall(r"^window\.(SHELL_\w+) = (.*);$", body, re.M))
    return json.loads(declared["SHELL_COUNTS"])["pages"]


class Stand:
    """Two example pages: Live reads `/api/example-live` on each request, Kept shows its last `/api/example-kept`."""

    def __init__(self):
        self.live = {"attention": {"state": "caution", "n": 2, "what": "rows to watch"}}
        self.kept = {"attention": {"state": "warning", "n": 1, "what": "rows broken"}}
        self.calls = []

    def read_live(self, read):
        self.calls.append(("live", read.path))
        if isinstance(self.live, Exception):
            raise self.live
        return self.live

    def read_kept(self, read):
        self.calls.append(("kept", read.path))
        if isinstance(self.kept, Exception):
            raise self.kept
        return self.kept

    def pages(self):
        return registry.PAGES + (
            Page(section="Live", routes=("/example-live",), html="tasks.html", item="sd:3446",
                 api=(Api("/api/example-live", self.read_live),), count="/api/example-live", live=True),
            Page(section="Kept", routes=("/example-kept",), html="tasks.html", item="sd:3446",
                 api=(Api("/api/example-kept", self.read_kept),), count="/api/example-kept"),
        )


class TheRule(unittest.TestCase):
    def test_the_louder_state_wins_and_counts_its_rows(self):
        self.assertEqual(attention(["caution", "warning", "caution", ""], WHAT), {"state": "warning", "n": 1, "what": "rows broken"})
        self.assertEqual(attention(["caution", "ok", "caution"], WHAT), {"state": "caution", "n": 2, "what": "rows to watch"})
        self.assertEqual(attention([], WHAT), {"state": "ok", "n": 0, "what": "rows to watch"})
        self.assertEqual(attention([], {**WHAT, "ok": "nothing due"})["what"], "nothing due")


class TheBadges(unittest.TestCase):
    def setUp(self):
        self.stand = Stand()
        patcher = patch.object(registry, "PAGES", self.stand.pages())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.badges = v2.Badges()

    def test_a_live_page_reads_on_each_request_and_a_kept_page_never(self):
        found = self.badges.read(Read(now="2026-10-13T12:00:00Z"))
        self.assertEqual(found["Live"], {"state": "caution", "n": 2, "what": "rows to watch"})
        self.assertNotIn("Kept", found)
        self.stand.live = {"attention": {"state": "ok", "n": 0, "what": "rows to watch"}}
        self.assertEqual(self.badges.read(Read(now="2026-10-13T12:00:01Z"))["Live"]["state"], "ok")
        self.assertEqual(self.stand.calls, [("live", "/api/example-live")] * 2)

    def test_a_kept_page_shows_its_last_read_with_its_time(self):
        self.badges.remember("/api/example-kept", self.stand.kept, "2026-10-13T12:00:00Z")
        self.assertEqual(self.badges.read(Read())["Kept"],
                         {"state": "warning", "n": 1, "what": "rows broken", "at": "2026-10-13T12:00:00Z"})

    def test_a_read_another_page_counts_is_kept_for_that_page_alone(self):
        # Evidence: the badge goes to the page that names the path, never to the page whose read ran first.
        for path in ("/api/example-live", "/api/tasks", "/api/example-kept/1", "/api/example"):
            self.badges.remember(path, self.stand.kept, "2026-10-13T12:00:00Z")
        self.assertNotIn("Kept", self.badges.read(Read()))

    def test_a_failed_or_shapeless_read_is_unknown_and_not_the_value_before_it(self):
        self.badges.remember("/api/example-kept", self.stand.kept, "2026-10-13T12:00:00Z")
        self.badges.remember("/api/example-kept", None, "2026-10-13T12:01:00Z")
        self.assertEqual(self.badges.read(Read())["Kept"],
                         {"state": "unknown", "n": 0, "what": "Kept read failed", "at": "2026-10-13T12:01:00Z"})
        for shapeless in ({}, {"attention": None}, {"attention": {"state": "loud", "n": 1, "what": "x"}},
                          {"attention": {"state": "caution", "n": -1, "what": "x"}},
                          {"attention": {"state": "caution", "n": True, "what": "x"}},
                          {"attention": {"state": "caution", "n": 1, "what": "x", "extra": 1}}, [1]):
            self.badges.remember("/api/example-kept", self.stand.kept, "2026-10-13T12:02:00Z")
            self.badges.remember("/api/example-kept", shapeless, "2026-10-13T12:03:00Z")
            self.assertEqual(self.badges.read(Read())["Kept"]["state"], "unknown", shapeless)

    def test_an_older_read_never_replaces_a_newer_one(self):
        # Steps: two reads of one page finish out of order; the badge keeps the one read later.
        self.badges.remember("/api/example-kept", self.stand.kept, "2026-10-13T12:05:00Z")
        self.badges.remember("/api/example-kept", {"attention": {"state": "ok", "n": 0, "what": "x"}}, "2026-10-13T12:04:00Z")
        self.assertEqual(self.badges.read(Read())["Kept"]["at"], "2026-10-13T12:05:00Z")

    def test_a_live_read_that_fails_is_unknown_and_the_rest_still_show(self):
        self.badges.remember("/api/example-kept", self.stand.kept, "2026-10-13T12:00:00Z")
        for failure in (RuntimeError("no database"), (503, {"error": "down"}),
                        {"attention": {"state": "caution", "n": "2", "what": "x"}}):
            self.stand.live = failure
            with patch("sys.stderr"):
                found = self.badges.read(Read())
            self.assertEqual(found["Live"], {"state": "unknown", "n": 0, "what": "Live not read"}, failure)
            self.assertEqual(found["Kept"]["state"], "warning")

    def test_each_server_keeps_its_own(self):
        self.badges.remember("/api/example-kept", self.stand.kept, "2026-10-13T12:00:00Z")
        self.assertNotIn("Kept", v2.Badges().read(Read()))


class TheScript(BrowserSession):
    def setUp(self):
        self.stand = Stand()
        patcher = patch.object(registry, "PAGES", self.stand.pages())
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def get(self, path, cookie=True):
        return self.request(path, headers={"Cookie": self.cookie} if cookie else {})

    def test_it_is_written_per_request_and_never_cached(self):
        status, headers, body = self.get("/ui/sections.js")
        self.assertEqual((status, headers["Content-Type"], headers["Cache-Control"]),
                         (200, "text/javascript; charset=utf-8", "no-store"))
        self.assertIn("Live", counts(body))
        # A page links it without a digest, and an old page's digest link gets today's badges too.
        _, _, page = self.get("/tasks")
        self.assertIn('<script src="/ui/sections.js"></script>', page)
        self.assertNotIn("sections.js", v2.VERSIONS)
        status, headers, _ = self.get("/ui/sections.js?v=0123456789abcdef")
        self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))

    def test_without_a_session_it_carries_the_map_and_no_badge(self):
        status, _, body = self.get("/ui/sections.js", cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(counts(body), {})
        self.assertIn("window.SHELL_PAGES = ", body)
        self.assertEqual(self.stand.calls, [])

    def test_a_kept_page_shows_what_its_own_read_carried(self):
        self.assertNotIn("Kept", counts(self.get("/ui/sections.js")[2]))
        self.assertEqual(self.get("/api/example-kept")[0], 200)
        self.assertEqual(counts(self.get("/ui/sections.js")[2])["Kept"]["state"], "warning")
        # The kept page's read runs only when the page reads it, never for the rail.
        self.assertEqual([call for call in self.stand.calls if call[0] == "kept"], [("kept", "/api/example-kept")])

    def test_a_kept_read_that_fails_leaves_unknown(self):
        self.get("/api/example-kept")
        self.stand.kept = (503, {"error": "the collector is down"})
        self.assertEqual(self.get("/api/example-kept")[0], 503)
        self.assertEqual(counts(self.get("/ui/sections.js")[2])["Kept"]["state"], "unknown")
        self.stand.kept = {"attention": {"state": "caution", "n": 4, "what": "x"}}
        self.get("/api/example-kept")
        self.stand.kept = KeyError("broken read")
        with patch("sys.stderr"):
            try:
                self.get("/api/example-kept")
            except OSError:
                pass  # the handler's exception drops the connection; the badge is what this test reads
        self.assertEqual(counts(self.get("/ui/sections.js")[2])["Kept"]["state"], "unknown")

    def test_a_v2_bookmark_still_reaches_it(self):
        status, headers, _ = self.request("/v2/static/sections.js")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
