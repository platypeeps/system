"""The v2 Metrics page (sd:2119).

What this slice promises: `/metrics` answers under the shared policy and loads
its script before `shell.js`; `/api/metrics` carries the month exactly as
`sd usage` reads it, the 7-day window's last meter reading of each day for a
week, the week's numbers, the provider scorecard, four
weeks of skill use, age in status and observed activity. Each part is guarded
on its own, so one that fails is that part's error and the page shows it as
unknown with the reason. By model, CI health and flaky tests have no reader:
the document names each with its reason, and the page never draws them empty.

Cap and Configure (sd:3387) post `/api/bills/<bill>/cap` and
`/api/providers/configure` with the registry revision `/api/metrics` read. A
400 or 409 wrote nothing; any other failure leaves the outcome unknown.

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

from sd_db import provider_controls, reads, transition, usage, workflow
from sd_db.writes import record_skill_use

from sd_dashboard import metrics_screen, server, v2

from support import NOW, ScreenCase, record_cost
from test_v2_today import OSASCRIPT, Refused
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar

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

    def doc(self):
        return metrics_screen.document(self.connection, now=NOW)


#: `kimi` calls a URL on the capped company bill; `claude` is a `start` entry on `sub`, so `sub` takes no cap.
REGISTRY = """bills:
  capped: {cost: company}
  plan: {cost: plan}
  sub: {cost: subscription}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: sub, roles: [author, reviewer], reader: claude-json}
  kimi: {url: "https://example.invalid/v1", model: fixture, vendor: moonshot, bill: capped, roles: [author, reviewer]}
roles:
  author: [claude, kimi]
  reviewer: [kimi, claude]
"""


class Controlled(Fixture):
    """The fixture with a provider registry beside its database, as the server finds it."""

    def setUp(self):
        super().setUp()
        (self.path.parent / "providers.yaml").write_text(REGISTRY)


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

    def test_the_slow_parts_come_from_the_kept_reads_with_when_they_were_observed(self):
        """sd:4955: the month and the week numbers take seconds; the server keeps them, so the request reads none."""
        kept = {name: metrics_screen.slow(self.connection, name, now=NOW) for name in metrics_screen.SLOW}
        answers = {"usage": dict(kept["usage"], observed="2026-09-06T11:59:00Z"),
                   "numbers": dict(kept["numbers"], observed="2026-09-06T11:58:00Z", stale="git did not answer")}
        with mock.patch.object(usage, "read", side_effect=AssertionError("usage.read ran")), \
                mock.patch.object(reads, "weekly_numbers", side_effect=AssertionError("weekly_numbers ran")):
            doc = metrics_screen.document(self.connection, now=NOW, kept=answers.__getitem__)
        self.assertEqual(doc["usage"], answers["usage"])
        self.assertEqual(doc["numbers"], answers["numbers"])
        self.assertEqual(kept["usage"], self.doc()["usage"])
        self.assertEqual(kept["numbers"], self.doc()["numbers"])

    def test_a_kept_part_with_no_reading_is_its_error_and_a_stale_month_lights_no_lamp(self):
        def kept(name):
            raise sqlite3.OperationalError("database is locked")

        doc = metrics_screen.document(self.connection, now=NOW, kept=kept)
        self.assertEqual((doc["usage"], doc["numbers"]), ({"error": "OperationalError: database is locked"},) * 2)
        stale = dict(self.doc()["usage"], observed="2026-09-06T11:31:00Z", stale="database is locked")
        self.assertEqual(metrics_screen._attention(stale, self.doc()["trend"]),
                         {"state": "unknown", "n": 0, "what": "readings not read"})

    def test_the_panels_no_reader_covers_are_named_with_their_reason(self):
        unread = {u["id"]: u["reason"] for u in self.doc()["unread"]}
        self.assertEqual(sorted(unread), ["ci", "flaky", "model"])
        self.assertTrue(all(unread.values()))

    def test_no_registry_is_the_controls_error_and_the_rest_still_answer(self):
        doc = self.doc()
        self.assertEqual(doc["controls"], {"error": f"RegistryError: no provider registry at {self.path.parent.resolve() / 'providers.yaml'}"})
        self.assertEqual(doc["usage"]["error"], "")


class TheControls(Controlled):
    def test_the_controls_carry_the_revision_the_routes_check_and_each_bills_write(self):
        controls = self.doc()["controls"]
        self.assertEqual(controls["revision"], provider_controls.snapshot(self.connection)["revision"])
        self.assertEqual(controls["orders"], {"author": ["claude", "kimi"], "reviewer": ["kimi", "claude"]})
        self.assertEqual([(p["name"], p["enabled"]) for p in controls["providers"]], [("claude", True), ("kimi", True)])
        self.assertEqual({b["name"]: (b["cap_usd_month"], b["write"], b["why"]) for b in controls["bills"]},
                         {"capped": (100.0, "set", ""), "plan": (None, "set", ""),
                          "sub": (None, "", "a 'start' entry is billed to it, so no cap can be set")})

    def test_a_legacy_cap_on_a_start_bill_takes_the_clear_alone(self):
        self.doc()  # the first read seeds the `sub` row
        with self.connection:
            self.connection.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'sub'")
        controls = self.doc()["controls"]
        sub = next(b for b in controls["bills"] if b["name"] == "sub")
        self.assertEqual((sub["cap_usd_month"], sub["write"]), (None, "clear"))
        self.assertIn("holds a cap of $5.00 that nothing enforces", sub["why"])
        self.assertEqual(len(controls["warnings"]), 1)

    def test_a_bill_name_the_route_does_not_take_has_no_write(self):
        self.assertEqual(metrics_screen._writes("a/b", set(), {}), ("", "its name is outside what the cap route accepts"))


class TheWrites(BrowserSession):
    """The revision the served `/api/metrics` read, posted back to the routes: the page's writes end to end."""

    def setUp(self):
        super().setUp()
        (self.path.parent / "providers.yaml").write_text(REGISTRY)

    def controls(self):
        status, _, body = self.request("/api/metrics", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        return json.loads(body)["controls"]

    def test_the_cap_lands_on_the_read_revision_and_a_second_post_of_it_is_stale(self):
        read = self.controls()
        status, _, answer = self.post("/api/bills/capped/cap", {"revision": read["revision"], "cap_usd_month": 50})
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.connection.execute("SELECT cap_usd_month FROM bill WHERE name = 'capped'").fetchone()[0], 50.0)
        before = self.snapshot()
        self.assertEqual(self.post("/api/bills/capped/cap", {"revision": read["revision"], "cap_usd_month": 60})[0], 409)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.controls()["revision"], answer["revision"])

    def test_configure_lands_on_the_read_revision_and_a_refusal_writes_nothing(self):
        read = self.controls()
        before = self.snapshot()
        refused = {"revision": read["revision"], "enabled": {"claude": True, "kimi": True}, "orders": {"author": ["kimi"], "reviewer": ["kimi"]}}
        self.assertEqual(self.post("/api/providers/configure", refused)[0], 400)
        self.assertEqual(self.snapshot(), before)
        status, _, answer = self.post("/api/providers/configure", {**refused, "orders": {"author": ["kimi", "claude"], "reviewer": ["claude", "kimi"]}})
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.controls()["orders"], {"author": ["kimi", "claude"], "reviewer": ["claude", "kimi"]})


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Metrics · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "metrics.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/metrics")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/metrics?range=30", headers=cookie)[0], 400)
        status, _, body = self.request("/api/metrics", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["numbers"]["error"], "")

    def test_the_rail_opens_metrics_at_its_page_and_keeps_the_classic_usage_screen(self):
        self.assertEqual(v2.SECTIONS["Metrics"], "/metrics")
        self.assertNotIn("Metrics", v2.CLASSIC)


METRICS_SHELL = r"""
window.shell.row = () => null;
window.shell.pages = { Tasks: '/tasks' };
"""


class TheScript(Controlled):
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
        self.assertEqual(out["R"]["reg"], [["providers.configure", "provider", "undo", "f", True],
                                           ["bill.cap", "bill", "undo", "p", True]])

    def test_the_badge_rule_lights_what_the_lamps_light(self):
        # sd:3446: the rule is the server's, and every page's rail shows it; it must count the cells metrics.js lights.
        def lit(doc):
            lamps = self.run_page("R.lamps = ELS.annunciator.html;", doc)["R"]["lamps"]
            states = re.findall(r'data-cell="[\w-]+"[^>]*data-state="(\w+)"', lamps)
            worst = "warning" if "warning" in states else "caution" if "caution" in states else "ok"
            return worst, states.count(worst) if worst != "ok" else 0

        counted = []
        for used, bound in ((80, False), (100, True), (40, False), (None, True)):
            doc = self.doc()
            for meter in doc["usage"]["meter"]:
                meter["used_percent"] = used
            doc["usage"]["bound"] = [{"bill": "example"}] if bound else []
            doc["attention"] = metrics_screen._attention(doc["usage"], doc["trend"])
            self.assertEqual(lit(doc), (doc["attention"]["state"], doc["attention"]["n"]), (used, bound))
            counted.append(doc["attention"]["state"])
        self.assertEqual(counted, ["caution", "warning", "ok", "caution"])
        self.assertEqual(metrics_screen._attention({"error": "OSError: locked"}, {}),
                         {"state": "unknown", "n": 0, "what": "readings not read"})

    def test_each_cell_is_a_reading_or_unknown_with_its_reason(self):
        out = self.run_page("R.lamps = ELS.annunciator.html;")
        self.assertEqual(out["gets"], ["/api/metrics"])
        lamps = out["R"]["lamps"]
        self.assertRegex(lamps, r'data-cell="spend" data-ev="spend" data-state="ok".*?<b>\$80.00</b>')
        self.assertRegex(lamps, r'data-cell="w-claude" data-ev="win:claude:10080" data-state="caution".*?<b>80%</b> used')
        self.assertRegex(lamps, r'data-cell="ci" data-ev="unread:ci" data-state="unknown".*?not read')
        self.assertEqual(cell_grammar(self, lamps)[-1], ("button", None))
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "readings past a threshold"})
        self.assertIsNone(out["states"][-1])

    def test_the_panels_draw_the_readings_and_name_what_is_not_read(self):
        out = self.run_page("""R.bills = ELS.bills.html; R.trends = ELS.trends.html; R.model = ELS.model.html; R.ci = ELS['ci-state'].html;
R.weeks = ELS['su-weeks'].html; R.top = ELS['su-top'].html; R.sc = ELS['sc-body'].html; R.hist = ELS.hist.html;""")
        r = out["R"]
        self.assertRegex(r["bills"], r'data-ev="bill:capped".*?<rect class="fill caution" width="80.0".*?<span class="g-caution" aria-hidden="true">▲</span>\$80.00')
        self.assertRegex(r["bills"], r'data-ev="bill:plan".*?class="track hatch".*?▨ window')
        self.assertEqual(re.findall(r'data-ev="(t:[^"]+)"', r["trends"]), ["t:claude:2026-09-01", "t:claude:2026-09-06"])
        self.assertIn("<b>▨ Not read.</b> the cost ledger records bill and provider, not model", r["model"])
        self.assertIn("GitHub Actions is off", r["ci"])
        self.assertIn("no test-result store exists", r["ci"])
        self.assertEqual(re.findall(r'data-ev="(skill:[^"]+)"', r["top"]), ["skill:sd-ship", "skill:sd-review"])
        self.assertEqual(re.findall(r'data-ev="sc:([^"]+)"', r["sc"]), ["claude", "kimi"])
        self.assertIn('aria-label="3-6d: 1 items, 1 ready to send"', r["hist"])

    def test_without_a_registry_cap_and_configure_are_off_with_the_reason(self):
        (self.path.parent / "providers.yaml").unlink()
        out = self.run_page("""R.cap = cmd('bill.cap').when(OBJ.get('bill:capped')); R.cf = cmd('providers.configure').when(OBJ.get('sc:kimi'));
C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();""")
        self.assertTrue(out["R"]["cap"].startswith("the provider registry was not read: RegistryError: no provider registry at "))
        self.assertEqual(out["R"]["cf"], out["R"]["cap"])
        self.assertEqual(out["posts"], [])
        self.assertIn("controls: RegistryError", out["states"][-1]["text"])

    def test_a_part_that_failed_is_a_partial_read_and_its_panel_is_unknown(self):
        doc = self.doc()
        doc["scorecard"] = {"error": "OperationalError: database is locked"}
        out = self.run_page("R.sc = ELS['sc-body'].html; R.bills = ELS.bills.html;", doc)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("scorecard: OperationalError: database is locked", out["states"][-1]["text"])
        self.assertIn("<b>▨ Not read.</b> OperationalError: database is locked", out["R"]["sc"])
        self.assertIn('data-ev="bill:capped"', out["R"]["bills"])

    def test_a_stale_month_lights_no_lamp_and_the_clock_shows_the_oldest_reading(self):
        doc = self.doc()
        doc["usage"] = dict(doc["usage"], observed="2026-09-06T11:31:00Z", stale="database is locked")
        doc["numbers"] = dict(doc["numbers"], observed="2026-09-06T11:58:00Z")
        doc["attention"] = metrics_screen._attention(doc["usage"], doc["trend"])
        out = self.run_page("R.lamps = ELS.annunciator.html;", doc)
        lamps = out["R"]["lamps"]
        states = dict(re.findall(r'data-cell="([\w-]+)"[^>]*data-state="(\w+)"', lamps))
        self.assertEqual({k: v for k, v in states.items() if k != "ci"}, {"spend": "unknown", "w-claude": "unknown", "bound": "unknown"})
        self.assertIn("<b>11:31</b> UTC", lamps)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("usage: stale since 11:31 UTC: database is locked", out["states"][-1]["text"])

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


WRITE = """
var POSTED = (path, body) => [200, { revision: 'after-' + OUT.posts.length }];
ANSWER = (path, body) => path === '/api/metrics' ? [STATUS, DOC] : POSTED(path, body);
const said = () => (ELS.details.html.match(/data-said="([a-z]+)"[^>]*>(?:<span class="g-unknown" aria-hidden="true">▨<\\/span> )?(.*?)<\\/p>/) || []).slice(1);
"""


class TheControlsScript(Controlled):
    """Cap and Configure in metrics.js: what each posts, and what the page says for each answer."""

    run_page = TheScript.run_page

    def write(self, body, doc=None):
        return self.run_page(WRITE + body, doc)

    def test_cap_posts_the_typed_number_with_the_read_revision_and_undo_posts_the_old_cap(self):
        revision = self.doc()["controls"]["revision"]
        out = self.write("""open('bill:capped'); FIELD_VALUES = { cap: '50' }; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();
R.said = said(); lastUndo().undo(); await flush();""")
        self.assertEqual(out["posts"][0][:2], ["/api/bills/capped/cap", {"revision": revision, "cap_usd_month": 50}])
        self.assertEqual(out["posts"][1][:2], ["/api/bills/capped/cap", {"revision": "after-1", "cap_usd_month": 100.0}])
        self.assertEqual(out["confirms"], ["bill.cap"])
        self.assertEqual(out["toasts"][:1], [["Cap set to $50.00 · capped bill", True]])
        self.assertEqual(out["toasts"][-1], ["Cap set to $50.00 undone · capped bill", False])
        self.assertEqual(out["R"]["said"], ["landed", "Cap set to $50.00."])

    def test_none_clears_the_cap_and_a_start_bill_is_off_with_the_reason(self):
        self.doc()  # the first read seeds the `sub` row the cost below names
        self.cost("run", "2026-09-04T10:00:00Z", provider="claude", bill="sub", role="author", usd=1)
        out = self.write("""FIELD_VALUES = { cap: 'none' }; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();
R.sub = cmd('bill.cap').when(OBJ.get('bill:sub'));""")
        self.assertEqual(out["posts"][0][1]["cap_usd_month"], None)
        self.assertEqual(out["toasts"][0], ["Cap cleared · capped bill", True])
        self.assertEqual(out["R"]["sub"], "a 'start' entry is billed to it, so no cap can be set")

    def test_a_legacy_clear_asks_first_posts_null_and_offers_no_undo(self):
        self.doc()
        self.cost("run", "2026-09-04T10:00:00Z", provider="claude", bill="sub", role="author", usd=1)
        with self.connection:
            self.connection.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'sub'")
        out = self.write("""R.fields = cmd('bill.cap').fields(OBJ.get('bill:sub')); R.ask = cmd('bill.cap').askFirst;
C.run(cmd('bill.cap'), OBJ.get('bill:sub')); await flush();""")
        self.assertEqual((out["R"]["fields"], out["R"]["ask"]), ([], True))
        self.assertEqual(out["posts"][0][:2], ["/api/bills/sub/cap", {"revision": self.doc()["controls"]["revision"], "cap_usd_month": None}])
        self.assertEqual(out["toasts"], [["Cap cleared · sub bill", False]])

    def test_a_400_or_a_409_says_nothing_was_written_and_reads_again(self):
        for status, error in ((400, "a cap must be a finite number"), (409, "provider configuration changed; reload before saving")):
            with self.subTest(status=status):
                out = self.write(f"""POSTED = () => [{status}, {{ error: '{error}' }}];
open('bill:capped'); FIELD_VALUES = {{ cap: '50' }}; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush(); R.said = said();""")
                text = f"capped bill not changed: {error}. Nothing was written."
                self.assertEqual(out["toasts"], [[text, False]])
                self.assertEqual(out["R"]["said"], ["refused", text])
                self.assertEqual(out["gets"], ["/api/metrics", "/api/metrics"])

    def test_any_other_failure_or_no_answer_leaves_the_outcome_unknown(self):
        for answer, error in (("[503, { error: 'database is locked' }]", "database is locked"), ("(() => { throw new Error('connection reset'); })()", "connection reset"),
                              ("[403, { error: 'Open the dashboard again before saving.' }]", "Open the dashboard again before saving.")):
            with self.subTest(error=error):
                out = self.write(f"""POSTED = () => {answer};
open('bill:capped'); FIELD_VALUES = {{ cap: '50' }}; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush(); R.said = said(); R.det = ELS.details.html;""")
                text = f"Not known whether capped bill changed: {error.rstrip('.')}. Metrics was read again; check it before you act again."
                self.assertEqual(out["toasts"], [[text, False]])
                self.assertEqual(out["R"]["said"], ["unknown", text])
                self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span>', out["R"]["det"])
                self.assertNotIn("not changed", json.dumps(out["toasts"]))

    def test_an_undo_that_fails_unknown_is_not_called_not_undone(self):
        out = self.write("""FIELD_VALUES = { cap: '50' }; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();
POSTED = () => [502, { error: 'bad gateway' }]; lastUndo().undo(); await flush();""")
        self.assertEqual(out["toasts"][-1], ["Not known whether the undo landed on capped bill: bad gateway. Metrics was read again; check it before you act again.", False])

    def test_configure_posts_the_whole_configuration_with_blank_fields_kept(self):
        revision = self.doc()["controls"]["revision"]
        out = self.write("""FIELD_VALUES = { enabled: 'kimi, claude', reviewer: 'claude, kimi' }; C.run(cmd('providers.configure'), OBJ.get('sc:kimi')); await flush();
lastUndo().undo(); await flush();""")
        self.assertEqual(out["posts"][0][:2], ["/api/providers/configure", {"revision": revision, "enabled": {"claude": True, "kimi": True},
                                                                         "orders": {"author": ["claude", "kimi"], "reviewer": ["claude", "kimi"]}}])
        self.assertEqual(out["posts"][1][:2], ["/api/providers/configure", {"revision": "after-1", "enabled": {"claude": True, "kimi": True},
                                                                         "orders": {"author": ["claude", "kimi"], "reviewer": ["kimi", "claude"]}}])
        self.assertEqual(out["toasts"][0], ["Providers configured · kimi", True])

    def test_configure_disables_the_providers_left_out_and_refuses_an_unknown_name_before_posting(self):
        out = self.write("""FIELD_VALUES = { enabled: 'kimi' }; C.run(cmd('providers.configure'), OBJ.get('sc:kimi')); await flush();
FIELD_VALUES = { enabled: 'kimi, opus' }; C.run(cmd('providers.configure'), OBJ.get('sc:kimi')); await flush();""")
        self.assertEqual(out["posts"][0][1]["enabled"], {"claude": False, "kimi": True})
        self.assertEqual(len(out["posts"]), 1)
        self.assertEqual(out["toasts"][-1], ["kimi not changed: no provider opus; the registry names claude, kimi.", False])

    def test_a_providers_details_name_its_place_in_each_order(self):
        out = self.write("open('sc:kimi'); R.det = ELS.details.html;")
        self.assertIn("<dt>Author order</dt><dd>2 of 2</dd>", out["R"]["det"])
        self.assertIn("<dt>Reviewer order</dt><dd>1 of 2</dd>", out["R"]["det"])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "metrics", "Metrics", "/metrics", ("/api/metrics",)


if __name__ == "__main__":
    unittest.main()
