"""Today reads /api/now through the shared reader (sd:2483).

What this slice promises: `today.js` reads its document through `shell.read`
(`read.js`, sd:2418). Of two overlapping reads only the newest draws; a row a
read no longer lists runs no command; a failed read clears the rows and
retires their objects; a selection whose row is gone moves to the first row
the filter shows.

`today.js` runs under JavaScriptCore (osascript) against the Tasks stand-in
page and shell, with the real reader and the shell's own `reconcile`, and
with the document `now_screen.document` builds as its fetch answer.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from sd_dashboard import now_screen, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document, repo
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT

V2 = Path(v2.__file__).resolve().parent
TODAY_JS = (V2 / "static" / "today.js").read_text(encoding="utf-8")
SNOOZE_JS = (V2 / "static" / "snooze.js").read_text(encoding="utf-8")
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
# reconcile reads the walk's rule for a shown row (sd:5113), so the extract carries that line too.
RECONCILE = (re.search(r"^  const shownRow = .*$", SHELL_JS, re.M).group(0) + "\n"
             + re.search(r"^  function reconcile\(.*?^  }$", SHELL_JS, re.S | re.M).group(0))

# What today.js reads beyond the Tasks stand-in: the address it writes ?row= into, and the ledger's rows as elements that
# keep their hidden flag and aria-selected between calls. The shell's row is the address's, as shell.js reads it.
PAGE = r"""
function URL(h) { var i = h.indexOf('?'), m = new Map();
  (i < 0 ? '' : h.slice(i + 1)).split('&').filter(Boolean).forEach(p => { var kv = p.split('='); m.set(kv[0], decodeURIComponent(kv[1] || '')); });
  this.searchParams = { set: (k, v) => m.set(k, String(v)), delete: k => m.delete(k), get: k => m.has(k) ? m.get(k) : null };
  Object.defineProperty(this, 'search', { get: () => { var s = [...m].map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&'); return s ? '?' + s : ''; } }); }
Object.defineProperty(location, 'href', { get: () => 'http://example.test/today' + location.search });
var history = { replaceState(a, b, u) { location.search = u.search; } };
var ROWS_EL = document.getElementById('rows'), ROW_CACHE = { html: null, els: [] };
ROWS_EL.querySelectorAll = () => {
  if (ROW_CACHE.html !== ROWS_EL.html) { ROW_CACHE.html = ROWS_EL.html;
    ROW_CACHE.els = [...(ROWS_EL.html || '').matchAll(/<tr data-id="([^"]*)" data-src="([^"]*)"/g)].map(m => { var e = El('tr'), attrs = {};
      e.dataset = { id: m[1], src: m[2] }; e.textContent = m[1]; e.getClientRects = () => e.hidden ? [] : [1]; e.closest = sel => sel === '[hidden]' && e.hidden ? e : null;
      e.setAttribute = (k, v) => { attrs[k] = String(v); }; e.getAttribute = k => k in attrs ? attrs[k] : null; return e; }); }
  return ROW_CACHE.els; };
"""
SHELL_MORE = r"""
window.shell.row = () => new URLSearchParams(location.search).get('row');
OUT.shown = []; OUT.counts = {}; window.shell.view = { show: id => { OUT.shown.push(id); return true; }, current: () => 'now', count: (id, n) => { OUT.counts[id] = n; } };
{ const commands = C;
""" + RECONCILE + """
  window.shell.reconcile = reconcile; }
const refresh = () => window.PAGE_COMMANDS.find(c => c.label === 'Read Now again').run();
const selected = () => ROWS_EL.querySelectorAll().filter(tr => tr.getAttribute('aria-selected') === 'true').map(tr => tr.dataset.id);
"""
# Every read waits until the test answers it: held[i](doc) answers the i-th read since the page started.
HELD = r"""
const held = [], copy = d => JSON.parse(JSON.stringify(d));
ANSWER = () => new Promise(ok => held.push(doc => ok(doc && doc.error ? [500, doc] : [200, doc])));
"""


class TheReader(ScreenCase):
    def setUp(self):
        super().setUp()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        jobs = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        self.doc = now_screen.document(self.connection, now=NOW, jobs=jobs,
                                       fleet=lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))
        self.assertEqual([row["id"] for row in self.doc["rows"]], ["job:nightly-sync:7", "ahead:pushy:1"])

    def run_page(self, body, search="", after=""):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + SHELL_MORE + READ_SHELL
                  + f"\nconst DOC0 = {json.dumps(self.doc)};\nlocation.search = {json.dumps(search)};\n"
                  + "ANSWER = () => [200, DOC0];\n" + SNOOZE_JS + COMMANDS_JS + TODAY_JS + after
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_first_read_draws_the_rows_and_selects_the_first(self):
        """A guard: the page as it was, through the reader."""
        out = self.run_page("R.rows = ELS.rows.html; R.sel = selected(); R.row = location.search;")
        self.assertEqual(out["gets"], ["/api/now"])
        self.assertIn("nightly-sync", out["R"]["rows"])
        self.assertEqual(out["R"]["sel"], ["job:nightly-sync:7"])
        self.assertEqual(out["R"]["row"], "?row=job%3Anightly-sync%3A7")

    def test_only_the_newest_read_draws(self):
        out = self.run_page(HELD + """const gone = copy(DOC0); gone.rows = gone.rows.filter(r => r.kind !== 'job');
refresh(); await flush(); refresh(); await flush();
held[1](gone); await flush(); held[0](copy(DOC0)); await flush();
R.rows = ELS.rows.html; R.type = C.get('job:nightly-sync:7').type;""")
        self.assertNotIn("nightly-sync", out["R"]["rows"], "an older read answered last and drew over the newest")
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_row_the_read_no_longer_lists_runs_no_command(self):
        out = self.run_page("""DOC0.rows = DOC0.rows.filter(r => r.kind !== 'job'); refresh(); await flush();
R.obj = [C.get('job:nightly-sync:7').type, C.get('job:nightly-sync:7').label]; R.kept = C.get('ahead:pushy:1').type;""")
        self.assertEqual(out["R"]["obj"], ["not listed", "nightly-sync failed with exit 7 (no longer listed)"])
        self.assertEqual(out["R"]["kept"], "repo")

    def test_a_failed_read_clears_the_rows_and_retires_their_objects(self):
        out = self.run_page("""ANSWER = () => [500, { error: 'fleet collection was stopped at its budget' }]; refresh(); await flush();
R.rows = ELS.rows.html; R.sub = ELS.subhead.textContent; R.type = C.get('job:nightly-sync:7').type; R.row = location.search;""")
        self.assertEqual(out["states"][-1:], [{"kind": "error", "source": "/api/now",
                                               "text": "The Now rows were not read: fleet collection was stopped at its budget. Reload retries it."}])
        self.assertNotIn("nightly-sync", out["R"]["rows"])
        self.assertIn("Now could not be read: fleet collection was stopped at its budget", out["R"]["rows"])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["R"]["row"], "")
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})

    def test_a_selection_whose_row_is_gone_moves_to_the_first_row(self):
        """A guard: shell.reconcile did this before the reader; the reader keeps it."""
        out = self.run_page("""DOC0.rows = DOC0.rows.filter(r => r.kind !== 'job'); refresh(); await flush();
R.sel = selected(); R.details = ELS.details.html;""", search="?row=job%3Anightly-sync%3A7")
        self.assertEqual(out["R"]["sel"], ["ahead:pushy:1"])
        self.assertIn("pushy", out["R"]["details"])

    def test_the_page_reads_through_the_shared_reader(self):
        self.assertIn("shell.read(", TODAY_JS)
        self.assertNotIn("fetch('/api/now'", TODAY_JS)

    # sd:3525 (review 2026-10-12, A2 F14): the shell owns j / k and Esc through PAGE_LIST, so a g-chord and the keys sheet
    # read the same on Today as on every other page. The page binds neither key itself.
    KEY = ("ROWS_EL.querySelectorAll().forEach(tr => { tr.scrollIntoView = () => {}; });\n"
           "const key = k => document.dispatchEvent({ type: 'keydown', key: k, target: El('main'), preventDefault() {} });\n")

    def test_the_shell_walks_the_rows_through_page_list_and_the_page_binds_no_j(self):
        out = self.run_page(self.KEY + """key('j'); R.afterJ = selected();
const L = window.PAGE_LIST, rows = [...L.rows()]; L.select(rows[1].dataset.id, rows[1]); R.sel = selected(); R.row = location.search;""")
        self.assertEqual(out["R"]["afterJ"], ["job:nightly-sync:7"], "today.js moved the row on j itself; the shell owns j")
        self.assertEqual(out["R"]["sel"], ["ahead:pushy:1"])
        self.assertEqual(out["R"]["row"], "?row=ahead%3Apushy%3A1")
        self.assertNotRegex(TODAY_JS, r"e\.key === '[jk]'")

    def test_esc_through_page_list_clears_the_source_filter_once(self):
        out = self.run_page(self.KEY + """const cell = { id: 'jobs-cell', dataset: { src: 'jobs' } };
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? cell : null } });
R.hidden = ROWS_EL.querySelectorAll().filter(tr => tr.hidden).map(tr => tr.dataset.id);
key('Escape'); R.afterEsc = ROWS_EL.querySelectorAll().filter(tr => tr.hidden).length;
R.cleared = window.PAGE_LIST.clear(); R.shown = ROWS_EL.querySelectorAll().filter(tr => tr.hidden).length; R.again = window.PAGE_LIST.clear();""")
        self.assertEqual(out["R"]["hidden"], ["ahead:pushy:1"])
        self.assertEqual(out["R"]["afterEsc"], 1, "today.js cleared the filter on Esc itself; the shell sends Esc to PAGE_LIST.clear")
        self.assertEqual((out["R"]["cleared"], out["R"]["shown"], out["R"]["again"]), (True, 0, False))



class TheViews(TheReader):
    """sd:5080: Today's areas are the shell's views; each tab counts what waits there, and an unread section hides it."""

    def test_the_areas_are_page_views_and_each_panel_holds_its_sections(self):
        # sd:5781: Decisions moved to Decide, GitHub and Issues to GitHub; Today keeps four views, by job.
        views = re.search(r"window\.PAGE_VIEWS = (\[.*?\]);", TODAY_JS, re.S).group(1)
        self.assertEqual(re.findall(r"id: '([a-z]+)'", views), ["now", "inbox", "status", "work"])
        self.assertIn("{ id: 'inbox', label: 'Inbox', key: 'm' }", views)
        page = (V2 / "today.html").read_text(encoding="utf-8")
        panels = {m.group(1): re.findall(r'<h2[^>]*id="([a-z]+-h)"', m.group(2))
                  for m in re.finditer(r'<div id="view-([a-z]+)"(.*?)(?=<div id="view-|</main>)', page, re.S)}
        self.assertEqual(panels, {"now": ["now-h"], "status": ["status-h"], "work": ["active-h", "work-h"], "inbox": ["problems-h", "briefs-h", "mail-h"]})
        scripts = "".join((V2 / "static" / f).read_text(encoding="utf-8") for f in ("today.js", "today-reports.js", "mail.js"))
        reported = set(re.findall(r"todayCount(?:\?\.)?\('([a-z]+)'", scripts))
        self.assertEqual(reported, {"now", "work", "problems", "briefs", "mail"})

    def test_a_read_counts_now_and_an_unread_section_hides_its_tabs_count(self):
        out = self.run_page("""R.now = OUT.counts.now;
window.todayCount('problems', 2); window.todayCount('briefs', 3); window.todayCount('mail', null); R.unread = OUT.counts.inbox;
window.todayCount('mail', 1); R.read = OUT.counts.inbox;
ANSWER = () => [500, { error: 'stopped' }]; refresh(); await flush(); R.failed = [OUT.counts.now, OUT.counts.work];""")
        self.assertEqual(out["R"]["now"], 2, "the failed job and the unpushed repo both want you")
        self.assertEqual((out["R"]["unread"], out["R"]["read"]), (None, 6))
        self.assertEqual(out["R"]["failed"], [None, None])

    def test_a_lamp_shows_now_which_it_filters(self):
        out = self.run_page("""const cell = { id: 'jobs-cell', dataset: { src: 'jobs' } };
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? cell : null } });""")
        self.assertEqual(out["shown"], ["now"])


class TheLamps(TheReader):
    """sd:TBD: a fleet area the server's snapshot kept says when it was read; a stale one is unknown, never green."""

    CELLS = """const CELLS = ['repos', 'sessions'].map(src => { const c = El('div'), val = El('span');
  c.dataset = { src }; c.querySelector = () => val; c.removeAttribute = k => { delete c[k]; }; c.val = val; return c; });
document.querySelectorAll = sel => sel === '.cell[data-src]' ? CELLS : [];
DOC0.observed = { repos: { at: '2026-09-06T11:31:00Z', stale: 'git did not answer' }, sessions: { at: '2026-09-06T11:58:00Z', stale: null } };
refresh(); await flush();
R.cells = CELLS.map(c => [c.dataset.src, c.dataset.state, c.title || null]); R.observed = ELS.observed.html;"""

    def test_a_kept_fleet_area_shows_its_read_time_and_a_stale_one_is_unknown(self):
        out = self.run_page(self.CELLS)
        self.assertEqual(out["R"]["cells"], [["repos", "unknown", "observed 11:31 UTC · stale: git did not answer"],
                                            ["sessions", "ok", "observed 11:58 UTC"]])
        self.assertIn("<b>11:31</b> UTC", out["R"]["observed"])


class TheFix(TheReader):
    """sd:3456: Fix on a work row Today lists posts /api/items/<id>/fix with the revision the Now read gave."""

    def setUp(self):
        super().setUp()
        from sd_db import workflow

        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.task = self.item("Write the brief", kind="task", status="in_progress")
        self.report = self.item("nightly: needs attention", kind="report", status="in_progress")
        jobs = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        self.doc = now_screen.document(self.connection, now=NOW, jobs=jobs,
                                       fleet=lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))
        self.revisions = {n: workflow.item_state(self.connection, n)["revision"] for n in (self.task, self.report)}

    def test_fix_asks_first_names_the_session_by_the_item_kind_and_posts_the_revision(self):
        out = self.run_page(f"""ANSWER = (path, body) => path.endsWith('/fix') ? [200, {{ revision: 'e'.repeat(64), fix: {{ session: 'builder', pane: 'w9:p1' }} }}] : [200, DOC0];
R.task = cmd('work item.fix').consequence(C.get('item:{self.task}')); R.report = cmd('work item.fix').consequence(C.get('item:{self.report}'));
R.on = cmd('work item.fix').when(C.get('item:{self.task}')) === true; // a listed work row is open, so Fix is always on
shellRun(cmd('work item.fix'), C.get('item:{self.task}')); await flush();""")
        self.assertIn(f"starts claude as a builder on #{self.task}", out["R"]["task"])
        self.assertIn(f"starts claude on report #{self.report}: the sd-debug skill", out["R"]["report"])
        self.assertTrue(out["R"]["on"])
        self.assertEqual(out["confirms"], ["work item.fix"])
        self.assertEqual(out["posts"], [[f"/api/items/{self.task}/fix", {"revision": self.revisions[self.task]}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], f"Fix started · #{self.task} · builder session in herdr pane w9:p1")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_a_stale_fix_reads_now_again_and_says_why(self):
        out = self.run_page("""ANSWER = (path, body) => path.endsWith('/fix') ? [409, { error: 'item changed; reload it before applying this change', reload: true }] : [200, DOC0];
shellRun(cmd('work item.fix'), C.get('item:""" + str(self.task) + """')); await flush();""")
        self.assertEqual(len(out["posts"]), 1)
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])
        self.assertIn("reload it", out["toasts"][-1]["msg"])


class TheSnooze(TheReader):
    """sd:1896: Snooze on every Today row, the snoozed rows apart with Unsnooze, and the fixed times."""

    def test_snooze_posts_the_row_and_reads_now_again(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { key: 'k', until: body.until }];
R.types = REG.filter(c => c.id.endsWith('.snooze')).map(c => c.on);
FIELD_VALUES.until = 'snooze-week'; shellRun(cmd('repo.snooze'), C.get('ahead:pushy:1')); await flush();
R.ahead = Date.parse(OUT.posts[0][1].until) - Date.now();""")
        self.assertEqual(out["R"]["types"], ["job", "pull request", "repo", "sessions", "collector", "work item"])
        # The row's own fingerprint goes with it, so the snooze holds only while the row reads the same.
        seen = next(row["seen"] for row in self.doc["rows"] if row["id"] == "ahead:pushy:1")
        self.assertEqual([(path, body["page"], body["row"], body["seen"]) for path, body, _ in out["posts"]],
                         [("/api/snooze", "today", "ahead:pushy:1", seen)])
        self.assertTrue(7 * 864e5 - 60e3 <= out["R"]["ahead"] <= 7 * 864e5, out["R"]["ahead"])
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_a_snoozed_row_is_drawn_apart_with_unsnooze(self):
        row = self.doc["rows"].pop()
        self.doc["snoozed"] = [{**row, "until": "2026-09-07T08:00:00+00:00"}]
        out = self.run_page("""R.rows = ELS.rows.html; R.snoozed = ELS.snoozed.html; R.obj = C.get('snoozed:ahead:pushy:1');
R.primary = REG.filter(c => c.on === 'snoozed row').map(c => c.id);""")
        self.assertNotIn("pushy", out["R"]["rows"])
        self.assertIn("Snoozed · 1", out["R"]["snoozed"])
        self.assertIn(row["what"], out["R"]["snoozed"])
        self.assertEqual(out["R"]["obj"], {"id": "snoozed:ahead:pushy:1", "type": "snoozed row", "label": row["what"],
                                           "row": "ahead:pushy:1", "until": "2026-09-07T08:00:00+00:00", "seen": row["seen"]})
        self.assertEqual(out["R"]["primary"], ["snoozed row.unsnooze"])

    ITEM = {"id": "item:7", "kind": "item", "item": 7, "what": "#7 Write the brief", "detail": "in_progress · due 2026-09-05",
            "state": "in_progress", "due": "2026-09-05", "seen": "0123456789abcdef"}

    def test_a_work_item_is_a_row_that_snoozes_under_its_own_key(self):
        """sd:3271: the items `sd today` lists get the same control as a Now row, keyed today:item:<id>."""
        self.doc["work"], self.doc["work_error"] = [self.ITEM, {**self.ITEM, "id": "item:9", "item": 9, "what": "#9 Later", "due": None,
                                                                 "detail": "in_progress"}], ""
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { key: 'k', until: body.until }];
R.work = ELS.work.html; R.tally = ELS['work-tally'].html; R.obj = C.get('item:7');
FIELD_VALUES.until = 'snooze-hour'; shellRun(cmd('work item.snooze'), C.get('item:7')); await flush();""")
        self.assertIn("Write the brief", out["R"]["work"])
        self.assertIn('<tr data-id="item:7" aria-selected="false">', out["R"]["work"])
        self.assertIn("in_progress · due 2026-09-05", out["R"]["work"])
        self.assertIn("1 overdue", out["R"]["tally"])
        self.assertEqual((out["R"]["obj"]["type"], out["R"]["obj"]["seen"]), ("work item", "0123456789abcdef"))
        self.assertEqual([(path, body["page"], body["row"], body["seen"]) for path, body, _ in out["posts"]],
                         [("/api/snooze", "today", "item:7", "0123456789abcdef")])
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    # A press on a row of a table: the click its tbody listens for, as the browser's closest() answers it.
    PRESS = "const press = (t, id) => ELS[t].listeners.click[0]({ target: { closest: s => s === 'tr[data-id]' ? { dataset: { id } } : null } });\n"
    SPY = "const SEL = []; C.select = id => SEL.push(id); R.panes = []; window.shell.openPane = p => R.panes.push(p);\n"

    def test_a_work_row_selects_into_the_details_as_a_now_row_does(self):
        """sd:5654: a press on a Work row selects it: ?row=, the shell's selection, its Details and the pane; a read keeps it."""
        self.doc["work"], self.doc["work_error"] = [{**self.ITEM, "item_kind": "task"}], ""
        out = self.run_page(self.PRESS + self.SPY + """press('work', 'item:7'); await flush();
R.details = ELS.details.html; R.row = location.search; R.sel = SEL.slice();
refresh(); await flush(); R.work = ELS.work.html; R.again = ELS.details.html; R.now = selected(); R.row2 = location.search;""")
        details = out["R"]["details"]
        self.assertIn('<h2><a href="/tasks?row=7">#7 Write the brief</a></h2>', details)
        for fact in ("Work item · overdue", "<dt>Status</dt><dd>in_progress</dd>", "<dt>Due</dt><dd>2026-09-05</dd>",
                     "<dt>Kind</dt><dd>task</dd>", "<dt>Id</dt><dd>sd:7</dd>", '<h3>Act</h3><div class="bar"></div>'):
            self.assertIn(fact, details)
        self.assertEqual((out["R"]["row"], out["R"]["sel"], out["R"]["panes"]), ("?row=item%3A7", ["item:7"], ["tab-details"]))
        # The read again keeps the Work row: no Now row takes the selection, and the redraw marks the row.
        self.assertIn('<tr data-id="item:7" aria-selected="true">', out["R"]["work"])
        self.assertIn("#7 Write the brief", out["R"]["again"])
        self.assertEqual((out["R"]["now"], out["R"]["row2"]), ([], "?row=item%3A7"))

    def test_one_selection_spans_now_and_work_rows(self):
        """sd:5654: Now and Work rows share today.js pick: selecting one unmarks the other. GitHub rows select on their own
        page since sd:5781 (test_v2_today_github)."""
        self.doc["work"], self.doc["work_error"] = [self.ITEM], ""
        out = self.run_page(self.PRESS + self.SPY + """
const sortWork = () => ELS['work-head'].listeners.click[0]({ target: { closest: s => s === 'button[data-sort]' ? { dataset: { sort: 'what' } } : null } });
press('rows', 'job:nightly-sync:7'); R.now = selected();
press('work', 'item:7'); sortWork(); R.now2 = selected(); R.work = ELS.work.html;
press('rows', 'job:nightly-sync:7'); sortWork(); R.work2 = ELS.work.html; R.sel = SEL.slice();""")
        self.assertEqual(out["R"]["now"], ["job:nightly-sync:7"])
        self.assertEqual(out["R"]["now2"], [])
        self.assertIn('<tr data-id="item:7" aria-selected="true">', out["R"]["work"])
        self.assertIn('<tr data-id="item:7" aria-selected="false">', out["R"]["work2"])
        self.assertEqual(out["R"]["sel"][-3:], ["job:nightly-sync:7", "item:7", "job:nightly-sync:7"])

    def test_a_work_read_that_failed_says_so_and_is_not_an_empty_list(self):
        self.doc["work"], self.doc["work_error"] = [], "database is locked"
        out = self.run_page("R.work = ELS.work.html;")
        self.assertIn("Work could not be read: database is locked", out["R"]["work"])
        self.assertNotIn("Nothing", out["R"]["work"])

    def test_no_work_item_says_so(self):
        self.doc["work"], self.doc["work_error"] = [], ""
        out = self.run_page("R.work = ELS.work.html;")
        self.assertIn("Nothing is due and nothing is in progress", out["R"]["work"])

    def test_a_snoozed_work_item_waits_under_snoozed_and_a_failed_read_retires_the_rows(self):
        self.doc["work"], self.doc["work_error"] = [], ""
        self.doc["snoozed"] = [{**self.ITEM, "until": "2026-09-07T08:00:00+00:00"}]
        out = self.run_page("""R.snoozed = ELS.snoozed.html; R.obj = C.get('snoozed:item:7');""")
        self.assertIn("#7 Write the brief", out["R"]["snoozed"])
        self.assertEqual((out["R"]["obj"]["type"], out["R"]["obj"]["row"], out["R"]["obj"]["seen"]),
                         ("snoozed row", "item:7", "0123456789abcdef"))
        self.doc["work"] = [self.ITEM]
        out = self.run_page("""ANSWER = () => [500, { error: 'fleet collection was stopped at its budget' }]; refresh(); await flush();
R.work = ELS.work.html; R.type = C.get('item:7').type;""")
        self.assertIn("Work could not be read", out["R"]["work"])
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_snooze_read_that_failed_is_a_partial_read(self):
        self.doc["snooze_error"] = "database is locked"
        out = self.run_page("")
        self.assertEqual(out["states"][-1], {"kind": "partial", "source": "/api/now",
                                             "text": "Snoozes were not read: database is locked. Every row shows."})

    def test_the_fixed_times_and_how_a_toast_says_them(self):
        out = self.run_page("""const S = window.snooze, at = (h, m) => new Date(2026, 9, 10, h, m);
const show = d => [d.getDate(), d.getHours(), d.getMinutes()];
R.early = show(S.CHOICES[0].until(at(7, 59))); R.late = show(S.CHOICES[0].until(at(8, 0)));
R.hour = show(S.CHOICES[1].until(at(23, 30)));
R.today = S.when(at(8, 0), at(7, 0)); R.other = S.when(new Date(2026, 9, 11, 8, 0), at(7, 0));
R.keys = S.CHOICES.map(c => [c.id, c.label]);""")
        self.assertEqual(out["R"]["early"], [10, 8, 0])
        self.assertEqual(out["R"]["late"], [11, 8, 0])
        self.assertEqual(out["R"]["hour"], [11, 0, 30])
        self.assertEqual((out["R"]["today"], out["R"]["other"]), ("08:00", "Sun Oct 11 08:00"))
        self.assertEqual(out["R"]["keys"], [["snooze", "The next 08:00"], ["snooze-hour", "One hour from now"],
                                            ["snooze-week", "One week from now"]])

    def test_snooze_is_one_command_per_row_that_asks_which_time(self):
        """sd:3450: one Snooze in the row's menu, key z, whose dialog offers the three times; the next 08:00 is the default."""
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { key: 'k', until: body.until }];
R.repo = REG.filter(c => c.on === 'repo').map(c => [c.id, c.label, c.key]);
R.fields = cmd('repo.snooze').fields(C.get('ahead:pushy:1'));
shellRun(cmd('repo.snooze'), C.get('ahead:pushy:1')); await flush();
R.until = new Date(OUT.posts[0][1].until); R.at8 = [R.until.getHours(), R.until.getMinutes()];
OUT.posts = []; FIELD_VALUES.until = 'snooze-hour'; const t0 = Date.now();
shellRun(cmd('repo.snooze'), C.get('ahead:pushy:1')); await flush();
R.hour = Date.parse(OUT.posts[0][1].until) - t0;""")
        # sd:5652: a repo row's other command is its Fix, on its own key.
        self.assertEqual(out["R"]["repo"], [["repo.fix", "Fix", "f"], ["repo.snooze", "Snooze", "z"]])
        self.assertEqual(out["R"]["fields"], [{"name": "until", "label": "Snooze until", "options": [
            {"value": "snooze", "label": "The next 08:00"}, {"value": "snooze-hour", "label": "One hour from now"},
            {"value": "snooze-week", "label": "One week from now"}]}])
        self.assertEqual(out["confirms"], ["repo.snooze", "repo.snooze"])
        self.assertEqual(out["R"]["at8"], [8, 0])
        self.assertTrue(3590e3 <= out["R"]["hour"] <= 3610e3, out["R"]["hour"])


DECISIONS_JS = (V2 / "static" / "decisions.js").read_text(encoding="utf-8")


class TheBadge(TheReader):
    """Today's badge (shell.attention) is the Now read's (sd:3446); a decision waiting counts on Decide's since sd:5781.

    decisions.js runs beside today.js here, as today.html loaded it until sd:5781, so a decision waits while Now reads:
    Today's badge stays the Now document's, and TheSort sorts the decisions table with the others. Decide's badge is
    TheDecideBadge."""

    DECISION = {"note": 7, "item": 3012, "title": "Dashboard v2", "repo": "/repos/system", "asked": "2026-10-08T21:00:00Z",
                "question": "Which shape?", "options": ["Option lines", "Free text"], "revision": "a" * 64}

    def run_badge(self, body, decisions, keep_job=False):
        self.doc["rows"] = [row for row in self.doc["rows"] if keep_job or row["kind"] != "job"]
        self.assertEqual([row["band"] for row in self.doc["rows"]], ["broken", "look"] if keep_job else ["look"])
        now = dict(self.doc, attention=now_screen._attention(self.doc["rows"]))
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + SHELL_MORE + READ_SHELL
                  # As shell.js does: a page passes its badge, or sets window.PAGE_ATTENTION and passes nothing.
                  + "\nwindow.shell.attention = a => { if (a) window.PAGE_ATTENTION = a; OUT.attention.push(window.PAGE_ATTENTION); };"
                  + f"\nconst DOC0 = {json.dumps(now)}, DECISIONS = {json.dumps({'decisions': decisions})};\n"
                  + "ANSWER = (path, body) => path === '/api/now' ? [200, DOC0]"
                    " : path === '/api/decisions' ? [200, DECISIONS]"
                    " : [200, { revision: 'b'.repeat(64), ruling: { id: 99, kind: 'decision', body: 'x' } }];\n"
                  + SNOOZE_JS + COMMANDS_JS + TODAY_JS + "\n" + DECISIONS_JS
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

    def test_a_waiting_decision_no_longer_counts_on_today(self):
        out = self.run_badge("click(7, 0); await flush();", [self.DECISION])
        self.assertEqual(out["posts"][0][0], "/api/decisions/answer")
        self.assertEqual({tuple(sorted(a)) for a in out["attention"]}, {("n", "state", "what")})
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "caution rows"})

    def test_an_unread_now_is_unknown_while_a_decision_waits(self):
        # sd:3446: the badge is the Now read's, so a failed read is unknown here as on every other page's rail.
        out = self.run_badge("ANSWER0 = ANSWER; ANSWER = (p, b) => p === '/api/now' ? [500, { error: 'stopped' }] : ANSWER0(p, b);"
                             " refresh(); await flush();", [self.DECISION])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})

    def test_a_warning_row_outranks_a_waiting_decision(self):
        """A guard: the badge counts its loudest state, and a decision is caution, as its ▲ in the section says."""
        out = self.run_badge("", [self.DECISION], keep_job=True)
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "warning rows"})


DECIDE_JS = (V2 / "static" / "decide.js").read_text(encoding="utf-8")


class TheDecideBadge(unittest.TestCase):
    """sd:5781: Decide's badge is what waits on its two tabs: the decisions still open and Scoring's open questions.

    decide.js and decisions.js run as decide.html loads them; review.js's part is its window.decideCount('scoring', n)
    call (test_v2_review runs review.js). An unread tab (n null) hides its count and adds nothing."""

    def run_page(self, body):
        decision = {**TheBadge.DECISION, "recommended": None}
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + "\nwindow.shell.attention = a => OUT.attention.push(a);"
                  + f"\nvar LIST = {json.dumps({'decisions': [decision, {**decision, 'note': 8}]})};\n"
                  + "ANSWER = (path, body) => path === '/api/decisions' ? [200, LIST]"
                    " : [200, { revision: 'b'.repeat(64), ruling: { id: 99, kind: 'decision', body: 'x' } }];\n"
                  + DECIDE_JS + "\n" + DECISIONS_JS
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

    def test_the_badge_adds_the_decisions_waiting_and_the_questions_to_score(self):
        out = self.run_page("R.read = OUT.attention.slice(-1)[0]; window.decideCount('scoring', 3); R.both = OUT.attention.slice(-1)[0];"
                            " click(7, 0); await flush(); R.ruled = OUT.attention.slice(-1)[0];"
                            " window.decideCount('scoring', null); R.unread = OUT.attention.slice(-1)[0];"
                            " click(8, 1); await flush(); R.none = OUT.attention.slice(-1)[0];")
        self.assertEqual(out["R"]["read"], {"state": "caution", "n": 2, "what": "decisions waiting"})
        self.assertEqual(out["R"]["both"], {"state": "caution", "n": 5, "what": "decisions waiting and questions open"})
        self.assertEqual(out["R"]["ruled"], {"state": "caution", "n": 4, "what": "decisions waiting and questions open"})
        self.assertEqual(out["R"]["unread"], {"state": "caution", "n": 1, "what": "decisions waiting"})
        self.assertEqual(out["R"]["none"], {"state": "ok", "n": 0, "what": "decisions or questions"})
        self.assertEqual((out["counts"]["decisions"], out["counts"]["scoring"]), (0, None))


class TheSort(TheReader):
    """sd:3455: each of Today's four tables has the shell's sort header; its order is kept in memory, not the address."""

    DECISION, ITEM, run_badge = TheBadge.DECISION, TheSnooze.ITEM, TheBadge.run_badge

    SORTED = r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"'

    def session(self, item, title, started, seconds):
        return {"item": item, "title": title, "kind": "worktree", "session": f"worktree sd{item}", "started": started,
                "elapsed": f"{seconds // 3600}h", "seconds": seconds, "via": "branch name", "verified": True}

    def test_each_table_sorts_by_its_header_and_the_selection_stays(self):
        self.doc["work"], self.doc["work_error"] = [self.ITEM,
                                                    {**self.ITEM, "id": "item:9", "item": 9, "what": "#9 Another", "due": None, "detail": "in_progress"}], ""
        self.doc["active"] = {"rows": [self.session(5, "Long", "2026-09-06T08:00:00Z", 7200), self.session(6, "Short", "2026-09-06T09:00:00Z", 3600)],
                              "sources": {}}
        other = {**self.DECISION, "note": 8, "item": 12, "title": "Another", "question": "All of it?"}
        click = "ELS['{h}'].listeners.click[0]({{ target: {{ closest: s => s === 'button[data-sort]' ? {{ dataset: {{ sort: '{k}' }} }} : null }} }});"
        body = ("R.heads = ['rows-head', 'active-head', 'work-head', 'decisions-head'].map(h => ELS[h].html); R.sel = selected();"
                + click.format(h="rows-head", k="what") + click.format(h="rows-head", k="what") + "R.now = ELS.rows.html; R.sel2 = selected();"
                + click.format(h="active-head", k="elapsed") + "R.active = ELS.active.html;"
                + click.format(h="work-head", k="what") + click.format(h="work-head", k="what") + "R.work = ELS.work.html;"
                + click.format(h="decisions-head", k="what") + "R.decisions = ELS.decisions.html; R.url = location.search;")
        out = self.run_badge(body, [self.DECISION, other], keep_job=True)
        heads = out["R"]["heads"]
        self.assertEqual([re.findall(r'data-sort="(\w+)"', h) for h in heads],
                         [["s", "what", "detail", "src"], ["s", "what", "started", "elapsed"], ["s", "what", "detail"], ["s", "what", "question"]])
        # Now and Working now start in the server's order and say so; Your work and Decisions keep theirs unmarked.
        self.assertEqual([re.findall(self.SORTED, h) for h in heads], [[("ascending", "s")], [("descending", "elapsed")], [], []])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["now"]), ["ahead:pushy:1", "job:nightly-sync:7"])
        self.assertNotIn("band-start", out["R"]["now"])
        self.assertEqual((out["R"]["sel"], out["R"]["sel2"]), (["job:nightly-sync:7"], ["job:nightly-sync:7"]))
        self.assertEqual(re.findall(r'<tr data-item="(\d+)"', out["R"]["active"]), ["6", "5"])
        self.assertEqual(re.findall(r'<tr data-id="item:(\d+)"', out["R"]["work"]), ["9", "7"])
        self.assertEqual(re.findall(r'<tr data-item="(\d+)"', out["R"]["decisions"]), ["12", "3012"])
        self.assertNotIn("sort=", out["R"]["url"])


class TheJobRow(TheReader):
    """sd:3449: a job row carries the commands Management and Activity give a job, from the one declaration in commands.js."""

    def test_a_job_row_shows_its_log_on_schedules_and_prints_its_launchd_record(self):
        out = self.run_page("""const o = C.get('job:nightly-sync:7'), log = cmd('jobs.log'), print = cmd('jobs.print');
R.got = [log.on, log.executes, log.cli(o), print.on, print.executes, print.when(o), print.cli(o), !!print.primary(o)];
window.shell.pages = { Management: '/management' }; R.said = log.run(o);""")
        self.assertEqual(out["R"]["got"], ["job", False, "local-cron-jobs/cron-jobs.sh logs nightly-sync",
                                           "job", False, True, "launchctl print fixture/nightly-sync", False])
        self.assertEqual(out["R"]["said"], "nightly-sync opens in Management > Schedules")
        # The address it opens is the one Activity's test reads (test_v2_activity): the same declaration builds it.
        self.assertEqual(out["posts"], [])

    def test_after_exit_127_print_is_the_rows_act(self):
        self.doc["rows"][0]["exit"] = 127
        out = self.run_page("R.print = !!cmd('jobs.print').primary(C.get('job:nightly-sync:7'));")
        self.assertTrue(out["R"]["print"])


class TheRetry(TheReader):
    """sd:3386: a failed job's Retry posts /api/jobs/<job>/retry with the row's revision, as Management and Activity do."""

    def test_retry_posts_the_job_route_with_the_rows_revision_and_reads_now_again(self):
        row = self.doc["rows"][0]
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' } }];
R.primary = cmd('jobs.retry').primary(C.get('job:nightly-sync:7'));
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();
R.cli = cmd('jobs.retry').cli(C.get('job:nightly-sync:7')); R.sends = cmd('jobs.retry').sends(C.get('job:nightly-sync:7'));""")
        self.assertEqual(out["posts"], [["/api/jobs/nightly-sync/retry", {"revision": row["revision"]}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "Retry started · nightly-sync")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])
        self.assertEqual((out["R"]["primary"], out["R"]["cli"], out["R"]["sends"]),
                         (True, "sd jobs retry nightly-sync", "launchctl kickstart fixture/nightly-sync"))

    def test_exit_127_keeps_retry_on_and_asks_first_naming_the_exit(self):
        """sd:3392, ruled 2026-09-30: the exit is the last run's, and a path fixed outside the dashboard needs Retry to test it."""
        self.doc["rows"][0]["exit"] = 127
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' } }];
const c = cmd('jobs.retry'), o = C.get('job:nightly-sync:7');
R.on = c.when(o); R.primary = !!c.primary(o); R.ask = !!c.askFirst(o); R.says = c.consequence(o);
shellRun(c, o); await flush();""")
        self.assertEqual((out["R"]["on"], out["R"]["primary"], out["R"]["ask"]), (True, False, True))
        self.assertIn("exit 127", out["R"]["says"])
        self.assertEqual(out["confirms"], ["jobs.retry"])
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/jobs/nightly-sync/retry"])
        self.assertEqual(out["toasts"][-1]["msg"], "Retry started · nightly-sync")

    def test_another_exit_retries_without_asking(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' } }];
R.ask = !!cmd('jobs.retry').askFirst(C.get('job:nightly-sync:7')); shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
        self.assertEqual((out["R"]["ask"], out["confirms"]), (False, []))

    def test_a_retry_the_read_refuses_is_off_with_the_reason(self):
        self.doc["rows"][0]["capability"] = {"allowed": False, "reason": "an unresolved restore prevents starting jobs"}
        out = self.run_page("shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1]["msg"], "off: an unresolved restore prevents starting jobs")

    def test_a_stale_retry_writes_nothing_says_so_and_reads_now_again(self):
        out = self.run_page("""ANSWER = path => path === '/api/now' ? [200, DOC0] : [409, { error: 'state changed; refresh before trying again' }];
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
        self.assertEqual(len(out["posts"]), 1)
        self.assertEqual(out["toasts"][-1]["msg"], "nightly-sync failed with exit 7 not changed: state changed; refresh before trying again")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_a_retry_with_no_answer_is_unknown_not_refused(self):
        out = self.run_page("""ANSWER = path => path === '/api/now' ? [200, DOC0] : Promise.reject(new TypeError('Load failed'));
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
        self.assertEqual(out["toasts"][-1]["msg"], "nightly-sync failed with exit 7 not changed: no answer (Load failed), so whether"
                         " launchd started it is not known; Now is read again")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_a_retry_shows_launchctls_answer_when_it_did_not_accept(self):
        """sd:3386, as sd:3388 words Management's: a 200 carries launchctl's answer; only `accepted` started a retry."""
        cases = {
            "{ request: { status: 'failed', message: 'launchctl refused the request (exit 3)' } }":
                "nightly-sync not retried: launchctl refused the request (exit 3)",
            "{ request: { status: 'unknown', message: 'launchctl timed out; the request outcome is unknown' } }":
                "Not known whether nightly-sync was retried: launchctl timed out; the request outcome is unknown",
            "{}": "Not known whether nightly-sync was retried: the answer named no outcome",
        }
        for answer, said in cases.items():
            with self.subTest(said=said):
                out = self.run_page(f"""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, {answer}];
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
                self.assertEqual(out["toasts"][-1]["msg"], said)
                self.assertEqual(out["gets"], ["/api/now", "/api/now"])


# The row's act, as shell.js's rowActions picks it: the first live command on the row's type that is primary for it.
ACT = """const act = id => { const o = C.get(id), on = REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true));
  const p = on.find(c => c.primary && c.primary(o)); return p ? [p.id, p.label, p.icon] : null; };
window.open = u => { OUT.opened = u; };
const PR = { rank: 4, kind: 'pr', id: 'pr:example/project#4:4', what: 'example/project#4 open', detail: 'Pull 4', source: 'prs', band: 'queued' };
const DARK = s => ({ rank: 0, kind: 'dark', id: `dark:${s}`, what: `${s} could not be read`, detail: 'no reason', source: s, band: 'broken' });
"""


class TheNowFix(TheReader):
    """sd:5652: every Now row has a Fix, its act where the kind has a route, routed to what already addresses the kind;
    a row with no route has Fix off with the reason."""

    def test_fix_is_each_routed_rows_act(self):
        out = self.run_page(ACT + """DOC0.rows.push(PR, DARK('prs'), DARK('jobs')); refresh(); await flush();
R.acts = ['job:nightly-sync:7', 'ahead:pushy:1', 'pr:example/project#4:4', 'dark:prs', 'dark:jobs'].map(act);""")
        self.assertEqual(out["R"]["acts"], [["job.fix", "Fix", "play"], ["repo.fix", "Fix", "bot"],
                                            ["pull request.fix", "Fix", "arrow-up-right"], ["collector.fix", "Fix", "bot"],
                                            ["collector.fix", "Fix", "bot"]])

    def test_fix_on_a_failed_job_is_its_run_now(self):
        row = self.doc["rows"][0]
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' } }];
const c = cmd('job.fix'), o = C.get('job:nightly-sync:7'); R.line = [c.cli(o), c.sends(o), c.key];
shellRun(c, o); await flush();""")
        self.assertEqual(out["posts"], [["/api/jobs/nightly-sync/retry", {"revision": row["revision"]}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "Retry started · nightly-sync")
        self.assertEqual(out["R"]["line"], ["sd jobs retry nightly-sync", "launchctl kickstart fixture/nightly-sync", "f"])

    def test_after_exit_127_fix_asks_first_and_print_stays_the_act(self):
        self.doc["rows"][0]["exit"] = 127
        out = self.run_page(ACT + """ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'ok' } }];
R.act = act('job:nightly-sync:7'); R.says = cmd('job.fix').consequence(C.get('job:nightly-sync:7'));
shellRun(cmd('job.fix'), C.get('job:nightly-sync:7')); await flush();""")
        self.assertEqual(out["R"]["act"], ["jobs.print", "Print", "terminal"])
        self.assertIn("exit 127", out["R"]["says"])
        self.assertEqual(out["confirms"], ["job.fix"])

    def test_a_job_with_no_route_has_fix_off_with_the_reason(self):
        self.doc["rows"][0].update(state="unloaded", retry=None, capability=None)
        out = self.run_page(ACT + "R.act = act('job:nightly-sync:7'); shellRun(cmd('job.fix'), C.get('job:nightly-sync:7')); await flush();")
        self.assertNotEqual((out["R"]["act"] or [None])[0], "job.fix")
        self.assertEqual(out["toasts"][-1]["msg"], "off: no failed run to retry")
        self.assertEqual(out["posts"], [])

    def test_fix_on_a_pull_request_opens_it_on_github(self):
        out = self.run_page(ACT + """DOC0.rows.push(PR); refresh(); await flush();
shellRun(cmd('pull request.fix'), C.get('pr:example/project#4:4')); await flush();""")
        self.assertEqual(out["opened"], "https://github.com/example/project/pull/4")
        self.assertEqual(out["toasts"][-1]["msg"], "Opened on GitHub in a new tab")

    def test_fix_on_the_dark_pull_request_collector_runs_its_sync(self):
        out = self.run_page(ACT + """ANSWER = (path, body) => path === '/api/shadow/state' ? [200, { running: false, trackers: [] }] : path === '/api/now' ? [200, DOC0] : [202, { started: true }];
DOC0.rows.push(DARK('prs')); refresh(); await flush();
shellRun(cmd('collector.fix'), C.get('dark:prs')); await flush();""")
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/shadow/sync"])
        self.assertIn("/api/shadow/state", out["gets"])
        self.assertEqual(out["toasts"][-1]["msg"], "Shadow sync started · Today reads again when it ends")

    def test_fix_on_a_repo_row_asks_first_and_posts_the_row_for_a_herdr_session(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now/fix' ? [200, { fix: { pane: 'w9:p1', checkout: '/repos/pushy', workspace: 'fix-ahead-pushy' } }] : [200, DOC0];
const c = cmd('repo.fix'), o = C.get('ahead:pushy:1'); R.says = c.consequence(o);
shellRun(c, o); await flush();""")
        self.assertEqual(out["confirms"], ["repo.fix"])
        self.assertIn("herdr", out["R"]["says"])
        self.assertEqual(out["posts"], [["/api/now/fix", {"row": "ahead:pushy:1"}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "Fix started · pushy has 1 unpushed commit · claude in herdr workspace fix-ahead-pushy, pane w9:p1")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_fix_on_another_dark_collector_posts_the_row_and_a_stale_one_reads_now_again(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now/fix' ? [409, { error: 'the jobs collector reads now; read Now again', reload: true }] : [200, DOC0];
DOC0.rows.push({ rank: 0, kind: 'dark', id: 'dark:jobs', what: 'jobs could not be read', detail: 'no reason', source: 'jobs', band: 'broken' });
refresh(); await flush(); shellRun(cmd('collector.fix'), C.get('dark:jobs')); await flush();""")
        self.assertEqual(out["posts"], [["/api/now/fix", {"row": "dark:jobs"}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "jobs could not be read not changed: the jobs collector reads now; read Now again")
        self.assertEqual(out["gets"][-1], "/api/now")
