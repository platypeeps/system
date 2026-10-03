"""The v2 shell's reader (sd:2418).

What this slice promises: `read.js` is the one way a page reads its document.
A read that is not the newest changes nothing; each read retires the command
objects and picks of rows it no longer lists; a selection whose row is gone
clears; a failed read clears the page; a failed reread after a write keeps the
rows, retires their objects and says the write landed; and a reread never
answers with a document read before its write landed.

`read.js` runs under JavaScriptCore (osascript) with stand-in fetch, commands,
state and row. `READ_SHELL` wires it into the page stand-in from the lines
`shell.js` itself runs, so a page test exercises the real reader.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from sd_dashboard import v2

from test_v2_today import OSASCRIPT

V2 = Path(v2.__file__).resolve().parent
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
READ_PATH = V2 / "static" / "read.js"
READ_JS = READ_PATH.read_text(encoding="utf-8") if READ_PATH.exists() else ""
WIRING = re.search(r"^  // read:start\n(.*?)^  // read:end$", SHELL_JS, re.S | re.M)

#: The reader in a page stand-in: read.js, then shell.js's own wiring lines, run against the stand-in's shell.
READ_SHELL = READ_JS + """
{ const commands = window.shell.commands, state = (...a) => window.shell.state(...a),
    rowParam = () => (window.shell.row ? window.shell.row() : null);
""" + (WIRING.group(1) if WIRING else "") + """
  window.shell.read = read; }
"""

#: A stand-in for the shell the reader is given: fetch answers from held promises, commands keep objects and picks as
#: shell.js does (a pick needs a bulk command on the object's type), and every call is recorded in OUT.
STAND_IN = r"""
var window = globalThis, OUT = { gets: [], states: [], selects: [], adopted: [], draws: 0, clears: [], shown: [], unselected: 0, error: null };
var LISTENERS = {}, HELD = [];
var document = { addEventListener(t, f) { (LISTENERS[t] = LISTENERS[t] || []).push(f); } };
function fetch(path) { OUT.gets.push(path); return new Promise((ok, fail) => HELD.push({ ok: (status, body) => ok({ ok: status < 300, status, json: () => Promise.resolve(body) }), fail })); }
var REG = [{ id: 'row.act', on: 'row', bulk: true }], OBJ = new Map(), PICKED = new Set();
var C = { put: o => { OBJ.set(o.id, o); return o; }, get: id => OBJ.get(id), select: id => OUT.selects.push(id),
  pick: id => { const o = OBJ.get(id); if (!o || !REG.some(c => c.on === o.type && c.bulk)) return; PICKED.has(id) ? PICKED.delete(id) : PICKED.add(id);
    (LISTENERS['shell:picked'] || []).forEach(f => f({ detail: [...PICKED] })); } };
var live = id => !!OBJ.get(id) && REG.some(c => c.on === OBJ.get(id).type);
var ROW = null;
var make = () => window.SHELL_READ({ fetch: (...a) => fetch(...a), commands: C, state: s => OUT.states.push(s), row: () => ROW,
  listen: (t, f) => document.addEventListener(t, f) });
var ROWS = [], SELECTED = null;
var spec = {
  source: '/api/rows', what: 'the rows',
  adopt: doc => { OUT.adopted.push(doc.n); ROWS = doc.rows;
    return { objects: ROWS.map(id => ({ id, type: 'row', label: 'row ' + id })), state: ROWS.length ? null : { kind: 'empty', text: 'No rows.' } }; },
  clear: err => { OUT.clears.push(err.message); ROWS = []; },
  draw: () => { OUT.draws++; OUT.shown = ROWS.slice(); },
  current: () => SELECTED,
  first: () => ROWS[0],
  select: (id, opened) => { SELECTED = id; OUT.selects.push(id); },
  unselect: () => { SELECTED = null; OUT.unselected++; },
};
const flush = async () => { for (let i = 0; i < 200; i++) await null; };
const doc = (n, rows) => ({ n, rows });
"""


def run(body):
    script = (STAND_IN + READ_JS + "\nvar R = {};\n(async () => { try {\n" + body
              + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
              + "function run() { OUT.R = R; OUT.picked = [...PICKED]; return JSON.stringify(OUT); }\n")
    result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    out = json.loads(result.stdout)
    if out["error"]:
        raise AssertionError(out["error"])
    return out


class TheReader(unittest.TestCase):
    def test_an_older_answer_that_arrives_last_is_dropped(self):
        out = run("""const r = make()(spec);
r.load(); r.load(); await flush();
HELD[1].ok(200, doc(2, ['b'])); await flush();
HELD[0].ok(200, doc(1, ['a'])); await flush();
R.selected = SELECTED;""")
        self.assertEqual(out["gets"], ["/api/rows", "/api/rows"])
        self.assertEqual(out["adopted"], [2], "the older answer was adopted after the newer one")
        self.assertEqual(out["draws"], 1)
        self.assertEqual(out["shown"], ["b"])
        self.assertEqual(out["R"]["selected"], "b")

    def test_an_older_failure_after_a_newer_success_changes_nothing(self):
        out = run("""const r = make()(spec);
r.load(); r.load(); await flush();
HELD[1].ok(200, doc(2, ['b'])); await flush();
HELD[0].ok(500, { error: 'an older read that failed' }); await flush();
R.live = live('b');""")
        self.assertEqual(out["clears"], [], "an older failure cleared the newer rows")
        self.assertNotIn("an older read that failed", json.dumps(out["states"]))
        self.assertIsNone(out["states"][-1])
        self.assertTrue(out["R"]["live"])

    def test_an_object_the_next_read_drops_is_retired_with_its_pick(self):
        out = run("""const r = make()(spec);
r.load(); await flush(); HELD[0].ok(200, doc(1, ['a', 'b'])); await flush();
C.pick('a'); C.pick('b');
r.load(); await flush(); HELD[1].ok(200, doc(2, ['b'])); await flush();
R.a = [live('a'), OBJ.get('a').type, OBJ.get('a').label]; R.b = live('b');""")
        self.assertEqual(out["R"]["a"], [False, "not listed", "row a (no longer listed)"], "a dropped row kept its command object")
        self.assertEqual(out["picked"], ["b"], "a dropped row kept its pick, or a kept row lost its pick")
        self.assertTrue(out["R"]["b"])

    def test_a_selection_the_next_read_drops_clears(self):
        out = run("""const r = make()(spec);
r.load(); await flush(); HELD[0].ok(200, doc(1, ['a'])); await flush();
R.before = SELECTED; OUT.selects = [];
r.load(); await flush(); HELD[1].ok(200, doc(2, [])); await flush();
R.after = SELECTED;""")
        self.assertEqual(out["R"]["before"], "a")
        self.assertIsNone(out["R"]["after"], "the page kept a selection whose row is gone")
        self.assertEqual(out["selects"], [None], "the shell's selection was not cleared")
        self.assertEqual(out["unselected"], 1, "Details were not reset to the nothing-selected text")
        self.assertEqual(out["states"][-1]["kind"], "empty")

    def test_a_kept_selection_stays_and_a_gone_one_moves_to_the_first_row(self):
        out = run("""const r = make()(spec);
r.load(); await flush(); HELD[0].ok(200, doc(1, ['a', 'b'])); await flush();
SELECTED = 'b'; OUT.selects = [];
r.load(); await flush(); HELD[1].ok(200, doc(2, ['a', 'b'])); await flush();
R.kept = SELECTED;
r.load(); await flush(); HELD[2].ok(200, doc(3, ['c', 'a'])); await flush();
R.moved = SELECTED;""")
        self.assertEqual(out["R"]["kept"], "b")
        self.assertEqual(out["R"]["moved"], "c")

    def test_the_first_read_opens_the_address_row(self):
        out = run("""ROW = 'b'; const opened = [];
const r = make()({ ...spec, select: (id, open) => { SELECTED = id; opened.push([id, open]); } });
r.load(); await flush(); HELD[0].ok(200, doc(1, ['a', 'b'])); await flush();
r.load(); await flush(); HELD[1].ok(200, doc(2, ['a', 'b'])); await flush();
R.opened = opened;""")
        self.assertEqual(out["R"]["opened"], [["b", True], ["b", False]])

    def test_a_failed_read_clears_the_rows_the_objects_and_the_selection(self):
        out = run("""const r = make()(spec);
r.load(); await flush(); HELD[0].ok(200, doc(1, ['a'])); await flush();
C.pick('a'); OUT.selects = [];
r.load(); await flush(); HELD[1].ok(500, { error: 'boom' }); await flush();
R.live = live('a'); R.selected = SELECTED;""")
        self.assertEqual(out["clears"], ["boom"])
        self.assertEqual(out["shown"], [])
        self.assertFalse(out["R"]["live"], "a failed read left a command object live")
        self.assertEqual(out["picked"], [])
        self.assertIsNone(out["R"]["selected"])
        self.assertEqual(out["selects"], [None])
        self.assertEqual(out["states"][-1], {"kind": "error", "text": "The rows were not read: boom. Reload retries it.",
                                             "source": "/api/rows"})

    def test_a_failed_reread_keeps_the_rows_runs_nothing_on_them_and_says_the_change_landed(self):
        out = run("""const r = make()(spec);
r.load(); await flush(); HELD[0].ok(200, doc(1, ['a', 'b'])); await flush();
C.pick('a');
const p = r.reread(); await flush(); HELD[1].fail(new Error('the server went away')); R.result = await p;
R.live = [live('a'), live('b')]; R.selected = SELECTED;""")
        self.assertEqual(out["clears"], [], "a failed reread cleared the rows")
        self.assertEqual(out["shown"], ["a", "b"], "a failed reread took the rows off the screen")
        self.assertEqual(out["R"]["live"], [False, False], "a failed reread left a command object live")
        self.assertEqual(out["picked"], [])
        self.assertIsNone(out["R"]["selected"])
        self.assertFalse(out["R"]["result"])
        last = out["states"][-1]
        self.assertEqual(last["kind"], "partial")
        self.assertIn("The change landed", last["text"])
        self.assertIn("the server went away", last["text"])
        self.assertNotIn("not changed", last["text"])

    def test_a_reread_waits_for_a_read_that_starts_after_it(self):
        # PR 73, finding r4167523338: a bulk write's first POST to land started the shared reread while later POSTs were
        # still out, so every command completed on a document read before some writes landed.
        out = run("""const r = make()(spec), done = [];
r.load(); await flush();
r.reread().then(v => done.push(['first', v])); await flush();
R.during = OUT.gets.length;
HELD[0].ok(200, doc(1, ['a'])); await flush();
R.after = OUT.gets.length; R.early = done.length;
HELD[1].ok(200, doc(2, ['a'])); await flush();
R.done = done;""")
        self.assertEqual(out["R"]["during"], 1, "a reread joined, or raced, a read that started before its write landed")
        self.assertEqual(out["R"]["after"], 2)
        self.assertEqual(out["R"]["early"], 0, "the reread settled on the read that started before it")
        self.assertEqual(out["R"]["done"], [["first", True]])
        self.assertEqual(out["adopted"], [1, 2])

    def test_rereads_before_a_queued_read_starts_share_it(self):
        out = run("""const r = make()(spec), done = [];
r.reread(); await flush();
r.reread().then(() => done.push(2)); r.reread().then(() => done.push(3)); r.reread().then(() => done.push(4)); await flush();
HELD[0].ok(200, doc(1, ['a'])); await flush();
R.gets = OUT.gets.length; HELD[1].ok(200, doc(2, ['a'])); await flush();
R.done = done;""")
        self.assertEqual(out["R"]["gets"], 2, "three writes that landed during one read did not share the next one")
        self.assertEqual(out["R"]["done"], [2, 3, 4])
        self.assertEqual(len(out["gets"]), 2)

    def test_a_stale_reread_failure_says_nothing(self):
        out = run("""const r = make()(spec);
r.load(); await flush(); HELD[0].ok(200, doc(1, ['a'])); await flush();
r.reread(); await flush(); r.load(); await flush();
HELD[2].ok(200, doc(3, ['a'])); await flush();
HELD[1].fail(new Error('late')); await flush();
R.live = live('a');""")
        self.assertNotIn("late", json.dumps(out["states"]))
        self.assertTrue(out["R"]["live"])

    def test_a_refused_answer_names_its_error_and_a_bodiless_one_its_status(self):
        out = run("""const r = make()(spec);
r.load(); await flush(); HELD[0].ok(403, { error: 'Open a dashboard page before reading Rows.' }); await flush();
R.first = OUT.states[OUT.states.length - 1].text;
r.load(); await flush(); HELD[1].ok(502, null); await flush();""")
        self.assertIn("Open a dashboard page before reading Rows.", out["R"]["first"])
        self.assertIn("HTTP 502", out["states"][-1]["text"])
        self.assertEqual(out["states"][0], {"kind": "loading", "text": "Reading the rows. Rows appear when /api/rows answers.",
                                            "source": "/api/rows"})


class TheShellWiring(unittest.TestCase):
    def test_shell_js_builds_shell_read_from_read_js_with_a_build_mark(self):
        self.assertIsNotNone(WIRING, "shell.js has no read:start ... read:end block")
        self.assertIn("build: (sd:2418)", WIRING.group(1))
        self.assertIn("window.SHELL_READ", WIRING.group(1))
        self.assertRegex(SHELL_JS, r"window\.shell = \{[^\n]*\bread\b")

    def test_the_reader_has_no_sink_and_draws_nothing_itself(self):
        self.assertTrue(READ_JS, "read.js does not exist")
        self.assertNotIn("innerHTML", READ_JS)
        self.assertNotRegex(READ_JS, r"(?<![.\w])put\(|getElementById|querySelector|window\.markup")


if __name__ == "__main__":
    unittest.main()
