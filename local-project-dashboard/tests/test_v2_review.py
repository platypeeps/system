"""The v2 Review page (sd:3709): the labelling queue on its own rail page.

What this slice promises: `/review` is a registered page in the rail's Now group (`g l`); `/api/review` lists each open
batch, nightly suggestions first, and its open questions, and a blind or sealed batch sends no subject or evidence; a
batch whose notes do not read is listed with its reason. A label and its undo go through `sd_db.review.rule` and
`withdraw`, the code `sd-db review` runs, under the batch revision the page read (409 when stale). Today's Decisions panel
lists no batch question and counts them for one line that links to Review. `review.js` runs under JavaScriptCore
(osascript) against the stand-in page `test_v2_tasks` uses: the digits label and advance, u undoes, a failed read is
unknown and never an empty queue. The browser half (the widths, the themes) is recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from sd_db import review, workflow
from sd_dashboard import decisions, review_screen, v2

from support import ScreenCase
from test_v2_registry import GROUP_BLOCK, Registers
from test_v2_tasks import MARKUP_JS, SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
STATIC = V2 / "static"
PAGE_JS = (STATIC / "review.js").read_text(encoding="utf-8") if (STATIC / "review.js").exists() else ""
PAGE_CSS = (STATIC / "review.css").read_text(encoding="utf-8") if (STATIC / "review.css").exists() else ""


def batch(name, *, queue="labels", mode="open", count=3, options=("yes", "no"), **extra):
    found = {"schema": 1, "queue": queue, "batch": name, "title": f"Label: {name}", "source": "system@1a2b3c4",
             "mode": mode, "questions": [{"subject": f"example.test/repo@1a2b3c4:file{n}.py",
                                          "text": f"Is file {n} right?\nRead it whole.",
                                          "evidence": [f"diff: git show 1a2b3c4 -- file{n}.py"],
                                          "options": list(options)} for n in range(1, count + 1)], **extra}
    if mode == "sealed":
        found["seal"] = {"sha256": "a" * 64, "name": "key.csv"}
    return found


def seed(case):
    """An open batch, a blind one, a nightly one filed last, a closed one and an item that only looks like a batch."""
    ids = {"open": review.file(case.connection, batch("C1"), who="test")["item"],
           "blind": review.file(case.connection, batch("B1", mode="blind", count=2), who="test")["item"],
           "nightly": review.file(case.connection, batch("2026-10-12", queue="nightly", count=2,
                                                         options=("Approve", "Reject")), who="test")["item"]}
    closed = review.file(case.connection, batch("D1", count=1), who="test")["item"]
    state = workflow.item_state(case.connection, closed)
    note = next(n["id"] for n in state["notes"] if n["kind"] == "question")
    state = review.rule(case.connection, closed, note, "yes", expected_revision=state["revision"], who="test")
    workflow.change_status(case.connection, closed, "done", who="test", reason="closed")
    ids["closed"] = closed
    broken = case.item("Not a batch")
    case.connection.execute("UPDATE item SET source = 'task-ref', external_id = 'review:labels:X9' WHERE id = ?", (broken,))
    case.connection.commit()
    case.note(broken, "Just a comment", kind="comment")
    ids["broken"] = broken
    return ids


def questions_of(connection, item):
    return [n["id"] for n in workflow.item_state(connection, item)["notes"] if n["kind"] == "question"]


class TheRegistration(Registers, unittest.TestCase):
    page, section, route = "review", "Review", "/review"
    api = ("/api/review", "/api/review/7/export", "/api/review/rule", "/api/review/withdraw")

    def test_review_follows_briefs_in_now_with_its_chord_and_icon(self):
        now = re.search(r"\['Now',\s*\[(.*?)\]\],", GROUP_BLOCK).group(1)
        self.assertIn("['Briefs', 'inbox', 'i'], ['Review', 'list-checks', 'l']", now)
        self.assertIn('<symbol id="i-list-checks"', (STATIC / "icons.js").read_text(encoding="utf-8"))


class TheDocument(ScreenCase):
    def test_open_batches_are_listed_nightly_first_with_their_open_questions(self):
        ids = seed(self)
        doc = review_screen.document(self.connection)
        self.assertEqual([b["item"] for b in doc["batches"]], [ids["nightly"], ids["open"], ids["blind"], ids["broken"]])
        opened = doc["batches"][1]
        self.assertEqual((opened["queue"], opened["name"], opened["mode"], opened["questions"], opened["open"], opened["labels"]),
                         ("labels", "C1", "open", 3, 3, 0))
        self.assertEqual(opened["revision"], workflow.item_state(self.connection, ids["open"])["revision"])
        self.assertTrue(doc["batches"][0]["nightly"])
        self.assertEqual([q["item"] for q in doc["questions"]],
                         [ids["nightly"]] * 2 + [ids["open"]] * 3 + [ids["blind"]] * 2)
        first = doc["questions"][2]
        self.assertEqual((first["text"], first["options"], first["subject"], first["evidence"]),
                         ("Is file 1 right?\nRead it whole.", ["yes", "no"], "example.test/repo@1a2b3c4:file1.py",
                          ["diff: git show 1a2b3c4 -- file1.py"]))

    def test_the_rail_badge_counts_every_open_question_in_a_batch_that_reads(self):
        # sd:3446: the rule every page's rail shows; the broken batch counts nothing, a blind one counts its questions.
        ids = seed(self)
        self.assertEqual(review_screen.document(self.connection)["attention"], {"state": "caution", "n": 7, "what": "questions open"})
        note = questions_of(self.connection, ids["open"])[0]
        revision = workflow.item_state(self.connection, ids["open"])["revision"]
        review_screen.rule_request({"item": ids["open"], "note": note, "option": "no", "revision": revision})(self.connection)
        self.assertEqual(review_screen.document(self.connection)["attention"]["n"], 6)

    def test_a_blind_batch_sends_no_subject_and_no_evidence(self):
        ids = seed(self)
        doc = review_screen.document(self.connection)
        blind = [q for q in doc["questions"] if q["item"] == ids["blind"]]
        self.assertEqual(len(blind), 2)
        for q in blind:
            self.assertEqual(set(q), {"item", "note", "text", "options", "asked"})
        self.assertNotIn("example.test", json.dumps(blind))

    def test_a_batch_that_does_not_read_is_listed_with_its_reason(self):
        ids = seed(self)
        broken = [b for b in review_screen.document(self.connection)["batches"] if b["item"] == ids["broken"]]
        self.assertEqual(len(broken), 1)
        self.assertIn("is not a review batch", broken[0]["error"])

    def test_a_labelled_question_leaves_the_list_and_its_withdrawal_brings_it_back(self):
        ids = seed(self)
        note = questions_of(self.connection, ids["open"])[0]
        revision = workflow.item_state(self.connection, ids["open"])["revision"]
        out = review_screen.rule_request({"item": ids["open"], "note": note, "option": "no", "revision": revision})(self.connection)
        doc = review_screen.document(self.connection)
        self.assertNotIn(note, [q["note"] for q in doc["questions"]])
        self.assertEqual(next(b for b in doc["batches"] if b["item"] == ids["open"])["labels"], 1)
        self.assertEqual(review.read(self.connection, ids["open"])["questions"][0]["label"], "no")
        review_screen.withdraw_request({"item": ids["open"], "ruling": out["ruling"], "revision": out["revision"]})(self.connection)
        self.assertIn(note, [q["note"] for q in review_screen.document(self.connection)["questions"]])
        bodies = [n["body"] for n in workflow.item_state(self.connection, ids["open"])["notes"] if n["kind"] == "decision"]
        self.assertEqual(bodies, [f"Ruling on note #{note}: no", f"Withdrawn: ruling #{out['ruling']} on note #{note}"])

    def test_nothing_open_is_an_empty_document(self):
        self.assertEqual(review_screen.document(self.connection), {"batches": [], "questions": [],
                                                                   "attention": {"state": "ok", "n": 0, "what": "questions open"}})


class TheExport(ScreenCase):
    def test_an_open_batch_exports_the_lines_sd_db_review_export_writes(self):
        ids = seed(self)
        out = review_screen.export(self.connection, ids["open"])
        self.assertEqual(out["name"], "review-labels-C1.jsonl")
        self.assertEqual(out["lines"][0]["kind"], "manifest")
        self.assertEqual(len(out["lines"]), 4)
        expected = review.export(self.connection, ids["open"], who="test")
        for line in (out["lines"][0], expected[0]):
            line.pop("exported_at")
        self.assertEqual(out["lines"], expected)

    def test_a_blind_batch_with_an_open_question_and_an_item_that_is_no_batch_are_refused(self):
        ids = seed(self)
        status, body = review_screen.export(self.connection, ids["blind"])
        self.assertEqual(status, 409)
        self.assertIn("has 2 open", body["error"])
        self.assertEqual(review_screen.export(self.connection, ids["broken"])[0], 400)
        self.assertEqual(review_screen.export(self.connection, 999999)[0], 404)


class TheRoutes(BrowserSession):
    def test_a_label_lands_once_a_second_on_the_old_revision_is_stale_and_undo_reopens(self):
        ids = seed(self)
        status, _, body = self.request("/api/review", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        doc = json.loads(body)
        q = next(q for q in doc["questions"] if q["item"] == ids["open"])
        revision = next(b["revision"] for b in doc["batches"] if b["item"] == ids["open"])
        payload = {"item": ids["open"], "note": q["note"], "option": "yes", "revision": revision}
        status, _, out = self.post("/api/review/rule", payload)
        self.assertEqual(status, 200, out)
        self.assertEqual(set(out), {"item", "note", "revision", "ruling"})
        status, _, refused = self.post("/api/review/rule", payload)
        self.assertEqual((status, refused.get("reload")), (409, True))
        status, _, pulled = self.post("/api/review/withdraw", {"item": ids["open"], "ruling": out["ruling"], "revision": out["revision"]})
        self.assertEqual(status, 200, pulled)
        self.assertTrue(review.read(self.connection, ids["open"])["questions"][0]["open"])
        status, _, exported = self.request("/api/review/%d/export" % ids["open"], headers={"Cookie": self.cookie})[0], None, None
        self.assertEqual(status, 200)

    def test_a_closed_batch_refuses_a_label(self):
        ids = seed(self)
        note = questions_of(self.connection, ids["closed"])[0]
        revision = workflow.item_state(self.connection, ids["closed"])["revision"]
        before = self.snapshot()
        status, _, out = self.post("/api/review/rule", {"item": ids["closed"], "note": note, "option": "no", "revision": revision})
        self.assertEqual(status, 400, out)
        self.assertIn("closed", out["error"])
        self.assertEqual(self.snapshot(), before)

    def test_a_body_out_of_shape_is_refused_before_the_database_opens(self):
        good = {"item": 3, "note": 4, "option": "yes", "revision": "a" * 64}
        for payload in ({k: v for k, v in good.items() if k != "revision"}, {**good, "revision": "x"}, {**good, "option": ""},
                        {**good, "note": "4"}, {**good, "item": 0}, {**good, "note": 2 ** 63}, {**good, "who": "me"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/review/rule", payload)[0], 400)
        for payload in ({"item": 3, "ruling": 4}, {"item": 3, "ruling": True, "revision": "a" * 64}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/review/withdraw", payload)[0], 400)


class TodaysDecisions(ScreenCase):
    def test_batch_questions_are_not_decision_rows_and_are_counted_for_review(self):
        ids = seed(self)
        plain = self.item("Plain work")
        asked = self.note(plain, "Which?\nOption: A\nOption: B", kind="question")
        doc = decisions.document(self.connection)
        self.assertEqual([d["note"] for d in doc["decisions"]], [asked])
        self.assertEqual(doc["review"], 7)
        self.assertEqual(ids["open"] in [d["item"] for d in doc["decisions"]], False)

    def test_the_batch_ref_today_names_is_the_one_review_files_under(self):
        self.assertEqual(decisions.BATCH_SOURCE, review.REF_SOURCE)
        self.assertTrue(f"review:labels:C1".startswith(decisions.BATCH_PREFIX))


class TheStylesheet(unittest.TestCase):
    def test_review_css_names_tokens_and_no_colour_literal(self):
        self.assertTrue(PAGE_CSS)
        self.assertNotRegex(PAGE_CSS, r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|oklch|oklab|lab|lch|color-mix)\(")
        tokens = set(re.findall(r"^\s*(--[a-z0-9-]+):", (STATIC / "tokens.css").read_text(encoding="utf-8"), re.M))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", PAGE_CSS))
        self.assertEqual(used - tokens, set())


def question(note, item=11, **changes):
    return {"item": item, "note": note, "text": f"Question {note}?\nmore", "options": ["Approve", "Reject", "Later <b>"],
            "asked": "2026-10-12T15:00:00Z", **changes}


def doc_with(n=3, **batch_changes):
    return {"batches": [{"item": 11, "title": "Nightly review", "created": "2026-10-12T15:00:00Z", "ref": "review:nightly:x",
                         "queue": "nightly", "name": "x", "mode": "open", "source": "s", "nightly": True,
                         "revision": "a" * 64, "questions": n, "open": n, "labels": 0, "error": None, **batch_changes}],
            "questions": [question(10 + i) for i in range(n)]}


class TheScript(unittest.TestCase):
    """review.js against a stand-in page: the lamps, the rows, the digits, Undo and the failed read."""

    def run_page(self, body, answer, search="", doc=None):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + "\nlocation.search = " + json.dumps(search) + ";\nvar DOC = " + json.dumps(doc) + ";\nANSWER = " + answer + ";\n"
                  + "window.shell.commands.select = id => { OUT.selected = id; };\n"
                  + "var BareEl = El; El = id => { const e = BareEl(id); e.click = () => { (OUT.saved = OUT.saved || []).push(e.download); }; return e; };\n"
                  + PAGE_JS
                  + "\nvar R = {};\nconst key = k => (DOC_LISTENERS.keydown || []).forEach(f => f({ key: k, target: { matches: () => false }, preventDefault() {} }));\n"
                  + "(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    RULE = """(path, body) => path === '/api/review' ? [200, DOC]
      : path === '/api/review/rule' ? [200, { item: body.item, note: body.note, revision: String(body.note).padStart(64, '0'), ruling: 100 + body.note }]
      : [200, { item: 11, revision: 'c'.repeat(64), withdrawal: 500 }]"""

    def test_the_lamps_rows_and_details_draw_and_values_stay_text(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.rows = ELS.rows.html; R.details = ELS.details.html; R.sub = ELS.sub.textContent;",
                            "(path, body) => [200, DOC]", doc=doc_with())
        self.assertEqual(out["gets"], ["/api/review"])
        self.assertIn("Nightly review", out["R"]["lamps"])
        self.assertIn("<b>3</b> of 3 open", out["R"]["lamps"])
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 3)
        self.assertIn('data-i="2"><kbd>3</kbd><span>Later &lt;b&gt;</span>', out["R"]["details"])
        self.assertRegex(out["R"]["sub"], r"^0 labelled · 3 open · 0 skipped · read ")

    def test_details_marks_the_proposed_option_in_an_open_batch_only(self):
        # A Kev file's proposal is an evidence line (sd:4789); its digit is the accept.
        for mode, marked in (("open", True), ("blind", False)):
            doc = doc_with(1, mode=mode)
            doc["questions"][0]["evidence"] = ["Proposed label: Reject"]
            out = self.run_page("R.details = ELS.details.html;", "(path, body) => [200, DOC]", doc=doc)
            with self.subTest(mode):
                self.assertEqual('<span>Reject</span><span class="tag">proposed</span>' in out["R"]["details"], marked)
                self.assertEqual(out["R"]["details"].count('class="tag">proposed'), int(marked))

    def test_ten_digits_write_ten_labels_in_turn_and_u_withdraws_the_last(self):
        doc = doc_with(10)
        out = self.run_page("for (const k of '1212121212') { key(k); } await flush(); R.rows = ELS.rows.html;"
                            " R.sub = ELS.sub.textContent; OUT.undoes = OUT.toasts.filter(t => t.undo).length;"
                            " OUT.toasts[OUT.toasts.length - 1].undo(); await flush();",
                            self.RULE, doc=doc)
        rules = [p for p in out["posts"] if p[0] == "/api/review/rule"]
        self.assertEqual([p[1]["note"] for p in rules], list(range(10, 20)))
        self.assertEqual([p[1]["option"] for p in rules], ["Approve", "Reject"] * 5)
        # Each write carries the revision the one before it returned: one at a time, never a stale pair.
        self.assertEqual(rules[0][1]["revision"], "a" * 64)
        self.assertEqual([p[1]["revision"] for p in rules[1:]], [str(n).zfill(64) for n in range(10, 19)])
        self.assertIn("No question is open", out["R"]["rows"])
        self.assertRegex(out["R"]["sub"], r"^10 labelled · 0 open")
        self.assertEqual(out["undoes"], 10)
        withdraw = [p for p in out["posts"] if p[0] == "/api/review/withdraw"]
        self.assertEqual(withdraw, [["/api/review/withdraw", {"item": 11, "ruling": 119, "revision": "19".zfill(64)}, 64]])
        self.assertEqual(out["gets"], ["/api/review", "/api/review"])

    def test_undo_selects_the_question_again_and_counts_no_skip(self):
        # The label moved the selection on by itself; undo moving it back is not a skip (browser run, 375 px).
        doc = doc_with(3)
        out = self.run_page("key('1'); await flush(); OUT.toasts[OUT.toasts.length - 1].undo(); await flush();"
                            " R.sub = ELS.sub.textContent;",
                            """(path, body) => path === '/api/review/rule' ? [200, { item: 11, note: body.note, revision: 'b'.repeat(64), ruling: 110 }]
                              : path === '/api/review/withdraw' ? [200, { item: 11, revision: 'c'.repeat(64), withdrawal: 111 }] : [200, JSON.parse(JSON.stringify(DOC))]""",
                            doc=doc)
        self.assertEqual(out["selected"], "q10")
        self.assertRegex(out["R"]["sub"], r"^0 labelled · 3 open · 0 skipped · ")

    # Batch-level actions sit on the batch header, which a lamp opens; none waits on a selected question (lane review 235).
    DOWNLOAD = ("globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} }; globalThis.Blob = function (parts) { this.parts = parts; };"
                " ELS.tally.listeners.click[0]({ target: { closest: s => s === '#export' ? { disabled: false } : null } }); await flush();")
    EXPORT = """(path, body) => path === '/api/review' ? [200, DOC]
      : [200, { name: 'review-nightly-x.jsonl', lines: [{ kind: 'manifest' }, { note: 10 }, { note: 11 }, { note: 12 }] }]"""

    def test_a_finished_blind_batch_downloads_from_its_header_with_no_question_selected(self):
        doc = doc_with(3, mode="blind", open=0, labels=3)
        doc["questions"] = []
        out = self.run_page("R.tally = ELS.tally.html; R.details = ELS.details.html; " + self.DOWNLOAD, self.EXPORT,
                            search="?batch=11", doc=doc)
        self.assertIsNone(out["selected"])
        self.assertIn('id="export">Download labels</button>', out["R"]["tally"])
        self.assertNotIn("disabled", out["R"]["tally"])
        self.assertEqual(out["gets"], ["/api/review", "/api/review/11/export"])
        self.assertIn("Downloaded review-nightly-x.jsonl · 3 questions", [t[0] for t in out["toasts"]])
        self.assertEqual(out["saved"], ["review-nightly-x.jsonl"])

    def test_a_blind_batch_with_open_questions_keeps_download_off_and_says_why(self):
        out = self.run_page("R.tally = ELS.tally.html; R.details = ELS.details.html;", self.EXPORT,
                            search="?batch=11", doc=doc_with(2, mode="blind"))
        self.assertIn('id="export" disabled>Download labels</button>', out["R"]["tally"])
        self.assertIn("A blind batch downloads once every question is ruled.", out["R"]["tally"])
        self.assertNotIn('id="export"', out["R"]["details"])

    def test_the_batch_header_links_the_batch_task_with_no_question_selected(self):
        # A comment on the whole batch is a note on its task; the header links it, so it waits on no question.
        doc = doc_with(3, open=0, labels=3)
        doc["questions"] = []
        out = self.run_page("R.tally = ELS.tally.html;", self.EXPORT, search="?batch=11", doc=doc)
        self.assertIn('<a href="/tasks?row=11">sd:11</a>', out["R"]["tally"])

    def test_a_digit_past_the_options_a_held_key_and_a_chord_label_nothing(self):
        out = self.run_page("key('7'); (DOC_LISTENERS.keydown || []).forEach(f => f({ key: '1', repeat: true, target: { matches: () => false }, preventDefault() {} }));"
                            " window.shell.chording = () => true; key('1'); await flush();",
                            self.RULE, doc=doc_with())
        self.assertEqual(out["posts"], [])

    def test_a_stale_label_rereads_and_puts_the_question_back(self):
        out = self.run_page("key('1'); await flush(); R.rows = ELS.rows.html;",
                            "((path, body) => path === '/api/review' ? [200, DOC]"
                            " : [409, { error: 'item 11 changed; reload it before applying this change', reload: true }])", doc=doc_with())
        self.assertEqual(out["gets"], ["/api/review", "/api/review"])
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 3)
        self.assertIn("The batch changed; reread", out["toasts"][-1][0])

    def test_a_refused_label_stays_open_and_says_why(self):
        out = self.run_page("key('2'); await flush(); R.rows = ELS.rows.html;",
                            "((path, body) => path === '/api/review' ? [200, DOC]"
                            " : [400, { error: 'sd:11 is closed' }])", doc=doc_with())
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 3)
        self.assertEqual(out["toasts"][-1], ["Not labelled · note #10: sd:11 is closed", False])

    def test_a_failed_read_is_unknown_and_never_an_empty_queue(self):
        out = self.run_page("R.rows = ELS.rows.html; R.sub = ELS.sub.textContent;",
                            "(path, body) => [500, { error: 'database is locked' }]")
        self.assertIn("▨", out["R"]["rows"])
        self.assertIn("Not read: database is locked", out["R"]["rows"])
        self.assertNotIn("No question is open", out["R"]["rows"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertEqual(out["R"]["sub"], "The review queue was not read")

    def test_a_batch_filter_from_the_address_and_esc_clears_it(self):
        doc = doc_with()
        doc["batches"].append({**doc["batches"][0], "item": 12, "title": "Label: C1", "nightly": False, "queue": "labels"})
        doc["questions"].append(question(30, item=12))
        out = self.run_page("R.rows = ELS.rows.html; R.cleared = window.PAGE_LIST.clear(); R.after = ELS.rows.html; R.again = window.PAGE_LIST.clear();",
                            "(path, body) => [200, DOC]", search="?batch=12", doc=doc)
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 1)
        self.assertTrue(out["R"]["cleared"])
        self.assertEqual(out["R"]["after"].count('<tr data-id="q'), 4)
        self.assertFalse(out["R"]["again"])

    def test_n_files_a_comment_on_the_batch_naming_the_question(self):
        out = self.run_page("R.toast = await window.SHELL_CAPTURE({ kind: 'note', title: 'looks off', item: 11, about: { id: 'q10' } });",
                            "((path, body) => path === '/api/review' ? [200, DOC]"
                            " : [200, { revision: 'd'.repeat(64), note: { id: 77 } }])", doc=doc_with())
        self.assertEqual(out["posts"], [["/api/items/11/notes", {"body": "On note #10: looks off", "kind": "comment", "revision": "a" * 64}, 64]])
        self.assertEqual(out["R"]["toast"], "Comment on note #10 added to sd:11")

    def test_the_page_binds_digits_alone_and_names_its_keys(self):
        self.assertIn("/^[1-9]$/", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"e\.key === '[jknu]'")
        out = self.run_page("R.keys = window.PAGE_KEYS.map(k => k[0]);", "(path, body) => [200, { batches: [], questions: [] }]")
        self.assertEqual(out["R"]["keys"], ["1 – 9", "j / k", "u", "n", "Esc"])
        self.assertEqual(out["states"][-1]["kind"], "empty")


if __name__ == "__main__":
    unittest.main()
