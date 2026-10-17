"""The HOA and Home sub-views (Issues, Briefs, Reports).

What this slice promises: one rule places a row in an area (`places.area_of`): an item of the hoa checkout is HOA, an
item with no repo is Home, and a report is in neither, since an area's Reports view holds it. The place pages' Issues
views come from that rule.
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

from sd_dashboard import places, reports_screen, v2

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


class Issues(Fixture):
    def test_hoa_issues_are_the_open_items_of_the_hoa_checkout(self):
        doc = places.issues(self.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.hoa_item]))

    def test_home_issues_are_the_open_items_with_no_repo_and_no_report(self):
        doc = places.issues(self.connection, "home", now=NOW)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.home_item]))

    def test_without_a_registered_hoa_checkout_hoa_issues_are_unknown_with_the_reason(self):
        bare = ScreenCase()
        bare.setUp()
        self.addCleanup(bare.doCleanups)
        bare.repo("/repos/system")
        doc = places.issues(bare.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("no repo named hoa", doc["reason"])


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
            for area in places.AREAS:
                for view in ("issues", "briefs", "reports"):
                    path = f"/api/{area}/{view}"
                    self.assertEqual(self.request(path)[0], 403, path)
                    status, _, body = self.request(path, headers={"Cookie": self.cookie})
                    self.assertEqual(status, 200, path)
                    doc = json.loads(body)
                    self.assertEqual(doc["area"], area, path)
                    # The fixture registers no hoa checkout and the folder holds no family file: unknown, with why.
                    if (area, view) != ("home", "issues"):
                        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []), path)
                        self.assertTrue(doc["reason"], path)


class ThePlaceScript(Fixture):
    """places.js on the HOA page: the switch, and each view reading its own document only when shown."""

    def run_page(self, body, docs):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + f"\nconst DOCS = {json.dumps(docs)};\n"
                  + """ANSWER = path => path in DOCS ? DOCS[path] : [404, { error: 'no answer' }];
location.hash = '';
document.body.dataset.place = 'hoa';
document.documentElement = { hasAttribute: () => false };
const SECTIONS = ['overview', 'issues', 'briefs', 'reports'].map(v => Object.assign(El('view-' + v), { dataset: { placeView: v } }));
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

    def docs(self):
        return {f"/api/hoa/{view}": [200, getattr(places, view)(self.connection, "hoa", now=NOW, **({} if view == "issues" else {"config": self.config}))]
                for view in ("issues", "briefs", "reports")}

    def test_the_overview_reads_no_view_and_each_view_reads_its_own_document_when_shown(self):
        out = self.run_page("""R.sw = ELS['place-views'].html; R.first = shown(); R.gets0 = OUT.gets.slice();
await go('#view=issues'); R.issues = ELS['issues-rows'].html; R.tally = ELS['issues-tally'].html; R.second = shown();
await go('#view=reports'); R.reports = ELS['reports-rows'].html; R.third = shown();""", self.docs())
        r = out["R"]
        self.assertEqual([r["gets0"], r["first"], r["second"], r["third"]], [[], ["overview"], ["issues"], ["reports"]])
        self.assertEqual(out["gets"], ["/api/hoa/issues", "/api/hoa/reports"])
        self.assertEqual(re.findall(r'data-switch="(\w+)"', r["sw"]), ["overview", "issues", "briefs", "reports"])
        self.assertIn(f'href="/tasks?row={self.hoa_item}">#{self.hoa_item}</a>', r["issues"])
        self.assertNotIn("Clean the gutters", r["issues"])
        self.assertIn("1 open item", r["tally"])
        self.assertIn(f'href="/reports?row=r{self.alarm}"', r["reports"])

    def test_a_view_that_did_not_read_is_unknown_with_its_reason_never_an_empty_table(self):
        docs = self.docs()
        docs["/api/hoa/briefs"] = [200, places.briefs(self.connection, "home", now=NOW, config=self.dir / "absent.conf")]
        docs["/api/hoa/reports"] = [500, {"error": "the server failed"}]
        out = self.run_page("""await go('#view=briefs'); R.briefs = ELS['briefs-rows'].html;
await go('#view=reports'); R.reports = ELS['reports-rows'].html;""", docs)
        self.assertIn("▨ Not read.</b> not configured: <config>/project-dashboard/absent.conf is missing", out["R"]["briefs"].replace("&lt;", "<").replace("&gt;", ">"))
        self.assertNotIn("<table", out["R"]["briefs"])
        self.assertIn("▨ Not read.</b> /api/hoa/reports did not answer: the server failed", out["R"]["reports"])


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
