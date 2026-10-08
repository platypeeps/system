"""The v2 Tasks page (sd:2124).

What this slice promises: `/tasks` answers under the shared policy and loads
its script before `shell.js`; `/api/tasks` is the row set v1 /backlog reads
(`reads.backlog_items`), each row carrying the revision a write sends back and
the statuses the library allows; `/api/tasks/<id>` is the `sd task show
--json` reading split into the Details sections (status history, notes,
assignments, artifact) plus the external context v1's item page shows.

`tasks.js` runs under JavaScriptCore (osascript, as `test_v2_today` runs
markup.js) against a stand-in page, with the documents above as its fetch
answers. The stand-in shell mirrors `shell.js`'s run contract: a `confirm`
command asks first, a `null` return means the page toasts on its own, and a
string return is toasted by the shell. The browser half -- drag, focus, the
look at 375 px -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import quote, urlsplit
from unittest.mock import patch

from sd_db import connect, reads, set_item_fields, upsert_repo, workflow
from sd_dashboard import server, tasks_screen, v2

from support import NOW, ScreenCase, transition, upsert_shadow
from test_v2_read import READ_SHELL
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
TASKS_JS = (V2 / "static" / "tasks.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
# The shell's run contract for writes, as shell.js has it: the stand-in runs these, not a copy.
SETTLE_JS = re.search(r"^  // bulk:start\n(.*?)^  // bulk:end$", SHELL_JS, re.S | re.M).group(1)
# The confirm dialog with its fields, as shell.js has it (sd:2200).
CONFIRM = re.search(r"^  // confirm:start\n(.*?)^  // confirm:end$", SHELL_JS, re.S | re.M)
# The page's reads and writes, as shell.js has them (sd:2588): every page posts through shell.post and reads through shell.getJSON.
FETCH_JS = re.search(r"^  // fetch:start\n(.*?)^  // fetch:end$", SHELL_JS, re.S | re.M).group(1)
# The list grammar, as shell.js has it (sd:2527, sd:2528, sd:2529): sort header, pager and filter chips.
LIST_JS = re.search(r"^  // list:start\n(.*?)^  // list:end$", SHELL_JS, re.S | re.M).group(1)


def seed(case):
    """Three open items and a week-old done one, on the fixture database."""
    repo = case.repo()
    plan = case.item("Plan the review", kind="task", priority=2, due="2026-09-09", repo=repo)
    ask = case.item("Answer the question", kind="followup", priority=3)
    port = case.item("Port the page", kind="work", repo=repo, path="docs/work/port/prd.md")
    old = case.item("Finished long ago", kind="task")
    workflow.change_status(case.connection, old, "done", who="test")
    case.connection.execute("UPDATE note SET timestamp = '2026-08-01T00:00:00Z' WHERE item = ?", (old,))
    case.connection.commit()
    case.note(plan, "Check the budget first")
    case.note(plan, "A plain comment", kind="comment")
    workflow.change_status(case.connection, plan, "ready", who="test")
    case.assignment(port, status="running")
    set_item_fields(case.connection, plan, source="github", external_id="https://example.invalid/i/1")
    upsert_shadow(case.connection, tracker="github", url="https://example.invalid/i/1", state="open", title="an issue")
    return {"plan": plan, "ask": ask, "port": port, "old": old}


class TheDocuments(ScreenCase):
    """`tasks_screen` builds both documents from library reads."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)

    def test_each_row_pairs_its_fields_with_the_revision_of_the_same_read(self):
        # An edit commits through a second connection after the row read and before the revisions are read. One snapshot
        # holds both, so the row shows the old title with the old revision; the edit's revision would let a write chosen
        # from the old title pass (review, PR #46).
        ask = self.ids["ask"]
        old = workflow.item_state(self.connection, ask)["revision"]
        writer = connect(self.path)
        self.addCleanup(writer.close)
        real = reads.backlog_items

        def rows_then_edit(connection, **arguments):
            rows = real(connection, **arguments)
            set_item_fields(writer, ask, title="Edited elsewhere")
            writer.commit()
            return rows

        with patch.object(reads, "backlog_items", rows_then_edit):
            doc = tasks_screen.document(self.connection, now=NOW)
        row = next(r for r in doc["rows"] if r["id"] == ask)
        self.assertEqual((row["title"], row["revision"]), ("Answer the question", old))
        self.assertFalse(self.connection.in_transaction)
        self.assertNotEqual(next(r for r in tasks_screen.document(self.connection, now=NOW)["rows"] if r["id"] == ask)["revision"], old)

    def test_each_row_says_whether_edit_item_takes_its_fields_and_why_not(self):
        # The seeded work row's repository keeps the schema default, status_source `file`, so edit_item refuses it; a work
        # row in a repository the database owns is editable. Each row's `edit` is checked against edit_item itself
        # (review, PR #46).
        owned = self.repo("/repos/owned")
        upsert_repo(self.connection, owned, status_source="row")
        mine = self.item("Owned work", kind="work", repo=owned, path="docs/work/mine/prd.md")
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertEqual(rows[self.ids["port"]]["edit"],
                         {"allowed": False, "reason": "work metadata belongs to its file owner until database cutover completes"})
        self.assertEqual(rows[mine]["edit"], {"allowed": True, "reason": None})
        for item, row in rows.items():
            try:
                workflow.edit_item(self.connection, item, {}, who="test")
                said = {"allowed": True, "reason": None}
            except workflow.WorkflowError as refused:
                said = {"allowed": False, "reason": str(refused)}
            self.assertEqual(row["edit"], said, f"item {item} ({row['kind']})")

    def test_the_rows_are_the_backlog_rows_with_revision_and_allowed_statuses(self):
        doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["statuses"], ["planning", "ready", "in_progress", "blocked", "done"])
        rows = {row["id"]: row for row in doc["rows"]}
        self.assertEqual(set(rows), {self.ids["plan"], self.ids["ask"], self.ids["port"]})
        plan = rows[self.ids["plan"]]
        self.assertEqual((plan["status"], plan["priority"], plan["due"], plan["repo"], plan["repo_path"]),
                         ("ready", 2, "2026-09-09", "system", "/repos/system"))
        self.assertEqual(plan["revision"], workflow.item_state(self.connection, self.ids["plan"])["revision"])
        self.assertEqual(plan["allowed"], workflow.allowed_statuses(self.connection, self.ids["plan"]))
        self.assertIsNone(plan["assignment"])
        port = rows[self.ids["port"]]
        self.assertEqual((port["assignment"], port["allowed"]), ("running", []))

    def test_each_row_carries_its_age_bucket_and_the_document_names_the_buckets(self):
        # The age filter is the histogram's (sd:2589): each row's key is reads.age_bucket, and the document lists every
        # bucket with the label Operations draws, so a bar's ?age= names a chip the page shows.
        self.age(self.ids["ask"], 10)
        doc = tasks_screen.document(self.connection, now=NOW)
        rows = {row["id"]: row for row in reads.backlog_items(self.connection, now=NOW)}
        for row in doc["rows"]:
            self.assertEqual(row["age"], reads.age_bucket(rows[row["id"]], now=NOW))
        self.assertEqual(next(r for r in doc["rows"] if r["id"] == self.ids["ask"])["age"], "7")
        self.assertEqual(doc["ages"], [{"key": bucket.key, "label": bucket.label} for bucket in reads.age_histogram([], now=NOW)])
        self.assertEqual([age["key"] for age in doc["ages"]], [str(lower) for lower, _ in reads.age_bounds()])

    def test_each_row_reads_its_history_once(self):
        # sd:2380: allowed_statuses takes the row's item_state, read in the same snapshot, instead of reading it again.
        calls, real = [], workflow.item_state

        def counted(connection, item):
            calls.append(item)
            return real(connection, item)

        with patch.object(workflow, "item_state", counted):
            doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual(sorted(calls), sorted(row["id"] for row in doc["rows"]), "a row's history was read more than once")
        for row in doc["rows"]:
            self.assertEqual(row["allowed"], workflow.allowed_statuses(self.connection, row["id"]), row["id"])
        details = tasks_screen.details(self.connection, self.ids["plan"], now=NOW)
        self.assertEqual(details["allowed"], workflow.allowed_statuses(self.connection, self.ids["plan"]))

    def test_the_details_carry_the_work_controls_for_a_work_item_only(self):
        # sd:2200: relink and cancel are on where progress.work_controls says the mutation would take them.
        from sd_db import progress

        port = tasks_screen.details(self.connection, self.ids["port"], now=NOW)
        self.assertEqual(port["work"], progress.work_controls(self.connection, self.ids["port"]))
        self.assertIsNone(tasks_screen.details(self.connection, self.ids["plan"], now=NOW)["work"])

    def test_the_details_split_the_show_reading_into_its_sections(self):
        got = tasks_screen.details(self.connection, self.ids["plan"], now=NOW)
        state = workflow.item_state(self.connection, self.ids["plan"])
        self.assertEqual((got["item"], got["revision"]), (state["item"], state["revision"]))
        self.assertEqual([n["kind"] for n in got["history"]], ["status_change", "status_change"])
        self.assertEqual([(n["kind"], n["body"], n["resolved"]) for n in got["notes"]],
                         [("followup", "Check the budget first", None), ("comment", "A plain comment", None)])
        self.assertEqual(got["assignments"], [])
        external = got["external"]
        self.assertEqual((external["tracker"], external["url"], external["state"]),
                         ("github", "https://example.invalid/i/1", "open"))
        self.assertEqual(set(external["freshness"]), {"state", "last_success_at", "reason"})

    def test_an_assignment_carries_the_revision_and_cancel_capability_sd_assignments_cancel_checks(self):
        from sd_db import operations

        got = tasks_screen.details(self.connection, self.ids["port"], now=NOW)
        (row,) = got["assignments"]
        self.assertEqual((row["role"], row["status"]), ("author", "running"))
        state = operations.assignment_state(self.connection, row["id"])
        self.assertEqual((row["revision"], row["cancel"]), (state["revision"], state["capabilities"]["cancel"]))
        # sd:3041: no runner readiness or runner capabilities ride on the Details.
        self.assertNotIn("run", got)
        self.assertNotIn("runner", row)
        self.assertIsNone(got["external"])
        self.assertEqual(got["item"]["path"], "docs/work/port/prd.md")

    def test_each_row_carries_the_reads_is_urgent_decision(self):
        report = self.item("Look at the failed run", kind="report", fields={"attention": True})
        backlog = {row["id"]: row for row in reads.backlog_items(self.connection, now=NOW)}
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertTrue(rows[report]["urgent"])
        self.assertEqual({n: r["urgent"] for n, r in rows.items()}, {n: reads.is_urgent(r, now=NOW) for n, r in backlog.items()})

    def test_urgency_beyond_the_due_date_comes_from_reads_is_urgent(self):
        report = self.item("Look at the failed run", kind="report", fields={"attention": True})
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertTrue(rows[report]["urgent_otherwise"])
        self.assertFalse(rows[self.ids["plan"]]["urgent_otherwise"], "a due date is the page's own rule, not sent")

    def test_a_missing_item_raises_for_the_route_to_answer_404(self):
        with self.assertRaises(workflow.MissingItem):
            tasks_screen.details(self.connection, 9999, now=NOW)


class ThePage(BrowserSession):
    def setUp(self):
        super().setUp()
        self.ids = seed(self)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Tasks · system</title>", body)
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "tasks.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_routes_need_a_session_and_take_no_query(self):
        self.assertEqual(self.request("/api/tasks")[0], 403)
        self.assertEqual(self.request(f"/api/tasks/{self.ids['plan']}")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/tasks?kind=task", headers=cookie)[0], 400)
        self.assertEqual(self.request("/api/tasks/9999", headers=cookie)[0], 404)
        self.assertEqual(self.request("/api/tasks/99999999999999999999", headers=cookie)[0], 404)
        self.assertEqual(self.request("/api/tasks/0", headers=cookie)[0], 404)

    def test_a_row_revision_is_the_one_the_status_write_accepts(self):
        cookie = {"Cookie": self.cookie}
        status, _, body = self.request("/api/tasks", headers=cookie)
        self.assertEqual(status, 200)
        row = next(r for r in json.loads(body)["rows"] if r["id"] == self.ids["ask"])
        status, _, state = self.post(f"/api/items/{row['id']}/status", {"status": "ready", "revision": row["revision"]})
        self.assertEqual((status, state["item"]["status"]), (200, "ready"))
        status, _, state = self.post(f"/api/items/{row['id']}/status", {"status": "planning", "revision": row["revision"]})
        self.assertEqual(status, 409)
        status, _, body = self.request(f"/api/tasks/{self.ids['plan']}", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["external"]["url"], "https://example.invalid/i/1")


# The stand-in page. Elements record the markup put() gives them; fetch answers from ANSWER(path, body); the shell records
# toasts and runs commands the way shell.js does. The test script is an async body: osascript drains the promise jobs
# between the top level and run(), so run() reads what the whole flow produced.
STAND_IN = r"""
var window = globalThis, OUT = { posts: [], gets: [], toasts: [], confirms: [], states: [], attention: [], error: null };
var innerWidth = 1440, location = { search: '' };
function URLSearchParams(q) { var m = new Map(); (q || '').replace(/^\?/, '').split('&').filter(Boolean).forEach(p => { var kv = p.split('='); m.set(kv[0], decodeURIComponent(kv[1] || '')); });
  this.get = k => m.has(k) ? m.get(k) : null; this.set = (k, v) => m.set(k, String(v));
  this.toString = () => [...m].map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&'); }
function El(id) { var e = { id: id, html: null, hidden: false, dataset: {}, style: {}, listeners: {}, value: '',
  classList: { add() {}, remove() {}, toggle() {} },
  addEventListener(t, f) { (e.listeners[t] = e.listeners[t] || []).push(f); }, setAttribute() {}, removeAttribute() {},
  toggleAttribute() {}, getAttribute() { return null; }, querySelector() { return null; }, querySelectorAll() { return []; },
  focus() {}, closest() { return null; }, matches() { return false; }, showModal() {}, remove() {},
  replaceChildren(f) { e.html = f ? f.parsed : ''; }, append(f) { e.html = (e.html || '') + (f && f.parsed || ''); }, prepend() {}, before() {} };
  return e; }
var ELS = {}, DOC_LISTENERS = {}, WIN_LISTENERS = {}, MADE = [];
var document = { body: El('body'),
  getElementById(id) { return ELS[id] = ELS[id] || El(id); },
  querySelector(sel) { return /sd-csrf/.test(sel) ? { content: 'c'.repeat(64) } : null; },
  querySelectorAll() { return []; },
  createElement(tag) { if (tag !== 'template') { var made = El(tag); MADE.push(made); return made; }
    var t = {}; Object.defineProperty(t, 'innerHTML', { set: function (v) { t.content = { parsed: v }; } }); return t; },
  addEventListener(t, f) { (DOC_LISTENERS[t] = DOC_LISTENERS[t] || []).push(f); },
  dispatchEvent(e) { (DOC_LISTENERS[e.type] || []).forEach(f => f(e)); } };
function addEventListener(t, f) { (WIN_LISTENERS[t] = WIN_LISTENERS[t] || []).push(f); }
function CustomEvent(type, o) { this.type = type; this.detail = o && o.detail; }
function Event(type) { this.type = type; }
function matchMedia() { return { matches: false }; }
function requestAnimationFrame() { return 0; }
function setTimeout(f) { f(); return 0; }
var ANSWER = null;
function fetch(path, o) {
  var body = o && o.body ? JSON.parse(o.body) : null;
  if (o && o.method === 'POST') OUT.posts.push([path, body, o.headers['X-SD-CSRF'].length]); else OUT.gets.push(path);
  return Promise.resolve(ANSWER(path, body)).then(a => ({ ok: a[0] < 300, status: a[0], json: () => Promise.resolve(a[1]) }));
}
"""
SHELL = SETTLE_JS + FETCH_JS + r"""
var REG = [], OBJ = new Map();
var C = { register: (...cs) => { REG.push(...cs); }, put: o => { OBJ.set(o.id, o); }, get: id => OBJ.get(id), select() {}, pick() {},
  rowActions: () => mk``, bar: () => mk`<div class="bar"></div>` };
// A command with fields asks for them (shell.js's confirm): FIELD_VALUES is what the operator typed, and a required field left
// empty keeps OK off, so nothing runs.
var FIELD_VALUES = {};
function shellRun(c, o) {
  var off = c.when ? c.when(o) : true;
  if (off !== true) { OUT.toasts.push({ msg: 'off: ' + off }); return; }
  if (c.risk === 'confirm' || c.fields) OUT.confirms.push(c.id);
  var v = c.fields ? Object.fromEntries(c.fields(o).map(f => [f.name, String(FIELD_VALUES[f.name] || '').trim()])) : undefined;
  if (c.fields && c.fields(o).some(f => f.required && !v[f.name])) { OUT.toasts.push({ msg: 'not run: OK is off until ' + c.fields(o).filter(f => f.required && !v[f.name]).map(f => f.label).join(', ') + ' is typed' }); return; }
  var msg = c.run ? c.run(o, v) : '';
  if (thenable(msg)) { settleOne(c, o, msg, { toast: shellToast }); return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } })); }
  if (msg === null) return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id, form: true } }));
  OUT.toasts.push({ msg: msg || c.label, undo: c.risk === 'undo' && c.undo ? () => c.undo(o) : null });
  document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } }));
}
C.run = shellRun;
// shell.js's runBulk: runEach starts one run per picked object in the same tick, settled by settleBulk. A command with
// batch(objs, done) takes the whole group in one call instead (handOver, sd:2590); done() clears the picks, recorded here.
function shellBulk(c, objs) {
  if (handOver(c, objs, () => { OUT.cleared = (OUT.cleared || 0) + 1; })) return;
  var rs = runEach(c, objs);
  settleBulk(c, objs, rs, { toast: shellToast, plural: window.markup.plural });
  document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: 'bulk', form: true } }));
}
C.runBulk = shellBulk;
function shellToast(msg, undo) { OUT.toasts.push({ msg: msg, undo: undo || null }); }
window.shell = { commands: C, post, getJSON, ICON: () => mk`<svg></svg>`, toast: shellToast, shq: s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`,
  suggest() {}, openPane() {}, url() {}, chording: () => false, reconcile() {}, views() {}, capture() {},
  state: s => OUT.states.push(s), attention: a => OUT.attention.push(a),
  list: (() => { const { html, plural } = window.markup, ICON = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
""" + LIST_JS + r"""
    return list; })() };
const cmd = id => REG.find(c => c.id === id);
const flush = async () => { for (let i = 0; i < 400; i++) await null; };
const open = key => document.dispatchEvent(new CustomEvent('shell:open', { detail: String(key) }));
const lastToast = () => OUT.toasts[OUT.toasts.length - 1];
const lastUndo = () => OUT.toasts.filter(t => t.undo).pop();
"""
# What Tasks adds to the shared stand-in: shell.row() as shell.js answers it, and the real reader (sd:2484).
TASKS_SHELL = """window.shell.row = () => new URLSearchParams(location.search).get('row');
""" + READ_SHELL
# A landed write's readback moves the stored row with it, as the server's next read lists it: the page rereads after each
# landed write, and a reread that brought back the row as it was before would undo the write on the page.
STORED = r"""
const STORED_FIELDS = ['status', 'priority', 'due', 'recurrence', 'recurrence_anchor'];
function stored(a) {
  const body = a && a[0] < 300 && a[1], row = body && body.item && body.revision && DOC.rows.find(r => r.id === body.item.id);
  if (row) { STORED_FIELDS.forEach(k => { if (k in body.item) row[k] = body.item[k]; }); row.revision = body.revision; }
  return a;
}
const storing = a => a && typeof a.then === 'function' ? a.then(stored) : stored(a);
"""


class TheShellRunContract(unittest.TestCase):
    """shell.js's settleOne and settleBulk, run alone: a run may return a promise, and Undo covers only what landed."""

    def settle(self, body):
        script = "var window = globalThis;\n" + MARKUP_JS + SETTLE_JS + r"""
var OUT = { toasts: [], undone: [], error: null }, toast = (msg, undo) => OUT.toasts.push({ msg, undo: undo || null });
const plural = window.markup.plural, flush = async () => { for (let i = 0; i < 50; i++) await null; };
const obj = n => ({ id: String(n), label: '#' + n });
(async () => { try {
""" + body + """
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }
"""
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    COMMAND = """const c = { id: 'x.move', on: 'item', label: 'Move', risk: 'undo',
  undo: (o, v) => { OUT.undone.push([o.id, v]); return v === 'gone' ? false : null; } };
"""

    def test_a_bulk_group_toasts_after_every_run_and_undoes_only_what_landed(self):
        out = self.settle(self.COMMAND + """const objs = [obj(1), obj(2), obj(3)];
const p = settleBulk(c, objs, [Promise.resolve('a1'), Promise.reject(new Error('stale')), 'sync'], { toast, plural });
OUT.early = OUT.toasts.length; OUT.result = await p;
OUT.toasts[0].undo(); await flush();""")
        self.assertEqual(out["early"], 0, "the group toasted before its runs settled")
        self.assertEqual(out["result"], {"landed": 2, "failed": 1})
        self.assertEqual(out["undone"], [["1", "a1"], ["3", "sync"]])
        self.assertEqual(out["toasts"], [["Move · 2 items · 1 of 3 not changed: stale", True],
                                         ["Move undone · 2 of 3 reversed · not reversed: #2 (its change did not land)", False]])

    def test_a_run_that_throws_fails_its_own_row_and_the_rows_after_it_still_run(self):
        # A picked row gone in a refresh makes a run throw before it returns. It is that row's failure, not the group's
        # end: the next row runs, and the group's toast names the thrown reason (review, PR #46).
        out = self.settle(self.COMMAND + """const ran = [], objs = [obj(1), obj(2), obj(3)];
c.run = o => { if (o.id === '2') throw new Error('the task is no longer listed'); ran.push(o.id); return Promise.resolve(o.id); };
OUT.result = await settleBulk(c, objs, runEach(c, objs), { toast, plural }); OUT.ran = ran;""")
        self.assertEqual(out["ran"], ["1", "3"])
        self.assertEqual(out["result"], {"landed": 2, "failed": 1})
        self.assertEqual(out["toasts"], [["Move · 2 items · 1 of 3 not changed: the task is no longer listed", True]])

    def test_a_bulk_group_with_nothing_landed_offers_no_undo(self):
        out = self.settle(self.COMMAND + """await settleBulk(c, [obj(1)], [Promise.reject(new Error('refused'))], { toast, plural });""")
        self.assertEqual(out["toasts"], [["Move · 0 items · 1 of 1 not changed: refused", False]])

    def test_an_undo_that_had_nothing_to_reverse_is_named(self):
        out = self.settle(self.COMMAND + """await settleBulk(c, [obj(1), obj(2)], [Promise.resolve('gone'), Promise.resolve('b')], { toast, plural });
OUT.toasts[0].undo(); await flush();""")
        self.assertEqual(out["toasts"][-1], ["Move undone · 1 of 2 reversed · not reversed: #1 (nothing to reverse)", False])

    def test_a_single_run_toasts_when_it_lands_and_not_when_it_fails(self):
        out = self.settle(self.COMMAND + """await settleOne(c, obj(1), Promise.resolve({ text: 'Moved #1' }), { toast });
await settleOne(c, obj(2), Promise.reject(new Error('stale')), { toast });
OUT.toasts[0].undo(); await flush();""")
        self.assertEqual(out["toasts"], [["Moved #1", True], ["#2 not changed: stale", False], ["Move undone · #1", False]])
        self.assertEqual(out["undone"], [["1", {"text": "Moved #1"}]])


class TheConfirmFields(unittest.TestCase):
    """shell.js's confirmAction with fields (sd:2200), run against a stand-in dialog: OK is off until a required field is typed."""

    def confirm(self, body):
        self.assertIsNotNone(CONFIRM, "shell.js has no confirm:start block")
        script = "var window = globalThis;\n" + MARKUP_JS + r"""
const { html } = window.markup, put = (el, m) => { el.html = String(m); };
var OUT = { focused: null, result: null, error: null };
const yes = { disabled: false }, no = {}, line = { textContent: '' }, inputs = { reason: { value: '', tagName: 'INPUT' } }, L = {};
const form = { elements: inputs, addEventListener: (t, f) => { (L[t] = L[t] || []).push(f); },
  querySelector: s => s === '[value="yes"]' ? yes : s === '#confirm-cli' ? line : null,
  requestSubmit: b => { confirmDlg.returnValue = b === yes ? 'yes' : 'no'; confirmDlg.onclose(); } };
var confirmDlg = { html: '', returnValue: '', onclose: null, querySelector: s => s === 'form' ? form : s === '[value="no"]' ? no : null };
const modal = (d, first) => { OUT.focused = first === inputs.reason ? 'reason' : first === no ? 'keep' : null; return null; };
const type = v => { inputs.reason.value = v; (L.input || []).forEach(f => f({})); };
const key = k => { let stopped = false; (L.keydown || []).forEach(f => f({ key: k, target: inputs.reason, preventDefault: () => { stopped = true; } })); return stopped; };
const close = v => { confirmDlg.returnValue = v; confirmDlg.onclose(); };
""" + CONFIRM.group(1) + r"""
(async () => { try {
""" + body + """
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { return JSON.stringify(OUT); }
"""
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    FIELDS = """const p = confirmAction({ title: 'Cancel item: #7 Port the page?', cli: v => `sd work cancel 7 --reason ${v.reason || "'<why>'"}`,
  ok: 'Cancel item', fields: [{ name: 'reason', label: 'Reason', required: true }] });
"""

    def test_ok_stays_off_until_a_required_field_is_typed(self):
        out = self.confirm(self.FIELDS + """OUT.steps = [[yes.disabled, line.textContent]];
type('   '); OUT.steps.push([yes.disabled, line.textContent]);
type('superseded'); OUT.steps.push([yes.disabled, line.textContent]);
close('yes'); OUT.result = await p; OUT.form = confirmDlg.html;""")
        self.assertEqual(out["steps"], [[True, "sd work cancel 7 --reason '<why>'"], [True, "sd work cancel 7 --reason '<why>'"],
                                        [False, "sd work cancel 7 --reason superseded"]])
        self.assertEqual(out["focused"], "reason")
        self.assertEqual((out["result"]["yes"], out["result"]["values"]), (True, {"reason": "superseded"}))
        self.assertIn('id="cf-reason" name="reason"', out["form"])
        self.assertIn('required aria-required="true"', out["form"])
        self.assertIn('value="no" formnovalidate', out["form"])

    def test_a_close_on_ok_with_the_field_empty_runs_nothing_and_enter_is_ok(self):
        out = self.confirm(self.FIELDS + """OUT.enterEmpty = [key('Enter'), confirmDlg.returnValue];
close('yes'); OUT.result = await p;
const q = confirmAction({ title: 't', fields: [{ name: 'reason', label: 'Reason', required: true }] });
type('done elsewhere'); key('Enter'); OUT.second = await q;""")
        self.assertEqual(out["enterEmpty"], [True, ""], "Enter with the field empty closed the dialog")
        self.assertFalse(out["result"]["yes"], "OK ran with the required field empty")
        self.assertEqual((out["second"]["yes"], out["second"]["values"]), (True, {"reason": "done elsewhere"}))

    def test_a_confirm_without_fields_is_unchanged(self):
        out = self.confirm("""const p = confirmAction({ title: 'Delete: x?', cli: 'sd x' }); OUT.off = yes.disabled; close('yes'); OUT.result = await p;""")
        self.assertEqual(out["focused"], "keep")
        self.assertFalse(out["off"])
        self.assertEqual((out["result"]["yes"], out["result"]["values"]), (True, {}))


def state_answer(row, **changes):
    """A write's readback, `workflow.item_state` shaped, for a row of the tasks document."""
    item = {"id": row["id"], "status": row["status"], "priority": row["priority"], "due": row["due"],
            "recurrence": row["recurrence"], **changes}
    return {"item": item, "notes": [], "revision": "b" * 64}


class TheScript(ScreenCase):
    """tasks.js against the documents `tasks_screen` builds for the seeded database."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {str(i): tasks_screen.details(self.connection, i, now=NOW) for i in self.ids.values()}

    def row(self, name):
        return next(r for r in self.doc["rows"] if r["id"] == self.ids[name])

    def run_page(self, body, answer="null", *, prelude="", env=None, search=""):
        """Load tasks.js at `search`, fire DOMContentLoaded, run `body` (async), and return OUT plus what `body` set on R.

        Each address the page writes is in OUT.urls, as shell.url() received it.
        """
        script = (prelude + STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + TASKS_SHELL
                  + f"\nconst DOC = {json.dumps(self.doc)}, DETAILS = {json.dumps(self.details)};\n"
                  + "const WRITE = " + answer + ";\nvar DETAIL = null;\n" + STORED
                  + f"location.search = {json.dumps(search)}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q.toString());\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/tasks') return typeof FAIL_TASKS === 'string' ? [500, { error: FAIL_TASKS }] : [200, JSON.parse(JSON.stringify(DOC))];
  var m = path.match(/^\\/api\\/tasks\\/(\\d+)$/); if (m) return typeof DETAIL === 'function' ? DETAIL(m[1]) : [200, DETAILS[m[1]]];
  return WRITE ? storing(WRITE(path, body)) : [404, { error: 'no answer' }];
};\n""" + TASKS_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False,
                                env=None if env is None else {**os.environ, **env})
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_command_is_registered_with_its_risk_key_and_run_path(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null,
  typeof c.executes === 'boolean' ? c.executes : null, typeof c.run === 'function', typeof c.undo === 'function']);""")
        statuses = [[f"item.status.{s}", "item", "undo", str(i + 1), None, True, True]
                    for i, s in enumerate(["planning", "ready", "in_progress", "blocked", "done"])]
        self.assertEqual(out["R"]["reg"], statuses + [
            ["item.complete", "item", "confirm", "5", True, True, False],
            ["item.edit", "item", "safe", "e", None, True, False],
            ["item.move", "item", "safe", "m", None, True, False],
            ["item.p2", "item", "undo", None, None, True, True],
            ["item.note", "item", "safe", "n", None, True, False],
            ["item.delete", "item", "confirm", None, None, False, False],
            ["work.relink", "item", "safe", "l", True, True, False],
            ["work.cancel", "item", "confirm", "w", True, True, False],
            ["item.recur", "item", "undo", None, None, True, True],
            ["item.recur.clear", "item", "undo", None, None, True, True],
            ["note.resolve", "note", "confirm", "v", None, True, False],
            ["asg.cancel", "assignment", "confirm", "x", None, True, False],
            ["asg.get", "assignment", "safe", "o", None, True, False],
        ])

    def test_the_page_reads_the_rows_and_draws_the_kind_and_repo_filters(self):
        out = self.run_page("R.filters = ELS.filters.html; R.board = ELS['view-board'].html; R.sub = ELS.subhead.html;")
        self.assertEqual(out["gets"], ["/api/tasks"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        filters = out["R"]["filters"]
        for kind in ("followup", "task", "work"):
            self.assertIn(f'data-f="kind" data-v="{kind}"', filters)
        self.assertIn('data-f="repo" data-v="system"', filters)
        self.assertIn("Port the page", out["R"]["board"])
        self.assertIn("3 tasks", out["R"]["sub"])
        # The seeded due date is in the past on any day this runs after 2026-09-09: one overdue task lights the page.
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "overdue tasks"})

    def test_the_kind_filter_narrows_the_rows(self):
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'kind', v: 'work' } } : null } });
R.board = ELS['view-board'].html;""")
        self.assertIn("Port the page", out["R"]["board"])
        self.assertNotIn("Plan the review", out["R"]["board"])

    def test_details_show_history_notes_assignments_artifact_and_external_context(self):
        plan, port = self.ids["plan"], self.ids["port"]
        out = self.run_page(f"""open({plan}); await flush(); R.plan = ELS.details.html;
open({port}); await flush(); R.port = ELS.details.html;""")
        self.assertEqual(out["gets"], ["/api/tasks", f"/api/tasks/{plan}", f"/api/tasks/{port}"])
        plan_html, port_html = out["R"]["plan"], out["R"]["port"]
        for heading in ("Status history", "Notes", "Assignments", "Artifact", "Recurrence", "External context"):
            self.assertIn(f"<h3>{heading}", plan_html)
        self.assertIn("2 notes · 1 open followup", plan_html)
        self.assertIn("Check the budget first", plan_html)
        self.assertIn("https://example.invalid/i/1", plan_html)
        self.assertIn("No assignment has run on this item.", plan_html)
        self.assertNotIn("External context", port_html)
        self.assertIn("<b>running</b>", port_html)
        self.assertIn("docs/work/port/prd.md", port_html)

    def test_a_status_move_toasts_after_the_write_and_undo_posts_the_old_status_with_the_new_revision(self):
        ask = self.ids["ask"]
        row = next(r for r in self.doc["rows"] if r["id"] == ask)
        answer = f"""(path, body) => [200, {{ item: {{ id: {ask}, status: body.status, priority: 3, due: null, recurrence: null }},
  notes: [], revision: body.status === 'ready' ? 'b'.repeat(64) : 'c'.repeat(64) }}]"""
        out = self.run_page(f"""shellRun(cmd('item.status.ready'), C.get('{ask}'));
R.before = OUT.toasts.length; await flush(); R.after = OUT.toasts.length;
lastToast().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["before"], 0, "the toast came before the write landed")
        self.assertEqual(out["R"]["after"], 1)
        self.assertEqual(out["posts"], [
            [f"/api/items/{ask}/status", {"status": "ready", "revision": row["revision"]}, 64],
            [f"/api/items/{ask}/status", {"status": "planning", "revision": "b" * 64}, 64],
        ])
        self.assertEqual(out["toasts"][0], [f"#{ask} Planning → Ready · sd task status {ask} ready", True])
        self.assertEqual(out["toasts"][1], [f"Status → Ready undone · #{ask} Answer the question", False])

    def test_a_refused_move_posts_nothing(self):
        port = self.ids["port"]
        out = self.run_page(f"shellRun(cmd('item.status.ready'), C.get('{port}')); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], [["off: A runner assignment is running. Status is locked until it ends.", False]])

    def test_a_stale_revision_toasts_and_reads_the_rows_again(self):
        ask = self.ids["ask"]
        out = self.run_page(f"shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();",
                            "() => [409, { error: 'The item changed. Reload it.', reload: true }]")
        self.assertEqual(out["gets"], ["/api/tasks", "/api/tasks"])
        self.assertEqual(out["toasts"], [[f"#{ask} Answer the question not changed: The item changed. Reload it.", False]])

    def test_a_conflict_reads_the_open_details_again(self):
        # A 409 means the item changed elsewhere: the rows are read again, and so are the open Details, or the pane keeps
        # showing what the conflict replaced (review, PR #46).
        ask = self.ids["ask"]
        after = json.loads(json.dumps(self.details[str(ask)]))
        after["notes"] = [{"id": 77, "kind": "comment", "at": "2026-09-06T12:00:00Z", "body": "Written elsewhere", "session": "other", "resolved": None}]
        out = self.run_page(f"""var reads = 0; DETAIL = id => ++reads === 1 ? [200, DETAILS[id]] : [200, AFTER];
open({ask}); await flush(); shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush(); R.html = ELS.details.html;""",
                            "() => [409, { error: 'The item changed. Reload it.' }]", prelude=f"var AFTER = {json.dumps(after)};\n")
        self.assertEqual(out["gets"], ["/api/tasks", f"/api/tasks/{ask}", "/api/tasks", f"/api/tasks/{ask}"])
        self.assertIn("Written elsewhere", out["R"]["html"])

    # sd:2484: the rows are read through shell.read (read.js), as Activity and Documents read theirs.
    def test_a_landed_write_reads_the_rows_again_and_a_row_no_longer_listed_runs_nothing(self):
        ask, port = self.ids["ask"], self.ids["port"]
        answer = f"""(path, body) => {{ DOC.rows = DOC.rows.filter(r => r.id !== {port});
  return [200, {{ item: {{ id: {ask}, status: body.status, priority: 3, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]; }}"""
        out = self.run_page(f"""shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
R.types = [C.get('{ask}').type, C.get('{port}').type]; R.board = ELS['view-board'].html;""", answer)
        self.assertEqual(out["gets"], ["/api/tasks", "/api/tasks"], "the landed write did not read the rows again")
        self.assertEqual(out["R"]["types"], ["item", "not listed"])
        self.assertNotIn("Port the page", out["R"]["board"])

    def test_a_failed_reread_after_a_write_keeps_the_rows_and_says_the_write_landed(self):
        ask = self.ids["ask"]
        answer = f"(path, body) => [200, {{ item: {{ id: {ask}, status: body.status, priority: 3, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""const A = ANSWER; ANSWER = (path, body) => path === '/api/tasks' ? [500, {{ error: 'database is locked' }}] : A(path, body);
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
R.board = ELS['view-board'].html; R.details = ELS.details.html; R.type = C.get('{ask}').type;""", answer)
        self.assertIn("Answer the question", out["R"]["board"], "the failed reread dropped the rows")
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertTrue(out["states"][-1]["text"].startswith("The change landed; the tasks were not read again: database is locked."), out["states"][-1])
        self.assertIn("The tasks were not read again after the change, so nothing is selected.", out["R"]["details"])

    def test_a_read_sent_before_a_newer_one_never_draws_over_it(self):
        # A refused write loads the rows; a task is added while that read is out. Each read's answer is the rows as they
        # were when it was sent, and the newest is answered first: the older answer must not draw the added task away.
        ask = self.ids["ask"]
        new = {**self.row("ask"), "id": 99, "title": "Call the bank", "kind": "task", "status": "planning"}
        answer = f"""(path, body) => path === '/api/items' ? (DOC.rows.push({json.dumps(new)}), [201, {{ item: {{ id: 99, title: 'Call the bank' }} }}])
  : [409, {{ error: 'The item changed. Reload it.' }}]"""
        out = self.run_page(f"""const A = ANSWER, HELD = []; DETAIL = id => DETAILS[id] ? [200, DETAILS[id]] : [404, {{ error: 'no such item' }}];
ANSWER = (path, body) => {{ if (path !== '/api/tasks') return A(path, body); const snap = A(path, body); return new Promise(r => HELD.push(() => r(snap))); }};
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
var box = document.getElementById('shift-in'); box.value = 'Call the bank';
box.listeners.keydown.forEach(f => f({{ key: 'Enter', preventDefault() {{}}, stopPropagation() {{}} }})); await flush();
for (let i = 0; i < 4; i++) {{ HELD.splice(0).reverse().forEach(f => f()); await flush(); }}
R.board = ELS['view-board'].html; R.listed = C.get('99') ? C.get('99').type : null;""", answer)
        self.assertIn("Call the bank", out["R"]["board"], "an older read drew over the newer one")
        self.assertEqual(out["R"]["listed"], "item")

    def test_a_failed_load_draws_no_row_and_lights_unknown(self):
        out = self.run_page("R.board = ELS['view-board'].html; R.details = ELS.details.html;", prelude="var FAIL_TASKS = 'database is locked';\n")
        self.assertEqual(out["states"][-1], {"kind": "error", "text": "The tasks were not read: database is locked. Reload retries it.", "source": "/api/tasks"})
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "tasks not read"})
        self.assertNotIn('data-key="', out["R"]["board"])
        self.assertIn("The tasks were not read, so nothing is selected.", out["R"]["details"])

    def test_the_read_guards_are_the_readers_not_a_copy(self):
        self.assertIn("shell.read({", TASKS_JS)
        for own in (r"\brereading\b", r"\bwrote\b", r"getJSON\('/api/tasks'\)", r"\bstarted = "):
            self.assertIsNone(re.search(own, TASKS_JS), own)

    def test_a_repeating_task_offers_no_remove_due_date(self):
        # workflow._recurring refuses a repeating item with no due date, so neither the Edit dialog nor the move out of
        # Urgent offers Remove due date on one; each says why instead (review, PR #46).
        self.repeat_plan()
        plan = self.ids["plan"]
        why = "Remove due date is off: a repeating task needs a due date. Stop repeating first to remove it."
        out = self.run_page(f"""const D = MADE.find(e => /date-dlg/.test(e.className || ''));
D.querySelector = sel => {{ const e = El(sel); e.querySelector = () => El('in'); return e; }};
open({plan}); await flush(); shellRun(cmd('item.edit'), C.get('{plan}')); await flush(); R.edit = D.html;
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush();
document.dispatchEvent({{ type: 'keydown', key: '2', target: El('card'), preventDefault() {{}} }}); await flush(); R.out = D.html;""")
        self.assertIn(f"Edit #{plan}", out["R"]["edit"])
        self.assertIn(f"#{plan} → Schedule", out["R"]["out"])
        for dialog in ("edit", "out"):
            self.assertNotIn("Remove due date</button>", out["R"][dialog], dialog)
            self.assertIn(why, out["R"][dialog], dialog)

    def test_resolve_confirms_first_posts_the_item_revision_and_offers_no_undo(self):
        plan = self.ids["plan"]
        note = self.details[str(plan)]["notes"][0]["id"]
        row = next(r for r in self.doc["rows"] if r["id"] == plan)
        answer = f"() => [200, {{ item: {{ id: {plan}, status: 'ready', priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""open({plan}); await flush(); R.when = cmd('note.resolve').when(C.get('note:{note}'));
R.comment = cmd('note.resolve').when(C.get('note:{self.details[str(plan)]["notes"][1]["id"]}'));
shellRun(cmd('note.resolve'), C.get('note:{note}')); await flush();""", answer)
        self.assertEqual(out["R"]["when"], True)
        self.assertEqual(out["R"]["comment"], "a comment note has nothing to resolve")
        self.assertEqual(out["confirms"], ["note.resolve"])
        self.assertEqual(out["posts"], [[f"/api/notes/{note}/resolve", {"revision": row["revision"]}, 64]])
        self.assertEqual(out["toasts"], [[f"Resolved note {note} · sd note resolve {note}", False]])

    def owned_work(self):
        """A work item in a repository the database owns, with no assignment: sd work relink and cancel take it."""
        owned = self.repo("/repos/owned")
        upsert_repo(self.connection, owned, status_source="row")
        mine = self.item("Owned work", kind="work", repo=owned, path="docs/work/mine/prd.md")
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details[str(mine)] = tasks_screen.details(self.connection, mine, now=NOW)
        return mine

    def test_relink_and_cancel_post_to_the_work_routes_with_the_typed_text(self):
        # sd:2200: both execute through /api/items/<id>/(relink|cancel) with the row's revision, after the confirm takes
        # the text only the operator has. Neither offers Undo.
        mine = self.owned_work()
        row = next(r for r in self.doc["rows"] if r["id"] == mine)
        answer = f"""(path, body) => [200, {{ item: {{ id: {mine}, status: path.endsWith('/cancel') ? 'done' : 'planning', priority: null, due: null, recurrence: null }},
  notes: [], revision: path.endsWith('/cancel') ? 'c'.repeat(64) : 'b'.repeat(64) }}]"""
        out = self.run_page(f"""open({mine}); await flush();
R.fields = [cmd('work.relink').fields(C.get('{mine}')), cmd('work.cancel').fields(C.get('{mine}'))].map(fs => fs.map(f => [f.name, f.required, f.placeholder]));
R.when = [cmd('work.relink').when(C.get('{mine}')), cmd('work.cancel').when(C.get('{mine}'))];
R.cli = [cmd('work.relink').cli(C.get('{mine}')), cmd('work.cancel').cli(C.get('{mine}'), {{ reason: "it's superseded" }})];
FIELD_VALUES = {{ path: 'docs/work/moved/prd.md' }}; shellRun(cmd('work.relink'), C.get('{mine}')); await flush();
FIELD_VALUES = {{ reason: "it's superseded" }}; shellRun(cmd('work.cancel'), C.get('{mine}')); await flush();""", answer)
        self.assertEqual(out["R"]["fields"], [[["path", True, "docs/work/mine/prd.md"]], [["reason", True, "why the work stops"]]])
        self.assertEqual(out["R"]["when"], [True, True])
        self.assertEqual(out["R"]["cli"], [f"sd work relink {mine} <moved path>", f"sd work cancel {mine} --reason 'it'\\''s superseded'"])
        self.assertEqual(out["confirms"], ["work.relink", "work.cancel"])
        self.assertEqual(out["posts"], [[f"/api/items/{mine}/relink", {"path": "docs/work/moved/prd.md", "revision": row["revision"]}, 64],
                                        [f"/api/items/{mine}/cancel", {"reason": "it's superseded", "revision": "b" * 64}, 64]])
        self.assertEqual(out["toasts"], [[f"#{mine} relinked → docs/work/moved/prd.md · sd work relink {mine} 'docs/work/moved/prd.md'", False],
                                         [f"#{mine} cancelled · sd work cancel {mine} --reason 'it'\\''s superseded'", False]])

    def test_cancel_with_no_reason_posts_nothing(self):
        mine = self.owned_work()
        out = self.run_page(f"""open({mine}); await flush(); FIELD_VALUES = {{ reason: '   ' }}; shellRun(cmd('work.cancel'), C.get('{mine}')); await flush();""")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], [["not run: OK is off until Reason is typed", False]])

    def test_relink_and_cancel_are_off_where_sd_work_refuses_them(self):
        # The seeded work row's repository lets its files own status, and it has a running assignment: progress.work_controls
        # gives the reason the mutation would. A task row is not work at all.
        port, plan = self.ids["port"], self.ids["plan"]
        why = self.details[str(port)]["work"]["reason"]
        out = self.run_page(f"""open({port}); await flush(); open({plan}); await flush();
R.port = [cmd('work.relink').when(C.get('{port}')), cmd('work.cancel').when(C.get('{port}'))];
R.plan = [cmd('work.relink').when(C.get('{plan}')), cmd('work.cancel').when(C.get('{plan}'))];""")
        self.assertTrue(why)
        self.assertEqual(out["R"]["port"], [why, why])
        self.assertEqual(out["R"]["plan"], ["sd work relink acts on a work item; this is a task", "sd work cancel acts on a work item; this is a task"])

    def test_recur_needs_a_due_date_and_undo_clears_it(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        row = next(r for r in self.doc["rows"] if r["id"] == plan)
        answer = f"(path, body) => [200, {{ item: {{ id: {plan}, status: 'ready', priority: 2, due: '2026-09-09', recurrence: body.recurrence }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""open({ask}); await flush(); R.noDue = cmd('item.recur').when(C.get('{ask}'));
open({plan}); await flush(); R.cli = cmd('item.recur').cli(C.get('{plan}'));
shellRun(cmd('item.recur'), C.get('{plan}')); await flush(); lastToast().undo(); await flush();""", answer)
        self.assertEqual(out["gets"].count(f"/api/tasks/{plan}"), 3, "the open Details were not read again after each write")
        self.assertEqual(out["R"]["noDue"], "--recur needs a due date")
        self.assertEqual(out["R"]["cli"], f"sd task edit {plan} --recur FREQ=WEEKLY")
        self.assertEqual(out["posts"], [[f"/api/items/{plan}", {"recurrence": "FREQ=WEEKLY", "revision": row["revision"]}, 64],
                                        [f"/api/items/{plan}", {"recurrence": None, "revision": "b" * 64}, 64]])

    def test_a_due_weekday_is_a_calendar_date_across_the_dst_change(self):
        # Friday 2026-10-30 in Denver; the clocks go back on Sunday 2026-11-01, so "due mon" is 2026-11-02.
        prelude = """var RealDate = Date; Date = class extends RealDate { constructor(...a) { if (a.length) super(...a); else super(2026, 9, 30, 9, 0, 0); } };\n"""
        out = self.run_page("""var box = document.getElementById('shift-in'); box.value = 'Call the bank due mon';
box.listeners.keydown.forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} })); await flush();""",
                            "() => [201, { item: { id: 99, title: 'Call the bank' } }]", prelude=prelude, env={"TZ": "America/Denver"})
        self.assertEqual(out["posts"][0][:2], ["/api/items", {"title": "Call the bank", "due": "2026-11-02"}])

    def test_details_read_before_a_write_never_replace_the_read_after_it(self):
        plan = self.ids["plan"]
        after = json.loads(json.dumps(self.details[str(plan)]))
        after["notes"][0]["body"] = "Written after the move"
        answer = f"(path, body) => [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""var release, reads = 0;
DETAIL = id => ++reads === 1 ? new Promise(r => {{ release = () => r([200, DETAILS[id]]); }}) : [200, AFTER];
open({plan}); await flush();
shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush();
release(); await flush(); R.html = ELS.details.html; R.reads = reads;""", answer, prelude=f"var AFTER = {json.dumps(after)};\n")
        self.assertEqual(out["R"]["reads"], 2)
        self.assertIn("Written after the move", out["R"]["html"])
        self.assertNotIn("Check the budget first", out["R"]["html"])

    def test_commands_are_off_where_the_library_would_refuse_them(self):
        plan, ask, port = self.ids["plan"], self.ids["ask"], self.ids["port"]
        (asg,) = self.details[str(port)]["assignments"]
        out = self.run_page(f"""open({ask}); await flush(); open({port}); await flush(); open({plan}); await flush();
R.p2 = cmd('item.p2').when(C.get('{plan}'));
R.recur = cmd('item.recur').when(C.get('{port}'));
R.cancel = cmd('asg.cancel').when(C.get('asg:{asg["id"]}'));""")
        self.assertEqual(out["R"], {
            "p2": "it is already P2",
            "recur": "a work item cannot recur",
            "cancel": asg["cancel"]["reason"]})
        self.assertTrue(out["R"]["cancel"])

    def test_edit_controls_are_off_for_a_work_row_its_files_own(self):
        # The seeded work row's repository lets its files own status, so edit_item refuses every field edit: Edit, P2 and a
        # matrix placement are off with the library's reason, and nothing is posted (review, PR #46).
        port = self.ids["port"]
        why = "work metadata belongs to its file owner until database cutover completes"
        out = self.run_page(f"""open({port}); await flush();
R.edit = cmd('item.edit').when(C.get('{port}')); R.p2 = cmd('item.p2').when(C.get('{port}'));
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush();
document.dispatchEvent({{ type: 'keydown', key: '1', target: El('card'), preventDefault() {{}} }}); await flush();""")
        self.assertEqual((out["R"]["edit"], out["R"]["p2"]), (why, why))
        self.assertEqual(out["posts"], [])
        self.assertIn([f"#{port} not moved: {why}", False], out["toasts"])

    def test_a_second_stop_repeating_queued_behind_the_first_sends_nothing(self):
        # Two quick Stop repeating: the second waits for the first, and by then the task no longer repeats. It refuses as
        # it leaves, so no empty write goes out and no Undo sets back the rule the first one cleared (review, PR #46).
        self.repeat_plan()
        plan, row = self.ids["plan"], self.row("plan")
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {plan}, status: 'ready', priority: 2,
  due: '2026-09-09', recurrence: body.recurrence, recurrence_anchor: body.recurrence_anchor ?? null }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        out = self.run_page(f"""open({plan}); await flush();
shellRun(cmd('item.recur.clear'), C.get('{plan}')); shellRun(cmd('item.recur.clear'), C.get('{plan}')); await flush();
for (let i = 0; i < 3; i++) {{ HOLD.splice(0).forEach(f => f()); await flush(); }}
OUT.toasts.find(t => t.undo).undo(); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual(out["posts"], [
            [f"/api/items/{plan}", {"recurrence": None, "revision": row["revision"]}, 64],
            [f"/api/items/{plan}", {"recurrence": "FREQ=WEEKLY", "recurrence_anchor": row["recurrence_anchor"], "revision": "b" * 64}, 64]])
        self.assertIn([f"#{plan} Plan the review not changed: the task does not repeat", False], out["toasts"])

    def test_a_status_with_no_column_gets_the_other_lane(self):
        ask = self.ids["ask"]
        for row in self.doc["rows"]:
            if row["id"] == ask:
                row["status"] = "ready_to_send"
        out = self.run_page("R.board = ELS['view-board'].html;")
        other = out["R"]["board"].split('aria-label="Other statuses"', 1)
        self.assertEqual(len(other), 2, "no Other lane")
        self.assertIn("Answer the question", other[1])
        self.assertIn("data-other", out["R"]["board"])

    def test_the_matrix_places_by_reads_is_urgent_and_each_quadrant_owns_its_cards(self):
        ask = self.ids["ask"]
        for row in self.doc["rows"]:
            if row["id"] == ask:
                row["urgent"] = True  # the server's reads.is_urgent decision, with no due date on the row
        out = self.run_page("""document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'matrix' })); await flush();
R.matrix = ELS['view-matrix'].html;""")
        quads = dict(re.findall(r'data-q="(\w+)"(.*?)(?=data-q="|$)', out["R"]["matrix"], re.S))
        self.assertIn("Answer the question", quads["delegate"])
        # Every card sits in a quadrant's drop list, a labelled group; a card is an article, not an option.
        self.assertEqual(out["R"]["matrix"].count('<div class="drop" role="group"'), 4)
        self.assertNotIn('<div class="drop">', out["R"]["matrix"])

    # 2026-09-06T12:00Z, 06:00 in Denver: Sep 14 is 8 calendar days away there, and 7 whole days by reads.is_urgent.
    SEP6 = "var RealDate = Date; Date = class extends RealDate { constructor(...a) { if (a.length) super(...a); else super(2026, 8, 6, 6, 0, 0); } };\n"

    def test_a_due_edit_is_urgent_by_the_servers_rule_from_the_read_stamp(self):
        # A due edit lands before the rows are read again, so the page places it with reads.is_urgent's due rule: from the
        # document's read stamp in whole days, not the viewer's calendar days (review, PR #46).
        plan, read = self.ids["plan"], "2026-09-06T12:00:00Z"
        self.doc["read"] = read
        row = self.row("plan")
        row.update(due="2026-12-01", urgent=False)
        self.assertTrue(reads.is_urgent({"due": "2026-09-14", "status": "ready", "kind": "task"}, now=read))
        answer = f"(path, body) => [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-14', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        # The landed write's reread is held: what the page shows meanwhile is its own placement, not the server's.
        out = self.run_page(f"""const A = ANSWER; ANSWER = (path, body) => path === '/api/tasks' ? new Promise(() => {{}}) : A(path, body);
shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush();
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush(); R.matrix = ELS['view-matrix'].html;""",
                            answer, prelude=self.SEP6, env={"TZ": "America/Denver"})
        quads = dict(re.findall(r'data-q="(\w+)"(.*?)(?=data-q="|$)', out["R"]["matrix"], re.S))
        self.assertIn("Plan the review", quads["do"], "a task due in 7 whole days is not urgent on the page")

    def test_the_week_filter_is_the_servers_due_rule(self):
        # The ≤ 7 days chip is the window the matrix calls urgent: for each due date, the rows it keeps are the ones
        # reads.is_urgent's due rule calls urgent at the document's read stamp (review, PR #46).
        read = "2026-09-06T12:00:00Z"
        self.doc["read"] = read
        base = self.row("plan")
        dues = ["2026-09-06", "2026-09-07", "2026-09-13", "2026-09-14", "2026-09-15", "2026-10-01"]
        self.doc["rows"] = [{**base, "id": 900 + n, "title": f"due {due}", "due": due, "urgent": False} for n, due in enumerate(dues)]
        out = self.run_page("""document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'list' })); await flush();
ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'due', v: 'week' } } : null } }); await flush();
R.list = ELS['view-list'].html;""", prelude=self.SEP6, env={"TZ": "America/Denver"})
        kept = {due for due in dues if f"due {due}" in out["R"]["list"]}
        self.assertEqual(kept, {due for due in dues if reads.is_urgent({"due": due, "status": "planning", "kind": "task"}, now=read)})
        self.assertIn("2026-09-14", kept)

    def test_the_date_dialog_bounds_are_the_servers_urgent_window(self):
        # At 2026-09-06T12:00Z the server calls Sep 14 urgent, so out of Urgent starts on Sep 15 and into Urgent ends on
        # Sep 14; 7 and 8 local days put the boundary a day early (review, PR #46).
        plan, ask = self.ids["plan"], self.ids["ask"]
        self.doc["read"] = "2026-09-06T12:00:00Z"
        self.row("plan").update(due="2026-09-10", urgent=True)
        self.row("ask").update(due=None, urgent=False, urgent_otherwise=False)
        out = self.run_page(f"""const D = MADE.find(e => /date-dlg/.test(e.className || '')); const due = El('due-in'); D.querySelector = () => due;
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush();
open({plan}); await flush(); document.dispatchEvent({{ type: 'keydown', key: '2', target: El('card'), preventDefault() {{}} }}); await flush();
R.out = D.html;
open({ask}); await flush(); document.dispatchEvent({{ type: 'keydown', key: '1', target: El('card'), preventDefault() {{}} }}); await flush();
R.into = D.html;""", prelude=self.SEP6, env={"TZ": "America/Denver"})
        self.assertIn('min="2026-09-15"', out["R"]["out"])
        self.assertIn("Not urgent means due after Sep 14.", out["R"]["out"])
        self.assertIn('min="2026-09-06" max="2026-09-14"', out["R"]["into"])

    def test_a_card_holding_controls_is_an_article_and_the_selection_is_aria_current(self):
        # An option's descendants are presentational to assistive technology, so a card with a checkbox and buttons
        # is no option, and no list holding cards is a listbox (review, PR #46).
        plan = self.ids["plan"]
        out = self.run_page(f"""open({plan}); await flush();
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'board' }})); await flush(); R.board = ELS['view-board'].html;
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush(); R.matrix = ELS['view-matrix'].html;""")
        for view in ("board", "matrix"):
            html = out["R"][view]
            self.assertNotIn('role="option"', html, view)
            self.assertNotIn('role="listbox"', html, view)
            self.assertNotIn("aria-selected", html, view)
            self.assertIn(f'<article class="card" tabindex="0" data-key="{plan}" aria-current="true"', html, view)

    def landed_plan(self, refused):
        """A write answer: `refused` gets a 409; a write to `plan` lands, and the stored row moves with it, as a re-read sees."""
        plan = self.ids["plan"]
        return f"""(path, body) => {refused}
  ? [409, {{ error: 'The item changed. Reload it.' }}]
  : (Object.assign(DOC.rows.find(r => r.id === {plan}), {{ status: body.status, revision: 'b'.repeat(64) }}),
     [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}])"""

    def test_a_bulk_undo_reverses_only_the_writes_that_landed(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = self.landed_plan(f"path === '/api/items/{ask}/status' && body.status === 'in_progress'")
        # No Undo is offered before every write answered; then it reverses the landed one and skips the refused one.
        out = self.run_page(f"""shellBulk(cmd('item.status.in_progress'), [C.get('{plan}'), C.get('{ask}')]);
R.early = OUT.toasts.length; await flush(); lastUndo().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["early"], 0, "the bulk toast came before the writes answered")
        self.assertEqual([[p[0], p[1]["status"], p[1]["revision"]] for p in out["posts"]][2], [f"/api/items/{plan}/status", "ready", "b" * 64])
        self.assertEqual([[p[0], p[1]["status"]] for p in out["posts"]], [
            [f"/api/items/{plan}/status", "in_progress"], [f"/api/items/{ask}/status", "in_progress"], [f"/api/items/{plan}/status", "ready"]])
        self.assertIn(f"Status → In progress undone · 1 of 2 reversed · not reversed: #{ask} Answer the question (its change did not land)", [t[0] for t in out["toasts"]])

    def test_a_bulk_failure_toast_offers_undo_for_the_landed_writes_only(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = self.landed_plan(f"path === '/api/items/{ask}/status'")
        out = self.run_page(f"""shellBulk(cmd('item.status.blocked'), [C.get('{plan}'), C.get('{ask}')]); await flush();
R.gets = OUT.gets.length; lastUndo().undo(); await flush();""", answer)
        # The first read, the landed write's reread, and the refused write's load (sd:2484).
        self.assertEqual(out["R"]["gets"], 3, "the refused write did not read the rows again")
        self.assertIn(["Status → Blocked · 1 item · 1 of 2 not changed: The item changed. Reload it.", True], out["toasts"])
        self.assertEqual(out["toasts"][-1], [f"Status → Blocked undone · 1 of 2 reversed · not reversed: #{ask} Answer the question (its change did not land)", False])
        self.assertEqual([p[0] for p in out["posts"]].count(f"/api/items/{ask}/status"), 1, "Undo wrote the refused item")
        self.assertEqual(out["posts"][-1][:2], [f"/api/items/{plan}/status", {"status": "ready", "revision": "b" * 64}])

    def test_a_bulk_undo_refuses_a_row_another_writer_changed_after_its_write(self):
        # Review round 3: the group moves plan; another writer then blocks plan; ask's 409 re-reads the rows, which bring
        # that writer's revision. Undo must not move plan back over it: it names the row and posts nothing for it.
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{ask}/status'
  ? (Object.assign(DOC.rows.find(r => r.id === {plan}), {{ status: 'blocked', revision: 'f'.repeat(64) }}), [409, {{ error: 'The item changed. Reload it.' }}])
  : [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        out = self.run_page(f"""shellBulk(cmd('item.status.in_progress'), [C.get('{plan}'), C.get('{ask}')]); await flush();
R.status = DOC.rows.find(r => r.id === {plan}).status; lastUndo().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["status"], "blocked")
        self.assertEqual([[p[0], p[1]["status"]] for p in out["posts"]], [
            [f"/api/items/{plan}/status", "in_progress"], [f"/api/items/{ask}/status", "in_progress"]], "Undo wrote over the other writer's change")
        self.assertEqual(out["toasts"][-1], [
            f"Status → In progress undone · 0 of 2 reversed · not reversed: #{ask} Answer the question (its change did not land), "
            f"#{plan} Plan the review (it changed after this write, and Undo would overwrite that change)", False])

    def test_a_single_undo_refuses_a_row_another_writer_changed_after_its_write(self):
        # A single move lands at revision b. Another writer blocks the task; a refused write elsewhere re-reads the rows,
        # which bring that writer's revision f. Undo posts nothing.
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{ask}/status' ? [409, {{ error: 'The item changed. Reload it.' }}]
  : [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        out = self.run_page(f"""shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush(); const undo = lastUndo().undo;
Object.assign(DOC.rows.find(r => r.id === {plan}), {{ status: 'blocked', revision: 'f'.repeat(64) }});
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush(); undo(); await flush();""", answer)
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{plan}/status", f"/api/items/{ask}/status"], "Undo wrote over the other writer's change")
        self.assertEqual(out["toasts"][-1], [f"Status → In progress not undone · #{plan} Plan the review: it changed after this write, and Undo would overwrite that change", False])

    def test_an_edit_undo_refuses_a_row_another_writer_changed_after_its_write(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{plan}/status' ? [409, {{ error: 'The item changed. Reload it.' }}]
  : [200, {{ item: {{ id: {ask}, status: 'planning', priority: body.priority, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        out = self.run_page(f"""shellRun(cmd('item.p2'), C.get('{ask}')); await flush(); const undo = lastUndo().undo;
Object.assign(DOC.rows.find(r => r.id === {ask}), {{ priority: 1, revision: 'f'.repeat(64) }});
shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush(); undo(); await flush();""", answer)
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{ask}", f"/api/items/{plan}/status"], "Undo wrote over the other writer's change")
        self.assertEqual(out["toasts"][-1], [f"Edit → P2 not undone · #{ask} Answer the question: it changed after this write, and Undo would overwrite that change", False])

    def test_a_queued_done_asks_again_whether_the_task_repeats(self):
        # Review round 3: repeat weekly is sent; Done is pressed before it answers, while the task does not repeat yet. As
        # Done's write leaves, the task repeats, so the plain move would complete a series without its confirm: it refuses.
        plan = self.ids["plan"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {plan}, status: 'ready', priority: 2, due: '2026-09-09',
  recurrence: body.recurrence === undefined ? null : body.recurrence }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        out = self.run_page(f"""open({plan}); await flush();
shellRun(cmd('item.recur'), C.get('{plan}')); shellRun(cmd('item.status.done'), C.get('{plan}')); await flush();
HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{plan}"], "Done was sent after the task began to repeat")
        self.assertIn([f"#{plan} Plan the review not changed: it repeats: 5 completes it and opens the next occurrence", False], out["toasts"])

    def test_each_undo_reverses_its_own_operation(self):
        ask = self.ids["ask"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {ask}, status: body.status, priority: 3,
  due: null, recurrence: null }}, notes: [], revision: body.status.slice(0, 1).repeat(64).replace(/[^a-f0-9]/g, 'd') }}])))"""
        # Ready, then In progress, both sent before either answered: the second Undo goes back to Ready, not Planning.
        out = self.run_page(f"""shellRun(cmd('item.status.ready'), C.get('{ask}')); shellRun(cmd('item.status.in_progress'), C.get('{ask}'));
await flush(); HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();
R.toasts = OUT.toasts.map(t => t.msg); OUT.toasts[1].undo(); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""",
                            answer, prelude="var HOLD = [];\n")
        self.assertEqual(out["R"]["toasts"][:2], [f"#{ask} Planning → Ready · sd task status {ask} ready",
                                                  f"#{ask} Ready → In progress · sd task status {ask} in_progress"])
        self.assertEqual([p[1]["status"] for p in out["posts"]], ["ready", "in_progress", "ready"])
        self.assertEqual(out["toasts"][-1], [f"Status → In progress undone · #{ask} Answer the question", False])

    def test_an_edit_undo_restores_the_fields_its_own_write_changed(self):
        ask = self.ids["ask"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {ask}, status: 'planning', priority: body.priority,
  due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        # Two P2 edits sent before either answered: the second one found P2, so its Undo sets P2, not the P3 before the first.
        out = self.run_page(f"""shellRun(cmd('item.p2'), C.get('{ask}')); shellRun(cmd('item.p2'), C.get('{ask}'));
await flush(); HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();
OUT.toasts[1].undo(); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual([p[1]["priority"] for p in out["posts"]], [2, 2, 2])
        self.assertEqual(out["toasts"][-1], [f"Edit → P2 undone · #{ask} Answer the question", False])

    def test_completing_a_repeating_task_is_confirmed_shows_the_next_occurrence_and_has_no_undo(self):
        plan = self.ids["plan"]
        row = self.row("plan")
        row["recurrence"], row["next_due"] = "FREQ=WEEKLY", "2026-09-16"
        successor = {**row, "id": 99, "status": "planning", "due": "2026-09-16", "revision": "e" * 64}
        answer = f"""(path, body) => {{ DOC.rows.push({json.dumps(successor)});
  return [200, {{ item: {{ id: {plan}, status: 'done', priority: 2, due: '2026-09-09', recurrence: null }}, notes: [],
    revision: 'b'.repeat(64), next_occurrence: 99, next_occurrence_reason: null }}]; }}"""
        out = self.run_page(f"""R.done = cmd('item.status.done').when(C.get('{plan}'));
R.complete = cmd('item.complete').when(C.get('{plan}')); R.key = cmd('item.complete').key;
shellRun(cmd('item.complete'), C.get('{plan}')); await flush(); R.listed = !!C.get('99');""", answer)
        self.assertEqual(out["R"]["done"], "it repeats: 5 completes it and opens the next occurrence")
        self.assertEqual(out["R"]["complete"], True)
        self.assertEqual(out["R"]["key"], "5")
        self.assertEqual(out["confirms"], ["item.complete"])
        self.assertEqual(out["posts"], [[f"/api/items/{plan}/status", {"status": "done", "revision": row["revision"]}, 64]])
        self.assertEqual(out["gets"], ["/api/tasks", "/api/tasks"], "the rows were not read again for the next occurrence")
        self.assertTrue(out["R"]["listed"], "the next occurrence is not listed")
        self.assertEqual(out["toasts"], [[f"#{plan} Ready → Done · next occurrence #99 due Sep 16 · sd task status {plan} done", False]])

    def test_a_queued_complete_asks_again_whether_the_task_still_repeats(self):
        # The mirror case: stop repeating is sent, and 5 is confirmed before it answers. As the completion leaves, the task
        # no longer repeats, so it would end without a next occurrence the confirm promised: it refuses.
        self.repeat_plan()
        plan = self.ids["plan"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {plan}, status: 'ready', priority: 2, due: '2026-09-09',
  recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        out = self.run_page(f"""open({plan}); await flush();
shellRun(cmd('item.recur.clear'), C.get('{plan}')); shellRun(cmd('item.complete'), C.get('{plan}')); await flush();
HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{plan}"], "the completion was sent after the series stopped")
        self.assertIn([f"#{plan} Plan the review not changed: the task does not repeat; Status → Done completes it", False], out["toasts"])

    def repeat_plan(self):
        """Make `plan` weekly in the store, and read the documents again."""
        set_item_fields(self.connection, self.ids["plan"], recurrence="FREQ=WEEKLY")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {str(i): tasks_screen.details(self.connection, i, now=NOW) for i in self.ids.values()}

    def test_a_completion_reads_the_rows_again_after_a_read_already_in_flight(self):
        # A 409 on ask starts a re-read; plan's completion lands while that read is in flight, and the server then lists
        # the next occurrence. The completion's re-read must not join the older read, which was sent before it.
        self.repeat_plan()
        plan, ask = self.ids["plan"], self.ids["ask"]
        successor = {**self.row("plan"), "id": 99, "status": "planning", "due": "2026-09-16", "recurrence": None, "revision": "e" * 64}
        answer = f"""(path, body) => path === '/api/items/{ask}/status' ? [409, {{ error: 'The item changed. Reload it.' }}]
  : (DOC.rows.push({json.dumps(successor)}), [200, {{ item: {{ id: {plan}, status: 'done', priority: 2, due: '2026-09-09', recurrence: null }},
     notes: [], revision: 'b'.repeat(64), next_occurrence: 99, next_occurrence_reason: null }}])"""
        out = self.run_page(f"""const A = ANSWER, HELD = [];
ANSWER = (path, body) => {{ if (path !== '/api/tasks') return A(path, body); const snap = JSON.parse(JSON.stringify(A(path, body)));
  return new Promise(r => HELD.push(() => r(snap))); }};
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
shellRun(cmd('item.complete'), C.get('{plan}')); await flush();
for (let i = 0; i < 4; i++) {{ HELD.splice(0).forEach(f => f()); await flush(); }}
R.listed = !!C.get('99');""", answer)
        self.assertEqual(out["gets"].count("/api/tasks"), 3, "the completion joined the read sent before it")
        self.assertTrue(out["R"]["listed"], "the next occurrence is not listed")
        self.assertIn(f"#{plan} Ready → Done · next occurrence #99 due Sep 16 · sd task status {plan} done", [t[0] for t in out["toasts"]])

    def test_exactly_one_key_5_command_is_on_for_a_repeating_and_a_plain_row(self):
        self.repeat_plan()
        plan, ask = self.ids["plan"], self.ids["ask"]
        out = self.run_page(f"""const five = k => REG.filter(c => c.key === '5' && c.when(C.get(k)) === true).map(c => c.id);
R.plan = five('{plan}'); R.ask = five('{ask}');""")
        self.assertEqual(out["R"], {"plan": ["item.complete"], "ask": ["item.status.done"]})

    def test_the_confirm_names_the_next_occurrence_before_the_write(self):
        self.repeat_plan()
        plan = self.ids["plan"]
        self.assertEqual(self.row("plan")["next_due"], "2026-09-16")
        self.assertIsNone(self.row("ask")["next_due"])
        out = self.run_page(f"R.text = cmd('item.complete').consequence(C.get('{plan}'));")
        self.assertTrue(out["R"]["text"].startswith(f"Completes #{plan} and opens the next occurrence, due Sep 16 (FREQ=WEEKLY)."), out["R"]["text"])
        self.assertEqual(out["posts"], [])
        # No next date (workflow.next_occurrence_due returned None): the completion ends the series, and both the confirm
        # and Done's off reason say so instead of promising an occurrence (review, PR #46).
        self.row("plan")["next_due"] = None
        out = self.run_page(f"R.text = cmd('item.complete').consequence(C.get('{plan}')); R.done = cmd('item.status.done').when(C.get('{plan}'));")
        self.assertEqual(out["R"]["text"], f"Completes #{plan} and ends the series: its rule (FREQ=WEEKLY) gives no next date, so no next occurrence opens. There is no Undo.")
        self.assertEqual(out["R"]["done"], "it repeats: 5 completes it and ends the series, since its rule gives no next date")

    def test_5_on_a_mixed_selection_moves_the_plain_rows_and_skips_the_repeating_ones(self):
        self.repeat_plan()
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"(path, body) => [200, {{ item: {{ id: {ask}, status: 'done', priority: 3, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""document.dispatchEvent(new CustomEvent('shell:picked', {{ detail: ['{plan}', '{ask}'] }}));
document.dispatchEvent({{ type: 'keydown', key: '5', target: El('card'), preventDefault() {{}} }}); await flush();
R.bulk = cmd('item.complete').bulk;""", answer)
        self.assertEqual(out["posts"], [[f"/api/items/{ask}/status", {"status": "done", "revision": self.row("ask")["revision"]}, 64]])
        self.assertIn([f"Skipped #{plan}: it repeats: 5 completes it and opens the next occurrence", False], out["toasts"])
        self.assertIn(["Status → Done · 1 item", True], out["toasts"])
        self.assertIsNone(out["R"].get("bulk"))

    def test_a_refused_cancel_reads_the_assignment_again(self):
        # A 409 means the assignment revision the Details hold is old; a retry would send it again unless they are read
        # again (review, PR #46).
        port = self.ids["port"]
        (asg,) = self.details[str(port)]["assignments"]
        answer = f"(path) => path === '/api/assignments/{asg['id']}/cancel' ? [409, {{ error: 'The assignment changed. Reload it.' }}] : [404, {{}}]"
        out = self.run_page(f"""open({port}); await flush();
try {{ await cmd('asg.cancel').run(C.get('asg:{asg['id']}')); }} catch (e) {{ R.err = e.message; }} await flush();
R.reads = OUT.gets.filter(g => g === '/api/tasks/{port}').length;""", answer)
        self.assertEqual(out["R"]["err"], "The assignment changed. Reload it.")
        self.assertEqual([p[:2] for p in out["posts"]], [[f"/api/assignments/{asg['id']}/cancel", {"revision": asg["revision"]}]])
        self.assertGreaterEqual(out["R"]["reads"], 2, "the Details were not read again after the refusal")

    def test_a_task_added_under_a_filter_that_hides_it_is_not_selected(self):
        # Selection stays on a visible task: a new task the filters hide is added and named, not selected (review, PR #46).
        new = {**self.row("ask"), "id": 99, "title": "Call the bank", "kind": "task", "repo": None, "repo_path": None}
        answer = f"(path) => path === '/api/items' ? (DOC.rows.push({json.dumps(new)}), [201, {{ item: {{ id: 99, title: 'Call the bank' }} }}]) : [404, {{}}]"
        out = self.run_page("""ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'kind', v: 'work' } } : null } }); await flush();
var box = document.getElementById('shift-in'); box.value = 'Call the bank';
box.listeners.keydown.forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} })); await flush();""", answer)
        self.assertIn(["Added #99 to Planning · the filters hide it; Clear all shows it", False], out["toasts"])
        self.assertNotIn("/api/tasks/99", out["gets"], "the hidden task was selected and its Details read")

    def test_the_confirm_names_the_rule_of_a_byday_series(self):
        set_item_fields(self.connection, self.ids["plan"], recurrence="FREQ=WEEKLY;BYDAY=MO,TH")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        plan = self.ids["plan"]
        out = self.run_page(f"""const o = C.get('{plan}'); R.text = cmd('item.complete').consequence(o); R.danger = cmd('item.complete').danger(o);
R.done = cmd('item.status.done').when(o);""")
        # The library walks no BYDAY rule, so the completion ends that series; the confirm names the rule and says so, on the
        # danger button, since the series ends for good (sd:2250, case 3).
        self.assertIsNone(self.row("plan")["next_due"])
        self.assertEqual(out["R"]["text"], f"Completes #{plan} and ends the series: sd cannot walk FREQ=WEEKLY;BYDAY=MO,TH, so no next occurrence opens. There is no Undo.")
        self.assertTrue(out["R"]["danger"])
        self.assertEqual(out["R"]["done"], "it repeats: 5 completes it and ends the series, since sd cannot walk its rule")

    def test_a_rule_whose_date_the_page_has_not_read_says_sd_sets_it(self):
        # sd:2250, cases 1 and 2. The row's next_due is the date the confirm names, on a plain confirm button. A readback
        # that moves the due date leaves the next date unknown until the rows are read again (the reread is held here): the
        # confirm names the valid rule and says sd sets its date.
        plan = self.ids["plan"]
        self.row("plan").update(recurrence="FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=9", next_due="2026-11-09")
        answer = f"(path, body) => [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-10', recurrence: 'FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=9' }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""const o = C.get('{plan}'); R.dated = [cmd('item.complete').consequence(o), cmd('item.complete').danger(o)];
const A = ANSWER; ANSWER = (path, body) => path === '/api/tasks' ? new Promise(() => {{}}) : A(path, body);
shellRun(cmd('item.status.in_progress'), o); await flush();
R.undated = [cmd('item.complete').consequence(o), cmd('item.complete').danger(o)];""", answer)
        rule = "FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=9"
        self.assertEqual(out["R"]["dated"], [f"Completes #{plan} and opens the next occurrence, due Nov 9 ({rule}). A move back would leave two open tasks, so there is no Undo.", False])
        self.assertEqual(out["R"]["undated"], [f"Completes #{plan} and opens the next occurrence ({rule}); sd sets its date. A move back would leave two open tasks, so there is no Undo.", False])

    def test_the_rule_parts_the_page_walks_are_the_librarys(self):
        from sd_db import recurrence

        parts = re.search(r"const PARTS = new Set\(\[([^\]]*)\]\)", TASKS_JS)
        self.assertIsNotNone(parts)
        self.assertEqual(tuple(re.findall(r"'([A-Z]+)'", parts.group(1))), recurrence.PARTS)
        # The shell's confirm colour asks the command where it declares danger(o).
        self.assertIn("danger: c.danger ? !!c.danger(o) : c.risk === 'confirm'", SHELL_JS)

    def test_work_ops_and_done_rows_have_neither_key_5_command(self):
        port, ask, plan = self.ids["port"], self.ids["ask"], self.ids["plan"]
        for row in self.doc["rows"]:
            if row["id"] == ask:
                row["status"] = "done"
            if row["id"] == plan:
                row["kind"] = "ops"
        out = self.run_page(f"""const five = k => REG.filter(c => c.key === '5' && c.when(C.get(k)) === true).map(c => c.id);
R.work = five('{port}'); R.done = five('{ask}'); R.ops = five('{plan}');""")
        self.assertEqual(out["R"], {"work": [], "done": [], "ops": []})

    def test_a_task_that_does_not_repeat_has_no_complete_occurrence(self):
        ask = self.ids["ask"]
        out = self.run_page(f"R.when = cmd('item.complete').when(C.get('{ask}')); R.done = cmd('item.status.done').when(C.get('{ask}'));")
        self.assertEqual(out["R"], {"when": "the task does not repeat; Status → Done completes it", "done": True})

    def test_the_narrow_board_is_a_carousel_with_or_without_the_other_lane(self):
        # `.board[data-other]` outranks `.board`, so the narrow-screen rule names both, or a board with an Other lane keeps
        # its six columns on a phone (review, PR #46).
        css = (V2 / "static" / "tasks.css").read_text(encoding="utf-8")
        narrow = re.search(r"^@media \(max-width: 719px\) \{\n(.*?)^\}", css, re.S | re.M).group(1)
        rule = re.search(r"^  ([^{]*)\{[^}]*grid-template-columns: none;", narrow, re.M)
        self.assertIsNotNone(rule, "the narrow block sets no carousel")
        self.assertEqual({sel.strip() for sel in rule.group(1).split(",")}, {".board", ".board[data-other]"})

    def test_a_plain_toast_clears_a_live_undo_and_the_bulk_bar(self):
        # The bar lifts both toasts one layer, and a plain message over a live Undo is one more: with all three up, the plain
        # one sits two layers above the bar's, or "Copied" covers the Undo (review, PR #46).
        css = (V2 / "static" / "shell.css").read_text(encoding="utf-8")
        layer = r"\{ bottom: calc\(var\(--space-lg\) \+ (\S+) \+ env\(safe-area-inset-bottom\)\); \}"
        offsets = {sel.strip(): size for sels, size in re.findall(r"^([^{\n]*)" + layer, css, re.M) for sel in sels.split(",")}
        self.assertEqual(offsets.get(".toast:has(~ .bulkbar:not([hidden]))"), "3.5rem")
        self.assertEqual(offsets.get(".toast:not([hidden]) ~ .toast.plain:has(~ .bulkbar:not([hidden]))"), "7rem",
                         "a plain toast over a live Undo with the bulk bar up shares the Undo's offset")

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", TASKS_JS)
        self.assertNotIn("createElement('style')", TASKS_JS)
        self.assertNotIn("setAttribute('style'", TASKS_JS)
        self.assertNotRegex(TASKS_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", TASKS_JS)), [])
        # A page-level j/k or Escape handler is drift (the shell owns them through PAGE_LIST).
        self.assertNotRegex(TASKS_JS, r"e\.key === '[jk]'")


def keys(fragment: str) -> set[int]:
    """The ids of the rows or cards a view drew."""
    return {int(found) for found in re.findall(r'data-key="(\d+)"', fragment or "")}


class TheFilterAddress(ScreenCase):
    """The query v1 /backlog took -- status, age, active, q and page -- read and written by Tasks (sd:2589)."""

    run_page = TheScript.run_page

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {}
        self.base = next(r for r in self.doc["rows"] if r["id"] == self.ids["plan"])
        self.doc["rows"] = [self.made(901, "Send the reply", "ready_to_send", "3", kind="message"),
                            self.made(902, "Budget review", "ready", "7"),
                            self.made(903, "Closed last week", "done", "0"),
                            self.made(904, "Plan the trip", "planning", "14", repo="other")]

    def made(self, number, title, status, age, **more):
        return {**self.base, "id": number, "title": title, "status": status, "age": age, **more}

    def listed(self, search, body=""):
        out = self.run_page(body + "\nR.list = ELS['view-list'].html; R.board = ELS['view-board'].html; R.filters = ELS.filters.html;",
                            search=search)
        return keys(out["R"]["list"]) | keys(out["R"]["board"]), out

    def test_only_the_sorted_header_carries_aria_sort_and_a_click_moves_it(self):
        """sd:2527: each column with an order sorts through its button; aria-sort is on the sorted th alone."""
        click = "ELS['view-list'].listeners.click[0]({ target: { closest: s => s === 'button[data-sort]' ? { dataset: { sort: 'due' } } : null } });"
        sorted_ = r'aria-sort="(\w+)"[^>]*><button class="sorter"[^>]*data-sort="(\w+)"'
        out = self.run_page(f"R.a = ELS['view-list'].html; {click} R.b = ELS['view-list'].html; {click} R.c = ELS['view-list'].html;"
                            " R.url = OUT.urls[OUT.urls.length - 1];", search="?view=list")
        for key, want in (("a", [("ascending", "p")]), ("b", [("ascending", "due")]), ("c", [("descending", "due")])):
            self.assertEqual(re.findall(sorted_, out["R"][key]), want, key)
            self.assertEqual(out["R"][key].count("aria-sort"), 1, key)
        # sd:2682: every column with an order sorts, through shell.list's header.
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["a"]), ["id", "title", "repo", "status", "p", "due"])
        self.assertIn("sort=due&dir=desc", out["R"]["url"])

    def test_status_age_and_active_narrow_as_v1_did(self):
        for search, expected in (("?view=list&status=ready_to_send", {901}), ("?view=list&age=7", {902}),
                                 ("?view=list&active=1", {901, 902, 904}), ("?view=list&active=1&age=0", set()),
                                 ("?view=list&status=ready,done", {902, 903}),
                                 # A bucket v1 would not take is dropped, as v1 dropped it.
                                 ("?view=list&age=5", {901, 902, 903, 904})):
            with self.subTest(search=search):
                self.assertEqual(self.listed(search)[0], expected)
        _, out = self.listed("?view=list&status=ready_to_send&age=3&active=1")
        for key, value in (("status", "ready_to_send"), ("age", "3"), ("active", "1")):
            self.assertIn(f'data-f="{key}" data-v="{value}" aria-pressed="true"', out["R"]["filters"])
        self.assertIn('data-v="14" aria-pressed="false">14-29d<', out["R"]["filters"])

    def test_the_text_filter_reads_q_and_the_box_writes_it(self):
        self.assertEqual(self.listed("?view=list&q=BUDGET")[0], {902})
        self.assertEqual(self.listed("?view=list&q=%23904")[0], {904})
        # The v1 capture check opens /tasks?q=<title> in the default view (static/dashboard.js).
        self.assertEqual(self.listed("?q=" + quote("Send the reply"))[0], {901})
        shown, out = self.listed("?view=list", "const box = document.getElementById('find-in'); box.value = ' Trip ';\n"
                                 "box.listeners.input[0]({ target: box }); await flush();")
        self.assertEqual(shown, {904})
        self.assertEqual(out["urls"][-1], "view=list&q=trip")

    def test_the_address_carries_every_filter(self):
        _, out = self.listed("?view=list&status=ready_to_send&age=3&active=1&q=reply")
        self.assertEqual(out["urls"][-1], "view=list&status=ready_to_send&age=3&active=1&q=reply")

    def test_the_list_pages_fifty_and_the_address_keeps_the_page(self):
        self.doc["rows"] = [self.made(1000 + n, f"row {n}", "planning", "0") for n in range(60)]
        for search, count, words in (("?view=list", 50, "1–50 of 60"), ("?view=list&page=2", 10, "51–60 of 60"),
                                     ("?view=list&page=2&size=25", 25, "26–50 of 60"), ("?view=list&page=9", 10, "51–60 of 60"),
                                     ("?view=list&page=1.5", 50, "1–50 of 60")):
            with self.subTest(search=search):
                shown, out = self.listed(search)
                self.assertEqual(len(shown), count)
                self.assertIn(words, out["R"]["list"])
        click = "ELS['view-list'].listeners.click[0]({ target: { id: '', closest: s => s === '.list-pager [data-page]' ? { dataset: { page: '2' } } : null } }); await flush();"
        shown, out = self.listed("?view=list", click)
        self.assertEqual(shown, {1000 + n for n in range(50, 60)})
        self.assertEqual(out["urls"][-1], "view=list&page=2")
        self.assertIn('data-page="2" aria-label="Page 2" aria-current="page"', out["R"]["list"])
        self.assertEqual(re.findall(r'data-size="(\d+)"', out["R"]["list"]), ["25", "50", "100", "200"])
        # A filter change starts the list at its first page again.
        chip = "ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'status', v: 'planning' } } : null } }); await flush();"
        _, out = self.listed("?view=list&page=2", chip)
        self.assertEqual(out["urls"][-1], "view=list&status=planning")
        # A short list draws its range and no page numbers; the board shows every filtered card, whatever the page.
        short = self.listed("?view=list&q=row%2059")[1]["R"]["list"]
        self.assertIn('<span class="range">1–1 of 1</span>', short)
        self.assertNotIn("data-page=", short)
        self.assertEqual(len(self.listed("?view=board&page=2")[0]), 60)

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        """sd:2682: one chip per active filter value above the list, with the count and one Clear all (shell.list)."""
        unfilter = "ELS.filters.listeners.click[0]({ target: { id: '', closest: s => s === '[data-unfilter]' ? { dataset: { unfilter: 'status:ready_to_send' } } : null } }); await flush();"
        clear = "ELS.filters.listeners.click[0]({ target: { id: '', closest: s => s === '[data-unfilter-all]' ? {} : null } }); await flush();"
        _, out = self.listed("?view=list&status=ready_to_send&active=1&q=reply",
                             f"R.chips = ELS.filters.html; {unfilter} R.chips2 = ELS.filters.html; R.url2 = OUT.urls[OUT.urls.length - 1];"
                             f" {clear} R.chips3 = ELS.filters.html; R.url3 = OUT.urls[OUT.urls.length - 1]; R.box = ELS['find-in'].value;")
        chips = lambda key: re.findall(r'data-unfilter="([\w:]+)"', out["R"][key])
        self.assertEqual(chips("chips"), ["status:ready_to_send", "active:1", "q"])
        self.assertIn("3 filters · 1 of 4", out["R"]["chips"])
        self.assertEqual(chips("chips2"), ["active:1", "q"])
        self.assertEqual(out["R"]["url2"], "view=list&active=1&q=reply")
        self.assertEqual((chips("chips3"), out["R"]["url3"], out["R"]["box"]), ([], "view=list", ""))

    def test_a_linked_row_opens_the_page_that_holds_it(self):
        self.doc["rows"] = [self.made(1000 + n, f"row {n}", "planning", "0") for n in range(60)]
        shown, _ = self.listed("?view=list&row=1055")
        self.assertIn(1055, shown)


class TheOperationsBars(ScreenCase):
    """Operations' age histogram opens Tasks (sd:2589): follow every bar into tasks.js and compare the rows it lists."""

    run_page = TheScript.run_page
    DAYS = (0, 2, 5, 10, 20, 40, 90)

    def setUp(self):
        super().setUp()
        for days in self.DAYS:
            self.age(self.item(f"aged {days} days", status="planning"), days)
        for days in (5, 20):
            row = self.item(f"unsent for {days} days", status="planning")
            transition(self.connection, row, "ready_to_send", who="sd-ship")
            self.age(row, days)
        # Done this week, so /api/tasks lists it: a bar counts open items only, and its link says active=1.
        self.age(self.item("recently completed", status="done"), 5)
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {}

    def bars(self):
        page = self.render("/operations", {"area": ["progress"]})
        found = []
        for opening in re.findall(r"<a\s[^>]*>", page):
            href, age, series = (re.search(rf'{name}="([^"]*)"', opening) for name in ("href", "data-age", "data-series"))
            if href and age and series:
                found.append((html.unescape(href.group(1)), age.group(1), series.group(1)))
        return found

    def test_every_bar_lists_exactly_its_own_rows_on_tasks(self):
        bars = self.bars()
        self.assertGreaterEqual(len(bars), 5, "the histogram drew almost no bars")
        rows = reads.backlog_items(self.connection, now=NOW)
        for href, key, series in bars:
            with self.subTest(bucket=key, series=series):
                parts = urlsplit(href)
                self.assertEqual(parts.path, "/tasks")
                expected = {row["id"] for row in rows if row["status"] != "done" and reads.age_bucket(row, now=NOW) == key
                            and (series != "ready_to_send" or row["status"] == "ready_to_send")}
                self.assertTrue(expected, "a bar was drawn for a bucket holding no rows")
                out = self.run_page("R.list = ELS['view-list'].html;", search="?" + parts.query)
                self.assertEqual(keys(out["R"]["list"]), expected)


class TheLinksIntoTasks(unittest.TestCase):
    """Operations, the v1 capture check (sd:2589) and the Skills page's Run with agent (sd:2590) open Tasks, not /backlog.

    sd:2356 retires /backlog itself.
    """

    def test_no_link_opens_a_backlog_query(self):
        root = V2.parent
        found = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".js", ".html"):
                hits = re.findall(r"""["']/backlog\?""", path.read_text(encoding="utf-8"))
                if hits:
                    found[path.relative_to(root).as_posix()] = len(hits)
        self.assertEqual(found, {})


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "tasks", "Tasks", "/tasks", ("/api/tasks", "/api/tasks/1")


if __name__ == "__main__":
    unittest.main()
