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

import sqlite3

from sd_db import reporting, routine_briefs, writes

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


class Realms(Fixture):
    """sd:5064: `places.realm_of` puts a Tasks row in Personal or a realm of the dashboard.realms list."""

    RULES = [{"id": "work", "label": "Work", "globs": ["/repos/example-work/*", "/repos/system"]}]

    def conf(self, text):
        path = self.dir / "realms.conf"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_repo_a_glob_matches_is_that_realm_and_any_other_repo_personal(self):
        self.assertEqual(places.realm_of("/repos/example-work/api", "task", self.RULES), "work")
        self.assertEqual(places.realm_of("/repos/system", "task", self.RULES), "work")
        self.assertEqual(places.realm_of("/repos/hoa", "task", self.RULES), "personal")
        self.assertEqual(places.realm_of("/repos/system-two", "task", self.RULES), "personal")

    def test_a_repo_stored_with_a_tilde_matches_a_tilde_glob(self):
        # The database keeps a repo as ~/repos/...; realms() expands a glob's ~, so the repo's ~ must expand too.
        rules = places.realms(self.connection, self.conf("realm|work|Work|~/repos/example-work/*\n"))["list"]
        self.assertEqual(places.realm_of("~/repos/example-work/api", "task", rules), "work")
        self.assertEqual(places.realm_of(str(Path.home() / "repos/example-work/api"), "task", rules), "work")

    def test_no_repo_is_personal_and_a_report_is_in_no_realm(self):
        self.assertEqual(places.realm_of(None, "task", self.RULES), "personal")
        self.assertEqual(places.realm_of(None, "task", None), "personal")
        self.assertIsNone(places.realm_of(None, "report", self.RULES))
        self.assertIsNone(places.realm_of("/repos/system", "report", self.RULES))

    def test_without_realms_conf_a_repo_row_is_unknown_not_personal(self):
        self.assertIsNone(places.realm_of("/repos/hoa", "task", None))
        doc = places.realms(self.connection, self.dir / "realms.conf")
        self.assertEqual((doc["state"], doc["list"], doc["source"]),
                         ("missing", [], "<config>/project-dashboard/realms.conf"))

    def test_the_file_reads_in_order_and_one_id_on_two_lines_gets_both_globs(self):
        doc = places.realms(self.connection, self.conf("# comment\nrealm|work|Work|/repos/example-work/*\nrealm|brand|Brand|~/brand\n"
                                      "realm|work|Work|/repos/system\n"))
        self.assertEqual(doc["state"], "read")
        self.assertEqual([(r["id"], r["label"]) for r in doc["list"]], [("work", "Work"), ("brand", "Brand")])
        self.assertEqual(doc["list"][0]["globs"], ["/repos/example-work/*", "/repos/system"])
        self.assertEqual(doc["list"][1]["globs"], [str(Path.home() / "brand")])

    def test_a_bad_line_or_a_reserved_id_is_skipped_and_named(self):
        doc = places.realms(self.connection, self.conf("realm|work|Work\nrealm|all|Everything|/repos/*\nrealm|Work|W|/x\n"
                                      "realm|lab|Lab|/repos/lab\n"))
        self.assertEqual(doc["state"], "partial")
        self.assertEqual([r["id"] for r in doc["list"]], ["lab"])
        self.assertEqual([p.split(":")[0] for p in doc["problems"]], ["line 1", "line 2", "line 3"])

    def test_an_unreadable_file_is_an_error_not_missing(self):
        path = self.conf("")
        path.write_bytes(b"\xff\xfe")
        self.assertEqual(places.realms(self.connection, path)["state"], "error")

    def test_each_tasks_row_carries_its_realm_and_the_document_the_files_state(self):
        with mock.patch.object(places, "REALMS_CONFIG", self.conf("realm|work|Work|/repos/sys*\n")):
            doc = tasks_screen.document(self.connection, now=NOW)
        realm = {r["id"]: r["realm"] for r in doc["rows"]}
        self.assertEqual((realm[self.hoa_item], realm[self.home_item], realm[self.other_item]),
                         ("personal", "personal", "work"))
        self.assertTrue(all(r["realm"] is None for r in doc["rows"] if r["kind"] == "report"))
        self.assertEqual(doc["realms"], {"state": "read", "source": "<config>/project-dashboard/realms.conf",
                                         "problems": []})
        with mock.patch.object(places, "REALMS_CONFIG", self.dir / "absent.conf"):
            doc = tasks_screen.document(self.connection, now=NOW)
        realm = {r["id"]: r["realm"] for r in doc["rows"]}
        self.assertEqual((realm[self.hoa_item], realm[self.home_item], realm[self.other_item]), (None, "personal", None))
        self.assertEqual(doc["realms"]["state"], "missing")


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

    def test_a_missing_family_list_is_unknown_naming_the_setting_never_an_empty_list(self):
        doc = places.reports(self.connection, "home", now=NOW, config=self.dir / "absent.conf")
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("dashboard.report_families on Settings", doc["reason"])

    def test_a_file_without_the_family_is_unknown_naming_the_line_to_add(self):
        self.config.write_text(FAMILIES.splitlines()[0] + "\n", encoding="utf-8")
        doc = places.reports(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual(doc["state"], "unknown")
        self.assertIn("names no family home", doc["reason"])


class TheRowWins(Fixture):
    """sd:5109: the realm and family lists are setting rows. A row wins over the old file, a cleared row hides it, and a
    row that does not read is unknown: no page reads the file in its place."""

    def setUp(self):
        super().setUp()
        realms = self.dir / "realms.conf"
        realms.write_text("realm|work|Work|/repos/sys*\n", encoding="utf-8")
        self.enterContext(mock.patch.object(places, "REALMS_CONFIG", realms))

    def refuse(self):
        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        return mock.patch.object(writes, "read_settings", refuse)

    def realm(self, doc):
        return {r["id"]: r["realm"] for r in doc["rows"]}[self.other_item]

    def test_no_row_reads_the_old_file(self):
        self.assertEqual(self.realm(tasks_screen.document(self.connection, now=NOW)), "work")

    def test_a_realms_row_wins_over_the_file(self):
        writes.set_setting(self.connection, "dashboard.realms", "realm|lab|Lab|/repos/system\n")
        doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual(self.realm(doc), "lab")
        self.assertEqual(doc["realms"]["source"], "workflow database setting dashboard.realms")

    def test_a_cleared_realms_row_hides_the_file(self):
        writes.set_setting(self.connection, "dashboard.realms", "realm|lab|Lab|/repos/system\n")
        writes.set_setting(self.connection, "dashboard.realms", None)
        doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual((doc["realms"]["state"], self.realm(doc)), ("missing", None))

    def test_a_realms_row_that_does_not_read_is_unknown_and_reads_no_file(self):
        with self.refuse():
            doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual((doc["realms"]["state"], self.realm(doc)), ("error", None))
        self.assertIn("database is locked", doc["realms"]["problems"][0])

    def test_a_families_row_wins_over_the_file(self):
        writes.set_setting(self.connection, "dashboard.report_families", "family|home|Home|house|weekly-scan\n")
        doc = places.reports(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual((doc["state"], doc["jobs"], self.ids(doc)), ("read", ["weekly-scan"], [self.stray]))

    def test_a_families_row_that_does_not_read_leaves_reports_and_briefs_unknown(self):
        with self.refuse():
            reports = places.reports(self.connection, "hoa", now=NOW, config=self.config)
            briefs = places.briefs(self.connection, "hoa", now=NOW, config=self.config)
        # The hoa checkout's own briefs could be listed, but which series are HOA's is unknown, not "none".
        self.assertEqual((reports["state"], reports["rows"], briefs["state"], briefs["rows"]), ("error", [], "error", []))
        self.assertIn("database is locked", briefs["reason"])


class TheRoutes(BrowserSession):
    def test_sections_js_names_the_realms_only_to_a_signed_in_page(self):
        """sd:5064: the realm labels are the operator's own, so a request without a session gets an empty list."""
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / "realms.conf"
            conf.write_text("realm|work|Work|/repos/example-work/*\n", encoding="utf-8")
            with mock.patch.object(places, "REALMS_CONFIG", conf):
                signed = self.request("/ui/sections.js", headers={"Cookie": self.cookie})[2]
                bare = self.request("/ui/sections.js")[2]
        realms = lambda body: json.loads(re.search(r"^window\.SHELL_REALMS = (.*);$", body, re.M).group(1))
        self.assertEqual(realms(signed), [{"id": "work", "label": "Work"}])
        self.assertEqual(realms(bare), [])
        self.assertNotIn("example-work", signed, "a glob is a path; the page needs only the id and label")

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

    def run_page(self, body, docs, place="hoa", setup=""):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + f"\nconst DOCS = {json.dumps(docs)}, PLACE = {json.dumps(place)}, VIEWS = {json.dumps(self.VIEWS[place])};\n"
                  + """ANSWER = path => path in DOCS ? DOCS[path] : [404, { error: 'no answer' }];
location.hash = '';
document.body.dataset.place = PLACE;
document.documentElement = { hasAttribute: () => false };
const SECTIONS = VIEWS.map(([v, label, api]) => Object.assign(El('view-' + v), { dataset: Object.assign({ placeView: v, label }, api ? { api } : {}) }));
document.querySelectorAll = sel => sel === '[data-place-view]' ? SECTIONS : [];
const shown = () => SECTIONS.filter(x => !x.hidden).map(x => x.dataset.placeView);
// The shell's view switch (shell.js view:start): the first view shows at load, and each change fires 'shell:view'.
let VIEW = VIEWS[0][0];
window.shell.view = { current: () => VIEW, show: () => true, count: () => {} };
const go = async hash => { VIEW = hash.replace('#view=', ''); document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: VIEW } })); await flush(); };
""" + PLACES_JS + "\nvar R = {};\n(async () => { try {\n" + setup + "\n(DOC_LISTENERS['shell:ready'] || []).forEach(f => f());\nawait flush();\n"
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
        out = self.run_page("""R.first = shown(); R.gets0 = OUT.gets.slice();
await go('#view=tasks'); R.second = shown(); R.gets1 = OUT.gets.slice();
await go('#view=reports'); R.reports = ELS['reports-rows'].html; R.third = shown();""", self.docs())
        r = out["R"]
        self.assertEqual([r["gets0"], r["first"], r["second"], r["gets1"], r["third"]], [[], ["overview"], ["tasks"], [], ["reports"]])
        self.assertEqual(out["gets"], ["/api/hoa/reports"])
        self.assertIn(f'href="/reports?row=r{self.alarm}"', r["reports"])

    def test_the_page_declares_its_views_for_the_shell_before_shell_js(self):
        # places.js loads before shell.js, which reads window.PAGE_VIEWS when it loads (shell.js view:start).
        for place in ("hoa", "home"):
            out = self.run_page("R.views = window.PAGE_VIEWS;", self.docs(place), place=place)
            self.assertEqual(out["R"]["views"], [{"id": v, "label": label, **({"api": api} if api else {})} for v, label, api in self.VIEWS[place]], place)

    def test_with_the_shells_view_switch_the_page_draws_none_and_reads_the_view_the_shell_shows(self):
        # The shell's 'shell:view' fired at DOMContentLoaded, before shell:ready: the view it shows then is read at shell:ready.
        setup = "window.shell.view = { current: () => 'briefs', show: () => true, count: () => {} };"
        out = self.run_page("""R.sw = (ELS['viewswitch'] || {}).html || ''; R.first = shown(); R.gets0 = OUT.gets.slice();
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'reports', first: true, api: '/api/hoa/reports' } })); await flush();
R.second = shown(); R.reports = ELS['reports-rows'].html;""", self.docs(), setup=setup)
        r = out["R"]
        self.assertFalse(r["sw"])
        self.assertEqual([r["first"], r["gets0"], r["second"], out["gets"]],
                         [["briefs"], ["/api/hoa/briefs"], ["reports"], ["/api/hoa/briefs", "/api/hoa/reports"]])
        self.assertIn(f'href="/reports?row=r{self.alarm}"', r["reports"])

    def test_a_view_reads_again_each_time_it_shows(self):
        out = self.run_page("""await go('#view=briefs'); await go('#view=overview'); await go('#view=briefs');""", self.docs())
        self.assertEqual(out["gets"], ["/api/hoa/briefs", "/api/hoa/briefs"])

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
        self.assertIn("▨ Not read.</b> not configured: no report families are set", out["R"]["briefs"].replace("&lt;", "<").replace("&gt;", ">"))
        self.assertNotIn("<table", out["R"]["briefs"])
        self.assertIn("▨ Not read.</b> /api/hoa/reports did not answer: the server failed", out["R"]["reports"])


class TheTasksSwitch(Fixture):
    """Tasks' Personal | <realms> | HOA | Home | All: tasks.js with places.js, against the rows `tasks_screen` builds."""

    #: The realms /ui/sections.js hands the page, and the realms.conf line that puts /repos/system in Work.
    REALMS = "globalThis.SHELL_REALMS = [{ id: 'work', label: 'Work' }];\n"

    def realm_doc(self):
        conf = self.dir / "realms.conf"
        conf.write_text("realm|work|Work|/repos/sys*\n", encoding="utf-8")
        with mock.patch.object(places, "REALMS_CONFIG", conf):
            return tasks_screen.document(self.connection, now=NOW)

    def test_each_view_narrows_the_board_to_its_realm_or_area(self):
        self.doc, self.details = self.realm_doc(), {}
        body = (f"window.shell.view = {{ current: () => 'personal', show: () => true, count: () => {{}} }};\n(0, eval)({json.dumps(PLACES_JS)});\n"
                + """(DOC_LISTENERS['shell:ready'] || []).forEach(f => f()); await flush();
R.first = ELS['view-board'].html; R.views = window.PAGE_VIEWS.map(v => v.id);
for (const v of ['work', 'hoa', 'home', 'all']) { document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: v } })); await flush(); R[v] = ELS['view-board'].html; }""")
        out = test_v2_tasks.TheScript.run_page(self, body, prelude=self.REALMS)
        r = out["R"]
        self.assertEqual(r["views"], ["personal", "work", "hoa", "home", "all"])
        titles = ("Fix the valve log", "Clean the gutters", "Tune the gate")
        # sd:5064: Personal, the first view, is every row no realm claims: the hoa checkout's and the one with no repo.
        self.assertEqual([t in r["first"] for t in titles], [True, True, False])
        self.assertEqual([t in r["work"] for t in titles], [False, False, True])
        self.assertEqual([t in r["hoa"] for t in titles], [True, False, False])
        self.assertEqual([t in r["home"] for t in titles], [False, True, False])
        self.assertEqual([t in r["all"] for t in titles], [True, True, True])

    def test_without_realms_conf_a_realm_view_holds_the_no_repo_rows_and_says_why(self):
        with mock.patch.object(places, "REALMS_CONFIG", self.dir / "realms.conf"):
            self.doc, self.details = tasks_screen.document(self.connection, now=NOW), {}
        body = (f"window.shell.view = {{ current: () => 'personal', show: () => true, count: () => {{}} }};\n(0, eval)({json.dumps(PLACES_JS)});\n"
                + """(DOC_LISTENERS['shell:ready'] || []).forEach(f => f()); await flush(); R.board = ELS['view-board'].html; R.sub = ELS.subhead.html;""")
        r = test_v2_tasks.TheScript.run_page(self, body, prelude=self.REALMS)["R"]
        self.assertEqual([t in r["board"] for t in ("Fix the valve log", "Clean the gutters", "Tune the gate")], [False, True, False])
        self.assertIn("repos in no realm: no realm list; set dashboard.realms on Settings", r["sub"])

    def test_the_personal_and_work_views_carry_their_sweep_and_run_sweep_posts_the_realm(self):
        """sd:5096, sd:5097: the subhead names the realm's last sweep on Personal and Work only; Run sweep posts the
        realm, and is off while that realm's run goes. A running sweep reads again after SWEEP_POLL, not at once."""
        self.doc, self.details = self.realm_doc(), {}
        never = {"running": False, "error": "", "command": "python3 -m sd_dashboard.realm_sweep run personal",
                 "last": {"state": "never"}}
        going = {"running": True, "error": "", "command": "python3 -m sd_dashboard.realm_sweep run work",
                 "last": {"state": "running", "started": "2026-09-06T11:30:00+00:00"}}
        self.doc["sweeps"] = {"personal": never, "work": going}
        prelude = self.REALMS + "var TIMERS = []; setTimeout = (f, ms) => { TIMERS.push(ms); return TIMERS.length; };\n"
        answer = "(path, body) => path === '/api/tasks/sweep' ? [202, { started: '2026-09-06T12:01:00+00:00', repo: '/x', command: 'c' }] : null"
        body = (f"window.shell.view = {{ current: () => 'personal', show: () => true, count: () => {{}} }};\n(0, eval)({json.dumps(PLACES_JS)});\n"
                + """(DOC_LISTENERS['shell:ready'] || []).forEach(f => f()); await flush(); R.personal = ELS.subhead.html;
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'work' } })); await flush(); R.work = ELS.subhead.html;
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'all' } })); await flush(); R.all = ELS.subhead.html;
R.objects = [...OBJ.keys()].filter(k => k.startsWith('sweep:')); R.timers = TIMERS.slice();
C.run(cmd('realm-sweep.run'), C.get('sweep:work')); await flush();
C.run(cmd('realm-sweep.run'), C.get('sweep:personal')); await flush();""")
        out = test_v2_tasks.TheScript.run_page(self, body, answer=answer, prelude=prelude)
        r = out["R"]
        self.assertIn('data-sweep="personal">Personal sweep not run yet', r["personal"])
        self.assertIn('data-sweep="work">Work sweep running since 11:30 UTC', r["work"])
        self.assertNotIn("data-sweep", r["all"])
        self.assertEqual(sorted(r["objects"]), ["sweep:personal", "sweep:work"])
        self.assertEqual(r["timers"], [15000])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/tasks/sweep", {"realm": "personal"}]])
        self.assertIn(["off: a sweep is running now", False], out["toasts"])
        self.assertIn(["Personal sweep started 12:01 UTC · Tasks reads again until it ends", False], out["toasts"])

    def test_a_done_sweep_names_the_sources_it_did_not_read(self):
        """sd:5096 review: a done run with a gap says so, rather than a plain done that hides an unread source."""
        self.doc, self.details = self.realm_doc(), {}
        done = {"running": False, "error": "", "command": "c",
                "last": {"state": "done", "finished": "2026-09-06T11:40:00+00:00", "gaps": ["jira", "github"]}}
        self.doc["sweeps"] = {"personal": done, "work": {**done, "last": {**done["last"], "gaps": []}}}
        body = (f"window.shell.view = {{ current: () => 'personal', show: () => true, count: () => {{}} }};\n(0, eval)({json.dumps(PLACES_JS)});\n"
                + """(DOC_LISTENERS['shell:ready'] || []).forEach(f => f()); await flush(); R.personal = ELS.subhead.html;
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'work' } })); await flush(); R.work = ELS.subhead.html;""")
        r = test_v2_tasks.TheScript.run_page(self, body, prelude=self.REALMS)["R"]
        self.assertIn("Personal sweep done 2026-09-06 11:40 UTC · not read: jira, github", r["personal"])
        self.assertIn("Work sweep done 2026-09-06 11:40 UTC ", r["work"])
        self.assertNotIn("not read", r["work"])

    def test_the_view_keys_skip_the_list_board_and_matrix_keys(self):
        body = "R.views = window.PAGE_VIEWS.map(v => [v.id, v.key]);"
        prelude = "globalThis.SHELL_REALMS = [{ id: 'work', label: 'Work' }, { id: 'brand', label: 'Brand' }, { id: 'lab', label: 'Lab' }, { id: 'all', label: 'Sneaky' }];\n"
        self.doc, self.details = tasks_screen.document(self.connection, now=NOW), {}
        r = test_v2_tasks.TheScript.run_page(self, body, prelude=prelude)["R"]
        # brand cannot take b (the board), so it takes r; every letter of lab is taken, so lab has no key.
        # A realm named all, a reserved view, is dropped.
        self.assertEqual(r["views"], [["personal", "p"], ["work", "w"], ["brand", "r"], ["lab", None],
                                      ["hoa", "h"], ["home", "o"], ["all", "a"]])


    def test_with_the_shells_view_switch_tasks_follows_shell_view(self):
        self.doc, self.details = tasks_screen.document(self.connection, now=NOW), {}
        body = (f"window.shell.view = {{ current: () => 'hoa', show: () => true, count: () => {{}} }};\n(0, eval)({json.dumps(PLACES_JS)});\n"
                + """(DOC_LISTENERS['shell:ready'] || []).forEach(f => f()); await flush();
R.views = window.PAGE_VIEWS; R.sw = (ELS['viewswitch'] || {}).html || ''; R.hoa = ELS['view-board'].html;
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'home', first: true, api: null } })); await flush(); R.home = ELS['view-board'].html;""")
        r = test_v2_tasks.TheScript.run_page(self, body)["R"]
        self.assertEqual(r["views"], [{"id": "personal", "label": "Personal", "key": "p"}, {"id": "hoa", "label": "HOA", "key": "h"},
                                      {"id": "home", "label": "Home", "key": "o"}, {"id": "all", "label": "All", "key": "a"}])
        self.assertFalse(r["sw"])
        self.assertEqual([("Fix the valve log" in r["hoa"], "Clean the gutters" in r["hoa"]), ("Fix the valve log" in r["home"], "Clean the gutters" in r["home"])],
                         [(True, False), (False, True)])


class ThePages(unittest.TestCase):
    def test_page_views_are_declared_before_shell_js_and_each_page_has_the_switch_host(self):
        for page, script in (("hoa", "places.js"), ("home", "places.js"), ("tasks", "tasks.js")):
            text = (STATIC.parent / f"{page}.html").read_text(encoding="utf-8")
            self.assertLess(text.index(f'src="/ui/{script}"'), text.index('src="/ui/shell.js"'), page)
            self.assertEqual(text.count('id="viewswitch"'), 1, page)


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
