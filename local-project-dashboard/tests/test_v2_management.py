"""The v2 Management page (sd:2118).

What this slice promises: `/management` answers under the shared policy and
loads its script before `shell.js`; `/api/management` is one document built
from the reads v1 already makes (the sd-db repo table with each checkout's
`.github/sd-review.json` and protection reading, the fleet's git state and
sessions, the assignment lane, services and jobs), where a source that fails is
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

from sd_db import repos, upsert_repo, workflow
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
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")


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

    def document(self, read=fleet, grant=GRANTED):
        return management_screen.document(self.connection, now=NOW, fleet=read, jobs=self.jobs, services=NoServices(),
                                          grant=grant)

    def test_every_source_is_read_and_says_so(self):
        doc = self.document()
        self.assertEqual(doc["sources"], {k: "" for k in ("repos", "git", "lane", "assignments", "sessions", "services", "jobs",
                                                          "grant")})
        self.assertEqual(doc["read"], NOW)
        # sd:3041: the runner stopped, so no archive refresh from runner.sh and no runner heartbeat.
        self.assertNotIn("archive", doc)
        self.assertNotIn("heartbeat", doc["lane"])

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

    def test_a_job_carries_the_end_of_its_log_from_a_whole_line(self):
        """sd:3388: the page reads a job's log as v1's Jobs card could not: the end of logs/<job>.log, whole lines only."""
        lines = [f"[nightly-sync] line {n:04d} of the run" for n in range(400)]
        (self.jobs.cron_root / "logs" / "nightly-sync.log").write_text("\n".join(lines) + "\n")
        jobs = {job["name"]: job["log"] for job in self.document()["jobs"]}
        log = jobs["nightly-sync"]
        self.assertEqual((log["state"], log["cut"], log["reason"]), ("read", True, ""))
        self.assertTrue(log["text"].endswith(lines[-1] + "\n"))
        self.assertIn(log["text"].splitlines()[0], lines)
        self.assertLessEqual(len(log["text"].encode()), management_screen.LOG_BYTES)
        self.assertEqual((jobs["quiet"]["state"], jobs["quiet"]["text"]), ("none", ""))
        self.assertIn("no log", jobs["quiet"]["reason"])

    def test_a_short_log_is_read_whole_and_a_log_that_is_not_a_file_is_unread_without_blocking(self):
        import os
        import threading

        (self.jobs.cron_root / "logs" / "nightly-sync.log").write_text("one run\nFAILED rc=7\n")
        os.mkfifo(self.jobs.cron_root / "logs" / "quiet.log")
        alone = {}
        # The connection belongs to this thread, so the bounded read is tried alone in another first.
        reader = threading.Thread(target=lambda: alone.update(management_screen.job_log(self.jobs.cron_root, "quiet")), daemon=True)
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive(), "reading a FIFO log blocked the document")
        got = {job["name"]: job["log"] for job in self.document()["jobs"]}
        self.assertEqual(alone, got["quiet"])
        self.assertEqual(got["nightly-sync"], {"state": "read", "text": "one run\nFAILED rc=7\n", "cut": False, "reason": ""})
        self.assertEqual((got["quiet"]["state"], got["quiet"]["reason"]), ("unread", "the log is not a regular file"))
        self.assertEqual(management_screen.job_log(None, "quiet")["reason"], "the jobs backend names no logs folder")

    def test_a_job_carries_the_last_request_and_launchctls_answer(self):
        """sd:3388: a cancel's outcome is what the next read shows, as v1's Last request line did."""
        from sd_db import operations

        class Sends(JobsBackend):
            calls = []

            def perform(self, action, service):
                self.calls.append((action, service))
                return subprocess.CompletedProcess([], 0, "", "")

        backend = Sends(self.tmp.name, jobs=[("running-fixture", "running", None, None)])
        doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=backend, services=NoServices(), grant=GRANTED)
        running = next(job for job in doc["jobs"] if job["name"] == "running-fixture")
        self.assertIsNone(running["last_request"])
        self.assertTrue(running["capabilities"]["cancel"]["allowed"])
        operations.cancel_job(self.connection, "running-fixture", expected_revision=running["revision"], who="dashboard", backend=backend)
        doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=backend, services=NoServices(), grant=GRANTED)
        request = next(job for job in doc["jobs"] if job["name"] == "running-fixture")["last_request"]
        self.assertEqual((request["action"], request["status"], request["who"]), ("cancel", "accepted", "dashboard"))
        self.assertEqual(backend.calls, [("cancel", "fixture/running-fixture")])

    def test_the_badge_counts_failed_jobs_and_an_unread_jobs_list_is_unknown(self):
        # sd:3446: the read's rule, which every page's rail shows from Management's last read.
        jobs = [{"state": "failed"}, {"state": "interrupted"}, {"state": "running"}, {"state": "failed"}]
        self.assertEqual(management_screen._attention(jobs), {"state": "warning", "n": 2, "what": "scheduled jobs failed"})
        self.assertEqual(management_screen._attention(jobs[1:3]), {"state": "ok", "n": 0, "what": "scheduled jobs failed"})
        self.assertEqual(management_screen._attention(None), {"state": "unknown", "n": 0, "what": "scheduled jobs not read"})
        self.assertEqual(self.document()["attention"], management_screen._attention(self.document()["jobs"]))

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

    def test_assignments_carry_the_history_counts(self):
        doc = self.document()
        (blocked,) = doc["assignments"]["latest"]
        self.assertEqual((blocked["id"], blocked["status"]), (self.ids["blocked"], "blocked"))
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

    def test_a_repo_carries_its_dependabot_flag_and_the_owner_gate_and_the_verb_flips_it(self):
        """sd:3984: the flag is the row's column; a platypeeps row is the owner's and a denied one is denied."""
        checkout = self.ids["checkout"]
        upsert_repo(self.connection, "/repos/own", remote="git@github.com:platypeeps/own.git")
        upsert_repo(self.connection, "/repos/denied", remote="git@github.com:platypeeps/Trellis.git")
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual([(rows[path]["dependabot"], rows[path]["dependabot_gate"]) for path in
                          (checkout, "/repos/own", "/repos/denied")], [("no", None), ("no", "owner"), ("no", "denied")])
        got = management_screen.set_repo(self.connection, "dependabot", checkout, "yes", "no")
        self.assertEqual((got["field"], got["value"], got["before"]), ("dependabot", "yes", "no"))
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "dependabot", checkout, "yes", "no")
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["dependabot"], "yes")
        from sd_db.remote import HubOnly

        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly):
                management_screen.set_repo(self.connection, "dependabot", checkout, "no", "yes")
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["dependabot"], "yes")

    def test_a_repo_carries_its_github_issues_setting_and_the_verb_flips_it(self):
        """sd:4840: `repo.github_issues` reads on or off and `repos.set_github_issues` writes it, refusing a stale before."""
        checkout = self.ids["checkout"]
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["github_issues"], "off")
        got = management_screen.set_repo(self.connection, "github-issues", checkout, "on", "off")
        self.assertEqual((got["field"], got["value"], got["before"]), ("github_issues", "on", "off"))
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "github-issues", checkout, "on", "off")
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["github_issues"], "on")

    def test_the_lane_host_reads_hub_and_moves_refusing_a_stale_before_a_bad_name_and_an_unknown_path(self):
        """sd:3075, acceptances 2 and 3. NULL reads `hub`; a move writes the name."""
        checkout = self.ids["checkout"]
        lane = lambda: {row["path"]: row for row in self.document()["repos"]}[checkout]["lane_host"]  # noqa: E731
        self.assertEqual(lane(), "hub")
        got = management_screen.set_repo(self.connection, "lane-host", checkout, "build-2", "hub")
        self.assertEqual((got["field"], got["value"], got["before"]), ("lane_host", "build-2", "hub"))
        self.assertEqual(lane(), "build-2")
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "lane-host", checkout, "build-3", "hub")
        with self.assertRaises(repos.RepoRefusal) as refused:
            management_screen.set_repo(self.connection, "lane-host", checkout, "Build_2", "build-2")
        self.assertIn("not a lane host", str(refused.exception))
        with self.assertRaises(repos.RepoRefusal):
            management_screen.set_repo(self.connection, "lane-host", checkout + "-absent", "hub", "build-2")
        # sd:3075 part 1: the hub cannot take build-2's ship lock, so it cannot move the lane back (sd:3003 part 2).
        with self.assertRaises(repos.RepoRefusal) as refused:
            management_screen.set_repo(self.connection, "lane-host", checkout, "hub", "build-2")
        self.assertIn("run the move on build-2 (sd:3003 part 2)", str(refused.exception))
        self.assertEqual(lane(), "build-2")

    def test_the_channels_form_writes_a_setting_and_refuses_a_stale_before_a_bad_folder_and_a_satellite(self):
        """sd:3465: a value writes through `repos.set_channels` and answers the render remedy; an old `before`, a folder
        `channels.validate` refuses and a satellite's write each change nothing. NULL reads `none`, the defaults."""
        from sd_db.remote import HubOnly

        checkout = self.ids["checkout"]
        row = lambda: {r["path"]: r for r in self.document()["repos"]}[checkout]  # noqa: E731
        self.assertEqual((row()["channels"], row()["channels_problem"]), ("none", None))
        got = management_screen.set_repo(self.connection, "channels", checkout,
                                          {"obsidian": {"folder": "Briefs/x"}, "drive": {}}, "none")
        written = '{"drive":{},"obsidian":{"folder":"Briefs/x"}}'
        self.assertEqual((got["field"], got["value"], got["before"]), ("channels", written, "none"))
        self.assertIn("sd-research-kit render", got["remedy"])
        self.assertEqual(row()["channels"], written)
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "channels", checkout, None, "none")
        with self.assertRaises(repos.RepoRefusal) as refused:
            management_screen.set_repo(self.connection, "channels", checkout, {"notion": {"folder": "a/b"}}, written)
        self.assertIn("one page name", str(refused.exception))
        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly) as refused:
                management_screen.set_repo(self.connection, "channels", checkout, None, written)
        self.assertIn("repo channels runs on the sd hub only", str(refused.exception))
        self.assertEqual(row()["channels"], written)
        got = management_screen.set_repo(self.connection, "channels", checkout, None, written)
        self.assertEqual((got["value"], got["before"], row()["channels"]), ("none", written, "none"))

    def test_a_channels_write_answers_the_stored_value_when_the_library_keeps_a_feed(self):
        """sd:4632: a set that omits `feed` keeps the stored one, so the answer is the row read back, not the posted object:
        the form's Undo then compares against what the row holds."""
        checkout = self.ids["checkout"]
        feed = '{"feed":{"url":"https://example.test/feed.xml"}}'
        self.connection.execute("UPDATE repo SET channels = ? WHERE path = ?", (feed, checkout))
        self.connection.commit()
        stored = lambda: self.connection.execute("SELECT channels FROM repo WHERE path = ?", (checkout,)).fetchone()[0]  # noqa: E731
        got = management_screen.set_repo(self.connection, "channels", checkout, {"obsidian": {}}, feed)
        kept = '{"feed":{"url":"https://example.test/feed.xml"},"obsidian":{}}'
        self.assertEqual(stored(), kept)
        self.assertEqual((got["value"], got["before"]), (kept, feed))
        got = management_screen.set_repo(self.connection, "channels", checkout, {"obsidian": {}}, kept)
        self.assertEqual(got["value"], kept, "a set that changes nothing answers the row, still")

    def test_the_channels_answer_is_read_in_the_transaction_that_wrote_it(self):
        """sd:4632: a writer between the write and the read-back would put its setting into this form's Undo as `before`."""
        checkout = self.ids["checkout"]
        seen = []
        real = repos.row_for

        def probe(connection, path):
            seen.append(connection.in_transaction)
            return real(connection, path)

        with patch.object(management_screen.repos, "row_for", probe):
            management_screen.set_repo(self.connection, "channels", checkout, {"drive": {}}, "none")
        self.assertEqual(seen, [True])

    def test_a_stored_channels_value_the_library_refuses_is_named_not_read_as_the_defaults(self):
        # sd:3465: `repo_channels` raises on such a value rather than publish to the defaults; the form says why and offers Clear.
        self.connection.execute("UPDATE repo SET channels = ? WHERE path = ?", ('{"site": {}}', self.ids["checkout"]))
        self.connection.commit()
        got = {r["path"]: r for r in self.document()["repos"]}[self.ids["checkout"]]
        self.assertEqual(got["channels"], '{"site":{}}')
        self.assertIn("'site' is not a channel", got["channels_problem"])

    def test_a_satellite_refuses_a_lane_move_and_writes_nothing(self):
        """sd:3075 part 1: a move holds the lane host's ship lock, which only the hub's dashboard reaches."""
        from sd_db.remote import HubOnly

        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly) as refused:
                management_screen.set_repo(self.connection, "lane-host", self.ids["checkout"], "build-2", "hub")
        self.assertIn("repo lane-host runs on the sd hub only", str(refused.exception))
        self.assertIsNone(self.connection.execute(
            "SELECT lane_host FROM repo WHERE path = ?", (self.ids["checkout"],)).fetchone()[0])

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

    def test_a_pin_that_is_not_a_regular_file_is_refused_without_blocking(self):
        """sd:2911: a FIFO, or a link to one, is refused before any read; reading one would hang /api/management."""
        import os
        import threading

        root = Path(self.tmp.name) / "fifo-pins"
        root.mkdir()
        os.mkfifo(root / ".python-version")
        os.symlink(root / ".python-version", root / ".nvmrc")
        got = {}
        reader = threading.Thread(target=lambda: got.update(management_screen._runtimes(str(root))), daemon=True)
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive(), "reading a FIFO pin blocked the document")
        self.assertEqual(got, {"python": {"error": ".python-version is not a regular file"},
                               "node": {"error": ".nvmrc is not a regular file"}})

    def test_a_pin_file_past_the_limit_is_refused_after_a_bounded_read(self):
        root = Path(self.tmp.name) / "big-pins"
        root.mkdir()
        (root / "pyproject.toml").write_text("#" * (management_screen.PIN_BYTES + 10))
        reads = []
        real = management_screen.os.read

        def counted(fd, n):
            reads.append(n)
            return real(fd, n)

        with patch.object(management_screen.os, "read", counted):
            got = management_screen._runtimes(str(root))
        self.assertEqual(got["python"], {"error": "pyproject.toml is larger than a pin file"})
        self.assertLessEqual(sum(reads), 2 * (management_screen.PIN_BYTES + 1))

    def test_a_deeply_nested_manifest_is_that_repos_error_not_the_documents(self):
        """sd:2911: tomllib raises RecursionError on deep nesting, which no FAILURES entry caught; the whole document failed."""
        checkout = Path(self.ids["checkout"])
        depth = 15000
        self.assertLess(depth * 2 + 20, management_screen.PIN_BYTES)
        (checkout / "pyproject.toml").write_text("[project]\nx = " + "[" * depth + "]" * depth + "\n")
        doc = self.document()
        self.assertEqual(doc["sources"]["repos"], "")
        rows = {row["path"]: row for row in doc["repos"]}
        self.assertEqual(rows[str(checkout)]["runtimes"]["python"], {"error": "pyproject.toml nests too deeply to read"})

    def test_a_json_parser_that_recurses_too_deep_is_a_reason_too(self):
        """The C JSON scanner takes the nesting these sizes allow; a pure-Python one raises RecursionError (sd:2911)."""
        checkout = Path(self.ids["checkout"])
        (checkout / "package.json").write_text("{}")
        real = json.loads

        def deep(text, *args, **kwargs):
            # The two checkout files only: package.json is "{}", and _review hands json.loads bytes.
            if text == "{}" or isinstance(text, bytes):
                raise RecursionError("maximum recursion depth exceeded")
            return real(text, *args, **kwargs)

        with patch.object(management_screen.json, "loads", deep):
            doc = self.document()
        self.assertEqual(doc["sources"]["repos"], "")
        row = {row["path"]: row for row in doc["repos"]}[str(checkout)]
        self.assertEqual(row["runtimes"]["node"], {"error": "package.json nests too deeply to read"})
        self.assertEqual(row["review"]["error"], "nests too deeply to read")

    def test_a_setting_of_the_wrong_type_is_that_repos_error_not_a_value(self):
        """sd:1629 round 2: one checkout's `{"toString": null}` reached the page, and String() of it stopped every view."""
        checkout = Path(self.ids["checkout"])
        (checkout / ".github" / "sd-review.json").write_text(
            '{"severity_floor": 7, "copilot_review": {"automatic_deep": {"toString": null}}}')
        body = {"gaps": [], "detail": {"required_contexts": ["ci", {"toString": None}], "strict": True}}
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES (?, '2026-10-04T01:00:00Z', 'protected', 'main', ?)", (str(checkout), json.dumps(body)))
        self.connection.commit()
        doc = self.document()
        self.assertEqual(doc["sources"]["repos"], "")
        row = {row["path"]: row for row in doc["repos"]}[str(checkout)]
        review, guarded = row["review"], row["protection"]
        self.assertEqual((review["severity_floor"], review["automatic_deep"]), (None, None))
        self.assertEqual(review["error"], "severity_floor is not a string; copilot_review.automatic_deep is not true or false")
        self.assertEqual((guarded["required"], guarded["required_error"]), (None, "a required check is not a name"))

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
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "commands.js", "management.js", "shell.js"])
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
        self.assertIn(self.ids["checkout"], [row["path"] for row in doc["repos"]])

    def test_the_repo_write_flips_once_and_refuses_the_same_before_again(self):
        body = {"path": self.ids["checkout"], "value": "auto", "before": "manual"}
        status, _, got = self.post("/api/repos/runner-merge", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "auto", "manual"))
        self.assertEqual(self.post("/api/repos/runner-merge", body)[0], 409)
        self.assertEqual(self.post("/api/repos/runner-merge", {**body, "value": "sometimes"})[0], 400)
        self.assertEqual(self.post("/api/repos/managed", {"path": self.ids["checkout"], "value": "yes", "before": "yes"})[0], 400)
        self.assertEqual(self.post("/api/repos/mode", {"path": self.ids["checkout"], "value": "x", "before": "y"})[0], 404)

    def test_the_dependabot_route_flips_once_and_refuses_a_stale_before_and_a_bad_word(self):
        """sd:3984: `/api/repos/dependabot` takes yes or no and the value the page showed."""
        body = {"path": self.ids["checkout"], "value": "yes", "before": "no"}
        status, _, got = self.post("/api/repos/dependabot", body)
        self.assertEqual((status, got["field"], got["value"], got["before"]), (200, "dependabot", "yes", "no"))
        self.assertEqual(self.post("/api/repos/dependabot", body)[0], 409)
        self.assertEqual(self.post("/api/repos/dependabot", {**body, "value": "always", "before": "yes"})[0], 400)

    def test_the_github_issues_route_flips_once_and_refuses_a_stale_before_and_a_bad_word(self):
        """sd:4840: `/api/repos/github-issues` takes on or off and the value the page showed."""
        body = {"path": self.ids["checkout"], "value": "on", "before": "off"}
        status, _, got = self.post("/api/repos/github-issues", body)
        self.assertEqual((status, got["field"], got["value"], got["before"]), (200, "github_issues", "on", "off"))
        self.assertEqual(self.post("/api/repos/github-issues", body)[0], 409)
        self.assertEqual(self.post("/api/repos/github-issues", {**body, "value": "yes", "before": "on"})[0], 400)

    def test_the_lane_host_route_moves_once_and_refuses_a_stale_before_and_a_bad_name(self):
        """sd:3075, acceptance 2: `Build_2` is a 400 naming the rule; a repeated move is a stale 409."""
        body = {"path": self.ids["checkout"], "value": "build-2", "before": "hub"}
        status, _, got = self.post("/api/repos/lane-host", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "build-2", "hub"))
        self.assertEqual(self.post("/api/repos/lane-host", body)[0], 409)
        status, _, got = self.post("/api/repos/lane-host", {**body, "value": "Build_2", "before": "build-2"})
        self.assertEqual(status, 400)
        self.assertIn("not a lane host", got["error"])
        status, _, got = self.post("/api/repos/lane-host", {**body, "path": "/nowhere", "value": "hub", "before": "build-2"})
        self.assertEqual(status, 400)
        self.assertIn("not a registered repository", got["error"])
        self.assertEqual(self.post("/api/repos/lane-host", {**body, "value": "", "before": "build-2"})[0], 400)

    def test_the_channels_route_writes_once_and_refuses_an_old_before_a_bad_folder_and_a_satellite(self):
        """sd:3465 acceptance: a form changes a repo's row, and a second form posted with the old value is refused."""
        body = {"path": self.ids["checkout"], "value": {"notion": {"folder": "Example"}}, "before": "none"}
        status, _, got = self.post("/api/repos/channels", body)
        self.assertEqual((status, got["value"], got["before"]), (200, '{"notion":{"folder":"Example"}}', "none"))
        self.assertIn("sd-research-kit render", got["remedy"])
        status, _, got = self.post("/api/repos/channels", {**body, "value": {"drive": {}}})
        self.assertEqual((status, got.get("reload")), (409, True))
        status, _, got = self.post("/api/repos/channels", {**body, "value": {"drive": {"folder": "../up"}}, "before": '{"notion":{"folder":"Example"}}'})
        self.assertEqual(status, 400)
        self.assertIn("'..' segment", got["error"])
        self.assertEqual(self.post("/api/repos/channels", {**body, "value": "drive="})[0], 400)
        self.assertEqual(self.post("/api/repos/channels", {**body, "before": None})[0], 400)
        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            status, _, got = self.post("/api/repos/channels", {**body, "value": None, "before": '{"notion":{"folder":"Example"}}'})
        self.assertEqual(status, 400)
        self.assertIn("runs on the sd hub only", got["error"])
        status, _, got = self.post("/api/repos/channels", {**body, "value": None, "before": '{"notion":{"folder":"Example"}}'})
        self.assertEqual((status, got["value"]), (200, "none"))

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
                                              grant=GRANTED)

    def run_page(self, body, answer="null", doc=None, search="", extra=""):
        script = (STAND_IN + EXTRA + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_EXTRA + READ_SHELL + extra
                  + f"\nconst DOC0 = {json.dumps(doc or self.doc)};\n"
                  + "const WRITE = " + answer + ";\n"
                  + f"location.search = {json.dumps(search)};\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/management') return [200, DOC0];
  return WRITE ? WRITE(path, body) : [200, {}];
};\n""" + COMMANDS_JS + PAGE_JS + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
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
            ["repo.lane-host", "repo", "safe", "h", False, False],
            ["repo.pull", "repo", "safe", "l", False, False],
            ["sddb.run", "sd-db change", "undo", "u", True, True],
            ["file.prepare", "file change", "safe", "p", False, False],
            ["sddb.withdraw", "sd-db change", "safe", None, False, False],
            ["file.withdraw", "file change", "safe", None, False, False],
            ["asg.get", "assignment", "safe", "o", False, False],
            ["item.show", "item", "safe", "o", False, False],
            ["wt.prune", "worktrees", "confirm", "p", None, False],
            ["svc.restart", "service", "confirm", "t", None, False],
            ["svc.stop", "service", "confirm", "s", None, False],
            ["svc.start", "service", "safe", "a", None, False],
            ["jobs.retry", "job", "safe", "t", None, False],
            ["jobs.cancel", "job", "confirm", "x", None, False],
            ["jobs.log", "job", "safe", "l", False, False],
            ["jobs.print", "job", "safe", "p", False, False],
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
        self.assertEqual(set(cells), {"managed", "merge", "lane", "floor", "copilot", "checks", "python", "node"})
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

    def test_the_repo_page_shows_the_lane_host_and_move_lane_runs_the_verb_and_undoes(self):
        """sd:3075, acceptance 3: the list and the page show the host; the field offers hub and each named host;
        Run posts the move with the value the page showed, and Undo moves it back."""
        doc = json.loads(json.dumps(self.doc))
        doc["repos"].append(dict(doc["repos"][0], path="/srv/other", lane_host="build-3"))
        path = self.ids["checkout"]
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('lane_host', 'build-2'); R.cmd = pending[0].cmd; R.note = pending[0].note;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            doc=doc, search=f"?repo={path}", answer="(p, b) => [200, {}]")
        page = out["R"]["page"]
        self.assertIn('data-key="lane_host"', page)
        self.assertIn('data-set="lane_host"', page)
        self.assertEqual(re.findall(r'<option value="([^"]+)"></option>', page.split('id="lane-hosts"')[1]), ["hub", "build-3"])
        self.assertEqual(out["R"]["cmd"], f"~/repos/platypeeps/sd-ai-command-pack/bin/sd-db repo lane-host '{path}' 'build-2'")
        # sd:3284: the queue lives in the hub database, so the move takes the pending entries with it.
        self.assertIn("Pending entries follow", out["R"]["note"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/lane-host", {"path": path, "value": "build-2", "before": "hub"}),
                          ("/api/repos/lane-host", {"path": path, "value": "hub", "before": "build-2"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · Move lane hub → build-2")
        out = self.run_page("R.repos = ELS['view-repos'].html;", doc=doc)
        cells = re.findall(r'data-k="lane">([^<]*)</td>', out["R"]["repos"])
        self.assertIn("hub", cells)
        self.assertIn("build-3", cells)

    # The channels form's inputs: document.querySelector answers each `#ch-form [data-ch...]` selector from FORM.
    FORM = """const FORM = {}; const fill = (c, on, folder) => { FORM[`#ch-form [data-ch="${c}"]`] = { checked: on }; FORM[`#ch-form [data-chf="${c}"]`] = { value: folder }; };
const pageQuery = document.querySelector; document.querySelector = sel => FORM[sel] || pageQuery(sel);
"""

    def test_the_channels_form_shows_the_defaults_proposes_the_verb_runs_it_and_undoes(self):
        """sd:3465: a repo with no setting shows the defaults; a checkbox and a folder per channel propose the
        `sd-db repo channels` line; Run posts the value with the setting the page showed and toasts the render remedy;
        Undo writes the old setting back; a form with no channel checked proposes nothing."""
        path = self.ids["checkout"]
        name = Path(path).name
        remedy = f"Run `sd-research-kit render` in {path} to publish under it, or wait for the next commit."
        out = self.run_page(self.FORM + """R.page = ELS['view-repos'].html;
fill('obsidian', true, ' Briefs/x '); fill('notion', false, 'Ignored'); fill('drive', true, '');
propose('channels', 'on'); R.cmd = pending[0].cmd; R.title = pending[0].title; R.note = pending[0].note;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();
fill('obsidian', false, ''); fill('drive', false, ''); propose('channels', 'on'); R.left = pending.length;""",
                            search=f"?repo={path}", answer="(p, b) => [200, { value: '{\"drive\":{},\"obsidian\":{\"folder\":\"Briefs/x\"}}', remedy: " + json.dumps(remedy) + " }]")
        page = out["R"]["page"]
        self.assertIn('data-key="channels"', page)
        self.assertIn('<span class="no">none · defaults</span>', page)
        self.assertIn(f"in Briefs/{name} and {name}.", page)
        self.assertEqual(len(re.findall(r'<input type="checkbox" data-set="channels" data-ch="\w+" checked>', page)), 3)
        self.assertIn(f'data-chf="drive" value="" placeholder="Briefs/{name}"', page)
        self.assertNotIn("data-ch-clear", page, "no setting, nothing to clear")
        self.assertEqual(out["R"]["cmd"], f"~/repos/platypeeps/sd-ai-command-pack/bin/sd-db repo channels '{path}' 'obsidian=Briefs/x' 'drive='")
        self.assertEqual(out["R"]["title"], "channels none → obsidian=Briefs/x drive=")
        self.assertIn("sd-research-kit render", out["R"]["note"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/channels", {"path": path, "value": {"obsidian": {"folder": "Briefs/x"}, "drive": {}}, "before": "none"}),
                          ("/api/repos/channels", {"path": path, "value": None, "before": '{"drive":{},"obsidian":{"folder":"Briefs/x"}}'})])
        self.assertEqual(out["R"]["toast"], f"Ran locally · channels none → obsidian=Briefs/x drive= · {remedy}")
        self.assertEqual(out["R"]["left"], 0)
        self.assertEqual(out["toasts"][-1], ["Not proposed: no channel is checked. Clear setting removes the setting.", False])

    FEED = '{"url":"https://example.test/feed.xml"}'

    def stored_doc(self, text):
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])["channels"] = text
        return doc

    def test_the_channels_form_undoes_against_the_value_the_write_answered_when_a_feed_was_kept(self):
        """sd:4632: the library keeps the stored feed, so the row after the write is not the posted object. Undo posts the
        answered value as its `before` and writes back the whole old setting, feed included."""
        path = self.ids["checkout"]
        old = '{"drive":{},"feed":' + self.FEED + '}'
        kept = '{"feed":' + self.FEED + ',"obsidian":{}}'
        out = self.run_page(self.FORM + """fill('obsidian', true, ''); fill('notion', false, ''); fill('drive', false, '');
propose('channels', 'on'); C.run(cmd('sddb.run'), pending[0]); await flush(); await lastToast().undo(); await flush();""",
                            doc=self.stored_doc(old), search=f"?repo={path}",
                            answer="(p, b) => [200, { value: " + json.dumps(kept) + " }]")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/channels", {"path": path, "value": {"obsidian": {}}, "before": old}),
                          ("/api/repos/channels", {"path": path, "value": {"drive": {}, "feed": json.loads(self.FEED)}, "before": kept})])

    def test_a_form_that_matches_the_stored_channels_proposes_nothing_and_a_feed_only_row_has_no_undo(self):
        """sd:4632: the stored feed is not part of the form's value, so an unchanged form is no change; a row that held
        only a feed cannot be put back by a set, so that write offers no Undo rather than one that drops the feed."""
        path = self.ids["checkout"]
        out = self.run_page(self.FORM + """fill('obsidian', false, ''); fill('notion', false, ''); fill('drive', true, '');
propose('channels', 'on'); R.same = pending.length;""",
                            doc=self.stored_doc('{"drive":{},"feed":' + self.FEED + '}'), search=f"?repo={path}")
        self.assertEqual(out["R"]["same"], 0)
        out = self.run_page(self.FORM + """fill('obsidian', true, ''); fill('notion', false, ''); fill('drive', false, '');
propose('channels', 'on'); C.run(cmd('sddb.run'), pending[0]); await flush(); await lastToast().undo(); await flush();""",
                            doc=self.stored_doc('{"feed":' + self.FEED + '}'), search=f"?repo={path}",
                            answer="(p, b) => [200, { value: '{}' }]")
        self.assertEqual(len(out["posts"]), 1)
        self.assertIn("nothing to reverse", out["toasts"][-1][0])

    def test_the_channels_form_shows_a_stored_feed_as_text_and_offers_no_field_for_it(self):
        """sd:4192: an inbound key the row holds shows beside the channels, display only; a feed alone is no outbound
        setting, so the defaults show and every channel box stays on."""
        path = self.ids["checkout"]
        both = self.run_page("R.page = ELS['view-repos'].html;", search=f"?repo={path}",
                             doc=self.stored_doc('{"drive":{},"feed":' + self.FEED + '}'))["R"]["page"]
        alone = self.run_page("R.page = ELS['view-repos'].html;", search=f"?repo={path}",
                              doc=self.stored_doc('{"feed":' + self.FEED + '}'))["R"]["page"]
        for page in (both, alone):
            self.assertIn("Inbound feed", page)
            self.assertIn("<code>https://example.test/feed.xml</code>", page)
            self.assertNotIn('data-chf="feed"', page)
            self.assertNotIn('data-ch="feed"', page)
        self.assertIn("drive Briefs/", both)
        self.assertEqual(len(re.findall(r'data-ch="\w+" checked>', both)), 1)
        self.assertIn('<span class="no">none · defaults</span>', alone)
        self.assertEqual(len(re.findall(r'data-ch="\w+" checked>', alone)), 3)
        plain = self.run_page("R.page = ELS['view-repos'].html;", search=f"?repo={path}")["R"]["page"]
        self.assertNotIn("Inbound", plain)

    def test_the_email_aliases_field_proposes_the_verb_posts_only_email_and_undoes(self):
        """sd:4191: the repo's mail aliases are their own field. The write names only `email`, so the library keeps the
        channels and the other inbound keys; Undo writes the whole old setting back; an unchanged or emptied field
        proposes nothing, since only Clear setting removes the key."""
        path = self.ids["checkout"]
        old = '{"drive":{},"email":{"aliases":["acme"]}}'
        new = '{"drive":{},"email":{"aliases":["acme","kiln"]}}'
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('mail_aliases', 'kiln, acme'); R.cmd = pending[0].cmd;
R.title = pending[0].title; C.run(cmd('sddb.run'), pending[0]); await flush(); await lastToast().undo(); await flush();
propose('mail_aliases', 'acme'); R.same = pending.length; propose('mail_aliases', ''); R.empty = pending.length;""",
                            doc=self.stored_doc(old), search=f"?repo={path}", answer="(p, b) => [200, { value: " + json.dumps(new) + " }]")
        page = out["R"]["page"]
        self.assertIn('data-key="mail_aliases"', page)
        self.assertIn('data-set="mail_aliases" value="acme"', page)
        self.assertNotIn("Inbound email", page)
        self.assertEqual(out["R"]["cmd"], f"~/repos/platypeeps/sd-ai-command-pack/bin/sd-db repo channels '{path}' 'email=acme,kiln'")
        self.assertEqual(out["R"]["title"], "email aliases acme → acme,kiln")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/channels", {"path": path, "value": {"email": {"aliases": ["acme", "kiln"]}}, "before": old}),
                          ("/api/repos/channels", {"path": path, "value": json.loads(old), "before": new})])
        self.assertEqual((out["R"]["same"], out["R"]["empty"]), (0, 0))
        self.assertEqual(out["toasts"][-1], ["Not proposed: an alias field cannot be emptied alone. Clear setting removes the whole setting.", False])

    def test_aliases_on_a_row_with_no_setting_undo_to_none_and_a_row_without_aliases_has_no_undo(self):
        """sd:4191: a set that names `email` over NULL is undone by clearing; over a setting without aliases, writing the
        old setting back would keep the new aliases, so that write offers no Undo."""
        path = self.ids["checkout"]
        out = self.run_page("""propose('mail_aliases', 'acme'); C.run(cmd('sddb.run'), pending[0]); await flush();
await lastToast().undo(); await flush();""", search=f"?repo={path}", answer="(p, b) => [200, { value: '{\"email\":{\"aliases\":[\"acme\"]}}' }]")
        self.assertEqual([b["value"] for _, b, _ in out["posts"]], [{"email": {"aliases": ["acme"]}}, None])
        out = self.run_page("""propose('mail_aliases', 'acme'); C.run(cmd('sddb.run'), pending[0]); await flush();
await lastToast().undo(); await flush();""", doc=self.stored_doc('{"drive":{}}'), search=f"?repo={path}",
                            answer="(p, b) => [200, { value: '{}' }]")
        self.assertEqual(len(out["posts"]), 1)
        self.assertIn("nothing to reverse", out["toasts"][-1][0])

    def test_a_channels_value_the_library_refuses_shows_why_and_clear_writes_none_without_an_undo(self):
        # sd:3465: such a value is not read as the defaults; it cannot be written back, so the clear has no Undo.
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == path)
        row.update(channels='{"site":{}}', channels_problem="'site' is not a channel; expected obsidian, notion or drive")
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('channels', null); R.title = pending[0].title;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            doc=doc, search=f"?repo={path}", answer="(p, b) => [200, {}]")
        page = out["R"]["page"]
        self.assertIn("Cannot be used: &#39;site&#39; is not a channel", page)
        self.assertIn("data-ch-clear", page)
        self.assertNotIn(" checked>", page.split('id="ch-form"')[1].split("</fieldset>")[0])
        self.assertEqual(out["R"]["title"], 'channels {"site":{}} → none')
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/repos/channels", {"path": path, "value": None, "before": '{"site":{}}'})])
        self.assertEqual(out["R"]["toast"], 'Ran locally · channels {"site":{}} → none')
        self.assertEqual(out["toasts"][-1], ['Run not undone · channels {"site":{}} → none: nothing to reverse', False])

    def test_the_repo_page_shows_github_issues_and_the_toggle_runs_the_verb_and_undoes(self):
        """sd:4840: on or off, written through `sd-db repo github-issues`; an unread setting shows no toggle."""
        path = self.ids["checkout"]
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('github_issues', 'on'); R.cmd = pending[0].cmd;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            search=f"?repo={path}", answer="(p, b) => [200, {}]")
        self.assertIn('data-set="github_issues"', out["R"]["page"])
        self.assertEqual(out["R"]["cmd"], f"~/repos/platypeeps/sd-ai-command-pack/bin/sd-db repo github-issues '{path}' on")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/github-issues", {"path": path, "value": "on", "before": "off"}),
                          ("/api/repos/github-issues", {"path": path, "value": "off", "before": "on"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · github_issues off → on")
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["github_issues"] = None
        page = self.run_page("R.page = ELS['view-repos'].html;", doc=doc, search=f"?repo={path}")["R"]["page"]
        cell = page.split('data-key="github_issues"')[1].split('<div class="row"')[0]
        self.assertIn("not read", cell)
        self.assertNotIn('data-set="github_issues"', cell)

    def test_the_repo_page_shows_dependabot_and_the_toggle_runs_the_verb_and_undoes(self):
        """sd:3984: yes or no for a repo outside the owner rule; an owner's or a denied repo shows the rule and no toggle."""
        path = self.ids["checkout"]
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('dependabot', 'yes'); R.cmd = pending[0].cmd;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            search=f"?repo={path}", answer="(p, b) => [200, {}]")
        self.assertIn('data-set="dependabot"', out["R"]["page"])
        self.assertEqual(out["R"]["cmd"], f"~/repos/platypeeps/sd-ai-command-pack/bin/sd-db repo dependabot '{path}' yes")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/dependabot", {"path": path, "value": "yes", "before": "no"}),
                          ("/api/repos/dependabot", {"path": path, "value": "no", "before": "yes"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · dependabot no → yes")
        doc = json.loads(json.dumps(self.doc))
        for gate, shown in (("owner", "always (owner)"), ("denied", "denied by name"), (None, "not read")):
            row = next(r for r in doc["repos"] if r["path"] == path)
            row["dependabot_gate"] = gate
            row["dependabot"] = "no" if gate else None
            page = self.run_page("R.page = ELS['view-repos'].html;", doc=doc, search=f"?repo={path}")["R"]["page"]
            cell = page.split('data-key="dependabot"')[1].split('<div class="row"')[0]
            self.assertIn(shown, cell)
            self.assertNotIn('data-set="dependabot"', cell)

    def test_a_refused_write_says_why_and_reads_again(self):
        path = self.ids["checkout"]
        out = self.run_page(f"C.run(cmd('repo.managed'), C.get('repo:{path}')); await flush();",
                            answer="(p, b) => [409, {error: 'managed is yes now, not no'}]")
        self.assertEqual(out["toasts"][-1], [f"{path} not changed: managed is yes now, not no", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])

    def test_a_failed_job_retries_through_its_route_and_an_idle_one_is_off(self):
        revision = next(j for j in self.doc["jobs"] if j["name"] == "nightly-sync")["revision"]
        out = self.run_page("""C.run(cmd('jobs.retry'), C.get('cron:nightly-sync')); await flush();
C.run(cmd('jobs.retry'), C.get('cron:quiet')); await flush();""",
                            answer="(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/jobs/nightly-sync/retry", {"revision": revision})])
        self.assertEqual(out["toasts"][0], ["Retry started · nightly-sync", False])
        self.assertTrue(out["toasts"][1][0].startswith("off: "), out["toasts"])


    def test_after_exit_127_retry_asks_first_naming_the_exit_and_print_is_the_rows_act(self):
        """sd:3392, ruled 2026-09-30: the exit is the last run's, and a path fixed outside the dashboard needs Retry to test it."""
        doc = json.loads(json.dumps(self.doc))
        next(j for j in doc["jobs"] if j["name"] == "nightly-sync")["last_exit"] = 127
        out = self.run_page("""const c = cmd('jobs.retry'), o = C.get('cron:nightly-sync');
R.on = c.when(o); R.primary = !!c.primary(o); R.print = !!cmd('jobs.print').primary(o); R.ask = !!c.askFirst(o); R.says = c.consequence(o);
C.run(c, o); await flush();""", answer="(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]", doc=doc)
        self.assertEqual((out["R"]["on"], out["R"]["primary"], out["R"]["print"], out["R"]["ask"]), (True, False, True, True))
        self.assertIn("exit 127", out["R"]["says"])
        self.assertEqual(out["confirms"], ["jobs.retry"])
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/jobs/nightly-sync/retry"])
        self.assertEqual(out["toasts"], [["Retry started · nightly-sync", False]])

    def test_after_another_exit_retry_is_the_rows_act_and_does_not_ask(self):
        out = self.run_page("""const c = cmd('jobs.retry'), o = C.get('cron:nightly-sync');
R.got = [!!c.primary(o), !!cmd('jobs.print').primary(o), !!c.askFirst(o)];""")
        self.assertEqual(out["R"]["got"], [True, False, False])

    def test_a_retry_shows_launchctls_answer_when_it_did_not_accept(self):
        """sd:3388: a 200 carries launchctl's answer; only `accepted` started a retry."""
        cases = {
            "(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]": "Retry started · nightly-sync",
            "(p, b) => [200, {request: {status: 'failed', message: 'launchctl refused the request (exit 3)'}}]":
                "nightly-sync not retried: launchctl refused the request (exit 3)",
            "(p, b) => [200, {request: {status: 'unknown', message: 'launchctl timed out; the request outcome is unknown'}}]":
                "Not known whether nightly-sync was retried: launchctl timed out; the request outcome is unknown",
        }
        for answer, said in cases.items():
            with self.subTest(said=said):
                out = self.run_page("C.run(cmd('jobs.retry'), C.get('cron:nightly-sync')); await flush();", answer=answer)
                self.assertEqual(out["toasts"], [[said, False]])

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
        self.assertEqual(out["R"]["group"], ["Switch runner-merge · 1 repo · 1 of 2 refused stale, read again: /repos/system"])
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

    def test_a_job_with_no_calendar_is_manual(self):
        """sd:4965: `JOB_SCHEDULE="manual"` installs a plist with no StartCalendarInterval; it runs only when started."""
        out = self.run_page("R.got = [human([]), nextRun([])];")
        self.assertEqual(out["R"]["got"], ["manual", ""])


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

    def test_the_lane_strip_names_no_runner_and_no_archive_refresh(self):
        """sd:3041: the runner stopped; the strip drew its heartbeat and its archive refresh."""
        out = self.run_page("R.lane = ELS['view-lane'].html;", search="?view=lane")
        self.assertNotIn('<span class="label">Runner</span>', out["R"]["lane"])
        self.assertNotIn('<span class="label">Archive refresh</span>', out["R"]["lane"])


class TheJobLogAndCancel(PageScript):
    """sd:3388: Management reads a job's log, cancels a running job, and filters and pages services."""

    def running_doc(self):
        jobs = JobsBackend(self.tmp.name, jobs=[("busy", "running", None, None), ("quiet", "idle", 0, None)])
        (jobs.cron_root / "logs" / "busy.log").write_text("[busy] started\nstep 2 <of> 3\n")
        return management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=jobs, services=NoServices(), grant=GRANTED)

    CANCEL = "C.run(cmd('jobs.cancel'), C.get('cron:busy')); await flush();"

    def test_a_running_job_cancels_through_its_route_with_its_revision_after_a_confirm_that_names_it(self):
        doc = self.running_doc()
        revision = next(j for j in doc["jobs"] if j["name"] == "busy")["revision"]
        out = self.run_page("R.why = cmd('jobs.cancel').consequence(C.get('cron:busy')); R.cli = cmd('jobs.cancel').cli(C.get('cron:busy'));"
                            + self.CANCEL + " C.run(cmd('jobs.cancel'), C.get('cron:quiet')); await flush();",
                            doc=doc, answer="(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]")
        self.assertEqual(out["confirms"], ["jobs.cancel"])
        self.assertIn("busy (PID 7)", out["R"]["why"])
        self.assertEqual(out["R"]["cli"], "sd jobs cancel busy")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/jobs/busy/cancel", {"revision": revision})])
        self.assertEqual(out["toasts"][0], ["Cancel sent · busy", False])
        self.assertEqual(out["toasts"][1], ["off: only a currently running job can cancel", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])

    def test_a_refusal_says_not_changed_and_any_other_failure_says_the_outcome_is_not_known(self):
        doc = self.running_doc()
        cases = {
            "(p, b) => [409, {error: 'state changed; refresh before trying again'}]": "busy not changed: state changed; refresh before trying again",
            "(p, b) => [400, {error: 'only a currently running job can cancel'}]": "busy not changed: only a currently running job can cancel",
            "(p, b) => [502, {error: 'the hub did not answer'}]": "Not known whether busy was cancelled: the hub did not answer.",
            "(p, b) => Promise.reject(new TypeError('Load failed'))": "Not known whether busy was cancelled: Load failed.",
            "(p, b) => [200, {request: {status: 'failed', message: 'launchctl refused the request (exit 3)'}}]":
                "busy not cancelled: launchctl refused the request (exit 3)",
            "(p, b) => [200, {request: {status: 'unknown', message: 'launchctl timed out; the request outcome is unknown'}}]":
                "Not known whether busy was cancelled: launchctl timed out; the request outcome is unknown",
        }
        for answer, said in cases.items():
            with self.subTest(said=said):
                out = self.run_page(self.CANCEL, doc=doc, answer=answer)
                self.assertTrue(out["toasts"][0][0].startswith(said), out["toasts"])
                # A 400 wrote nothing and reads nothing; every other answer reads the jobs again.
                self.assertEqual(len(out["gets"]), 1 if said.endswith("can cancel") else 2, out["gets"])

    def test_the_details_show_the_end_of_the_log_and_the_last_request(self):
        doc = self.running_doc()
        busy = next(j for j in doc["jobs"] if j["name"] == "busy")
        busy["last_request"] = {"action": "cancel", "status": "accepted", "at": "2026-09-06T02:20:00Z", "who": "dashboard",
                                "message": "launchctl accepted the request"}
        quiet = next(j for j in doc["jobs"] if j["name"] == "quiet")
        quiet["log"] = {"state": "unread", "text": "", "cut": False, "reason": "the log is not a regular file"}
        out = self.run_page("selectRow('cron:busy', false); R.busy = ELS.details.html; selectRow('cron:quiet', false); R.quiet = ELS.details.html;"
                            " R.shown = cmd('jobs.log').run(C.get('cron:busy')); R.after = ELS.details.html;", doc=doc, search="?view=schedules")
        self.assertIn('<pre class="code" tabindex="0" aria-label="busy log">[busy] started\nstep 2 &lt;of&gt; 3\n</pre>', out["R"]["busy"])
        self.assertIn("local-cron-jobs/logs/busy.log", out["R"]["busy"])
        self.assertIn("<dt>Last request</dt><dd>cancel · accepted · dashboard · 2026-09-06T02:20:00Z · launchctl accepted the request</dd>",
                      out["R"]["busy"])
        self.assertIn("The log was not read", out["R"]["quiet"])
        self.assertIn("the log is not a regular file", out["R"]["quiet"])
        self.assertIn("<dt>Last request</dt><dd>none recorded</dd>", out["R"]["quiet"])
        self.assertEqual(out["R"]["shown"], "busy's log is in Details")
        self.assertIn("busy log", out["R"]["after"])

    def services(self, n):
        """The document with n services: every third one failed, the rest running."""
        rows = [{"name": f"svc-{i:02d}", "label": f"local.example.svc-{i:02d}", "scope": "user", "domain": "gui/501",
                 "category": "tool", "state": "failed" if i % 3 == 0 else "running", "pid": None, "last_exit": None,
                 "last_signal": None, "revision": "0" * 64, "capabilities": {}} for i in range(n)]
        return dict(self.doc, services=rows)

    def test_services_page_through_the_shells_pager_and_keep_the_page_in_the_url(self):
        click = "ELS['view-deploys'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
        out = self.run_page("R.a = ELS['view-deploys'].html;" + click % ("'.list-pager [data-page]'", "{ page: '2' }")
                            + " R.b = ELS['view-deploys'].html;", doc=self.services(30), search="?view=deploys")
        self.assertIn('<span class="range">1–25 of 30</span>', out["R"]["a"])
        self.assertIn('<span class="range">26–30 of 30</span>', out["R"]["b"])
        self.assertIn("svc-29", out["R"]["b"])
        self.assertNotIn("svc-00", out["R"]["b"])
        self.assertIn("view=deploys&page=2", out["qs"])

    def test_services_filter_by_a_state_chip_and_by_any_column(self):
        click = "ELS['view-deploys'].listeners.click[0]({ target: { closest: s => s === '[data-svc-state]' ? { dataset: { svcState: 'failed' } } : null } });"
        out = self.run_page("R.a = ELS['view-deploys'].html;" + click + " R.b = ELS['view-deploys'].html; R.box = ELS.shift.value;",
                            doc=self.services(30), search="?view=deploys")
        self.assertEqual(re.findall(r'data-svc-state="(\w+)"', out["R"]["a"]), ["failed", "running"])
        self.assertIn("10 of 30", out["R"]["b"])
        self.assertEqual(out["R"]["box"], "state:failed")
        self.assertEqual(out["qs"][-1], "view=deploys&q=state%3Afailed")
        out = self.run_page("R.a = ELS['view-deploys'].html;", doc=self.services(30), search="?view=deploys&q=gui/501 svc-1")
        self.assertIn("10 of 30", out["R"]["a"])
        self.assertIn("svc-19", out["R"]["a"])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "management", "Management", "/management", ("/api/management",)
