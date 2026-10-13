"""The v2 Documents page (sd:2114).

What this slice promises: `/documents` answers under the shared policy and
loads its script before `shell.js`, and the old listing is retired
(sd:3402); `/api/documents` lists every root and every file the
server serves (`documents.roots`, `documents.documents`), with each file's
title, h1 and stand line, its derived kind and its render state, and names a
key two checkouts claim. The page registers the design's Documents commands
with the design's ids, labels, keys and risks. Pin, hide and tag are the
workflow database's view state (sd:3428): each row carries it, each command
posts `/api/documents/view` with Undo, the Hidden filter shows the hidden
files, and a store that was not read turns the six off with its reason.
Disable render is off because no switch exists, and Render and Request are
copy only.

`documents.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses. The browser half is a manual check: the pull
request records what was looked at (1440 px) and what was not verified (375 px
and 320 px, both schemes, a coarse pointer, focus visibility).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect, writes

from sd_dashboard import documents, documents_screen, server, v2

from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
DOCUMENTS_JS = (V2 / "static" / "documents.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: The design's Documents commands (design source products/system/designs/pages/documents.js): id, object type, label, key, risk.
COMMANDS = [
    ["document.render", "document", "Render", "r", "safe"],
    ["document.open", "document", "Open document", "o", "safe"],
    ["document.pin", "document", "Pin", "p", "undo"],
    ["document.unpin", "document", "Unpin", "p", "undo"],
    ["document.hide", "document", "Hide", "h", "undo"],
    ["document.unhide", "document", "Unhide", "h", "undo"],
    ["document.tag", "document", "Tag", "t", "undo"],
    ["document.untag", "document tag", "Untag", "u", "undo"],
    ["document.skip", "document", "Disable render", "s", "safe"],
    ["document.skip-repo", "document", "Disable render for repo", "e", "safe"],
    # build (sd:3525, review 2026-10-12 A2 F5): the design's "Run", r, undo read as a run with an Undo, and r is Render on a
    # document. It only copies the line, so it is Request, safe, and has no key until it files the request.
    ["document.request", "document request draft", "Request", None, "safe"],
]

#: Seconds since the epoch, a day apart: the fixture's files and sources are dated with them.
DAY = 86400
T0 = 1790000000


def write(path: Path, text: str, when: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (when, when))
    return path


def page(title="", h1="", stand="", meta=""):
    head = f"<title>{title}</title>" if title else ""
    head += f'<meta name="description" content="{meta}">' if meta else ""
    body = f"<h1>{h1} <span>part</span></h1>" if h1 else ""
    body += f'<p class="standfirst">{stand}</p>' if stand else ""
    # A chart's own <title> never names the page.
    return f"<!doctype html><html><head>{head}</head><body><svg><title>Chart title</title></svg>{body}</body></html>"


def fixture(base: Path) -> None:
    """Two research documents (one render-stale by research.conf.py, one fresh by name), one without a source, a report,
    an empty root and a contested key."""
    lab = base / "lab"
    write(lab / "research.conf.py", 'DOCS = [{"src": "notes/PLAN.md", "out": "plan"}]\n', T0)
    write(lab / "notes" / "PLAN.md", "# plan\n", T0 + 3 * DAY)
    write(lab / "docs" / "dashboard" / "plan.html", page(title="The plan", h1="Plan", stand="What we build next."), T0 + 2 * DAY)
    write(lab / "00-overview" / "Overview.md", "# overview\n", T0)
    write(lab / "docs" / "dashboard" / "overview.html", page(title="Overview"), T0 + DAY)
    write(lab / "docs" / "dashboard" / "orphan.html", page(), T0 + 4 * DAY)
    write(base / "group" / "civic" / "docs" / "dashboard" / "brief.html",
          page(title="Brief", h1="Civic brief", stand="Not this one.", meta="The meta line wins."), T0 + 5 * DAY)
    (base / "quiet" / "docs" / "dashboard").mkdir(parents=True)
    write(base / "a" / "twin" / "docs" / "dashboard" / "x.html", page(title="A"), T0)
    write(base / "b" / "twin" / "docs" / "dashboard" / "x.html", page(title="B"), T0)


class TheDocument(ScreenCase):
    """`documents_screen.document` lists what the server serves, with what each file says and whether it is render-stale."""

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(self.base)
        self.conf = write(self.base / "documents.conf", "label|lab|Lab\n", T0)
        self.doc = documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)

    def rows(self):
        return {f"{r['key']}/{r['file']}": r for r in self.doc["documents"]}

    def test_every_served_file_is_a_row_newest_first(self):
        self.assertEqual(list(self.rows()), ["civic/brief.html", "lab/orphan.html", "lab/plan.html", "lab/overview.html"])
        for key, row in self.rows().items():
            self.assertEqual(row["href"], f"/documents/{key}")
            self.assertIsNotNone(documents.resolve(row["key"], row["file"], self.conf, self.base), key)
        self.assertEqual(self.rows()["lab/plan.html"]["modified"], "2026-09-23T14:13:20Z")

    def test_the_roots_carry_label_count_and_whether_they_are_research(self):
        self.assertEqual([(r["key"], r["label"], r["n"], r["research"]) for r in self.doc["roots"]],
                         [("civic", "Civic", 1, False), ("lab", "Lab", 3, True), ("quiet", "Quiet", 0, False)])
        self.assertEqual(self.doc["contested"][0]["key"], "twin")
        self.assertEqual(len(self.doc["contested"][0]["paths"]), 2)

    def test_kind_and_render_state_come_from_the_checkout(self):
        rows = self.rows()
        self.assertEqual({k: r["kind"] for k, r in rows.items()},
                         {"civic/brief.html": "report", "lab/orphan.html": "research", "lab/plan.html": "research", "lab/overview.html": "research"})
        # research.conf.py names plan's source, which changed after the page; overview's source is found by name and is older.
        self.assertEqual((rows["lab/plan.html"]["src"], rows["lab/plan.html"]["stale"]), ("notes/PLAN.md", True))
        self.assertEqual((rows["lab/overview.html"]["src"], rows["lab/overview.html"]["stale"]), ("00-overview/Overview.md", False))
        self.assertEqual((rows["lab/orphan.html"]["src"], rows["lab/orphan.html"]["stale"]), ("", False))
        self.assertEqual((rows["civic/brief.html"]["src"], rows["civic/brief.html"]["stale"]), ("", False))

    def test_two_sources_with_the_page_name_match_neither(self):
        write(self.base / "lab" / "elsewhere" / "overview.md", "# again\n", T0)
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual(rows["overview.html"]["src"], "")

    def test_a_unique_source_deeper_than_three_folders_is_found(self):
        # The contract is one .md with the page's name anywhere in the checkout (sd:2417); skipped folders still hold none.
        write(self.base / "lab" / "docs" / "dashboard" / "deep.html", page(title="Deep"), T0 + DAY)
        write(self.base / "lab" / "a" / "b" / "c" / "d" / "Deep.md", "# deep\n", T0)
        write(self.base / "lab" / "a" / "b" / "c" / "d" / "storage" / "deep.md", "# skipped\n", T0)
        write(self.base / "lab" / "a" / "b" / "c" / "d" / ".cache" / "deep.md", "# skipped\n", T0)
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual((rows["deep.html"]["src"], rows["deep.html"]["stale"]), ("a/b/c/d/Deep.md", False))

    def test_a_configured_source_that_is_not_a_file_is_not_a_source(self):
        # A missing src leaves the name match; a src that is a folder leaves the render state unknown, never fresh.
        write(self.base / "lab" / "research.conf.py",
              'DOCS = [{"src": "notes/GONE.md", "out": "overview"}, {"src": "notes", "out": "plan"}]\n', T0)
        (self.base / "lab" / "notes" / "PLAN.md").unlink()
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual((rows["overview.html"]["src"], rows["overview.html"]["stale"]), ("00-overview/Overview.md", False))
        self.assertEqual((rows["plan.html"]["src"], rows["plan.html"]["stale"]), ("", False))

    def test_each_row_says_its_title_h1_and_stand_line(self):
        rows = self.rows()
        self.assertEqual([rows["lab/plan.html"][k] for k in ("title", "h1", "desc")], ["The plan", "Plan part", "What we build next."])
        self.assertEqual([rows["civic/brief.html"][k] for k in ("title", "h1", "desc")], ["Brief", "Civic brief part", "The meta line wins."])
        self.assertEqual([rows["lab/orphan.html"][k] for k in ("title", "h1", "desc")], ["", "", ""])

    def test_an_empty_meta_description_leaves_the_stand_line(self):
        # The meta wins only when it says something; an empty one falls back to the stand-class element.
        path = write(self.base / "page.html", '<html><head><meta name="description" content="  "></head>'
                     '<body><p class="lede">The lede line.</p></body></html>', T0)
        self.assertEqual(documents_screen.facts(path)["desc"], "The lede line.")

    def test_with_no_connection_the_store_is_not_claimed(self):
        self.assertEqual(self.doc["store"]["available"], False)
        self.assertIn("not read", self.doc["store"]["reason"])
        self.assertEqual((self.doc["read"], self.doc["config"]), (NOW, "<config>/project-dashboard/documents.conf"))


class TheViewState(ScreenCase):
    """Pin, hide and tag (sd:3428): the pack's `view` rows, keyed `<root key>/<file>`, on each row the page lists."""

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(self.base)
        self.conf = self.base / "none.conf"

    def read(self, connection):
        doc = documents_screen.document(connection, now=NOW, config_path=self.conf, repo_root=self.base)
        return doc, {f"{r['key']}/{r['file']}": r for r in doc["documents"]}

    def test_each_row_carries_its_pin_hide_and_tags(self):
        writes.set_view(self.connection, "lab/plan.html", pinned=True, tags=["q4", "board"])
        writes.set_view(self.connection, "civic/brief.html", hidden=True)
        doc, rows = self.read(self.connection)
        self.assertEqual(doc["store"], {"available": True, "reason": ""})
        state = {key: (row["pinned"], row["hidden"], row["tags"]) for key, row in rows.items()}
        self.assertEqual(state, {"civic/brief.html": (False, True, []), "lab/orphan.html": (False, False, []),
                                 "lab/plan.html": (True, False, ["q4", "board"]), "lab/overview.html": (False, False, [])})

    def test_a_view_for_a_file_no_longer_served_lists_nothing(self):
        writes.set_view(self.connection, "lab/gone.html", pinned=True)
        doc, rows = self.read(self.connection)
        self.assertNotIn("lab/gone.html", rows)
        self.assertTrue(doc["store"]["available"])

    def test_an_unread_store_hides_nothing_and_says_why(self):
        # A failed read is not a negative answer: the page must know the hidden file shows only because nothing was read.
        writes.set_view(self.connection, "civic/brief.html", hidden=True)
        closed = connect(self.path)
        closed.close()
        doc, rows = self.read(closed)
        self.assertEqual(doc["store"]["available"], False)
        self.assertIn("not read", doc["store"]["reason"])
        self.assertEqual(rows["civic/brief.html"]["hidden"], False)

    def test_a_pack_without_the_view_store_says_to_pull_it(self):
        # A command pack from before sd:3430 has neither reader nor writer; the page still lists every file.
        with mock.patch.object(documents_screen, "writes", types.SimpleNamespace()):
            doc, rows = self.read(self.connection)
            self.assertEqual(len(rows), 4)
            self.assertIn("pull it", doc["store"]["reason"])
            with self.assertRaisesRegex(ValueError, "pull it"):
                documents_screen.request({"root": "lab", "file": "plan.html", "pinned": True},
                                         config_path=self.conf, repo_root=self.base)

    def test_a_write_names_a_served_file_and_a_field(self):
        ask = lambda payload: documents_screen.request(payload, config_path=self.conf, repo_root=self.base)
        for payload in ({}, {"root": "lab", "file": "plan.html"}, {"root": "lab", "file": "plan.html", "pinned": True, "seen": 1},
                        {"root": "lab", "pinned": True}, {"root": 1, "file": "plan.html", "pinned": True},
                        {"root": "lab", "file": "gone.html", "pinned": True}, {"root": "lab", "file": "../x.html", "hidden": True},
                        {"root": "twin", "file": "x.html", "pinned": True}):
            with self.assertRaises(ValueError, msg=payload):
                ask(payload)
        write = ask({"root": "lab", "file": "plan.html", "pinned": True, "tags": ["q4"]})
        self.assertEqual(write(self.connection), {"key": "lab/plan.html", "pinned": True, "hidden": False, "tags": ["q4"]})
        # A field left out keeps its value: Undo of a pin posts pinned alone.
        self.assertEqual(ask({"root": "lab", "file": "plan.html", "pinned": False})(self.connection),
                         {"key": "lab/plan.html", "pinned": False, "hidden": False, "tags": ["q4"]})
        self.assertEqual(writes.views(self.connection), {"lab/plan.html": {"pinned": False, "hidden": False, "tags": ["q4"]}})


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/documents")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Documents · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "documents.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_a_view_write_lands_and_the_next_read_carries_it(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(base)
        cookie = {"Cookie": self.cookie}
        with mock.patch.object(documents, "REPO_ROOT", base), mock.patch.object(documents, "CONFIG", base / "none.conf"):
            status, _, out = self.post("/api/documents/view", {"root": "lab", "file": "plan.html", "hidden": True})
            self.assertEqual((status, out), (200, {"key": "lab/plan.html", "pinned": False, "hidden": True, "tags": []}))
            # The pack judges the values: a tag past its limit is the library's 400, and nothing is written.
            status, _, out = self.post("/api/documents/view", {"root": "lab", "file": "plan.html", "tags": ["x" * 65]})
            self.assertEqual(status, 400, out)
            self.assertEqual(self.post("/api/documents/view", {"root": "lab", "file": "gone.html", "pinned": True})[0], 400)
            # The session and CSRF rules every write keeps.
            self.assertEqual(self.post("/api/documents/view", {"root": "lab", "file": "plan.html", "pinned": True},
                                       **{"X-SD-CSRF": "0" * 64})[0], 403)
            doc = json.loads(self.request("/api/documents", headers=cookie)[2])
        rows = {f"{r['key']}/{r['file']}": r for r in doc["documents"]}
        self.assertEqual((rows["lab/plan.html"]["hidden"], rows["lab/plan.html"]["tags"]), (True, []))
        self.assertEqual(doc["store"], {"available": True, "reason": ""})

    def test_the_rail_opens_the_new_page_and_the_old_listing_is_retired(self):
        self.assertEqual(v2.SECTIONS.get("Documents"), "/documents")
        self.assertNotIn("Documents", v2.CLASSIC)
        # sd:3402 retired the classic listing; `test_classic_retired` holds the 404.
        self.assertNotIn("Documents (classic)", v2.SCREENS)
        self.assertEqual(self.request("/classic/documents")[0], 404)

    def test_the_data_route_needs_a_session_takes_no_query_and_reads_the_roots(self):
        self.assertEqual(self.request("/api/documents")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/documents?repo=lab", headers=cookie)[0], 400)
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(base)
        with mock.patch.object(documents, "REPO_ROOT", base), mock.patch.object(documents, "CONFIG", base / "none.conf"):
            status, _, body = self.request("/api/documents", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual([r["key"] for r in doc["roots"]], ["civic", "lab", "quiet"])
        self.assertEqual(len(doc["documents"]), 4)


# The stand-in additions documents.js needs beyond test_v2_tasks: the shell's row, url, setContext and the window's open.
SHELL_MORE = r"""
window.shell.row = () => ROW; window.shell.setContext = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.opened = []; window.open = (u, t, f) => { OUT.opened.push([u, t, f]); };
"""


class TheScript(ScreenCase):
    """documents.js against the document `documents_screen` builds from the fixture."""

    def setUp(self):
        super().setUp()
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(base)
        self.doc = documents_screen.document(now=NOW, config_path=base / "none.conf", repo_root=base)
        # The page shows the paths the document gives; a fixed one keeps the expected lines readable.
        for root in self.doc["roots"]:
            root["path"] = f"~/repos/{root['key']}/docs/dashboard"
        # The view store answered (sd:3428); the case that it did not sets its own.
        self.doc["store"] = {"available": True, "reason": ""}

    def rows_of(self, **views):
        """The document with each named row's view state set, as the server lists it after a write."""
        for row in self.doc["documents"]:
            row.update(views.get(f"{row['key']}/{row['file']}", {}))

    def run_page(self, body, answer=None, search="", row="null", shell_more=""):
        answer = answer or f"() => [200, {json.dumps(self.doc)}]"
        script = (STAND_IN + f"location.search = {json.dumps(search)}; location.origin = 'http://dash.example.test';\nvar ROW = {row};\n"
                  + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + shell_more + "\n" + READ_SHELL + f"\nANSWER = {answer};\n" + DOCUMENTS_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.page_attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    # The list grammar from shell.list (sd:2682, as Reports in sd:2527-2529). Each click goes to the listener the page put on its holder.
    CLICK = "ELS['%s'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
    URL = "String(OUT.urls[OUT.urls.length - 1])"

    def many(self, n):
        brief = self.doc["documents"][0]
        self.doc["documents"] += [{**brief, "file": f"n{i:02}.html", "href": f"/documents/civic/n{i:02}.html",
                                   "modified": "2026-01-01T00:00:00Z"} for i in range(n)]

    def test_every_column_with_an_order_sorts_through_the_shared_header(self):
        """sd:2682: the header is shell.list's; aria-sort is on the sorted th alone, and the sort is in the URL."""
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'title' }")
        out = self.run_page(f"R.head = ELS['rows-head'].html; {click} R.head2 = ELS['rows-head'].html; R.url = {self.URL};"
                            f" {click} R.url2 = {self.URL};")
        sorted_th = r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"'
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["title", "repo", "mod", "size"])
        self.assertEqual(re.findall(sorted_th, out["R"]["head"]), [("descending", "mod")])
        self.assertEqual(re.findall(sorted_th, out["R"]["head2"]), [("ascending", "title")])
        self.assertEqual((out["R"]["url"], out["R"]["url2"]), ("sort=title&dir=asc", "sort=title&dir=desc"))
        # An older link's ?sort=col.dir still opens its order.
        out = self.run_page("R.head = ELS['rows-head'].html;", search="?sort=size.asc")
        self.assertEqual(re.findall(sorted_th, out["R"]["head"]), [("ascending", "size")])

    def test_the_pager_numbers_pages_and_keeps_page_and_size_in_the_url(self):
        self.many(60)
        out = self.run_page("R.pager = ELS.pager.html;" + self.CLICK % ("pager", "'.list-pager [data-page]'", "{ page: '3' }")
                            + f" R.pager2 = ELS.pager.html; R.url2 = {self.URL};"
                            + self.CLICK % ("pager", "'.list-pager [data-size]'", "{ size: '100' }")
                            + f" R.pager3 = ELS.pager.html; R.url3 = {self.URL};", search="?page=2&size=25")
        total = len(self.doc["documents"])
        self.assertIn(f'<span class="range">26–50 of {total}</span>', out["R"]["pager"])
        self.assertEqual(re.findall(r'data-page="(\d+)"', out["R"]["pager"]), ["1", "2", "3"])
        self.assertIn(f'<span class="range">51–{total} of {total}</span>', out["R"]["pager2"])
        self.assertEqual(out["R"]["url2"], "page=3&size=25")
        self.assertIn(f'<span class="range">1–{total} of {total}</span>', out["R"]["pager3"])
        self.assertEqual(out["R"]["url3"], "size=100")

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        out = self.run_page("R.chips = ELS.chips.html;" + self.CLICK % ("chips", "'[data-unfilter]'", "{ unfilter: 'kind:research' }")
                            + f" R.chips2 = ELS.chips.html; R.url2 = {self.URL};"
                            + " ELS.chips.listeners.click[0]({ target: { closest: s => s === '[data-unfilter-all]' ? {} : null } });"
                            + f" R.chips3 = ELS.chips.html; R.url3 = {self.URL}; R.input = ELS.shift.value;", search="?kind=research&fresh=stale&q=plan")
        self.assertEqual(re.findall(r'data-unfilter="([\w:]+)"', out["R"]["chips"]), ["kind:research", "fresh:stale", "q"])
        self.assertIn("3 filters · 1 of ", out["R"]["chips"])
        self.assertEqual(re.findall(r'data-unfilter="([\w:]+)"', out["R"]["chips2"]), ["fresh:stale", "q"])
        self.assertEqual(out["R"]["url2"], "fresh=stale&q=plan")
        self.assertEqual((out["R"]["chips3"], out["R"]["url3"], out["R"]["input"]), ("", "", ""))

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);")
        self.assertEqual(out["R"]["reg"], COMMANDS)

    def test_an_unread_store_turns_view_state_off_with_its_reason_and_posts_nothing(self):
        reason = "the view store was not read: boom"
        self.doc["store"], self.doc["contested"] = {"available": False, "reason": reason}, []
        out = self.run_page("""const d = C.get('lab/plan.html');
C.put({ id: 'tag:x', type: 'document tag', doc: d, tag: 'x', label: 'x' }); document.getElementById('tag-in').value = 'q4';
const ids = ['document.pin', 'document.unpin', 'document.hide', 'document.unhide', 'document.tag', 'document.untag'];
R.when = ids.map(id => cmd(id).when(cmd(id).on === 'document tag' ? C.get('tag:x') : d));
ids.forEach(id => shellRun(cmd(id), cmd(id).on === 'document tag' ? C.get('tag:x') : d)); await flush();
R.det = ELS.details.html; F.hidden = true; render(); R.empty = ELS.empty.html;""", row="'lab/plan.html'")
        # Unpin and Unhide say first that the row is not pinned or hidden; the unread store is why none is.
        self.assertEqual(out["R"]["when"], [reason, "not pinned", reason, "not hidden", reason, reason])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["gets"], ["/api/documents"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn(f"were not read: {reason}. Every document shows.", out["states"][-1]["text"])
        self.assertIn(f"Tags were not read: {reason}.", out["R"]["det"])
        self.assertNotIn('id="tag-in"', out["R"]["det"])
        self.assertIn(reason, out["R"]["empty"])

    VIEW = "(path, body) => path === '/api/documents/view' ? [200, { key: body.root + '/' + body.file }] : [200, %s]"

    def view_posts(self, out):
        return [body for path, body, _ in out["posts"] if path == "/api/documents/view"]

    def test_pin_and_hide_post_the_field_they_change_and_undo_posts_it_back(self):
        self.rows_of(**{"lab/overview.html": {"pinned": True, "hidden": True}})
        out = self.run_page("""const plan = C.get('lab/plan.html'), over = C.get('lab/overview.html');
for (const [id, o] of [['document.pin', plan], ['document.hide', plan], ['document.unpin', over], ['document.unhide', over]]) {
  shellRun(cmd(id), o); await flush(); lastToast().undo(); await flush(); }
R.when = [cmd('document.pin').when(over), cmd('document.hide').when(over)];""", answer=self.VIEW % json.dumps(self.doc))
        at = lambda file, **change: {"root": "lab", "file": file, **change}
        self.assertEqual(self.view_posts(out), [
            at("plan.html", pinned=True), at("plan.html", pinned=False), at("plan.html", hidden=True), at("plan.html", hidden=False),
            at("overview.html", pinned=False), at("overview.html", pinned=True), at("overview.html", hidden=False),
            at("overview.html", hidden=True)])
        self.assertEqual([t for t in out["toasts"] if t[1]], [["Pinned The plan", True], ["Hidden: The plan. The Hidden filter shows it.", True],
                                                             ["Unpinned Overview", True], ["Unhidden: Overview", True]])
        self.assertEqual([t for t in out["toasts"] if not t[1]], [["Pin undone · The plan", False], ["Hide undone · The plan", False],
                                                                 ["Unpin undone · Overview", False], ["Unhide undone · Overview", False]])
        # Every landed write and every Undo reads the list again, so the rows show what the store now holds.
        self.assertEqual(out["gets"], ["/api/documents"] * 9)
        self.assertEqual(out["R"]["when"], ["already pinned", "already hidden"])

    def test_a_refused_write_says_why_offers_no_undo_and_reads_nothing_again(self):
        out = self.run_page("""shellRun(cmd('document.pin'), C.get('lab/plan.html')); await flush();""",
                            answer="(path, body) => path === '/api/documents/view' ? [400, { error: 'boom' }] : [200, %s]" % json.dumps(self.doc))
        self.assertEqual(out["toasts"], [["The plan not changed: boom", False]])
        self.assertEqual(out["gets"], ["/api/documents"])

    def test_tag_posts_the_list_with_the_typed_tag_and_untag_posts_it_without(self):
        self.rows_of(**{"lab/plan.html": {"tags": ["board"]}})
        out = self.run_page("""const d = C.get('lab/plan.html'), tag = cmd('document.tag');
const enter = v => { document.getElementById('tag-in').value = v;
  ELS.details.listeners.keydown.forEach(f => f({ target: { id: 'tag-in', value: v }, key: 'Enter', preventDefault() {} })); };
R.det = ELS.details.html; R.when = tag.when(d); enter('board'); await flush();
enter('  Q4 Plan '); await flush(); lastToast().undo(); await flush();
C.put({ id: 'tag:b', type: 'document tag', doc: d, tag: 'board', label: 'board on The plan' });
shellRun(cmd('document.untag'), C.get('tag:b')); await flush(); lastToast().undo(); await flush();""",
                            answer=self.VIEW % json.dumps(self.doc), row="'lab/plan.html'")
        self.assertIn('id="tag-in"', out["R"]["det"])
        self.assertIn('data-cmd="document.untag"', out["R"]["det"])
        self.assertIs(out["R"]["when"], True)
        tags = lambda *t: {"root": "lab", "file": "plan.html", "tags": list(t)}
        self.assertEqual(self.view_posts(out), [tags("board", "q4-plan"), tags("board"), tags(), tags("board")])
        self.assertEqual([t[0] for t in out["toasts"]], ["The plan not changed: already tagged board", "Tagged q4-plan · The plan",
                                                        "Tag undone · The plan", "Removed tag board", "Untag undone · board on The plan"])
        self.assertEqual(out["confirms"], [], "Enter in the Tags field tags the shown document without asking")

    def test_tag_asks_with_the_last_tag_filled_in_and_names_the_tag(self):
        # REGRESSION sd:3523: after one tag, Tag on another row applied the remembered tag with no field and no label.
        out = self.run_page("""const tag = cmd('document.tag'), other = C.get('lab/overview.html');
document.getElementById('tag-in').value = 'q4';
ELS.details.listeners.keydown.forEach(f => f({ target: { id: 'tag-in', value: 'q4' }, key: 'Enter', preventDefault() {} })); await flush();
document.getElementById('tag-in').value = '';
R.fields = (tag.fields ? tag.fields(other) : []).map(f => [f.name, f.value, f.required]);
shellRun(tag, other); await flush();
FIELD_VALUES.tag = 'Board Q1'; shellRun(tag, other); await flush();""", answer=self.VIEW % json.dumps(self.doc), row="'lab/plan.html'")
        self.assertEqual(out["R"]["fields"], [["tag", "q4", True]])
        self.assertEqual(out["confirms"], ["document.tag", "document.tag"], "Tag ran on another row without showing its tag")
        self.assertEqual([t[0] for t in out["toasts"]], ["Tagged q4 · The plan", "Tagged q4 · Overview", "Tagged board-q1 · Overview"])

    def test_the_hidden_filter_shows_the_hidden_documents_and_only_them(self):
        self.rows_of(**{"civic/brief.html": {"hidden": True}, "lab/plan.html": {"pinned": True}})
        click = "ELS.facets.listeners.click[0]({ target: { closest: () => ({ id: 'f-hidden', dataset: {}, getAttribute: () => null }) } });"
        out = self.run_page(f"""R.rows = ELS.rows.html; R.facets = ELS.facets.html; {click}
R.hidden = ELS.rows.html; R.url = {self.URL}; {click} R.back = ELS.rows.html;""")
        ids = lambda rows: re.findall(r'data-id="([^"]+)"', rows)
        # Pinned first; the hidden one is out of the list until the filter shows it.
        self.assertEqual(ids(out["R"]["rows"]), ["lab/plan.html", "lab/orphan.html", "lab/overview.html"])
        self.assertRegex(out["R"]["facets"], r'id="f-hidden"[^>]*>.*Hidden <span class="n">1</span>')
        self.assertEqual(ids(out["R"]["hidden"]), ["civic/brief.html"])
        self.assertIn('class="hid"', out["R"]["hidden"])
        self.assertEqual(out["R"]["url"], "hidden=1")
        self.assertEqual(ids(out["R"]["back"]), ids(out["R"]["rows"]))
        # A link to the hidden view opens it.
        out = self.run_page("R.rows = ELS.rows.html;", search="?hidden=1")
        self.assertEqual(ids(out["R"]["rows"]), ["civic/brief.html"])

    def test_disable_render_is_off_and_render_is_a_copy_only_line_for_a_research_root(self):
        out = self.run_page("""const plan = C.get('lab/plan.html'), brief = C.get('civic/brief.html');
R.skip = [cmd('document.skip').when(plan), cmd('document.skip-repo').when(plan)];
const r = cmd('document.render');
R.render = [r.executes, r.cli(plan), r.when(plan), r.when(brief), r.primary(plan), r.primary(C.get('lab/overview.html'))];
shellRun(r, plan); R.toast = lastToast().msg;""")
        self.assertIn("SD_SKIP_RENDER skips every render", out["R"]["skip"][0])
        self.assertIn("skip|<key>|<file>", out["R"]["skip"][0])
        for reason in out["R"]["skip"]:
            self.assertIn("without stopping its render", reason)
        self.assertEqual(out["R"]["render"], [False, "cd '~/repos/lab' && sd-research-kit render", True,
                                              "not a research repo: no source renders this page", True, False])
        self.assertIn("does not run it", out["R"]["toast"])

    def test_the_badge_rule_drops_a_hidden_stale_document(self):
        # sd:3446: the rail's rule is the read's; a stale row the operator hid no longer lights it.
        self.assertEqual(self.doc["attention"], {"state": "caution", "n": 1, "what": "render-stale documents"})
        self.rows_of(**{"lab/plan.html": {"hidden": True}})
        self.assertEqual(documents_screen._attention(self.doc["documents"]), {"state": "ok", "n": 0, "what": "render-stale documents"})

    def test_open_opens_the_served_address_in_a_new_tab(self):
        out = self.run_page("""const o = cmd('document.open'), d = C.get('civic/brief.html'); R.cli = o.cli(d); shellRun(o, d);""")
        self.assertEqual(out["opened"], [["/documents/civic/brief.html", "_blank", "noopener"]])
        self.assertEqual(out["R"]["cli"], "open http://dash.example.test/documents/civic/brief.html")

    def test_the_rows_carry_each_render_state_and_the_rail_counts_the_stale_ones(self):
        out = self.run_page("R.rows = ELS.rows.html; R.tally = ELS.tally.html; R.sub = ELS.subhead.html;")
        rows = out["R"]["rows"]
        self.assertEqual(re.findall(r'data-id="([^"]+)"', rows), ["civic/brief.html", "lab/orphan.html", "lab/plan.html", "lab/overview.html"])
        self.assertEqual(re.findall(r'class="g g-(\w+)"', rows), ["unknown", "unknown", "caution", "ok"])
        self.assertIn("▲ 1 render-stale", out["R"]["tally"])
        self.assertIn("4 documents in 3 roots", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "render-stale documents"})

    def test_a_request_is_a_copy_only_line_and_files_nothing(self):
        out = self.run_page("""const input = ELS.shift; input.value = 'request water summary repo:lab';
input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f());
const r = cmd('document.request'), d = C.get('draft:request');
R.r = [r.executes, r.when(d), r.cli(d)]; shellRun(r, d); await flush();""")
        executes, when, cli = out["R"]["r"]
        self.assertEqual((executes, when), (False, True))
        self.assertEqual(cli, "sd task add 'Document request: water summary' --body 'repo=lab kind=research skill=sd-research-repo'")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1], ["Copy the line: the dashboard does not file document requests yet", False])

    def test_a_request_names_only_a_repository_it_can_resolve(self):
        # An unknown or ambiguous repo: never falls back to the selected row's repository; an exact key or a unique prefix wins.
        self.doc["roots"].append({"key": "lab-two", "label": "Lab Two", "path": "~/repos/lab-two/docs/dashboard", "n": 0, "research": False})
        out = self.run_page("""const input = ELS.shift, r = cmd('document.request'), d = C.get('draft:request');
const ask = v => { input.value = ''; input.listeners.beforeinput.forEach(f => f()); input.value = v;
  input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f()); return [r.when(d), d.repo, r.cli(d)]; };
R.unknown = ask('request memo repo:nowhere'); R.ambiguous = ask('request memo repo:la');
R.exact = ask('request memo repo:lab'); R.unique = ask('request memo repo:ci');""")
        R = out["R"]
        for got, word in ((R["unknown"], "nowhere"), (R["ambiguous"], "lab, lab-two")):
            self.assertIsInstance(got[0], str)
            self.assertIn(word, got[0])
            self.assertEqual(got[1:], [None, ""])
        self.assertEqual(R["exact"][:2], [True, "lab"])
        self.assertEqual(R["unique"][:2], [True, "civic"])
        self.assertNotIn("repo:", R["exact"][2])

    def test_a_request_takes_the_skill_its_hint_asks_for(self):
        # A report has no skill; the hint says to name one with skill:, so that token names it and leaves the title.
        out = self.run_page("""const input = ELS.shift; input.value = 'request budget memo repo:civic skill:sd-writer';
input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f());
R.cli = cmd('document.request').cli(C.get('draft:request'));""")
        self.assertEqual(out["R"]["cli"], "sd task add 'Document request: budget memo' --body 'repo=civic kind=report skill=sd-writer'")

    def test_each_read_state_is_said_in_the_slot(self):
        out = self.run_page("")
        self.assertEqual(out["states"][0]["kind"], "loading")
        # The fixture's twin key is contested: the slot names it, and the source is the config.
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("claim the key twin", out["states"][-1]["text"])
        empty = {**self.doc, "roots": [], "documents": [], "contested": []}
        last = self.run_page("", answer=f"() => [200, {json.dumps(empty)}]")["states"][-1]
        self.assertEqual((last["kind"], last["title"]), ("empty", "No document roots"))
        self.assertIn("docs/dashboard", last["text"])
        failed = self.run_page("", answer="() => [500, { error: 'boom' }]")["states"][-1]
        self.assertEqual(failed["kind"], "error")
        self.assertIn("boom", failed["text"])

    def test_an_unavailable_chip_stays_focusable_says_why_and_does_nothing(self):
        out = self.run_page("""R.facets = ELS.facets.html;
const chip = (f, v) => ({ id: '', dataset: { f: f, v: v }, getAttribute: a => a === 'aria-disabled' ? 'true' : null });
['kind:design', 'repo:quiet'].forEach(fv => { const [f, v] = fv.split(':'), c = chip(f, v);
  ELS.facets.listeners.click.forEach(h => h({ target: { closest: () => c } })); });
R.picked = [...F.kind, ...F.repo];""")
        facets = out["R"]["facets"]
        # A native disabled button leaves the tab order, and its title with it; the reason must stay reachable.
        self.assertNotRegex(facets, r"<button[^>]*\sdisabled[\s>]")
        design = re.search(r'<button[^>]*data-v="design"[^>]*>', facets).group(0)
        quiet = re.search(r'<button[^>]*data-v="quiet"[^>]*>', facets).group(0)
        for tag in (design, quiet):
            self.assertIn('aria-disabled="true"', tag)
        # A title is a hover tooltip only; the reason is an element the chip names, so focus and a screen reader reach it.
        for tag, reason in ((design, "No design documents"), (quiet, "Nothing generated yet in ~/repos/quiet/docs/dashboard")):
            ref = re.search(r'aria-describedby="([^"]+)"', tag)
            self.assertIsNotNone(ref, tag)
            said = re.search(rf'<span[^>]*id="{re.escape(ref.group(1))}"[^>]*>([^<]*)</span>', facets)
            self.assertIsNotNone(said, ref.group(1))
            self.assertIn(reason, said.group(1))
        self.assertNotIn("aria-disabled", re.search(r'<button[^>]*data-v="research"[^>]*>', facets).group(0))
        self.assertEqual(out["R"]["picked"], [])

    def test_the_row_in_the_address_is_selected_and_details_name_its_facts(self):
        out = self.run_page("R.det = ELS.details.html;", row="'lab/plan.html'")
        det = out["R"]["det"]
        self.assertIn("render-stale", det)
        self.assertIn("notes/PLAN.md", det)
        self.assertIn("What we build next.", det)
        self.assertIn('id="tag-in"', det)

    def test_a_linked_row_that_is_not_first_survives_the_first_render(self):
        # The real shell: reconcile selects the first shown row when nothing is selected, and select rewrites ?row=.
        rewrites = (r"""C.select = id => { ROW = id; };
window.shell.reconcile = o => { if (o.current != null) return o.current;"""
                    r""" const m = /data-id="([^"]+)"/.exec(ELS.rows.html || ''); if (m) { o.select(m[1]); return m[1]; } return null; };""")
        out = self.run_page("R.row = ROW; R.det = ELS.details.html;", row="'lab/overview.html'", shell_more=rewrites)
        self.assertEqual(out["R"]["row"], "lab/overview.html")
        self.assertIn("00-overview/Overview.md", out["R"]["det"])

    def test_the_page_reads_through_the_shared_reader(self):
        # read.js holds the generation, retirement and failure rules (sd:2490); the page keeps no fetch of its own.
        self.assertIn("shell.read(", DOCUMENTS_JS)
        self.assertNotIn("fetch(", DOCUMENTS_JS)
        out = self.run_page("")
        self.assertEqual(out["states"][0]["text"], "Reading the documents. Rows appear when /api/documents answers.")

    def test_a_document_the_next_read_drops_runs_no_command_and_loses_the_selection(self):
        out = self.run_page("""const first = ANSWER()[1];
ANSWER = () => [200, { ...first, documents: first.documents.filter(d => d.file !== 'plan.html'), attention: { state: 'ok', n: 0, what: 'render-stale documents' } }];
await load();
R.type = C.get('lab/plan.html').type; R.rows = ELS.rows.html; R.det = ELS.details.html; R.att = window.PAGE_ATTENTION;""",
                            row="'lab/plan.html'")
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertNotIn("lab/plan.html", out["R"]["rows"])
        self.assertNotIn("What we build next.", out["R"]["det"])
        self.assertIn("Civic brief", out["R"]["det"])
        self.assertEqual(out["R"]["att"], {"state": "ok", "n": 0, "what": "render-stale documents"})

    def test_an_older_read_that_answers_last_changes_nothing(self):
        out = self.run_page("""const first = ANSWER()[1], held = [];
ANSWER = () => new Promise(ok => held.push(ok));
const older = load(), newer = load();
held[1]([200, { ...first, documents: first.documents.slice(0, 1) }]); await newer;
held[0]([200, first]); await older;
R.rows = ELS.rows.html; R.type = C.get('lab/plan.html').type;""")
        self.assertEqual(re.findall(r'data-id="([^"]+)"', out["R"]["rows"]), ["civic/brief.html"])
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_failed_read_after_a_good_one_clears_the_rows_and_the_rail(self):
        out = self.run_page("""ANSWER = () => [500, { error: 'boom' }]; await load();
R.rows = ELS.rows.html; R.type = C.get('lab/plan.html').type; R.att = window.PAGE_ATTENTION; R.det = ELS.details.html;""",
                            row="'lab/plan.html'")
        self.assertEqual(re.findall(r'data-id="([^"]+)"', out["R"]["rows"]), [])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["R"]["att"]["state"], "unknown")
        self.assertIn("were not read", out["R"]["det"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])

    def test_a_page_number_that_is_not_a_whole_number_reads_as_page_one(self):
        # ?page=1.5 sliced mid-page, pressed no pager button and stayed in the address (sd:2427).
        self.many(60)
        for search, page_no in (("?page=1.5", "1"), ("?page=0x2", "1"), ("?page=2", "2")):
            out = self.run_page(f"R.pager = ELS.pager.html; R.url = {self.URL};", search=search)
            current = re.findall(r'data-page="(\d+)"[^>]*aria-current="page"', out["R"]["pager"])
            self.assertEqual(current, [page_no], search)
            self.assertEqual(re.findall(r"page=(\d+)", out["R"]["url"]), [] if page_no == "1" else [page_no], search)

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", DOCUMENTS_JS)
        self.assertNotIn("setAttribute('style'", DOCUMENTS_JS)
        self.assertNotRegex(DOCUMENTS_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", DOCUMENTS_JS)
        self.assertNotRegex(DOCUMENTS_JS, r"e\.key !?== '[jk]'")
        self.assertNotIn("127.0.0.1", DOCUMENTS_JS)
        self.assertIn("window.PAGE_LIST", DOCUMENTS_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "documents", "Documents", "/documents", ("/api/documents", "/api/documents/view")


if __name__ == "__main__":
    unittest.main()
