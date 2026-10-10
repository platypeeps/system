"""The v2 Health page (sd:2115).

What this slice promises: `/fleet-health` answers under the shared policy and
loads its script before `shell.js` (`/health` stays the service's own check);
`/api/health` is every area of the design, in its order, each saying whether a
reader covers it. Worktrees come from the fleet child's registrations, grouped
per checkout. An area with no
reader, or whose reader failed, is unknown on the page and names what it does
not read, never a clean lamp. Ports is Operations > Ports' reader with its
counts and warnings; Protection is `protection.rows` as a matrix, one column
per repository, with a table carrying the same cells, and an unread
repository shows no cell. Disk and Branches (sd:2202, sd:2204) read this
machine through `health_collectors`, each inside its budget: the collector
tests below run real git against fixture repositories, with `df` and `du`
stubbed where the answer depends on the machine; every other test fills both
seams with the fixtures below, so no test reads the operator's disks.

`health.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` defines, with the document above as its fetch
answer. The browser half -- focus, the look at 375 px -- is a manual check
recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import credentials, upsert_repo, writes
from sd_db.writes import record_state, snooze

from sd_dashboard import health_collectors, health_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar

V2 = Path(v2.__file__).resolve().parent
HEALTH_JS = (V2 / "static" / "health.js").read_text(encoding="utf-8")
SNOOZE_JS = (V2 / "static" / "snooze.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

TREES = [
    {"repo": "group/alpha", "name": "a", "path": "/work/wt-a", "branch": "feat/a", "live": False, "state": "abandoned"},
    {"repo": "group/alpha", "name": "b", "path": "/work/wt-b", "branch": "fix/b", "live": False, "state": "abandoned"},
    {"repo": "beta", "name": "c", "path": "", "branch": "?", "live": False, "state": "unknown"},
    {"repo": "beta", "name": "d", "path": "/work/wt-d", "branch": "main", "live": True, "state": "live"},
]


def fleet_of(trees, root="/checkouts", exists=True):
    def read(area):
        assert area == "sessions", area
        return {"root": root, "rootExists": exists, "worktrees": trees, "processes": [], "processes_error": "",
                "processes_truncated": False, "abandoned": 0, "counts": {}}
    return read


def ports_snapshot():
    """One configured port listening, one uninspected, and one listener no service claims."""
    return {"complete": True,
            "services": [{"name": "svc-a", "ports": ["9010"], "state": "running", "mark": "+"},
                         {"name": "svc-b", "ports": ["9020"], "state": "stopped", "mark": "."}],
            "listeners": {"9010": {"state": "listening", "source": "lsof", "holder": "svc", "pid": 42},
                          "9020": {"state": "unknown"}},
            "observed": {"complete": False, "ports": {"5000": [{"pid": 7, "command": "other", "address": "*:5000"}]}}}


def flag(check, value, flagged):
    return {"id": check, "value": value, "flagged": flagged, "gap": f"{check} sentence"}


PROTECTION = [
    {"repo": "/checkouts/alpha", "slug": "group/alpha", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None, "gaps": [{"id": "reviews", "gap": "no approving review is required"}],
     "merge_settings": [flag("squash_message", "PR_TITLE / PR_BODY", False), flag("required_check", "sd/local-gate", True)]},
    {"repo": "/checkouts/beta", "slug": None, "status": "unprotected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None,
     "gaps": [{"id": "unprotected", "gap": "main has no protection"}, {"id": "required_checks", "gap": "no check is required"}],
     "merge_settings": []},
    {"repo": "/checkouts/gamma", "slug": "group/gamma", "status": "unknown", "observed_at": "2026-09-04T08:00:00Z",
     "default_branch": None, "reason": "token does not reach it", "gaps": [], "merge_settings": [flag("rebase_merge", "allowed", True)]},
    {"repo": "/checkouts/delta", "slug": "group/delta", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None, "gaps": [], "merge_settings": []},
]


# `protection.rows` since sd:2205 carries `managed` and `alerts`; none of these is managed.
PROTECTION = [{**repo, "managed": False, "alerts": None} for repo in PROTECTION]


def protection_of(rows):
    return lambda connection: rows


#: Managed repositories with alerts as the nightly sync stores them (sd:2205, sd:2206); beta is not managed, and
#: alpha-copy is a second checkout of group/alpha.
ALERTED = [
    {**PROTECTION[0], "managed": True, "alerts": {
        "dependabot": {"open": 100, "more": True, "severity": {"critical": 1, "low": 99}},
        "secret_scanning": {"visibility": "public", "setting": "enabled", "open": 1, "more": False}}},
    {**PROTECTION[0], "repo": "/checkouts/alpha-copy", "managed": True, "alerts": PROTECTION[0].get("alerts")},
    {**PROTECTION[1], "managed": False, "alerts": None},
    {**PROTECTION[2], "managed": True, "alerts": None},
    {**PROTECTION[3], "managed": True, "alerts": {
        "dependabot": {"open": 2, "more": False, "severity": {"medium": 2}},
        "secret_scanning": {"visibility": "public", "setting": "disabled"}}},
    {"repo": "/checkouts/epsilon", "slug": "group/epsilon", "status": "protected", "observed_at": "2026-09-04T08:00:00Z",
     "reason": None, "managed": True, "alerts": {
         "dependabot": {"reason": "API HTTP 403; retry on a later collection"},
         "secret_scanning": {"visibility": "public", "setting": None, "reason": "security_and_analysis not shown"}}},
    {"repo": "/checkouts/zeta", "slug": "group/zeta", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "reason": None, "managed": True, "alerts": {"dependabot": {"archived": True}, "secret_scanning": {"visibility": "private"}}},
]


GIB = 1024 ** 2
DISK = {
    "volumes": [
        {"filesystem": "/dev/disk3s5", "size_kb": 1000 * GIB, "used_kb": 850 * GIB, "avail_kb": 150 * GIB, "capacity": 85,
         "mount": "/System/Volumes/Data"},
        {"filesystem": "/dev/disk5s1", "size_kb": 2000 * GIB, "used_kb": 800 * GIB, "avail_kb": 1200 * GIB, "capacity": 40,
         "mount": "/Volumes/store"},
    ],
    "build": {"checked": 4, "unread": [],
              "merged": [{"path": "/work/wt-old", "head": "0" * 40, "branch": "feat/done", "repo": "/checkouts/alpha",
                          "dirs": ["target", "node_modules"]}]},
    "storage": [{"root": "/Volumes/store/repo-storage", "error": "",
                 "folders": [{"path": "/Volumes/store/repo-storage/alpha", "kb": 5 * GIB},
                             {"path": "/Volumes/store/repo-storage/beta", "kb": 2 * GIB},
                             {"path": "/Volumes/store/repo-storage/gamma", "kb": GIB},
                             {"path": "/Volumes/store/repo-storage/delta", "kb": 10 * 1024}]}],
    "refused": [], "config": "/config/project-dashboard/disk.conf",
}
BRANCHES = {
    "repos": 2, "no_default": ["/checkouts/beta"], "unread": [],
    "merged": [{"repo": "/checkouts/alpha", "path": "/checkouts/alpha",
                "deletable": [["feat/one", "2026-08-01"], ["feat/two", "2026-09-01"], ["fix/three", "2026-07-15"]],
                "checked_out": [["feat/live", "/work/wt-live"]]}],
}
CLEAN_DISK = {**DISK, "volumes": [DISK["volumes"][1]], "build": {"checked": 4, "merged": [], "unread": []}}
CLEAN_BRANCHES = {"repos": 2, "merged": [], "no_default": [], "unread": []}
REAL_DISK_SCAN, REAL_BRANCH_SCAN = health_collectors.disk_scan, health_collectors.branch_scan


def scan_of(scan):
    return lambda connection, **_: scan


class HeldClock:
    """`time` whose `monotonic` stays at its first reading: a walk that spends no budget (sd:2667)."""

    def __init__(self):
        self.held = None

    def monotonic(self):
        if self.held is None:
            self.held = time.monotonic()
        return self.held

    def __getattr__(self, name):
        return getattr(time, name)


class Collectors:
    """Disk and Branches read the fixtures above unless a test names its own reader: no test reads this machine."""

    def setUp(self):
        super().setUp()
        # A scan left running by an earlier test, or its kept answer, is not this test's.
        health_screen._SCANS.clear()
        self.addCleanup(health_screen._SCANS.clear)
        for name, scan in (("disk_scan", DISK), ("branch_scan", BRANCHES)):
            patcher = patch.object(health_collectors, name, scan_of(scan))
            patcher.start()
            self.addCleanup(patcher.stop)


def git(*args, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-05T10:00:00Z", "GIT_COMMITTER_DATE": "2026-09-05T10:00:00Z",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", *args], cwd=cwd, check=True, env=env, capture_output=True, text=True).stdout.strip()


def cloned(path: Path) -> Path:
    """A repository on main with origin/HEAD at its tip, the way a clone knows its default branch."""
    git("init", "-q", "-b", "main", str(path))
    git("-C", str(path), "commit", "-q", "--allow-empty", "-m", "first")
    git("-C", str(path), "update-ref", "refs/remotes/origin/main", "HEAD")
    git("-C", str(path), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return path


class TheDocument(Collectors, ScreenCase):
    """`health_screen.document` from the fleet child."""

    def doc(self, trees=TREES, **kwargs):
        return health_screen.document(self.connection, now=NOW, fleet=kwargs.get("fleet") or fleet_of(trees),
                                      ports=kwargs.get("ports") or ports_snapshot,
                                      protection=kwargs.get("protection") or protection_of(PROTECTION),
                                      disk=kwargs.get("disk"), branches=kwargs.get("branches"))

    def test_every_design_area_is_there_in_order_and_says_whether_a_reader_covers_it(self):
        areas = self.doc()["areas"]
        self.assertEqual([(a["id"], a["read"]) for a in areas],
                         [("disk", True), ("cred", True), ("wt", True),
                          ("br", True), ("dep", True), ("sec", True), ("ports", True), ("prot", True)])
        for area in areas:
            if area["id"] not in ("br", "ports", "prot", "cred", "dep", "sec"):
                self.assertTrue(area["missing"], f"{area['id']} names nothing it does not read")
            if not area["read"]:
                self.assertEqual((area["rows"], area["error"], area["source"]), ([], "", None))

    def seen(self, row_id, doc=None):
        """The row's fingerprint as the page reads it, which the page posts with its snooze."""
        return next(row["seen"] for area in (doc or self.doc())["areas"] for row in area["rows"] if row["id"] == row_id)

    def test_a_snoozed_row_leaves_its_area_until_its_time_and_then_comes_back(self):
        """sd:1896. The key is the page and the row id; Today's key for the same id hides nothing here."""
        snooze(self.connection, "health:gone:group/alpha", "2026-09-06T15:00:00Z", seen=self.seen("gone:group/alpha"), now=NOW)
        snooze(self.connection, "today:unread:beta", "2026-09-06T15:00:00Z", seen=self.seen("unread:beta"), now=NOW)
        doc = self.doc()
        wt = doc["areas"][2]
        self.assertEqual([row["id"] for row in wt["rows"]], ["unread:beta"])
        self.assertEqual([(row["id"], row["until"], row["state"]) for row in wt["snoozed"]],
                         [("gone:group/alpha", "2026-09-06T15:00:00+00:00", "caution")])
        self.assertEqual(doc["snooze_error"], "")
        self.assertTrue(all(area["snoozed"] == [] for area in doc["areas"] if area["id"] != "wt"))
        later = health_screen.document(self.connection, now="2026-09-06T15:00:01Z", fleet=fleet_of(TREES),
                                       ports=ports_snapshot, protection=protection_of(PROTECTION))
        self.assertEqual({row["id"] for row in later["areas"][2]["rows"]}, {"gone:group/alpha", "unread:beta"})
        self.assertEqual(later["areas"][2]["snoozed"], [])

    def test_a_snoozed_row_whose_problem_changed_shows_again(self):
        """sd:1896 review: `dep:<repo>` names the repository, not its alerts. A snooze of one low alert does not
        hide the critical one that joins it, and the row hides again only if it reads as it did."""
        def alerts(severity):
            found = [{**PROTECTION[0], "managed": True, "observed_at": NOW,
                      "alerts": {"dependabot": {"open": sum(severity.values()), "severity": severity}}}]
            return self.doc(protection=lambda connection: found)
        low = alerts({"low": 1})
        snooze(self.connection, "health:dep:/checkouts/alpha", "2026-09-06T15:00:00Z",
               seen=self.seen("dep:/checkouts/alpha", low), now=NOW)
        self.assertEqual([row["id"] for row in alerts({"low": 1})["areas"][4]["snoozed"]], ["dep:/checkouts/alpha"])
        worse = alerts({"low": 1, "critical": 1})["areas"][4]
        self.assertEqual([(row["id"], row["state"]) for row in worse["rows"]], [("dep:/checkouts/alpha", "warning")])
        self.assertEqual(worse["snoozed"], [])

    def test_a_snooze_read_that_fails_shows_every_row_and_says_why(self):
        snooze(self.connection, "health:gone:group/alpha", "2026-09-06T15:00:00Z", seen=self.seen("gone:group/alpha"), now=NOW)
        with patch.object(writes, "snoozed", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertEqual({row["id"] for row in doc["areas"][2]["rows"]}, {"gone:group/alpha", "unread:beta"})
        self.assertEqual((doc["areas"][2]["snoozed"], doc["snooze_error"]), ([], "database is locked"))

    def test_registrations_whose_directory_is_gone_are_one_row_per_checkout(self):
        wt = self.doc()["areas"][2]
        rows = {row["id"]: row for row in wt["rows"]}
        self.assertEqual(set(rows), {"gone:group/alpha", "unread:beta"})
        gone = rows["gone:group/alpha"]
        self.assertEqual((gone["state"], gone["type"], gone["repo_path"]), ("caution", "worktree registrations", "/checkouts/group/alpha"))
        self.assertEqual(gone["what"], "group/alpha: 2 worktrees registered, directory gone")
        self.assertEqual(gone["facts"]["Registered"], "2")
        self.assertEqual(gone["list"], ["/work/wt-a (feat/a)", "/work/wt-b (fix/b)"])
        unread = rows["unread:beta"]
        self.assertEqual((unread["state"], unread["type"], unread["list"]), ("unknown", "unread registrations", ["(unnamed) (?)"]))

    def test_a_fleet_with_every_registration_present_is_one_ok_row(self):
        (row,) = self.doc(trees=[TREES[3]])["areas"][2]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"]), ("wt:ok", "ok", "check"))
        self.assertIn("checked 1 registration in 1 checkout under /checkouts", row["detail"])

    def test_a_reader_that_fails_is_its_area_error_and_the_other_still_answers(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        areas = self.doc(fleet=broken)["areas"]
        self.assertEqual((areas[2]["error"], areas[2]["rows"]), ("fleet collection was stopped at its budget: sessions", []))
        self.assertEqual((areas[6]["error"], bool(areas[6]["rows"])), ("", True))
        areas = self.doc(fleet=lambda area: {"root": "/checkouts", "worktrees": [{"repo": "x"}]})["areas"]
        self.assertEqual(areas[2]["error"], "fleet collector returned an incomplete sessions document")

    def test_a_checkout_root_that_does_not_exist_is_not_read_rather_than_healthy(self):
        # The collector answers a missing REPO_ROOT with no worktrees, which would read as "checked 0 registrations".
        worktrees = self.doc(fleet=fleet_of([], root="/checkouts/missing", exists=False))["areas"][2]
        self.assertEqual(worktrees["rows"], [], "a missing checkout root was reported as a reading")
        self.assertEqual(worktrees["error"], "the checkout root /checkouts/missing does not exist; REPO_ROOT names it")

    def test_ports_are_the_operations_rows_with_its_counts_and_warnings(self):
        ports = self.doc()["areas"][6]
        rows = {row["id"]: row for row in ports["rows"]}
        self.assertEqual({key: (row["state"], row["type"], row["port"]) for key, row in rows.items()},
                         {"port:observed:5000": ("queued", "port", "5000"), "port:svc-a:9010": ("ok", "port", "9010"),
                          "port:svc-b:9020": ("unknown", "port", "9020")})
        self.assertEqual(rows["port:observed:5000"]["what"], ":5000 observed only")
        self.assertEqual(rows["port:svc-a:9010"]["facts"]["Process"], "svc (pid 42)")
        self.assertEqual(rows["port:svc-b:9020"]["facts"]["Process"], "Listener inspection unavailable")
        self.assertEqual(ports["extra"]["counts"], {"configured": 2, "listening": 2, "unknown": 1})
        self.assertEqual(ports["extra"]["warnings"], [
            "Listener state is unknown for 1 configured ports.",
            "The wider TCP listener inventory is incomplete or unavailable; positive observations remain visible."])
        self.assertEqual(ports["at"], NOW)

    def test_a_port_scan_over_its_budget_is_the_area_error_not_an_empty_inventory(self):
        def refused():
            raise health_screen.ports_screen.OverBudget("lsof ran past 5 s")
        areas = self.doc(ports=refused)["areas"]
        self.assertEqual(areas[6]["rows"], [])
        self.assertIn("exceeded its collection budget and was stopped rather than waited on: lsof ran past 5 s", areas[6]["error"])
        self.assertEqual(areas[2]["rows"][0]["id"], "gone:group/alpha")
        empty = self.doc(ports=lambda: {"services": [], "complete": False})["areas"][6]
        self.assertEqual(empty["error"], "port inventory is unavailable")

    def test_protection_cells_follow_the_classic_screen_and_an_unread_repo_has_none(self):
        prot = self.doc()["areas"][7]
        rows = {row["id"]: row for row in prot["rows"]}
        self.assertEqual(prot["extra"]["columns"],
                         ["prot:/checkouts/beta", "prot:/checkouts/gamma", "prot:/checkouts/alpha", "prot:/checkouts/delta"])
        self.assertEqual(prot["extra"]["counts"], {"unprotected": 1, "unknown": 1, "protected": 2})
        self.assertEqual(prot["extra"]["reasons"], ["token does not reach it"])
        self.assertEqual(prot["at"], "2026-09-05T08:00:00Z")
        self.assertEqual(len(prot["extra"]["checks"]), 11)
        cells = lambda key: {cell[0]: cell[1:] for cell in rows[key]["cells"]}
        alpha = cells("prot:/checkouts/alpha")
        self.assertEqual((alpha["Reviews"], alpha["Admins"], alpha["Squash message"], alpha["Required check"], alpha["Rebase merge"]),
                         (["gap", "no approving review is required"], ["ok"], ["ok"],
                          ["gap", "sd/local-gate · required_check sentence"], ["na"]))
        beta = cells("prot:/checkouts/beta")
        self.assertEqual((beta["Checks"], beta["Reviews"], beta["Admins"], beta["Bypass"]),
                         (["gap", "no check is required"], ["ok"], ["na"], ["na"]))
        # Unknown is not protected: no cell at all, not even the merge setting the body carries.
        self.assertEqual(rows["prot:/checkouts/gamma"]["cells"], [])
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()}, {
            "prot:/checkouts/alpha": ("caution", "group/alpha: 2 gaps"),
            "prot:/checkouts/beta": ("warning", "/checkouts/beta: main is unprotected"),
            "prot:/checkouts/gamma": ("unknown", "group/gamma: protection not read"),
            "prot:/checkouts/delta": ("ok", "group/delta: protected, no gap")})
        self.assertEqual(rows["prot:/checkouts/beta"]["sentence"], "main has no protection")

    def test_an_empty_registry_is_one_unknown_row_not_a_protected_fleet(self):
        (row,) = self.doc(protection=protection_of([]))["areas"][7]["rows"]
        self.assertEqual((row["id"], row["state"]), ("prot:none", "unknown"))

    def test_the_default_protection_reader_shows_an_unobserved_repo_as_unknown(self):
        self.repo("/checkouts/never")
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot)
        (row,) = doc["areas"][7]["rows"]
        self.assertEqual((row["state"], row["reason"], row["cells"]), ("unknown", "not yet observed", []))

    def test_a_checkout_borrowing_a_siblings_row_names_the_lender(self):
        """sd:1607: a second checkout of one repository reads its sibling's row, and says whose."""
        upsert_repo(self.connection, "/checkouts/widget", remote="git@github.com:example/widget.git")
        upsert_repo(self.connection, "/checkouts/widget-copy", remote="git@github.com:example/widget.git")
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES ('/checkouts/widget', '2026-10-04T01:00:00Z', 'protected', 'main', '{}')")
        self.connection.commit()
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot)
        rows = {row["id"]: row for row in doc["areas"][7]["rows"]}
        self.assertEqual(rows["prot:/checkouts/widget-copy"]["status"], "protected")
        self.assertEqual(rows["prot:/checkouts/widget-copy"]["facts"]["Borrowed from"], "/checkouts/widget")
        self.assertNotIn("Borrowed from", rows["prot:/checkouts/widget"]["facts"])

    def area(self, key, **kwargs):
        return next(area for area in self.doc(**kwargs)["areas"] if area["id"] == key)

    def test_dependencies_are_a_row_per_managed_repo_with_open_alerts_and_unread_is_never_clean(self):
        """sd:2205: the stored alerts of managed repositories; a sibling checkout is not counted twice."""
        rows = {row["id"]: row for row in self.area("dep", protection=protection_of(ALERTED))["rows"]}
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()}, {
            "dep:/checkouts/alpha": ("warning", "group/alpha: 100+ open Dependabot alerts"),
            "dep:/checkouts/delta": ("caution", "group/delta: 2 open Dependabot alerts"),
            "dep:unread": ("unknown", "2 managed repos: dependencies not read"),
        })
        self.assertEqual(rows["dep:/checkouts/alpha"]["detail"], "1 critical · 99 low · severity past the first page not read")
        self.assertEqual(rows["dep:/checkouts/alpha"]["cli"],
                         "gh api --paginate 'repos/group/alpha/dependabot/alerts?state=open' --jq '.[].html_url'")
        self.assertEqual(rows["dep:unread"]["list"], ["group/gamma: token does not reach it",
                                                      "group/epsilon: API HTTP 403; retry on a later collection"])
        self.assertEqual(self.area("dep", protection=protection_of(ALERTED))["at"], "2026-09-05T08:00:00Z")

    def test_a_page_cut_short_is_a_warning_since_the_rest_may_be_grave(self):
        paged = {**ALERTED[4], "alerts": {"dependabot": {"open": 100, "more": True, "severity": {"low": 100}}}}
        (row,) = self.area("dep", protection=protection_of([paged]))["rows"]
        self.assertEqual((row["state"], row["detail"]), ("warning", "100 low · severity past the first page not read"))

    def test_a_repo_not_re_read_in_48_hours_is_a_caution_even_beside_a_fresh_one(self):
        old = {**ALERTED[4], "observed_at": "2026-09-04T08:00:00Z",
               "alerts": {"dependabot": {"open": 0, "more": False}, "secret_scanning": {"visibility": "private"}}}
        fresh = {**ALERTED[0], "alerts": {"dependabot": {"open": 0, "more": False}, "secret_scanning": {"visibility": "private"}}}
        for key, name in (("dep", "dependencies"), ("sec", "security")):
            rows = {row["id"]: row for row in self.area(key, protection=protection_of([old, fresh]))["rows"]}
            self.assertEqual((rows[f"{key}:stale"]["state"], rows[f"{key}:stale"]["what"]),
                             ("caution", f"1 managed repo: {name} not re-read in 48 hours"), key)
            self.assertEqual(rows[f"{key}:stale"]["list"], ["group/delta: observed 2026-09-04T08:00:00Z"])

    def test_of_sibling_checkouts_the_newest_observation_counts(self):
        stale = {**ALERTED[0], "repo": "/checkouts/aardvark", "observed_at": "2026-09-01T08:00:00Z",
                 "alerts": {"dependabot": {"open": 0, "more": False}}}
        rows = [row["id"] for row in self.area("dep", protection=protection_of([stale, ALERTED[0]]))["rows"]]
        self.assertEqual(rows, ["dep:/checkouts/alpha"])

    def test_no_open_alert_is_an_ok_row_and_no_managed_repo_is_unknown(self):
        clean = [{**repo, "alerts": {"dependabot": {"open": 0, "more": False}}} for repo in ALERTED[:2]]
        (row,) = self.area("dep", protection=protection_of(clean))["rows"]
        self.assertEqual((row["id"], row["state"], row["what"]), ("dep:ok", "ok", "No open Dependabot alert in 1 managed repo"))
        for key in ("dep", "sec"):
            (row,) = self.area(key)["rows"]
            self.assertEqual((row["id"], row["state"]), (f"{key}:none", "unknown"))

    def test_an_older_library_without_the_managed_flag_is_the_area_error_not_an_empty_fleet(self):
        older = [{key: value for key, value in repo.items() if key not in ("managed", "alerts")} for repo in PROTECTION]
        for key in ("dep", "sec"):
            area = self.area(key, protection=protection_of(older))
            self.assertEqual(area["rows"], [])
            self.assertIn("reports no managed flag", area["error"])

    def test_security_is_open_secret_alerts_and_public_repos_with_scanning_off(self):
        """sd:2206: private repos are not scanned, by policy, and are counted, not flagged."""
        rows = {row["id"]: row for row in self.area("sec", protection=protection_of(ALERTED))["rows"]}
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()}, {
            "sec:/checkouts/alpha": ("warning", "group/alpha: 1 open secret-scanning alert"),
            "sec:off:/checkouts/delta": ("caution", "group/delta: public, secret scanning off"),
            "sec:unread": ("unknown", "2 managed repos: security not read"),
        })
        self.assertEqual(rows["sec:/checkouts/alpha"]["cli"],
                         "gh api --paginate 'repos/group/alpha/secret-scanning/alerts?state=open' --jq '.[].html_url'")
        clean = [{**ALERTED[0], "alerts": {"secret_scanning": {"visibility": "public", "setting": "enabled", "open": 0}}},
                 {**ALERTED[4], "alerts": {"secret_scanning": {"visibility": "private"}}}]
        (row,) = self.area("sec", protection=protection_of(clean))["rows"]
        self.assertEqual((row["state"], row["facts"]), ("ok", {"Clean": "1", "Private": "1"}))

    def test_the_default_reader_finds_the_alerts_the_nightly_sync_stored(self):
        upsert_repo(self.connection, "/checkouts/widget", remote="git@github.com:example/widget.git")
        self.connection.execute("UPDATE repo SET managed = 1")
        body = {"alerts": {"dependabot": {"open": 1, "more": False, "severity": {"high": 1}},
                           "secret_scanning": {"visibility": "public", "setting": "disabled"}}}
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES ('/checkouts/widget', '2026-09-05T02:20:00Z', 'protected', 'main', ?)",
                                (json.dumps(body),))
        self.connection.commit()
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot)
        areas = {area["id"]: area for area in doc["areas"]}
        self.assertEqual([(row["id"], row["state"]) for row in areas["dep"]["rows"]], [("dep:/checkouts/widget", "warning")])
        self.assertEqual([(row["id"], row["state"]) for row in areas["sec"]["rows"]], [("sec:off:/checkouts/widget", "caution")])

    def heartbeat(self, probes, stamp="2026-09-06T03:10:00Z"):
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp=stamp, body={"probes": probes})

    def test_credentials_light_by_presence_rejection_and_days_to_expiry(self):
        """sd:2203. NOW is 2026-09-06T12:00Z: 3 days left is a warning, 19 a caution, 86 fine."""
        self.heartbeat([
            {"id": "github_pat", "name": "GitHub PAT (GITHUB_PERSONAL_ACCESS_TOKEN)", "present": True, "valid": True,
             "expires": "2026-09-10T00:00:00Z"},
            {"id": "soon", "name": "Soon", "present": True, "valid": True, "expires": "2026-09-26T00:00:00Z"},
            {"id": "later", "name": "Later", "present": True, "valid": True, "expires": "2026-12-01T00:00:00Z"},
            {"id": "gh", "name": "gh CLI sign-in (gh auth status)", "signed_in": False},
            {"id": "ha_token", "name": "Home Assistant token (HA_TOKEN)", "present": False},
            {"id": "unreached", "name": "Unreached", "present": True, "reason": "not reached: OSError"},
            {"id": "mcp", "name": "MCP servers (claude mcp list)",
             "servers": [{"name": "github", "status": "connected"}, {"name": "slack", "status": "needs_auth"}]},
        ])
        area = self.area("cred")
        self.assertEqual(area["at"], "2026-09-06T03:10:00Z")
        self.assertEqual({row["id"]: (row["state"], row["what"]) for row in area["rows"]}, {
            "cred:github_pat": ("warning", "GitHub PAT (GITHUB_PERSONAL_ACCESS_TOKEN): expires in 3 days"),
            "cred:soon": ("caution", "Soon: expires in 19 days"),
            "cred:later": ("ok", "Later: expires in 85 days"),
            "cred:gh": ("warning", "gh CLI sign-in (gh auth status): rejected"),
            "cred:ha_token": ("warning", "Home Assistant token (HA_TOKEN): absent"),
            "cred:unreached": ("unknown", "Unreached: not read"),
            "cred:mcp": ("caution", "1 of 2 MCP servers not connected"),
        })

    def test_an_expiry_exactly_on_a_threshold_lights_that_threshold(self):
        self.heartbeat([{"id": "week", "name": "Week", "present": True, "valid": True, "expires": "2026-09-13T12:00:00Z"},
                        {"id": "month", "name": "Month", "present": True, "valid": True, "expires": "2026-10-06T12:00:00Z"}])
        self.assertEqual({row["id"]: row["state"] for row in self.area("cred")["rows"]},
                         {"cred:week": "warning", "cred:month": "caution"})

    def test_credentials_never_observed_or_gone_stale_say_so(self):
        (row,) = self.area("cred")["rows"]
        self.assertEqual((row["id"], row["state"]), ("cred:none", "unknown"))
        self.heartbeat([{"id": "gh", "name": "gh", "signed_in": True}], stamp="2026-09-01T03:10:00Z")
        rows = {row["id"]: row for row in self.area("cred")["rows"]}
        self.assertEqual((rows["cred:gh"]["state"], rows["cred:stale"]["state"]), ("ok", "caution"))
        self.assertEqual(rows["cred:stale"]["what"], "Credentials last observed 5 days ago")

    def test_an_older_library_without_the_credentials_module_is_the_area_error(self):
        with patch.dict("sys.modules", {"sd_db.credentials": None}):
            area = self.area("cred")
        self.assertEqual(area["rows"], [])
        self.assertIn("has no credentials module", area["error"])

    def test_malformed_stored_data_is_unknown_never_clean(self):
        bad = [{**ALERTED[0], "alerts": {"dependabot": ["bad"], "secret_scanning": ["bad"]}}, ALERTED[4]]
        for key in ("dep", "sec"):
            area = self.area(key, protection=protection_of(bad))
            rows = {row["id"]: row for row in area["rows"]}
            self.assertEqual(area["error"], "")
            self.assertEqual(rows[f"{key}:unread"]["list"], ["group/alpha: stored alerts are malformed"])
            self.assertEqual(len(rows), 2, rows)
        self.heartbeat([{"id": "mcp", "name": "MCP servers", "servers": [None]}, "junk"])
        self.assertEqual({row["id"]: row["state"] for row in self.area("cred")["rows"]},
                         {"cred:mcp": "caution", "cred:bad1": "unknown"})
        self.heartbeat([], stamp="2026-09-06T04:10:00Z")
        self.assertEqual([(row["id"], row["state"]) for row in self.area("cred")["rows"]], [("cred:empty", "unknown")])

    def test_a_credentials_row_without_a_probe_list_is_the_area_error(self):
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, body={"oops": 1})
        area = self.area("cred")
        self.assertEqual((area["rows"], area["error"]), ([], "the credentials heartbeat has no probe list"))

    def test_merged_branches_are_one_row_per_repo_and_a_checked_out_one_is_its_own(self):
        br = self.doc()["areas"][3]
        rows = {row["id"]: row for row in br["rows"]}
        self.assertEqual(list(rows), ["br:/checkouts/alpha", "brs:/checkouts/alpha", "br:no_default"])
        merged = rows["br:/checkouts/alpha"]
        self.assertEqual((merged["state"], merged["type"], merged["what"]),
                         ("caution", "merged branches", "alpha: 3 merged branches not deleted"))
        self.assertEqual(merged["facts"], {"Repo": "/checkouts/alpha", "Merged": "3", "Oldest": "2026-07-15", "Newest": "2026-09-01"})
        self.assertEqual(merged["cli"], "git -C /checkouts/alpha branch -d feat/one feat/two fix/three")
        self.assertEqual(merged["list"], ["feat/one · 2026-08-01", "feat/two · 2026-09-01", "fix/three · 2026-07-15"])
        held = rows["brs:/checkouts/alpha"]
        self.assertEqual((held["state"], held["type"], held["cli"]), ("queued", "merged branches", "git -C /checkouts/alpha worktree list"))
        self.assertIn("git branch -d refuses it", held["disabled"])
        self.assertEqual((rows["br:no_default"]["state"], rows["br:no_default"]["list"]), ("unknown", ["/checkouts/beta"]))
        self.assertIn("remote set-head origin --auto", rows["br:no_default"]["cli"])

    def test_no_merged_branch_is_an_ok_check_and_no_repo_is_unknown_not_clean(self):
        clean = {"repos": 3, "merged": [], "no_default": [], "unread": []}
        (row,) = self.doc(branches=scan_of(clean))["areas"][3]["rows"]
        self.assertEqual((row["id"], row["state"], row["detail"]),
                         ("br:ok", "ok", "checked the local branches of 3 repos against origin/HEAD"))
        (row,) = self.doc(branches=scan_of({**clean, "repos": 0}))["areas"][3]["rows"]
        self.assertEqual((row["id"], row["state"]), ("br:none", "unknown"))

    def test_disk_is_a_row_per_full_volume_the_biggest_storage_folders_and_merged_build_output(self):
        disk = self.doc()["areas"][0]
        rows = {row["id"]: row for row in disk["rows"]}
        self.assertEqual(list(rows), ["vol:/System/Volumes/Data", "rs:/Volumes/store/repo-storage/alpha",
                                      "rs:/Volumes/store/repo-storage/beta", "rs:/Volumes/store/repo-storage/gamma",
                                      "build:merged"])
        volume = rows["vol:/System/Volumes/Data"]
        self.assertEqual((volume["state"], volume["what"], volume["detail"]),
                         ("caution", "Mac data is 85% full", "/System/Volumes/Data · 150.0 GiB free of 1000.0 GiB"))
        folder = rows["rs:/Volumes/store/repo-storage/alpha"]
        self.assertEqual((folder["state"], folder["type"], folder["what"]), ("queued", "storage folder", "repo-storage/alpha holds 5.0 GiB"))
        self.assertEqual(folder["cli"], "du -sh /Volumes/store/repo-storage/alpha/* | sort -h | tail -5")
        build = rows["build:merged"]
        self.assertEqual((build["state"], build["type"], build["what"]), ("caution", "build output", "1 merged worktree keeps build output"))
        self.assertEqual(build["cli"], "rm -rf /work/wt-old/target\nrm -rf /work/wt-old/node_modules")
        self.assertEqual([volume["name"] for volume in disk["extra"]["volumes"]], ["Mac data", "store"])
        full = {**DISK, "volumes": [{**DISK["volumes"][1], "capacity": 90}], "build": {"checked": 2, "merged": [], "unread": []}}
        rows = {row["id"]: row for row in self.doc(disk=scan_of(full))["areas"][0]["rows"]}
        self.assertEqual(rows["vol:/Volumes/store"]["state"], "warning")
        self.assertEqual((rows["build:merged"]["state"], rows["build:merged"]["type"]), ("ok", "check"))

    def test_disk_with_no_storage_folder_configured_names_the_file_rather_than_a_clean_area(self):
        rows = {row["id"]: row for row in self.doc(disk=scan_of({**DISK, "storage": [], "refused": ["line 2: junk"]}))["areas"][0]["rows"]}
        self.assertEqual(rows["rs:none"]["state"], "unknown")
        self.assertIn("storage|<path> line in /config/project-dashboard/disk.conf (1 line not understood)", rows["rs:none"]["detail"])
        unread = {**DISK, "storage": [{"root": "/Volumes/gone", "folders": [], "error": "du did not finish inside the Disk budget"}]}
        rows = {row["id"]: row for row in self.doc(disk=scan_of(unread))["areas"][0]["rows"]}
        self.assertEqual((rows["rs:/Volumes/gone"]["state"], rows["rs:/Volumes/gone"]["detail"]),
                         ("unknown", "du did not finish inside the Disk budget"))

    def test_the_branch_reader_lists_merged_branches_and_names_repos_it_could_not_read(self):
        root = Path(self.tmp.name)
        alpha = cloned(root / "alpha")
        git("-C", str(alpha), "branch", "merged-one")
        git("-C", str(alpha), "branch", "held")
        git("-C", str(alpha), "worktree", "add", "-q", str(root / "wt-held"), "held")
        git("-C", str(alpha), "checkout", "-q", "-b", "ahead")
        git("-C", str(alpha), "commit", "-q", "--allow-empty", "-m", "not merged")
        git("-C", str(alpha), "checkout", "-q", "main")
        beta = root / "beta"
        git("init", "-q", "-b", "main", str(beta))
        git("-C", str(beta), "commit", "-q", "--allow-empty", "-m", "first")
        for path in (alpha, beta, root / "gone"):
            self.repo(str(path))
        scan = REAL_BRANCH_SCAN(self.connection)
        self.assertEqual(scan["repos"], 1)
        (found,) = scan["merged"]
        # main is the default branch and ahead is not merged: neither is listed.
        self.assertEqual(found["deletable"], [("merged-one", "2026-09-05")])
        self.assertEqual([branch for branch, _ in found["checked_out"]], ["held"])
        self.assertEqual(Path(found["checked_out"][0][1]).resolve(), (root / "wt-held").resolve())
        self.assertEqual((scan["no_default"], scan["unread"]), ([str(beta)], [str(root / "gone")]))

    def test_a_slow_branch_walk_is_stopped_at_its_budget_and_is_the_branches_error(self):
        stub = self.stub("git", self.stalled())
        for name in ("one", "two"):
            self.repo(f"/checkouts/{name}")
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            doc = self.doc(branches=lambda connection: REAL_BRANCH_SCAN(
                connection, within=0.5, repo_paths=["/checkouts/one", "/checkouts/two"]))
        br = doc["areas"][3]
        self.assertFalse(self.finished.exists(), "the page waited on the branch walk instead of stopping it")
        self.assertEqual((br["rows"], br["error"]), ([], "the merged-branch walk ran past its budget of 0.5 seconds "
                                                         "and was stopped rather than waited on"))

    @property
    def finished(self):
        return Path(self.tmp.name) / "stalled-finished"

    def stalled(self):
        """A stub body that answers after 5 s and leaves a marker only then (sd:2667).

        The budget kills the shell, the one holding the pipes; its sleep holds
        none, so the read ends at the kill. A caller that returns without the
        marker stopped the command rather than waited it out, however long
        the kill took on a loaded machine.
        """
        return f'sleep 5 >/dev/null 2>&1\ntouch "{self.finished}"\n'

    def stub(self, name, body):
        stub = Path(self.tmp.name) / "bin"
        stub.mkdir(exist_ok=True)
        (stub / name).write_text("#!/bin/sh\n" + body)
        (stub / name).chmod(0o755)
        return stub

    DF = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
          "/dev/disk3s1s1 1000 400 600 40% /\n"
          "/dev/disk3s5 1000 850 150 85% /System/Volumes/Data\n"
          "devfs 10 10 0 100% /dev\n"
          "pseudofs 0 0 0 - /Volumes/pseudo\n"
          "/dev/disk5s1 2000 800 1200 40% /Volumes/my store\n")

    def test_the_disk_reader_keeps_the_data_volume_and_volumes_and_finds_merged_build_output(self):
        root = Path(self.tmp.name)
        alpha = cloned(root / "alpha")
        git("-C", str(alpha), "branch", "done")
        git("-C", str(alpha), "branch", "plain")
        git("-C", str(alpha), "branch", "ahead")
        for branch in ("done", "plain", "ahead"):
            git("-C", str(alpha), "worktree", "add", "-q", str(root / f"wt-{branch}"), branch)
        git("-C", str(root / "wt-ahead"), "commit", "-q", "--allow-empty", "-m", "not merged")
        (root / "wt-done" / "target").mkdir()
        (root / "wt-ahead" / "node_modules").mkdir()
        self.repo(str(alpha))
        store = root / "store"
        for name, size in (("big", 64), ("small", 8)):
            (store / name).mkdir(parents=True)
            (store / name / "data").write_bytes(b"x" * size * 1024)
        config = root / "disk.conf"
        config.write_text(f"# storage folders\nstorage|{store}\nstorage|{root / 'missing'}\nvolume|/nowhere\n")
        stub = self.stub("df", f"cat <<'EOF'\n{self.DF}EOF\n")
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            scan = REAL_DISK_SCAN(self.connection, config=config)
        self.assertEqual([volume["mount"] for volume in scan["volumes"]], ["/System/Volumes/Data", "/Volumes/my store"])
        self.assertEqual(scan["volumes"][0]["capacity"], 85)
        self.assertEqual(scan["build"]["checked"], 3)
        (merged,) = scan["build"]["merged"]
        self.assertEqual((Path(merged["path"]).resolve(), merged["branch"], merged["dirs"]), ((root / "wt-done").resolve(), "done", ["target"]))
        found, missing = scan["storage"]
        self.assertEqual([Path(folder["path"]).name for folder in found["folders"]], ["big", "small"])
        self.assertEqual(missing["error"], "the folder does not exist or is not mounted")
        self.assertEqual(scan["refused"], ["line 4: volume|/nowhere"])

    def test_the_committed_example_is_one_storage_folder_the_reader_understands(self):
        example = Path(health_collectors.__file__).resolve().parents[1] / "disk.conf.example"
        roots, refused, read = health_collectors.storage_roots(example)
        self.assertEqual((roots, refused, read), ([Path("/Volumes/change-me/repo-storage")], [], example))

    def test_a_du_that_does_not_finish_is_a_row_not_a_refusal_of_the_area(self):
        store = Path(self.tmp.name) / "store"
        store.mkdir()
        config = Path(self.tmp.name) / "disk.conf"
        config.write_text(f"storage|{store}\n")
        stub = self.stub("df", f"cat <<'EOF'\n{self.DF}EOF\n")
        self.stub("du", self.stalled())
        # The walk's clock holds still, so df spends none of the 1 s however
        # long it takes to start, and du alone meets its timeout (sd:2667).
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}), \
                patch.object(health_collectors, "time", HeldClock()):
            doc = self.doc(disk=lambda connection: REAL_DISK_SCAN(connection, within=1, config=config, repo_paths=[]))
        disk = doc["areas"][0]
        self.assertFalse(self.finished.exists(), "the page waited on du instead of stopping it")
        self.assertEqual(disk["error"], "")
        rows = {row["id"]: row for row in disk["rows"]}
        self.assertEqual(rows[f"rs:{store}"]["detail"], "du did not finish inside the Disk budget")


    def test_the_walkers_run_at_once_so_the_page_waits_for_the_slowest_not_the_sum(self):
        # Four walkers that each wait for all four: at once they meet and
        # answer; in series the first waits alone until the page's budget
        # leaves it. A meeting, not a stopwatch: 5 x 0.8 s against a 2 s
        # bound failed under gate load with the walkers at once (sd:2667).
        # The timeout only ends a walker that waits alone, as a page that ran
        # them inline would leave it.
        met = threading.Barrier(4, timeout=10)
        self.addCleanup(met.abort)

        def slow(answer):
            def read(*args, **kwargs):
                met.wait()
                return answer(*args, **kwargs)
            return read
        doc = self.doc(fleet=slow(fleet_of(TREES)), ports=slow(ports_snapshot),
                       disk=slow(scan_of(DISK)), branches=slow(scan_of(BRANCHES)))
        self.assertEqual([(area["id"], area["error"], bool(area["rows"])) for area in doc["areas"] if area["read"]],
                         [(key, "", True) for key in ("disk", "cred", "wt", "br", "dep", "sec", "ports", "prot")])

    def test_a_reader_past_the_page_budget_is_its_area_error_and_the_page_does_not_wait(self):
        # The reader answers only once the page has returned, so a page that
        # waited for it could not return before the reader's own 30 s: no
        # elapsed bound to lose under load (sd:2667).
        returned, answered = threading.Event(), threading.Event()
        self.addCleanup(returned.set)

        def stuck(area):
            returned.wait(30)
            answered.set()
            return fleet_of(TREES)(area)
        with patch.object(health_screen, "PAGE_SECONDS", 0.5):
            doc = self.doc(fleet=stuck)
        self.assertFalse(answered.is_set(), "the page waited on a reader past its budget")
        returned.set()
        wt = doc["areas"][2]
        self.assertEqual((wt["rows"], wt["error"]), ([], "the Worktrees reader was still running at the page's budget of "
                                                         "0.5 seconds and was left rather than waited on"))
        self.assertEqual((doc["areas"][6]["error"], bool(doc["areas"][6]["rows"])), ("", True))

    def test_the_default_walkers_get_the_registry_as_paths_not_the_connection(self):
        # A sqlite connection refuses a second thread: a default walker handed it would be its area's error.
        alpha = cloned(Path(self.tmp.name) / "alpha")
        git("-C", str(alpha), "branch", "merged-one")
        self.repo(str(alpha))
        with patch.object(health_collectors, "branch_scan", REAL_BRANCH_SCAN):
            doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot, protection=protection_of([]), disk=scan_of(DISK))
        br = doc["areas"][3]
        self.assertEqual((br["error"], [row["id"] for row in br["rows"]]), ("", [f"br:{alpha}"]))


class OneScanPerArea(Collectors, ScreenCase):
    """A reader past the page's budget holds one thread however often the page is read (sd:2520 review).

    The page made a thread pool per request and left a stuck reader's worker
    alive, so each timed-out refresh added a thread, and the interpreter
    joined them all at exit.
    """

    def blocked(self):
        """A fleet reader that blocks until the test ends, and the threads it ran on."""
        gate, threads = threading.Event(), []

        def read(area):
            threads.append(threading.current_thread())
            gate.wait(10)
            return fleet_of(TREES)(area)
        self.addCleanup(gate.set)
        return read, threads

    def doc(self, fleet, now=NOW):
        return health_screen.document(self.connection, now=now, fleet=fleet, ports=ports_snapshot, protection=protection_of(PROTECTION))

    def test_timed_out_requests_leave_at_most_one_live_worker_for_a_blocked_reader(self):
        read, threads = self.blocked()
        with patch.object(health_screen, "PAGE_SECONDS", 0.2):
            docs = [self.doc(read) for _ in range(4)]
        self.assertLessEqual(len([thread for thread in threads if thread.is_alive()]), 1, threads)
        self.assertEqual(len(threads), 1, "a request started a second scan while the first still ran")
        self.assertTrue(all(thread.daemon for thread in threads), "interpreter exit would wait on the scan")
        self.assertTrue(all(doc["areas"][2]["error"] for doc in docs))
        self.assertIn(f"that scan started at {NOW}, and no second one starts while it runs", docs[-1]["areas"][2]["error"])

    def test_a_reader_still_running_shows_its_last_answer_marked_stale(self):
        first = self.doc(fleet_of(TREES))["areas"][2]
        self.assertEqual((first["error"], first["stale"]), ("", ""))
        read, _ = self.blocked()
        later = "2026-09-05T12:30:00Z"
        with patch.object(health_screen, "PAGE_SECONDS", 0.2):
            wt = self.doc(read, now=later)["areas"][2]
        self.assertEqual((wt["error"], wt["rows"], wt["at"]), ("", first["rows"], NOW))
        self.assertTrue(wt["stale"].startswith("the Worktrees reader was still running at the page's budget"), wt["stale"])
        self.assertTrue(wt["stale"].endswith(f"these rows are the read of {NOW}"), wt["stale"])

    def test_a_reader_that_finishes_answers_again_on_the_next_request(self):
        self.doc(fleet_of(TREES))
        wt = self.doc(fleet_of([]), now="2026-09-05T12:30:00Z")["areas"][2]
        self.assertEqual((wt["error"], wt["stale"], wt["at"]), ("", "", "2026-09-05T12:30:00Z"))
        self.assertNotEqual(wt["rows"], self.doc(fleet_of(TREES))["areas"][2]["rows"])


class ThePage(Collectors, BrowserSession):
    fleet_backend = staticmethod(fleet_of(TREES))

    def setUp(self):
        super().setUp()
        # No lsof or docker runs in a test: the Ports area reads a fixture.
        self.listening.RequestHandlerClass.ports_backend = staticmethod(ports_snapshot)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/fleet-health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Health · system</title>", body)
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "snooze.js", "health.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)
        # /health is the service's own check, which the runtime reads; the page does not take it over.
        status, _, body = self.request("/health")
        self.assertEqual(json.loads(body)["service"], "sd-dashboard")

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/health")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/health?area=wt", headers=cookie)[0], 400)
        status, _, body = self.request("/api/health", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertRegex(doc["read"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([row["id"] for row in doc["areas"][2]["rows"]], ["gone:group/alpha", "unread:beta"])
        self.assertEqual(len(doc["areas"][6]["rows"]), 3)

    def test_the_rail_opens_health_at_its_page(self):
        self.assertEqual(v2.SECTIONS["Health"], "/fleet-health")
        self.assertNotIn("Health", v2.CLASSIC)
        self.assertIn("/operations?area=sessions", v2.SCREENS.values())


# Extras the Health script reads from the shell that the Tasks stand-in does not carry.
HEALTH_SHELL = r"""
window.shell.shq = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
window.shell.row = () => null;
window.shell.url = q => { OUT.url = String(q && q.toString ? q.toString() : q); };
window.shell.state = s => OUT.states.push(s);
"""


SNOOZED_TYPES = ("check", "storage folder", "build output", "volume", "worktree registrations", "unread registrations",
                 "merged branches", "port", "branch protection", "dependabot alerts", "secret scanning", "credential")


class TheScript(Collectors, ScreenCase):
    """health.js against the document `health_screen` builds from the fixtures above."""

    def run_page(self, body, doc=None, status=200):
        doc = doc if doc is not None else health_screen.document(
            self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
            protection=protection_of(PROTECTION))
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + HEALTH_SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "URLSearchParams.prototype.toString = function () { return ''; };\n"
                  + "ANSWER = (path, body) => path === '/api/health' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + SNOOZE_JS + HEALTH_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
                  + "(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_command_is_registered_with_the_design_id_risk_and_key(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null, typeof c.executes === 'boolean' ? c.executes : null, !!c.bulk]);""")
        self.assertEqual(out["R"]["reg"], [
            ["check.recheck", "check", "safe", "e", False, False],
            ["storage folder.du", "storage folder", "safe", "b", False, False],
            ["build output.rm", "build output", "confirm", "r", False, False],
            ["volume.df", "volume", "safe", "b", False, False],
            ["worktree registrations.prune", "worktree registrations", "confirm", "p", False, True],
            ["merged branches.delete", "merged branches", "safe", "d", False, True],
            ["port.inspect", "port", "safe", "i", False, False],
            ["protection.settings", "branch protection", "safe", "o", False, False],
            ["collector.sync", "collector", "safe", "r", None, False],
            ["dependabot alerts.review", "dependabot alerts", "safe", "o", False, False],
            ["secret scanning.review", "secret scanning", "safe", "o", False, False],
            ["credential.probe", "credential", "safe", "r", False, False],
        ] + [[f"{t}.{c}", t, "undo", key, None, True] for t in SNOOZED_TYPES
              for c, key in (("snooze", "z"), ("snooze-hour", "h"), ("snooze-week", "w"))]
          + [["snoozed row.unsnooze", "snoozed row", "undo", "s", None, True]])

    def test_an_area_with_no_reader_is_an_unknown_lamp_that_names_what_it_does_not_read(self):
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot, protection=protection_of(PROTECTION))
        cred = next(area for area in doc["areas"] if area["id"] == "cred")
        cred.update(read=False, source=None, rows=[], at=None,
                    missing=["GitHub PAT presence and expiry", "gh CLI sign-in"])
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html; R.sub = ELS.subhead.html;", doc)
        self.assertEqual(out["gets"], ["/api/health"])
        lamps = out["R"]["lamps"]
        self.assertRegex(lamps, r'data-area="cred" data-state="unknown"[^>]*>.*?no reader')
        for area in ("dep", "sec"):
            self.assertRegex(lamps, rf'data-area="{area}" data-state="unknown"[^>]*><span class="lbl">[^<]*<svg[^>]*><use[^>]*/></svg></span><span class="val"><span class="ph">not read</span>', area)
        self.assertRegex(lamps, r'data-area="wt" data-state="caution"[^>]*>.*?<b>2</b> dir gone')
        self.assertRegex(lamps, r'data-area="disk" data-state="caution"[^>]*>.*?<b>85%</b> fullest</span> <span class="ph">Mac data')
        self.assertRegex(lamps, r'data-area="br" data-state="caution"[^>]*>.*?<b>3</b> merged')
        self.assertEqual({tag for tag, _ in cell_grammar(self, lamps)}, {"button"})
        areas = out["R"]["areas"]
        self.assertIn("<b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: GitHub PAT presence and expiry", areas)
        self.assertIn("<b>Not read here:</b> merged worktrees still on disk", areas)
        self.assertIn("<b>Not read here:</b> build output sizes", areas)
        self.assertIn('data-id="gone:group/alpha"', areas)
        self.assertIn("8 areas · 1 warning, 5 caution rows want you · 1 area with no reader yet", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "findings want you"})
        self.assertIsNone(out["states"][-1])

    def test_an_alert_lamp_keeps_a_paged_total_and_says_when_a_repo_was_not_read(self):
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot, protection=protection_of(ALERTED))
        lamps = self.run_page("R.lamps = ELS.annunciator.html;", doc)["R"]["lamps"]
        self.assertRegex(lamps, r'data-area="dep"[^>]*>.*?<b>102\+</b> open alerts</span> · <span class="ph">not all read</span>')
        self.assertRegex(lamps, r'data-area="sec"[^>]*>.*?<b>1</b> open alerts</span> · <span class="ph">not all read</span>')

    def test_a_failed_reader_is_a_partial_read_and_its_lamp_is_unknown(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        doc = health_screen.document(self.connection, now=NOW, fleet=broken, ports=ports_snapshot,
                                     protection=protection_of([]), disk=scan_of(CLEAN_DISK), branches=scan_of(CLEAN_BRANCHES))
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html;", doc)
        self.assertRegex(out["R"]["lamps"], r'data-area="wt" data-state="unknown"[^>]*>.*?not read')
        self.assertIn("<b>Not read:</b> fleet collection was stopped at its budget: sessions", out["R"]["areas"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "findings to watch"})

    def test_a_stale_area_keeps_its_rows_and_says_it_was_not_re_read(self):
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot, protection=protection_of([]), disk=scan_of(CLEAN_DISK),
                                     branches=scan_of(CLEAN_BRANCHES))
        wt = doc["areas"][2]
        wt["stale"] = "the Worktrees reader was still running; these rows are the read of " + NOW
        out = self.run_page("R.areas = ELS.areas.html;", doc)
        self.assertIn("<b>Not re-read:</b> the Worktrees reader was still running; these rows are the read of " + NOW,
                      out["R"]["areas"])
        self.assertIn(f'data-id="{wt["rows"][0]["id"]}"', out["R"]["areas"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Worktrees: the Worktrees reader was still running", out["states"][-1]["text"])

    def test_a_document_that_does_not_arrive_leaves_no_row_and_says_so(self):
        out = self.run_page("""R.before = ELS.areas.html;
DOC = { error: 'Open a dashboard page before reading Health.' }; STATUS = 403;
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } }); await flush();
R.areas = ELS.areas.html; R.lamps = ELS.annunciator.html;""")
        self.assertEqual(out["gets"], ["/api/health", "/api/health"])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["before"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("Open a dashboard page before reading Health.", out["states"][-1]["text"])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})
        self.assertNotIn("data-id=", out["R"]["areas"], "rows from the last read stayed on screen")
        self.assertIn("not read", out["R"]["lamps"])

    def test_the_filter_finds_a_row_by_a_path_in_its_facts_or_its_list(self):
        # The box says "repo, path, branch": a registered worktree's path is in `list`, the checkout's in `facts`.
        out = self.run_page("""const filter = v => { ELS.q.value = v; ELS.q.listeners.input[0](); return ELS.areas.html; };
R.list = filter('/work/wt-b'); R.facts = filter('/checkouts/group/alpha'); R.none = filter('/work/nowhere');""")
        self.assertIn('data-id="gone:group/alpha"', out["R"]["list"], "a path in the row's list did not match")
        self.assertIn('data-id="gone:group/alpha"', out["R"]["facts"], "a path in the row's facts did not match")
        self.assertNotIn('data-id="gone:group/alpha"', out["R"]["none"])

    def test_a_refresh_retires_the_command_objects_and_picks_of_rows_it_no_longer_lists(self):
        # The shell keeps every object put and every pick; the bulk bar must not offer a prune built from a row that is gone.
        out = self.run_page("""var PICKED = new Set();
C.pick = id => { PICKED.has(id) ? PICKED.delete(id) : PICKED.add(id); document.dispatchEvent(new CustomEvent('shell:picked', { detail: [...PICKED] })); };
const refresh = () => ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } });
const live = id => !!OBJ.get(id) && REG.some(c => c.on === OBJ.get(id).type);
C.pick('gone:group/alpha'); C.pick('unread:beta');
DOC = { ...DOC, areas: DOC.areas.map(a => ({ ...a, rows: a.rows.filter(r => r.id !== 'gone:group/alpha') })) };
refresh(); await flush();
R.dropped = [live('gone:group/alpha'), PICKED.has('gone:group/alpha')]; R.kept = [live('unread:beta'), PICKED.has('unread:beta')];
DOC = { error: 'Open a dashboard page before reading Health.' }; STATUS = 403;
refresh(); await flush();
R.failed = [live('unread:beta'), PICKED.has('unread:beta'), PICKED.size];""")
        self.assertEqual(out["R"]["dropped"], [False, False], "a row the refresh dropped kept its command object or pick")
        self.assertEqual(out["R"]["kept"], [True, True], "a row the refresh still lists lost its object or pick")
        self.assertEqual(out["R"]["failed"], [False, False, 0], "a failed refresh kept a row's command object or pick")

    def test_an_older_read_that_answers_last_does_not_replace_the_newer_one(self):
        # Two re-reads overlap on the threaded server and the first one answers last: once as a failed request, once
        # as an older reading.
        out = self.run_page("""var pending = [];
ANSWER = () => new Promise((answer, fail) => pending.push({ answer, fail }));
const refresh = () => ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: 'refresh', dataset: {} } : null } });
refresh(); refresh(); await flush();
pending[1].answer([200, DOC]); await flush();
pending[0].fail(new Error('an older read that failed')); await flush();
R.areas = ELS.areas.html;
refresh(); refresh(); await flush();
pending[3].answer([200, DOC]); await flush();
pending[2].answer([200, { ...DOC, read: '2026-09-01T00:00:00Z' }]); await flush();
R.observed = document.body.dataset.observed;""")
        self.assertEqual(out["gets"], ["/api/health"] * 5)
        self.assertNotEqual(out["R"]["observed"], "2026-09-01T00:00:00Z", "an older answer replaced the newer reading")
        self.assertNotIn("an older read that failed", json.dumps(out["states"]), "an older reading replaced the newer one")
        self.assertIsNone(out["states"][-1])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["areas"])

    def test_a_lamp_shows_its_area_only_and_a_state_chip_narrows_the_rows(self):
        click = "ELS.{el}.listeners.click[0]({{ target: {{ closest: s => s === '{sel}' ? {{ id: '', dataset: {data} }} : null }} }});"
        out = self.run_page(click.format(el="annunciator", sel="button.cell", data="{ area: 'cred' }")
                            + "R.cred = ELS.areas.html; R.note = ELS.filtered.html;"
                            + click.format(el="annunciator", sel="button.cell", data="{ area: 'cred' }")
                            + click.format(el="filters", sel="[data-f]", data="{ f: 'state', v: 'unknown' }")
                            + "R.unknown = ELS.areas.html;")
        cred, unknown = out["R"]["cred"], out["R"]["unknown"]
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', cred), ["cred"])
        self.assertIn("Filtered to Credentials only: 1 of 20 rows.", out["R"]["note"])
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', unknown),
                         ["cred", "wt", "br", "dep", "sec", "ports", "prot"])
        self.assertEqual(re.findall(r'data-id="([^"]+)"', unknown),
                         ["cred:none", "unread:beta", "br:no_default", "dep:none", "sec:none", "port:svc-b:9020",
                          "prot:/checkouts/gamma"])

    def test_a_second_lamp_adds_its_area_and_pressing_one_again_takes_it_out(self):
        click = "ELS.annunciator.listeners.click[0]({{ target: {{ closest: s => s === 'button.cell' ? {{ id: '', dataset: {{ area: '{a}' }} }} : null }} }});"
        out = self.run_page(click.format(a="wt") + click.format(a="ports") + "R.both = ELS.areas.html;"
                            + click.format(a="wt") + "R.one = ELS.areas.html;")
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["both"]), ["wt", "ports"])
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["one"]), ["ports"])

    def test_ports_show_the_counts_line_and_warnings_and_protection_has_no_lamp(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = ELS.areas.html;")
        lamps, areas = out["R"]["lamps"], out["R"]["areas"]
        self.assertRegex(lamps, r'data-area="ports" data-state="unknown"[^>]*>.*?<b>1</b> unknown</span> · <span class="ph">2 listening')
        self.assertNotIn('data-area="prot"', lamps)
        self.assertIn('<p class="counts">2 configured ports · 2 listening · 1 unknown</p>', areas)
        self.assertIn("Listener state is unknown for 1 configured ports.", areas)

    def test_the_matrix_is_one_column_per_repo_and_an_unread_column_draws_no_cell(self):
        out = self.run_page("R.areas = ELS.areas.html;")
        areas = out["R"]["areas"]
        cols = re.findall(r'<button type="button" class="pmx-col" data-row="([^"]+)" data-status="(\w+)"[^>]*>(.*?)</button>', areas, re.S)
        self.assertEqual([(row, status) for row, status, _ in cols], [
            ("prot:/checkouts/beta", "unprotected"), ("prot:/checkouts/gamma", "unknown"),
            ("prot:/checkouts/alpha", "protected"), ("prot:/checkouts/delta", "protected")])
        svg = {row: body for row, _, body in cols}
        self.assertNotIn("<rect", svg["prot:/checkouts/gamma"])
        self.assertEqual(svg["prot:/checkouts/alpha"].count('class="c-gap"'), 2)
        self.assertEqual(svg["prot:/checkouts/beta"].count('class="c-gap"'), 1)
        self.assertEqual(svg["prot:/checkouts/delta"].count('class="c-gap"'), 0)
        self.assertIn('aria-label="/checkouts/beta: unprotected, 1 gap"', areas)
        self.assertIn('aria-label="group/gamma: unknown"', areas)
        self.assertIn("1 unknown, all for one reason: token does not reach it.", areas)
        self.assertIn('<p class="counts">2 protected · 1 unprotected · 1 unknown · ', areas)
        # The table fallback carries the same cells: GAP with its sentence, ok, and — where a check does not apply.
        table = areas[areas.index('<details class="pmx-table">'):]
        self.assertIn("<summary>Table: 4 repositories × 11 checks</summary>", table)
        self.assertIn('<a href="https://github.com/group/alpha/settings/branches" target="_blank" rel="noopener noreferrer">group/alpha</a>', table)
        self.assertIn("<b>GAP</b> <span class=\"sent\">no approving review is required</span>", table)
        self.assertIn("<th scope=\"row\">/checkouts/beta</th>", table)
        self.assertEqual(table.count("<td>token does not reach it</td>"), 1)
        # An unread repository has no cells: each of its checks reads "not read", never — (does not apply).
        gamma = re.search(r'<tr><th scope="row"><a [^>]*>group/gamma</a></th>.*?</tr>', table, re.S).group(0)
        self.assertEqual(gamma.count("<td>not read</td>"), 11, gamma)
        self.assertEqual(gamma.count("<td>—</td>"), 1, "only the Branch cell, which an unread repository has none of")
        self.assertIn("— does not apply; not read where the repository was not read.", table)

    def test_a_matrix_column_opens_its_repo_with_every_check_in_details(self):
        out = self.run_page("""ELS.areas.listeners.click[0]({ target: { closest: s => s === '.pmx-col' ? { dataset: { row: 'prot:/checkouts/alpha' } } : null } });
R.d = ELS.details.html; R.cli = cmd('protection.settings').cli(C.get('prot:/checkouts/alpha'));
R.off = cmd('protection.settings').when(C.get('prot:/checkouts/beta')); R.sync = cmd('collector.sync').cli(C.get('collector:protection'));
ELS.areas.listeners.click[0]({ target: { closest: s => s === '.pmx-col' ? { dataset: { row: 'prot:/checkouts/gamma' } } : null } });
R.unread = ELS.details.html; R.lsof = cmd('port.inspect').cli(C.get('port:svc-a:9010'));""")
        details = out["R"]["d"]
        self.assertIn("<h2>group/alpha: 2 gaps</h2>", details)
        self.assertIn('<dt>Reviews</dt><dd class="c-gap">GAP · no approving review is required</dd>', details)
        self.assertIn('<dt>Admins</dt><dd class="c-ok">ok</dd>', details)
        self.assertIn('<dt>Rebase merge</dt><dd class="c-na">—</dd>', details)
        self.assertIn("group/alpha · settings/branches", details)
        self.assertEqual(out["R"]["cli"], "open https://github.com/group/alpha/settings/branches")
        self.assertEqual(out["R"]["off"], "no github.com remote: there is no settings page")
        self.assertEqual(out["R"]["sync"], "sd shadow sync --max-seconds 120")
        self.assertEqual(out["R"]["lsof"], "lsof -nP -iTCP:9010 -sTCP:LISTEN")
        self.assertIn("No cell is shown: token does not reach it.", out["R"]["unread"])
        self.assertNotIn("<dl class=\"checks\">", out["R"]["unread"])

    def test_prune_confirms_first_is_copy_only_and_posts_nothing(self):
        out = self.run_page("""var o = C.get('gone:group/alpha'); R.cli = cmd('worktree registrations.prune').cli(o);
R.why = cmd('worktree registrations.prune').consequence(o); R.snooze = cmd('worktree registrations.snooze').when(o);
shellRun(cmd('worktree registrations.prune'), o); await flush();""")
        self.assertEqual(out["R"]["cli"], "git -C '/checkouts/group/alpha' worktree prune -v")
        self.assertEqual(out["R"]["why"], "Removes 2 worktree registrations whose directories are gone. No directory is touched.")
        self.assertIs(out["R"]["snooze"], True)
        self.assertEqual(out["confirms"], ["worktree registrations.prune"])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1][0], "Not run here: copy the line from Details and run it in a terminal · "
                                               "group/alpha: 2 worktrees registered, directory gone")

    def test_snooze_posts_the_row_and_its_time_then_reads_health_again_and_undo_clears_it(self):
        """sd:1896: the server judges the time, so the page sends one it computed, and Undo writes the same key with none."""
        out = self.run_page("""ANSWER = (path, body) => path === '/api/health' ? [200, DOC] : [200, { key: 'k', until: body.until }];
var o = C.get('gone:group/alpha'), t0 = Date.now(); OUT.gets = [];
shellRun(cmd('worktree registrations.snooze-hour'), o); await flush();
R.ahead = Date.parse(OUT.posts[0][1].until) - t0;
OUT.toasts[OUT.toasts.length - 1].undo(); await flush();
R.off = cmd('check.snooze').when({ id: 'wt:ok', state: 'ok' }); R.seen = o.seen;""")
        # The row's own fingerprint goes with it, so the snooze holds only while the row reads the same.
        self.assertRegex(out["R"]["seen"], r"^[0-9a-f]{16}$")
        self.assertEqual([(path, {k: v for k, v in body.items() if k != "until"}) for path, body, _ in out["posts"]],
                         [("/api/snooze", {"page": "health", "row": "gone:group/alpha", "seen": out["R"]["seen"]})] * 2)
        self.assertEqual(out["posts"][1][1]["until"], None)
        self.assertTrue(3590e3 <= out["R"]["ahead"] <= 3610e3, out["R"]["ahead"])
        self.assertEqual(out["gets"], ["/api/health", "/api/health"])
        self.assertRegex(out["toasts"][-2][0], r"^Snoozed until (\w{3} \w{3} \d\d )?\d\d:\d\d · group/alpha: ")
        self.assertTrue(out["toasts"][-2][1])
        self.assertEqual(out["toasts"][-1][0], "Snooze 1 hour undone · group/alpha: 2 worktrees registered, directory gone")
        self.assertEqual(out["R"]["off"], "an ok row has nothing to snooze")

    def test_a_snoozed_row_is_drawn_apart_and_unsnooze_brings_it_back(self):
        seen = next(row["seen"] for row in health_screen.document(
            self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
            protection=protection_of(PROTECTION))["areas"][2]["rows"] if row["id"] == "gone:group/alpha")
        snooze(self.connection, "health:gone:group/alpha", "2026-09-07T08:00:00Z", seen=seen, now=NOW)
        out = self.run_page("""ANSWER = (path, body) => path === '/api/health' ? [200, DOC] : [200, { key: 'k', until: body.until }];
R.areas = ELS.areas.html; R.snoozed = ELS.snoozed.html; var o = C.get('snoozed:gone:group/alpha'); R.type = o.type;
shellRun(cmd('snoozed row.unsnooze'), o); await flush(); OUT.toasts[OUT.toasts.length - 1].undo(); await flush();""")
        self.assertNotIn("directory gone", out["R"]["areas"])
        self.assertIn("Snoozed · 1", out["R"]["snoozed"])
        self.assertIn("group/alpha: 2 worktrees registered, directory gone", out["R"]["snoozed"])
        self.assertEqual(out["R"]["type"], "snoozed row")
        self.assertEqual([body for _, body, _ in out["posts"]],
                         [{"page": "health", "row": "gone:group/alpha", "until": None, "seen": seen},
                          {"page": "health", "row": "gone:group/alpha", "until": "2026-09-07T08:00:00+00:00", "seen": seen}])
        self.assertEqual(out["toasts"][-2][0], "Shows again · group/alpha: 2 worktrees registered, directory gone")

    def test_a_snooze_read_that_failed_is_a_partial_read(self):
        with patch.object(writes, "snoozed", side_effect=sqlite3.OperationalError("database is locked")):
            out = self.run_page("R.snoozed = ELS.snoozed.html;")
        self.assertEqual(out["states"][-1], {"kind": "partial", "source": "/api/health",
                                             "text": "Snoozes were not read: database is locked. Every row shows."})
        self.assertEqual(out["R"]["snoozed"], "")

    def sync(self, finished):
        """Re-run collector on the protection collector: the run starts, reads as running once, then `finished`."""
        return self.run_page("""var n = 0; ANSWER = (path, body) => path === '/api/health' ? [200, DOC]
  : path === '/api/shadow/sync' ? [202, { running: true }]
  : path === '/api/shadow/state' ? (n++ ? [200, """ + json.dumps(finished) + """] : [200, { running: true }]) : [404, { error: 'no answer' }];
OUT.gets = []; shellRun(cmd('collector.sync'), C.get('collector:protection')); await flush();""")

    def test_rerun_collector_starts_the_servers_sync_and_reads_health_again_when_it_ends(self):
        # sd:2894: the route Contributions posts (sd:2207); `sd shadow sync` writes the protection rows Health shows.
        out = self.sync({"running": False, "error": None, "trackers": [
            {"tracker": "github", "ok": True, "configured": True, "reason": "", "lines": []}]})
        self.assertEqual(out["posts"], [["/api/shadow/sync", {}, 64]])
        self.assertEqual(out["gets"], ["/api/shadow/state", "/api/shadow/state", "/api/health"])
        self.assertEqual([t[0] for t in out["toasts"]][-1], "Shadow sync started · Health reads again when it ends")

    def test_a_sync_that_ends_broken_says_why(self):
        out = self.sync({"running": False, "error": "OperationalError: database is locked", "trackers": []})
        self.assertEqual(out["toasts"][-1][0], "Shadow sync ended with a problem: OperationalError: database is locked")

    def test_disk_draws_a_bar_per_volume_and_its_lines_are_copy_only(self):
        out = self.run_page("""R.areas = ELS.areas.html; var o = C.get('build:merged');
R.rm = cmd('build output.rm').cli(o); R.why = cmd('build output.rm').consequence(o);
R.du = cmd('storage folder.du').cli(C.get('rs:/Volumes/store/repo-storage/alpha'));
shellRun(cmd('build output.rm'), o); await flush();""")
        areas = out["R"]["areas"]
        self.assertIn('<div class="bars" role="group" aria-label="Volume use: Mac data 85%, store 40%">', areas)
        self.assertIn('<span class="name" title="/Volumes/store">store</span>', areas)
        self.assertIn('<rect class="fill" x="0.5" y="0.5" width="85.0%" height="11" rx="2"/>', areas)
        self.assertIn('<span class="v">85% · 150.0 GiB free</span>', areas)
        self.assertEqual(out["R"]["rm"], "rm -rf /work/wt-old/target\nrm -rf /work/wt-old/node_modules")
        self.assertIn("Deletes the build output of 1 merged worktrees: /work/wt-old.", out["R"]["why"])
        self.assertEqual(out["R"]["du"], "du -sh /Volumes/store/repo-storage/alpha/* | sort -h | tail -5")
        self.assertEqual(out["confirms"], ["build output.rm"])
        self.assertEqual(out["posts"], [])

    def test_delete_merged_is_copy_only_and_a_checked_out_branch_says_why_git_refuses(self):
        out = self.run_page("""var o = C.get('br:/checkouts/alpha'), held = C.get('brs:/checkouts/alpha');
R.cli = cmd('merged branches.delete').cli(o); R.why = cmd('merged branches.delete').consequence(o);
R.on = cmd('merged branches.delete').when(o); R.held = cmd('merged branches.delete').when(held);
shellRun(cmd('merged branches.delete'), o); await flush();""")
        self.assertEqual(out["R"]["cli"], "git -C /checkouts/alpha branch -d feat/one feat/two fix/three")
        self.assertEqual(out["R"]["why"], "Deletes 3 local branches already in origin's default branch. git branch -d refuses any unmerged one.")
        self.assertIs(out["R"]["on"], True)
        self.assertEqual(out["R"]["held"], "checked out in a worktree: git branch -d refuses it until the worktree is removed")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1][0], "Not run here: copy the line from Details and run it in a terminal · "
                                               "alpha: 3 merged branches not deleted")

    def test_details_show_the_facts_and_the_registrations_behind_a_row(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: 'gone:group/alpha' })); R.d = ELS.details.html;")
        details = out["R"]["d"]
        self.assertIn("<h2>group/alpha: 2 worktrees registered, directory gone</h2>", details)
        self.assertIn("<dt>Repo</dt><dd>/checkouts/group/alpha</dd>", details)
        self.assertIn("<li>/work/wt-a (feat/a)</li>", details)
        self.assertIn("<dt>Source</dt><dd>the fleet&#39;s .git/worktrees registrations</dd>", details)

    def test_the_first_read_opens_the_first_row_in_details(self):
        out = self.run_page("R.d = document.getElementById('details').html || '';")
        self.assertIn("<h2>Mac data is 85% full</h2>", out["R"]["d"],
                      "Details still say the fleet is being read after the rows arrived")

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", HEALTH_JS)
        self.assertNotRegex(HEALTH_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", HEALTH_JS)), [])
        # The shell owns j / k and Esc through PAGE_LIST; a page-level handler is drift.
        self.assertNotRegex(HEALTH_JS, r"e\.key === '[jk]'")


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "health", "Health", "/fleet-health", ("/api/health",)


if __name__ == "__main__":
    unittest.main()
