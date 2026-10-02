"""The v2 Reports page (sd:2121).

What this slice promises: `/reports` answers under the shared policy and loads
its script before `shell.js`; `/api/reports` gives the newest reports v1
lists, each with its provenance, the revision the acknowledge route checks
and the followups that hold it, the launchd jobs, each job's runs per local
day from its `cron-jobs.sh` log, and the job families the config folder's
`report-families.conf` names (job names are the operator's, so the public
checkout carries only `report-families.conf.example`). A source that fails is
`null` with its reason. `/api/reports/clean` is v1's clean preview and
writes nothing. The page registers the design's Reports commands with the
design's ids, labels, keys and risks; Acknowledge posts v1's route and
declares no Undo, because sd-db has no verb that reopens a report.

`reports.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses. The browser half -- the look at 375 px,
focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import timedelta, timezone
from pathlib import Path
from unittest import mock

from sd_db import reporting, workflow
from sd_dashboard import reports_screen, server, v2

from support import ScreenCase
from test_now_screen import JobsBackend
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
REPORTS_JS = (V2 / "static" / "reports.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
EXAMPLE = Path(reports_screen.__file__).resolve().parents[1] / "report-families.conf.example"
#: The machine's offset in the fixtures: the logs write -0600, so local days are those dates.
TZ = timezone(timedelta(hours=-6))
#: Two days after the newest fixture report, so a clean preview at 2026-09-08 is not a cutoff in the future.
LATER = "2026-09-10T12:00:00Z"
DAYS = ["2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]

#: The design's Reports commands (design source products/system/designs/v2/reports.js): id, object type, label, key, risk.
COMMANDS = [
    ["report.ack", "report", "Acknowledge", "a", "confirm"],  # build: confirm by operator ruling; sd-db cannot reopen a report
    ["report.show", "report", "Open item", "o", "safe"],
    ["report.log", "report", "Show log", "l", "safe"],
    ["report.task", "report", "Make task", "k", "safe"],
    ["mail.ack", "status mail", "Acknowledge", "a", "undo"],
    ["mail.open", "status mail", "Open in Gmail", "o", "safe"],
    ["status mail.log", "status mail", "Show log", "l", "safe"],
    ["status mail.task", "status mail", "Make task", "k", "safe"],
    ["jobs.retry", "job", "Retry", "t", "safe"],
]


class Jobs(JobsBackend):
    """`JobsBackend` with a launchd calendar per job, and a log written line by line."""

    def __init__(self, root, jobs=(), schedules=None, refuse=""):
        super().__init__(root, jobs=jobs, refuse=refuse)
        self.schedules = schedules or {}

    def inspect(self, name):
        return super().inspect(name) | {"schedule": self.schedules.get(name, [])}

    def write(self, name, lines):
        (self.cron_root / "logs" / f"{name}.log").write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def run_lines(job, day, outcome="done", at="02:15:00"):
    return [f"[{job}] {day}T{at}-0600 starting (cwd: /tmp)", f"[{job}] {day}T{at}-0600 {outcome}"]


def seed(case):
    """A failing nightly job and its report, a clean report, an acknowledged one, and the logs behind them."""
    ids = {}
    ids["failed"] = reporting.ingest(case.connection, job="nightly-sync", run_id="r1", started="2026-09-08T08:15:00Z",
                                     ended="2026-09-08T08:16:00Z", exit_code=7, text="line one\nmirror failed\n",
                                     source_path="/home/example/repos/system/local-cron-jobs/logs/nightly-sync.log",
                                     attention=True, attention_basis="job exited 7")["item"]["id"]
    ids["clean"] = reporting.ingest(case.connection, job="weekly-scan", run_id="r2", started="2026-09-06T13:00:00Z",
                                    ended="2026-09-06T13:01:00Z", exit_code=0, text="x" * 2000,
                                    source_path="sd-db.sh scan")["item"]["id"]
    done = reporting.ingest(case.connection, job="weekly-scan", run_id="r0", started="2026-09-05T13:00:00Z",
                            ended="2026-09-05T13:01:00Z", exit_code=0, text="old", source_path="sd-db.sh scan")
    ids["done"] = done["item"]["id"]
    # `ingest` stamps the real clock; backdate each report to its run, as the store would hold it (seeding, not a writer).
    for key, at in (("failed", "2026-09-08T08:16:00+00:00"), ("clean", "2026-09-06T13:01:00+00:00"), ("done", "2026-09-05T13:01:00+00:00")):
        case.connection.execute("UPDATE item SET created_at = ? WHERE id = ?", (at, ids[key]))
    case.connection.commit()
    reporting.acknowledge(case.connection, ids["done"], expected_revision=workflow.item_state(case.connection, ids["done"])["revision"],
                          who="fixture")
    case.jobs = Jobs(case.dir, jobs=[("nightly-sync", "failed", 7, None), ("weekly-scan", "idle", 0, None),
                                     ("quiet-one", "idle", 0, None)],
                     schedules={"weekly-scan": [{"Weekday": 0, "Hour": 7}]})  # Sundays: 2026-09-06
    case.jobs.write("nightly-sync", [line for day in DAYS[2:6] for line in run_lines("nightly-sync", day)]
                    + run_lines("nightly-sync", DAYS[6], "FAILED rc=7") + run_lines("nightly-sync", DAYS[6], "FAILED rc=7", "03:00:00"))
    case.jobs.write("weekly-scan", run_lines("weekly-scan", DAYS[2]))
    return ids


class TheDocument(ScreenCase):
    """`reports_screen.document` gives every source the page reads, each guarded on its own."""

    def setUp(self):
        super().setUp()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ids = seed(self)
        self.enterContext(mock.patch.object(reports_screen, "_html_folders", lambda: 3))

    def doc(self, **kw):
        kw.setdefault("config", EXAMPLE)
        return reports_screen.document(self.connection, now=LATER, jobs=self.jobs, tz=TZ, **kw)

    def test_each_report_carries_its_provenance_revision_and_the_followup_that_holds_it(self):
        doc = self.doc()
        self.assertEqual(doc["sources"], {"reports": "", "jobs": "", "cadence": "", "html": ""})
        rows = {r["id"]: r for r in doc["reports"]}
        failed = rows[self.ids["failed"]]
        self.assertEqual((failed["job"], failed["exit"], failed["basis"], failed["attention"], failed["status"]),
                         ("nightly-sync", 7, "job exited 7", True, "planning"))
        self.assertEqual(failed["revision"], workflow.item_state(self.connection, self.ids["failed"])["revision"])
        notes = [n["id"] for n in workflow.item_state(self.connection, self.ids["failed"])["notes"] if n["kind"] == "followup"]
        self.assertEqual(failed["followups"], notes)
        self.assertEqual(len(notes), 1)
        clean = rows[self.ids["clean"]]
        self.assertEqual((clean["attention"], clean["followups"], clean["cut"], len(clean["body"])), (False, [], True, 1400))
        self.assertEqual(rows[self.ids["done"]]["status"], "done")
        self.assertEqual(doc["mail"], {"available": False, "reason": reports_screen.MAIL_REASON})
        self.assertEqual(doc["html"], 3)

    def test_the_cadence_counts_each_local_day_from_the_log_and_the_calendar_leaves_days_out(self):
        doc = self.doc()
        self.assertEqual(doc["days"], DAYS)
        sync = doc["cadence"]["nightly-sync"]
        self.assertEqual((sync["log"], sync["from"], sync["read_from"]), (True, "2026-09-06", None))
        self.assertEqual([sync["runs"][d] for d in DAYS], [[0, 0], [0, 0], [1, 0], [1, 0], [1, 0], [1, 0], [0, 2]])
        self.assertEqual(sync["scheduled"], [True] * 7)
        # Weekday 0 is Sunday: 2026-09-06 only.
        self.assertEqual(doc["cadence"]["weekly-scan"]["scheduled"], [d == "2026-09-06" for d in DAYS])
        self.assertEqual(doc["cadence"]["quiet-one"], {"log": False, "from": None, "read_from": None, "runs": {}, "scheduled": [True] * 7,
                                                       "last": {"day": DAYS[5], "read": False, "runs": [0, 0]}})
        # The last scheduled day before today, whatever the cadence, and whether the log holds it.
        self.assertEqual(sync["last"], {"day": DAYS[5], "read": True, "runs": [1, 0]})
        self.assertEqual(doc["cadence"]["weekly-scan"]["last"], {"day": "2026-09-06", "read": True, "runs": [1, 0]})

    def test_a_job_scheduled_less_often_than_weekly_names_its_last_scheduled_run_and_whether_it_was_read(self):
        logs = self.jobs.cron_root / "logs"
        (logs / "monthly.log").write_text("\n".join(run_lines("monthly", "2026-09-01", "FAILED rc=2")) + "\n", encoding="utf-8")
        (logs / "late-log.log").write_text("\n".join(run_lines("late-log", "2026-09-05")) + "\n", encoding="utf-8")
        (logs / "yearly.log").write_text("\n".join(run_lines("yearly", "2026-09-05")) + "\n", encoding="utf-8")
        jobs = [{"name": "monthly", "schedule": [{"Day": 1, "Hour": 3}]},
                {"name": "late-log", "schedule": [{"Day": 1, "Hour": 3}]},  # its log begins after its last scheduled run
                {"name": "yearly", "schedule": [{"Month": 1, "Day": 1, "Hour": 3}]},
                {"name": "never", "schedule": [{"Month": 2, "Day": 30}]},
                {"name": "no-log", "schedule": [{"Day": 1, "Hour": 3}]}]
        got = reports_screen.cadence(jobs, logs, DAYS)
        self.assertEqual(got["monthly"]["scheduled"], [False] * 7)
        self.assertEqual(got["monthly"]["last"], {"day": "2026-09-01", "read": True, "runs": [0, 1]})
        self.assertEqual(got["late-log"]["last"], {"day": "2026-09-01", "read": False, "runs": [0, 0]})
        self.assertEqual(got["yearly"]["last"], {"day": "2026-01-01", "read": False, "runs": [0, 0]})
        self.assertEqual(got["never"]["last"], {"day": None, "read": False, "runs": [0, 0]})
        self.assertEqual(got["no-log"]["last"], {"day": "2026-09-01", "read": False, "runs": [0, 0]})
        self.assertNotIn("2026-09-01", got["monthly"]["runs"])
        with mock.patch.object(reports_screen, "TAIL_BYTES", 60):
            self.assertEqual(reports_screen.cadence(jobs[:1], logs, DAYS)["monthly"]["last"]["read"], False)

    def test_an_outcome_marker_after_job_output_with_no_newline_still_counts(self):
        # cron-jobs.sh's own example: `JOB_COMMAND='printf error; exit 7'` writes `error[demo] ... FAILED rc=7`.
        path = self.dir / "demo.log"
        path.write_text("[demo] 2026-09-09T02:15:00-0600 starting (cwd: /tmp)\n"
                        "error[demo] 2026-09-09T02:15:01-0600 FAILED rc=7\n"
                        "[demo] 2026-09-10T02:15:00-0600 starting (cwd: /tmp)\n"
                        "all fine[demo] 2026-09-10T02:15:01-0600 done\n", encoding="utf-8")
        got = reports_screen.job_log(path, DAYS)
        self.assertEqual(got["runs"]["2026-09-09"], [0, 1])
        self.assertEqual(got["runs"]["2026-09-10"], [1, 0])

    def test_a_log_longer_than_the_read_says_where_the_read_began(self):
        lines = [f"[big] 2026-09-0{n}T01:00:00-0600 done" for n in range(4, 10) for _ in range(200)]
        path = self.dir / "big.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with mock.patch.object(reports_screen, "TAIL_BYTES", 40 * 300):
            got = reports_screen.job_log(path, DAYS)
        self.assertEqual(got["from"], "2026-09-04")
        self.assertEqual(got["read_from"], "2026-09-08")
        self.assertEqual(got["runs"]["2026-09-04"], [0, 0])
        self.assertEqual(got["runs"]["2026-09-09"], [200, 0])

    def test_a_failed_source_is_null_with_its_reason_and_the_rest_still_count(self):
        self.jobs.refuse = "launchctl is not here"
        doc = self.doc()
        self.assertIsNone(doc["jobs"])
        self.assertIsNone(doc["cadence"])
        self.assertEqual(doc["sources"]["jobs"], "launchctl is not here")
        self.assertIn("job list was not read", doc["sources"]["cadence"])
        self.assertEqual(len(doc["reports"]), 3)

    def test_families_come_from_the_config_and_a_line_it_cannot_read_is_named(self):
        doc = self.doc()
        self.assertEqual(doc["families"]["state"], "read")
        self.assertEqual([f["key"] for f in doc["families"]["list"]], ["backups", "repos", "host", "agents"])
        missing = self.doc(config=self.dir / "report-families.conf")["families"]
        self.assertEqual(missing, {"state": "missing", "source": "<config>/project-dashboard/report-families.conf", "problems": [], "list": []})
        found, problems = reports_screen.parse_families("\n".join([
            "family|a|A|bot|one,two",
            "family|b|B|bot",
            "family|Bad Key|C|bot|three",
            "family|c|C|bot|two",
            "family|a|Again|bot|four",
            "family|d|D|bot|Not A Job",
            "family|e|E|heart-pulse|five",
            "family|f|F|bot|six,seven,six",
        ]))
        self.assertEqual([f["key"] for f in found], ["a", "e"])
        self.assertEqual([p.split(":", 1)[0] for p in problems], ["line 2", "line 3", "line 4", "line 5", "line 6", "line 8"])
        self.assertIn("two is already in the family a", problems[2])
        # A job twice within one line is refused too: the cadence table would list it twice and the lamp count it twice.
        self.assertIn("six is listed twice in the family f", problems[5])

    def test_the_clean_preview_is_the_librarys_and_writes_nothing(self):
        before = tuple(self.connection.iterdump())
        got = reports_screen.clean(self.connection, "2026-09-08", now=LATER)
        self.assertEqual(got["selected"], [self.ids["clean"]])
        self.assertEqual([d["id"] for d in got["declined"]], [])
        got = reports_screen.clean(self.connection, "2026-09-09", now=LATER)
        self.assertEqual(got["selected"], [self.ids["clean"]])
        self.assertEqual([d["id"] for d in got["declined"]], [self.ids["failed"]])
        self.assertEqual(tuple(self.connection.iterdump()), before)
        with self.assertRaises(workflow.WorkflowError):
            reports_screen.clean(self.connection, "2026-09-08T01:00:00Z", now=LATER)


class ThePage(BrowserSession):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.backend = Jobs(self.dir, jobs=[("quiet-one", "idle", 0, None)])
        super().setUp()
        self.enterContext(mock.patch.object(reports_screen, "_html_folders", lambda: 0))

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/reports")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Reports · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "reports.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_old_screen_stays_in_the_palette(self):
        self.assertEqual(v2.SECTIONS.get("Reports"), "/reports")
        self.assertNotIn("Reports", v2.CLASSIC)
        self.assertEqual(v2.SCREENS.get("Reports (classic)"), "/operations?area=reports")

    def test_the_data_routes_need_a_session_and_the_reading_takes_no_query(self):
        self.assertEqual(self.request("/api/reports")[0], 403)
        self.assertEqual(self.request("/api/reports/clean?before=2026-09-01")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/reports?act=1", headers=cookie)[0], 400)
        status, _, body = self.request("/api/reports", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual((doc["reports"], [j["name"] for j in doc["jobs"]], doc["sources"]["cadence"]), ([], ["quiet-one"], ""))

    def test_the_clean_preview_takes_one_date_and_writes_nothing(self):
        cookie = {"Cookie": self.cookie}
        for query in ("", "?before=2026-09-01&before=2026-09-02", "?before=2026-09-01&x=1"):
            self.assertEqual(self.request("/api/reports/clean" + query, headers=cookie)[0], 400, query)
        status, _, body = self.request("/api/reports/clean?before=yesterday", headers=cookie)
        self.assertEqual(status, 400)
        self.assertIn("YYYY-MM-DD", json.loads(body)["error"])
        before = self.snapshot()
        status, _, body = self.request("/api/reports/clean?before=2026-09-01", headers=cookie)
        self.assertEqual((status, json.loads(body)["selected"]), (200, []))
        self.assertEqual(self.snapshot(), before)


# The stand-in additions reports.js needs beyond test_v2_tasks: the shell's url, context and views.
SHELL_MORE = r"""
OUT.urls = []; window.shell.url = q => OUT.urls.push(q); window.shell.setContext = () => {}; OUT.picks = [];
window.shell.commands.pick = id => OUT.picks.push(id); window.shell.commands.list = () => REG;
"""


class TheScript(ScreenCase):
    """reports.js against the document `reports_screen` builds from the seeded store and logs."""

    def setUp(self):
        super().setUp()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ids = seed(self)
        with mock.patch.object(reports_screen, "_html_folders", lambda: 2):
            self.doc = reports_screen.document(self.connection, now=LATER, jobs=self.jobs, tz=TZ, config=EXAMPLE)

    def served(self, tail=None):
        with mock.patch.object(reports_screen, "_html_folders", lambda: 2), \
             mock.patch.object(reports_screen, "TAIL_BYTES", tail or reports_screen.TAIL_BYTES):
            return reports_screen.document(self.connection, now=LATER, jobs=self.jobs, tz=TZ, config=EXAMPLE)

    def run_page(self, body, answer=None, doc=None):
        doc = doc or self.doc
        answer = answer or f"(path, body) => path.startsWith('/api/reports/clean') ? [200, CLEAN] : path === '/api/reports' ? [200, {json.dumps(doc)}] : [200, {{}}]"
        clean = {"before": "2026-09-08T00:00:00+00:00", "count": 1, "max_batch": 1000, "selected": [self.ids["clean"], 99999],
                 "declined": [{"id": self.ids["failed"], "why": "it needs attention"}]}
        script = (STAND_IN + f"var CLEAN = {json.dumps(clean)};\n" + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + f"\nANSWER = {answer};\n" + REPORTS_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.undo = REG.filter(c => c.undo).map(c => c.id);")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        # No command declares an Undo: sd-db has no verb that reopens a report.
        self.assertEqual(out["R"]["undo"], [])

    def test_acknowledge_posts_the_route_with_the_revision_offers_no_undo_and_reads_again(self):
        n = self.ids["clean"]
        revision = workflow.item_state(self.connection, n)["revision"]
        out = self.run_page(f"shellRun(cmd('report.ack'), C.get('r{n}')); await flush();")
        self.assertEqual(out["posts"], [[f"/api/reports/{n}/acknowledge", {"revision": revision}, 64]])
        self.assertEqual(out["toasts"][-1], [f"Acknowledged · #{n}. No Undo: sd-db has no verb that reopens a report", False])
        self.assertEqual(out["gets"], ["/api/reports", "/api/reports"])

    def test_acknowledge_is_off_for_a_report_a_followup_holds_and_for_an_acknowledged_one(self):
        note = next(r for r in self.doc["reports"] if r["id"] == self.ids["failed"])["followups"]
        out = self.run_page(f"""R.when = [cmd('report.ack').when(C.get('r{self.ids['failed']}')), cmd('report.ack').when(C.get('r{self.ids['done']}')),
  cmd('report.ack').when(C.get('r{self.ids['clean']}'))];""")
        self.assertEqual(out["R"]["when"], [f"an open followup holds it: sd note resolve {note[0]}", "already acknowledged", True])

    def test_the_cadence_and_the_lamps_read_the_logs(self):
        doc = json.loads(json.dumps(self.doc))
        doc["families"]["list"] = [{"key": "sync", "label": "Sync", "icon": "bot", "jobs": ["nightly-sync"]},
                                   {"key": "scan", "label": "Scan", "icon": "bot", "jobs": ["weekly-scan"]}]
        out = self.run_page("R.cad = ELS['cad-body'].html; R.lamps = ELS.annunciator.html; R.tally = ELS['cad-tally'].html;"
                            " R.marks = DAYS.map(d => mark('nightly-sync', d)[2]); R.weekly = DAYS.map(d => mark('weekly-scan', d)[0]);",
                            doc=doc)
        self.assertEqual(out["R"]["marks"], ["no log yet", "no log yet", "ran", "ran", "ran", "ran", "2 failed"])
        self.assertEqual(out["R"]["weekly"], ["none", "none", "ok", "none", "none", "none", "none"])
        self.assertRegex(out["R"]["lamps"], r'data-fam="sync" data-state="warning"')
        self.assertRegex(out["R"]["lamps"], r'data-fam="scan" data-state="ok"')
        self.assertIn("■ 1 failed last run", out["R"]["tally"])
        self.assertEqual(out["attention"], {"state": "warning", "n": 1, "what": "reports want you"})

    def test_a_family_lamp_without_evidence_for_a_job_is_unknown_not_all_clear(self):
        def lamps(doc, jobs=("weekly-scan",)):
            doc["families"]["list"] = [{"key": "scan", "label": "Scan", "icon": "bot", "jobs": list(jobs)}]
            return self.run_page("R.lamps = ELS.annunciator.html;", doc=doc)["R"]["lamps"]

        unread = json.loads(json.dumps(self.doc))
        unread["cadence"], unread["sources"]["cadence"] = None, "no log root"
        # The other cases are documents the server builds from the logs and calendars, so the page reads what it would.
        cut = self.served(tail=60)
        self.assertEqual(cut["cadence"]["weekly-scan"]["read_from"], "2026-09-06")
        (self.jobs.cron_root / "logs" / "weekly-scan.log").unlink()
        no_log = self.served()
        self.jobs.schedules["weekly-scan"] = [{"Day": 1, "Hour": 3}]
        idle = self.served()
        self.assertEqual(idle["cadence"]["weekly-scan"]["scheduled"], [False] * len(DAYS))
        cases = {"cadence not read": lamps(unread), "job not installed": lamps(json.loads(json.dumps(self.doc)), ("weekly-scan", "gone-job")),
                 "no log": lamps(no_log), "no log, nothing scheduled this week": lamps(idle), "its last run was not read": lamps(cut)}
        for case, html in cases.items():
            with self.subTest(case):
                self.assertRegex(html, r'data-fam="scan" data-state="unknown"')
                self.assertNotIn("all clear", html)
                self.assertIn("not read", html)
        self.assertIn("gone-job (not read)", cases["job not installed"])
        # A job scheduled less often than weekly: the lamp follows its own last scheduled run, read or not.
        def monthly(last):
            doc = json.loads(json.dumps(self.doc))
            doc["cadence"]["monthly"] = {"log": True, "from": "2026-01-01", "read_from": None, "runs": {d: [0, 0] for d in DAYS},
                                         "scheduled": [False] * len(DAYS), "last": last}
            return lamps(doc, ("weekly-scan", "monthly"))
        self.assertRegex(monthly({"day": "2026-09-01", "read": False, "runs": [0, 0]}), r'data-fam="scan" data-state="unknown"')
        self.assertRegex(monthly({"day": "2026-01-01", "read": False, "runs": [0, 0]}), r'data-fam="scan" data-state="unknown"')
        self.assertRegex(monthly({"day": None, "read": False, "runs": [0, 0]}), r'data-fam="scan" data-state="unknown"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [0, 1]}), r'data-fam="scan" data-state="warning"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [1, 1]}), r'data-fam="scan" data-state="caution"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [0, 0]}), r'data-fam="scan" data-state="caution"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [1, 0]}), r'data-fam="scan" data-state="ok"')
        # With every job's log read and no failure, the lamp is still all clear.
        self.assertRegex(lamps(json.loads(json.dumps(self.doc))), r'data-fam="scan" data-state="ok"')

    def test_the_day_a_cut_read_begins_on_is_unknown_not_clean_failed_or_missing(self):
        # A cut read can start partway through its first day, so that day was not read in full.
        doc = json.loads(json.dumps(self.doc))
        sync = doc["cadence"]["nightly-sync"]
        sync["read_from"] = DAYS[4]
        sync["runs"][DAYS[3]], sync["runs"][DAYS[4]], sync["runs"][DAYS[5]] = [0, 0], [1, 0], [0, 0]
        cases = {"clean": [1, 0], "failed": [0, 1], "no run logged": [0, 0]}
        for case, runs in cases.items():
            with self.subTest(case):
                sync["runs"][DAYS[4]] = runs
                out = self.run_page(f"R.marks = [3, 4, 5].map(i => mark('nightly-sync', DAYS[i]));", doc=doc)
                self.assertEqual(out["R"]["marks"][0], ["unknown", "▨", "not read"])
                self.assertEqual(out["R"]["marks"][1], ["unknown", "▨", "not read"])
                self.assertEqual(out["R"]["marks"][2], ["caution", "▲", "no run logged"])

    def test_a_job_that_has_not_run_yet_today_is_not_a_gap(self):
        doc = json.loads(json.dumps(self.doc))
        doc["cadence"]["nightly-sync"]["runs"][DAYS[6]] = [0, 0]
        out = self.run_page("R.today = mark('nightly-sync', DAYS[6]); R.last = lastState('nightly-sync');", doc=doc)
        self.assertEqual(out["R"]["today"], ["none", "·", "no run yet today"])
        self.assertEqual(out["R"]["last"], "ok")

    def test_the_state_names_status_mail_a_missing_family_list_and_a_failed_source(self):
        doc = json.loads(json.dumps(self.doc))
        doc["families"] = {"state": "missing", "source": "<config>/project-dashboard/report-families.conf", "problems": [], "list": []}
        doc["cadence"], doc["sources"]["cadence"] = None, "no log root"
        out = self.run_page("R.lamps = ELS.annunciator.html; R.cad = ELS['cad-body'].html;", doc=doc)
        last = out["states"][-1]
        self.assertEqual(last["kind"], "partial")
        self.assertIn(reports_screen.MAIL_REASON, last["text"])
        self.assertIn("cadence (no log root)", last["text"])
        self.assertIn("project-dashboard/report-families.conf.example", last["text"])
        self.assertIn("no family list", out["R"]["lamps"])
        self.assertIn("The run cadence was not read: no log root.", out["R"]["cad"])
        out = self.run_page("", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])

    def test_select_clean_picks_the_servers_selection_and_names_each_decline(self):
        out = self.run_page("ELS['ack-date'].value = '2026-09-08'; await selectClean(); R.sel = ELS['ack-sel'].html; R.dec = ELS['ack-dec'].html;"
                            " R.sum = ELS['ack-sum'].textContent; R.shown = shown.map(r => r.n);")
        self.assertIn("/api/reports/clean?before=2026-09-08", out["gets"])
        self.assertEqual(out["picks"], [f"r{self.ids['clean']}"])
        self.assertEqual(out["R"]["shown"], [self.ids["clean"]])
        self.assertIn(f"#{self.ids['failed']}</b> nightly-sync: it needs attention", out["R"]["dec"])
        self.assertIn("1 more are older than the newest 200", out["R"]["sum"])

    def test_details_show_the_report_its_source_from_home_and_the_diff_with_the_previous_one(self):
        out = self.run_page(f"open('r{self.ids['failed']}'); R.failed = ELS.details.html; open('r{self.ids['clean']}'); R.clean = ELS.details.html;")
        self.assertIn("~/repos/system/local-cron-jobs/logs/nightly-sync.log", out["R"]["failed"])
        self.assertNotIn("/home/example", out["R"]["failed"])
        self.assertIn("An open followup", out["R"]["failed"])
        self.assertIn("Last 1400 characters shown.", out["R"]["clean"])
        self.assertIn("Since the previous report", out["R"]["clean"])
        self.assertIn('class="del">old', out["R"]["clean"])

    def test_retry_posts_the_job_route_with_its_revision(self):
        job = next(j for j in self.doc["jobs"] if j["name"] == "nightly-sync")
        out = self.run_page("shellRun(cmd('jobs.retry'), C.get('job:nightly-sync')); await flush(); R.off = cmd('jobs.retry').when(C.get('job:weekly-scan'));")
        self.assertEqual(out["posts"], [["/api/jobs/nightly-sync/retry", {"revision": job["revision"]}, 64]])
        self.assertEqual(out["toasts"][-1], ["Retry started · nightly-sync", False])
        idle = next(j for j in self.doc["jobs"] if j["name"] == "weekly-scan")
        self.assertEqual(out["R"]["off"], idle["capabilities"]["retry"]["reason"])
        self.assertIn("can retry", out["R"]["off"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", REPORTS_JS)
        self.assertNotIn("setAttribute('style'", REPORTS_JS)
        self.assertNotRegex(REPORTS_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", REPORTS_JS)
        self.assertNotIn("location.reload", REPORTS_JS)
        self.assertNotRegex(REPORTS_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", REPORTS_JS)
        self.assertNotIn("REPORTS_DATA", REPORTS_JS)


if __name__ == "__main__":
    import unittest

    unittest.main()
