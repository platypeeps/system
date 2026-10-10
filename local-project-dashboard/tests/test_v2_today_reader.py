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
SNOOZE_JS = (V2 / "static" / "snooze.js").read_text(encoding="utf-8")
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
                  + "ANSWER = () => [200, DOC0];\n" + SNOOZE_JS + TODAY_JS
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



class TheSnooze(TheReader):
    """sd:1896: Snooze on every Today row, the snoozed rows apart with Unsnooze, and the fixed times."""

    def test_snooze_posts_the_row_and_reads_now_again(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { key: 'k', until: body.until }];
R.types = REG.filter(c => c.id.endsWith('.snooze')).map(c => c.on);
shellRun(cmd('repo.snooze-week'), C.get('ahead:pushy:1')); await flush();
R.ahead = Date.parse(OUT.posts[0][1].until) - Date.now();""")
        self.assertEqual(out["R"]["types"], ["job", "pull request", "repo", "sessions", "collector"])
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
R.keys = S.CHOICES.map(c => [c.id, c.label, c.key]);""")
        self.assertEqual(out["R"]["early"], [10, 8, 0])
        self.assertEqual(out["R"]["late"], [11, 8, 0])
        self.assertEqual(out["R"]["hour"], [11, 0, 30])
        self.assertEqual((out["R"]["today"], out["R"]["other"]), ("08:00", "Sun Oct 11 08:00"))
        self.assertEqual(out["R"]["keys"], [["snooze", "Snooze until 08:00", "z"], ["snooze-hour", "Snooze 1 hour", "h"],
                                            ["snooze-week", "Snooze 1 week", "w"]])


DECISIONS_JS = (V2 / "static" / "decisions.js").read_text(encoding="utf-8")


class TheBadge(TheReader):
    """Today's badge (shell.attention) counts the decisions waiting beside the Now rows (sd:3012).

    today.js and decisions.js run together, as today.html loads them; /api/decisions answers with one decision, and the
    Now document keeps only its caution row, so the badge is caution and its count is the row plus the decision."""

    DECISION = {"note": 7, "item": 3012, "title": "Dashboard v2", "repo": "/repos/system", "asked": "2026-10-08T21:00:00Z",
                "question": "Which shape?", "options": ["Option lines", "Free text"], "revision": "a" * 64}

    def run_badge(self, body, decisions, keep_job=False):
        self.doc["rows"] = [row for row in self.doc["rows"] if keep_job or row["kind"] != "job"]
        self.assertEqual([row["band"] for row in self.doc["rows"]], ["broken", "look"] if keep_job else ["look"])
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + SHELL_MORE + READ_SHELL
                  # As shell.js does: a page passes its badge, or sets window.PAGE_ATTENTION and passes nothing.
                  + "\nwindow.shell.attention = a => { if (a) window.PAGE_ATTENTION = a; OUT.attention.push(window.PAGE_ATTENTION); };"
                  + f"\nconst DOC0 = {json.dumps(self.doc)}, DECISIONS = {json.dumps({'decisions': decisions})};\n"
                  + "ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : path === '/api/decisions' ? [200, DECISIONS]"
                    " : [200, { revision: 'b'.repeat(64), ruling: { id: 99, kind: 'decision', body: 'x' } }];\n"
                  + SNOOZE_JS + TODAY_JS + "\n" + DECISIONS_JS
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

    def test_a_waiting_decision_counts_in_the_badge(self):
        out = self.run_badge("", [self.DECISION])
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 2, "what": "caution rows and decisions waiting"})

    def test_an_answered_decision_stops_counting(self):
        out = self.run_badge("R.before = OUT.attention[OUT.attention.length - 1]; click(7, 0); await flush();", [self.DECISION])
        self.assertEqual(out["posts"][0][0], "/api/decisions/answer")
        self.assertEqual(out["R"]["before"]["n"], 2)
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "caution rows"})

    def test_a_resolved_decision_the_read_no_longer_lists_counts_nothing(self):
        out = self.run_badge("R.before = OUT.attention[OUT.attention.length - 1]; DECISIONS.decisions = [];"
                             " ELS.refresh.listeners.click[0](); await flush();", [self.DECISION])
        self.assertEqual(out["R"]["before"]["n"], 2)
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "caution rows"})

    def test_a_decision_waiting_while_now_is_unread_still_lights_the_badge(self):
        out = self.run_badge("ANSWER0 = ANSWER; ANSWER = (p, b) => p === '/api/now' ? [500, { error: 'stopped' }] : ANSWER0(p, b);"
                             " refresh(); await flush();", [self.DECISION])
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "decisions waiting"})

    def test_a_warning_row_outranks_a_waiting_decision(self):
        """A guard: the badge counts its loudest state, and a decision is caution, as its ▲ in the section says."""
        out = self.run_badge("", [self.DECISION], keep_job=True)
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "warning rows"})
