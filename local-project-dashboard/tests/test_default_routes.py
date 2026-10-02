"""The new design is the default (sd:2163).

What this slice promises: `/` and `/today` serve the new Today; every `/v2/`
page or file a bookmark can name answers 301 with its new address, and
nothing else under `/v2/` redirects; the old Today lives at `/classic/today`
and every other old screen keeps its path; and each rail section that is not
ported opens its old screen, from one map (`v2.SECTIONS`, `v2.CLASSIC`,
`v2.SCREENS`) that the shell reads as `/ui/sections.js`.
"""

from __future__ import annotations

import http.client
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from sd_db import services

from sd_dashboard import server, v2

from test_now_screen import JobsBackend, fleet_document, repo
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent

#: Every old screen, and where it lives now. The old Today is the one move: `/` and `/today` are the new Today's.
OLD_SCREENS = ("/classic/today", "/backlog", "/contributions", "/protection", "/writing", "/skills",
               "/documents", "/designs", "/operations",
               *(f"/operations?area={key}" for key in ("jobs", "services", "ports", "progress", "usage", "reports",
                                                        "resources", "trackers", "repos", "sessions", "commands")))


def ports_fixture():
    return {"services": [], "listeners": {}, "complete": True}


class TheDefault(BrowserSession):
    fleet_backend = staticmethod(lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.backend = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        # No collector child runs in a test: the repos, sessions and resources areas read fixtures.
        for target, value in (("sd_dashboard.repos_screen.collect", lambda area: fleet_document(area)),
                              ("sd_dashboard.sessions_screen.collect", lambda area: fleet_document(area)),
                              ("sd_dashboard.reports_screen.collect", lambda area: "")):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        self.listening.RequestHandlerClass.ports_backend = staticmethod(ports_fixture)
        empty = Path(root.name) / "launchd"
        empty.mkdir()
        # No service is installed, so the Services area makes no launchctl call.
        self.listening.RequestHandlerClass.services_backend = services.ServiceBackend(
            user_agents=empty, system_daemons=empty, runner=lambda argv: self.fail(f"launchctl {argv}"))

    def raw(self, path, method="GET", headers=None):
        """One request, redirects not followed."""
        host, port = self.listening.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=10)
        try:
            connection.request(method, path, headers=headers or {})
            answer = connection.getresponse()
            return answer.status, answer.headers, answer.read().decode("utf-8", "replace")
        finally:
            connection.close()

    def test_root_and_today_serve_the_new_today_under_the_shared_policy(self):
        for path in ("/", "/today"):
            status, headers, body = self.raw(path)
            self.assertEqual(status, 200, path)
            self.assertIn("<title>Today · system</title>", body, path)
            self.assertEqual(headers["Content-Security-Policy"], server.CSP, path)
            self.assertIn("sd_session=", headers["Set-Cookie"], path)
            self.assertNotRegex(body, r"""(?:src|href)="/v2/""", path)

    def test_a_v2_page_or_file_redirects_permanently_to_its_new_address(self):
        for old, new in (("/v2/today", "/today"), ("/v2/today?since=2026-09-29", "/today?since=2026-09-29"),
                         ("/v2/static/shell.js", "/ui/shell.js"),
                         ("/v2/static/fonts/ibm-plex-sans-var.woff2", "/ui/fonts/ibm-plex-sans-var.woff2")):
            for method in ("GET", "HEAD"):
                status, headers, _ = self.raw(old, method)
                self.assertEqual((status, headers["Location"]), (301, new), (method, old))
                self.assertEqual(headers["Content-Security-Policy"], server.CSP, old)
            self.assertEqual(self.raw(new)[0], 200, new)

    def test_a_v2_path_that_names_nothing_is_a_404_and_never_a_redirect(self):
        for path in ("/v2/", "/v2/nowhere", "/v2//example.test/x", "/v2/static//example.test/x.js",
                     "/v2/static/nowhere.js", "/v2/static/../__init__.py", "/v2/static/%2e%2e/__init__.py",
                     "/v2/static/", "/v2/today.html", "/ui/nowhere.js", "/ui/../__init__.py", "/ui/"):
            status, headers, _ = self.raw(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn("Location", headers, path)
            self.assertEqual(headers["Content-Security-Policy"], server.CSP, path)

    def test_the_old_today_lives_at_classic_today_and_its_lists_stay_there(self):
        status, _, body = self.raw("/classic/today")
        self.assertEqual(status, 200)
        self.assertIn("<title>Today — sd</title>", body)
        actions = set(re.findall(r'<form[^>]*method="get"[^>]*action="([^"]*)"', body))
        self.assertIn("/classic/today", actions)
        self.assertNotIn("/", actions)

    def test_every_old_screen_answers_at_its_address(self):
        for path in OLD_SCREENS:
            status, headers, body = self.raw(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Security-Policy"], server.CSP, path)
            self.assertIn("— sd</title>", body, path)

    def test_every_rail_and_palette_target_answers(self):
        targets = {**v2.SECTIONS, **v2.CLASSIC, **v2.SCREENS}
        for name, href in targets.items():
            status, _, _ = self.raw(href)
            self.assertEqual(status, 200, (name, href))

    def test_the_rail_map_is_served_once_and_reaches_the_operations_screens(self):
        status, headers, body = self.raw("/ui/sections.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        declared = dict(re.findall(r"^window\.(SHELL_\w+) = (.*);$", body, re.M))
        self.assertEqual({key: json.loads(value) for key, value in declared.items()},
                         {"SHELL_PAGES": v2.SECTIONS, "SHELL_CLASSIC": v2.CLASSIC, "SHELL_SCREENS": v2.SCREENS})
        self.assertEqual(v2.SECTIONS, {"Today": "/today", "Briefs": "/briefs", "Tasks": "/tasks", "Management": "/management", "Home": "/home",
                                       "Health": "/fleet-health", "Research": "/research"})
        reachable = set(v2.CLASSIC.values()) | set(v2.SCREENS.values())
        for required in ("/operations?area=jobs", "/operations?area=trackers", "/operations?area=ports",
                         "/operations?area=repos", "/protection", "/designs", "/classic/today", "/backlog",
                         "/operations?area=resources"):
            self.assertIn(required, reachable)
        # A ported section is never also classic, and no page script names a section's address.
        self.assertEqual(set(v2.SECTIONS) & set(v2.CLASSIC), set())
        for script in (V2 / "static").glob("*.js"):
            self.assertNotIn("SHELL_PAGES =", script.read_text(encoding="utf-8"), script.name)

    def test_the_session_guard_holds_at_the_new_address(self):
        status, _, _ = self.raw("/api/now")
        self.assertEqual(status, 403)
        _, headers, _ = self.raw("/today")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, _ = self.raw("/api/now", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
