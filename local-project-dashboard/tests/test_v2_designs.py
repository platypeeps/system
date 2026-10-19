"""The v2 Designs page (sd:2126).

What this slice promises: `/designs` answers under the shared policy and loads the reader and its script before `shell.js`,
the old listing is retired (sd:3402), and the files stay at `/designs/<path>`.
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

from sd_db import writes

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
    "products": [{"name": "brand", "title": "brand brief", "brief": "products/brand/README.md", "status": "extracted; no `redesign`.", "pages": 0,
                  "briefText": "# brand brief\n\n**Status:** extracted; no `redesign`.\n\nWho it is for.", "briefCut": False},
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
DOC["attention"] = designs._attention(DOC)

#: The design's commands (design source products/system/designs/pages/designs.js): id, object type, label, key, risk.
COMMANDS = [
    ["design.log", "page", "History", "l", "safe"],
    ["design.shots", "page", "Retake screenshots", "s", "safe"],
    ["design.brief", "brief", "Read brief", "b", "safe"],
    # sd:3447: Hide and Unhide, the view state Documents' Hide writes, on each row type.
    ["design.hide", "page", "Hide", "h", "undo"], ["design.unhide", "page", "Unhide", "h", "undo"],
    ["design.hide", "brief", "Hide", "h", "undo"], ["design.unhide", "brief", "Unhide", "h", "undo"],
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
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "designs.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_the_old_one_is_retired_and_the_files_stay_put(self):
        self.assertEqual(v2.SECTIONS.get("Designs"), "/designs")
        self.assertNotIn("Designs", v2.CLASSIC)
        # sd:3402 retired the classic listing; `test_classic_retired` holds the 404.
        self.assertNotIn("Designs (classic)", v2.SCREENS)
        self.assertEqual(self.request("/classic/designs")[0], 404)
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
        self.assertEqual(set(doc), set(designs.ledger(self.root)) | {"store", "attention"})
        self.assertEqual(doc["store"], {"available": True, "reason": ""})
        self.assertEqual([p["hidden"] for p in doc["pages"]], [False])

    def test_every_page_shows_the_badge_the_last_read_carried(self):
        # sd:3446: the ledger runs git, so the rail never reads it; it shows the page's own last read, with its time.
        cookie = {"Cookie": self.cookie}
        badges = lambda: json.loads(re.search(r"^window\.SHELL_COUNTS = (.*);$", self.request("/ui/sections.js", headers=cookie)[2],  # noqa: E731
                                              re.M).group(1))["pages"]
        self.assertNotIn("Designs", badges())
        doc = json.loads(self.request("/api/designs", headers=cookie)[2])
        kept = badges()["Designs"]
        self.assertEqual({key: kept[key] for key in ("state", "n", "what")}, doc["attention"])
        self.assertRegex(kept["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_hide_writes_the_view_state_the_ledger_reads_back_and_refuses_an_unlisted_row(self):
        # sd:3447: the write is Documents' view store, keyed designs/<path>; a row the checkout does not list writes nothing.
        cookie = {"Cookie": self.cookie}
        status, _, out = self.post("/api/designs/view", {"id": "products/system/designs/v1-today.html", "hidden": True})
        self.assertEqual((status, out), (200, {"id": "products/system/designs/v1-today.html", "pinned": False, "hidden": True, "tags": []}))
        status, _, out = self.post("/api/designs/view", {"id": "product:empty", "hidden": True})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.post("/api/designs/view", {"id": "products/system/designs/gone.html", "hidden": True})[0], 400)
        self.assertEqual(self.post("/api/designs/view", {"id": "product:..", "hidden": True})[0], 400)
        self.assertEqual(self.post("/api/designs/view", {"id": "products/system/designs/v1-today.html", "hidden": "yes"})[0], 400)
        self.assertEqual(self.post("/api/designs/view", {"id": "products/system/designs/v1-today.html", "hidden": False},
                                   **{"X-SD-CSRF": "0" * 64})[0], 403)
        doc = json.loads(self.request("/api/designs", headers=cookie)[2])
        self.assertEqual([p["hidden"] for p in doc["pages"]], [True])
        self.assertEqual({p["name"]: p["hidden"] for p in doc["products"]}, {"empty": True, "system": False})
        self.assertEqual(writes.views(self.connection)["designs/products/system/designs/v1-today.html"]["hidden"], True)


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.selected = []; C.select = id => OUT.selected.push(id);
const click = sel => DOC_LISTENERS.click.forEach(f => f({ target: { closest: s => s === sel.s ? { id: sel.id, dataset: sel.dataset || {} } : null } }));
// The shell's view switch: the hash names the view, Pages by default; count() records each tab's count.
location.pathname = '/designs'; location.hash = ''; OUT.counts = {}; OUT.replacedState = null;
var history = { state: null, replaceState(s, t, u) { OUT.replacedState = u; location.hash = u.slice(u.indexOf('#')); } };
window.shell.view = { current: () => location.hash === '#view=hidden' ? 'hidden' : 'pages', count: (id, n) => { OUT.counts[id] = n; },
  show(id) { location.hash = '#view=' + id; document.dispatchEvent(new CustomEvent('shell:view', { detail: { id } })); } };
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
        # The three design commands are copy only; Hide and Unhide (sd:3447) write view state, as Documents' do.
        self.assertEqual(out["R"]["exec"], [False, False, False, None, None, None, None])
        self.assertEqual(out["R"]["cli"], f"git -C ~/repos/example/ui-design log -5 --format='%h %cs %s' -- {PAGES}/today.html")

    def test_the_copy_only_commands_post_nothing_and_say_so(self):
        out = self.run_page(f"shellRun(cmd('design.shots'), C.get('{PAGES}/tasks.html')); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], ["Copy the line to retake designs/pages/tasks.html at 1440 and 375; the dashboard does not run it"])

    def test_read_brief_shows_the_brief_in_details(self):
        # Tuning: Read brief only toasted "Copy the line", its line folded shut, so it seemed to do nothing. It now selects
        # the row and shows the brief's text in Details, as the design source's run ("Brief of <product> shown") meant.
        out = self.run_page(f"open('{PAGES}/today.html'); shellRun(cmd('design.brief'), C.get('product:brand')); await flush(); R.details = ELS.details.html;")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], ["Brief of brand shown in Details"])
        self.assertEqual(out["selected"][-1], "product:brand")
        self.assertIn('<pre class="brief">', out["R"]["details"])
        self.assertIn("Who it is for.", out["R"]["details"])

    def test_a_cut_brief_says_where_the_rest_is(self):
        doc = json.loads(json.dumps(DOC))
        doc["products"][0]["briefCut"] = True
        out = self.run_page("open('product:brand'); R.details = ELS.details.html;", answer=f"(path) => [200, {json.dumps(doc)}]")
        self.assertIn("The rest is in products/brand/README.md.", out["R"]["details"])

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

    SHOW_HIDDEN = "shell.view.show('hidden');"

    def hidden_doc(self):
        """The ledger with the stale page hidden."""
        doc = json.loads(json.dumps(DOC))
        doc["pages"][1]["hidden"] = True
        doc["store"] = {"available": True, "reason": ""}
        doc["attention"] = designs._attention(doc)
        return doc

    def test_a_hidden_row_leaves_the_ledger_the_lamps_and_the_badge_and_the_hidden_view_lists_it(self):
        # sd:3447: the stale page is hidden, so the Shot stale lamp and the badge drop it. sd:5064: Hidden is a page view;
        # Pages, the default, lists the unhidden rows, Hidden the hidden ones alone, and the lamps count the rows the view draws.
        out = self.run_page(f"R.lamps = ELS.annunciator.html; R.rows = ELS.rows.html; {self.SHOW_HIDDEN}"
                            " R.hidden = ELS.rows.html; R.lamps2 = ELS.annunciator.html;"
                            " R.unhide = cmd('design.unhide').when(C.get('" + PAGES + "/tasks.html'));"
                            " R.hide = cmd('design.hide').when(C.get('" + PAGES + "/tasks.html'));", answer=f"(path) => [200, {json.dumps(self.hidden_doc())}]")
        self.assertEqual(re.findall(r'data-filter="(\w+)"[^>]*>.*?<b>(\d+)</b>', out["R"]["lamps"], re.S),
                         [("recent", "2"), ("stale", "0"), ("noshot", "1"), ("dirty", "1"), ("brief", "1")])
        self.assertNotIn(f"{PAGES}/tasks.html", out["R"]["rows"])
        self.assertEqual(out["counts"], {"pages": 4, "hidden": 1})
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["hidden"]), [f"{PAGES}/tasks.html"])
        self.assertEqual(re.findall(r'data-filter="(\w+)"[^>]*>.*?<b>(\d+)</b>', out["R"]["lamps2"], re.S),
                         [("recent", "0"), ("stale", "1"), ("noshot", "0"), ("dirty", "0"), ("brief", "0")])
        # The view is the hash the shell writes, so the query carries no show key and the Show chips are gone.
        self.assertNotIn("show", out["urls"][-1] if out["urls"] else {})
        self.assertNotIn("data-show", json.dumps(out["html"]))
        self.assertEqual((out["R"]["unhide"], out["R"]["hide"]), (True, "already hidden"))
        self.assertEqual(out["page_attention"]["n"], 2)

    def test_an_older_show_hidden_or_hidden_lamp_link_lands_on_the_hidden_view(self):
        for search, query in (("?show=hidden", ""), ("?f=hidden", ""), ("?show=hidden&q=tasks", "?q=tasks")):
            out = self.run_page("R.rows = ELS.rows.html;", answer=f"(path) => [200, {json.dumps(self.hidden_doc())}]", search=search)
            self.assertEqual(out["replacedState"], f"/designs{query}#view=hidden", search)
            self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), [f"{PAGES}/tasks.html"], search)
        out = self.run_page("", search="?f=stale")
        self.assertIsNone(out["replacedState"])

    def test_a_lamp_and_the_hidden_view_combine(self):
        out = self.run_page(f"{self.SHOW_HIDDEN} click({{ s: '.annunciator .cell', dataset: {{ filter: 'noshot' }} }}); R.rows = ELS.rows.html; R.empty = ELS.empty.textContent;",
                            answer=f"(path) => [200, {json.dumps(self.hidden_doc())}]")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), [])
        self.assertEqual(out["urls"][-1], {"f": "noshot"})

    def test_hide_posts_the_row_and_undo_posts_it_back(self):
        out = self.run_page(f"shellRun(cmd('design.hide'), C.get('{PAGES}/notes.html')); await flush();"
                            " cmd('design.hide').undo(C.get('" + PAGES + "/notes.html')); await flush();")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/designs/view", {"id": f"{PAGES}/notes.html", "hidden": True}],
                                                         ["/api/designs/view", {"id": f"{PAGES}/notes.html", "hidden": False}]])
        self.assertEqual(out["toasts"][0], "Hidden: designs/pages/notes.html. The Hidden view lists it.")

    def test_an_unread_store_hides_nothing_and_turns_hide_off_with_its_reason(self):
        doc = json.loads(json.dumps(DOC))
        doc["store"] = {"available": False, "reason": "the view store was not read: locked"}
        out = self.run_page("R.off = cmd('design.hide').when(C.get('" + PAGES + "/notes.html'));", answer=f"(path) => [200, {json.dumps(doc)}]")
        self.assertEqual(out["R"]["off"], "Hide is off: the view store was not read: locked")
        self.assertEqual(len(re.findall(r'<tr data-id=', out["html"]["rows"])), 5)

    def test_a_header_sorts_the_pages_inside_their_product_and_the_url_keeps_it(self):
        # sd:3455: the shell's sort header; the product groups stay, a tie keeps the design's order (kind, then path).
        head = "ELS['rows-head'].listeners.click[0]({ target: { closest: s => s === 'button[data-sort]' ? { dataset: { sort: 'at' } } : null } });"
        ids = r'<tr data-id="([^"]+)"'
        out = self.run_page(f"R.head = ELS['rows-head'].html; {head} R.up = ELS.rows.html; R.head2 = ELS['rows-head'].html; {head} R.down = ELS.rows.html;")
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["s", "page", "at"])
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head"]), [("ascending", "page")])
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head2"]), [("ascending", "at")])
        self.assertEqual(re.findall(ids, out["R"]["up"]), [f"{PAGES}/notes.html", f"{PAGES}/tasks.html", f"{PAGES}/today.html",
                                                            "products/system/designs/v1-today.html", "product:brand"])
        self.assertEqual(re.findall(ids, out["R"]["down"]), ["products/system/designs/v1-today.html", f"{PAGES}/today.html",
                                                              f"{PAGES}/notes.html", f"{PAGES}/tasks.html", "product:brand"])
        self.assertEqual(out["urls"][-1], {"sort": "at", "dir": "desc"})
        out = self.run_page("R.rows = ELS.rows.html; R.head = ELS['rows-head'].html;", search="?sort=at&dir=desc")
        self.assertEqual(re.findall(ids, out["R"]["rows"])[0], "products/system/designs/v1-today.html")
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head"]), [("descending", "at")])

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
    page, section, route, api = "designs", "Designs", "/designs", ("/api/designs", "/api/designs/view")


class TheToolbar(unittest.TestCase):
    """sd:5281: the Designs page's top controls are one toolbar row: the views, the filter, then the count at the right."""

    HTML = (V2 / "designs.html").read_text(encoding="utf-8")

    def test_the_views_then_the_filter_share_one_row_and_the_count_sits_on_the_title_line(self):
        row = re.search(r'<div class="toolbar" id="toolbar">(.*?)\n    </div>', self.HTML, re.S)
        self.assertIsNotNone(row, "designs.html has no toolbar row")
        self.assertLess(row.group(1).index('id="viewswitch"'), row.group(1).index('id="shift"'))
        self.assertNotIn('id="tally"', row.group(1))
        title = re.search(r'<div class="sec-head">(.*?)\n      </div>', self.HTML, re.S).group(1)
        self.assertIn('<p class="tally" id="tally"', title)

    def test_the_head_keeps_no_control_of_its_own(self):
        head = re.search(r'<header class="head">(.*?)</header>', self.HTML, re.S).group(1)
        self.assertNotIn('class="shift"', head)
        self.assertNotIn("<input", head)


if __name__ == "__main__":
    unittest.main()
