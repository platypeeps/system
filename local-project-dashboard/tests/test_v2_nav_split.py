"""The rail split by job (sd:5781).

What this slice promises: the rail's Now group is Today, Decide, Briefs and Queue, and its Work group gains GitHub after
Tasks. Today has no tabs since sd:5921. Decide holds Decisions and Scoring, the Review page renamed; its
rail badge counts both. GitHub holds Pull requests, Your issues and Intake. Each old address reaches its new home with its
query: `/review` serves Decide and opens Scoring, and Today's `#view=decisions`, `#view=github` and `#view=issues` open the
view on its new page. The `moved:start` blocks run under JavaScriptCore (osascript) with a stand-in location and history.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_db import repos
from sd_dashboard import now_screen, review_screen, v2
from sd_dashboard.v2.pages import Read

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document
from test_v2_registry import GROUP_BLOCK, Registers, registry
from test_v2_review import seed
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
STATIC = V2 / "static"


def js(name):
    path = STATIC / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def views(script):
    """The ids and labels of a page script's window.PAGE_VIEWS."""
    found = re.search(r"window\.PAGE_VIEWS = (\[.*?\]);", script, re.S)
    return re.findall(r"id: '([a-z-]+)', label: '([^']+)'", found.group(1)) if found else []


def panels(page):
    path = V2 / page
    return re.findall(r'<div id="view-([a-z-]+)"', path.read_text(encoding="utf-8")) if path.is_file() else []


def group(name):
    found = re.search(rf"\['{name}',\s*\[(.*?)\]\],", GROUP_BLOCK)
    return re.findall(r"\['([A-Za-z ]+)', '([a-z0-9-]+)', '([a-z]?)'\]", found.group(1))


def moved(script, *, path="/today", search="", hash=""):
    """What a page script's moved:start block asks of the address: each replace or replaceState it makes."""
    block = re.search(r"^// moved:start\n(.*?)^// moved:end$", script, re.S | re.M)
    if block is None:
        return None
    stand_in = (f"var calls = [], location = {{ pathname: {json.dumps(path)}, search: {json.dumps(search)}, hash: {json.dumps(hash)},"
                " replace: u => calls.push(['replace', u]) };\n"
                "var history = { state: null, replaceState: (a, b, u) => calls.push(['replaceState', u]) };\n")
    result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", stand_in + block.group(1) + "\nJSON.stringify(calls)"],
                            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class TheRail(unittest.TestCase):
    def test_now_is_today_decide_briefs_and_queue(self):
        self.assertEqual([name for name, _, _ in group("Now")], ["Today", "Decide", "Briefs", "Queue"])
        # Decide keeps Review's chord and icon: g l still opens the queue's page.
        self.assertIn(("Decide", "list-checks", "l"), group("Now"))

    def test_work_gains_github_after_tasks(self):
        self.assertEqual([name for name, _, _ in group("Work")], ["Tasks", "GitHub", "Writing", "Research", "Contributions"])
        self.assertIn(("GitHub", "git-branch", "b"), group("Work"))
        self.assertIn('<symbol id="i-git-branch"', js("icons.js"))

    def test_no_page_is_named_review_any_more(self):
        sections = {page.section for page in registry().PAGES}
        self.assertLessEqual({"Decide", "GitHub"}, sections)
        self.assertNotIn("Review", sections)
        self.assertNotIn("['Review',", GROUP_BLOCK)


class TheGitHubRegistration(Registers, unittest.TestCase):
    page, section, route = "github", "GitHub", "/github"
    api = ("/api/today/github", "/api/github/issues", "/api/today/github/copilot-review", "/api/today/github/merge/preview",
           "/api/today/github/merge", "/api/today/github/close/preview", "/api/today/github/close")


class TheTabs(unittest.TestCase):
    def test_today_has_no_tabs(self):
        # sd:5921 folded Now, Inbox, Status and Work into one page.
        self.assertEqual((views(js("today.js")), panels("today.html")), ([], []))
        page = (V2 / "today.html").read_text(encoding="utf-8")
        for gone in ("/ui/decisions.js", "/ui/today-github.js", "/ui/issues.js", "/ui/decisions.css"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, page)

    def test_decide_has_decisions_and_scoring(self):
        self.assertEqual(views(js("decide.js")), [("decisions", "Decisions"), ("scoring", "Scoring")])
        self.assertEqual(panels("decide.html"), ["decisions", "scoring"])
        page = (V2 / "decide.html").read_text(encoding="utf-8")
        self.assertIn("<title>Decide · system</title>", page)
        self.assertIn('data-page="Decide"', page)
        scripts = re.findall(r'<script src="/ui/([\w-]+\.js)"></script>', page)
        self.assertLess(scripts.index("decide.js"), scripts.index("review.js"))
        self.assertLess(scripts.index("decisions.js"), scripts.index("shell.js"))
        found = {page.name: page for page in registry().PAGES}["decide"]
        self.assertEqual((found.section, found.routes, found.html), ("Decide", ("/decide", "/review"), "decide.html"))

    def test_github_has_pull_requests_your_issues_and_intake(self):
        self.assertEqual(views(js("github.js")), [("pulls", "Pull requests"), ("issues", "Your issues"), ("intake", "Intake")])
        self.assertEqual(panels("github.html"), ["pulls", "issues", "intake"])
        page = (V2 / "github.html").read_text(encoding="utf-8")
        for there in ('id="github-sections"', '<tbody id="issues">', '<tbody id="intake">', 'id="details"'):
            with self.subTest(there=there):
                self.assertIn(there, page)
        # The view keys differ: Your issues takes y, so i is Intake's.
        self.assertIn("{ id: 'issues', label: 'Your issues', key: 'y' }", js("github.js"))

    def test_the_moved_scripts_name_no_today_hook(self):
        # todayPick and todayCount were today.js's; the moved views now count and select on their own page.
        for name in ("today-github.js", "issues.js", "decisions.js", "review.js"):
            with self.subTest(name=name):
                self.assertNotRegex(js(name), r"todayPick|todayCount|today:decisions")


class TheOldAddresses(unittest.TestCase):
    def test_review_opens_scoring_with_its_query(self):
        self.assertEqual(moved(js("decide.js"), path="/review", search="?batch=7&reviewed=1"),
                         [["replaceState", "/decide?batch=7&reviewed=1#view=scoring"]])
        self.assertEqual(moved(js("decide.js"), path="/review"), [["replaceState", "/decide#view=scoring"]])
        self.assertEqual(moved(js("decide.js"), path="/decide", hash="#view=decisions"), [])

    def test_todays_moved_views_open_their_new_page_with_the_query(self):
        today = js("today.js")
        self.assertEqual(moved(today, search="?row=7", hash="#view=decisions"), [["replace", "/decide?row=7#view=decisions"]])
        self.assertEqual(moved(today, search="?row=gh%3Aexample%2Frepo%2312&gh=all", hash="#view=github"),
                         [["replace", "/github?row=gh%3Aexample%2Frepo%2312&gh=all#view=pulls"]])
        self.assertEqual(moved(today, hash="#view=issues"), [["replace", "/github#view=issues"]])
        for old in ("now", "inbox", "status", "work"):
            with self.subTest(view=old):
                self.assertEqual(moved(today, search="?row=item%3A7", hash=f"#view={old}"),
                                 [["replaceState", "/today?row=item%3A7"]])
        for stays in ("#main", ""):
            with self.subTest(hash=stays):
                self.assertEqual(moved(today, hash=stays), [])


class TheBadges(ScreenCase):
    def test_decides_badge_counts_the_decisions_and_the_questions_to_score(self):
        seed(self)
        scoring = review_screen.document(self.connection)["attention"]["n"]
        self.assertGreater(scoring, 0)
        asked = self.item("Plain work")
        self.note(asked, "Which?\nOption: A\nOption: B", kind="question")
        found = {page.name: page for page in registry().PAGES}["decide"]
        self.assertEqual((found.count, found.live), ("/api/decisions", True))
        _, route = v2.api(found.count)
        doc = route.read(Read(connection=self.connection, now=NOW, path=found.count))
        self.assertEqual(doc["attention"], {"state": "caution", "n": 1 + scoring, "what": "decisions waiting and questions open"})

    def test_todays_badge_no_longer_counts_decisions(self):
        asked = self.item("Plain work")
        self.note(asked, "Which?\nOption: A\nOption: B", kind="question")
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(root), fleet=lambda area: fleet_document(area))
        self.assertEqual(doc["attention"], {"state": "ok", "n": 0, "what": "rows"})
        for gone in ("waiting", "issues", "intake"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, doc)


class TheRoutes(BrowserSession):
    def test_the_old_review_address_serves_decide(self):
        status, _, body = self.request("/review?batch=7")
        self.assertEqual(status, 200)
        self.assertIn("<title>Decide · system</title>", body)
        self.assertIn('<script src="/ui/decide.js?v=', body)

    def test_github_serves_its_page_and_its_issue_read(self):
        status, _, body = self.request("/github")
        self.assertEqual(status, 200)
        self.assertIn("<title>GitHub · system</title>", body)
        with mock.patch.object(repos, "mine_issues", create=True, return_value={}), \
                mock.patch.object(repos, "intake_issues", create=True, return_value={}):
            status, _, out = self.request("/api/github/issues", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(out), {"issues": [], "issues_error": "", "github_error": "", "intake": [], "intake_error": ""})


if __name__ == "__main__":
    unittest.main()
