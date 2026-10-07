"""The v2 Management page (sd:2118).

What this slice promises: `/management` answers under the shared policy and
loads its script before `shell.js`; `/api/management` is one document built
from the reads v1 already makes (the sd-db repo table with each checkout's
`.github/sd-review.json` and protection reading, the fleet's git state and
sessions, the runner lane, services and jobs), where a source that fails is
a reason and never an empty list; `/api/repos/<verb>` runs the two sd-db repo
verbs and refuses a stale `before`.

`management.js` runs under JavaScriptCore (osascript, as `test_v2_tasks`
runs `tasks.js`) against the same stand-in page and shell, with the document
above as its fetch answer. The browser half -- focus, the look at 375 px --
is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner, upsert_repo, workflow
from sd_dashboard import management_screen, server, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document, tree
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_read import READ_SHELL

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "management.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")


def git_row(name, *, behind=0, dirty=0, ahead=0, branch="main"):
    """A fleet repos row with every key `repos_screen.primary` reads."""
    return {"name": name, "group": ".", "path": f"/repos/{name}", "branch": branch, "default": "main", "dirty": dirty,
            "ahead": ahead, "behind": behind, "behind_default": behind, "fetched_iso": NOW.replace("Z", "+00:00"),
            "last": "2026-09-01", "last_iso": "2026-09-01T10:00:00+00:00", "subject": "a commit", "author": "someone",
            "web": "", "truncated": [], "error": ""}


def fleet(area):
    if area == "repos":
        return {"root": "/repos", "rootExists": True, "repos": [git_row("system", behind=2), git_row("busy", behind=1, dirty=3)],
                "counts": {"repos": 2, "dirty": 1, "ahead": 0, "unread": 0}}
    return fleet_document(area, trees=[tree("gone", live=False), tree("here")])


def dark_fleet(area):
    raise ValueError("fleet collection was stopped at its budget")


def runner_status(body, code=0, err=""):
    """A `runner.sh status` stand-in: its exit code, its one-line body and its stderr."""
    return lambda: (code, "" if body is None else json.dumps(body) + "\n", err)


SCHEDULE = {"last_completed_at": "2026-09-06T03:00:00+00:00", "next_due_at": "2026-09-07T03:00:00+00:00", "due": False,
            "cadence_seconds": 86400}
REFRESHED = runner_status({"ok": True, "interval_seconds": 10, "archive_refresh_schedule": SCHEDULE})
#: `sd config get sd.assistant_merge` stand-in: exit code, stdout, stderr (sd:1629).
GRANTED = lambda: (0, "controlled\n", "")  # noqa: E731
UNSET = (1, "", "sd: sd.assistant_merge is not set (controlled: merge ...). Run:\n    sd config set sd.assistant_merge <value>\n")


def stamp(backend, name, text):
    """The cron-jobs wrapper's `logs/.<job>.stamp` for `name` (sd:2210)."""
    (backend.cron_root / "logs" / f".{name}.stamp").write_text(text)


class NoServices:
    """A services backend double that lists nothing, so no launchctl call is made."""

    def names(self):
        return []


def seed(case):
    """A registered checkout with an sd-review.json, a blocked author run and a finished merge."""
    checkout = Path(case.tmp.name).resolve() / "checkout"
    (checkout / ".github").mkdir(parents=True)
    (checkout / ".github" / "sd-review.json").write_text(
        '{\n  "severity_floor": "high",\n  "copilot_review": {"automatic_deep": false}\n}\n')
    upsert_repo(case.connection, str(checkout), remote="git@example.invalid:example/checkout.git")
    case.repo()
    item = case.item("Port the page", repo="/repos/system")
    blocked = case.assignment(item, status="blocked")
    merged = case.assignment(item, role="merge", status="done")
    return {"checkout": str(checkout), "item": item, "blocked": blocked, "merged": merged}


class TheDocument(ScreenCase):
    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None), ("quiet", "idle", 0, None)])

    def document(self, read=fleet, runner=REFRESHED, grant=GRANTED):
        return management_screen.document(self.connection, now=NOW, fleet=read, jobs=self.jobs, services=NoServices(),
                                          runner=runner, grant=grant)

    def test_every_source_is_read_and_says_so(self):
        doc = self.document()
        self.assertEqual(doc["sources"], {k: "" for k in ("repos", "git", "lane", "assignments", "sessions", "services", "jobs",
                                                          "archive", "grant")})
        self.assertEqual(doc["read"], NOW)

    def test_a_job_carries_its_last_run_from_the_wrappers_stamp(self):
        """sd:2210: launchd keeps no run time; the cron-jobs wrapper stamps each run's start, end and exit."""
        stamp(self.jobs, "nightly-sync", "started=2026-09-06T02:15:00Z\nended=2026-09-06T02:16:30Z\nexit=7\n")
        jobs = {job["name"]: job for job in self.document()["jobs"]}
        self.assertEqual(jobs["nightly-sync"]["last_run"], {"state": "finished", "started": "2026-09-06T02:15:00Z",
                                                            "ended": "2026-09-06T02:16:30Z", "exit": 7, "reason": ""})
        quiet = jobs["quiet"]["last_run"]
        self.assertEqual((quiet["state"], quiet["started"]), ("none", None))
        self.assertIn("no run stamp", quiet["reason"])

    def test_a_stamp_with_no_end_is_open_and_a_broken_one_is_unread_with_its_reason(self):
        stamp(self.jobs, "nightly-sync", "started=2026-09-06T02:15:00Z\n")
        stamp(self.jobs, "quiet", "started=yesterday\nended=2026-09-06T02:16:30Z\nexit=0\n")
        jobs = {job["name"]: job["last_run"] for job in self.document()["jobs"]}
        self.assertEqual(jobs["nightly-sync"], {"state": "open", "started": "2026-09-06T02:15:00Z", "ended": None, "exit": None,
                                                "reason": ""})
        self.assertEqual((jobs["quiet"]["state"], jobs["quiet"]["reason"]), ("unread", "the run stamp names no start time"))
        stamp(self.jobs, "quiet", "started=2026-09-06T02:15:00Z\nended=2026-09-06T02:16:30Z\n")
        quiet = {job["name"]: job["last_run"] for job in self.document()["jobs"]}["quiet"]
        self.assertEqual((quiet["state"], quiet["reason"]), ("unread", "the run stamp has an end without an exit code"))

    def test_the_archive_refresh_is_what_runner_status_prints(self):
        """sd:2209: the dashboard does not know the runner's config; `runner.sh status` names the last and next refresh."""
        self.assertEqual(self.document()["archive"], {"last": "2026-09-06T03:00:00+00:00", "next": "2026-09-07T03:00:00+00:00",
                                                      "due": False})
        never = runner_status({"ok": False, "reason": "heartbeat stale",
                               "archive_refresh_schedule": {**SCHEDULE, "last_completed_at": None, "due": True}}, code=1)
        self.assertEqual(self.document(runner=never)["archive"], {"last": None, "next": "2026-09-07T03:00:00+00:00", "due": True})

    def test_an_archive_refresh_runner_status_does_not_name_is_a_reason(self):
        cases = {
            "runner.sh status names no archive refresh: runner runtime is not provisioned: /x/python":
                runner_status({"ok": False, "reason": "runner runtime is not provisioned: /x/python"}, code=3),
            "runner.sh status names no archive refresh; the runner predates sd:2209": runner_status({"ok": True}),
            "the archive refresh was not read: archive refresh cadence belongs to another configuration":
                runner_status({"ok": True, "archive_refresh_schedule": {"reason": "archive refresh cadence belongs to another configuration"}}),
            "runner.sh status printed no body: runner: the runner heartbeat body is a JSON array":
                runner_status(None, code=1, err="runner: the runner heartbeat body is a JSON array\n"),
            "runner.sh status exited 2: usage": runner_status(None, code=2, err="usage\n"),
            "runner.sh status printed a body that is not JSON": lambda: (0, "{not json\n", ""),
            "runner.sh status named a next refresh that is not a time": runner_status(
                {"ok": True, "archive_refresh_schedule": {**SCHEDULE, "next_due_at": "soon"}}),
        }
        for reason, runner in cases.items():
            with self.subTest(reason=reason):
                doc = self.document(runner=runner)
                self.assertIsNone(doc["archive"])
                self.assertEqual(doc["sources"]["archive"], reason)
                self.assertEqual(doc["sources"]["jobs"], "")

    def test_runner_status_runs_this_checkouts_runner_sh_within_its_ceiling(self):
        calls = []

        def ran(argv, **options):
            calls.append((argv, options.get("timeout")))
            return subprocess.CompletedProcess(argv, 3, '{"ok": false}\n', "")

        with patch.object(management_screen.subprocess, "run", ran):
            self.assertEqual(management_screen.runner_status(), (3, '{"ok": false}\n', ""))
        runner_sh = Path(management_screen.__file__).resolve().parents[2] / "local-sd-runner" / "runner.sh"
        self.assertTrue(runner_sh.is_file())
        self.assertEqual(calls, [(["sh", str(runner_sh), "status"], management_screen.RUNNER_SECONDS)])

        def slow(argv, **options):
            raise subprocess.TimeoutExpired(argv, options["timeout"])

        with patch.object(management_screen.subprocess, "run", slow):
            doc = self.document(runner=management_screen.runner_status)
        self.assertEqual(doc["sources"]["archive"], f"runner.sh status ran past its {management_screen.RUNNER_SECONDS:g} seconds")

    def test_a_repo_carries_its_row_its_review_file_and_its_protection_reading(self):
        rows = {row["path"]: row for row in self.document()["repos"]}
        checkout = rows[self.ids["checkout"]]
        self.assertEqual((checkout["runner_merge"], checkout["managed"]), ("manual", "no"))
        self.assertEqual((checkout["review"]["severity_floor"], checkout["review"]["automatic_deep"]), ("high", False))
        self.assertIn('"severity_floor": "high"', checkout["review"]["file"])
        self.assertEqual(checkout["protection"]["status"], "unknown")
        # A checkout not on disk has no review reading at all, not an absent file.
        self.assertIsNone(rows["/repos/system"]["review"])

    def test_a_checkout_borrowing_a_siblings_protection_row_names_the_lender(self):
        """sd:1607: the GitHub panel says which checkout's reading it shows."""
        upsert_repo(self.connection, "/repos/widget", remote="git@github.com:example/widget.git")
        upsert_repo(self.connection, "/repos/widget-copy", remote="git@github.com:example/widget.git")
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES ('/repos/widget', '2026-10-04T01:00:00Z', 'protected', 'main', '{}')")
        self.connection.commit()
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual(rows["/repos/widget-copy"]["protection"]["status"], "protected")
        self.assertEqual(rows["/repos/widget-copy"]["protection"]["borrowed_from"], "/repos/widget")
        self.assertIsNone(rows["/repos/widget"]["protection"]["borrowed_from"])

    def test_a_protection_row_from_an_older_library_reads_as_the_checkouts_own(self):
        """An installed `sd_db` from before sd:1607 returns rows with no `borrowed_from`."""
        from sd_db import protection
        older = [{key: value for key, value in row.items() if key != "borrowed_from"}
                 for row in protection.rows(self.connection)]
        with patch.object(protection, "rows", lambda connection: older):
            doc = self.document()
        self.assertIsNotNone(doc["repos"], doc["sources"])
        rows = {row["path"]: row for row in doc["repos"]}
        self.assertIsNone(rows[self.ids["checkout"]]["protection"]["borrowed_from"])

    def test_git_state_is_primarys_reading_with_its_remedy_or_refusal(self):
        git = {row["name"]: row for row in self.document()["git"]["repos"]}
        self.assertEqual((git["system"]["state"], git["system"]["remedy"]), ("behind", "git -C /repos/system pull --ff-only"))
        self.assertEqual(git["busy"]["remedy"], "No pull offered: 3 uncommitted files.")

    def test_assignments_carry_the_queue_revision_and_the_history_counts(self):
        doc = self.document()
        (blocked,) = doc["assignments"]["latest"]
        self.assertEqual((blocked["id"], blocked["status"]), (self.ids["blocked"], "blocked"))
        self.assertEqual(blocked["revision"], runner.queue_state(self.connection, self.ids["blocked"])["revision"])
        self.assertEqual([m["id"] for m in doc["lane"]["merges"]], [self.ids["merged"]])
        self.assertEqual(doc["assignments"]["history"], {"blocked": 1, "done": 1})
        self.assertEqual((doc["sessions"]["registered"], doc["sessions"]["abandoned"]), (2, 1))
        jobs = {job["name"]: job for job in doc["jobs"]}
        self.assertEqual((jobs["nightly-sync"]["state"], jobs["nightly-sync"]["capabilities"]["retry"]["allowed"]), ("failed", True))

    def test_a_source_that_fails_is_a_reason_and_the_others_still_answer(self):
        doc = self.document(read=dark_fleet)
        self.assertIsNone(doc["git"])
        self.assertIsNone(doc["sessions"])
        self.assertEqual(doc["sources"]["git"], "fleet collection was stopped at its budget")
        self.assertEqual(doc["sources"]["repos"], "")
        self.assertTrue(doc["repos"])

    def test_the_repo_verb_writes_and_refuses_a_stale_before(self):
        got = management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        self.assertEqual((got["value"], got["before"]), ("auto", "manual"))
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        management_screen.set_repo(self.connection, "managed", self.ids["checkout"], "yes", "no")
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual((rows[self.ids["checkout"]]["runner_merge"], rows[self.ids["checkout"]]["managed"]), ("auto", "yes"))

    def test_a_repo_carries_its_required_checks_and_its_runtime_pins(self):
        """sd:1629: the overview's columns. Required checks are the protection reading's; runtimes are the checkout's pins."""
        checkout = Path(self.ids["checkout"])
        (checkout / ".python-version").write_text("3.14\n")
        (checkout / "package.json").write_text('{"engines": {"node": ">=22"}}')
        body = {"gaps": [], "detail": {"required_contexts": ["ci", "sd/local-gate"], "strict": True}}
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES (?, '2026-10-04T01:00:00Z', 'protected', 'main', ?)", (str(checkout), json.dumps(body)))
        self.connection.commit()
        rows = {row["path"]: row for row in self.document()["repos"]}
        row = rows[str(checkout)]
        self.assertEqual((row["protection"]["required"], row["protection"]["strict"]), (["ci", "sd/local-gate"], True))
        self.assertEqual(row["runtimes"], {"python": {"value": "3.14", "source": ".python-version"},
                                           "node": {"value": ">=22", "source": "package.json engines.node"}})
        # A checkout not on disk has no runtime reading, and no protection observation has no required list.
        self.assertIsNone(rows["/repos/system"]["runtimes"])
        self.assertIsNone(rows["/repos/system"]["protection"]["required"])

    def test_a_runtime_pin_comes_from_the_first_file_that_names_one(self):
        cases = (
            ({".python-version": "3.12\n", ".tool-versions": "python 3.13.1\n"}, {"value": "3.12", "source": ".python-version"}, None),
            ({".tool-versions": "nodejs 22.1.0\npython 3.13.1\n", "pyproject.toml": '[project]\nrequires-python = ">=3.11"\n'},
             {"value": "3.13.1", "source": ".tool-versions"}, {"value": "22.1.0", "source": ".tool-versions"}),
            ({"pyproject.toml": '[project]\nrequires-python = ">=3.13"\n', ".nvmrc": "v20\n"},
             {"value": ">=3.13", "source": "pyproject.toml requires-python"}, {"value": "v20", "source": ".nvmrc"}),
            ({".node-version": "\n", "README.md": "python 3.9"}, None, None),
            ({"package.json": "{not json", "pyproject.toml": "[project"},
             {"error": "pyproject.toml is not valid TOML"}, {"error": "package.json is not valid JSON"}),
        )
        for number, (files, python, node) in enumerate(cases):
            with self.subTest(files=sorted(files)):
                root = Path(self.tmp.name) / f"pins-{number}"
                root.mkdir()
                for name, text in files.items():
                    (root / name).write_text(text)
                self.assertEqual(management_screen._runtimes(str(root)), {"python": python, "node": node})

    def test_the_machine_merge_grant_is_what_sd_config_answers(self):
        """sd:1629: sd.assistant_merge is machine-wide, so the document carries it once; unset is a reading, not a failure."""
        self.assertEqual(self.document()["grant"], {"assistant_merge": "controlled"})
        doc = self.document(grant=lambda: UNSET)
        self.assertEqual((doc["grant"], doc["sources"]["grant"]), ({"assistant_merge": None}, ""))
        for answer, reason in (((2, "", "sd: invalid sd.assistant_merge policy\n"),
                                "sd config get sd.assistant_merge exited 2: sd: invalid sd.assistant_merge policy"),
                               ((0, "sometimes\n", ""), "sd config get sd.assistant_merge printed sometimes, not controlled or ask")):
            with self.subTest(reason=reason):
                doc = self.document(grant=lambda answer=answer: answer)
                self.assertIsNone(doc["grant"])
                self.assertEqual(doc["sources"]["grant"], reason)
                self.assertEqual(doc["sources"]["repos"], "")

    def test_assistant_merge_runs_sd_config_get_within_its_ceiling(self):
        calls = []

        def ran(argv, **options):
            calls.append((argv, options.get("timeout")))
            return subprocess.CompletedProcess(argv, 0, "ask\n", "")

        with patch.object(management_screen.subprocess, "run", ran):
            self.assertEqual(management_screen.assistant_merge(), (0, "ask\n", ""))
        self.assertEqual(calls, [(["sd", "config", "get", "sd.assistant_merge"], management_screen.SD_SECONDS)])

        def missing(argv, **options):
            raise FileNotFoundError(argv[0])

        with patch.object(management_screen.subprocess, "run", missing):
            doc = self.document(grant=management_screen.assistant_merge)
        self.assertEqual(doc["sources"]["grant"], "sd is not on the dashboard's PATH, so the machine merge grant was not read")

    def test_a_satellite_refuses_the_repo_verb_and_writes_nothing(self):
        """sd:1629: the repo settings change on the hub; a satellite's dashboard refuses before it reads or writes the row."""
        from sd_db.remote import HubOnly

        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly) as refused:
                management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        self.assertIn("repo runner-merge runs on the sd hub only", str(refused.exception))
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual(rows[self.ids["checkout"]]["runner_merge"], "manual")

    def test_the_stale_check_and_the_write_hold_one_write_transaction(self):
        """Copilot on PR #50: two requests could read the same old value, and the late one overwrote.

        While the verb reads the row, a second connection must not be able to
        take the write lock; before the fix it could, and wrote in between.
        """
        import sqlite3

        from sd_db import connect, repos

        other = connect(self.path, busy_timeout=0)
        self.addCleanup(other.close)
        seen = []
        read = repos.row_for

        def racing(connection, path):
            row = read(connection, path)
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as locked:
                seen.append(str(locked))
            else:
                other.execute("UPDATE repo SET runner_merge = 'auto' WHERE path = ?", (row["path"],))
                other.execute("COMMIT")
                seen.append("the other writer got in")
            return row

        with patch.object(repos, "row_for", racing):
            management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        self.assertEqual(seen, ["database is locked"])


class ThePage(BrowserSession):
    fleet_backend = staticmethod(fleet)

    def setUp(self):
        patcher = patch("sd_db.services.ServiceBackend", NoServices)
        patcher.start()
        self.addCleanup(patcher.stop)
        runner = patch.object(management_screen, "runner_status", REFRESHED)
        runner.start()
        self.addCleanup(runner.stop)
        grant = patch.object(management_screen, "assistant_merge", GRANTED)
        grant.start()
        self.addCleanup(grant.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = JobsBackend(self.tmp.name, jobs=[("quiet", "idle", 0, None)])
        super().setUp()
        self.ids = seed(self)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/management")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Management · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "management.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/management")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/management?view=lane", headers=cookie)[0], 400)
        status, _, body = self.request("/api/management", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["sources"]["services"], "")
        self.assertEqual((doc["sources"]["archive"], doc["archive"]["next"]), ("", SCHEDULE["next_due_at"]))
        self.assertIn(self.ids["checkout"], [row["path"] for row in doc["repos"]])

    def test_the_repo_write_flips_once_and_refuses_the_same_before_again(self):
        body = {"path": self.ids["checkout"], "value": "auto", "before": "manual"}
        status, _, got = self.post("/api/repos/runner-merge", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "auto", "manual"))
        self.assertEqual(self.post("/api/repos/runner-merge", body)[0], 409)
        self.assertEqual(self.post("/api/repos/runner-merge", {**body, "value": "sometimes"})[0], 400)
        self.assertEqual(self.post("/api/repos/managed", {"path": self.ids["checkout"], "value": "yes", "before": "yes"})[0], 400)
        self.assertEqual(self.post("/api/repos/mode", {"path": self.ids["checkout"], "value": "x", "before": "y"})[0], 404)

    def test_the_repo_write_is_refused_on_a_satellite(self):
        """sd:1629: the POST passes the session and CSRF checks, then the verb refuses: a 400 naming the hub, no change."""
        body = {"path": self.ids["checkout"], "value": "yes", "before": "no"}
        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            status, _, got = self.post("/api/repos/managed", body)
        self.assertEqual(status, 400)
        self.assertIn("runs on the sd hub only", got["error"])
        status, _, got = self.post("/api/repos/managed", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "yes", "no"))


# The Management stand-in adds what the page reads beyond Tasks': a <main> to listen on, CSS.escape, and the shell's
# row and context calls.
EXTRA = r"""
var CSS = { escape: s => s };
var MAIN = El('main');
var baseQuery = document.querySelector;
document.querySelector = sel => sel === 'main' ? MAIN : baseQuery(sel);
// A sub-view lamp's value is a child the page fills: each element answers querySelector with one child per selector.
var baseById = document.getElementById;
document.getElementById = id => { var e = baseById.call(document, id);
  if (!e.kids) { e.kids = {}; e.querySelector = sel => { if (!e.kids[sel]) { e.kids[sel] = El(id + ' ' + sel);
    e.kids[sel].after = f => { e.html = (e.html || '') + (f.html || ''); }; } return e.kids[sel]; }; } return e; };
"""
SHELL_EXTRA = r"""
var ROW = null;
window.shell.row = (...a) => a.length ? (ROW = a[0]) : ROW;
window.shell.setContext = () => {};
OUT.urls = []; OUT.qs = [];
window.shell.url = p => { OUT.urls.push({ view: p.get('view'), p: p.get('page'), q: p.get('q') }); OUT.qs.push(p.toString()); };
"""


class PageScript(ScreenCase):
    """management.js against the document `management_screen` builds for the seeded database."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None), ("quiet", "idle", 0, None)])
        stamp(jobs, "nightly-sync", "started=2026-09-06T02:15:00Z\nended=2026-09-06T02:16:30Z\nexit=7\n")
        self.doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=jobs, services=NoServices(),
                                              runner=REFRESHED, grant=GRANTED)

    def run_page(self, body, answer="null", doc=None, search="", extra=""):
        script = (STAND_IN + EXTRA + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_EXTRA + READ_SHELL + extra
                  + f"\nconst DOC0 = {json.dumps(doc or self.doc)};\n"
                  + "const WRITE = " + answer + ";\n"
                  + f"location.search = {json.dumps(search)};\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/management') return [200, DOC0];
  return WRITE ? WRITE(path, body) : [200, {}];
};\n""" + PAGE_JS + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out


class TheScript(PageScript):
    def test_every_command_is_registered_with_its_risk_key_and_run_path(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null,
  typeof c.executes === 'boolean' ? c.executes : null, typeof c.undo === 'function']);""")
        self.assertEqual(out["R"]["reg"], [
            ["repo.runner-merge", "repo", "undo", "m", True, True],
            ["repo.managed", "repo", "undo", "g", True, True],
            ["repo.pull", "repo", "safe", "l", False, False],
            ["sddb.run", "sd-db change", "undo", "u", True, True],
            ["file.prepare", "file change", "safe", "p", False, False],
            ["sddb.withdraw", "sd-db change", "safe", None, False, False],
            ["file.withdraw", "file change", "safe", None, False, False],
            ["asg.requeue", "assignment", "undo", "q", None, True],
            ["asg.cancel", "assignment", "confirm", "x", None, False],
            ["asg.get", "assignment", "safe", "o", False, False],
            ["item.show", "item", "safe", "o", False, False],
            ["wt.prune", "worktrees", "confirm", "p", None, False],
            ["svc.restart", "service", "confirm", "t", None, False],
            ["svc.stop", "service", "confirm", "s", None, False],
            ["svc.start", "service", "safe", "a", None, False],
            ["jobs.retry", "job", "safe", "t", None, False],
            ["job.print", "job", "safe", "l", False, False],
        ])

    def test_the_page_reads_the_document_and_draws_repos_with_git_state(self):
        out = self.run_page("R.repos = ELS['view-repos'].html; R.sched = ELS['view-schedules'].html;")
        self.assertEqual(out["gets"], ["/api/management"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        repos = out["R"]["repos"]
        self.assertIn("2 registered", repos)
        self.assertIn("+0/−2", repos)
        self.assertIn("busy", repos)  # a checkout sd-db does not list joins the list
        self.assertIn("not registered", repos)
        self.assertIn("nightly-sync", out["R"]["sched"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "scheduled jobs failed"})

    def test_only_the_sorted_header_carries_aria_sort_and_a_click_moves_it(self):
        """sd:2527: each column with an order sorts through its button; aria-sort is on the sorted th alone."""
        click = "ELS['view-repos'].listeners.click[0]({ target: { closest: s => s === 'button[data-sort]' ? { dataset: { sort: 'managed' } } : null } });"
        sorted_ = r'aria-sort="(\w+)"[^>]*><button class="sorter"[^>]*data-sort="(\w+)"'
        out = self.run_page(f"R.a = ELS['view-repos'].html; {click} R.b = ELS['view-repos'].html; {click} R.c = ELS['view-repos'].html;")
        for key, want in (("a", [("ascending", "name")]), ("b", [("ascending", "managed")]), ("c", [("descending", "managed")])):
            self.assertEqual(re.findall(sorted_, out["R"][key]), want, key)
            self.assertEqual(out["R"][key].count("aria-sort"), 1, key)

    def jobs(self, n):
        """The document with n more launchd jobs, idle, named job-00 onward."""
        quiet = next(j for j in self.doc["jobs"] if j["name"] == "quiet")
        return dict(self.doc, jobs=self.doc["jobs"] + [dict(quiet, name=f"job-{i:02d}") for i in range(n)])

    def test_the_pager_is_the_shells_and_keeps_page_and_size_in_the_url(self):
        """sd:2682: shell.list's pager, with sizes 25 to 200; ?page= and ?size= in the URL, and an older ?p=/?n= link still opens."""
        click = "ELS['view-schedules'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
        out = self.run_page("R.a = ELS['view-schedules'].html;" + click % ("'.list-pager [data-page]'", "{ page: '2' }")
                            + " R.b = ELS['view-schedules'].html;" + click % ("'.list-pager [data-size]'", "{ size: '50' }")
                            + " R.c = ELS['view-schedules'].html;", doc=self.jobs(30), search="?view=schedules")
        self.assertIn('<span class="range">1–25 of 32</span>', out["R"]["a"])
        self.assertEqual(re.findall(r'data-size="(\d+)"', out["R"]["a"]), ["25", "50", "100", "200"])
        self.assertIn('<span class="range">26–32 of 32</span>', out["R"]["b"])
        self.assertIn("view=schedules&page=2", out["qs"])
        self.assertIn('<span class="range">1–32 of 32</span>', out["R"]["c"])
        self.assertEqual(out["qs"][-1], "view=schedules&size=50")
        out = self.run_page("R.a = ELS['view-schedules'].html;", doc=self.jobs(30), search="?view=schedules&p=2&n=50")
        self.assertIn('<span class="range">1–32 of 32</span>', out["R"]["a"])
        self.assertEqual(out["qs"][-1], "view=schedules&size=50")

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        """sd:2682: Repos' managed, merge and text filters as shell.list chips above the list."""
        click = "document.querySelector('main').listeners.click.forEach(f => f({ target: { closest: s => s === %s ? { dataset: %s } : null } }));"
        out = self.run_page("R.a = ELS['view-repos'].html;" + click % ("'[data-unfilter]'", "{ unfilter: 'managed' }")
                            + " R.b = ELS['view-repos'].html;" + click % ("'[data-unfilter-all]'", "{}")
                            + " R.c = ELS['view-repos'].html; R.box = ELS.shift.value;", search="?view=repos&managed=yes&q=busy")
        chips = lambda key: re.findall(r'data-unfilter="(\w+)"', out["R"][key])
        self.assertEqual(chips("a"), ["managed", "q"])
        self.assertIn("2 filters · ", out["R"]["a"])
        self.assertEqual(chips("b"), ["q"])
        self.assertEqual(out["qs"][-2], "view=repos&q=busy")
        self.assertEqual((chips("c"), out["qs"][-1], out["R"]["box"]), ([], "view=repos", ""))

    def settings_doc(self):
        """The document with the seeded checkout managed, protected with two required checks, and pinned to Python 3.14."""
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        row["managed"] = "yes"
        row["protection"] = dict(row["protection"], status="protected", required=["ci", "sd/local-gate"], strict=True)
        row["runtimes"] = {"python": {"value": "3.14", "source": ".python-version"}, "node": None}
        return doc

    def test_the_settings_columns_condense_each_managed_repo_to_one_row(self):
        """sd:1629: ?cols=settings swaps the git columns for the settings; floor and Copilot are read, never edited, here."""
        out = self.run_page("R.repos = ELS['view-repos'].html;", doc=self.settings_doc(), search="?view=repos&cols=settings&managed=yes")
        repos = out["R"]["repos"]
        self.assertEqual(re.findall(r'<tr data-id="repo:([^"]+)"', repos), [self.ids["checkout"]])
        cells = dict(re.findall(r'data-k="(\w+)">(.*?)</td>', repos))
        self.assertEqual(set(cells), {"managed", "merge", "floor", "copilot", "checks", "python", "node"})
        self.assertEqual((cells["managed"], cells["merge"], cells["floor"], cells["copilot"]), ("yes", "manual", "high", "false"))
        self.assertEqual(re.findall(r"<code>([^<]+)</code>", cells["checks"]), ["ci", "sd/local-gate"])
        self.assertIn("3.14", cells["python"])
        self.assertIn("not pinned", cells["node"])
        self.assertIn("machine merge grant <b>controlled</b>", repos)
        self.assertIn("change through a pull request", repos)
        self.assertNotIn("data-set=", repos)
        self.assertNotIn("<select", repos)

    def test_the_column_set_is_a_control_kept_in_the_url(self):
        click = ("ELS['view-repos'].listeners.click[0]({ target: { closest: s => s === '[data-cols]' ? { dataset: { cols: '%s' } } : null } });"
                 " R.%s = ELS['view-repos'].html;")
        out = self.run_page(click % ("settings", "a") + click % ("git", "b"), doc=self.settings_doc())
        self.assertIn('data-k="floor"', out["R"]["a"])
        self.assertIn('data-cols="settings" aria-pressed="true"', out["R"]["a"])
        self.assertIn('data-k="branch"', out["R"]["b"])
        self.assertNotIn('data-k="floor"', out["R"]["b"])
        self.assertEqual(out["qs"][-2:], ["view=repos&cols=settings", "view=repos"])

    def test_a_grant_or_a_reading_that_failed_is_unknown_with_its_reason(self):
        doc = self.settings_doc()
        doc["grant"] = None
        doc["sources"]["grant"] = "sd is not on the dashboard's PATH"
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        row["protection"] = dict(row["protection"], status="unknown", reason="not yet observed", required=None)
        row["runtimes"] = {"python": {"error": "pyproject.toml is not valid TOML"}, "node": None}
        out = self.run_page("R.repos = ELS['view-repos'].html;", doc=doc, search="?view=repos&cols=settings&managed=yes")
        repos = out["R"]["repos"]
        self.assertIn("machine merge grant not read: sd is not on the dashboard&#39;s PATH", repos)
        cells = dict(re.findall(r'data-k="(\w+)">(.*?)</td>', repos))
        self.assertIn("▨", cells["checks"])
        self.assertIn("not yet observed", cells["checks"])
        self.assertIn("pyproject.toml is not valid TOML", cells["python"])

    def test_a_source_that_failed_renders_unknown_with_its_reason(self):
        doc = dict(self.doc, git=None, sessions=None, sources=dict(self.doc["sources"], git="stopped at its budget", sessions="stopped at its budget"))
        out = self.run_page("R.repos = ELS['view-repos'].html; R.sess = ELS['view-sessions'].html;", doc=doc)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("git, sessions", out["states"][-1]["text"])
        self.assertIn("git state not read: stopped at its budget", out["R"]["repos"])
        self.assertIn("Sessions were not read", out["R"]["sess"])

    def test_a_repo_flip_posts_the_verb_with_before_toasts_after_the_write_and_undoes(self):
        path = self.ids["checkout"]
        out = self.run_page(f"""C.run(cmd('repo.runner-merge'), C.get('repo:{path}')); R.early = OUT.toasts.length; await flush();
R.toast = lastToast().msg; const undo = lastToast().undo; await undo(); await flush(); await undo(); await flush();""", answer="(p, b) => [200, {}]")
        posts = [(p, b) for p, b, _ in out["posts"]]
        self.assertEqual(out["R"]["early"], 0, "the toast came before the write landed")
        self.assertEqual(posts, [("/api/repos/runner-merge", {"path": path, "value": "auto", "before": "manual"}),
                                 ("/api/repos/runner-merge", {"path": path, "value": "manual", "before": "auto"})])
        self.assertEqual(out["R"]["toast"], f"runner_merge auto · {path}")
        self.assertEqual(out["posts"][0][2], 64)

    def test_a_refused_write_says_why_and_reads_again(self):
        path = self.ids["checkout"]
        out = self.run_page(f"C.run(cmd('repo.managed'), C.get('repo:{path}')); await flush();",
                            answer="(p, b) => [409, {error: 'managed is yes now, not no'}]")
        self.assertEqual(out["toasts"][-1], [f"{path} not changed: managed is yes now, not no", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])

    def test_requeue_and_cancel_post_the_runner_routes_with_the_queue_revision(self):
        n = self.ids["blocked"]
        revision = runner.queue_state(self.connection, n)["revision"]
        out = self.run_page(f"""C.run(cmd('asg.requeue'), C.get('asg:{n}')); await flush();
C.get('asg:{n}').status = 'queued'; C.run(cmd('asg.cancel'), C.get('asg:{n}')); await flush();""", answer="(p, b) => [200, {}]")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [(f"/api/runner/{n}/requeue", {"revision": revision}), (f"/api/runner/{n}/cancel", {"revision": revision})])
        self.assertEqual(out["confirms"], ["asg.cancel"])
        self.assertEqual(out["toasts"][0], [f"Requeued · #{n}. The runner starts it on its next tick.", True])

    def test_a_failed_job_retries_through_its_route_and_an_idle_one_is_off(self):
        revision = next(j for j in self.doc["jobs"] if j["name"] == "nightly-sync")["revision"]
        out = self.run_page("""C.run(cmd('jobs.retry'), C.get('cron:nightly-sync')); await flush();
C.run(cmd('jobs.retry'), C.get('cron:quiet')); await flush();""", answer="(p, b) => [200, {}]")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/jobs/nightly-sync/retry", {"revision": revision})])
        self.assertEqual(out["toasts"][0], ["Retry started · nightly-sync", False])
        self.assertTrue(out["toasts"][1][0].startswith("off: "), out["toasts"])


class TheFindings(PageScript):
    """The Copilot and CodeQL findings on PR #50, one test each."""

    def test_a_monthly_or_yearly_calendar_has_a_next_run(self):
        out = self.run_page("""const at = (...a) => new Date(...a).toISOString().replace('.000', '');
const now = new Date(2026, 8, 30, 12, 0).getTime();
R.got = [nextRun([{ Day: 15, Hour: 3, Minute: 0 }], now), nextRun([{ Month: 1, Day: 1, Hour: 0, Minute: 5 }], now),
  nextRun([{ Month: 2, Day: 29, Hour: 0, Minute: 0 }], now), nextRun([{ Minute: 30 }], now), nextRun([{ Weekday: 7, Hour: 2, Minute: 0 }], now)];
R.want = [at(2026, 9, 15, 3, 0), at(2027, 0, 1, 0, 5), at(2028, 1, 29, 0, 0), at(2026, 8, 30, 12, 30), at(2026, 9, 4, 2, 0)];""")
        self.assertEqual(out["R"]["got"], out["R"]["want"])

    def test_a_page_past_the_last_is_clamped_before_the_slice_and_the_url_follows(self):
        out = self.run_page("R.sched = ELS['view-schedules'].html;", search="?view=schedules&p=5")
        self.assertIn("nightly-sync", out["R"]["sched"])
        self.assertTrue(out["urls"], "the clamp wrote no URL, so ?p=5 stays in the address bar")
        self.assertEqual(out["urls"][-1], {"view": "schedules", "p": None, "q": None})
        out = self.run_page("R.repos = ELS['view-repos'].html;", search="?view=repos&p=3")
        self.assertIn("busy", out["R"]["repos"])
        self.assertEqual(out["urls"][-1]["p"], None)

    def test_the_failed_state_chip_writes_the_url(self):
        out = self.run_page("""showView('schedules');
const chip = { dataset: { stateF: 'failed' } };
const target = { closest: sel => sel === '[data-state-f]' ? chip : null };
ELS['view-schedules'].listeners.click.forEach(f => f({ target }));
R.sched = ELS['view-schedules'].html;""")
        self.assertEqual(out["urls"][-1], {"view": "schedules", "p": None, "q": "state:failed"})
        self.assertNotIn(">quiet<", out["R"]["sched"])

    def test_a_view_name_from_the_url_is_one_of_five_or_repos(self):
        for name in ("constructor", "toString", "__proto__", "nope"):
            with self.subTest(name=name):
                out = self.run_page("R.view = view; R.repos = ELS['view-repos'].html;", search=f"?view={name}")
                self.assertEqual(out["R"]["view"], "repos")
                self.assertIn("busy", out["R"]["repos"])
        out = self.run_page("view = 'constructor'; try { render(); R.drew = true; } catch (e) { R.refused = e.message; }")
        self.assertEqual(out["R"], {"refused": "no such view: constructor"})

    def test_a_bulk_flip_toasts_once_after_every_write_and_undoes_only_what_landed(self):
        """Copilot 5eccfcac7af7 on PR #50: the shell's bulk group waits for each run's promise (sd:2124's contract)."""
        a, b = self.ids["checkout"], "/repos/system"
        out = self.run_page(f"""C.runBulk(cmd('repo.runner-merge'), [C.get('repo:{a}'), C.get('repo:{b}')]); R.early = OUT.toasts.length;
await flush(); R.group = OUT.toasts.map(t => t.msg); await lastUndo().undo(); await flush(); R.after = OUT.toasts.map(t => t.msg);""",
                            answer=f"(p, b) => b.path === {json.dumps(b)} ? [409, {{error: 'runner_merge is auto now'}}] : [200, {{}}]")
        self.assertEqual(out["R"]["early"], 0, "the group toast came before the writes landed")
        self.assertEqual(out["R"]["group"], ["Switch runner-merge · 1 repo · 1 of 2 not changed: runner_merge is auto now"])
        self.assertEqual([(p, body) for p, body, _ in out["posts"]], [
            ("/api/repos/runner-merge", {"path": a, "value": "auto", "before": "manual"}),
            ("/api/repos/runner-merge", {"path": b, "value": "auto", "before": "manual"}),
            ("/api/repos/runner-merge", {"path": a, "value": "manual", "before": "auto"})])
        self.assertEqual(out["R"]["after"][-1],
                         f"Switch runner-merge undone · 1 of 2 reversed · not reversed: {b} (its change did not land)")


class TheSecondRound(PageScript):
    """Copilot's review of f06cbf5 on PR #50: the review-file proposal and the schedule's words."""

    LAST = '{\n  "copilot_review": {\n    "automatic_deep": false\n  },\n  "severity_floor": "high"\n}\n'

    def proposal(self, text, calls):
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        body = json.loads(text) if text.startswith("{") and text.rstrip().endswith("}") and "!" not in text else {}
        row["review"] = {"file": text, "severity_floor": body.get("severity_floor"),
                         "automatic_deep": (body.get("copilot_review") or {}).get("automatic_deep"), "schema": None}
        return self.run_page(calls + """
R.texts = pending.map(p => p.diff.filter(d => d[0] !== 'del' && d[0] !== 'hd').map(d => d[1]).join('\\n'));
R.parsed = R.texts.map(t => { try { return JSON.parse(t); } catch (e) { return 'invalid: ' + e.message; } });""",
                             doc=doc, search=f"?repo={self.ids['checkout']}")

    def test_removing_the_last_property_leaves_valid_json(self):
        out = self.proposal(self.LAST, "propose('severity_floor', '');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": False}}])

    def test_adding_or_changing_the_floor_and_flipping_automatic_deep_leave_valid_json(self):
        bare = '{\n  "copilot_review": {\n    "automatic_deep": false\n  }\n}\n'
        out = self.proposal(bare, "propose('severity_floor', 'medium');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": False}, "severity_floor": "medium"}])
        out = self.proposal(self.LAST, "propose('severity_floor', 'low');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": False}, "severity_floor": "low"}])
        out = self.proposal(self.LAST, "propose('automatic_deep', 'true');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": True}, "severity_floor": "high"}])

    def test_the_diff_shows_only_the_lines_that_change(self):
        out = self.proposal(self.LAST, "propose('severity_floor', ''); R.diff = pending[0].diff;")
        self.assertEqual([d for d in out["R"]["diff"] if d[0] in ("add", "del")],
                         [["del", "  },"], ["del", '  "severity_floor": "high"'], ["add", "  }"]])

    def test_a_review_file_that_is_not_json_is_not_proposed(self):
        out = self.proposal('{\n  "severity_floor": "high",\n}\n!', "propose('severity_floor', 'low');")
        self.assertEqual(out["R"]["parsed"], [])
        self.assertEqual(out["toasts"][-1][0], "Not proposed: .github/sd-review.json is not valid JSON; fix it in the checkout first")

    def test_a_date_pinned_calendar_names_its_date(self):
        out = self.run_page("""R.got = [human([{ Day: 15, Hour: 3, Minute: 0 }]), human([{ Month: 1, Day: 1, Hour: 0, Minute: 5 }]),
  human([{ Day: 1, Minute: 30 }]), human([{ Hour: 3, Minute: 0 }]), human([{ Minute: 30 }]), human([{ Weekday: 1, Hour: 9, Minute: 0 }]),
  human([{ Minute: 0 }, { Minute: 30 }])];""")
        self.assertEqual(out["R"]["got"], ["day 15 03:00", "Jan 1 00:05", "day 1 *:30", "daily 03:00", "hourly at :30", "Mon 09:00",
                                           "every 30 min"])


class TheRequeueUndo(PageScript):
    """The lane's local review of f06cbf5: requeue's Undo cancels only the queued attempt the requeue made."""

    def requeue_then(self, between):
        n = self.ids["blocked"]
        return n, self.run_page(f"""const asg = () => DOC0.assignments.latest.find(a => a.id === {n});
C.run(cmd('asg.requeue'), C.get('asg:{n}')); await flush();
const undo = lastUndo().undo; {between} await load(); await flush();
await undo(); await flush(); R.toast = lastToast().msg;""",
                                answer="(p, b) => p.endsWith('/requeue') ? [200, {id: 1, status: 'queued', revision: 'queued-rev'}] : [200, {}]")

    def test_undo_refuses_once_the_runner_claimed_the_assignment(self):
        n, out = self.requeue_then("Object.assign(asg(), {status: 'running', revision: 'running-rev'});")
        self.assertEqual([p for p, _, _ in out["posts"]], [f"/api/runner/{n}/requeue"], "Undo cancelled running work")
        self.assertEqual(out["R"]["toast"],
                         f"Requeue not undone · #{n} Port the page: assignment #{n} is running now; Undo cancels only the queued attempt the requeue made, so use Cancel")

    def test_undo_cancels_the_queued_attempt_with_the_revision_the_requeue_answered(self):
        n, out = self.requeue_then("Object.assign(asg(), {status: 'queued', revision: 'queued-rev'});")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]][1:], [(f"/api/runner/{n}/cancel", {"revision": "queued-rev"})])
        self.assertEqual(out["R"]["toast"], f"Requeue undone · #{n} Port the page")


# A view's rows as the browser finds them: `#view-<v> tr[data-id]` and `#view-<v> tr[data-id="<id>"]` read the view's drawn
# markup, so the page's first-row and row-still-drawn checks run as they do in a browser.
ROWS_QUERY = r"""
var pageQuery = document.querySelector;
document.querySelector = sel => { const m = sel.match(/^#view-(\w+) tr\[data-id(?:="([^"]*)")?\]$/); if (!m) return pageQuery(sel);
  const ids = [...(((ELS['view-' + m[1]] || {}).html) || '').matchAll(/<tr data-id="([^"]*)"/g)].map(x => x[1]);
  const id = m[2] === undefined ? ids[0] : ids.find(x => x === m[2]); return id === undefined ? null : { dataset: { id } }; };
"""
# Every read of the document waits until the test answers it, newest first; a read started after a write sees the write.
HELD = r"""
const held = [], copy = d => JSON.parse(JSON.stringify(d));
let wrote = false, after = copy(DOC0);
ANSWER = (path, body) => { if (path !== '/api/management') { wrote = true; return [200, {}]; }
  const doc = wrote ? after : copy(DOC0); return new Promise(ok => held.push(() => ok([200, doc]))); };
const answerNewestFirst = async () => { while (held.length) { held.pop()(); await flush(); } };
"""


class TheSharedReader(PageScript):
    """sd:2485: the page reads through shell.read (read.js, sd:2418), so it gets the reader's guards."""

    WHAT = "repos, the lane, sessions, services and jobs"

    def test_only_the_newest_read_draws(self):
        out = self.run_page(HELD + """after.jobs = after.jobs.filter(j => j.name !== 'quiet'); wrote = true;
load(); await flush(); wrote = false; load(); await flush();
held[0](); await flush(); held[1](); await flush();
R.sched = ELS['view-schedules'].html; R.type = C.get('cron:quiet').type;""")
        self.assertIn(">quiet<", out["R"]["sched"], "the newest read lists quiet")
        self.assertEqual(out["R"]["type"], "job")
        out = self.run_page(HELD + """after.jobs = after.jobs.filter(j => j.name !== 'quiet');
load(); await flush(); wrote = true; load(); await flush();
held[1](); await flush(); held[0](); await flush();
R.sched = ELS['view-schedules'].html; R.type = C.get('cron:quiet').type;""")
        self.assertNotIn(">quiet<", out["R"]["sched"], "an older read answered last and drew over the newest")
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_row_the_read_no_longer_lists_runs_no_command(self):
        out = self.run_page("""DOC0.jobs = DOC0.jobs.filter(j => j.name !== 'quiet'); await load(); await flush();
R.obj = [C.get('cron:quiet').type, C.get('cron:quiet').label]; R.kept = C.get('cron:nightly-sync').type;""")
        self.assertEqual(out["R"]["obj"], ["not listed", "quiet (no longer listed)"])
        self.assertEqual(out["R"]["kept"], "job")

    def test_a_selection_whose_row_is_gone_moves_to_the_first_row(self):
        out = self.run_page("""selectRow('cron:quiet', false); R.before = ELS.details.html.includes('<h2>quiet</h2>');
DOC0.jobs = DOC0.jobs.filter(j => j.name !== 'quiet'); await load(); await flush();
R.details = ELS.details.html; R.row = ROW;""", search="?view=schedules", extra=ROWS_QUERY)
        self.assertTrue(out["R"]["before"])
        self.assertIn("<h2>nightly-sync</h2>", out["R"]["details"], "Details still shows a job the read no longer lists")
        self.assertEqual(out["R"]["row"], "cron:nightly-sync")

    def test_the_first_read_selects_the_linked_row_in_its_view_only(self):
        """A guard: the link's row is the reader's first-read ask; a row of another view is not selected."""
        out = self.run_page("R.details = ELS.details.html;", search="?view=schedules&row=cron:quiet",
                            extra=ROWS_QUERY + "\nROW = 'cron:quiet';\n")
        self.assertIn("<h2>quiet</h2>", out["R"]["details"])
        out = self.run_page("R.details = ELS.details.html;", search="?view=schedules&row=wt:abandoned",
                            extra=ROWS_QUERY + "\nROW = 'wt:abandoned';\n")
        self.assertIn("<h2>nightly-sync</h2>", out["R"]["details"])

    def test_a_reread_waits_for_the_read_its_write_overtook(self):
        """The write barrier: a read in flight when the write lands answers with the old row, so it must not be the last."""
        path = self.ids["checkout"]
        out = self.run_page(HELD + f"""after.repos.find(r => r.path === {json.dumps(path)}).runner_merge = 'auto';
load(); await flush();
C.run(cmd('repo.runner-merge'), C.get('repo:{path}')); await flush();
await answerNewestFirst();
R.merge = C.get('repo:{path}').row.merge; R.toasts = OUT.toasts.map(t => t.msg);""")
        self.assertEqual(out["R"]["merge"], "auto", "the page settled on a read that started before its write landed")
        self.assertEqual(out["R"]["toasts"], [f"runner_merge auto · {path}"])

    def test_a_failed_reread_after_a_write_keeps_the_rows_and_says_the_write_landed(self):
        path = self.ids["checkout"]
        out = self.run_page(f"""ANSWER = p => p === '/api/management' ? [500, {{error: 'database is busy'}}] : [200, {{}}];
C.run(cmd('repo.runner-merge'), C.get('repo:{path}')); await flush();
R.sched = ELS['view-schedules'].html; R.type = C.get('cron:quiet').type;""")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertEqual(out["states"][-1]["text"],
                         f"The change landed; {self.WHAT} were not read again: database is busy. Reload reads them.")
        self.assertIn("nightly-sync", out["R"]["sched"])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["toasts"][-1], [f"runner_merge auto · {path}", True])

    def test_a_failed_load_clears_the_rows_and_their_commands(self):
        out = self.run_page("""ANSWER = () => [500, {error: 'database is busy'}]; await load(); await flush();
R.sched = ELS['view-schedules'].html; R.repos = ELS['view-repos'].html; R.type = C.get('cron:quiet').type;
R.lamps = ['repos', 'lane', 'sessions', 'schedules'].map(v => ELS['sv-' + v].dataset.state);""")
        self.assertEqual(out["states"][-1], {"kind": "error", "source": "/api/management",
                                             "text": "Repos, the lane, sessions, services and jobs were not read: database is busy. Reload retries it."})
        self.assertNotIn("nightly-sync", out["R"]["sched"])
        self.assertIn("database is busy", out["R"]["sched"])
        self.assertNotIn("busy</button>", out["R"]["repos"])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["R"]["lamps"], ["unknown"] * 4)
        self.assertEqual(out["attention"][-1]["state"], "unknown")


class TheMinuteMarks(PageScript):
    """sd:2385 (Copilot 65f0869637eb on PR 50): 'every N min' only when the marks are evenly spaced around the hour."""

    def test_an_uneven_minute_list_names_its_marks(self):
        out = self.run_page("""R.got = [human([{ Minute: 5 }, { Minute: 30 }]), human([{ Minute: 0 }, { Minute: 15 }, { Minute: 45 }]),
  human([{ Minute: 30 }, { Minute: 0 }]), human([{ Minute: 0 }, { Minute: 20 }, { Minute: 40 }]), human([{ Minute: 10 }, { Minute: 10 }])];""")
        self.assertEqual(out["R"]["got"], ["hourly at :05, :30", "hourly at :00, :15, :45", "every 30 min", "every 20 min", "hourly at :10"])


class TheLastRun(PageScript):
    """sd:2210 and sd:2209: Schedules shows each job's stamped last run; the lane strip shows the archive refresh."""

    def test_each_schedule_row_shows_its_stamped_last_run_or_why_not(self):
        out = self.run_page("R.sched = ELS['view-schedules'].html;", search="?view=schedules")
        sched = out["R"]["sched"]
        self.assertIn(">Last run<", sched)
        self.assertIn('data-k="last"><time class="rel" datetime="2026-09-06T02:16:30Z"', sched)
        self.assertIn("exit 7", sched)
        self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span> not stamped', sched)

    def test_a_run_with_no_end_says_so_and_a_running_one_says_since(self):
        doc = json.loads(json.dumps(self.doc))
        for job in doc["jobs"]:
            job["last_run"] = {"state": "open", "started": "2026-09-06T02:15:00Z", "ended": None, "exit": None, "reason": ""}
            if job["name"] == "quiet":
                job["state"] = "running"
        out = self.run_page("R.sched = ELS['view-schedules'].html;", doc=doc, search="?view=schedules")
        sched = out["R"]["sched"]
        self.assertIn('<span class="g-caution" aria-hidden="true">▲</span> no end', sched)
        self.assertIn('<span class="g-queued" aria-hidden="true">◌</span> since', sched)

    def test_the_details_name_the_stamp_or_the_reason_there_is_none(self):
        out = self.run_page("selectRow('cron:nightly-sync', false); R.failed = ELS.details.html; selectRow('cron:quiet', false); R.quiet = ELS.details.html;",
                            search="?view=schedules")
        self.assertIn("<dt>Last run</dt>", out["R"]["failed"])
        self.assertIn('datetime="2026-09-06T02:15:00Z"', out["R"]["failed"])
        self.assertIn('datetime="2026-09-06T02:16:30Z"', out["R"]["failed"])
        self.assertIn("exit 7", out["R"]["failed"])
        self.assertIn("logs/.nightly-sync.stamp", out["R"]["failed"])
        self.assertIn("no run stamp", out["R"]["quiet"])

    def test_the_lane_strip_names_the_last_and_next_archive_refresh(self):
        out = self.run_page("R.lane = ELS['view-lane'].html;", search="?view=lane")
        lane = out["R"]["lane"]
        self.assertIn('<span class="label">Archive refresh</span>', lane)
        self.assertIn('datetime="2026-09-06T03:00:00+00:00"', lane)
        self.assertIn('datetime="2026-09-07T03:00:00+00:00" data-future', lane)

    def test_an_archive_refresh_not_read_is_unknown_with_its_reason(self):
        doc = dict(self.doc, archive=None, sources=dict(self.doc["sources"], archive="runner.sh status ran past its 15 seconds"))
        out = self.run_page("R.lane = ELS['view-lane'].html;", doc=doc, search="?view=lane")
        self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span> not read: runner.sh status ran past its 15 seconds',
                      out["R"]["lane"])
        never = dict(self.doc, archive={"last": None, "next": "2026-09-06T12:00:00+00:00", "due": True})
        out = self.run_page("R.lane = ELS['view-lane'].html;", doc=never, search="?view=lane")
        self.assertIn('<span class="g-caution" aria-hidden="true">▲</span> never run · due', out["R"]["lane"])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "management", "Management", "/management", ("/api/management",)
