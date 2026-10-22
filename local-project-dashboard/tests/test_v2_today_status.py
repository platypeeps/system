"""Today's Status view (sd:5099): the sections `sd today` and `sd-status` print, on the page.

What this slice promises: `/api/today/status` is the pack's `sd_db.today.sections` (pack sd:5098) as it stands, with
the read time, so the view and the command cannot drift; a gate slot read that failed reaches the page as its reason.
`today-status.js` reads only once the view shows; it draws a flow strip whose legend links to each section, the first
5 rows of a section with Show all for the rest, and one line for the empty sections; it says unknown for a gate it
could not read and why a failed read left nothing. It runs under JavaScriptCore against the stand-in page, as
`test_v2_decisions` runs `decisions.js`. The look at 375 px and focus order are a manual check on the pull request.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_db import today
from sd_dashboard import today_status, v2
from sd_dashboard.v2 import pages

from support import NOW, ScreenCase
from test_v2_tasks import MARKUP_JS, SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
SCRIPT = V2 / "static" / "today-status.js"
PAGE_JS = SCRIPT.read_text(encoding="utf-8") if SCRIPT.exists() else ""
UNREAD = "sd is not on PATH, so the load was not read"


class TheDocument(ScreenCase):
    def seed(self):
        repo = self.repo()
        self.item("Blocked on the operator", repo=repo, status="blocked")
        self.item("Nobody scheduled this", repo=repo, priority=2)
        self.item("Due next week", repo=repo, due="2026-09-10")
        asked = self.item("Pick a store", repo=repo)
        self.note(asked, "Which store?\nOption: A\nOption: B", kind="question")

    def test_the_document_is_the_packs_sections_with_the_read_time(self):
        self.seed()
        found = today_status.document(self.connection, now=NOW, gate=lambda: UNREAD)
        self.assertEqual(found, {"now": NOW, **today.sections(self.connection, now=NOW, gate=UNREAD)})
        self.assertEqual([row["title"] for row in found["blocked"]], ["Blocked on the operator"])
        self.assertEqual([row["title"] for row in found["planned"]], ["Due next week"])
        self.assertEqual(len(found["decisions"]), 1)
        json.dumps(found)  # the page reads it as JSON

    def test_a_gate_read_that_failed_is_its_reason_never_an_idle_gate(self):
        found = today_status.document(self.connection, now=NOW, gate=lambda: UNREAD)
        self.assertEqual(found["landing"]["gate"], {"holders": [], "waiters": [], "error": UNREAD})
        held = today_status.document(self.connection, now=NOW, gate=lambda: {
            "holders": [{"slot": 1, "label": "b1 tests", "since": NOW, "pid": 9}], "waiters": []})
        self.assertEqual(held["landing"]["gate"], {"holders": [{"slot": 1, "label": "b1 tests", "since": NOW}],
                                                   "waiters": [], "error": None})

    def test_the_today_page_serves_the_read(self):
        page = next(page for page in pages.PAGES if page.section == "Today")
        self.assertIn("/api/today/status", [api.path for api in page.api])
        html = (V2 / "today.html").read_text(encoding="utf-8")
        self.assertIn('<div id="view-status"', html)
        self.assertLess(html.index('src="/ui/today-status.js"'), html.index('src="/ui/shell.js"'))
        # Status follows Inbox since sd:5781, the views by job: Now, Inbox, Status, Work.
        self.assertIn("{ id: 'inbox', label: 'Inbox', key: 'm' }, { id: 'status', label: 'Status' },", (V2 / "static" / "today.js").read_text(encoding="utf-8"))


class TheRoute(BrowserSession):
    def test_the_session_the_page_opens_reads_the_sections(self):
        self.item("Blocked on the operator", repo=self.repo(), status="blocked")
        with mock.patch.object(today_status, "_gate", lambda: UNREAD):
            status, _, body = self.request("/api/today/status", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        found = json.loads(body)
        self.assertEqual([key for key in found if key in today.ORDER], list(today.ORDER))
        self.assertEqual([row["title"] for row in found["blocked"]], ["Blocked on the operator"])
        self.assertEqual(found["landing"]["gate"]["error"], UNREAD)
        refused, _, _ = self.request("/api/today/status")
        self.assertNotEqual(refused, 200)


def row(n, **changes):
    return {"id": n, "repo": "~/repos/system", "status": "planning", "title": f"Item {n}", "priority": 3,
            "created_at": "2026-10-18T10:00:00Z", **changes}


def doc(**changes):
    found = {"now": "2026-10-19T16:00:00Z", "attention": [], "operations": {"host": None, "findings": [], "gate": None},
             "decisions": [],
             "landing": {"entries": [], "gate": {"holders": [], "waiters": [], "error": None}},
             "delivered": [], "planned": [], "blocked": [], "unscheduled": [], "followups": [], "work": [],
             "closed_by_hand": []}
    found.update(changes)
    found["summary"] = {**{key: len(found[key]) for key in today.ORDER if key not in ("landing", "operations")},
                        "operations": len(found["operations"]["findings"]),
                        "landing": len(found["landing"]["entries"]), "planned_this_week": len(found["planned"]),
                        "closed_by_hand": len(found["closed_by_hand"]), "review_questions": 0}
    return found


class TheScript(unittest.TestCase):
    """today-status.js against the stand-in page: what it reads and what it draws."""

    def run_page(self, body, answer):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + "\nANSWER = " + answer + ";\n" + PAGE_JS
                  + "\nvar R = {};\nconst show = () => document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'status', first: true } }));\n"
                  + "const more = key => ELS['status-sections'].listeners.click[0]({ target: { closest: s => s === 'button[data-st-more]'"
                    " ? { dataset: { stMore: key } } : null } });\n"
                  + "(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_nothing_is_read_until_the_view_shows(self):
        out = self.run_page("R.sections = ELS['status-sections'].html;", f"(path, body) => [200, {json.dumps(doc())}]")
        self.assertEqual(out["gets"], [])
        self.assertIn("Reading status", out["R"]["sections"])

    def test_a_section_shows_five_rows_and_show_all_opens_the_rest_in_place(self):
        found = doc(unscheduled=[row(n) for n in range(1, 8)], blocked=[row(20, status="blocked", why="<b>the operator</b>",
                                                                            since="2026-10-19T12:00:00Z")])
        out = self.run_page("show(); await flush(); R.first = ELS['status-sections'].html; more('unscheduled'); R.all = ELS['status-sections'].html;",
                            f"(path, body) => [200, {json.dumps(found)}]")
        self.assertEqual(out["gets"], ["/api/today/status"])
        first, every = out["R"]["first"], out["R"]["all"]
        unscheduled = first[first.index('id="st-unscheduled"'):]
        self.assertEqual(unscheduled.count("<tr data-item="), 5)
        self.assertIn('data-st-more="unscheduled" aria-expanded="false">Show all 7</button>', first)
        self.assertEqual(every[every.index('id="st-unscheduled"'):].count("<tr data-item="), 7)
        self.assertIn("Show the first 5", every)
        self.assertIn("&lt;b&gt;the operator&lt;/b&gt;", first)
        self.assertIn('href="/tasks?row=20"', first)
        self.assertLess(first.index('id="st-blocked"'), first.index('id="st-unscheduled"'))
        # The empty sections share one line, in the command's order.
        self.assertIn("Nothing in Attention · Decisions · Landing now · Delivered · Planned · Followups.", first)
        self.assertNotIn('id="st-attention"', first)

    def test_the_unscheduled_list_filters_by_repo(self):
        # sd:5653: a Repo select on Unscheduled draws one repo's rows; its options count each repo.
        found = doc(unscheduled=[row(1), row(2, repo="~/repos/platypeeps/sd-ai-command-pack"), row(3)])
        pick = ("ELS['status-sections'].listeners.change[0]({ target: { closest: s => s === 'select[data-st-repo]'"
                " ? { value: 'sd-ai-command-pack' } : null } });")
        out = self.run_page("show(); await flush(); R.first = ELS['status-sections'].html; " + pick + " R.one = ELS['status-sections'].html;",
                            f"(path, body) => [200, {json.dumps(found)}]")
        first, one = out["R"]["first"], out["R"]["one"]
        self.assertIn('<option value="">All repos (3)</option>', first)
        self.assertIn('<option value="system">system (2)</option>', first)
        self.assertEqual(first[first.index('id="st-unscheduled"'):].count("<tr data-item="), 3)
        part = one[one.index('id="st-unscheduled"'):]
        self.assertEqual(part.count("<tr data-item="), 1)
        self.assertIn('<tr data-item="2">', part)
        self.assertIn('<option value="sd-ai-command-pack" selected>', part)

    def test_the_flow_strip_draws_each_section_with_work_and_its_key_links_to_every_section(self):
        found = doc(delivered=[row(5, pr=12, commit="a" * 40, at="2026-10-19T15:00:00Z")], unscheduled=[row(6)],
                    decisions=[row(7, note=99, item_title="Pick a store", title="Which store?", at="2026-10-19T15:00:00Z")])
        out = self.run_page("show(); await flush(); R.flow = ELS['status-flow'].html; R.tally = ELS['status-tally'].html;",
                            f"(path, body) => [200, {json.dumps(found)}]")
        flow, tally = out["R"]["flow"], out["R"]["tally"]
        self.assertEqual(flow.count("<rect "), 2)
        self.assertIn('class="seg-delivered"', flow)
        self.assertIn('class="seg-unscheduled"', flow)
        self.assertIn('aria-label="Delivered, 24 h 1, Landing now 0, Planned 0, Unscheduled 1, Blocked 0"', flow)
        for key in ("delivered", "unscheduled"):
            self.assertIn(f'<a href="#st-{key}">', flow)
        for key in ("landing", "planned", "blocked"):
            self.assertNotIn(f'href="#st-{key}"', flow)
            self.assertIn(f'<span class="sw seg-{key}" aria-hidden="true"></span>', flow)
        self.assertIn('<a class="g-caution" href="#st-decisions">▲ 1 decisions</a>', tally)
        self.assertIn('href="#view=work">0 in your work</a>', tally)

    def test_the_gate_line_says_unknown_with_the_reason_and_names_the_holders(self):
        # The gate line sits under the strip: an unread gate shows with no lane entry too.
        idle = {"entries": [], "gate": {"holders": [], "waiters": [], "error": UNREAD}}
        out = self.run_page("show(); await flush(); R.flow = ELS['status-flow'].html;",
                            f"(path, body) => [200, {json.dumps(doc(landing=idle))}]")
        self.assertIn(f"Gate slots unknown: {UNREAD}", out["R"]["flow"])
        lane = {"entries": [row(8, status="running", place=1, since="2026-10-19T15:30:00Z")],
                "gate": {"holders": [{"slot": 1, "label": "b1 tests", "since": "2026-10-19T15:30:00Z"}],
                         "waiters": [{"place": 1, "label": "b2 tests", "since": None}], "error": None}}
        held = self.run_page("show(); await flush(); R.flow = ELS['status-flow'].html; R.sections = ELS['status-sections'].html;",
                             f"(path, body) => [200, {json.dumps(doc(landing=lane))}]")
        self.assertIn("Gate slots: 1 holding (b1 tests) · 1 waiting", held["R"]["flow"])
        self.assertIn('class="g g-ok" title="ok">●', held["R"]["sections"])
        self.assertIn("position 1", held["R"]["sections"])

    def test_a_section_with_no_rows_shows_when_it_counts_what_it_leaves_out(self):
        found = doc(closed_by_hand=[row(30, at="2026-10-19T15:00:00Z", pr=None, commit=None)])
        out = self.run_page("show(); await flush(); R.sections = ELS['status-sections'].html;",
                            f"(path, body) => [200, {json.dumps(found)}]")
        delivered = out["R"]["sections"]
        self.assertIn('id="st-delivered"', delivered)
        self.assertIn("+1 closed by hand", delivered)
        self.assertNotIn("<table", delivered)
        self.assertIn("Nothing in Attention · Decisions · Landing now · Planned · Blocked · Unscheduled · Followups.", delivered)

    def test_a_failed_read_says_why_and_draws_no_count(self):
        out = self.run_page("show(); await flush(); R.sections = ELS['status-sections'].html; R.tally = ELS['status-tally'].html;",
                            "(path, body) => [500, { error: 'database is locked' }]")
        self.assertIn("Status could not be read: database is locked. Refresh tries again.", out["R"]["sections"])
        self.assertEqual(out["R"]["tally"], "")


if __name__ == "__main__":
    unittest.main()
