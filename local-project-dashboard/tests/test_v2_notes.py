"""The v2 Notes page (sd:2120).

What this slice promises: `/notes` answers under the shared policy and loads
its script before `shell.js`; `/api/notes` carries the last seven days, each a
day in this machine's own zone, with its sd-ship merges, the items that
reached done, the count opened, and the runner runs. A source that raises is
named and adds no rows, so a failed read never looks like a quiet day. Mail,
the daily note text and quick-note storage have no reader: the document names
each with its reason, and the page draws them as unknown.

The zone is pinned per test (`TZ`), so a merge stamped 03:00 UTC falls on the
day before under a zone six hours behind UTC, whatever the test machine's zone.
`notes.js` runs under JavaScriptCore (osascript) against the stand-in page and
shell `test_v2_tasks` defines. The browser half -- the look at 375 px -- is a
manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_db import reads, transition
from sd_db.ship import note_merge
from sd_dashboard import notes_screen, server, v2

from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
NOTES_JS = (V2 / "static" / "notes.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: Six hours behind UTC, all year: NOW (12:00 UTC) is 06:00 on 2026-09-06 here.
BEHIND = "Etc/GMT+6"


class Zoned:
    """Pin the process zone for one test; Python's local-time reads follow `TZ` after `tzset`."""

    zone = BEHIND

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"TZ": self.zone})
        patch.start()
        self.addCleanup(time.tzset)
        self.addCleanup(patch.stop)
        time.tzset()


class Fixture(Zoned, ScreenCase):
    """Two merges either side of local midnight, an item opened and done on two days, and one blocked run."""

    def setUp(self):
        super().setUp()
        repo = self.repo()
        port = self.item("Port the page", kind="work", repo=repo)
        old = self.item("An old slice", kind="work", repo=repo)
        note_merge(self.connection, port, {"pull_request": {"url": "https://github.com/example-org/system/pull/41"},
                                           "merge_commit": "1acdcf6" + "0" * 33})
        note_merge(self.connection, old, {"pull_request": {"url": "https://github.com/example-org/system/pull/7"},
                                          "merge_commit": "abcdef1" + "0" * 33})
        merges = [row[0] for row in self.connection.execute("SELECT id FROM note WHERE body LIKE 'Code delivery%' ORDER BY id")]
        # Seeding, as support.ScreenCase.age does: the library writes the real clock, and the fixture's clock is NOW.
        self.stamp(merges[0], "2026-09-06T09:30:00Z")     # 03:30 local, 09-06
        self.stamp(merges[1], "2026-09-06T03:00:00Z")     # 21:00 local, 09-05: the UTC date would be 09-06
        self.task = self.item("Finish the page", kind="task", repo=repo)
        transition(self.connection, self.task, "done", who="test")
        opened, done = [row[0] for row in self.connection.execute(
            "SELECT id FROM note WHERE item = ? AND kind = 'status_change' ORDER BY id", (self.task,))]
        self.stamp(opened, "2026-09-04T15:00:00Z")
        self.stamp(done, "2026-09-05T15:00:00Z")
        early = self.item("Done long ago", kind="task")
        transition(self.connection, early, "done", who="test")
        self.connection.execute("UPDATE note SET timestamp = '2026-08-20T10:00:00Z' WHERE item = ?", (early,))
        self.blocked = self.assignment(port, status="blocked")
        self.connection.execute("UPDATE assignment SET started = ?, ended = ? WHERE id = ?",
                                ("2026-09-06T08:00:00Z", "2026-09-06T09:30:00Z", self.blocked))
        self.connection.commit()
        self.merges = merges

    def stamp(self, note, at):
        self.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", (at, note))

    def doc(self):
        return notes_screen.document(self.connection, now=NOW)


class TheDocument(Fixture):
    def test_the_days_are_the_last_seven_in_the_machine_s_zone(self):
        doc = self.doc()
        self.assertEqual(list(doc["days"]), [f"2026-08-{d}" for d in (31,)] + [f"2026-09-0{d}" for d in range(1, 7)])
        self.assertEqual(doc["today"], "2026-09-06")
        self.assertEqual(doc["tz"], "-06")

    def test_a_merge_falls_on_its_local_day(self):
        days = self.doc()["days"]
        self.assertEqual([m["ref"] for m in days["2026-09-06"]["merges"]], ["system#41"])
        self.assertEqual([m["ref"] for m in days["2026-09-05"]["merges"]], ["system#7"])

    def test_done_and_opened_come_from_the_day_s_status_changes(self):
        days = self.doc()["days"]
        self.assertEqual([(d["item"], d["from"], d["by"]) for d in days["2026-09-05"]["done"]], [(self.task, "planning", "test")])
        self.assertEqual((days["2026-09-04"]["opened"], days["2026-09-04"]["done"]), (1, []))
        self.assertEqual(sum(len(day["done"]) for day in days.values()), 1)

    def test_a_run_is_on_the_day_it_ended_with_its_state(self):
        runs = self.doc()["days"]["2026-09-06"]["runs"]
        self.assertEqual([(r["n"], r["s"], r["status"]) for r in runs], [(self.blocked, "caution", "blocked")])

    def test_a_source_that_raises_is_named_and_the_rest_still_answer(self):
        with mock.patch.object(reads, "status_change_notes", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertEqual(doc["sources"], {"changes": "OperationalError: database is locked"})
        self.assertEqual(len(doc["days"]["2026-09-06"]["merges"]), 1)

    def test_the_sources_no_reader_covers_are_named_with_their_reason(self):
        unknown = self.doc()["unknown"]
        self.assertEqual(sorted(unknown), ["daily", "mail", "quick"])
        self.assertTrue(all(unknown.values()))


class InUtc(Fixture):
    zone = "UTC"

    def test_the_same_merge_falls_on_the_utc_date_under_utc(self):
        days = self.doc()["days"]
        self.assertEqual([m["ref"] for m in days["2026-09-06"]["merges"]], ["system#41", "system#7"])


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/notes")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Notes · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "notes.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/notes")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/notes?day=2026-09-01", headers=cookie)[0], 400)
        status, _, body = self.request("/api/notes", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["days"]), notes_screen.DAYS)

    def test_the_rail_opens_notes_at_its_page(self):
        self.assertEqual(v2.SECTIONS["Notes"], "/notes")
        self.assertNotIn("Notes", v2.CLASSIC)


NOTES_SHELL = r"""
window.shell.row = () => null;
window.shell.pages = { Tasks: '/tasks' };
"""


class TheScript(Fixture):
    """notes.js against the document `notes_screen` builds from the fixture database."""

    def run_page(self, body, doc=None, status=200):
        doc = doc if doc is not None else self.doc()
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + NOTES_SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "ANSWER = (path, body) => path === '/api/notes' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + NOTES_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
                  + "(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_page_opens_on_today_with_its_tally_and_names_what_is_not_read(self):
        out = self.run_page("R.daily = ELS.daily.html; R.day = ELS['day-name'].textContent; R.next = ELS.next.disabled; R.prev = ELS.prev.disabled;")
        self.assertEqual(out["gets"], ["/api/notes"])
        r = out["R"]
        self.assertEqual((r["day"], r["next"], r["prev"]), ("Sunday, September 6", True, False))
        self.assertRegex(r["daily"], r'href="#merges"><b>1</b><span>merges in 1 repo</span>')
        self.assertRegex(r["daily"], r'href="#done"><b>0</b><span>tasks done · 0 opened</span>')
        self.assertRegex(r["daily"], r'href="#runs"><b>1</b><span>runs · 1 blocked</span>')
        self.assertRegex(r["daily"], r'href="#mail" class="unk"><b>▨</b><span>mail not read')
        self.assertIn("<b>▨ Daily note text unknown.</b> No collector reads the daily note", r["daily"])
        self.assertEqual(re.findall(r'<li class="row" data-id="([^"]+)"', r["daily"]), [f"merge:{self.merges[0]}", f"run:{self.blocked}"])
        self.assertIsNone(out["states"][-1])
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": ""})
        self.assertIn('<span class="t" title="2026-09-06 09:30:00 UTC">', r["daily"])

    def test_read_again_reads_the_week_again(self):
        out = self.run_page("ELS.reload.listeners.click[0](); await flush();")
        self.assertEqual(out["gets"], ["/api/notes", "/api/notes"])

    def test_the_previous_day_shows_its_own_rows_and_details_names_the_source(self):
        out = self.run_page(f"""ELS.prev.listeners.click[0](); R.day = ELS['day-name'].textContent; R.daily = ELS.daily.html;
open('merge:{self.merges[1]}'); R.det = ELS.details.html;""")
        r = out["R"]
        self.assertEqual(r["day"], "Saturday, September 5")
        self.assertRegex(r["daily"], r'href="#done"><b>1</b><span>tasks done · 0 opened</span>')
        self.assertIn("Finish the page", r["daily"])
        self.assertIn("system#7", r["daily"])
        self.assertIn("Source: sd-ship delivery note (ship.note_merge)", r["det"])
        self.assertIn('href="https://github.com/example-org/system/pull/7"', r["det"])

    def test_a_kept_quick_note_stays_on_the_page_and_says_it_is_not_saved(self):
        out = self.run_page("""R.before = ELS['qn-list'].html; ELS['qn-in'].value = 'Call the plumber'; ELS['qn-keep'].listeners.click[0]();
R.after = ELS['qn-list'].html; R.reg = REG.map(c => [c.id, c.on, c.risk, c.key]); R.posts = OUT.posts.length;""")
        r = out["R"]
        self.assertIn("No quick notes. sd store has no kind for them yet", r["before"])
        self.assertIn("Call the plumber<small>▲ Not saved: no loose-note kind in sd store", r["after"])
        self.assertEqual(out["toasts"][-1], ["Kept on this page only: sd store has no loose-note kind.", False])
        self.assertEqual(r["reg"], [["quicknote.discard", "quick note", "undo", "d"]])
        self.assertEqual(r["posts"], 0)

    def test_a_source_that_failed_is_a_partial_read_and_its_section_is_unknown(self):
        doc = self.doc()
        doc["sources"] = {"runs": "OperationalError: database is locked"}
        out = self.run_page("R.daily = ELS.daily.html;", doc)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("runs: OperationalError: database is locked", out["states"][-1]["text"])
        self.assertRegex(out["R"]["daily"], r'href="#runs" class="unk"><b>▨</b><span>runs not read')
        self.assertIn("<b>▨ Not read.</b> OperationalError: database is locked", out["R"]["daily"])

    def test_a_document_that_does_not_arrive_leaves_no_row_and_says_so(self):
        out = self.run_page("R.daily = ELS.daily.html; R.det = ELS.details.html;", {"error": "Open a dashboard page before reading Notes."}, 403)
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertNotIn("data-id=", out["R"]["daily"] or "")
        self.assertIn("the last week could not be read", out["R"]["det"])

    def test_the_page_keeps_the_shells_keys_and_reads_through_the_shared_reader(self):
        self.assertIn("S.read(spec)", NOTES_JS)
        self.assertNotIn("innerHTML", NOTES_JS)
        self.assertNotRegex(NOTES_JS, r"\.style\.")
        self.assertNotRegex(NOTES_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", NOTES_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "notes", "Notes", "/notes", ("/api/notes",)


if __name__ == "__main__":
    unittest.main()
