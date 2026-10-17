"""Your issues on Today and Tasks (sd:4628): each repo's Jira and GitHub issues assigned to or filed by the operator.

The rows come from `sd_db.repos.mine_issues` (pack sd:4636), which filters `shadow` on its `reason` column. The pin in
`.sd-pack-rev` predates both, so these stub the read: what is under test is the page's half, the document, its failure
sentences and the drawn table. The pack's own suite tests the filter.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from unittest import mock

from sd_db import repos

from sd_dashboard import issues, now_screen, tasks_screen, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document
from test_v2_today import run_js

V2 = Path(v2.__file__).resolve().parent
ISSUES_JS = (V2 / "static" / "issues.js").read_text(encoding="utf-8") if (V2 / "static" / "issues.js").exists() else ""

FOUND = {
    "/home/example/repos/acme/anvil": [
        {"tracker": "jira", "url": "https://example.test/browse/EXAMPLE-8", "number": None, "title": "Fix the <b>widget</b>",
         "state": "open", "reason": "filed", "last_seen": "2026-09-06T08:00:00+00:00"},
        {"tracker": "github", "url": "https://github.com/example/project/issues/12", "number": 12, "title": "Crash on start",
         "state": "open", "reason": "assigned", "last_seen": "2026-09-05T08:00:00+00:00"},
    ],
    "/home/example/repos/other/kiln": [
        {"tracker": "github", "url": "javascript:alert(1)", "number": 3, "title": "Odd link", "state": "open",
         "reason": "assigned", "last_seen": "2026-09-04T08:00:00+00:00"},
    ],
}


def stub(**kwargs):
    return mock.patch.object(repos, "mine_issues", create=True, **kwargs)


class TheDocument(ScreenCase):
    def test_each_issue_names_its_repo_tracker_reference_reason_and_a_web_link_only(self):
        with stub(return_value=FOUND) as read:
            document = issues.document(self.connection)
        read.assert_called_once_with(self.connection)
        self.assertEqual(document["issues_error"], "")
        self.assertEqual([(row["repo"], row["tracker"], row["ref"], row["reason"], row["url"]) for row in document["issues"]], [
            ("anvil", "Jira", "EXAMPLE-8", "filed", "https://example.test/browse/EXAMPLE-8"),
            ("anvil", "GitHub", "#12", "assigned", "https://github.com/example/project/issues/12"),
            ("kiln", "GitHub", "#3", "assigned", None),
        ])

    def test_a_pack_without_the_read_is_said_with_its_remedy(self):
        with mock.patch.object(repos, "mine_issues", None, create=True):
            document = issues.document(self.connection)
        self.assertEqual(document, {"issues": [], "issues_error":
                                    "this sd_db has no repos.mine_issues; update the command pack"})

    def test_a_database_without_the_reason_column_is_said_with_its_remedy(self):
        with stub(side_effect=sqlite3.OperationalError("no such column: reason")):
            document = issues.document(self.connection)
        self.assertEqual(document, {"issues": [], "issues_error": "no such column: reason; run sd-db migrate"})

    def test_another_failed_read_is_said_not_empty(self):
        with stub(side_effect=sqlite3.OperationalError("database is locked")):
            document = issues.document(self.connection)
        self.assertEqual(document, {"issues": [], "issues_error": "database is locked"})


class ThePages(ScreenCase):
    def test_today_and_tasks_carry_the_issues(self):
        with stub(return_value=FOUND):
            today = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name),
                                        fleet=lambda area: fleet_document(area))
            tasks = tasks_screen.document(self.connection, now=NOW)
        for document in (today, tasks):
            self.assertEqual(([row["ref"] for row in document["issues"]], document["issues_error"]), (["EXAMPLE-8", "#12", "#3"], ""))

    def test_a_failed_issue_read_leaves_the_rest_of_today_and_tasks(self):
        with stub(side_effect=sqlite3.OperationalError("database is locked")):
            today = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name),
                                        fleet=lambda area: fleet_document(area))
            tasks = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual((today["issues_error"], today["work_error"]), ("database is locked", ""))
        self.assertEqual((tasks["issues_error"], tasks["rows"]), ("database is locked", []))

    def test_both_pages_draw_the_section_and_load_its_script_before_the_shell(self):
        for name, page in (("today", "today.js"), ("tasks", "tasks.js")):
            html = (V2 / f"{name}.html").read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn('<tbody id="issues">', html)
                self.assertIn('id="issues-tally"', html)
                self.assertIn('<link rel="stylesheet" href="/ui/issues.css">', html)
                scripts = re.findall(r'<script src="/ui/([a-z-]+\.js)"></script>', html)
                self.assertLess(scripts.index("issues.js"), scripts.index(page))
                self.assertIn("issues:read", (V2 / "static" / page).read_text(encoding="utf-8"))


class TheTable(ScreenCase):
    """issues.js under JavaScriptCore: it draws what the page's read hands it, escaped, and says a failure."""

    PAGE = """
var els = {}, listeners = {};
document.getElementById = id => (els[id] = els[id] || { id, replaceChildren(f) { sunk.push([id, f.parsed]); } });
document.addEventListener = (t, f) => { listeners[t] = f; };
"""

    def drawn(self, detail):
        script = self.PAGE + ISSUES_JS + f"listeners['issues:read']({{ detail: {json.dumps(detail)} }}); JSON.stringify(sunk)"
        return dict(run_js(self, script))

    def test_rows_link_the_reference_escape_the_title_and_name_the_reason(self):
        with stub(return_value=FOUND):
            document = issues.document(self.connection)
        drawn = self.drawn({"issues": document["issues"], "error": ""})
        self.assertIn('<a href="https://example.test/browse/EXAMPLE-8">Jira EXAMPLE-8</a>', drawn["issues"])
        self.assertIn("Fix the &lt;b&gt;widget&lt;/b&gt;", drawn["issues"])
        self.assertIn(">assigned<", drawn["issues"])
        self.assertNotIn("javascript:", drawn["issues"])
        self.assertIn("GitHub #3", drawn["issues"])
        self.assertIn("3 open", drawn["issues-tally"])

    def test_a_failure_is_a_row_naming_it_and_none_is_said(self):
        failed = self.drawn({"issues": [], "error": "database is locked"})
        self.assertIn("Issues could not be read: database is locked. Refresh tries again.", failed["issues"])
        self.assertNotIn("open", failed["issues-tally"])
        empty = self.drawn({"issues": [], "error": ""})
        self.assertIn("No open issue is assigned to you or filed by you", empty["issues"])


INTAKE = {
    "/home/example/repos/acme/anvil": [
        {"tracker": "github", "repo": "example/project", "url": "https://github.com/example/project/issues/21",
         "number": 21, "title": "Docs <i>typo</i>", "state": "open", "reason": "watched", "author": "octocat",
         "last_seen": "2026-09-06T08:00:00+00:00"},
        {"tracker": "github", "repo": "example/project", "url": "javascript:alert(1)", "number": 22, "title": "Odd",
         "state": "open", "reason": None, "author": None, "last_seen": "2026-09-05T08:00:00+00:00"},
    ],
}


def stub_intake(**kwargs):
    return mock.patch.object(repos, "intake_issues", create=True, **kwargs)


class TheIntake(ScreenCase):
    """Issue intake on Today (sd:4840): each `github_issues` repo's open third-party issues that no sd item carries,
    from `sd_db.repos.intake_issues` (pack sd:4880). Stubbed here as Your issues is: the pack's suite tests the read."""

    def test_each_row_names_its_repo_reference_author_and_the_ref_an_item_files_it_under(self):
        with stub_intake(return_value=INTAKE) as read:
            document = issues.intake(self.connection)
        read.assert_called_once_with(self.connection)
        self.assertEqual(document["intake_error"], "")
        self.assertEqual([(row["repo"], row["repo_path"], row["ref"], row["key"], row["author"], row["url"])
                          for row in document["intake"]], [
            ("anvil", "/home/example/repos/acme/anvil", "#21", "github:example/project#21", "octocat",
             "https://github.com/example/project/issues/21"),
            ("anvil", "/home/example/repos/acme/anvil", "#22", "github:example/project#22", "", None),
        ])

    def test_a_pack_without_the_read_or_a_failed_read_is_said_not_empty(self):
        with mock.patch.object(repos, "intake_issues", None, create=True):
            self.assertEqual(issues.intake(self.connection), {"intake": [], "intake_error":
                             "this sd_db has no repos.intake_issues; update the command pack"})
        with stub_intake(side_effect=sqlite3.OperationalError("no such column: github_issues")):
            self.assertEqual(issues.intake(self.connection), {"intake": [], "intake_error":
                             "no such column: github_issues; run sd-db migrate"})
        with stub_intake(side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(issues.intake(self.connection), {"intake": [], "intake_error": "database is locked"})

    def test_today_carries_the_intake_and_a_failure_leaves_the_rest(self):
        with stub_intake(return_value=INTAKE):
            today = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name),
                                        fleet=lambda area: fleet_document(area))
        self.assertEqual([row["ref"] for row in today["intake"]], ["#21", "#22"])
        with stub_intake(side_effect=sqlite3.OperationalError("database is locked")):
            today = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name),
                                        fleet=lambda area: fleet_document(area))
        self.assertEqual((today["intake_error"], today["work_error"]), ("database is locked", ""))

    def test_today_draws_the_section_and_hands_the_rows_over(self):
        html = (V2 / "today.html").read_text(encoding="utf-8")
        self.assertIn('<tbody id="intake">', html)
        self.assertIn('id="intake-tally"', html)
        self.assertIn("intake:read", (V2 / "static" / "today.js").read_text(encoding="utf-8"))


class TheIntakeTable(ScreenCase):
    """issues.js draws the intake rows, each with File as item, which posts the item under the issue's ref."""

    PAGE = TheTable.PAGE + """
var posted = [];
window.shell = { post: (path, body) => { posted.push([path, body]); return new Promise(() => {}); } };
"""

    def drawn(self, detail, then=""):
        script = (self.PAGE + ISSUES_JS + f"listeners['intake:read']({{ detail: {json.dumps(detail)} }});" + then
                  + "JSON.stringify({ sunk, posted })")
        return run_js(self, script)

    def test_rows_escape_link_only_https_and_carry_a_file_control(self):
        with stub_intake(return_value=INTAKE):
            document = issues.intake(self.connection)
        out = self.drawn({"intake": document["intake"], "error": ""})
        drawn = dict(out["sunk"])
        self.assertIn('<a href="https://github.com/example/project/issues/21">GitHub #21</a>', drawn["intake"])
        self.assertIn("Docs &lt;i&gt;typo&lt;/i&gt;", drawn["intake"])
        self.assertNotIn("javascript:", drawn["intake"])
        self.assertIn(">octocat<", drawn["intake"])
        self.assertEqual(drawn["intake"].count(">File as item<"), 2)
        self.assertIn('data-key="github:example/project#21"', drawn["intake"])
        self.assertIn("2 waiting", drawn["intake-tally"])

    def test_file_posts_a_task_under_the_issue_s_ref_in_its_repo_and_marks_the_row_busy(self):
        with stub_intake(return_value=INTAKE):
            document = issues.intake(self.connection)
        out = self.drawn({"intake": document["intake"], "error": ""}, "window.issues.file('github:example/project#21');")
        self.assertEqual(out["posted"], [["/api/items", {"title": "Docs <i>typo</i>", "repo": "/home/example/repos/acme/anvil",
                                                         "ref": "github:example/project#21"}]])
        self.assertIn('aria-busy="true"', out["sunk"][-2][1])

    def test_a_failure_is_a_row_naming_it_and_none_is_said(self):
        failed = dict(self.drawn({"intake": [], "error": "database is locked"})["sunk"])
        self.assertIn("Issue intake could not be read: database is locked. Refresh tries again.", failed["intake"])
        empty = dict(self.drawn({"intake": [], "error": ""})["sunk"])
        self.assertIn("No third-party issue waits for an item", empty["intake"])
