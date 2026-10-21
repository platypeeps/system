"""The v2 Activity page (sd:2111).

What this slice promises: `/activity` answers under the shared policy and
loads its script before `shell.js`; `/api/activity` is one 24-hour timeline
built from records the library already keeps -- the delivery notes
`ship.note_merge` writes, runner assignments with their queue revision,
launchd jobs placed at their log time -- plus the execution journal
`runner_exec.executions` lists, older records too. A kind whose every source
failed to read is unknown, with the reason, rather than zero; the design's
deploy and mail kinds, which nothing records, are left out. The page follows
the design source at d82daa1.

`activity.js` runs under JavaScriptCore (osascript), against the stand-in
page and shell `test_v2_tasks` uses, with the real reader (`read.js`,
sd:2418; adopted by sd:2489) and the document above as its fetch answer. The browser half -- focus, the look at 375 px, the lane chart -- is a
manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db.ship import note_merge
from sd_dashboard import activity_screen, server, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar

V2 = Path(v2.__file__).resolve().parent
ACTIVITY_JS = (V2 / "static" / "activity.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")

INSIDE = "2026-09-06T09:30:00Z"   # 2.5 hours before NOW
OUTSIDE = "2026-09-05T08:00:00Z"  # 28 hours before NOW


def stamp(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def seed(case):
    """A merge in the window and one before it, three assignments, four journal records, on the fixture database."""
    repo = case.repo()
    port = case.item("Port the page", kind="work", repo=repo)
    old = case.item("An old slice", kind="work", repo=repo)
    note_merge(case.connection, port, {"pull_request": {"url": "https://github.com/example-org/system/pull/41"},
                                       "merge_commit": "1acdcf6" + "0" * 33, "reviewed_head": "b0d1e5" + "0" * 34,
                                       "review_selection": {"requested_provider": None, "reviewed_by": ["codex"]},
                                       "review_clearance": None})
    note_merge(case.connection, old, {"pull_request": {"url": "https://github.com/example-org/system/pull/7"},
                                      "merge_commit": "abcdef1" + "0" * 33})
    case.note(port, "Code delivery is a phrase, not a record", kind="comment")
    notes = [row[0] for row in case.connection.execute("SELECT id FROM note WHERE body LIKE 'Code delivery%' ORDER BY id")]
    blocked = case.assignment(port, status="blocked")
    done = case.assignment(port, status="done")
    stale = case.assignment(old, status="done")
    exec_row = case.assignment(port, role="exec", status="done")
    ok_run = case.note(port, "{}", kind="exec", started=INSIDE, ended=INSIDE, exit_code=0, session="dashboard")
    bad_run = case.note(port, "{}", kind="exec", started=INSIDE, ended=INSIDE, exit_code=2, session="dashboard")
    stopped = case.note(port, "{}", kind="exec", started=INSIDE, ended=INSIDE, session="dashboard")
    older = case.note(old, "{}", kind="exec", started=OUTSIDE, ended=OUTSIDE, exit_code=0, session="dashboard")
    for note, command, at in ((ok_run, "status", INSIDE), (bad_run, "prune", INSIDE), (stopped, "sync", INSIDE), (older, "status", OUTSIDE)):
        body = {"version": 1, "note": note, "command": command, "scope": "item"}
        case.connection.execute("UPDATE note SET body = ?, timestamp = ? WHERE id = ?", (json.dumps(body), at, note))
    runner_note = case.note(port, "clone busy; lease retained", kind="exec", started=INSIDE, ended=INSIDE, session="runner")
    case.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", (INSIDE, runner_note))
    case.note(port, "not a record anyone writes", kind="exec", started=INSIDE, ended=INSIDE, session="dashboard")
    # Seeding, as support.ScreenCase.age does: the library writes the real clock, and the fixture's clock is NOW.
    case.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", (INSIDE, notes[0]))
    case.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", (OUTSIDE, notes[1]))
    case.connection.execute("UPDATE note SET timestamp = ? WHERE body LIKE 'Code delivery is%'", (INSIDE,))
    case.connection.execute("UPDATE assignment SET started = ?, ended = ? WHERE id IN (?, ?, ?)",
                            ("2026-09-06T08:00:00Z", INSIDE, blocked, done, exec_row))
    case.connection.execute("UPDATE assignment SET started = ?, ended = ? WHERE id = ?", (OUTSIDE, OUTSIDE, stale))
    case.connection.commit()
    return {"port": port, "old": old, "merge": notes[0], "blocked": blocked, "done": done, "stale": stale,
            "exec": exec_row, "ok_run": ok_run, "bad_run": bad_run, "stopped": stopped, "older": older, "runner": runner_note}


def backend(case, *, refuse=""):
    jobs = JobsBackend(case.tmp.name, jobs=[("nightly-sync", "failed", 7, None), ("backup", "idle", 0, None),
                                            ("weekly", "idle", 0, None), ("lost-log", "failed", 1, None),
                                            ("stopped", "interrupted", None, 15), ("lost-stop", "interrupted", None, 15)],
                       refuse=refuse)
    jobs.log("nightly-sync", stamp(INSIDE))
    jobs.log("backup", stamp("2026-09-06T02:00:00Z"))
    jobs.log("weekly", stamp(OUTSIDE))
    jobs.log("stopped", stamp(INSIDE))
    return jobs


class TheDocument(ScreenCase):
    """`activity_screen.document` builds the timeline from library records only."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self))
        self.by_id = {event["id"]: event for event in self.doc["events"]}

    def test_the_window_is_the_24_hours_before_the_read(self):
        self.assertEqual((self.doc["read"], self.doc["from"], self.doc["to"]),
                         (NOW, "2026-09-05T12:00:00Z", NOW))
        self.assertEqual(self.doc["kinds"], ["merge", "run", "review", "command"])
        stamps = [event["at"] for event in self.doc["events"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_a_merge_is_a_delivery_note_in_the_window(self):
        merges = [event for event in self.doc["events"] if event["k"] == "merge"]
        self.assertEqual([event["id"] for event in merges], [f"merge:{self.ids['merge']}"])
        merge = merges[0]
        self.assertEqual((merge["at"], merge["s"], merge["repo"], merge["owner"], merge["pr"], merge["item"], merge["what"]),
                         (INSIDE, "ok", "system", "example-org", 41, self.ids["port"], "Port the page"))
        self.assertEqual(merge["url"], "https://github.com/example-org/system/pull/41")
        self.assertEqual(merge["ref"], "system#41")

    def test_a_run_carries_its_state(self):
        blocked = self.by_id[f"run:{self.ids['blocked']}"]
        self.assertEqual((blocked["s"], blocked["status"], blocked["n"], blocked["item"]),
                         ("caution", "blocked", self.ids["blocked"], self.ids["port"]))
        self.assertEqual(self.by_id[f"run:{self.ids['done']}"]["s"], "ok")
        self.assertNotIn(f"run:{self.ids['stale']}", self.by_id)
        self.assertNotIn(f"run:{self.ids['exec']}", self.by_id, "an exec assignment is the command kind's")

    def test_a_run_carries_its_items_repository_under_the_label_merges_use(self):
        merge = self.by_id[f"merge:{self.ids['merge']}"]
        self.assertEqual(self.by_id[f"run:{self.ids['blocked']}"]["repo"], merge["repo"])
        self.assertEqual(merge["repo"], "system")

    def test_a_job_is_placed_at_its_log_time_and_a_failed_one_without_a_log_says_so(self):
        failed = self.by_id["job:nightly-sync"]
        self.assertEqual((failed["k"], failed["at"], failed["s"], failed["failed"], failed["what"]),
                         ("run", INSIDE, "warning", True, "nightly-sync failed with exit 7"))
        self.assertTrue(failed["retry"]["allowed"])
        self.assertEqual(failed["exit"], 7, "the exit code Retry's 127 guard reads (sd:2415)")
        self.assertEqual(self.by_id["job:backup"]["s"], "ok")
        self.assertTrue(self.by_id["job:backup"]["retry"]["allowed"], "an idle job may run now (sd:4982)")
        self.assertNotIn("job:weekly", self.by_id)
        self.assertNotIn("job:lost-log", self.by_id)
        self.assertEqual(self.doc["undated"], ["lost-log", "lost-stop"])

    def test_an_interrupted_job_is_a_caution_that_can_retry(self):
        stopped = self.by_id["job:stopped"]
        self.assertEqual((stopped["s"], stopped["failed"], stopped["interrupted"]), ("caution", True, True))
        self.assertRegex(stopped["what"], r"^stopped was interrupted \(.+\)$")
        self.assertTrue(stopped["retry"]["allowed"])

    def test_the_journal_states_its_cap_and_counts_the_notes_it_did_not_read(self):
        self.assertEqual((self.doc["journal_cap"], self.doc["journal_unread"]), (activity_screen.JOURNAL_CAP, 0))
        with patch.object(activity_screen, "JOURNAL_CAP", 3):
            doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self))
        self.assertEqual((doc["journal_cap"], doc["journal_unread"]), (3, 3))
        self.assertEqual(sum(1 for e in doc["events"] if e["k"] == "command") + sum(doc["journal_skipped"].values()), 3)

    def test_a_command_is_a_journal_record_and_older_ones_come_too(self):
        ok_run, bad_run = self.by_id[f"cmd:{self.ids['ok_run']}"], self.by_id[f"cmd:{self.ids['bad_run']}"]
        self.assertEqual((ok_run["s"], ok_run["what"], ok_run["note"], ok_run["item"], ok_run["exit"], ok_run["ref"]),
                         ("ok", "status · exit 0", self.ids["ok_run"], self.ids["port"], 0, f"note {self.ids['ok_run']}"))
        self.assertEqual((bad_run["s"], bad_run["what"]), ("warning", "prune · exit 2"))
        stopped = self.by_id[f"cmd:{self.ids['stopped']}"]
        self.assertEqual((stopped["s"], stopped["what"]), ("caution", "sync · no exit code"))
        older = self.by_id[f"cmd:{self.ids['older']}"]
        self.assertEqual((older["at"], older["title"]), (OUTSIDE, "An old slice"))
        runner_run = self.by_id[f"cmd:{self.ids['runner']}"]
        self.assertEqual((runner_run["source"], runner_run["what"], runner_run["detail"], runner_run["s"]),
                         ("runner", "runner · no exit code", "clone busy; lease retained", "caution"))
        self.assertEqual(self.doc["journal_skipped"], {"unknown writer": 1})
        self.assertFalse(any(e["at"] < self.doc["from"] for e in self.doc["events"] if e["k"] != "command"),
                         "only the journal reaches before the window")

    def test_deploy_and_mail_are_not_kinds(self):
        # No repository records GitHub deployments, and the operator ruled out a mail reader (sd:2212).
        for kind in ("deploy", "mail"):
            self.assertNotIn(kind, activity_screen.KINDS)
            self.assertNotIn(kind, self.doc["kinds"])
        self.assertNotIn("unknown", self.doc, "no kind is unknown by design; only a failed source is")
        self.assertEqual(self.doc["sources"], {"merge": "", "review": "", "run": "", "job": "", "command": ""})

    # --- reviews: the review each merge carried (operator ruling on sd:2211) ---

    def test_a_review_is_the_one_a_merge_in_the_window_carried(self):
        reviews = [event for event in self.doc["events"] if event["k"] == "review"]
        self.assertEqual([event["id"] for event in reviews], [f"review:{self.ids['merge']}"])
        review = reviews[0]
        self.assertEqual((review["at"], review["s"], review["repo"], review["ref"], review["pr"], review["item"]),
                         (INSIDE, "ok", "system", "system#41", 41, self.ids["port"]))
        self.assertEqual((review["reviewers"], review["requested"], review["clearance"], review["head"]),
                         (["codex"], None, None, "b0d1e5" + "0" * 34))
        self.assertEqual(review["what"], "codex reviewed system#41")
        self.assertEqual(review["detail"], "clean or advisory · head b0d1e5000000")
        self.assertEqual(self.doc["review_unrecorded"], 0, "the old merge is outside the window; the phrase is no delivery")

    def deliver(self, number, evidence):
        item = self.item(f"Slice {number}", kind="work", repo=self.repo())
        note_merge(self.connection, item, {"pull_request": {"url": f"https://github.com/example-org/system/pull/{number}"},
                                           "merge_commit": f"{number:07d}" + "0" * 33, **evidence})
        self.connection.execute("UPDATE note SET timestamp = ? WHERE body LIKE ?", (INSIDE, f"Code delivery %/pull/{number} %"))
        self.connection.commit()
        doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self))
        return doc, {event["id"]: event for event in doc["events"] if event["k"] == "review" and event["pr"] == number}

    def test_blocking_findings_whose_dispositions_were_accepted_are_a_caution(self):
        doc, found = self.deliver(52, {"review_selection": {"requested_provider": "codex", "reviewed_by": ["codex"]},
                                       "review_clearance": {"kind": "adjudicated", "key": "ship-adjudication:x", "revision": 3}})
        (review,) = found.values()
        self.assertEqual((review["s"], review["clearance"], review["requested"]), ("caution", "adjudicated", "codex"))
        self.assertIn("accepted their dispositions", review["verdict"])
        self.assertEqual(review["detail"], review["verdict"], "no reviewed head recorded, so none is named")

    def test_a_clearance_this_page_does_not_know_is_unknown_not_clean(self):
        _, found = self.deliver(53, {"review_selection": {"reviewed_by": ["kimi"]}, "review_clearance": {"kind": "waived"}})
        (review,) = found.values()
        self.assertEqual(review["s"], "unknown")
        self.assertIn("'waived'", review["verdict"])

    def test_a_merge_with_no_review_record_is_counted_not_drawn(self):
        for number, evidence in ((54, {}), (55, {"review_selection": {"reviewed_by": []}})):
            doc, found = self.deliver(number, evidence)
            self.assertEqual(found, {})
        self.assertEqual(doc["review_unrecorded"], 2)
        self.assertEqual(sum(1 for e in doc["events"] if e["k"] == "merge"), 3, "each is still a merge")

    def test_a_review_read_that_fails_is_named_and_merges_still_answer(self):
        with patch.object(activity_screen, "reviews", side_effect=sqlite3.OperationalError("database locked")):
            doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self))
        self.assertEqual(doc["sources"]["review"], "database locked")
        self.assertEqual((doc["review_unrecorded"], sum(1 for e in doc["events"] if e["k"] == "merge")), (0, 1))
        self.assertFalse(any(e["k"] == "review" for e in doc["events"]))

    def test_a_source_that_fails_is_named_and_the_others_still_answer(self):
        doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self, refuse="launchctl did not answer"))
        self.assertEqual(doc["sources"]["job"], "launchctl did not answer")
        self.assertEqual({event["k"] for event in doc["events"]}, {"merge", "review", "run", "command"})
        self.assertFalse(any(event["id"].startswith("job:") for event in doc["events"]))


class ThePage(BrowserSession):
    def setUp(self):
        # `backend` is the server's `operations_backend`, which Activity reads for jobs; no launchctl call is made.
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.backend = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        super().setUp()
        seed(self)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/activity")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Activity · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "commands.js", "activity.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/activity")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/activity?kind=merge", headers=cookie)[0], 400)
        status, _, body = self.request("/api/activity", headers=cookie)
        self.assertEqual(status, 200)
        self.assertIn("events", json.loads(body))


#: A retry's 200 carries launchctl's answer (operations._act); only `accepted` started the run.
ACCEPTED = "(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]"

PAGE = r"""
C.select = () => {}; C.pick = () => {};
window.shell.row = () => null; window.shell.setContext = () => {}; window.shell.openChat = () => {}; window.shell.send = () => {};
window.shell.pages = { Tasks: '/tasks' };
OUT.urls = []; window.shell.url = p => OUT.urls.push([p.get('kind'), p.get('range')]); OUT.shown = []; window.shell.view = { show: id => OUT.shown.push(id), current: () => 'timeline', count(id, n) { if (id === 'timeline') OUT.timeline = n; } };
"""


class TheScript(ScreenCase):
    """activity.js against the document `activity_screen` builds for the seeded database."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self))

    def run_page(self, body, answer="null", doc=None, pre=""):
        """Load activity.js, fire DOMContentLoaded, run `body` (async), and return OUT plus what `body` set on R.

        `pre` runs before activity.js does, as a page address does."""
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + pre + "\n" + READ_SHELL
                  + f"\nconst DOC = {json.dumps(doc or self.doc)};\n"
                  + "const WRITE = " + answer + ";\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/activity') return DOC.status ? [DOC.status, { error: DOC.error }] : [200, DOC];
  return WRITE ? WRITE(path, body) : [404, { error: 'no answer' }];
};\n""" + COMMANDS_JS + ACTIVITY_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_command_is_registered_with_its_risk_key_and_run_path(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null,
  typeof c.executes === 'boolean' ? c.executes : null, typeof c.run === 'function', typeof c.undo === 'function', !!c.bulk]);""")
        self.assertEqual(out["R"]["reg"], [
            ["pr.open", "pull request", "safe", "o", None, True, False, False],
            ["pr.item", "pull request", "safe", "i", None, True, False, False],
            ["review.open", "review", "safe", "o", None, True, False, False],
            ["review.item", "review", "safe", "i", None, True, False, False],
            ["asg.get", "assignment", "safe", "o", False, True, False, False],  # copy only: Details show it (sd:3524)
            ["asg.item", "assignment", "safe", "i", None, True, False, False],
            ["jobs.retry", "job", "safe", "t", None, True, False, True],
            ["jobs.log", "job", "safe", "l", False, True, False, False],
            ["command.reconcile", "command", "safe", "r", None, True, False, False],
            ["command.output", "command", "safe", "o", None, True, False, False],
            ["command.item", "command", "safe", "i", None, True, False, False],
        ])

    def test_the_page_reads_the_document_once_and_draws_no_hatched_kind(self):
        out = self.run_page("R.lanes = ELS.lanes.html; R.ann = ELS.annunciator.html; R.rows = ELS.rows.html; R.sub = ELS.sub.html;"
                            " R.filters = ELS.filters.html; R.table = ELS['chart-table'].html;")
        self.assertEqual(out["gets"], ["/api/activity"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        lanes = out["R"]["lanes"]
        for kind in ("deploy", "mail"):
            for part in ("lanes", "ann", "filters"):
                self.assertNotIn(f'"{kind}"', out["R"][part], part)
        self.assertNotIn("Deploys", out["R"]["table"])
        self.assertNotIn('data-state="unknown"', out["R"]["ann"], "every source read, so no lamp is hatched")
        self.assertNotIn("track unknown", lanes)
        self.assertEqual(re.findall(r'class="lane-btn" type="button" data-kind="(\w+)"', lanes), ["merge", "run", "review", "command"])
        self.assertIn('data-kind="command" data-state="warning"', out["R"]["ann"])
        self.assertIn("Port the page", out["R"]["rows"])
        self.assertIn("nightly-sync failed with exit 7", out["R"]["rows"])
        inside = [e for e in self.doc["events"] if e["at"] >= self.doc["from"]]
        self.assertIn(f"{len(inside)} events", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 2, "what": "failed events in 24 h"})

    def test_an_old_link_to_deploys_or_mail_keeps_only_the_kinds_the_page_draws(self):
        # A saved view or bookmark from before the kinds were dropped: it must not filter every row away.
        out = self.run_page("R.rows = ELS.rows.html;", pre="location.search = '?kind=deploy,merge,mail';")
        self.assertEqual(out["urls"][-1], ["merge", None])
        self.assertIn("Port the page<small>", out["R"]["rows"])

    def test_a_kind_whose_every_source_failed_is_hatched_with_the_reason(self):
        with patch.object(activity_screen, "reviews", side_effect=sqlite3.OperationalError("database locked")):
            doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self))
        out = self.run_page("""R.ann = ELS.annunciator.html; R.lanes = ELS.lanes.html;
ELS.lanes.listeners.click[0]({ target: { closest: s => s === '.lane-btn' ? { dataset: { kind: 'review' } } : null } });
R.details = ELS.details.html;""", doc=doc)
        self.assertIn('<div class="cell" data-kind="review" data-state="unknown">', out["R"]["ann"])
        self.assertIn("▨ unknown · reviews not read", out["R"]["lanes"])
        self.assertIn("Reviews not read", out["R"]["details"])
        self.assertIn("The read failed: database locked.", out["R"]["details"])
        self.assertIn('data-kind="merge" data-state="', out["R"]["ann"])
        self.assertNotIn('data-kind="merge" data-state="unknown"', out["R"]["ann"], "merges read, so merges are not hatched")

    def test_a_failed_read_is_an_error_and_a_failed_source_is_partial(self):
        out = self.run_page("", doc={"status": 500, "error": "database locked"})
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("database locked", out["states"][-1]["text"])
        partial = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self, refuse="launchctl did not answer"))
        out = self.run_page("", doc=partial)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("job: launchctl did not answer", out["states"][-1]["text"])

    def test_the_kind_filter_narrows_the_rows(self):
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'kind', v: 'command' } } : null } });
R.rows = ELS.rows.html;""")
        self.assertIn(">status · exit 0<", out["R"]["rows"])
        self.assertNotIn("Port the page<small>", out["R"]["rows"])

    def test_run_now_is_on_for_an_idle_job_too_and_posts_its_revision(self):
        failed = next(e for e in self.doc["events"] if e["id"] == "job:nightly-sync")
        out = self.run_page("""R.off = cmd('jobs.retry').when(C.get('job:backup'));
R.cli = cmd('jobs.retry').cli(C.get('job:nightly-sync'));
R.sends = cmd('jobs.retry').sends ? cmd('jobs.retry').sends(C.get('job:nightly-sync')) : null;
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync')); await flush();""", ACCEPTED)
        # The capability is the server's answer (sd:3524); an idle job may run now (sd:4982).
        self.assertIs(out["R"]["off"], True)
        # The line to copy is the sd verb, which passes the operation gate and the audit; kickstart is only what it sends.
        self.assertEqual(out["R"]["cli"], "sd jobs retry nightly-sync")
        self.assertEqual(out["R"]["sends"], "launchctl kickstart fixture/nightly-sync")
        self.assertEqual(out["posts"], [["/api/jobs/nightly-sync/retry", {"revision": failed["revision"]}, 64]])
        self.assertEqual(out["toasts"], [["Run started · nightly-sync", False]])

    def test_show_log_opens_the_jobs_row_on_management_schedules_and_keeps_the_line_to_copy(self):
        # sd:2392, ruling #8769: the row on Schedules, filtered to the job so a fleet of more than 25 jobs still shows it.
        out = self.run_page("""window.shell.pages.Management = '/management';
R.cli = cmd('jobs.log').cli(C.get('job:nightly-sync'));
shellRun(cmd('jobs.log'), C.get('job:nightly-sync')); await flush(); R.href = location.href;""")
        self.assertEqual(out["R"].get("href"), "/management?q=nightly-sync&row=cron%3Anightly-sync#view=schedules")
        self.assertEqual(out["R"]["cli"], "local-cron-jobs/cron-jobs.sh logs nightly-sync")
        self.assertEqual(out["toasts"], [["nightly-sync opens in Management > Schedules", False]])
        self.assertEqual(out["posts"], [])

    def test_show_output_reads_the_execution_record_into_details(self):
        note = self.ids["bad_run"]
        answer = "(path) => [200, { state: 'finished', output: 'removed 3 worktrees', output_expired: null }]"
        out = self.run_page(f"shellRun(cmd('command.output'), C.get('cmd:{note}')); await flush(); R.details = ELS.details.html;",
                            answer)
        self.assertEqual(out["gets"], ["/api/activity", f"/api/executions/{note}?offset=0"])
        self.assertIn("removed 3 worktrees", out["R"]["details"])
        self.assertEqual(out["toasts"], [[f"Output of note {note} shown in Details", False]])
        self.assertEqual(out["posts"], [])

    def test_reviews_draw_as_a_kind_with_a_source_not_hatched(self):
        out = self.run_page("R.lanes = ELS.lanes.html; R.ann = ELS.annunciator.html; R.rows = ELS.rows.html;")
        self.assertRegex(out["R"]["lanes"], r'data-kind="review"[^>]*><svg[^>]*></svg><span>Reviews</span><span class="n">1</span>')
        self.assertIn('<button class="cell" type="button" data-kind="review" data-state="ok"', out["R"]["ann"])
        self.assertEqual(cell_grammar(self, out["R"]["ann"])[-1], ("button", None))  # the lamps, then Observed
        self.assertIn("<b>0</b> adjudicated</span> · <span class=\"ph\">1 clean</span>", out["R"]["ann"])
        self.assertNotIn("unrecorded", out["R"]["ann"])
        self.assertIn("codex reviewed system#41", out["R"]["rows"])

    def test_a_review_shows_who_reviewed_and_how_it_let_the_merge_through(self):
        review = f"review:{self.ids['merge']}"
        out = self.run_page("ELS.rows.listeners.click[0]({ target: { closest: s => s === 'tr[data-id]' ? { dataset: { id: '%s' } } : null } });"
                            "R.details = ELS.details.html; R.open = cmd('review.open').cli(C.get('%s'));" % (review, review))
        details = out["R"]["details"]
        for term, value in (("Reviewed by", "codex"), ("Verdict", "clean or advisory"), ("Requested", "no provider named"),
                            ("Reviewed head", "b0d1e5" + "0" * 34), ("Pull request", "system#41")):
            self.assertIn(f"<dt>{term}</dt><dd>{value}</dd>", details)
        self.assertIn("the merge it let through", details)
        self.assertEqual(out["R"]["open"], "gh pr view 41 --repo example-org/system --web")

    def test_merges_with_no_review_record_are_said_on_the_lamp(self):
        doc = dict(self.doc, review_unrecorded=2)
        out = self.run_page("R.ann = ELS.annunciator.html;", doc=doc)
        self.assertIn("1 clean · 2 merges unrecorded", re.sub(r"<[^>]+>", "", out["R"]["ann"]))

    def test_a_lamp_its_chip_and_its_lane_add_to_one_kind_set(self):
        click = "ELS.%s.listeners.click[0]({ target: { closest: s => s === '%s' ? { dataset: { kind: '%s' } } : null } });"
        out = self.run_page(click % ("lanes", ".lane-btn", "run") + click % ("annunciator", "button.cell", "merge")
                            + "R.ann = ELS.annunciator.html; R.lanes = ELS.lanes.html; R.filters = ELS.filters.html;")
        self.assertEqual(out["urls"][-1], ["run,merge", None], "a lamp adds to the set, never replaces it")
        for kind in ("merge", "run"):
            self.assertIn(f'data-kind="{kind}" data-state="', out["R"]["ann"])
            self.assertRegex(out["R"]["ann"], rf'data-kind="{kind}" data-state="\w+" aria-pressed="true"')
            self.assertRegex(out["R"]["lanes"], rf'data-kind="{kind}" aria-pressed="true"')
            # The bar shows each picked kind as a removable token; the lamps and lanes are its pressable controls.
            self.assertRegex(out["R"]["filters"], rf'<button type="button" class="tok" data-f="kind" data-v="{kind}" aria-label="Remove filter: ')
        # sd:5064: the lanes are the Cadence view and the lamps sit above both, so each shows the Timeline it filtered.
        self.assertEqual(out["shown"], ["timeline", "timeline"])

    def test_a_tick_on_the_cadence_chart_opens_its_event_on_the_timeline(self):
        merge = next(e["id"] for e in self.doc["events"] if e["k"] == "merge")
        out = self.run_page("ELS.lanes.listeners.click[0]({ target: { closest: s => s === 'rect.tick' ? { dataset: { id: '%s' } } : null } });"
                            "R.details = ELS.details.html;" % merge)
        self.assertEqual(out["shown"], ["timeline"])
        self.assertIn("system#41", out["R"]["details"])

    def test_the_timeline_is_the_first_view_and_the_lane_chart_the_cadence_view(self):
        page = (V2 / "activity.html").read_text(encoding="utf-8")
        self.assertIn("window.PAGE_VIEWS = [{ id: 'timeline', label: 'Timeline' }, { id: 'cadence', label: 'Cadence' }];", ACTIVITY_JS)
        self.assertLess(page.index('<section id="view-timeline"'), page.index('<section class="chart" id="view-cadence"'))
        self.assertNotIn('id="views"', page)
        self.assertNotIn("shell.views", ACTIVITY_JS)

    def test_only_a_link_reads_the_older_journal_records_and_its_token_goes_back_to_24h(self):
        """The 7d, 30d and All chips are gone; palette.py's range=all link still opens the older records."""
        older = self.ids["older"]
        tok = "ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f=\"range\"]' ? { dataset: { f: 'range', v: 'all' } } : null } });"
        day = self.run_page("R.day = ELS.rows.html; R.lanes = ELS.lanes.html; R.note = ELS['journal-note'].html; R.hid = ELS['journal-note'].hidden;")
        self.assertNotIn(f'data-id="cmd:{older}"', day["R"]["day"])
        self.assertNotIn(f'data-id="cmd:{older}"', day["R"]["lanes"], "the chart is the 24 hours")
        self.assertEqual(day["R"]["note"], "1 exec note no known writer left is skipped.")
        self.assertFalse(day["R"]["hid"])
        out = self.run_page("R.all = ELS.rows.html; R.bar = ELS.filters.html;" + tok + "R.back = ELS.rows.html;",
                            pre="location.search = '?range=all';")
        self.assertIn(f'data-id="cmd:{older}"', out["R"]["all"])
        self.assertIn('class="tok" data-f="range" data-v="all" aria-label="Remove filter: Older journal records"', out["R"]["bar"])
        self.assertNotIn(f'data-id="cmd:{older}"', out["R"]["back"])

    def test_the_lamps_count_the_timeline_range(self):
        # The Commands lamp said "55 in the journal" on a 24-hour page whose 55 records were weeks old.
        range_ = "ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f=\"range\"]' ? { dataset: { f: 'range', v: '%s' } } : null } });"
        out = self.run_page("R.day = ELS.annunciator.html;" + range_ % "1h" + "R.hour = ELS.annunciator.html;")
        self.assertIn('<span class="ph">4 in 24 h</span>', out["R"]["day"])
        self.assertRegex(out["R"]["hour"], r'<span class="ph">\d+ in 1 h</span>')
        self.assertNotIn("in 24 h", out["R"]["hour"])
        out = self.run_page("R.all = ELS.annunciator.html;", pre="location.search = '?range=all';")
        self.assertIn('<span class="ph">5 in the journal</span>', out["R"]["all"])

    def test_a_row_names_its_state_in_words_and_where_it_belongs(self):
        port = self.ids["port"]
        out = self.run_page("R.rows = ELS.rows.html;")
        row = re.search(rf'<tr data-id="cmd:{self.ids["bad_run"]}".*?</tr>', out["R"]["rows"], re.S).group(0)
        self.assertIn('<span class="sr">warning</span>', row)
        self.assertIn(f'<td class="ref">sd:{port}</td>', row)

    def test_a_runner_record_has_no_output_to_show(self):
        out = self.run_page(f"R.off = cmd('command.output').when(C.get('cmd:{self.ids['runner']}'));"
                            f"R.on = cmd('command.output').when(C.get('cmd:{self.ids['ok_run']}'));")
        self.assertEqual(out["R"]["off"], "a runner record keeps its log in the retained clone, not in the execution log directory")
        self.assertIs(out["R"]["on"], True)

    def test_the_rail_badge_counts_the_24_hours_only(self):
        doc = json.loads(json.dumps(self.doc))
        next(e for e in doc["events"] if e["id"] == f"cmd:{self.ids['older']}")["s"] = "warning"
        out = self.run_page("", doc=doc)
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 2, "what": "failed events in 24 h"})

    def test_the_hourly_table_applies_the_repo_filter_the_lanes_apply(self):
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'repo', v: 'system' } } : null } });
R.table = ELS['chart-table'].html; R.lanes = ELS.lanes.html;""")
        cells = [int(n) for n in re.findall(r"<td>(\d+)</td>", out["R"]["table"])]
        self.assertEqual(sum(cells), len(re.findall(r'<rect class="tick', out["R"]["lanes"])))
        inside = [e for e in self.doc["events"] if e["repo"] == "system" and e["at"] >= self.doc["from"]]
        self.assertEqual(sum(cells), len(inside))
        self.assertEqual(sorted(e["k"] for e in inside), ["merge", "review", "run", "run"],
                         "the merge, the review it carried and the item's two runs")

    def test_the_runs_cell_counts_failed_jobs_and_blocked_assignments_apart(self):
        doc = json.loads(json.dumps(self.doc))
        blocked = next(e for e in doc["events"] if e["id"] == f"run:{self.ids['blocked']}")
        doc["events"].append(dict(blocked, id="run:9999", s="warning", status="failed", title="A failed run"))
        runs = [e for e in doc["events"] if e["k"] == "run"]
        self.assertTrue(any(e.get("job") and e["s"] == "caution" for e in runs), "the seed has an interrupted job")
        failed_jobs = sum(1 for e in runs if e.get("job") and e["s"] == "warning")
        blocked_runs = sum(1 for e in runs if e.get("status") == "blocked")
        assignments = sum(1 for e in runs if e.get("status"))
        out = self.run_page("R.ann = ELS.annunciator.html;", doc=doc)
        self.assertIn(f"<b>{failed_jobs}</b> jobs failed", out["R"]["ann"])
        self.assertIn(f"{blocked_runs} of {assignments} runs blocked", out["R"]["ann"])

    REFRESH = ("const refresh = () => ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' "
               "? { id: 'refresh', dataset: {} } : null } });\n")

    def test_an_older_read_that_answers_last_does_not_replace_a_newer_one(self):
        newer = json.loads(json.dumps(self.doc))
        newer["events"] = [e for e in newer["events"] if e["id"] == f"run:{self.ids['blocked']}"]
        for older in ("[200, DOC]", "[500, { error: 'database locked' }]"):
            out = self.run_page(self.REFRESH + f"""const held = [];
ANSWER = path => new Promise(res => held.push(res));
refresh(); refresh(); await flush();
held[1]([200, {json.dumps(newer)}]); await flush();
held[0]({older}); await flush();
R.rows = ELS.rows.html;""")
            self.assertIn(f'data-id="run:{self.ids["blocked"]}"', out["R"]["rows"], older)
            self.assertNotIn('data-id="job:nightly-sync"', out["R"]["rows"], older)
            self.assertIsNone(out["states"][-1], older)

    def test_a_failed_refresh_leaves_no_row_object_or_pick_from_the_last_read(self):
        n = self.ids["blocked"]
        out = self.run_page(self.REFRESH + f"""const picked = []; C.pick = id => picked.push(id);
document.dispatchEvent(new CustomEvent('shell:picked', {{ detail: ['run:{n}'] }}));
ANSWER = () => [500, {{ error: 'database locked' }}]; refresh(); await flush();
R.rows = ELS.rows.html; R.picked = picked; R.type = C.get('run:{n}').type; R.observed = document.body.dataset.observed || null;""")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertNotIn("data-id=", out["R"]["rows"])
        self.assertEqual(out["R"]["picked"], [f"run:{n}"], "the pick is dropped")
        self.assertEqual(out["R"]["type"], "not listed", "no command is on a retired object")
        self.assertIsNone(out["R"]["observed"])
        self.assertEqual(out["attention"][-1]["state"], "unknown")

    def test_a_refresh_retires_the_events_it_no_longer_lists(self):
        n, gone = self.ids["blocked"], f"run:{self.ids['done']}"
        newer = json.loads(json.dumps(self.doc))
        newer["events"] = [e for e in newer["events"] if e["id"] != gone]
        out = self.run_page(self.REFRESH + f"""const picked = []; C.pick = id => picked.push(id);
document.dispatchEvent(new CustomEvent('shell:picked', {{ detail: ['{gone}', 'run:{n}'] }}));
ANSWER = () => [200, {json.dumps(newer)}]; refresh(); await flush();
R.picked = picked; R.gone = C.get('{gone}').type; R.kept = C.get('run:{n}').type;""")
        self.assertEqual(out["R"]["picked"], [gone], "only the vanished pick is dropped")
        self.assertEqual(out["R"]["gone"], "not listed")
        self.assertEqual(out["R"]["kept"], "assignment")

    def test_a_filter_that_hides_every_row_clears_details_and_its_commands(self):
        out = self.run_page("""window.shell.reconcile = o => { OUT.clear = typeof o.clear; if (o.clear) o.clear(); return null; };
document.dispatchEvent(new CustomEvent('shell:open', { detail: 'job:nightly-sync' }));
R.before = ELS.details.html;
ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'repo', v: 'no-such-repo' } } : null } });
R.after = ELS.details.html; R.current = window.PAGE_LIST.current();""")
        self.assertIn('class="bar"', out["R"]["before"])
        self.assertEqual(out["clear"], "function")
        self.assertNotIn('class="bar"', out["R"]["after"])
        self.assertIn("nothing is selected", out["R"]["after"])
        self.assertIsNone(out["R"]["current"])

    def test_the_page_size_in_the_address_is_shown_and_kept(self):
        for asked, shown, kept in (("25", "25", "25"), ("100", "100", "100"), ("7", "50", None)):
            out = self.run_page("R.pager = ELS.pager.html;",
                                pre=f"location.search = '?size={asked}'; window.shell.url = p => {{ OUT.size = p.get('size'); }};")
            self.assertIn(f'data-size="{shown}" aria-pressed="true"', out["R"]["pager"], asked)
            self.assertEqual(out.get("size"), kept, asked)

    def test_a_page_number_must_be_a_positive_integer(self):
        base = self.run_page("R.rows = ELS.rows.html;")["R"]["rows"]
        self.assertIn("data-id=", base)
        for bad in ("-1", "1.5", "0", "two"):
            out = self.run_page("R.rows = ELS.rows.html;", pre=f"location.search = '?page={bad}';")
            self.assertEqual(out["R"]["rows"], base, bad)

    def test_the_journal_note_says_what_the_cap_left_unread(self):
        doc = json.loads(json.dumps(self.doc)); doc["journal_unread"] = 7
        out = self.run_page("R.note = ELS['journal-note'].html;", doc=doc)
        self.assertIn(f"The journal reads the latest {self.doc['journal_cap']} exec notes; 7 older are not read.", out["R"]["note"])

    def test_the_log_is_a_line_to_copy(self):
        out = self.run_page("R.cli = cmd('jobs.log').cli(C.get('job:nightly-sync'));")
        self.assertEqual(out["R"]["cli"], "local-cron-jobs/cron-jobs.sh logs nightly-sync")

    def test_the_page_reads_through_the_shared_reader(self):
        """sd:2489. The guards are read.js's, not a copy: no counter or retire of the page's own."""
        self.assertIn("shell.read(", ACTIVITY_JS)
        self.assertNotIn("++generation", ACTIVITY_JS)
        self.assertNotRegex(ACTIVITY_JS, r"function retire\(")

    def test_a_write_whose_reread_fails_keeps_the_rows_and_says_it_landed(self):
        """sd:2489. The write landed; a failed read after it is partial, not "nothing below is current"."""
        n = "job:nightly-sync"
        answer = ACCEPTED
        out = self.run_page(f"""ANSWER = (path, body) => path === '/api/activity' ? [500, {{ error: 'database locked' }}] : WRITE(path, body);
shellRun(cmd('jobs.retry'), C.get('{n}')); await flush();
R.rows = ELS.rows.html; R.type = C.get('{n}').type; R.details = ELS.details.html;""", answer)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("not read again after the change", out["R"]["details"])
        self.assertIn("The change landed", out["states"][-1]["text"])
        self.assertIn(f'data-id="{n}"', out["R"]["rows"], "the rows stay on screen")
        self.assertEqual(out["R"]["type"], "not listed", "no command runs on a row the reread did not confirm")
        self.assertEqual(out["toasts"], [["Run started · nightly-sync", False]])

    def test_a_bulk_retry_ends_in_at_most_two_reads(self):
        """sd:2489. The write barrier: N landed writes share one reread queued behind the read in flight."""
        doc = json.loads(json.dumps(self.doc))
        failed = next(e for e in doc["events"] if e["id"] == "job:nightly-sync")
        doc["events"] += [dict(failed, id=f"job:extra-{i}", job=f"extra-{i}", ref=f"extra-{i}") for i in range(3)]
        ids = ["job:nightly-sync"] + [f"job:extra-{i}" for i in range(3)]
        out = self.run_page(f"shellBulk(cmd('jobs.retry'), {json.dumps(ids)}.map(id => C.get(id))); await flush();",
                            ACCEPTED, doc=doc)
        self.assertEqual(len(out["posts"]), 4)
        self.assertLessEqual(out["gets"].count("/api/activity"), 3, out["gets"])

    def test_retry_asks_first_and_names_the_exit_after_a_command_not_found(self):
        """sd:3392, ruled 2026-09-30: after exit 127 Retry stays on, asks first and names the exit, and is not the row's act.

        The exit is the last run's; a path fixed outside the dashboard needs Retry to test it."""
        doc = json.loads(json.dumps(self.doc))
        next(e for e in doc["events"] if e["id"] == "job:nightly-sync")["exit"] = 127
        out = self.run_page("""const c = cmd('jobs.retry'), o = C.get('job:nightly-sync');
R.on = c.when(o); R.primary = !!c.primary(o); R.ask = !!c.askFirst(o); R.says = c.consequence(o);
shellRun(c, o); await flush();""", ACCEPTED, doc=doc)
        self.assertEqual((out["R"]["on"], out["R"]["primary"], out["R"]["ask"]), (True, False, True))
        self.assertIn("exit 127", out["R"]["says"])
        self.assertEqual(out["confirms"], ["jobs.retry"])
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/jobs/nightly-sync/retry"])
        self.assertEqual(out["toasts"], [["Run started · nightly-sync", False]], "the launchctl answer's wording stays")

    def test_retry_after_another_exit_does_not_ask(self):
        out = self.run_page("const c = cmd('jobs.retry'), o = C.get('job:nightly-sync'); R.ask = !!c.askFirst(o);"
                            " R.primary = !!c.primary(o); shellRun(c, o); await flush();", ACCEPTED)
        self.assertEqual((out["R"]["ask"], out["R"]["primary"], out["confirms"]), (False, True, []))

    def test_a_bulk_retry_with_a_command_not_found_row_asks_first(self):
        """sd:3392. A 127 row was off and left out of a bulk retry; now it is in it, so the group asks first."""
        doc = json.loads(json.dumps(self.doc))
        failed = next(e for e in doc["events"] if e["id"] == "job:nightly-sync")
        doc["events"].append(dict(failed, id="job:lost-path", job="lost-path", ref="lost-path", exit=127))
        out = self.run_page("shellBulk(cmd('jobs.retry'), [C.get('job:nightly-sync'), C.get('job:lost-path')]); await flush();",
                            ACCEPTED, doc=doc)
        self.assertEqual(out["confirms"], ["jobs.retry"])
        self.assertEqual(len(out["posts"]), 2)

    def test_retry_words_its_toast_from_launchctls_answer(self):
        """sd:3391, as Management words it (sd:3388): a 200 carries launchctl's answer; only `accepted` started a retry."""
        for answer, toast in {
            ACCEPTED: "Run started · nightly-sync",
            "(p, b) => [200, {request: {status: 'failed', message: 'launchctl refused the request (exit 3)'}}]":
                "nightly-sync not retried: launchctl refused the request (exit 3)",
            "(p, b) => [200, {request: {status: 'unknown', message: 'launchctl timed out; the request outcome is unknown'}}]":
                "Not known whether nightly-sync was retried: launchctl timed out; the request outcome is unknown",
            "(p, b) => [200, {}]": "Not known whether nightly-sync was retried: the answer named no outcome",
            "(p, b) => Promise.reject(new TypeError('Load failed'))": "Not known whether nightly-sync was retried: Load failed",
        }.items():
            with self.subTest(answer=answer):
                out = self.run_page("shellRun(cmd('jobs.retry'), C.get('job:nightly-sync')); await flush();", answer)
                self.assertEqual(out["toasts"], [[toast, False]])
                self.assertEqual(out["gets"].count("/api/activity"), 2, "the jobs are read again, whatever the answer")

    def test_a_refused_retry_says_why_and_is_not_worded_as_unknown(self):
        """A 4xx wrote nothing: the shell's own refusal toast, not "not known"."""
        out = self.run_page("shellRun(cmd('jobs.retry'), C.get('job:nightly-sync')); await flush();",
                            "(p, b) => [400, {error: 'launchd refuses a retry now'}]")
        self.assertEqual(len(out["toasts"]), 1)
        self.assertIn("not changed: launchd refuses a retry now", out["toasts"][0][0])

    def unfinished(self):
        """The seeded stopped record, as the journal shows one whose response never came back."""
        doc = json.loads(json.dumps(self.doc))
        row = next(e for e in doc["events"] if e["id"] == f"cmd:{self.ids['stopped']}")
        row.update(ended=None, exit=None, s="queued")
        return doc

    def test_reconcile_is_on_only_for_an_unfinished_palette_record(self):
        """sd:3391: Activity takes the Commands section's Reconcile outcome (palette.history)."""
        doc = self.unfinished()
        out = self.run_page(f"""const r = cmd('command.reconcile');
R.on = r.when(C.get('cmd:{self.ids['stopped']}')); R.primary = r.primary(C.get('cmd:{self.ids['stopped']}'));
R.ended = r.when(C.get('cmd:{self.ids['ok_run']}')); R.endedPrimary = r.primary(C.get('cmd:{self.ids['ok_run']}'));
R.runner = r.when(C.get('cmd:{self.ids['runner']}')); R.cli = r.cli ? r.cli(C.get('cmd:{self.ids['stopped']}')) : null;""", doc=doc)
        self.assertIs(out["R"]["on"], True)
        self.assertTrue(out["R"]["primary"])
        self.assertEqual(out["R"]["ended"], "the run ended; its outcome is recorded")
        self.assertFalse(out["R"]["endedPrimary"])
        self.assertEqual(out["R"]["runner"], "a runner record is the runner's to finish; no palette request waits on it")
        self.assertIsNone(out["R"]["cli"], "no sd verb reconciles; the line would be invented")

    def test_reconcile_is_off_for_a_pages_command_the_dashboard_ends_itself(self):
        """sd:5541: a page's command record is the dashboard's to end; no palette request waits on it."""
        doc = self.unfinished()
        row = next(e for e in doc["events"] if e["id"] == f"cmd:{self.ids['stopped']}")
        row.update(source="dashboard", scope="dashboard")
        out = self.run_page(f"""const r = cmd('command.reconcile');
R.on = r.when(C.get('cmd:{self.ids['stopped']}')); R.primary = r.primary(C.get('cmd:{self.ids['stopped']}'));""", doc=doc)
        self.assertEqual(out["R"]["on"], "a page's command is the dashboard's to finish; it records how the run ended")
        self.assertFalse(out["R"]["primary"])

    def test_reconcile_posts_the_old_route_shows_the_outcome_and_reads_again(self):
        note = self.ids["stopped"]
        answer = ("(p, b) => [200, { note: %d, state: 'finished', ended: '2026-09-06T09:40:00Z', exit_code: 125,"
                  " output: 'Prepared request closed before dispatch. No command was replayed.', output_expired: null }]" % note)
        out = self.run_page(f"shellRun(cmd('command.reconcile'), C.get('cmd:{note}')); await flush(); R.details = ELS.details.html;",
                            answer, doc=self.unfinished())
        self.assertEqual(out["posts"], [[f"/api/palette/{note}/reconcile", {}, 64]])
        self.assertEqual(out["toasts"], [[f"Outcome of note {note} recorded · exit 125", False]])
        self.assertEqual(out["gets"].count("/api/activity"), 2)
        self.assertIn("Prepared request closed before dispatch.", out["R"]["details"])

    def test_a_reconcile_with_no_answer_is_not_known_and_reads_again(self):
        note = self.ids["stopped"]
        for answer, why in (("(p, b) => Promise.reject(new TypeError('Load failed'))", "Load failed"),
                            ("(p, b) => [502, {error: 'the hub did not answer'}]", "the hub did not answer")):
            with self.subTest(answer=answer):
                out = self.run_page(f"shellRun(cmd('command.reconcile'), C.get('cmd:{note}')); await flush();", answer,
                                    doc=self.unfinished())
                self.assertEqual(out["toasts"], [[f"Not known whether note {note} was reconciled: {why}", False]])
                self.assertEqual(out["gets"].count("/api/activity"), 2)

    def test_a_reconcile_the_target_has_not_proved_says_why(self):
        note = self.ids["stopped"]
        out = self.run_page(f"shellRun(cmd('command.reconcile'), C.get('cmd:{note}')); await flush();",
                            "(p, b) => [400, {error: 'the target has not proved this outcome; it remains uncertain'}]",
                            doc=self.unfinished())
        self.assertEqual(len(out["toasts"]), 1)
        self.assertIn("not changed: the target has not proved this outcome", out["toasts"][0][0])

    def test_show_output_follows_next_offset_past_one_page(self):
        """sd:2416. read_execution answers 64 KiB a read; a full page means more follows at next_offset."""
        note = self.ids["bad_run"]
        answer = ("(path) => path.endsWith('?offset=0') ? [200, { state: 'finished', output: 'first page|', next_offset: 65536 }]"
                  " : path.endsWith('?offset=65536') ? [200, { state: 'finished', output: 'second page', next_offset: 65547 }]"
                  " : [500, { error: 'read past the end' }]")
        out = self.run_page(f"shellRun(cmd('command.output'), C.get('cmd:{note}')); await flush(); R.details = ELS.details.html;",
                            answer)
        self.assertEqual(out["gets"], ["/api/activity", f"/api/executions/{note}?offset=0", f"/api/executions/{note}?offset=65536"])
        self.assertIn("first page|second page", out["R"]["details"])

    # --- the filter bar: one compact row (operator, 2026-10-17: "they take up a lot of space") ---

    def repos_doc(self):
        """The seeded document plus merges in two more repositories, so the repo list has something to filter."""
        doc = json.loads(json.dumps(self.doc))
        merge = next(e for e in doc["events"] if e["k"] == "merge")
        for n, repo in enumerate(("ui-design", "sd-ai-command-pack", "ui-design")):
            doc["events"].append(dict(merge, id=f"merge:90{n}", repo=repo, what=f"Slice in {repo}"))
        return doc

    def test_the_filters_are_one_bar_with_no_stacked_label_rows(self):
        out = self.run_page("R.filters = ELS.filters.html; R.tally = OUT.timeline;")
        bar = out["R"]["filters"]
        self.assertEqual(bar.count('class="fbar"'), 1)
        self.assertTrue(bar.startswith('<div class="fbar" role="group" aria-label="Filters">'), bar[:80])
        self.assertNotIn("fgroup", bar)
        self.assertNotIn('class="label"', bar, "no label line above a group")
        self.assertNotIn('data-f="kind"', bar, "the lamps and lanes filter by kind; no kind chip row")
        state = re.search(r'<div class="fseg" role="group" aria-label="State">(.*?)</div>', bar).group(1)
        failed = sum(1 for e in self.doc["events"] if e["s"] == "warning")
        self.assertRegex(state, rf'data-f="state" data-v="warning" aria-pressed="false" title="failed: {failed}"><span class="g-warning" aria-hidden="true">■</span><span class="n">{failed}</span>')
        self.assertEqual(re.findall(r'aria-hidden="true">(.)</span>', state), ["■", "▲", "●"])
        rng = re.search(r'<div class="fseg" role="group" aria-label="Range">(.*?)</div>', bar).group(1)
        self.assertEqual(re.findall(r'data-f="range" data-v="(\w+)"', rng), ["1h", "24h"])
        self.assertIn('data-v="24h" aria-pressed="true"', rng)
        self.assertRegex(bar, r'<button type="button" class="pick" id="repo-btn" aria-haspopup="listbox" aria-expanded="false" aria-controls="repo-pop">Repo')
        self.assertTrue(bar.endswith('<div class="fsum"></div></div>'), "no filter is on, so there is nothing to clear")
        # sd:5463: the count sits once, on the Timeline title line, not in the bar.
        inside = sum(1 for e in self.doc["events"] if e["at"] >= self.doc["from"])
        self.assertEqual(out["R"]["tally"], inside, "24h leaves the older record out (sd:5524: the Timeline tab's count)")

    def test_the_repo_popover_opens_filters_and_closes_and_round_trips_the_address(self):
        key = "ELS['%s'].listeners.keydown[0]({ key: '%s', preventDefault() {}, stopPropagation() { R.stopped = (R.stopped || 0) + 1; } });"
        out = self.run_page("""const btn = () => ELS.filters.listeners.click[0]({ target: { closest: s => s === '#repo-btn' ? {} : null } });
R.closed = ELS['repo-pop'].hidden; btn(); R.open = ELS['repo-pop'].hidden; R.bar = ELS.filters.html; R.all = ELS['repo-list'].html;
ELS['repo-q'].value = 'ui'; ELS['repo-q'].listeners.input[0]({}); R.typed = ELS['repo-list'].html;
ELS['repo-list'].listeners.click[0]({ target: { closest: s => s === '[role="option"]' ? { dataset: { repo: 'ui-design' } } : null } });
R.picked = ELS['repo-list'].html; R.one = ELS.filters.html;
ELS['repo-q'].value = 'sd-'; ELS['repo-q'].listeners.input[0]({});
""" + key % ("repo-pop", "Enter") + """R.two = ELS.filters.html;
""" + key % ("repo-pop", "Escape") + "R.after = ELS['repo-pop'].hidden; R.shut = ELS.filters.html; R.rows = ELS.rows.html;",
                            doc=self.repos_doc(), pre="window.shell.url = p => OUT.repos = (OUT.repos || []).concat([p.get('repo')]);")
        R = out["R"]
        self.assertEqual((R["closed"], R["open"], R["after"]), (True, False, True))
        self.assertIn('aria-expanded="true"', R["bar"])
        self.assertEqual(re.findall(r'data-repo="([\w-]+)"', R["all"]), ["system", "ui-design", "sd-ai-command-pack"],
                         "every repo, most events first, with no 'more' cut")
        self.assertEqual(re.findall(r'data-repo="([\w-]+)"', R["typed"]), ["ui-design"], "typing filters the list")
        self.assertRegex(R["picked"], r'data-repo="ui-design" aria-selected="true"[^>]*><span class="ck" aria-hidden="true">✓</span>')
        self.assertIn('id="repo-btn" aria-haspopup="listbox" aria-expanded="true" aria-controls="repo-pop">Repo <span class="n">· 1</span>', R["one"])
        self.assertEqual(out["repos"][-2:], ["ui-design", "ui-design,sd-ai-command-pack"], "Enter picks the active option")
        self.assertIn('<span class="n">· 2</span>', R["two"])
        self.assertEqual(R["stopped"], 2, "Enter and Esc stop at the popover, so the shell does not clear the filter")
        self.assertIn('aria-expanded="false"', R["shut"])
        for repo in ("ui-design", "sd-ai-command-pack"):
            self.assertIn(f'class="tok" data-f="repo" data-v="{repo}" aria-label="Remove filter: {repo}"', R["shut"])
        self.assertIn("Slice in ui-design", R["rows"])
        self.assertNotIn("Port the page<small>", R["rows"])
        # The address round-trips: a link with two repos opens on both, picked in the list.
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '#repo-btn' ? {} : null } });
R.bar = ELS.filters.html; R.list = ELS['repo-list'].html;""", doc=self.repos_doc(), pre="location.search = '?repo=ui-design,gone-repo';")
        self.assertIn('Repo <span class="n">· 2</span>', out["R"]["bar"])
        self.assertRegex(out["R"]["list"], r'data-repo="ui-design" aria-selected="true"')
        self.assertRegex(out["R"]["list"], r'data-repo="gone-repo" aria-selected="true"', "a picked repo with no events can still be unpicked")

    def test_clear_all_resets_every_filter_and_the_address(self):
        out = self.run_page("""R.before = ELS.filters.html;
ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-fclear]' ? {} : null } });
R.after = ELS.filters.html; R.rows = ELS.rows.html; R.input = (OUT.shift ?? null);""",
                            pre="location.search = '?kind=merge&repo=system&state=ok&range=1h&q=port'; "
                                "window.shell.url = p => OUT.qs = (OUT.qs || []).concat([p.toString()]);")
        before, after = out["R"]["before"], out["R"]["after"]
        self.assertIn('data-fclear', before)
        self.assertEqual(len(re.findall(r'class="tok"', before)), 3, "kind, repo and the query are tokens")
        self.assertIn('data-f="q" data-v="" aria-label="Remove filter: “port”"', before)
        self.assertRegex(before, r'data-v="ok" aria-pressed="true"')
        self.assertRegex(before, r'data-v="1h" aria-pressed="true"')
        self.assertEqual(out["qs"][-1], "")
        self.assertNotIn('data-fclear', after)
        self.assertNotIn('class="tok"', after)
        self.assertNotIn('aria-pressed="true" title="', re.search(r'aria-label="State">(.*?)</div>', after).group(1))
        self.assertRegex(after, r'data-v="24h" aria-pressed="true"')
        self.assertEqual(out["R"]["input"], "")

    def test_a_query_token_clears_the_field(self):
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'q', v: '' } } : null } });
R.bar = ELS.filters.html; R.input = (OUT.shift ?? null);""", pre="location.search = '?q=port';"
                                                      "window.shell.url = p => OUT.qs = (OUT.qs || []).concat([p.toString()]);")
        self.assertEqual((out["R"]["input"], out["qs"][-1]), ("", ""))
        self.assertNotIn('class="tok"', out["R"]["bar"])

    def test_a_search_from_the_address_matches_without_case(self):
        """sd:2424 (round-6 finding). A typed query is lowercased; one restored from ?q= was matched as written."""
        out = self.run_page("R.rows = ELS.rows.html;", pre="location.search = '?q=Port%20The';")
        self.assertIn("Port the page", out["R"]["rows"])

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", ACTIVITY_JS)
        self.assertNotIn("createElement('style')", ACTIVITY_JS)
        self.assertNotIn("setAttribute('style'", ACTIVITY_JS)
        self.assertNotRegex(ACTIVITY_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", ACTIVITY_JS)), [])
        # A page-level j/k or Escape handler is drift (the shell owns them through PAGE_LIST).
        self.assertNotRegex(ACTIVITY_JS, r"e\.key === '[jk]'")
        self.assertNotRegex(ACTIVITY_JS, r"e\.key === 'Escape' && active")


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "activity", "Activity", "/activity", ("/api/activity",)

    def test_activity_took_the_commands_section_so_the_rail_has_no_commands_entry(self):
        """sd:3391: Read output and Reconcile live on Activity's command rows; the classic Commands entry goes."""
        self.assertNotIn("Commands", v2.CLASSIC)
        shell_js = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
        groups = re.search(r"const GROUPS = \[(.*?)\n  \];", shell_js, re.S).group(1)
        self.assertNotIn("'Commands'", groups)
        self.assertNotRegex(shell_js, r"Commands: 'commands\.html'")


class TheBadge(unittest.TestCase):
    def test_the_badge_counts_the_24_hour_window_only(self):
        # sd:3446: the read's rule, which every page's rail shows; an old journal record or an undated one never lights it.
        start = activity_screen._utc("2026-09-05T12:00:00Z")
        events = [{"at": "2026-09-06T10:00:00Z", "s": "warning"}, {"at": "2026-09-05T12:00:00Z", "s": "caution"},
                  {"at": "2026-09-01T10:00:00Z", "s": "warning"}, {"at": None, "s": "warning"}]
        self.assertEqual(activity_screen._attention(events, start), {"state": "warning", "n": 1, "what": "failed events in 24 h"})
        self.assertEqual(activity_screen._attention(events[1:], start), {"state": "caution", "n": 1, "what": "caution events in 24 h"})
        self.assertEqual(activity_screen._attention(events[2:], start)["state"], "ok")


if __name__ == "__main__":
    unittest.main()
