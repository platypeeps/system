"""The v2 Writing page (sd:2125).

What this slice promises: `/writing` answers under the shared policy and loads the reader and its script before `shell.js`,
and the old list moves to `/classic/writing`, which the palette lists. `/api/writing` is every piece, parked ones too, with
the stage, gate problems, next and correction stages that `writing.piece_state` gives, each file's size, and the opening
of the draft; it names the repository by its folder only and writes nothing. The page draws the board with four gate
lamps per piece; Stage asks first and posts the item stage route with the piece's revision, Correct posts it with a
reason, and Park and Revive post their routes and undo each other. Publish and Capture are copy only.

`writing.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses, with the real
reader (`read.js`). The browser half -- the look at 1440 and 375 px, drag and the confirm dialog -- is a manual check.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from sd_db import upsert_repo, writing

from sd_dashboard import server, v2, writing_screen

from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "writing.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
NOW = "2026-09-23T12:00:00Z"


class Pieces(BrowserSession):
    """Two row-owned pieces in a writing checkout: one at review with its reports, one parked at drafting."""

    def seed(self):
        self.root = Path(self.tmp.name) / "writing"
        ids = {}
        for slug, stage in (("fixture", "review"), ("parked", "drafting")):
            folder = self.root / "content" / "2026" / slug
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "index.md").write_text(f"---\ntitle: {slug.capitalize()} piece\nstatus: {stage}\n---\n\n## Draft\n\n"
                                             "A carefully scoped fixture argument.\n\nA second paragraph here.\n")
            upsert_repo(self.connection, str(self.root), pieces_source="file")
            state = writing.import_piece(self.connection, str(self.root), f"2026/{slug}", who="operator")
            ids[slug] = state["item"]["id"]
            digest = state["writing"]["gates"]["digest"]
            (folder / "research.md").write_text(f"Research fixture\n<!-- reconciled-with-draft: {digest} gen=0 -->\n")
            (folder / "adversarial.md").write_text("Adversarial fixture report\n")
            if slug == "fixture":
                (folder / "fact-check.md").write_text("Fact-check fixture report\n")
        upsert_repo(self.connection, str(self.root), pieces_source="row")
        state = writing.piece_state(self.connection, ids["parked"])
        writing.park_piece(self.connection, ids["parked"], expected_revision=state["revision"], who="operator")
        return ids


class TheDocument(Pieces):
    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def test_each_piece_carries_its_stage_gates_moves_files_and_draft(self):
        doc = writing_screen.document(self.connection, now=NOW)
        by = {p["id"]: p for p in doc["pieces"]}
        piece = by[self.ids["fixture"]]
        state = writing.piece_state(self.connection, self.ids["fixture"])
        self.assertEqual((piece["stage"], piece["parked"], piece["piece"], piece["repo"], piece["folder"], piece["owner"]),
                         ("review", False, "2026/fixture", "writing", "content", "row"))
        self.assertEqual((piece["next"], piece["corrections"]), (["ready"], ["researching", "drafting"]))
        self.assertEqual(piece["revision"], state["revision"])
        self.assertEqual(piece["problems"], state["writing"]["gates"]["problems"])
        self.assertFalse(piece["gates_ok"])
        self.assertFalse(piece["ready_recorded"])
        self.assertEqual(piece["draft"], ["A carefully scoped fixture argument.", "A second paragraph here."])
        self.assertEqual(piece["words"], 9)
        self.assertEqual(piece["files"]["fact-check.md"]["bytes"], len("Fact-check fixture report\n"))
        self.assertIsNone(by[self.ids["parked"]]["files"]["fact-check.md"])
        # A parked piece is listed, and it has no moves until it is revived.
        self.assertEqual((by[self.ids["parked"]]["parked"], by[self.ids["parked"]]["next"]), (True, []))

    def test_no_field_holds_a_local_path(self):
        text = json.dumps(writing_screen.document(self.connection, now=NOW))
        self.assertNotIn(self.tmp.name, text)
        self.assertNotIn(str(self.root), text)

    def test_the_document_writes_nothing(self):
        before = self.snapshot()
        writing_screen.document(self.connection, now=NOW)
        self.assertEqual(self.snapshot(), before)


class ThePage(Pieces):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/writing")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Writing · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "writing.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_palette_keeps_the_old_one(self):
        self.assertEqual(v2.SECTIONS.get("Writing"), "/writing")
        self.assertNotIn("Writing", v2.CLASSIC)
        self.assertEqual(v2.SCREENS.get("Writing (classic)"), "/classic/writing")
        self.seed()
        status, _, body = self.request("/classic/writing?parked=all")
        self.assertEqual(status, 200)
        self.assertIn('action="/classic/writing"', body)
        self.assertIn("Fixture piece", body)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/writing")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/writing?view=editor", headers=cookie)[0], 400)
        status, _, body = self.request("/api/writing", headers=cookie)
        self.assertEqual((status, json.loads(body)["pieces"]), (200, []))

    def test_a_piece_the_page_reads_is_one_the_stage_and_park_routes_accept(self):
        ids = self.seed()
        cookie = {"Cookie": self.cookie}
        digest = writing.piece_state(self.connection, ids["fixture"])["writing"]["gates"]["digest"]
        for artifact in ("fact-check", "adversarial"):
            writing.record_gate(self.connection, ids["fixture"], artifact, verdict="pass", findings=[], reason="Explicit fixture verdict",
                                reviewed_digest=digest, who="operator")
        piece = next(p for p in json.loads(self.request("/api/writing", headers=cookie)[2])["pieces"] if p["id"] == ids["fixture"])
        self.assertEqual(self.post(f"/api/items/{piece['id']}/stage", {"revision": piece["revision"], "stage": "ready"})[0], 200)
        piece = next(p for p in json.loads(self.request("/api/writing", headers=cookie)[2])["pieces"] if p["id"] == ids["fixture"])
        self.assertEqual((piece["stage"], piece["ready_recorded"]), ("ready", True))
        self.assertEqual(self.post(f"/api/items/{piece['id']}/park", {"revision": piece["revision"]})[0], 200)


def piece(id, title, stage, **fields):
    entry = {"id": id, "title": title, "stage": stage, "parked": False, "piece": f"2026/{title.lower().replace(' ', '-')}", "repo": "writing-pack",
             "folder": "content", "updated": "2026-09-20T10:00:00+00:00", "revision": f"{id:064x}", "gates_ok": False, "problems": [],
             "owner": "row", "ready_recorded": False, "publication": None, "next": [], "corrections": [],
             "files": {"index.md": {"bytes": 4096, "mtime": "2026-09-20T10:00:00Z"}, "research.md": {"bytes": 2048, "mtime": "2026-09-19T10:00:00Z"},
                       "fact-check.md": {"bytes": 1024, "mtime": "2026-09-19T10:00:00Z"}, "adversarial.md": {"bytes": 1024, "mtime": "2026-09-19T10:00:00Z"}},
             "draft": ["First paragraph.", "Second paragraph."], "words": 1200}
    entry.update(fields)
    return entry


#: One piece per lamp story: a drafting piece with a missing fact-check, a ready-gated review piece, a parked one and a
#: file-owned one.
DOC = {"read": NOW, "stages": list(writing.STAGES), "pieces": [
    piece(11, "Three Budgets", "drafting", next=["review"], corrections=["researching"],
          problems=["fact-check.md is missing", "fact-check has no explicit current gate record; record its verdict and findings",
                    "adversarial has no explicit current gate record; record its verdict and findings"],
          files={"index.md": {"bytes": 4096, "mtime": "2026-09-20T10:00:00Z"}, "research.md": {"bytes": 2048, "mtime": "2026-09-19T10:00:00Z"},
                 "fact-check.md": None, "adversarial.md": {"bytes": 1024, "mtime": "2026-09-19T10:00:00Z"}}),
    piece(12, "Gated Review", "review", gates_ok=True, next=["ready"], corrections=["researching", "drafting"]),
    piece(13, "Parked Idea", "accepted", parked=True),
    piece(14, "File Owned", "review", owner="file", next=["ready"], gates_ok=True),
]}

#: The design's piece commands (design source products/system/designs/pages/writing.js): id, label, key, risk, bulk.
#: Stage is `confirm` here, not the design's `undo`: going back is a correction, which takes a reason.
COMMANDS = [
    ["piece.stage", "Stage", "s", "confirm", False],
    ["piece.correct", "Correct", "o", "safe", False],
    ["piece.park", "Park", "p", "undo", True],
    ["piece.revive", "Revive", "v", "undo", True],
    ["piece.readiness", "Readiness", "r", "safe", False],
    ["piece.publish", "Publish", "u", "confirm", False],
]

SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
window.shell.setContext = () => {}; C.pick = () => {};
var DIALOG = null, pageMake = document.createElement;
document.createElement = t => { var e = pageMake(t); if (t === 'dialog') { e.querySelector = s => document.getElementById(s.slice(1)); DIALOG = e; } return e; };
var navigator = {};
"""


class TheScript(unittest.TestCase):
    """writing.js against a document with one piece per lamp story."""

    def run_page(self, body, answer=None, search=""):
        answer = answer or "(path) => path === '/api/writing' ? [200, DOC] : [200, { revision: 'f'.repeat(64) }]"
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(DOC)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.page_attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.text = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.textContent])); OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.label, c.key, c.risk, !!c.bulk]); R.exec = cmd('piece.publish').executes;")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertIs(out["R"]["exec"], False)

    def test_stage_asks_first_posts_the_next_stage_with_the_revision_and_rereads(self):
        out = self.run_page("shellRun(cmd('piece.stage'), C.get('12')); await flush();")
        self.assertEqual(out["confirms"], ["piece.stage"])
        self.assertEqual(out["posts"], [["/api/items/12/stage", {"stage": "ready", "revision": f"{12:064x}"}, 64]])
        self.assertEqual(out["gets"], ["/api/writing", "/api/writing"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["#12 Review → Ready · readiness recorded", False])

    def test_off_moves_name_their_reason(self):
        out = self.run_page("const w = (id, key) => cmd(id).when(C.get(key));"
                            " R.off = [w('piece.stage', '13'), w('piece.stage', '14'), w('piece.park', '14'), w('piece.revive', '12'), w('piece.correct', '13'), w('piece.stage', '11')];")
        self.assertEqual(out["R"]["off"], ["Parked. Revive it first; a parked piece keeps its stage.",
                                           "The piece files own this piece; the dashboard moves it after the writing cutover.",
                                           "the piece files own this piece; the dashboard moves it after the writing cutover",
                                           "not parked", "parked; revive first", True])

    def test_park_posts_its_route_and_undo_revives_with_the_revision_park_returned(self):
        out = self.run_page("shellRun(cmd('piece.park'), C.get('12')); await flush(); lastUndo().undo(); await flush();")
        self.assertEqual(out["posts"], [["/api/items/12/park", {"revision": f"{12:064x}"}, 64], ["/api/items/12/revive", {"revision": "f" * 64}, 64]])
        self.assertEqual(out["toasts"][0], ["#12 parked", True])
        self.assertEqual(out["toasts"][-1], ["Park undone · #12 Gated Review", False])

    def test_correct_asks_for_a_reason_and_posts_it_as_a_correction(self):
        out = self.run_page("shellRun(cmd('piece.correct'), C.get('12')); ELS['to-in'].value = 'drafting'; ELS['why-in'].value = 'The argument needs a rewrite';"
                            " DIALOG.returnValue = 'ok'; DIALOG.onclose(); await flush();")
        self.assertEqual(out["posts"], [["/api/items/12/stage", {"stage": "drafting", "correct": True, "reason": "The argument needs a rewrite", "revision": f"{12:064x}"}, 64]])
        self.assertEqual(out["toasts"][-1], ["#12 corrected to Drafting", False])

    def test_a_correction_without_a_reason_posts_nothing(self):
        out = self.run_page("shellRun(cmd('piece.correct'), C.get('12')); ELS['why-in'].value = '  '; DIALOG.returnValue = 'ok'; DIALOG.onclose(); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1], ["#12 not moved.", False])

    def test_a_refused_write_says_why_and_reads_again(self):
        answer = "(path) => path === '/api/writing' ? [200, DOC] : [409, { error: 'item 12 changed; reload it before applying this change' }]"
        out = self.run_page("shellRun(cmd('piece.stage'), C.get('12')); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1], ["#12 Gated Review not changed: item 12 changed; reload it before applying this change", False])
        self.assertEqual(out["gets"], ["/api/writing", "/api/writing"])
        self.assertEqual(out["states"][-2]["kind"], "loading", "a refused write reads again with load()")

    def test_publish_and_capture_are_copy_only(self):
        out = self.run_page("shellRun(cmd('piece.publish'), Object.assign({}, C.get('12'))); await flush();"
                            " ELS['shift-in'].value = 'A new idea'; ELS['shift-in'].listeners.keydown[0]({ key: 'Enter', preventDefault() {} }); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertIn("Copy the line to capture the idea; the dashboard does not write to the vault: sd store add sdw.blog-idea 'A new idea' --field status=inbox",
                      [t[0] for t in out["toasts"]])

    def test_the_board_lamps_strip_and_badge_come_from_the_document(self):
        out = self.run_page("")
        board = out["html"]["view-board"]
        # Columns in stage order: Drafting holds #11, Review holds #12 and #14.
        self.assertEqual(re.findall(r'<article class="card[^"]*"[^>]*data-id="(\d+)"', board), ["11", "12", "14"])
        self.assertNotIn('data-id="13"', board, "a parked piece shows only with Parked on")
        self.assertIn("Not read: the dashboard has no reader for the vault's blog ideas yet.", board)
        card = re.search(r'<article class="card"[^>]*data-id="11".*?</article>', board, re.S).group(0)
        self.assertEqual(re.findall(r'data-s="(\w+)"', card), ["ok", "ok", "queued", "unknown"])
        self.assertIn("<b>3</b> active", out["html"]["strip"])
        self.assertIn("vault ideas not read", out["html"]["strip"])
        self.assertEqual(out["page_attention"], {"state": "ok", "n": 0, "what": "pieces need a gate"})
        self.assertEqual(out["text"]["parked-n"], 1)

    def test_the_editor_shows_the_draft_read_only_and_each_files_facts(self):
        out = self.run_page("open('11');", search="?view=editor")
        editor = out["html"]["view-editor"]
        self.assertIn("<p>First paragraph.</p><p>Second paragraph.</p>", editor)
        self.assertIn('aria-readonly="true"', editor)
        self.assertNotIn("contenteditable", editor)
        self.assertIn("writing-pack/content/2026/three-budgets/index.md", editor)
        self.assertIn("Read only", editor)

    def test_a_failed_read_clears_the_page_and_says_so(self):
        out = self.run_page("", answer="() => [500, { error: 'database is locked' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("The pieces were not read: database is locked", out["states"][-1]["text"])
        self.assertEqual(out["page_attention"], {"state": "unknown", "n": 0, "what": "pieces not read"})
        self.assertIn("The pieces were not read. Reload retries it.", out["html"]["details"])

    def test_the_script_adds_no_sink_no_sample_data_and_no_reader_of_its_own(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotIn("WRITING_DATA", PAGE_JS)
        self.assertNotIn("mockup", PAGE_JS)
        self.assertNotIn("contenteditable", PAGE_JS.replace('[contenteditable="true"]', ""))
        self.assertIn("window.shell.read({", PAGE_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "writing", "Writing", "/writing", ("/api/writing",)


if __name__ == "__main__":
    unittest.main()
