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
from pathlib import Path

from sd_dashboard import now_screen, v2

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document, repo
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT

V2 = Path(v2.__file__).resolve().parent
TODAY_JS = (V2 / "static" / "today.js").read_text(encoding="utf-8")
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
RECONCILE = re.search(r"^  function reconcile\(.*?^  }$", SHELL_JS, re.S | re.M).group(0)

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
      e.dataset = { id: m[1], src: m[2] }; e.textContent = m[1]; e.getClientRects = () => e.hidden ? [] : [1];
      e.setAttribute = (k, v) => { attrs[k] = String(v); }; e.getAttribute = k => k in attrs ? attrs[k] : null; return e; }); }
  return ROW_CACHE.els; };
"""
SHELL_MORE = r"""
window.shell.row = () => new URLSearchParams(location.search).get('row');
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

    def run_page(self, body, search=""):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + SHELL_MORE + READ_SHELL
                  + f"\nconst DOC0 = {json.dumps(self.doc)};\nlocation.search = {json.dumps(search)};\n"
                  + "ANSWER = () => [200, DOC0];\n" + TODAY_JS
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
