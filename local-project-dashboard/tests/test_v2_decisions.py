"""Today's Decisions section (sd:3012): a pending decision with one button per option.

What this slice promises: a decision is a `question` note with one `Option: <text>`
line per choice; `/api/decisions` lists each unresolved one, with the item's
revision; a click posts `/api/decisions/answer` with the item and note, which writes a `decision`
note through `workflow.add_item_note` and resolves the question through
`workflow.resolve_item_note`, in one transaction under the revision the page
read, so a stale page writes nothing (409). The ruling reads back from
`workflow.item_state`, the document `sd task show` prints. An old prose
"Decision needed" comment, a question with fewer than two options and a
resolved question list nothing and get no buttons.

`decisions.js` runs under JavaScriptCore (osascript), as `test_v2_tasks` runs
`tasks.js`, against the same stand-in page and shell. The browser half -- the
look at 375 px, focus order -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from sd_db import workflow
from sd_dashboard import decisions, v2

from support import ScreenCase
from test_v2_tasks import MARKUP_JS, SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "decisions.js").read_text(encoding="utf-8") if (V2 / "static" / "decisions.js").exists() else ""

ASKED = "Which store holds quick notes?\nThe page keeps them for the visit today.\nOption: A pack store kind\nOption: Workflow database rows"


def seed(case):
    """One item with a decision, a resolved one, a one-option question and an old prose comment; one done item."""
    item = case.item("Notes quick capture", repo=case.repo())
    ids = {"item": item,
           "asked": case.note(item, ASKED, kind="question"),
           "settled": case.note(item, "Pick a colour\nOption: Red\nOption: Blue", kind="question"),
           "single": case.note(item, "Approve it?\nOption: Approve", kind="question"),
           "prose": case.note(item, "Decision needed: (a) a store kind, or (b) database rows?", kind="comment")}
    case.connection.execute("UPDATE note SET resolved_at = ? WHERE id = ?", ("2026-09-06T11:00:00Z", ids["settled"]))
    case.connection.commit()
    closed = case.item("Closed work", status="done")
    ids["closed"] = case.note(closed, "Ship it?\nOption: Yes\nOption: No", kind="question")
    return ids


class TheFormat(unittest.TestCase):
    def test_option_lines_are_the_choices_and_the_rest_is_the_question(self):
        self.assertEqual(decisions.parse(ASKED), ("Which store holds quick notes?\nThe page keeps them for the visit today.",
                                                  ["A pack store kind", "Workflow database rows"]))

    def test_only_a_line_that_starts_with_option_counts_and_a_repeat_counts_once(self):
        body = "Keep it?\n  Option:   Keep  \nOption: Drop\nOption: Keep\nOption:\nThe Option: inline is prose\noption: lower"
        self.assertEqual(decisions.parse(body), ("Keep it?\nThe Option: inline is prose\noption: lower", ["Keep", "Drop"]))

    def test_prose_options_are_not_parsed(self):
        self.assertEqual(decisions.parse("Decision needed: (a) one, or (b) the other?")[1], [])


class TheDocument(ScreenCase):
    def test_only_an_unresolved_question_with_two_options_on_an_open_item_is_listed(self):
        ids = seed(self)
        doc = decisions.document(self.connection)
        self.assertEqual([row["note"] for row in doc["decisions"]], [ids["asked"]])
        row = doc["decisions"][0]
        self.assertEqual((row["item"], row["title"], row["repo"]), (ids["item"], "Notes quick capture", "/repos/system"))
        self.assertEqual(row["question"], "Which store holds quick notes?\nThe page keeps them for the visit today.")
        self.assertEqual(row["options"], ["A pack store kind", "Workflow database rows"])
        self.assertEqual(row["revision"], workflow.item_state(self.connection, ids["item"])["revision"])
        self.assertTrue(row["asked"])

    def test_no_decisions_is_an_empty_list(self):
        self.assertEqual(decisions.document(self.connection)["decisions"], [])


class TheAnswer(ScreenCase):
    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.revision = workflow.item_state(self.connection, self.ids["item"])["revision"]

    def notes(self):
        return {note["id"]: note for note in workflow.item_state(self.connection, self.ids["item"])["notes"]}

    def test_a_choice_writes_a_decision_note_and_resolves_the_question(self):
        state = decisions.answer(self.connection, self.ids["item"], self.ids["asked"], "Workflow database rows",
                                 expected_revision=self.revision, who="dashboard")
        ruling = state["ruling"]
        self.assertEqual((ruling["kind"], ruling["body"], ruling["session"]),
                         ("decision", f"Ruling on note #{self.ids['asked']}: Workflow database rows", "dashboard"))
        notes = self.notes()
        self.assertIsNotNone(notes[self.ids["asked"]]["resolved_at"])
        self.assertEqual(notes[ruling["id"]]["body"], ruling["body"])
        self.assertEqual(state["revision"], workflow.item_state(self.connection, self.ids["item"])["revision"])
        self.assertEqual(decisions.document(self.connection)["decisions"], [])

    def test_the_ruling_reads_back_as_sd_task_show_prints_it(self):
        decisions.answer(self.connection, self.ids["item"], self.ids["asked"], "A pack store kind", expected_revision=self.revision, who="dashboard")
        # The pack's `sd task show` prints each note of `item_state` as `note #<id> · <kind>[ (resolved)]: <body>`.
        lines = [f"note #{n['id']} · {n['kind']}: {n['body']}" for n in self.notes().values() if n["kind"] == "decision"]
        self.assertEqual(lines, [f"note #{max(self.notes())} · decision: Ruling on note #{self.ids['asked']}: A pack store kind"])

    def test_a_stale_revision_writes_nothing(self):
        workflow.add_item_note(self.connection, self.ids["item"], body="a later comment", who="cli")
        before = tuple(self.connection.iterdump())
        with self.assertRaises(workflow.StaleItem):
            decisions.answer(self.connection, self.ids["item"], self.ids["asked"], "A pack store kind", expected_revision=self.revision, who="dashboard")
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_a_choice_the_note_does_not_offer_and_a_note_that_is_no_decision_are_refused(self):
        before = tuple(self.connection.iterdump())
        for note, option in ((self.ids["asked"], "Something else"), (self.ids["settled"], "Red"),
                             (self.ids["single"], "Approve"), (self.ids["prose"], "(a) a store kind")):
            with self.subTest(note=note), self.assertRaises(workflow.WorkflowError) as refused:
                decisions.answer(self.connection, self.ids["item"], note, option, expected_revision=self.revision, who="dashboard")
            self.assertNotIsInstance(refused.exception, workflow.StaleItem)
        for note in (999999, self.ids["closed"]):
            with self.subTest(note=note), self.assertRaises(workflow.MissingNote):
                decisions.answer(self.connection, self.ids["item"], note, "Yes", expected_revision=self.revision, who="dashboard")
        self.assertEqual(tuple(self.connection.iterdump()), before)


class TheRoutes(BrowserSession):
    def test_the_list_and_the_answer_go_through_the_server_and_a_second_click_is_stale(self):
        ids = seed(self)
        status, _, body = self.request("/api/decisions", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        row = json.loads(body)["decisions"][0]
        payload = {"item": ids["item"], "note": ids["asked"], "revision": row["revision"], "option": row["options"][1]}
        status, _, state = self.post("/api/decisions/answer", payload)
        self.assertEqual(status, 200, state)
        self.assertEqual(state["ruling"]["body"], f"Ruling on note #{ids['asked']}: Workflow database rows")
        status, _, refused = self.post("/api/decisions/answer", payload)
        self.assertEqual(status, 409)
        self.assertTrue(refused["reload"])

    def test_a_body_without_a_revision_or_option_is_refused_before_the_database_opens(self):
        ids = seed(self)
        good = {"item": ids["item"], "note": ids["asked"], "revision": "a" * 64, "option": "Red"}
        for payload in ({k: v for k, v in good.items() if k != "item"}, {k: v for k, v in good.items() if k != "revision"},
                        {**good, "revision": "x"}, {**good, "option": ""}, {**good, "note": str(ids["asked"])},
                        {**good, "item": 0}, {**good, "note": 2 ** 63}, {**good, "who": "me"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/decisions/answer", payload)[0], 400)


def decision_row(**changes):
    return {"note": 7, "item": 3012, "title": "Dashboard v2", "repo": "/repos/system", "asked": "2026-10-08T21:00:00Z",
            "question": "Which shape?\n<b>not markup</b>", "options": ["Option lines", "Free text <i>"],
            "revision": "a" * 64, **changes}


class TheScript(unittest.TestCase):
    """decisions.js against a stand-in page: the rows it draws and the posts a click makes."""

    def run_page(self, body, answer):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + "\nANSWER = " + answer + ";\n" + PAGE_JS
                  + "\nvar R = {};\nconst click = (note, i) => ELS.decisions.listeners.click[0]({ target: { closest: s => s === 'button[data-option]'"
                    " ? { dataset: { note: String(note), option: String(i) }, closest: () => null } : null } });\n"
                  + "(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_each_decision_is_a_row_with_one_button_per_option_and_values_stay_text(self):
        doc = json.dumps({"decisions": [decision_row()]})
        out = self.run_page("R.rows = ELS.decisions.html; R.tally = ELS['decisions-tally'].html;",
                            f"(path, body) => [200, {doc}]")
        self.assertEqual(out["gets"], ["/api/decisions"])
        rows = out["R"]["rows"]
        self.assertEqual(rows.count('data-option="'), 2)
        self.assertIn('data-note="7" data-option="0"', rows)
        self.assertIn('data-note="7" data-option="1"', rows)
        self.assertIn("Free text &lt;i&gt;", rows)
        self.assertIn("&lt;b&gt;not markup&lt;/b&gt;", rows)
        self.assertIn('href="/tasks?row=3012"', rows)
        self.assertIn("1 waiting", out["R"]["tally"])

    def test_no_decisions_says_so_and_a_failed_read_says_why(self):
        empty = self.run_page("R.rows = ELS.decisions.html;", "(path, body) => [200, { decisions: [] }]")
        self.assertIn("No decision waits on you", empty["R"]["rows"])
        failed = self.run_page("R.rows = ELS.decisions.html;", "(path, body) => [500, { error: 'database is locked' }]")
        self.assertIn("Decisions could not be read: database is locked", failed["R"]["rows"])

    def test_a_click_posts_the_option_and_revision_and_the_row_says_what_was_ruled(self):
        doc = json.dumps({"decisions": [decision_row(), decision_row(note=8, options=["Yes", "No"])]})
        out = self.run_page("click(7, 1); await flush(); R.rows = ELS.decisions.html;",
                            f"""(path, body) => path === '/api/decisions' ? [200, {doc}]
                                : [200, {{ revision: '{"b" * 64}', ruling: {{ id: 99, kind: 'decision', body: 'Ruling on note #7: Free text <i>' }} }}]""")
        self.assertEqual(out["posts"], [["/api/decisions/answer", {"item": 3012, "note": 7, "revision": "a" * 64, "option": "Free text <i>"}, 64]])
        rows = out["R"]["rows"]
        self.assertIn("Ruled: Free text &lt;i&gt;", rows)
        self.assertIn("note #99", rows)
        # The other decision on the same item now carries the revision the answer returned, so its click is not stale.
        out = self.run_page("click(7, 1); await flush(); click(8, 0); await flush();",
                            f"""(path, body) => path === '/api/decisions' ? [200, {doc}]
                                : [200, {{ revision: '{"b" * 64}', ruling: {{ id: 99, kind: 'decision', body: 'x' }} }}]""")
        self.assertEqual(out["posts"][1], ["/api/decisions/answer", {"item": 3012, "note": 8, "revision": "b" * 64, "option": "Yes"}, 64])

    def test_a_stale_answer_reads_the_list_again_and_says_why(self):
        doc = json.dumps({"decisions": [decision_row()]})
        out = self.run_page("click(7, 0); await flush(); R.note = ELS['decisions-note'].textContent; R.hidden = ELS['decisions-note'].hidden;",
                            f"""(path, body) => path === '/api/decisions' ? [200, {doc}]
                                : [409, {{ error: 'item 3012 changed; reload it before applying this change', reload: true }}]""")
        self.assertEqual(out["gets"], ["/api/decisions", "/api/decisions"])
        self.assertIn("Not recorded: item 3012 changed", out["R"]["note"])
        self.assertFalse(out["R"]["hidden"])

    def test_a_refused_answer_stays_in_its_row(self):
        doc = json.dumps({"decisions": [decision_row()]})
        out = self.run_page("click(7, 0); await flush(); R.rows = ELS.decisions.html;",
                            f"""(path, body) => path === '/api/decisions' ? [200, {doc}]
                                : [400, {{ error: 'note 7 offers no option Option lines' }}]""")
        self.assertEqual(out["gets"], ["/api/decisions"])
        self.assertIn("Not recorded: note 7 offers no option Option lines", out["R"]["rows"])
        self.assertEqual(out["R"]["rows"].count('data-option="'), 2)


if __name__ == "__main__":
    unittest.main()
