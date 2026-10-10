"""The v2 Designs page (sd:2126).

What this slice promises: `/designs` answers under the shared policy and loads the reader and its script before `shell.js`,
the old listing moves to `/classic/designs`, which the palette lists, and the files stay at `/designs/<path>`.
`/api/designs` is `designs.ledger()`, the document the design's `data/designs-data.js` holds, read live. The page draws a
row per drawn page and per brief with no page, marks a page uncommitted, without a screenshot or with a stale one, opens
each page and screenshot at `/designs/<path>`, and registers the design's three commands, each copy only.

`designs.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses, with the real
reader (`read.js`). The browser half -- the look at 1440 and 375 px -- is a manual check.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import designs, server, v2

from test_designs import tree
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_v2_shell_shared import cell_grammar
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "designs.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

PAGES = "products/system/designs/pages"


def page(path, **fields):
    entry = {"path": path, "product": "system", "kind": "v2 mockup", "bytes": 2048, "title": "", "changed": "2026-09-20T10:00:00Z",
             "sha": "abc1234", "subject": "Draw it", "dirty": False, "shots": [], "shotStale": [], "shotSize": {}, "shotTimes": {},
             "shotChanged": "", "shotSha": "", "shotOldest": ""}
    entry.update(fields)
    return entry


#: A ledger with one page in each state and a product with a brief and no page.
DOC = {
    "read": "2026-09-23T12:00:00Z", "root": "~/repos/example/ui-design", "head": "abc1234", "branch": "main", "caution": 3,
    "products": [{"name": "brand", "title": "brand brief", "brief": "products/brand/README.md", "status": "extracted; no `redesign`.", "pages": 0},
                 {"name": "system", "title": "system brief", "brief": "products/system/README.md", "status": "v2 drawn.", "pages": 4}],
    "pages": [
        page(f"{PAGES}/today.html", title="Today · system", shots=["products/system/designs/shots/v2-today-1440.png", "products/system/designs/shots/v2-today-375.png"],
             shotSize={"products/system/designs/shots/v2-today-1440.png": [1440, 900]}, shotTimes={"products/system/designs/shots/v2-today-1440.png": "2026-09-20T10:00:00Z"},
             shotChanged="2026-09-20T10:00:00Z", shotSha="abc1234", shotOldest="products/system/designs/shots/v2-today-1440.png"),
        page(f"{PAGES}/tasks.html", shots=["products/system/designs/shots/v2-tasks-1440.png"], shotStale=["products/system/designs/shots/v2-tasks-1440.png"],
             changed="2026-09-01T10:00:00Z"),
        page(f"{PAGES}/notes.html", changed="2026-09-01T10:00:00Z"),
        page("products/system/designs/v1-today.html", kind="v1 mockup", dirty=True, shots=["products/system/designs/shots/v1-375.png"], changed="2026-09-22T10:00:00Z"),
    ],
}

#: The design's commands (design source products/system/designs/pages/designs.js): id, object type, label, key, risk.
COMMANDS = [
    ["design.log", "page", "History", "l", "safe"],
    ["design.shots", "page", "Retake screenshots", "s", "safe"],
    ["design.brief", "brief", "Read brief", "b", "safe"],
]


class ThePage(BrowserSession):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "ui-design"
        tree(self.root)
        patcher = mock.patch.object(designs, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/designs")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Designs · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "designs.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_the_palette_keeps_the_old_one_and_the_files_stay_put(self):
        self.assertEqual(v2.SECTIONS.get("Designs"), "/designs")
        self.assertNotIn("Designs", v2.CLASSIC)
        self.assertEqual(v2.SCREENS.get("Designs (classic)"), "/classic/designs")
        status, _, body = self.request("/classic/designs")
        self.assertEqual(status, 200)
        self.assertIn('href="/designs/products/system/designs/v1-today.html"', body)
        self.assertEqual(self.request("/designs/products/system/designs/v1-today.html")[0], 200)

    def test_the_data_route_needs_a_session_takes_no_query_and_is_the_ledger(self):
        self.assertEqual(self.request("/api/designs")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/designs?f=stale", headers=cookie)[0], 400)
        status, _, body = self.request("/api/designs", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual([p["path"] for p in doc["pages"]], ["products/system/designs/v1-today.html"])
        self.assertEqual({p["name"]: p["pages"] for p in doc["products"]}, {"empty": 0, "system": 1})
        self.assertEqual(set(doc), set(designs.ledger(self.root)))


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.selected = []; C.select = id => OUT.selected.push(id);
const click = sel => DOC_LISTENERS.click.forEach(f => f({ target: { closest: s => s === sel.s ? { id: sel.id, dataset: sel.dataset || {} } : null } }));
"""


class TheScript(unittest.TestCase):
    """designs.js against a ledger with a page in each state."""

    def run_page(self, body, answer=None, search=""):
        answer = answer or "(path) => [200, DOC]"
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(DOC)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.page_attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.text = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.textContent])); OUT.toasts = OUT.toasts.map(t => t.msg); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_and_each_is_copy_only(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.exec = REG.map(c => c.executes);"
                            " R.cli = cmd('design.log').cli(C.get('" + PAGES + "/today.html'));")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertEqual(out["R"]["exec"], [False, False, False])
        self.assertEqual(out["R"]["cli"], f"git -C ~/repos/example/ui-design log -5 --format='%h %cs %s' -- {PAGES}/today.html")

    def test_the_copy_only_commands_post_nothing_and_say_so(self):
        out = self.run_page(f"shellRun(cmd('design.shots'), C.get('{PAGES}/tasks.html')); shellRun(cmd('design.brief'), C.get('product:brand')); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], ["Copy the line to retake designs/pages/tasks.html at 1440 and 375; the dashboard does not run it", "Copy the line to read the brand brief"])

    def test_retake_is_off_for_a_page_shots_mjs_does_not_draw(self):
        out = self.run_page("R.off = cmd('design.shots').when(C.get('products/system/designs/v1-today.html'));")
        self.assertEqual(out["R"]["off"], "shots.mjs draws the v2 pages only")

    def test_rows_lamps_and_the_badge_come_from_the_ledger(self):
        out = self.run_page("")
        rows, lamps = out["html"]["rows"], out["html"]["annunciator"]
        # Products with pages first; within one, v2 pages before v1, then by path.
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows),
                         [f"{PAGES}/notes.html", f"{PAGES}/tasks.html", f"{PAGES}/today.html", "products/system/designs/v1-today.html", "product:brand"])
        self.assertIn("v2 mockup · no screenshot", rows)
        self.assertIn("v2 mockup · stale screenshot: v2-tasks-1440.png", rows)
        self.assertIn("v1 mockup · uncommitted change in the working tree", rows)
        self.assertIn("brief only · a brief and no page yet", rows)
        self.assertEqual(re.findall(r'data-filter="(\w+)" data-state="(\w+)"', lamps),
                         [("recent", "ok"), ("stale", "caution"), ("noshot", "caution"), ("dirty", "caution"), ("brief", "ok")])
        self.assertEqual(cell_grammar(self, lamps), [("button", s) for s in ("ok", "caution", "caution", "caution", "ok", None)])
        self.assertEqual(re.findall(r'data-filter="(\w+)"[^>]*>.*?<b>(\d+)</b>', lamps, re.S),
                         [("recent", "2"), ("stale", "1"), ("noshot", "1"), ("dirty", "1"), ("brief", "1")])
        self.assertEqual(out["text"]["sub"], "4 pages in 2 products")
        self.assertEqual(out["text"]["src"], "~/repos/example/ui-design at abc1234")
        self.assertEqual(out["page_attention"], {"state": "caution", "n": 3, "what": "pages uncommitted or without a current screenshot"})

    def test_a_lamp_filters_the_ledger_and_the_url_keeps_it(self):
        out = self.run_page("click({ s: '.annunciator .cell', dataset: { filter: 'stale' } }); R.rows = ELS.rows.html;")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), [f"{PAGES}/tasks.html"])
        self.assertEqual(out["urls"][-1], {"f": "stale"})
        out = self.run_page("R.rows = ELS.rows.html;", search="?f=brief")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["product:brand"])

    def test_details_open_the_page_and_its_screenshot_where_the_tab_serves_them(self):
        out = self.run_page(f"open('{PAGES}/today.html'); R.page = ELS.details.html; open('product:brand'); R.brief = ELS.details.html;")
        self.assertIn(f'<a class="open" href="/designs/{PAGES}/today.html">', out["R"]["page"])
        self.assertIn('<img src="/designs/products/system/designs/shots/v2-today-1440.png"', out["R"]["page"])
        self.assertIn('width="1440" height="900"', out["R"]["page"])
        self.assertIn("2, current", out["R"]["page"])
        self.assertIn("extracted; no redesign.", out["R"]["brief"])

    def test_a_failed_read_clears_the_page_and_says_so(self):
        out = self.run_page("", answer="() => [503, { error: 'no checkout' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("The designs were not read: no checkout", out["states"][-1]["text"])
        self.assertEqual(out["text"]["sub"], "Not read")
        self.assertEqual(out["page_attention"], {"state": "unknown", "n": 0, "what": "designs not read"})

    def test_an_empty_checkout_says_where_it_looked(self):
        out = self.run_page("", answer="() => [200, { read: '2026-09-23T12:00:00Z', root: '~/absent', head: '', branch: '', products: [], pages: [], caution: 0 }]")
        self.assertEqual(out["states"][-1]["kind"], "empty")
        self.assertIn("No design pages found in ~/absent/products.", out["states"][-1]["text"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_reader_of_its_own(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotIn("location.reload", PAGE_JS)
        self.assertNotIn("window.DESIGNS", PAGE_JS)
        self.assertNotIn("../../", PAGE_JS)
        self.assertIn("shell.read({", PAGE_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "designs", "Designs", "/designs", ("/api/designs",)


if __name__ == "__main__":
    unittest.main()
