"""The v2 Metrics page (sd:2119).

What this slice promises: `/metrics` answers under the shared policy and loads
its script before `shell.js`; `/api/metrics` carries the month exactly as
`sd usage` reads it, the 7-day window's last meter reading of each day for a
week, the week's numbers and missing trailers, the provider scorecard, four
weeks of skill use, age in status and observed activity. Each part is guarded
on its own, so one that fails is that part's error and the page shows it as
unknown with the reason. By model, CI health and flaky tests have no reader:
the document names each with its reason, and the page never draws them empty.

`metrics.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` defines, with the document above as its fetch answer.
The browser half -- the look at 375 px -- is a manual check recorded on the
pull request.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_db import reads, transition, usage, workflow
from sd_db.writes import record_skill_use

from sd_dashboard import metrics_screen, server, v2

from support import NOW, ScreenCase, record_cost
from test_v2_today import OSASCRIPT, Refused
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
METRICS_JS = (V2 / "static" / "metrics.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")


class Fixture(ScreenCase):
    """A month on a capped company bill and a plan bill, a week of window readings, skill use and three open items.

    NOW is Sunday 2026-09-06; its week starts Monday 08-31.
    """

    def setUp(self):
        super().setUp()
        for name in ("kimi", "claude"):
            self.provider(name)
        self.bill("capped", 100, "company")
        self.bill("plan", None, "plan")
        self.cost("run", "2026-09-02T10:00:00Z", provider="kimi", bill="capped", role="author", usd=80)
        self.cost("run", "2026-09-03T10:00:00Z", provider="claude", bill="plan", role="reviewer", usd=0)
        for at, window, pct in (("2026-08-30T10:00:00Z", 10080, 10),   # the day before the trend's seven: not carried
                                ("2026-09-01T08:00:00Z", 10080, 20),
                                ("2026-09-01T20:00:00Z", 10080, 30),   # the day's last reading wins
                                ("2026-09-05T20:00:00Z", 300, 99),     # another window: not on the 7-day trend
                                ("2026-09-06T09:00:00Z", 10080, 80)):
            self.cost("meter", at, provider="claude", bill="plan", window_minutes=window, used_percent=pct)
        for at, skill in (("2026-08-09T09:00:00Z", "sd-ship"),         # the Sunday before the fourth week: not carried
                          ("2026-08-25T09:00:00Z", "sd-ship"),
                          ("2026-09-01T09:00:00Z", "sd-review"),
                          ("2026-09-02T09:00:00Z", "sd-ship"),
                          ("2026-09-03T09:00:00Z", "sd-ship")):
            record_skill_use(self.connection, skill, surface="claude", timestamp=at)
        repo = self.repo()
        self.ready = self.item("Ready to send", status="planning", repo=repo)
        transition(self.connection, self.ready, "ready_to_send", who="sd-ship")
        self.age(self.ready, 4)
        self.item("Open and new", kind="task", repo=repo)
        done = self.item("Finished", kind="task", repo=repo)
        workflow.change_status(self.connection, done, "done", who="test")
        self.connection.commit()

    def bill(self, name, cap, basis):
        self.connection.execute("INSERT OR IGNORE INTO bill VALUES (?, ?, ?)", (name, basis, cap))
        self.connection.commit()

    def cost(self, source, at, **columns):
        row = record_cost(self.connection, source=source, **columns)
        self.connection.execute("UPDATE cost SET timestamp = ? WHERE id = ?", (at, row))
        self.connection.commit()

    def doc(self, trailers=lambda: 3):
        return metrics_screen.document(self.connection, now=NOW, trailers=trailers)


class TheDocument(Fixture):
    def test_the_month_is_sd_usage_s_own_document(self):
        doc = self.doc()["usage"]
        self.assertEqual(doc, {"error": "", **usage.read(self.connection, month=None, now=NOW).document()})
        self.assertEqual({b["name"]: (b["spent"], b["cap"]) for b in doc["bills"]}, {"capped": (80.0, 100.0), "plan": (0.0, None)})

    def test_the_trend_is_the_last_7_day_window_reading_of_each_of_seven_days(self):
        trend = self.doc()["trend"]
        self.assertEqual((trend["days"][0], trend["days"][-1], len(trend["days"])), ("2026-08-31", "2026-09-06", 7))
        self.assertEqual([(s["provider"], [p[:2] for p in s["points"]]) for s in trend["series"]],
                         [("claude", [["2026-09-01", 30.0], ["2026-09-06", 80.0]])])

    def test_skill_use_is_four_monday_weeks_and_this_week_s_top_skills(self):
        skills = self.doc()["skills"]
        self.assertEqual([(w["start"], w["uses"]) for w in skills["weeks"]],
                         [("2026-08-31", 3), ("2026-08-24", 1), ("2026-08-17", 0), ("2026-08-10", 0)])
        self.assertEqual(skills["top"], [["sd-ship", 2], ["sd-review", 1]])
        self.assertEqual((skills["since"], skills["first"]), ("2026-08-10", "2026-08-25"))

    def test_age_counts_open_items_and_splits_out_ready_to_send(self):
        buckets = self.doc()["age"]["buckets"]
        self.assertEqual(sum(b["ready_to_send"] + b["other"] for b in buckets), 2)
        self.assertEqual([(b["label"], b["ready_to_send"]) for b in buckets if b["ready_to_send"]], [("3-6d", 1)])

    def test_a_part_that_fails_is_its_error_and_the_rest_still_answer(self):
        with mock.patch.object(reads, "scorecard", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertEqual(doc["scorecard"], {"error": "OperationalError: database is locked"})
        self.assertEqual(doc["trend"]["error"], "")
        self.assertEqual(doc["usage"]["error"], "")

    def test_a_trailer_walk_past_its_budget_is_stopped_and_says_so(self):
        def over():
            raise reads.OverBudget("the trailer count ran past its budget of 10 seconds")

        self.assertEqual(self.doc(trailers=over)["trailers"],
                         {"error": "the trailer count ran past its budget of 10 seconds and was stopped rather than waited on"})
        self.assertEqual(self.doc()["trailers"], {"error": "", "count": 3})

    def test_the_panels_no_reader_covers_are_named_with_their_reason(self):
        unread = {u["id"]: u["reason"] for u in self.doc()["unread"]}
        self.assertEqual(sorted(unread), ["ci", "flaky", "model"])
        self.assertTrue(all(unread.values()))


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Metrics · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "metrics.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/metrics")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/metrics?range=30", headers=cookie)[0], 400)
        with mock.patch.object(reads, "missing_trailers", return_value=0):
            status, _, body = self.request("/api/metrics", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["trailers"], {"error": "", "count": 0})

    def test_the_rail_opens_metrics_at_its_page_and_keeps_the_classic_usage_screen(self):
        self.assertEqual(v2.SECTIONS["Metrics"], "/metrics")
        self.assertNotIn("Metrics", v2.CLASSIC)


METRICS_SHELL = r"""
window.shell.row = () => null;
window.shell.pages = { Tasks: '/tasks' };
"""


class TheScript(Fixture):
    """metrics.js against the document `metrics_screen` builds from the fixture database."""

    def run_page(self, body, doc=None, status=200):
        doc = doc if doc is not None else self.doc()
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + METRICS_SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "ANSWER = (path, body) => path === '/api/metrics' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + METRICS_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
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
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.risk, c.key, c.executes]);")
        self.assertEqual(out["R"]["reg"], [["providers.configure", "provider", "undo", "f", False],
                                           ["bill.cap", "bill", "undo", "p", False]])

    def test_each_cell_is_a_reading_or_unknown_with_its_reason(self):
        out = self.run_page("R.lamps = ELS.annunciator.html;")
        self.assertEqual(out["gets"], ["/api/metrics"])
        lamps = out["R"]["lamps"]
        self.assertRegex(lamps, r'data-cell="spend" data-state="ok" data-ev="spend".*?<b>\$80.00</b>')
        self.assertRegex(lamps, r'data-cell="w-claude" data-state="caution" data-ev="win:claude:10080".*?<b>80%</b> used')
        self.assertRegex(lamps, r'data-cell="trailers" data-state="caution".*?<b>3</b> commits')
        self.assertRegex(lamps, r'data-cell="ci" data-state="unknown".*?not read')
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 2, "what": "readings past a threshold"})
        self.assertIsNone(out["states"][-1])

    def test_the_panels_draw_the_readings_and_name_what_is_not_read(self):
        out = self.run_page("""R.bills = ELS.bills.html; R.trends = ELS.trends.html; R.model = ELS.model.html; R.ci = ELS['ci-state'].html;
R.weeks = ELS['su-weeks'].html; R.top = ELS['su-top'].html; R.sc = ELS['sc-body'].html; R.hist = ELS.hist.html;""")
        r = out["R"]
        self.assertRegex(r["bills"], r'data-ev="bill:capped".*?<rect class="fill caution" width="80.0"')
        self.assertRegex(r["bills"], r'data-ev="bill:plan".*?class="track hatch".*?▨ window')
        self.assertEqual(re.findall(r'data-ev="(t:[^"]+)"', r["trends"]), ["t:claude:2026-09-01", "t:claude:2026-09-06"])
        self.assertIn("<b>▨ Not read.</b> the cost ledger records bill and provider, not model", r["model"])
        self.assertIn("GitHub Actions is off", r["ci"])
        self.assertIn("no test-result store exists", r["ci"])
        self.assertEqual(re.findall(r'data-ev="(skill:[^"]+)"', r["top"]), ["skill:sd-ship", "skill:sd-review"])
        self.assertEqual(re.findall(r'data-ev="sc:([^"]+)"', r["sc"]), ["claude", "kimi"])
        self.assertIn('aria-label="3-6d: 1 items, 1 ready to send"', r["hist"])

    def test_the_cap_is_off_for_a_window_bill_and_has_no_cli(self):
        out = self.run_page("""var c = REG.find(x => x.id === 'bill.cap');
R.plan = c.when(OBJ.get('bill:plan')); R.capped = c.when(OBJ.get('bill:capped')); R.cli = c.cli(OBJ.get('bill:capped'));
R.run = c.run(OBJ.get('bill:capped')); R.href = location.href;""")
        self.assertEqual(out["R"]["plan"], "a plan bill spends a window, not dollars")
        self.assertIs(out["R"]["capped"], True)
        self.assertEqual(out["R"]["cli"], "no CLI: sd has no verb for bill caps (bill.cap_usd_month, capped, now $100.00)")
        self.assertEqual(out["R"]["href"], "/operations?area=usage")

    def test_a_part_that_failed_is_a_partial_read_and_its_panel_is_unknown(self):
        doc = self.doc()
        doc["scorecard"] = {"error": "OperationalError: database is locked"}
        out = self.run_page("R.sc = ELS['sc-body'].html; R.bills = ELS.bills.html;", doc)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("scorecard: OperationalError: database is locked", out["states"][-1]["text"])
        self.assertIn("<b>▨ Not read.</b> OperationalError: database is locked", out["R"]["sc"])
        self.assertIn('data-ev="bill:capped"', out["R"]["bills"])

    def test_a_document_that_does_not_arrive_leaves_no_reading_and_says_so(self):
        out = self.run_page("""R.before = ELS.annunciator.html;
DOC = { error: 'Open a dashboard page before reading Metrics.' }; STATUS = 403;
DOC_LISTENERS.click.forEach(f => f({ target: { closest: s => s === '#refresh' ? {} : null } })); await flush();
R.lamps = ELS.annunciator.html; R.bills = ELS.bills.html;""")
        self.assertEqual(out["gets"], ["/api/metrics", "/api/metrics"])
        self.assertIn("data-cell=", out["R"]["before"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertNotIn("data-cell=", out["R"]["lamps"])
        self.assertNotIn("data-ev=", out["R"]["bills"])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "Metrics not read"})

    def test_a_selected_reading_shows_its_facts_and_cli_line(self):
        out = self.run_page("open('sc:kimi'); R.det = ELS.details.html;")
        self.assertIn("<h2>kimi</h2>", out["R"]["det"])
        self.assertIn("sd providers configure --file CONFIG.json", out["R"]["det"])

    def test_the_page_keeps_the_shells_keys_and_reads_through_the_shared_reader(self):
        self.assertIn("S.read(spec)", METRICS_JS)
        self.assertNotIn("innerHTML", METRICS_JS)
        self.assertNotRegex(METRICS_JS, r"\.style\.")
        self.assertNotRegex(METRICS_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", METRICS_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "metrics", "Metrics", "/metrics", ("/api/metrics",)


if __name__ == "__main__":
    unittest.main()
