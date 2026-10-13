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

from sd_dashboard import health_collectors, health_screen, runtime, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar

V2 = Path(v2.__file__).resolve().parent
#: The real reader, before `Collectors` replaces it for every test.
REAL_READ_SERVICES = getattr(health_screen, "read_services", None)
HEALTH_JS = (V2 / "static" / "health.js").read_text(encoding="utf-8")
SNOOZE_JS = (V2 / "static" / "snooze.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")

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
                          "dirs": ["target", "node_modules"], "status": ""},
                         {"path": "/work/wt-dirty", "head": "1" * 40, "branch": "feat/wip", "repo": "/checkouts/alpha",
                          "dirs": ["target"], "status": "?? notes.txt\n"}],
              "revision": "b" * 64},
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
                "checked_out": [["feat/live", "/work/wt-live"]], "revision": "a" * 64}],
}
#: The doctor's lines as `parse_doctor` reads them: one FAIL, one WARN with its continuation, passes, a skip and context.
DOCTOR_OUT = """== doctor
  ok      profile recorded: hub
  FAIL    Xcode CLT missing — run: xcode-select --install
  WARN    brew doctor has warnings — run: brew doctor
  ok      fnm (v24.1.0)
  --      docker not checked (not installed, no services in this profile)
  WARN    set here but not in shell-env.example: EXAMPLE_TOKEN
          add the name (value change-me) so a rebuild gets it

  folders without a filled .env (.env.example present, .env absent):
    --      local-redis/.env  (copy .env.example)

  1 hard failure(s)
"""
DOCTOR = health_collectors.parse_doctor(DOCTOR_OUT, 1)
#: The checkout's HEAD in every test: no test reads this checkout's git (sd:3707).
CHECKOUT = "c" * 40


def service_of(label, state, *, pid=None, last_exit=None, scope="user", category="service", start="", restart="",
               reason=None, rev="1"):
    """One `sd_db.services.inventory` entry; `start` and `restart` are the reason each is off, "" when it is on."""
    return {"id": label, "label": label, "name": label.rsplit(".", 1)[-1], "domain": "gui/501" if scope == "user" else "system",
            "scope": scope, "category": category, "state": state, "pid": pid, "last_exit": last_exit, "last_signal": None,
            "reason": reason, "capabilities": {action: {"allowed": not why, "reason": why} for action, why in (
                ("start", start), ("stop", ""), ("restart", restart))},
            "last_request": None, "control_note": "", "revision": rev * 64}


#: What every test reads unless it hands `services` an inventory: one running service, so no count elsewhere moves.
QUIET_SERVICES = {"services": [service_of("com.example.alpha", "running", pid=101, start="service is already running")],
                  "revision": "0" * 64}
PROTECTED = "protected to preserve dashboard and tailnet access"
SERVICES = {"services": [
    service_of("com.example.alpha", "running", pid=101, start="service is already running"),
    service_of("com.example.beta", "unloaded", restart="service is already unloaded; use Start to load it",
               reason="service is installed but not loaded", rev="2"),
    service_of("com.example.gamma", "failed", last_exit=78, rev="3"),
    service_of(runtime.LABEL, "running", pid=202, start=PROTECTED, restart=PROTECTED, rev="4"),
    service_of("com.example.daily", "idle", category="scheduled"),
    service_of("com.example.daemon", "running", pid=1, scope="system"),
], "revision": "0" * 64}

CLEAN_DOCTOR = health_collectors.parse_doctor("== doctor\n  ok      profile recorded: hub\n\n  no hard failures\n", 0)
CLEAN_DISK = {**DISK, "volumes": [DISK["volumes"][1]], "build": {"checked": 4, "merged": [], "unread": []}}
CLEAN_BRANCHES = {"repos": 2, "merged": [], "no_default": [], "unread": []}
REAL_DISK_SCAN, REAL_BRANCH_SCAN, REAL_DOCTOR_SCAN = (health_collectors.disk_scan, health_collectors.branch_scan,
                                                     health_collectors.doctor_scan)


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
        # No test runs the machine's doctor (sd:3393): the default reader reads this fixture.
        patcher = patch.object(health_collectors, "doctor_scan", lambda **_: CLEAN_DOCTOR)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No test runs launchctl or reads this checkout's HEAD (sd:3707): a test hands `services` its inventory.
        for name, value in (("read_services", lambda connection, backend=None: backend or QUIET_SERVICES),):
            patcher = patch.object(health_screen, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(runtime, "checkout_commit", lambda: CHECKOUT)
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
                                      disk=kwargs.get("disk"), branches=kwargs.get("branches"),
                                      doctor=kwargs.get("doctor"), **{key: kwargs[key] for key in ("services", "served_commit")
                                                                      if key in kwargs})

    def test_every_design_area_is_there_in_order_and_says_whether_a_reader_covers_it(self):
        areas = self.doc()["areas"]
        self.assertEqual([(a["id"], a["read"]) for a in areas],
                         [("disk", True), ("cred", True), ("wt", True),
                          ("br", True), ("dep", True), ("sec", True), ("ports", True), ("prot", True), ("svc", True),
                          ("doc", True)])
        for area in areas:
            if area["id"] not in ("br", "ports", "prot", "svc", "cred", "dep", "sec", "doc"):
                self.assertTrue(area["missing"], f"{area['id']} names nothing it does not read")
            if not area["read"]:
                self.assertEqual((area["rows"], area["error"], area["source"]), ([], "", None))

    def seen(self, row_id, doc=None):
        """The row's fingerprint as the page reads it, which the page posts with its snooze."""
        return next(row["seen"] for area in (doc or self.doc())["areas"] for row in area["rows"] if row["id"] == row_id)

    def services(self, served_commit=CHECKOUT, inventory=SERVICES):
        area = next(area for area in self.doc(services=inventory, served_commit=served_commit)["areas"] if area["id"] == "svc")
        return area, {row["id"]: row for row in area["rows"]}

    def test_services_are_the_long_running_user_agents_with_launchds_state_and_the_controls_sd_services_allows(self):
        """sd:3707: a row per KeepAlive user LaunchAgent; down is caution, a failed exit warning; scheduled and system are not here."""
        area, rows = self.services()
        self.assertEqual(area["error"], "")
        self.assertEqual(list(rows), ["svc:com.example.alpha", "svc:com.example.beta", "svc:com.example.gamma",
                                      f"svc:{runtime.LABEL}"])
        self.assertEqual([(row["state"], row["type"]) for row in rows.values()],
                         [("ok", "service"), ("caution", "service"), ("warning", "service"), ("ok", "dashboard service")])
        beta = rows["svc:com.example.beta"]
        self.assertEqual((beta["what"], beta["detail"], beta["facts"]["Label"], beta["revision"]),
                         ("beta · unloaded", "service is installed but not loaded", "com.example.beta", "2" * 64))
        self.assertEqual(beta["can"], {"start": {"allowed": True, "reason": ""},
                                       "restart": {"allowed": False, "reason": "service is already unloaded; use Start to load it"}})
        self.assertEqual(rows["svc:com.example.gamma"]["detail"], "last exit 78")
        self.assertEqual(rows["svc:com.example.alpha"]["facts"]["PID"], "101")

    def test_the_dashboard_row_is_stale_when_it_serves_another_commit_and_gives_the_kickstart_line(self):
        """sd:3707 with sd:3528: the row asks what `dashboard.sh health` asks, and its own service is copy only."""
        _, rows = self.services(served_commit="a" * 40)
        row = rows[f"svc:{runtime.LABEL}"]
        self.assertEqual((row["state"], row["what"]), ("caution", f"{runtime.LABEL.rsplit('.', 1)[-1]} · stale"))
        self.assertEqual(row["detail"], f"served {'a' * 40}, checkout {CHECKOUT}: "
                                        f"launchctl kickstart -k gui/{os.getuid()}/{runtime.LABEL}")
        self.assertEqual(row["kickstart"], f"launchctl kickstart -k gui/{os.getuid()}/{runtime.LABEL}")
        self.assertEqual(row["facts"]["Served commit"], "a" * 40)
        self.assertIn("protected", row["note"])
        # A process that names no commit is not current either.
        self.assertEqual(self.services(served_commit=None)[1][f"svc:{runtime.LABEL}"]["state"], "caution")

    def test_a_checkout_commit_that_cannot_be_read_is_unknown_not_current(self):
        with patch.object(runtime, "checkout_commit", side_effect=runtime.RuntimeRefused("git failed: index.lock")):
            row = self.services()[1][f"svc:{runtime.LABEL}"]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["detail"], "the checkout's commit was not read: git failed: index.lock")

    def test_a_service_read_that_fails_is_the_area_error_and_no_service_at_all_is_unknown(self):
        def stuck(connection, backend=None):
            raise RuntimeError("launchctl gave no answer: timed out")
        with patch.object(health_screen, "read_services", stuck):
            area = next(area for area in self.doc()["areas"] if area["id"] == "svc")
        self.assertEqual((area["error"], area["rows"]), ("launchctl gave no answer: timed out", []))
        _, rows = self.services(inventory={"services": [], "revision": "0" * 64})
        self.assertEqual([(row["id"], row["state"]) for row in rows.values()], [("svc:none", "unknown")])

    def test_a_launchctl_that_does_not_answer_is_a_reader_error_the_area_settles(self):
        class Stuck:
            def names(self):
                raise subprocess.TimeoutExpired("launchctl", 10)
        with self.assertRaisesRegex(RuntimeError, "launchctl gave no answer"):
            REAL_READ_SERVICES(self.connection, Stuck())

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
        self.assertEqual((build["state"], build["type"], build["what"]), ("caution", "build output", "2 merged worktrees keep build output"))
        # sd:3393: only a clean worktree's folders are offered; one with uncommitted changes is listed and kept.
        self.assertEqual(build["cli"], "rm -rf /work/wt-old/target\nrm -rf /work/wt-old/node_modules")
        self.assertEqual((build["remove"], build["kept"], build["revision"]),
                         (["/work/wt-old/target", "/work/wt-old/node_modules"], ["/work/wt-dirty"], "b" * 64))
        self.assertEqual(build["list"][1], "/work/wt-dirty · target/ · feat/wip · uncommitted changes: kept")
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
                         [(key, "", True) for key in ("disk", "cred", "wt", "br", "dep", "sec", "ports", "prot", "svc", "doc")])

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


class TheDoctor(Collectors, ScreenCase):
    """The Doctor area (sd:3393): `machine-setup.sh doctor` lines, read in a bounded child. A stub stands in for the script."""

    def area(self, doc):
        return next(area for area in doc["areas"] if area["id"] == "doc")

    def doc(self, doctor=None, now=NOW):
        return health_screen.document(self.connection, now=now, fleet=fleet_of(TREES), ports=ports_snapshot,
                                      protection=protection_of(PROTECTION), doctor=doctor)

    def script(self, body):
        path = Path(self.tmp.name) / "machine-setup.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def test_each_warn_and_fail_line_is_a_row_and_the_rest_are_one_ok_row(self):
        area = self.area(self.doc(doctor=lambda: DOCTOR))
        rows = [(row["state"], row["what"], row["kind"]) for row in area["rows"]]
        self.assertEqual(rows, [("warning", "Xcode CLT missing", "Doctor · FAIL"),
                                ("caution", "brew doctor has warnings", "Doctor · WARN"),
                                ("caution", "set here but not in shell-env.example: EXAMPLE_TOKEN", "Doctor · WARN"),
                                ("ok", "2 doctor checks pass", "Doctor")])
        self.assertEqual(area["rows"][2]["detail"], "set here but not in shell-env.example: EXAMPLE_TOKEN "
                                                    "add the name (value change-me) so a rebuild gets it")
        self.assertEqual(area["rows"][3]["detail"], "2 not checked here · 1 hard failure")
        self.assertIn("folders without a filled .env (.env.example present, .env absent):", area["rows"][3]["list"])
        self.assertEqual(area["error"], "")

    def test_the_doctor_runs_in_a_child_and_its_closing_line_and_exit_must_agree(self):
        clean = self.script("echo '== doctor'\necho '  ok      fnm (v24)'\necho\necho '  no hard failures'\n")
        found = REAL_DOCTOR_SCAN(script=clean, within=10)
        self.assertEqual((found["checks"], found["fails"]), ([{"mark": "ok", "text": "fnm (v24)", "more": []}], 0))
        for body, why in (
                ("echo '  ok      fnm'\n", "machine-setup.sh doctor exited 0 without its closing line: ok      fnm"),
                ("echo 'machine-setup: no config' >&2\nexit 1\n", "exited 1 without its closing line (machine-setup: no config)"),
                ("echo '  no hard failures'\nexit 2\n", "exited 2 without its closing line: no hard failures")):
            with self.subTest(body=body):
                area = self.area(self.doc(doctor=lambda body=body: REAL_DOCTOR_SCAN(script=self.script(body), within=10)))
                self.assertEqual(area["rows"], [])
                self.assertIn(why, area["error"])

    def test_a_doctor_past_its_deadline_is_stopped_and_unknown_not_clean(self):
        marker = Path(self.tmp.name) / "doctor-finished"
        slow = self.script(f"sleep 5 >/dev/null 2>&1\ntouch '{marker}'\necho '  no hard failures'\n")
        with patch.object(health_screen, "DOCTOR_WAIT_SECONDS", 10.0):
            area = self.area(self.doc(doctor=lambda: REAL_DOCTOR_SCAN(script=slow, within=0.5)))
        self.assertFalse(marker.exists(), "the page waited the doctor out instead of stopping it")
        self.assertEqual(area["rows"], [])
        self.assertRegex(area["error"], r"^machine-setup.sh doctor was stopped: .* and was stopped rather than waited on$")

    def test_a_doctor_still_running_is_unknown_and_the_page_does_not_wait_for_it(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def running():
            release.wait(10)
            return CLEAN_DOCTOR
        started = time.monotonic()
        with patch.object(health_screen, "DOCTOR_WAIT_SECONDS", 0.2):
            area = self.area(self.doc(doctor=running))
        self.assertLess(time.monotonic() - started, health_screen.PAGE_SECONDS)
        self.assertEqual(area["rows"], [])
        self.assertIn(f"machine-setup.sh doctor started at {NOW} and is still running", area["error"])

    def test_an_answer_stands_for_ten_minutes_and_a_failed_run_drops_it(self):
        runs, release = [], threading.Event()
        self.addCleanup(release.set)

        def counted():
            runs.append(1)
            if len(runs) == 2:
                raise ValueError("machine-setup.sh doctor exited 2 without its closing line")
            if len(runs) == 3:
                release.wait(10)
            return CLEAN_DOCTOR
        self.area(self.doc(doctor=counted))
        self.assertEqual(self.area(self.doc(doctor=counted, now="2026-09-06T12:09:00Z"))["at"], NOW)
        self.assertEqual(len(runs), 1, "a fresh answer started a second doctor")
        failed = self.area(self.doc(doctor=counted, now="2026-09-06T12:11:00Z"))
        self.assertEqual((len(runs), failed["rows"]), (2, []))
        self.assertIn("exited 2", failed["error"])
        # The next run is still going: the answer from before the failure must not stand in for it.
        with patch.object(health_screen, "DOCTOR_WAIT_SECONDS", 0.2):
            running = self.area(self.doc(doctor=counted, now="2026-09-06T12:12:00Z"))
        self.assertEqual((len(runs), running["rows"], running["stale"]), (3, [], ""))
        self.assertIn("is still running", running["error"])


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
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "snooze.js", "commands.js", "health.js", "shell.js"])
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
                 "merged branches", "port", "branch protection", "dependabot alerts", "secret scanning", "credential", "service",
                 "dashboard service")


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
                  + SNOOZE_JS + COMMANDS_JS + HEALTH_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
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
            ["build output.rm", "build output", "confirm", "r", True, False],
            ["volume.df", "volume", "safe", "b", False, False],
            ["worktree registrations.prune", "worktree registrations", "confirm", "p", True, False],
            ["merged branches.delete", "merged branches", "confirm", "d", True, False],
            ["service.restart", "service", "confirm", "r", True, False],
            ["service.start", "service", "confirm", "s", True, False],
            ["dashboard service.kickstart", "dashboard service", "safe", "l", False, False],
            ["port.inspect", "port", "safe", "i", False, False],
            ["protection.settings", "branch protection", "safe", "o", False, False],
            ["collector.sync", "collector", "safe", "r", None, False],
            ["dependabot alerts.review", "dependabot alerts", "safe", "o", False, False],
            ["secret scanning.review", "secret scanning", "safe", "o", False, False],
            ["credential.probe", "credential", "safe", "r", False, False],
        ] + [[f"{t}.snooze", t, "undo", "z", None, True] for t in SNOOZED_TYPES]
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
        self.assertIn("10 areas · 1 warning, 5 caution rows want you · 1 area with no reader yet", out["R"]["sub"])
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
        self.assertIn("Filtered to Credentials only: 1 of 22 rows.", out["R"]["note"])
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

    CLEANED = "ANSWER = (path, body) => path === '/api/health' ? [200, DOC] : path === '/api/health/cleanup' ? [STATUS_W, WROTE] : [404, { error: 'no answer' }];\n"

    def cleanup(self, row, body, status=200, answer=None):
        """Run a cleanup on `row` as the page does: the confirm, then one post of the row and its revision."""
        answer = answer if answer is not None else {"ok": True, "row": row, "done": ["x"], "refused": []}
        return self.run_page(f"var STATUS_W = {status}, WROTE = {json.dumps(answer)};\n" + self.CLEANED
                             + f"var o = C.get({json.dumps(row)}); OUT.gets = [];\n" + body)

    def test_prune_confirms_with_the_names_then_posts_the_row_and_its_revision(self):
        """sd:3393: the operator ruled that Health runs the prune, behind a confirm that names what goes."""
        out = self.cleanup("gone:group/alpha", """R.cli = cmd('worktree registrations.prune').cli(o);
R.why = cmd('worktree registrations.prune').consequence(o); R.rev = o.revision;
shellRun(cmd('worktree registrations.prune'), o); await flush();""",
                           answer={"ok": True, "row": "gone:group/alpha", "done": ["a"], "refused": [{"name": "b", "why": "locked"}]})
        self.assertEqual(out["R"]["cli"], "git -C '/checkouts/group/alpha' worktree prune -v")
        self.assertEqual(out["R"]["why"], "Removes 2 worktree registrations whose directories are gone: /work/wt-a (feat/a), "
                                          "/work/wt-b (fix/b). No directory is touched.")
        self.assertRegex(out["R"]["rev"], r"^[0-9a-f]{64}$")
        self.assertEqual(out["confirms"], ["worktree registrations.prune"])
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/health/cleanup", {"row": "gone:group/alpha", "revision": out["R"]["rev"]}]])
        self.assertEqual(out["gets"], ["/api/health"])
        self.assertEqual(out["toasts"][-1][0], "group/alpha: 2 worktrees registered, directory gone: 1 done · kept 1: b (locked)")

    SERVICE_ANSWER = ("ANSWER = (path, body) => path === '/api/health' ? [200, DOC] : path.startsWith('/api/services/') "
                      "? [STATUS_W, WROTE] : [404, { error: 'no answer' }];\n")
    ACCEPTED = {"request": {"id": "x", "action": "restart", "status": "accepted",
                            "message": "launchctl accepted the request; current state is observed separately"}, "service": {}}

    def service_page(self, row, body, status=200, answer=None):
        """sd:3707: the page over the Services fixture, its dashboard serving another commit, with `o` the row."""
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                     protection=protection_of(PROTECTION), services=SERVICES, served_commit="a" * 40)
        return self.run_page(f"var STATUS_W = {status}, WROTE = {json.dumps(answer or self.ACCEPTED)};\n" + self.SERVICE_ANSWER
                             + f"var o = C.get({json.dumps(row)}); OUT.gets = [];\n" + body, doc=doc)

    def test_restart_confirms_then_posts_the_sd_services_route_with_the_rows_revision(self):
        """sd:3707, ruling Q3: the control runs the code `sd services restart` runs, through the route Operations posts."""
        out = self.service_page("svc:com.example.alpha", """var c = cmd('service.restart');
R.cli = c.cli(o); R.why = c.consequence(o); R.on = c.when(o); R.primary = c.primary(o);
R.start = cmd('service.start').when(o);
shellRun(c, o); await flush();""")
        self.assertEqual(out["R"]["cli"], "sd services restart com.example.alpha")
        self.assertEqual(out["R"]["why"], "Stops com.example.alpha and starts it again through launchctl (bootout, bootstrap, "
                                          "kickstart). Its open connections drop.")
        self.assertEqual((out["R"]["on"], out["R"]["primary"]), (True, True))
        self.assertEqual(out["R"]["start"], "service is already running")
        self.assertEqual(out["confirms"], ["service.restart"])
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/services/com.example.alpha/restart", {"revision": "1" * 64}]])
        self.assertEqual(out["gets"], ["/api/health"])
        self.assertEqual(out["toasts"][-1][0], "com.example.alpha: accepted · launchctl accepted the request; "
                                               "current state is observed separately")

    def test_a_down_service_offers_start_and_says_why_restart_is_off(self):
        out = self.service_page("svc:com.example.beta", """R.start = cmd('service.start').when(o); R.primary = cmd('service.start').primary(o);
R.cli = cmd('service.start').cli(o); R.restart = cmd('service.restart').when(o);
R.old = cmd('service.start').when({ ...o, revision: undefined });
shellRun(cmd('service.start'), o); await flush();""")
        self.assertEqual((out["R"]["start"], out["R"]["primary"]), (True, True))
        self.assertEqual(out["R"]["cli"], "sd services start com.example.beta")
        self.assertEqual(out["R"]["restart"], "service is already unloaded; use Start to load it")
        self.assertEqual(out["R"]["old"], "the row carries no revision: read Health again")
        self.assertEqual([post[:2] for post in out["posts"]], [["/api/services/com.example.beta/start", {"revision": "2" * 64}]])

    def test_the_stale_dashboard_row_is_copy_only_with_the_kickstart_line(self):
        """Restarting the dashboard from its own page would end the request that asks; sd_db.services protects it too."""
        out = self.service_page(f"svc:{runtime.LABEL}", """var k = cmd('dashboard service.kickstart');
R.cli = k.cli(o); R.state = o.state; R.what = o.what;
shellRun(k, o); await flush();""")
        self.assertEqual(out["R"]["cli"], f"launchctl kickstart -k gui/{os.getuid()}/{runtime.LABEL}")
        self.assertEqual(out["R"]["state"], "caution")
        self.assertTrue(out["R"]["what"].endswith(" · stale"))
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["confirms"], [])

    def test_a_refused_service_write_says_not_changed_and_an_unknown_one_says_so(self):
        refused = self.service_page("svc:com.example.alpha", "shellRun(cmd('service.restart'), o); await flush();",
                                    status=409, answer={"error": "The service changed since the page read it.", "reload": True})
        self.assertIn("The service changed since the page read it.", refused["toasts"][-1][0])
        self.assertEqual(refused["gets"], ["/api/health"])
        unknown = self.service_page("svc:com.example.alpha", "shellRun(cmd('service.restart'), o); await flush();",
                                    status=502, answer={"error": "bad gateway"})
        self.assertRegex(unknown["toasts"][-1][0], r"^Not known what changed · com\.example\.alpha: ")
        self.assertEqual(unknown["gets"], ["/api/health"])

    def test_a_refused_cleanup_says_not_changed_and_an_unknown_one_says_so(self):
        stale = self.cleanup("gone:group/alpha", "shellRun(cmd('worktree registrations.prune'), o); await flush();",
                             status=409, answer={"error": "The registrations changed since the page read it.", "reload": True})
        self.assertEqual(stale["toasts"][-1][0], "group/alpha: 2 worktrees registered, directory gone not changed: "
                                                 "The registrations changed since the page read it.")
        self.assertEqual(stale["gets"], ["/api/health"])
        unknown = self.cleanup("build:merged", "shellRun(cmd('build output.rm'), o); await flush();",
                               status=502, answer={"error": "/work/wt-old/target was removed only in part", "outcome": "unknown"})
        self.assertRegex(unknown["toasts"][-1][0], r"^Not known what changed · 2 merged worktrees keep build output: "
                                                    r"/work/wt-old/target was removed only in part\. Health was read again")
        self.assertEqual(unknown["gets"], ["/api/health"])

    def test_a_row_without_a_revision_or_with_an_unreadable_registration_runs_nothing(self):
        out = self.cleanup("gone:group/alpha", """var p = cmd('worktree registrations.prune');
R.old = p.when({ ...o, revision: undefined }); R.unread = p.when({ ...o, unreadable: 1 });
R.dirty = cmd('build output.rm').when({ ...C.get('build:merged'), remove: [] });""")
        self.assertEqual(out["R"]["old"], "the row carries no revision: read Health again")
        self.assertEqual(out["R"]["unread"], "1 registration of this checkout could not be read, and prune would judge them itself")
        self.assertEqual(out["R"]["dirty"], "every merged worktree has uncommitted changes: nothing is removed")
        self.assertEqual(out["posts"], [])

    def test_snooze_posts_the_row_and_its_time_then_reads_health_again_and_undo_clears_it(self):
        """sd:1896: the server judges the time, so the page sends one it computed, and Undo writes the same key with none."""
        out = self.run_page("""ANSWER = (path, body) => path === '/api/health' ? [200, DOC] : [200, { key: 'k', until: body.until }];
var o = C.get('gone:group/alpha'), t0 = Date.now(); OUT.gets = [];
FIELD_VALUES.until = 'snooze-hour'; shellRun(cmd('worktree registrations.snooze'), o); await flush();
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
        self.assertEqual(out["toasts"][-1][0], "Snooze undone · group/alpha: 2 worktrees registered, directory gone")
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

    def test_disk_draws_a_bar_per_volume_and_remove_names_the_folders_it_deletes(self):
        out = self.cleanup("build:merged", """R.areas = ELS.areas.html;
R.rm = cmd('build output.rm').cli(o); R.why = cmd('build output.rm').consequence(o);
R.du = cmd('storage folder.du').cli(C.get('rs:/Volumes/store/repo-storage/alpha'));
shellRun(cmd('build output.rm'), o); await flush();""")
        areas = out["R"]["areas"]
        self.assertIn('<div class="bars" role="group" aria-label="Volume use: Mac data 85%, store 40%">', areas)
        self.assertIn('<span class="name" title="/Volumes/store">store</span>', areas)
        self.assertIn('<rect class="fill" x="0.5" y="0.5" width="85.0%" height="11" rx="2"/>', areas)
        self.assertIn('<span class="v">85% · 150.0 GiB free</span>', areas)
        self.assertEqual(out["R"]["rm"], "rm -rf /work/wt-old/target\nrm -rf /work/wt-old/node_modules")
        self.assertEqual(out["R"]["why"], "Deletes 2 build output folders in merged, clean worktrees: /work/wt-old/target, "
                                          "/work/wt-old/node_modules. Keeps 1 with uncommitted changes: /work/wt-dirty. "
                                          "Each worktree is checked again first. This cannot be undone.")
        self.assertEqual(out["R"]["du"], "du -sh /Volumes/store/repo-storage/alpha/* | sort -h | tail -5")
        self.assertEqual(out["confirms"], ["build output.rm"])
        self.assertEqual([post[:2] for post in out["posts"]], [["/api/health/cleanup", {"row": "build:merged", "revision": "b" * 64}]])

    def test_delete_merged_confirms_with_the_branch_names_and_a_checked_out_branch_says_why_git_refuses(self):
        out = self.cleanup("br:/checkouts/alpha", """var held = C.get('brs:/checkouts/alpha');
R.cli = cmd('merged branches.delete').cli(o); R.why = cmd('merged branches.delete').consequence(o);
R.on = cmd('merged branches.delete').when(o); R.held = cmd('merged branches.delete').when(held);
shellRun(cmd('merged branches.delete'), o); await flush();""")
        self.assertEqual(out["R"]["cli"], "git -C /checkouts/alpha branch -d feat/one feat/two fix/three")
        self.assertEqual(out["R"]["why"], "Deletes 3 local branches already in origin's default branch: feat/one, feat/two, "
                                          "fix/three. git branch -d refuses any unmerged one.")
        self.assertIs(out["R"]["on"], True)
        self.assertEqual(out["R"]["held"], "checked out in a worktree: git branch -d refuses it until the worktree is removed")
        self.assertEqual(out["confirms"], ["merged branches.delete"])
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/health/cleanup", {"row": "br:/checkouts/alpha", "revision": "a" * 64}]])
        self.assertEqual(out["toasts"][-1][0], "alpha: 3 merged branches not deleted: 1 done")

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
    page, section, route, api = "health", "Health", "/fleet-health", ("/api/health", "/api/health/cleanup")


if __name__ == "__main__":
    unittest.main()
