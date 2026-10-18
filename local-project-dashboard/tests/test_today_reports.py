"""Briefs and problem reports on Today (operator ruling 2026-10-17): what the reports view used to push by ntfy or mail.

The rows are the database's: a routine brief filed in the last week and not marked seen, and an open report that asks for a
person. Seen is a Today snooze; Acknowledge is the Reports page's route. A read that fails is its reason, never an empty list.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from unittest import mock

from sd_db import reporting, routine_briefs, writes

from sd_dashboard import now_screen, server, today_reports, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document
from test_v2_today import run_js

V2 = Path(v2.__file__).resolve().parent
JS = V2 / "static" / "today-reports.js"
REPO = "/repos/system"


class ReportsCase(ScreenCase):
    def setUp(self):
        super().setUp()
        self.repo(REPO)

    def brief(self, stem, text="Three things moved overnight.", at="2026-09-05T12:00:00Z"):
        return routine_briefs.add(self.connection, stem, f"---\nseries: x\n---\n\n{text}\n", repo=REPO, who="test", at=at)

    def report(self, job, run, *, exit_code=1, attention=True, basis="job exited 1 on host.example.test", text="the log tail"):
        return reporting.ingest(self.connection, job=job, run_id=run, started="2026-09-06T09:00:00Z",
                                ended="2026-09-06T09:01:00Z", exit_code=exit_code, text=text, source_path="/tmp/x.log",
                                attention=attention, attention_basis=basis)["item"]["id"]

    def read(self, snoozes=None):
        return today_reports.document(self.connection, now=NOW, snoozes=snoozes or {})

    def today(self):
        return now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name), fleet=fleet_document)


class TheProblems(ReportsCase):
    def test_an_open_attention_report_shows_with_its_reason_and_revision(self):
        item = self.report("nas-full-copy", "r1", basis="nas-full-copy: 3 skipped downloads\nsecond finding")
        doc = self.read()
        self.assertEqual((doc["problems_error"], doc["problems_more"]), ("", 0))
        [row] = doc["problems"]
        self.assertEqual((row["id"], row["item"], row["job"], row["why"], row["fields_read"]),
                         (f"report:{item}", item, "nas-full-copy", "nas-full-copy: 3 skipped downloads", True))
        self.assertEqual(row["revision"], reporting.workflow.item_state(self.connection, item)["revision"])

    def test_an_acknowledged_report_leaves_the_list(self):
        item = self.report("hoa-core-bills", "r1")
        revision = reporting.workflow.item_state(self.connection, item)["revision"]
        # The Today button posts the Reports page's route; this is the function that route runs.
        server._acknowledge_report(self.connection, item, expected_revision=revision)
        self.assertEqual(self.read()["problems"], [])

    def test_a_quiet_run_report_and_a_brief_are_not_problems(self):
        self.report("cleanup", "r1", exit_code=0, attention=False, basis="")
        self.brief("2026-09-05 - Daily Intel Brief")
        self.assertEqual(self.read()["problems"], [])

    def test_a_report_whose_fields_cannot_be_read_shows_and_says_so(self):
        item = self.report("odd", "r1")
        self.connection.execute("UPDATE item SET fields = 'not json' WHERE id = ?", (item,))
        [row] = self.read()["problems"]
        self.assertEqual((row["item"], row["fields_read"], row["why"]), (item, False, "its fields cannot be read"))

    def test_a_failed_read_is_its_reason_never_an_empty_list(self):
        with mock.patch.object(today_reports.reads, "revisions", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.read()
        self.assertEqual((doc["problems"], doc["problems_error"]), ([], "database is locked"))

    def test_a_full_read_marks_its_counts_as_at_least(self):
        for run in ("r1", "r2", "r3"):
            self.report(f"job-{run}", run)
        with mock.patch.object(today_reports, "SHOWN", 1):
            whole = self.read()
            with mock.patch.object(today_reports, "READ", 2):
                full = self.read()
        self.assertEqual((len(whole["problems"]), whole["problems_more"], whole["problems_capped"]), (1, 2, False))
        self.assertEqual((len(full["problems"]), full["problems_more"], full["problems_capped"]), (1, 1, True))

    def test_open_reports_count_in_todays_badge(self):
        self.report("nas-full-copy", "r1")
        today = self.today()
        self.assertEqual(len(today["problems"]), 1)
        self.assertIn("problem reports", today["attention"]["what"])
        self.assertGreaterEqual(today["attention"]["n"], 1)


class TheBriefs(ReportsCase):
    def test_a_brief_from_the_last_week_shows_newest_first_and_an_older_one_does_not(self):
        self.brief("2026-09-04 - Weekly Intel Synthesis", at="2026-09-04T11:00:00Z")
        self.brief("2026-09-05 - Daily Intel Brief")
        self.brief("2026-08-20 - Daily Intel Brief", at="2026-08-20T12:00:00Z")
        doc = self.read()
        self.assertEqual(doc["briefs_error"], "")
        self.assertEqual([row["stem"] for row in doc["briefs"]],
                         ["2026-09-05 - Daily Intel Brief", "2026-09-04 - Weekly Intel Synthesis"])
        first = doc["briefs"][0]
        self.assertEqual((first["id"], first["until"], first["lead"]),
                         ("brief:2026-09-05 - Daily Intel Brief", "2026-09-13T12:00:00Z", "Three things moved overnight."))
        self.assertTrue(first["seen"])

    def test_a_brief_marked_seen_leaves_today_until_its_text_grows(self):
        self.brief("2026-09-05 - Daily Intel Brief")
        row = self.read()["briefs"][0]
        # Seen is the snooze Today already writes (POST /api/snooze), held to the brief's `until`.
        writes.snooze(self.connection, f"today:{row['id']}", row["until"], seen=row["seen"], now=NOW)
        self.assertEqual([r["stem"] for r in self.today()["briefs"]], [])
        routine_briefs.add(self.connection, "2026-09-05 - Daily Intel Brief",
                           "---\nseries: x\n---\n\nThree things moved overnight.\n\nA fourth.\n", repo=REPO, who="test",
                           mode="extend")
        self.assertEqual([r["stem"] for r in self.today()["briefs"]], ["2026-09-05 - Daily Intel Brief"])

    def test_a_failed_brief_read_is_its_reason_never_an_empty_list(self):
        with mock.patch.object(today_reports.briefs_screen, "_library", side_effect=ValueError("the installed sd_db has no routine_briefs")):
            doc = self.read()
        self.assertEqual((doc["briefs"], doc["briefs_error"]), ([], "the installed sd_db has no routine_briefs"))
        self.assertEqual(doc["problems_error"], "")


class ThePage(ReportsCase):
    def test_today_carries_both_lists_and_draws_them_from_its_read(self):
        html = (V2 / "today.html").read_text(encoding="utf-8")
        for needle in ('<tbody id="problems">', '<tbody id="briefs">', 'id="problems-tally"', 'id="briefs-tally"'):
            self.assertIn(needle, html)
        scripts = re.findall(r'<script src="/ui/([a-z-]+\.js)"></script>', html)
        self.assertLess(scripts.index("today-reports.js"), scripts.index("today.js"))
        self.assertIn("today-reports:read", (V2 / "static" / "today.js").read_text(encoding="utf-8"))


class TheTables(ReportsCase):
    """today-reports.js under JavaScriptCore: it draws what Today's read hands it, escaped, and says each reason."""

    PAGE = """
var els = {}, listeners = {};
document.getElementById = id => (els[id] = els[id] || { id, replaceChildren(f) { sunk.push([id, f.parsed]); } });
document.addEventListener = (t, f) => { listeners[t] = f; };
"""

    def drawn(self, detail):
        script = (self.PAGE + JS.read_text(encoding="utf-8")
                  + f"listeners['today-reports:read']({{ detail: {json.dumps(detail)} }}); JSON.stringify(sunk)")
        return dict(run_js(self, script))

    def test_rows_escape_their_text_and_carry_their_controls(self):
        self.report("nas-full-copy", "r1", basis="copy <b>failed</b>")
        self.brief("2026-09-05 - Daily Intel Brief")
        drawn = self.drawn(self.read())
        self.assertIn("copy &lt;b&gt;failed&lt;/b&gt;", drawn["problems"])
        self.assertIn("Acknowledge", drawn["problems"])
        self.assertIn("1 open", drawn["problems-tally"])
        self.assertIn("2026-09-05 - Daily Intel Brief", drawn["briefs"])
        self.assertIn("Seen", drawn["briefs"])
        self.assertIn("1 new", drawn["briefs-tally"])

    def test_a_full_read_draws_its_counts_with_a_plus(self):
        for run in ("r1", "r2"):
            self.report(f"job-{run}", run)
        with mock.patch.object(today_reports, "SHOWN", 1), mock.patch.object(today_reports, "READ", 2):
            drawn = self.drawn(self.read())
        self.assertIn("2+ open", drawn["problems-tally"])
        self.assertIn("1+ more open on Reports.", drawn["problems"])

    def test_an_unknown_read_says_unknown_and_none_is_said(self):
        failed = self.drawn({"problems_error": "database is locked", "briefs_error": "no routine_briefs"})
        self.assertIn("Reports could not be read: database is locked", failed["problems"])
        self.assertIn("Briefs could not be read: no routine_briefs", failed["briefs"])
        whole = self.drawn({"error": "HTTP 500"})
        self.assertIn("Reports could not be read: HTTP 500", whole["problems"])
        self.assertIn("Briefs could not be read: HTTP 500", whole["briefs"])
        empty = self.drawn(self.read())
        self.assertIn("No open report asks for you.", empty["problems"])
        self.assertIn("No new brief this week.", empty["briefs"])
