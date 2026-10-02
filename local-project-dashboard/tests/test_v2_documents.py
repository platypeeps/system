"""The v2 Documents page (sd:2114).

What this slice promises: `/documents` answers under the shared policy and
loads its script before `shell.js`, and the old listing moves to
`/classic/documents`; `/api/documents` lists every root and every file the
server serves (`documents.roots`, `documents.documents`), with each file's
title, h1 and stand line, its derived kind and its render state, and names a
key two checkouts claim. The page registers the design's Documents commands
with the design's ids, labels, keys and risks. Pin, hide and tag are off with
the store's reason, Disable render is off because no switch exists, Render and
Request are copy only, and no command declares an Undo: the page sends nothing.

`documents.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses. The browser half is a manual check: the pull
request records what was looked at (1440 px) and what was not verified (375 px
and 320 px, both schemes, a coarse pointer, focus visibility).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import documents, documents_screen, server, v2

from support import NOW, ScreenCase
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
DOCUMENTS_JS = (V2 / "static" / "documents.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: The design's Documents commands (design source products/system/designs/v2/documents.js): id, object type, label, key, risk.
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
    ["document.request", "document request draft", "Run", "r", "undo"],
]

#: The filter the copied request pair pipes `sd task add --json` through, so `sd run` gets the new item's id.
PICK_ID = """python3 -c 'import json, sys; print(json.load(sys.stdin)["item"]["id"])'"""

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

    def test_no_store_is_claimed(self):
        self.assertEqual(self.doc["store"], {"available": False, "reason": documents_screen.STORE_REASON})
        self.assertEqual((self.doc["read"], self.doc["config"]), (NOW, "<config>/project-dashboard/documents.conf"))


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/documents")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Documents · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "documents.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_old_listing_moved(self):
        self.assertEqual(v2.SECTIONS.get("Documents"), "/documents")
        self.assertNotIn("Documents", v2.CLASSIC)
        self.assertEqual(v2.SCREENS.get("Documents (classic)"), "/classic/documents")
        status, _, body = self.request("/classic/documents")
        self.assertEqual(status, 200)
        self.assertIn("Documents — sd</title>", body)

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

    def run_page(self, body, answer=None, search="", row="null", shell_more=""):
        answer = answer or f"() => [200, {json.dumps(self.doc)}]"
        script = (STAND_IN + f"location.search = {json.dumps(search)}; location.origin = 'http://dash.example.test';\nvar ROW = {row};\n"
                  + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + shell_more + f"\nANSWER = {answer};\n" + DOCUMENTS_JS
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

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);")
        self.assertEqual(out["R"]["reg"], COMMANDS)

    def test_view_state_is_off_with_the_store_reason_and_nothing_declares_an_undo(self):
        out = self.run_page("""const d = C.get('lab/plan.html');
C.put({ id: 'tag:x', type: 'document tag', doc: d, tag: 'x', label: 'x' });
const ids = ['document.pin', 'document.unpin', 'document.hide', 'document.unhide', 'document.tag', 'document.untag'];
R.when = ids.map(id => cmd(id).when(cmd(id).on === 'document tag' ? C.get('tag:x') : d));
R.undo = REG.filter(c => c.undo).map(c => c.id);
REG.forEach(c => shellRun(c, c.on === 'document tag' ? C.get('tag:x') : c.on === 'document' ? d : C.get('draft:request'))); await flush();""")
        reason = documents_screen.STORE_REASON
        # Unpin and Unhide say first that the row is not pinned or hidden; the store is why it never is.
        self.assertEqual(out["R"]["when"], [reason, "not pinned", reason, "not hidden", reason, reason])
        self.assertEqual(out["R"]["undo"], [])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["gets"], ["/api/documents"])
        self.assertTrue(all(not undo for _, undo in out["toasts"]), out["toasts"])

    def test_disable_render_is_off_and_render_is_a_copy_only_line_for_a_research_root(self):
        out = self.run_page("""const plan = C.get('lab/plan.html'), brief = C.get('civic/brief.html');
R.skip = [cmd('document.skip').when(plan), cmd('document.skip-repo').when(plan)];
const r = cmd('document.render');
R.render = [r.executes, r.cli(plan), r.when(plan), r.when(brief), r.primary(plan), r.primary(C.get('lab/overview.html'))];
shellRun(r, plan); R.toast = lastToast().msg;""")
        self.assertIn("SD_SKIP_RENDER skips every render", out["R"]["skip"][0])
        self.assertIn("sd:1904", out["R"]["skip"][0])
        self.assertIn("without stopping its render", out["R"]["skip"][1])
        self.assertEqual(out["R"]["render"], [False, "cd '~/repos/lab' && sd-research-kit render", True,
                                              "not a research repo: no source renders this page", True, False])
        self.assertIn("does not run it", out["R"]["toast"])

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

    def test_a_request_is_a_copy_only_pair_of_lines_and_files_nothing(self):
        out = self.run_page("""const input = ELS.shift; input.value = 'request water summary repo:lab';
input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f());
const r = cmd('document.request'), d = C.get('draft:request');
R.r = [r.executes, r.when(d), r.cli(d)]; shellRun(r, d); await flush();""")
        executes, when, cli = out["R"]["r"]
        self.assertEqual((executes, when), (False, True))
        self.assertEqual(cli, "ITEM=$(sd task add 'Document request: water summary' --body 'repo=lab kind=research skill=sd-research-repo' --json"
                              f" | {PICK_ID}) &&\n"
                              "sd run --sequential --role author --scope 'lab' --budget-minutes 30 \"$ITEM\"")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1], ["Copy the two lines: the dashboard does not file document requests yet", False])

    def test_the_copied_request_lines_queue_the_item_they_create(self):
        # `<item>` in sh redirects stdin from a file, and a stale ITEM would queue another task. Run the copied text in sh
        # against a stand-in sd: the run must get the id the add printed, and nothing runs when the add fails.
        out = self.run_page("""const input = ELS.shift; input.value = 'request water summary repo:lab';
input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f());
R.cli = cmd('document.request').cli(C.get('draft:request'));""")
        with tempfile.TemporaryDirectory() as bin_dir:
            log = Path(bin_dir) / "runs"
            stand_in = Path(bin_dir) / "sd"
            stand_in.write_text(f"""#!/bin/sh
if [ "$1 $2" = "task add" ]; then [ -n "$SD_ADD_FAILS" ] && exit 1; echo '{{"item": {{"id": 4242, "title": "x"}}}}'; exit 0; fi
echo "$*" >> {shlex.quote(str(log))}
""", encoding="utf-8")
            stand_in.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", ITEM="7")
            ran = subprocess.run(["sh", "-c", out["R"]["cli"]], env=env, cwd=bin_dir, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").split()[-1], "4242")
            log.unlink()
            failed = subprocess.run(["sh", "-c", out["R"]["cli"]], env=dict(env, SD_ADD_FAILS="1"), cwd=bin_dir,
                                    capture_output=True, text=True, timeout=30, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(log.exists(), "sd run ran after sd task add failed")

    def test_a_request_takes_the_skill_its_hint_asks_for(self):
        # A report has no skill; the hint says to name one with skill:, so that token names it and leaves the title.
        out = self.run_page("""const input = ELS.shift; input.value = 'request budget memo repo:civic skill:sd-writer';
input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f());
R.cli = cmd('document.request').cli(C.get('draft:request'));""")
        self.assertEqual(out["R"]["cli"], "ITEM=$(sd task add 'Document request: budget memo' --body 'repo=civic kind=report skill=sd-writer' --json"
                                          f" | {PICK_ID}) &&\n"
                                          "sd run --sequential --role author --scope 'civic' --budget-minutes 30 \"$ITEM\"")

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
        self.assertIn(documents_screen.STORE_REASON, det)

    def test_a_linked_row_that_is_not_first_survives_the_first_render(self):
        # The real shell: reconcile selects the first shown row when nothing is selected, and select rewrites ?row=.
        rewrites = (r"""C.select = id => { ROW = id; };
window.shell.reconcile = o => { if (o.current != null) return o.current;"""
                    r""" const m = /data-id="([^"]+)"/.exec(ELS.rows.html || ''); if (m) { o.select(m[1]); return m[1]; } return null; };""")
        out = self.run_page("R.row = ROW; R.det = ELS.details.html;", row="'lab/overview.html'", shell_more=rewrites)
        self.assertEqual(out["R"]["row"], "lab/overview.html")
        self.assertIn("00-overview/Overview.md", out["R"]["det"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", DOCUMENTS_JS)
        self.assertNotIn("setAttribute('style'", DOCUMENTS_JS)
        self.assertNotRegex(DOCUMENTS_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", DOCUMENTS_JS)
        self.assertNotRegex(DOCUMENTS_JS, r"e\.key !?== '[jk]'")
        self.assertNotIn("127.0.0.1", DOCUMENTS_JS)
        self.assertIn("window.PAGE_LIST", DOCUMENTS_JS)


if __name__ == "__main__":
    unittest.main()
