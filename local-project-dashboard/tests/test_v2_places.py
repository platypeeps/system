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
from support import NOW, ScreenCase, before_5648
from test_now_screen import JobsBackend
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

STATIC = Path(v2.__file__).resolve().parent / "static"
PLACES_JS = (STATIC / "places.js").read_text(encoding="utf-8")
MARKUP_JS = (STATIC / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (STATIC / "commands.js").read_text(encoding="utf-8")
BRIEF_DETAILS_JS = (STATIC / "brief-details.js").read_text(encoding="utf-8")
REPORT_DETAILS_JS = (STATIC / "report-details.js").read_text(encoding="utf-8")

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
        # The operations backend a Reports view reads its jobs through (sd:5636); weekly-scan is in no place's family.
        self.jobs = JobsBackend(self.dir, jobs=[("hoa-mission-alarms", "failed", 1, None), ("fun-events-nightly", "idle", 0, None),
                                                ("weekly-scan", "idle", 0, None)])

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

    def test_a_scheduled_occurrence_is_no_place_task_until_it_opens(self):
        """sd:5670: as Tasks does, Home and HOA Tasks hide a scheduled occurrence (pack sd:5648) until the day it opens.
        A pack before sd:5648 marks nothing scheduled, so the row shows as before."""
        self.schedule(self.home_item, "2026-09-09")
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)), [])
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now="2026-09-09T12:00:00Z")), [self.home_item])
        with before_5648():
            self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)), [self.home_item])


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
        doc = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], self.ids(doc), doc["jobs"]),
                         ("read", [self.alarm], ["hoa-mission-alarms", "hoa-morning-digest"]))
        self.assertTrue(doc["rows"][0]["attention"])

    def test_home_reports_are_the_home_familys(self):
        doc = places.reports(self.connection, "home", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.fun]))

    def test_a_missing_family_list_is_unknown_naming_the_setting_never_an_empty_list(self):
        doc = places.reports(self.connection, "home", now=NOW, config=self.dir / "absent.conf", jobs=self.jobs)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("dashboard.report_families on Settings", doc["reason"])

    def test_a_file_without_the_family_is_unknown_naming_the_line_to_add(self):
        self.config.write_text(FAMILIES.splitlines()[0] + "\n", encoding="utf-8")
        doc = places.reports(self.connection, "home", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual(doc["state"], "unknown")
        self.assertIn("names no family home", doc["reason"])

    def test_a_reports_details_are_the_reports_pages_document_for_the_familys_jobs_alone(self):
        # sd:5636: a Reports row's Details on HOA and Home are the Reports page's own, so the view carries that page's
        # document (reports_screen.document) for the family's jobs: their reports with bodies, installed jobs and cadence.
        doc = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=self.jobs)
        details = doc["details"]
        self.assertEqual([r["id"] for r in details["reports"]], [self.alarm])
        self.assertEqual(details["reports"][0]["body"], "ran\n")
        self.assertEqual([j["name"] for j in details["jobs"]], ["hoa-mission-alarms"])
        self.assertEqual(list(details["cadence"]), ["hoa-mission-alarms"])
        self.assertEqual((details["sources"], details["attention"]["n"]), ({"reports": "", "jobs": "", "cadence": ""}, 1))
        self.assertNotIn("html", details, "a place reads no HTML folder count")

    def test_a_jobs_read_that_fails_leaves_the_reports_and_names_what_was_not_read(self):
        # sd:5636: without the job list there is no Run now and no cadence; the view says so, never "no job".
        jobs = JobsBackend(self.dir, refuse="launchctl refused")
        doc = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=jobs)
        self.assertEqual((doc["state"], self.ids(doc), doc["details"]["jobs"], doc["details"]["cadence"]), ("read", [self.alarm], None, None))
        self.assertEqual(doc["reason"], "Not read: jobs (launchctl refused), cadence (the job list was not read, so no job log was).")


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
        doc = places.reports(self.connection, "home", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], doc["jobs"], self.ids(doc)), ("read", ["weekly-scan"], [self.stray]))

    def test_a_families_row_that_does_not_read_leaves_reports_and_briefs_unknown(self):
        with self.refuse():
            reports = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=self.jobs)
            briefs = places.briefs(self.connection, "hoa", now=NOW, config=self.config)
        # The hoa checkout's own briefs could be listed, but which series are HOA's is unknown, not "none".
        self.assertEqual((reports["state"], reports["rows"], briefs["state"], briefs["rows"]), ("error", [], "error", []))
        self.assertIn("database is locked", briefs["reason"])


class RealmPlaces(Fixture):
    """sd:5663: a realm whose id is also a report family key is a place page, with HOA's and Home's three views."""

    def setUp(self):
        super().setUp()
        self.realms = self.dir / "realms.conf"
        self.realms.write_text("realm|acme|Acme|/repos/acme-*\nrealm|lab|Lab|/repos/lab\n", encoding="utf-8")
        self.enterContext(mock.patch.object(places, "REALMS_CONFIG", self.realms))
        self.config.write_text(FAMILIES + "family|acme|Acme|map|acme-nightly\n", encoding="utf-8")
        self.site = self.repo("/repos/acme-site")
        self.acme_item = self.item("Ship the press kit", kind="task", repo=self.site)
        self.item("A settled Acme item", kind="task", repo=self.site, status="done")
        self.acme_report = report(self, "acme-nightly", "r4")
        self.site_brief = brief(self, "2026-09-05 - Site Notes", self.site)
        self.nightly_brief = brief(self, "2026-09-05 - Acme Nightly", self.other)

    def refuse(self):
        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        return mock.patch.object(writes, "read_settings", refuse)

    def test_tasks_are_the_realms_open_items_the_rows_of_tasks_realm_view(self):
        doc = places.tasks(self.connection, "acme", now=NOW)
        self.assertEqual((doc["state"], doc["area"], self.ids(doc)), ("read", "acme", [self.acme_item]))
        rows = tasks_screen.document(self.connection, now=NOW)["rows"]
        self.assertEqual(sorted(r["id"] for r in rows if r["realm"] == "acme" and r["status"] != "done" and not r["parked"]),
                         self.ids(doc))

    def test_briefs_are_the_realms_repos_and_those_whose_series_names_a_family_job(self):
        doc = places.briefs(self.connection, "acme", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", sorted([self.site_brief, self.nightly_brief])))

    def test_reports_are_the_familys(self):
        doc = places.reports(self.connection, "acme", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], self.ids(doc), doc["jobs"]), ("read", [self.acme_report], ["acme-nightly"]))

    def test_a_realm_the_list_does_not_name_is_unknown_with_the_line_to_add(self):
        for read in (places.tasks, places.briefs):
            doc = read(self.connection, "beta", now=NOW)
            self.assertEqual((doc["state"], doc["rows"]), ("unknown", []), read.__name__)
            self.assertIn("names no realm beta; add realm|beta|<label>|<glob> to dashboard.realms on Settings", doc["reason"])

    def test_a_missing_family_line_is_unknown_naming_the_line_with_the_realms_label(self):
        self.config.write_text(FAMILIES, encoding="utf-8")
        doc = places.reports(self.connection, "acme", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("names no family acme; add family|acme|Acme|<icon>|<job>,<job>,... to dashboard.report_families",
                      doc["reason"])
        # The realm's own repos still give its briefs, and the reason says what else would be read.
        briefs = places.briefs(self.connection, "acme", now=NOW, config=self.config)
        self.assertEqual((briefs["state"], self.ids(briefs)), ("read", [self.site_brief]))
        self.assertIn("names no family acme", briefs["reason"])

    def test_a_realms_row_that_does_not_read_leaves_tasks_and_briefs_error_not_empty(self):
        with self.refuse():
            tasks = places.tasks(self.connection, "acme", now=NOW)
        self.assertEqual((tasks["state"], tasks["rows"]), ("error", []))
        self.assertIn("database is locked", tasks["reason"])
        with mock.patch.object(places, "realms", return_value={"state": "error", "source": "x", "problems": ["not read: locked"],
                                                               "list": []}):
            briefs = places.briefs(self.connection, "acme", now=NOW, config=self.config)
        self.assertEqual((briefs["state"], briefs["rows"]), ("error", []))
        self.assertIn("not read: locked", briefs["reason"])

    def test_a_realm_is_a_place_only_when_a_family_has_its_id(self):
        self.assertEqual([(r["id"], r["place"]) for r in places.realm_places(self.connection, self.config)],
                         [("acme", "map"), ("lab", None)])
        with self.refuse():
            self.assertEqual(places.realm_places(self.connection, self.config), [])
        broken = self.dir / "broken.conf"
        broken.write_bytes(b"\xff\xfe")
        self.assertEqual([r["place"] for r in places.realm_places(self.connection, broken)], [None, None])

    def test_hoa_and_home_keep_their_rows(self):
        self.assertEqual(self.ids(places.tasks(self.connection, "hoa", now=NOW)), [self.hoa_item])
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)), [self.home_item])
        self.assertEqual(self.ids(places.briefs(self.connection, "home", now=NOW, config=self.config)), [self.fun_brief])


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
        self.assertEqual(realms(signed), [{"id": "work", "label": "Work", "place": None}])
        self.assertEqual(realms(bare), [])
        self.assertNotIn("example-work", signed, "a glob is a path; the page needs only the id and label")

    def test_a_realm_with_a_family_of_its_id_carries_its_icon_as_place(self):
        """sd:5663: the rail lists a realm place only when its realm and family lines both read."""
        with tempfile.TemporaryDirectory() as folder:
            conf, families = Path(folder) / "realms.conf", Path(folder) / "report-families.conf"
            conf.write_text("realm|acme|Acme|/repos/acme-*\nrealm|lab|Lab|/repos/lab\n", encoding="utf-8")
            families.write_text("family|acme|Acme|map|acme-nightly\n", encoding="utf-8")
            with mock.patch.object(places, "REALMS_CONFIG", conf), mock.patch.object(reports_screen, "FAMILIES_CONFIG", families):
                body = self.request("/ui/sections.js", headers={"Cookie": self.cookie})[2]
        realms = json.loads(re.search(r"^window\.SHELL_REALMS = (.*);$", body, re.M).group(1))
        self.assertEqual(realms, [{"id": "acme", "label": "Acme", "place": "map"}, {"id": "lab", "label": "Lab", "place": None}])

    def test_a_realm_places_views_are_routes_that_need_a_session(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(places, "REALMS_CONFIG", Path(folder) / "realms.conf"), \
                mock.patch.object(reports_screen, "FAMILIES_CONFIG", Path(folder) / "report-families.conf"):
            for view in ("tasks", "briefs", "reports"):
                path = f"/api/place/acme/{view}"
                self.assertEqual(self.request(path)[0], 403, path)
                status, _, body = self.request(path, headers={"Cookie": self.cookie})
                doc = json.loads(body)
                # No realm list and no family list: unknown, with the line to add, never an empty list.
                self.assertEqual((status, doc["area"], doc["state"], doc["rows"]), (200, "acme", "unknown", []), path)
                self.assertIn("add ", doc["reason"], path)
                # A Tasks place view or a malformed id is not a realm.
                for other in ("hoa", "personal", "all"):
                    self.assertEqual(self.request(f"/api/place/{other}/{view}", headers={"Cookie": self.cookie})[0], 404)
                self.assertEqual(self.request(f"/api/place/Acme/{view}", headers={"Cookie": self.cookie})[0], 404)
        status, _, page = self.request("/places?realm=acme", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn('src="/ui/place.js', page)

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
""" + COMMANDS_JS + BRIEF_DETAILS_JS + REPORT_DETAILS_JS + PLACES_JS + "\nvar R = {};\n(async () => { try {\n" + setup + "\n(DOC_LISTENERS['shell:ready'] || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def docs(self, area="hoa"):
        more = {"tasks": {}, "briefs": {"config": self.config}, "reports": {"config": self.config, "jobs": self.jobs}}
        return {f"/api/{area}/{view}": [200, getattr(places, view)(self.connection, area, now=NOW, **more[view])]
                for view in ("tasks", "briefs", "reports")}

    def test_a_click_on_a_row_opens_what_its_link_opens(self):
        # sd:5481: a row opens its page with its Details; a click on the link itself or with a modifier is the browser's.
        out = self.run_page("""R.went = []; location.assign = h => R.went.push(h); globalThis.getSelection = () => ({ toString: () => '' });
const tr = { querySelector: () => ({ href: '/tasks?row=5' }) };
const click = (onLink, more = {}) => (DOC_LISTENERS['click'] || []).forEach(f => f(Object.assign({ button: 0, target: { closest: sel => sel === '.place-rows tbody tr' ? tr : onLink ? {} : null } }, more)));
click(false); click(true); click(false, { metaKey: true });""", self.docs())
        self.assertEqual(out["R"]["went"], ["/tasks?row=5"])

    #: A Briefs row as the click handler sees it: its id, and the link the row used to follow.
    BRIEF_CLICK = """const clickRow = id => { const tr = { dataset: { id }, querySelector: () => ({ href: '/briefs?row=' + encodeURIComponent(id) }) };
  (DOC_LISTENERS['click'] || []).forEach(f => f({ button: 0, target: { closest: sel => sel === '.place-rows tbody tr' ? tr : null } })); };
R.went = []; location.assign = h => R.went.push(h); globalThis.getSelection = () => ({ toString: () => '' });
R.panes = []; window.shell.openPane = p => R.panes.push(p);
"""

    def test_a_briefs_row_shows_its_details_in_the_pane_and_the_page_stays(self):
        # sd:5620: a Briefs row on HOA or Home fills Details with the Briefs page's own Details and commands, rather than
        # opening Briefs; a link in Details still opens it there.
        for place, stem in (("hoa", "2026-09-05 - Board Notes"), ("home", "2026-09-05 - Fun Events Nightly")):
            out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=briefs'); R.rows = ELS['briefs-rows'].html;
clickRow({json.dumps(stem)}); await flush();
R.det = document.getElementById('details').html || ''; R.reg = REG.map(c => c.id); R.obj = (C.get({json.dumps(stem)}) || {{}}).type;""", self.docs(place), place=place)
            r = out["R"]
            self.assertEqual(r["went"], [], place)
            self.assertEqual(r["panes"], ["tab-details"], place)
            self.assertIn(f'<tr data-id="{stem}" aria-selected="false">', r["rows"], place)
            self.assertIn("Brief · ", r["det"], place)
            self.assertIn(f"<h2>{stem}</h2>", r["det"], place)
            self.assertIn(f'href="/briefs?row={stem.replace(" ", "%20")}"', r["det"], place)
            self.assertEqual(r["obj"], "brief", place)
            for command in ("brief.read", "brief.task", "brief.open", "brief.adversarial-review", "brief.publish"):
                self.assertIn(command, r["reg"], place)

    def test_j_k_and_enter_walk_the_briefs_rows_through_the_shells_list(self):
        # sd:5620: the shell's grammar (shell.js walk): j / k select a row, Details follows without opening the pane, and
        # Enter (shell:open) opens it.
        board, digest = "2026-09-05 - Board Notes", "2026-09-05 - HOA Morning Digest"
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=briefs');
window.PAGE_LIST.select({json.dumps(digest)}); await flush();
R.cur = window.PAGE_LIST.current(); R.det = ELS.details.html || ''; R.quiet = R.panes.slice();
open({json.dumps(board)}); await flush(); R.cur2 = window.PAGE_LIST.current(); R.det2 = ELS.details.html || '';""", self.docs())
        r = out["R"]
        self.assertEqual((r["cur"], r["quiet"]), (digest, []))
        self.assertIn(f"<h2>{digest}</h2>", r["det"])
        self.assertEqual((r["cur2"], r["panes"]), (board, ["tab-details"]))
        self.assertIn(f"<h2>{board}</h2>", r["det2"])

    def test_a_reports_row_shows_the_reports_pages_details_in_the_pane_and_the_page_stays(self):
        # sd:5636: a Reports row on HOA or Home fills Details with the Reports page's own Details and commands, rather than
        # opening Reports; a link in Details still opens it there.
        for place, n, job in (("hoa", self.alarm, "hoa-mission-alarms"), ("home", self.fun, "fun-events-nightly")):
            out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); R.rows = ELS['reports-rows'].html;
clickRow('r{n}'); await flush();
R.det = ELS.details.html || ''; R.reg = REG.map(c => c.id); R.obj = (C.get('r{n}') || {{}}).type; R.job = (C.get('job:{job}') || {{}}).type;""", self.docs(place), place=place)
            r = out["R"]
            self.assertEqual(r["went"], [], place)
            self.assertEqual(r["panes"], ["tab-details"], place)
            self.assertIn(f'<tr data-id="r{n}" aria-selected="false">', r["rows"], place)
            self.assertIn(f"Database report #{n} · ", r["det"], place)
            for part in ("<h3>Report source</h3>", "<h3>Last 7 days</h3>", "Since the previous report", f"<h3>Job · {job}</h3>",
                         f'href="/reports?row=r{n}"'):
                self.assertIn(part, r["det"], place)
            self.assertEqual((r["obj"], r["job"]), ("report", "report-job"), place)
            for command in ("report.ack", "report.resolve", "report.show", "report.log", "report.task", "report.fix", "report-job.retry"):
                self.assertIn(command, r["reg"], place)

    def test_a_reports_run_now_is_its_own_beside_hoas_source_jobs(self):
        # sd:5636: HOA's source rows are 'job' objects with HOA's own Run now (hoa.js, jobs.retry); a report's job is another
        # type, so neither page's Run now runs on the other's object, and a report's posts its job's retry route.
        setup = "C.register({ id: 'jobs.retry', on: 'job', run: () => { throw new Error('HOA source handler'); } });"
        docs = self.docs()
        docs["/api/jobs/hoa-mission-alarms/retry"] = [200, {"answered": True}]
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); clickRow('r{self.alarm}'); await flush();
R.ids = REG.map(c => c.id); const job = C.get('job:hoa-mission-alarms');
R.on = REG.filter(c => c.on === job.type).map(c => c.id);
shellRun(cmd('report-job.retry'), job); await flush();""", docs, setup=setup)
        r = out["R"]
        self.assertEqual(len(r["ids"]), len(set(r["ids"])), "each command id is registered once")
        self.assertEqual(r["on"], ["report-job.retry"])
        self.assertEqual([p[0] for p in out["posts"]], ["/api/jobs/hoa-mission-alarms/retry"])

    def test_a_reread_that_fails_keeps_the_report_selected_and_says_it_is_not_known(self):
        # sd:5636: a failed read is unknown, not "gone": Details say why, no command runs (its pick is dropped, as read.js
        # drops one), and the next read selects it again.
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); clickRow('r{self.fun}'); await flush();
R.unpicked = []; C.pick = id => R.unpicked.push(id); document.dispatchEvent(new CustomEvent('shell:picked', {{ detail: ['r{self.fun}'] }}));
const ok = DOCS['/api/home/reports']; DOCS['/api/home/reports'] = [503, {{ error: 'store busy' }}];
await window.places.reread('reports'); await flush(); R.det = ELS.details.html || ''; R.cur = window.PAGE_LIST.current(); R.type = C.get('r{self.fun}').type;
DOCS['/api/home/reports'] = ok; await window.places.reread('reports'); await flush(); R.det2 = ELS.details.html || ''; R.type2 = C.get('r{self.fun}').type;""", self.docs("home"), place="home")
        r = out["R"]
        self.assertIn("The reports were not read again, so the selected row is not known: /api/home/reports did not answer", r["det"])
        self.assertEqual((r["cur"], r["type"], r["type2"], r["unpicked"]), (f"r{self.fun}", "not listed", "report", [f"r{self.fun}"]))
        self.assertIn(f"Database report #{self.fun} · ", r["det2"])

    def test_acknowledge_in_a_reports_details_posts_the_reports_route_and_reads_the_view_again(self):
        # sd:5636: the command is the Reports page's (report-details.js), with the report's revision; the view reads again,
        # and Details show the report as the new read lists it. (HOA's alarm report waits on its followup: Acknowledge is off.)
        docs = self.docs("home")
        revision = docs["/api/home/reports"][1]["details"]["reports"][0]["revision"]
        docs[f"/api/reports/{self.fun}/acknowledge"] = [200, {"revision": "after-ack"}]
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); clickRow('r{self.fun}'); await flush();
shellRun(cmd('report.ack'), C.get('r{self.fun}')); await flush(); R.det = ELS.details.html || '';""", docs, place="home")
        self.assertEqual(out["posts"], [[f"/api/reports/{self.fun}/acknowledge", {"revision": revision}, 64]])
        self.assertEqual(out["gets"], ["/api/home/reports", "/api/home/reports"])
        self.assertEqual(out["toasts"][-1]["msg"], f"Acknowledged · #{self.fun}")
        self.assertIn(f"Database report #{self.fun} · ", out["R"]["det"])

    def test_the_overview_reads_no_view_and_each_view_reads_its_own_document_when_shown(self):
        out = self.run_page("""R.first = shown(); R.gets0 = OUT.gets.slice();
await go('#view=tasks'); R.second = shown(); R.gets1 = OUT.gets.slice();
await go('#view=reports'); R.reports = ELS['reports-rows'].html; R.third = shown();""", self.docs())
        r = out["R"]
        self.assertEqual([r["gets0"], r["first"], r["second"], r["gets1"], r["third"]], [[], ["overview"], ["tasks"], [], ["reports"]])
        self.assertEqual(out["gets"], ["/api/hoa/reports"])
        self.assertIn(f'<tr data-id="r{self.alarm}" aria-selected="false">', r["reports"])

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
        self.assertIn(f'<tr data-id="r{self.alarm}" aria-selected="false">', r["reports"])

    def test_a_view_reads_again_each_time_it_shows(self):
        out = self.run_page("""await go('#view=briefs'); await go('#view=overview'); await go('#view=briefs');""", self.docs())
        self.assertEqual(out["gets"], ["/api/hoa/briefs", "/api/hoa/briefs"])

    def test_home_tasks_read_their_own_document_when_shown(self):
        out = self.run_page("""await go('#view=tasks'); R.tasks = ELS['tasks-rows'].html; R.tally = ELS['tasks-tally'].html;""",
                            self.docs("home"), place="home")
        self.assertEqual(out["gets"], ["/api/home/tasks"])
        self.assertIn(f'href="/tasks?row={self.home_item}">sd:{self.home_item}</a>', out["R"]["tasks"])
        self.assertNotIn("Fix the valve log", out["R"]["tasks"])
        self.assertEqual("", out["R"]["tally"], "sd:5524: the Tasks tab carries the count")

    def test_a_view_that_did_not_read_is_unknown_with_its_reason_never_an_empty_table(self):
        docs = self.docs()
        docs["/api/hoa/briefs"] = [200, places.briefs(self.connection, "home", now=NOW, config=self.dir / "absent.conf")]
        docs["/api/hoa/reports"] = [500, {"error": "the server failed"}]
        out = self.run_page("""await go('#view=briefs'); R.briefs = ELS['briefs-rows'].html;
await go('#view=reports'); R.reports = ELS['reports-rows'].html;""", docs)
        self.assertIn("▨ Not read.</b> not configured: no report families are set", out["R"]["briefs"].replace("&lt;", "<").replace("&gt;", ">"))
        self.assertNotIn("<table", out["R"]["briefs"])
        self.assertIn("▨ Not read.</b> /api/hoa/reports did not answer: the server failed", out["R"]["reports"])

    def test_read_again_reads_the_shown_view_again_and_a_view_without_a_read_reads_nothing(self):
        # sd:5638: the shell's Read again on HOA, Home and a realm place reads the shown place view's document again.
        out = self.run_page("""await go('#view=briefs'); let before = OUT.gets.length;
await window.places.again(); await flush(); R.briefs = OUT.gets.slice(before);
await go('#view=overview'); before = OUT.gets.length;
await window.places.again(); await flush(); R.overview = OUT.gets.slice(before);""", self.docs())
        self.assertEqual((out["R"]["briefs"], out["R"]["overview"]), (["/api/hoa/briefs"], []))


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

    def test_a_realm_named_after_a_layout_panel_is_kept_out_of_the_views(self):
        """sd:5114: a realm named list, board or matrix would make the shell hide that layout panel (view-<id>); one named
        btn or menu, the layout menu's view-btn and view-menu (sd:5641)."""
        self.doc, self.details = self.realm_doc(), {}
        prelude = ("globalThis.SHELL_REALMS = [{ id: 'list', label: 'List' }, { id: 'board', label: 'Board' },"
                   " { id: 'matrix', label: 'Matrix' }, { id: 'btn', label: 'Btn' }, { id: 'menu', label: 'Menu' }, { id: 'work', label: 'Work' }];\n")
        out = test_v2_tasks.TheScript.run_page(self, "R.views = window.PAGE_VIEWS.map(v => v.id);", prelude=prelude)
        self.assertEqual(out["R"]["views"], ["personal", "work", "hoa", "home", "all"])

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


PLACE_JS = (STATIC / "place.js").read_text(encoding="utf-8")
SHELL_PLACES = re.search(r"^  // places:start\n(.*?)^  // places:end$", (STATIC / "shell.js").read_text(encoding="utf-8"), re.S | re.M)


class TheRealmPlaceScripts(unittest.TestCase):
    """sd:5663: place.js names the page and its views after ?realm=, and shell.js puts each realm place in the rail."""

    def run_script(self, script):
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out["R"]

    def place(self, search, realms):
        return self.run_script(STAND_IN + MARKUP_JS + f"""
location.search = {json.dumps(search)}; window.SHELL_REALMS = {json.dumps(realms)};
document.documentElement = {{ hasAttribute: () => false }};
const BLOCKS = ['tasks', 'briefs', 'reports'].map(v => Object.assign(El('view-' + v), {{ dataset: {{ placeView: v, label: v }} }}));
document.querySelectorAll = sel => sel === '[data-place-view]' ? BLOCKS : [];
var R = {{}};
try {{
{PLACE_JS}
{PLACES_JS}
R.page = document.body.dataset.page || null; R.place = document.body.dataset.place || null; R.title = document.title || null;
R.h1 = ELS.title ? ELS.title.textContent : null; R.sub = ELS.subhead ? ELS.subhead.textContent : null;
R.again = !!window.PAGE_AGAIN; R.views = window.PAGE_VIEWS || null; R.state = window.PAGE_STATE ? [window.PAGE_STATE.title, String(window.PAGE_STATE.text.text || window.PAGE_STATE.text)] : null;
}} catch (e) {{ OUT.error = String(e) + ' ' + e.stack; }}
function run() {{ OUT.R = R; return JSON.stringify(OUT); }}
""")

    def test_a_realm_place_names_the_page_and_points_each_view_at_its_route(self):
        r = self.place("?realm=acme", [{"id": "acme", "label": "Acme", "place": "map"}])
        self.assertEqual((r["page"], r["place"], r["title"], r["h1"]), ("Acme", "acme", "Acme · system", "Acme"))
        self.assertEqual(r["views"], [{"id": v, "label": v, "api": f"/api/place/acme/{v}"} for v in ("tasks", "briefs", "reports")])
        self.assertIsNone(r["state"])
        self.assertTrue(r["again"], "a realm place declares no Read again (sd:5638)")

    def test_a_realm_without_a_family_or_not_listed_keeps_its_views_and_says_why(self):
        r = self.place("?realm=acme", [{"id": "acme", "label": "Acme", "place": None}])
        self.assertIn("dashboard.report_families has no family acme", r["sub"])
        self.assertEqual(len(r["views"]), 3)
        r = self.place("?realm=acme", [])
        self.assertEqual((r["page"], r["h1"]), ("acme", "acme"))
        self.assertIn("dashboard.realms names no realm acme", r["sub"])
        self.assertEqual(r["views"][0]["api"], "/api/place/acme/tasks")
        # A label the rail could not find by [data-section="..."] is left to the id.
        self.assertEqual(self.place("?realm=acme", [{"id": "acme", "label": 'A"cme', "place": "map"}])["page"], "acme")

    def test_without_a_realm_the_page_lists_the_realm_places_and_reads_no_view(self):
        r = self.place("", [{"id": "acme", "label": "Acme", "place": "map"}, {"id": "lab", "label": "Lab", "place": None}])
        self.assertEqual((r["place"], r["views"], r["state"][0], r["again"]), (None, None, "Pick a place", False))
        self.assertIn('href="/places?realm=acme">Acme</a>', r["state"][1])
        self.assertNotIn("Lab", r["state"][1])
        r = self.place("?realm=../x", [])
        self.assertEqual((r["place"], r["state"][0]), (None, "No realm place"))

    def test_the_rail_lists_each_realm_place_under_places_with_no_g_key(self):
        self.assertIsNotNone(SHELL_PLACES, "shell.js has no places:start block")
        realms = [{"id": "acme", "label": "Acme", "place": "map"}, {"id": "lab", "label": "Lab", "place": None},
                  {"id": "tasks", "label": "Tasks", "place": "map"}, {"id": "q", "label": 'Q"', "place": "map"},
                  {"id": "bad", "label": "Bad", "place": "x y"}]
        r = self.run_script(f"""var window = globalThis, OUT = {{ error: null }}, R = {{}};
window.SHELL_REALMS = {json.dumps(realms)};
const PAGES = {{ Tasks: '/tasks', HOA: '/hoa', Home: '/home', Places: '/places' }};
const GROUPS = [['Work', [['Tasks', 'list-todo', 'k']]], ['Places', [['HOA', 'droplets', 'h'], ['Home', 'house', 'o']]]];
try {{
{SHELL_PLACES.group(1)}
R.places = GROUPS[1][1]; R.pages = PAGES;
}} catch (e) {{ OUT.error = String(e) + ' ' + e.stack; }}
function run() {{ OUT.R = R; return JSON.stringify(OUT); }}
""")
        self.assertEqual(r["places"], [["HOA", "droplets", "h"], ["Home", "house", "o"], ["Acme", "map", ""]])
        self.assertEqual(r["pages"]["Acme"], "/places?realm=acme")
        self.assertEqual(r["pages"]["Tasks"], "/tasks")

    def test_place_js_sets_the_views_before_places_js_reads_them(self):
        text = (STATIC.parent / "place.html").read_text(encoding="utf-8")
        self.assertLess(text.index('src="/ui/sections.js"'), text.index('src="/ui/place.js"'))
        self.assertLess(text.index('src="/ui/place.js"'), text.index('src="/ui/places.js"'))

    def test_a_rail_entry_or_palette_row_without_a_key_draws_no_kbd(self):
        shell = (STATIC / "shell.js").read_text(encoding="utf-8")
        self.assertIn("${key ? html`<kbd>g ${key}</kbd>` : ''}", shell)
        self.assertIn("${k ? html`<kbd>g ${k}</kbd>` : ''}", shell)
        self.assertIn(".filter(([, , k]) => k).map(([k2, , k]) => [k, k2])", shell)


class ThePages(unittest.TestCase):
    def test_page_views_are_declared_before_shell_js_and_each_page_has_the_switch_host(self):
        for page, script in (("hoa", "places.js"), ("home", "places.js"), ("place", "place.js"), ("place", "places.js"),
                             ("tasks", "tasks.js")):
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
