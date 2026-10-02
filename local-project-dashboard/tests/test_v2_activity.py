"""The v2 Activity page (sd:2111).

What this slice promises: `/activity` answers under the shared policy and
loads its script before `shell.js`; `/api/activity` is one 24-hour timeline
built from records the library already keeps -- the delivery notes
`ship.note_merge` writes, runner assignments with their queue revision,
launchd jobs placed at their log time -- plus the execution journal
`runner_exec.executions` lists, older records too, and names every kind it
has no collector for, with the reason, rather than showing it as zero. The
page follows the design source at d82daa1.

`activity.js` runs under JavaScriptCore (osascript), against the stand-in
page and shell `test_v2_tasks` uses, with the document above as its fetch
answer. The browser half -- focus, the look at 375 px, the lane chart -- is a
manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db import runner
from sd_db.ship import note_merge
from sd_dashboard import activity_screen, server, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
ACTIVITY_JS = (V2 / "static" / "activity.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

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
                                       "merge_commit": "1acdcf6" + "0" * 33})
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
        self.assertEqual(self.doc["kinds"], ["merge", "run", "review", "deploy", "mail", "command"])
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

    def test_a_run_carries_its_state_and_the_revision_requeue_checks(self):
        blocked = self.by_id[f"run:{self.ids['blocked']}"]
        self.assertEqual((blocked["s"], blocked["status"], blocked["n"], blocked["item"]),
                         ("caution", "blocked", self.ids["blocked"], self.ids["port"]))
        self.assertEqual(blocked["revision"], runner.queue_state(self.connection, self.ids["blocked"])["revision"])
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
        self.assertEqual(self.by_id["job:backup"]["s"], "ok")
        self.assertFalse(self.by_id["job:backup"]["retry"]["allowed"])
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

    def test_a_kind_with_no_collector_is_named_with_its_reason(self):
        self.assertEqual(set(self.doc["unknown"]), {"review", "deploy", "mail"})
        self.assertTrue(all(reason.startswith("No collector reads") for reason in self.doc["unknown"].values()))
        self.assertEqual(self.doc["sources"], {"merge": "", "run": "", "job": "", "command": ""})

    def test_a_source_that_fails_is_named_and_the_others_still_answer(self):
        doc = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self, refuse="launchctl did not answer"))
        self.assertEqual(doc["sources"]["job"], "launchctl did not answer")
        self.assertEqual({event["k"] for event in doc["events"]}, {"merge", "run", "command"})
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
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "activity.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/activity")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/activity?kind=merge", headers=cookie)[0], 400)
        status, _, body = self.request("/api/activity", headers=cookie)
        self.assertEqual(status, 200)
        self.assertIn("events", json.loads(body))


PAGE = r"""
C.select = () => {}; C.pick = () => {};
window.shell.row = () => null; window.shell.setContext = () => {}; window.shell.openChat = () => {}; window.shell.send = () => {};
window.shell.pages = { Tasks: '/tasks' };
OUT.urls = []; window.shell.url = p => OUT.urls.push([p.get('kind'), p.get('range')]); window.shell.views = v => { OUT.views = v.map(x => x.name); };
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
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + pre + "\n"
                  + f"\nconst DOC = {json.dumps(doc or self.doc)};\n"
                  + "const WRITE = " + answer + ";\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/activity') return DOC.status ? [DOC.status, { error: DOC.error }] : [200, DOC];
  return WRITE ? WRITE(path, body) : [404, { error: 'no answer' }];
};\n""" + ACTIVITY_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
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
            ["asg.requeue", "assignment", "undo", "q", None, True, True, True],
            ["asg.get", "assignment", "safe", "o", None, True, False, False],
            ["asg.item", "assignment", "safe", "i", None, True, False, False],
            ["jobs.retry", "job", "safe", "t", None, True, False, True],
            ["jobs.log", "job", "safe", "l", False, True, False, False],
            ["command.output", "command", "safe", "o", None, True, False, False],
            ["command.item", "command", "safe", "i", None, True, False, False],
        ])

    def test_the_page_reads_the_document_once_and_draws_unknown_kinds_hatched(self):
        out = self.run_page("R.lanes = ELS.lanes.html; R.ann = ELS.annunciator.html; R.rows = ELS.rows.html; R.sub = ELS.sub.html;")
        self.assertEqual(out["gets"], ["/api/activity"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        lanes = out["R"]["lanes"]
        for kind in ("review", "deploy", "mail"):
            self.assertRegex(lanes, rf'data-kind="{kind}"[^>]*>.*?<span class="n">▨</span>')
            self.assertRegex(out["R"]["ann"], rf'<div class="cell" data-kind="{kind}" data-state="unknown">')
        self.assertIn('data-kind="command" data-state="warning"', out["R"]["ann"])
        self.assertIn("Port the page", out["R"]["rows"])
        self.assertIn("nightly-sync failed with exit 7", out["R"]["rows"])
        inside = [e for e in self.doc["events"] if e["at"] >= self.doc["from"]]
        self.assertIn(f"{len(inside)} events", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 2, "what": "failed events in 24 h"})

    def test_a_failed_read_is_an_error_and_a_failed_source_is_partial(self):
        out = self.run_page("", doc={"status": 500, "error": "database locked"})
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("database locked", out["states"][-1]["text"])
        partial = activity_screen.document(self.connection, now=NOW, jobs_backend=backend(self, refuse="launchctl did not answer"))
        out = self.run_page("", doc=partial)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("job: launchctl did not answer", out["states"][-1]["text"])

    def test_the_kind_filter_narrows_the_rows(self):
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '.chip' ? { dataset: { f: 'kind', v: 'command' } } : null } });
R.rows = ELS.rows.html;""")
        self.assertIn(">status · exit 0<", out["R"]["rows"])
        self.assertNotIn("Port the page<small>", out["R"]["rows"])

    def test_requeue_toasts_after_the_write_and_undo_cancels_with_the_revision_it_answered(self):
        n = self.ids["blocked"]
        revision = runner.queue_state(self.connection, n)["revision"]
        answer = "(path, body) => [200, { id: %d, status: path.endsWith('requeue') ? 'queued' : 'cancelled', revision: 'b'.repeat(64) }]" % n
        out = self.run_page(f"""R.off = cmd('asg.requeue').when(C.get('run:{self.ids["done"]}'));
shellRun(cmd('asg.requeue'), C.get('run:{n}'));
R.before = OUT.toasts.length; await flush(); R.after = OUT.toasts.length;
lastToast().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["off"], "the assignment is done")
        self.assertEqual(out["R"]["before"], 0, "the toast came before the write landed")
        self.assertEqual(out["R"]["after"], 1)
        self.assertEqual(out["posts"], [[f"/api/runner/{n}/requeue", {"revision": revision}, 64],
                                        [f"/api/runner/{n}/cancel", {"revision": "b" * 64}, 64]])
        self.assertEqual(out["toasts"], [[f"Requeued · #{n}. The runner starts it on its next tick.", True],
                                         ["Requeue undone · Port the page", False]])
        self.assertEqual(out["gets"], ["/api/activity"] * 3, "the document was not read again after each write")

    def test_a_bulk_requeue_counts_only_the_writes_that_landed_and_undo_reverses_those(self):
        a, b = self.ids["blocked"], self.ids["done"]
        answer = ("(path) => path.includes('/%d/') ? [409, { error: 'the queue moved' }]"
                  " : [200, { status: path.endsWith('requeue') ? 'queued' : 'cancelled', revision: 'c'.repeat(64) }]") % b
        out = self.run_page(f"""shellBulk(cmd('asg.requeue'), [C.get('run:{a}'), C.get('run:{b}')]); await flush();
lastToast().undo(); await flush();""", answer)
        self.assertEqual(out["toasts"][0], ["Requeue · 1 assignment · 1 of 2 not changed: the queue moved", True])
        self.assertEqual([post[0] for post in out["posts"]],
                         [f"/api/runner/{a}/requeue", f"/api/runner/{b}/requeue", f"/api/runner/{a}/cancel"])
        self.assertEqual(out["posts"][-1][1], {"revision": "c" * 64})

    def test_retry_is_on_only_for_a_failed_job_and_posts_its_revision(self):
        failed = next(e for e in self.doc["events"] if e["id"] == "job:nightly-sync")
        out = self.run_page("""R.off = cmd('jobs.retry').when(C.get('job:backup'));
R.cli = cmd('jobs.retry').cli(C.get('job:nightly-sync'));
R.sends = cmd('jobs.retry').sends ? cmd('jobs.retry').sends(C.get('job:nightly-sync')) : null;
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync')); await flush();""", "() => [200, {}]")
        self.assertEqual(out["R"]["off"], "no failed run to retry")
        # The line to copy is the sd verb, which passes the operation gate and the audit; kickstart is only what it sends.
        self.assertEqual(out["R"]["cli"], "sd jobs retry nightly-sync")
        self.assertEqual(out["R"]["sends"], "launchctl kickstart fixture/nightly-sync")
        self.assertEqual(out["posts"], [["/api/jobs/nightly-sync/retry", {"revision": failed["revision"]}, 64]])
        self.assertEqual(out["toasts"], [["Retry started · nightly-sync", False]])

    def test_show_output_reads_the_execution_record_into_details(self):
        note = self.ids["bad_run"]
        answer = "(path) => [200, { state: 'finished', output: 'removed 3 worktrees', output_expired: null }]"
        out = self.run_page(f"shellRun(cmd('command.output'), C.get('cmd:{note}')); await flush(); R.details = ELS.details.html;",
                            answer)
        self.assertEqual(out["gets"], ["/api/activity", f"/api/executions/{note}?offset=0"])
        self.assertIn("removed 3 worktrees", out["R"]["details"])
        self.assertEqual(out["toasts"], [[f"Output of note {note} shown in Details", False]])
        self.assertEqual(out["posts"], [])

    def test_a_lamp_its_chip_and_its_lane_add_to_one_kind_set(self):
        click = "ELS.%s.listeners.click[0]({ target: { closest: s => s === '%s' ? { dataset: { kind: '%s' } } : null } });"
        out = self.run_page(click % ("lanes", ".lane-btn", "run") + click % ("annunciator", "button.cell", "merge")
                            + "R.ann = ELS.annunciator.html; R.lanes = ELS.lanes.html; R.filters = ELS.filters.html;")
        self.assertEqual(out["urls"][-1], ["run,merge", None], "a lamp adds to the set, never replaces it")
        for kind in ("merge", "run"):
            self.assertIn(f'data-kind="{kind}" data-state="', out["R"]["ann"])
            self.assertRegex(out["R"]["ann"], rf'data-kind="{kind}" data-state="\w+" aria-pressed="true"')
            self.assertRegex(out["R"]["lanes"], rf'data-kind="{kind}" aria-pressed="true"')
            self.assertRegex(out["R"]["filters"], rf'data-v="{kind}" aria-pressed="true"')

    def test_all_read_adds_the_older_journal_records_and_24h_leaves_them_out(self):
        older = self.ids["older"]
        range_ = "ELS.range.listeners.click[0]({ target: { closest: s => s === '.chip' ? { dataset: { r: '%s' } } : null } });"
        out = self.run_page("R.day = ELS.rows.html; R.chip = ELS.range.html; R.lanes = ELS.lanes.html; R.note = ELS['journal-note'].html;"
                            + range_ % "all" + "R.all = ELS.rows.html;" + range_ % "24h" + "R.back = ELS.rows.html;")
        self.assertNotIn(f'data-id="cmd:{older}"', out["R"]["day"])
        self.assertIn("All read <span class=\"n\">+1 older</span>", out["R"]["chip"])
        self.assertNotIn(f'data-id="cmd:{older}"', out["R"]["lanes"], "the chart is the 24 hours")
        self.assertIn(f'data-id="cmd:{older}"', out["R"]["all"])
        self.assertIn([None, "all"], out["urls"])
        self.assertNotIn(f'data-id="cmd:{older}"', out["R"]["back"])
        self.assertIn("5 records", out["R"]["note"])
        self.assertIn("palette and runner runs alike", out["R"]["note"])
        self.assertIn("1 exec note no known writer left is skipped", out["R"]["note"])
        self.assertNotIn("none shows here", out["R"]["note"])
        self.assertIn("Command journal", out["views"])

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
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '.chip' ? { dataset: { f: 'repo', v: 'system' } } : null } });
R.table = ELS['chart-table'].html; R.lanes = ELS.lanes.html;""")
        cells = [int(n) for n in re.findall(r"<td>(\d+)</td>", out["R"]["table"])]
        self.assertEqual(sum(cells), len(re.findall(r'<rect class="tick', out["R"]["lanes"])))
        inside = [e for e in self.doc["events"] if e["repo"] == "system" and e["at"] >= self.doc["from"]]
        self.assertEqual(sum(cells), len(inside))
        self.assertEqual(sorted(e["k"] for e in inside), ["merge", "run", "run"], "the merge and the item's two runs")

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
ELS.filters.listeners.click[0]({ target: { closest: s => s === '.chip' ? { dataset: { f: 'repo', v: 'no-such-repo' } } : null } });
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

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", ACTIVITY_JS)
        self.assertNotIn("createElement('style')", ACTIVITY_JS)
        self.assertNotIn("setAttribute('style'", ACTIVITY_JS)
        self.assertNotRegex(ACTIVITY_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", ACTIVITY_JS)), [])
        # A page-level j/k or Escape handler is drift (the shell owns them through PAGE_LIST).
        self.assertNotRegex(ACTIVITY_JS, r"e\.key === '[jk]'")
        self.assertNotRegex(ACTIVITY_JS, r"e\.key === 'Escape' && active")


if __name__ == "__main__":
    unittest.main()
