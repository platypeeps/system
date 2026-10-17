"""The HOA and Home sub-views (Tasks, Briefs, Reports) and Tasks' HOA and Home views.

What this slice promises: one rule places a row in an area (`places.area_of`): an item of the hoa checkout is HOA, an
item with no repo is Home, and a report is in neither, since an area's Reports view holds it. The place pages' Tasks
views (HOA's from `hoa_screen`'s items) and the Tasks rows' `area` all come from that rule, so they hold the same rows.
Briefs are the area's when their repo is the area's checkout or their series names a job of the area's report family;
Reports are the reports of the family's jobs. The family list is `report-families.conf`; a missing family is unknown
with the file it was looked for in, never an empty list.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import reporting, routine_briefs

from sd_dashboard import hoa_screen, places, reports_screen, tasks_screen, v2

import test_v2_tasks
from support import NOW, ScreenCase
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

STATIC = Path(v2.__file__).resolve().parent / "static"
PLACES_JS = (STATIC / "places.js").read_text(encoding="utf-8")
MARKUP_JS = (STATIC / "markup.js").read_text(encoding="utf-8")

FAMILIES = ("family|hoa|HOA|house|hoa-mission-alarms,hoa-morning-digest\n"
            "family|home|Home|house|ha-backups-prune,fun-events-nightly\n")


def brief(case, stem, repo):
    return routine_briefs.add(case.connection, stem, "---\nseries: x\n---\n\nA lead.\n", repo=repo, who="test",
                              at="2026-09-05T12:00:00Z")["item"]


def report(case, job, run, attention=False):
    return reporting.ingest(case.connection, job=job, run_id=run, started="2026-09-05T08:00:00Z",
                            ended="2026-09-05T08:01:00Z", exit_code=1 if attention else 0, text="ran\n",
                            source_path="sd-db scan", attention=attention,
                            attention_basis="job exited 1" if attention else "")["item"]["id"]


class Fixture(ScreenCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config = self.dir / "report-families.conf"
        self.config.write_text(FAMILIES, encoding="utf-8")
        self.hoa = self.repo("/repos/hoa")
        self.other = self.repo("/repos/system")
        self.hoa_item = self.item("Fix the valve log", kind="task", repo=self.hoa)
        self.home_item = self.item("Clean the gutters", kind="task")
        self.other_item = self.item("Tune the gate", kind="task", repo=self.other)
        self.item("A settled HOA item", kind="task", repo=self.hoa, status="done")
        # A report no repo owns is not a Home issue: the family's Reports view holds it.
        self.alarm = report(self, "hoa-mission-alarms", "r1", attention=True)
        self.fun = report(self, "fun-events-nightly", "r2")
        self.stray = report(self, "weekly-scan", "r3")
        self.hoa_brief = brief(self, "2026-09-05 - Board Notes", self.hoa)
        self.digest = brief(self, "2026-09-05 - HOA Morning Digest", self.other)
        self.fun_brief = brief(self, "2026-09-05 - Fun Events Nightly", self.other)
        self.system_brief = brief(self, "2026-09-05 - Daily Intel Brief", self.other)

    def ids(self, doc):
        return sorted(row["item"] if "item" in row else row["id"] for row in doc["rows"])


class Tasks(Fixture):
    def test_hoa_tasks_are_the_open_items_of_the_hoa_checkout(self):
        doc = places.tasks(self.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.hoa_item]))

    def test_home_tasks_are_the_open_items_with_no_repo_and_no_report(self):
        doc = places.tasks(self.connection, "home", now=NOW)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.home_item]))

    def test_without_a_registered_hoa_checkout_hoa_tasks_are_unknown_with_the_reason(self):
        bare = ScreenCase()
        bare.setUp()
        self.addCleanup(bare.doCleanups)
        bare.repo("/repos/system")
        doc = places.tasks(bare.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("no repo named hoa", doc["reason"])

    def test_tasks_hoa_and_home_views_hold_the_place_pages_tasks_rows(self):
        rows = tasks_screen.document(self.connection, now=NOW)["rows"]
        for area in places.AREAS:
            tasks = sorted(r["id"] for r in rows if r["area"] == area and r["status"] != "done" and not r["parked"])
            self.assertEqual(tasks, self.ids(places.tasks(self.connection, area, now=NOW)), area)
        # The HOA page draws its Tasks view from /api/hoa's items: the same rows, in urgency order.
        hoa = sorted(r["item"] for r in hoa_screen.document(self.connection, now=NOW)["items"]["rows"])
        self.assertEqual(hoa, self.ids(places.tasks(self.connection, "hoa", now=NOW)))
        self.assertEqual({r["id"]: r["area"] for r in rows if r["id"] == self.other_item}, {self.other_item: None})


class Briefs(Fixture):
    def test_hoa_briefs_are_the_checkouts_and_those_whose_series_names_an_hoa_job(self):
        doc = places.briefs(self.connection, "hoa", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", sorted([self.hoa_brief, self.digest])))

    def test_home_briefs_are_those_whose_series_names_a_home_job(self):
        doc = places.briefs(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.fun_brief]))

    def test_without_a_home_family_home_briefs_are_unknown_naming_the_file(self):
        self.config.write_text(FAMILIES.splitlines()[0] + "\n", encoding="utf-8")
        doc = places.briefs(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("<config>/project-dashboard/report-families.conf", doc["reason"])
        self.assertIn("family|home|", doc["reason"])


class Reports(Fixture):
    def test_hoa_reports_are_the_hoa_familys(self):
        doc = places.reports(self.connection, "hoa", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc), doc["jobs"]),
                         ("read", [self.alarm], ["hoa-mission-alarms", "hoa-morning-digest"]))
        self.assertTrue(doc["rows"][0]["attention"])

    def test_home_reports_are_the_home_familys(self):
        doc = places.reports(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.fun]))

    def test_a_missing_family_file_is_unknown_with_its_path_never_an_empty_list(self):
        doc = places.reports(self.connection, "home", now=NOW, config=self.dir / "absent.conf")
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("<config>/project-dashboard/absent.conf", doc["reason"])

    def test_a_file_without_the_family_is_unknown_naming_the_line_to_add(self):
        self.config.write_text(FAMILIES.splitlines()[0] + "\n", encoding="utf-8")
        doc = places.reports(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual(doc["state"], "unknown")
        self.assertIn("names no family home", doc["reason"])


class TheRoutes(BrowserSession):
    def test_each_view_is_its_own_route_that_needs_a_session(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(reports_screen, "FAMILIES_CONFIG", Path(folder) / "report-families.conf"):
            # HOA's Tasks view is /api/hoa's items, so HOA has no tasks read of its own.
            self.assertEqual(self.request("/api/hoa/tasks", headers={"Cookie": self.cookie})[0], 404)
            for area in places.AREAS:
                for view in ("tasks", "briefs", "reports") if area == "home" else ("briefs", "reports"):
                    path = f"/api/{area}/{view}"
                    self.assertEqual(self.request(path)[0], 403, path)
                    status, _, body = self.request(path, headers={"Cookie": self.cookie})
                    self.assertEqual(status, 200, path)
                    doc = json.loads(body)
                    self.assertEqual(doc["area"], area, path)
                    # The fixture registers no hoa checkout and the folder holds no family file: unknown, with why.
                    if (area, view) != ("home", "tasks"):
                        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []), path)
                        self.assertTrue(doc["reason"], path)


class ThePlaceScript(Fixture):
    """places.js on the HOA and Home pages: the switch, and each view reading its own document only when shown."""

    #: The views each page's markup declares: [id, label, api]. HOA's Tasks is drawn by hoa.js, so it reads nothing here.
    VIEWS = {"hoa": [["overview", "Overview", None], ["tasks", "Tasks", None], ["briefs", "Briefs", "/api/hoa/briefs"], ["reports", "Reports", "/api/hoa/reports"]],
             "home": [["overview", "Overview", None], ["tasks", "Tasks", "/api/home/tasks"], ["briefs", "Briefs", "/api/home/briefs"], ["reports", "Reports", "/api/home/reports"]]}

    def run_page(self, body, docs, place="hoa"):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + f"\nconst DOCS = {json.dumps(docs)}, PLACE = {json.dumps(place)}, VIEWS = {json.dumps(self.VIEWS[place])};\n"
                  + """ANSWER = path => path in DOCS ? DOCS[path] : [404, { error: 'no answer' }];
location.hash = '';
document.body.dataset.place = PLACE;
document.documentElement = { hasAttribute: () => false };
const SECTIONS = VIEWS.map(([v, label, api]) => Object.assign(El('view-' + v), { dataset: Object.assign({ placeView: v, label }, api ? { api } : {}) }));
document.querySelectorAll = sel => sel === '[data-place-view]' ? SECTIONS : [];
const shown = () => SECTIONS.filter(x => !x.hidden).map(x => x.dataset.placeView);
const go = async hash => { location.hash = hash; (WIN_LISTENERS.hashchange || []).forEach(f => f()); await flush(); };
""" + PLACES_JS + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS['shell:ready'] || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def docs(self, area="hoa"):
        return {f"/api/{area}/{view}": [200, getattr(places, view)(self.connection, area, now=NOW, **({} if view == "tasks" else {"config": self.config}))]
                for view in ("tasks", "briefs", "reports")}

    def test_the_overview_reads_no_view_and_each_view_reads_its_own_document_when_shown(self):
        out = self.run_page("""R.sw = ELS['place-views'].html; R.first = shown(); R.gets0 = OUT.gets.slice();
await go('#view=tasks'); R.second = shown(); R.gets1 = OUT.gets.slice();
await go('#view=reports'); R.reports = ELS['reports-rows'].html; R.third = shown();""", self.docs())
        r = out["R"]
        self.assertEqual([r["gets0"], r["first"], r["second"], r["gets1"], r["third"]], [[], ["overview"], ["tasks"], [], ["reports"]])
        self.assertEqual(out["gets"], ["/api/hoa/reports"])
        self.assertEqual(re.findall(r'data-switch="(\w+)" aria-pressed="\w+">(\w+)<', r["sw"]),
                         [("overview", "Overview"), ("tasks", "Tasks"), ("briefs", "Briefs"), ("reports", "Reports")])
        self.assertIn(f'href="/reports?row=r{self.alarm}"', r["reports"])

    def test_home_tasks_read_their_own_document_when_shown(self):
        out = self.run_page("""await go('#view=tasks'); R.tasks = ELS['tasks-rows'].html; R.tally = ELS['tasks-tally'].html;""",
                            self.docs("home"), place="home")
        self.assertEqual(out["gets"], ["/api/home/tasks"])
        self.assertIn(f'href="/tasks?row={self.home_item}">sd:{self.home_item}</a>', out["R"]["tasks"])
        self.assertNotIn("Fix the valve log", out["R"]["tasks"])
        self.assertIn("1 open item", out["R"]["tally"])

    def test_a_view_that_did_not_read_is_unknown_with_its_reason_never_an_empty_table(self):
        docs = self.docs()
        docs["/api/hoa/briefs"] = [200, places.briefs(self.connection, "home", now=NOW, config=self.dir / "absent.conf")]
        docs["/api/hoa/reports"] = [500, {"error": "the server failed"}]
        out = self.run_page("""await go('#view=briefs'); R.briefs = ELS['briefs-rows'].html;
await go('#view=reports'); R.reports = ELS['reports-rows'].html;""", docs)
        self.assertIn("▨ Not read.</b> not configured: <config>/project-dashboard/absent.conf is missing", out["R"]["briefs"].replace("&lt;", "<").replace("&gt;", ">"))
        self.assertNotIn("<table", out["R"]["briefs"])
        self.assertIn("▨ Not read.</b> /api/hoa/reports did not answer: the server failed", out["R"]["reports"])


class TheTasksSwitch(Fixture):
    """Tasks' All | HOA | Home: tasks.js with places.js, against the rows `tasks_screen` builds."""

    def test_hoa_and_home_narrow_the_board_to_the_rows_area(self):
        self.doc, self.details = tasks_screen.document(self.connection, now=NOW), {}
        body = (f"(0, eval)({json.dumps(PLACES_JS)});\n"
                + """(DOC_LISTENERS['shell:ready'] || []).forEach(f => f()); await flush();
R.first = ELS['view-board'].html; R.sw = ELS['place-views'].html;
for (const v of ['hoa', 'home', 'all']) { location.hash = '#view=' + v; (WIN_LISTENERS.hashchange || []).forEach(f => f()); await flush(); R[v] = ELS['view-board'].html; }""")
        out = test_v2_tasks.TheScript.run_page(self, body)
        r = out["R"]
        self.assertEqual(re.findall(r'data-switch="(\w+)"', r["sw"]), ["all", "hoa", "home"])
        titles = ("Fix the valve log", "Clean the gutters", "Tune the gate")
        self.assertEqual([t in r["first"] for t in titles], [True, True, True])
        self.assertEqual([t in r["hoa"] for t in titles], [True, False, False])
        self.assertEqual([t in r["home"] for t in titles], [False, True, False])
        self.assertEqual(r["all"], r["first"])


class ThePages(unittest.TestCase):
    def test_the_overviews_read_state_shows_with_the_overview_only(self):
        # The page's #state names the Overview's read (/api/hoa, /api/home); on Briefs it would describe another read.
        for page in ("hoa", "home"):
            text = (STATIC.parent / f"{page}.html").read_text(encoding="utf-8")
            overview, state, other = (text.index(mark) for mark in ('data-place-view="overview"', 'id="state"', 'data-place-view="tasks"'))
            self.assertLess(overview, state, page)
            self.assertLess(state, other, page)


class TheRule(unittest.TestCase):
    def test_area_of(self):
        self.assertEqual(places.area_of("/repos/hoa", "task", "/repos/hoa"), "hoa")
        self.assertEqual(places.area_of(None, "task", "/repos/hoa"), "home")
        self.assertEqual(places.area_of("", "followup", None), "home")
        self.assertIsNone(places.area_of(None, "report", "/repos/hoa"))
        self.assertIsNone(places.area_of("/repos/system", "task", "/repos/hoa"))
        self.assertIsNone(places.area_of("/repos/hoa", "task", None))

    def test_a_series_names_a_job_lower_cased_with_hyphens(self):
        self.assertEqual(places.series_job("HOA Morning Digest"), "hoa-morning-digest")
        self.assertEqual(places.series_job("  Fun  Events Nightly "), "fun-events-nightly")


if __name__ == "__main__":
    unittest.main()
