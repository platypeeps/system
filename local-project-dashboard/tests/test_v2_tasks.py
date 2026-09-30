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

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from sd_db import set_item_fields, workflow
from sd_dashboard import server, tasks_screen, v2

from support import NOW, ScreenCase, upsert_shadow
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
TASKS_JS = (V2 / "static" / "tasks.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")


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

    def test_an_assignment_carries_its_queue_revision_and_cancel_capability(self):
        from sd_db import operations, runner

        got = tasks_screen.details(self.connection, self.ids["port"], now=NOW)
        (row,) = got["assignments"]
        self.assertEqual((row["role"], row["status"]), ("author", "running"))
        self.assertEqual(row["revision"], runner.queue_state(self.connection, row["id"])["revision"])
        self.assertEqual(row["cancel"], operations.assignment_state(self.connection, row["id"])["capabilities"]["cancel"])
        self.assertIsNone(got["external"])
        self.assertEqual(got["item"]["path"], "docs/work/port/prd.md")

    def test_urgency_beyond_the_due_date_comes_from_reads_is_urgent(self):
        report = self.item("Look at the failed run", kind="report", fields={"attention": True})
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertTrue(rows[report]["urgent_otherwise"])
        self.assertFalse(rows[self.ids["plan"]]["urgent_otherwise"], "a due date is the page's own rule, not sent")

    def test_details_carry_run_readiness_and_runner_capabilities(self):
        from sd_db import runner_controls

        got = tasks_screen.details(self.connection, self.ids["ask"], now=NOW)
        ready = runner_controls.readiness(self.connection, self.ids["ask"])
        self.assertEqual(got["run"], {"allowed": ready["allowed"], "reason": ready["reason"]})
        self.assertFalse(got["run"]["allowed"])
        (row,) = tasks_screen.details(self.connection, self.ids["port"], now=NOW)["assignments"]
        self.assertEqual(row["runner"], {
            "requeue": {"allowed": False, "reason": "the assignment is running, not blocked or cancelled"},
            "cancel": {"allowed": False, "reason": "a legacy running assignment has no owned runner attempt to stop"}})

    def test_runner_capabilities_follow_the_runner_rules(self):
        caps = tasks_screen._runner_capabilities
        run = lambda **k: {"released_at": None, "cancel_requested": None, **k}
        self.assertTrue(caps({"role": "author", "status": "blocked", "run": None})["requeue"]["allowed"])
        self.assertTrue(caps({"role": "author", "status": "cancelled", "run": run(released_at="x")})["requeue"]["allowed"])
        self.assertFalse(caps({"role": "author", "status": "blocked", "run": run()})["requeue"]["allowed"])
        self.assertFalse(caps({"role": "exec", "status": "blocked", "run": None})["requeue"]["allowed"])
        self.assertTrue(caps({"role": "author", "status": "queued", "run": None})["cancel"]["allowed"])
        self.assertTrue(caps({"role": "author", "status": "running", "run": run()})["cancel"]["allowed"])
        self.assertFalse(caps({"role": "author", "status": "running", "run": run(cancel_requested="x")})["cancel"]["allowed"])

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
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "tasks.js", "shell.js"])
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
  this.get = k => m.has(k) ? m.get(k) : null; this.set = (k, v) => m.set(k, String(v)); }
function El(id) { var e = { id: id, html: null, hidden: false, dataset: {}, style: {}, listeners: {}, value: '',
  classList: { add() {}, remove() {}, toggle() {} },
  addEventListener(t, f) { (e.listeners[t] = e.listeners[t] || []).push(f); }, setAttribute() {}, removeAttribute() {},
  toggleAttribute() {}, getAttribute() { return null; }, querySelector() { return null; }, querySelectorAll() { return []; },
  focus() {}, closest() { return null; }, matches() { return false; }, showModal() {}, remove() {},
  replaceChildren(f) { e.html = f ? f.parsed : ''; }, append(f) { e.html = (e.html || '') + (f && f.parsed || ''); }, prepend() {}, before() {} };
  return e; }
var ELS = {}, DOC_LISTENERS = {}, WIN_LISTENERS = {};
var document = { body: El('body'),
  getElementById(id) { return ELS[id] = ELS[id] || El(id); },
  querySelector(sel) { return /sd-csrf/.test(sel) ? { content: 'c'.repeat(64) } : null; },
  querySelectorAll() { return []; },
  createElement(tag) { if (tag !== 'template') return El(tag);
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
SHELL = r"""
var REG = [], OBJ = new Map();
var C = { register: (...cs) => { REG.push(...cs); }, put: o => { OBJ.set(o.id, o); }, get: id => OBJ.get(id), select() {}, pick() {},
  rowActions: () => mk``, bar: () => mk`<div class="bar"></div>` };
function shellRun(c, o) {
  var off = c.when ? c.when(o) : true;
  if (off !== true) { OUT.toasts.push({ msg: 'off: ' + off }); return; }
  if (c.risk === 'confirm') OUT.confirms.push(c.id);
  var msg = c.run ? c.run(o) : '';
  if (msg === null) return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id, form: true } }));
  OUT.toasts.push({ msg: msg || c.label, undo: c.risk === 'undo' && c.undo ? () => c.undo(o) : null });
  document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } }));
}
C.run = shellRun;
// shell.js's bulk bar: one run per picked object in the same tick, then the shell's own toast whose Undo undoes every pick.
function shellBulk(c, objs) {
  objs.forEach(o => c.run(o));
  OUT.toasts.push({ msg: `${c.label} · ${objs.length} items`, undo: c.risk === 'undo' && c.undo ? () => objs.forEach(o => c.undo(o)) : null });
  document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: 'bulk' } }));
}
window.shell = { commands: C, ICON: () => mk`<svg></svg>`, toast: (msg, undo) => OUT.toasts.push({ msg: msg, undo: undo || null }),
  suggest() {}, openPane() {}, url() {}, chording: () => false, reconcile() {}, views() {}, capture() {},
  state: s => OUT.states.push(s), attention: a => OUT.attention.push(a) };
const cmd = id => REG.find(c => c.id === id);
const flush = async () => { for (let i = 0; i < 400; i++) await null; };
const open = key => document.dispatchEvent(new CustomEvent('shell:open', { detail: String(key) }));
const lastToast = () => OUT.toasts[OUT.toasts.length - 1];
const lastUndo = () => OUT.toasts.filter(t => t.undo).pop();
"""


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

    def run_page(self, body, answer="null", *, prelude="", env=None):
        """Load tasks.js, fire DOMContentLoaded, run `body` (async), and return OUT plus what `body` set on R."""
        script = (prelude + STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + f"\nconst DOC = {json.dumps(self.doc)}, DETAILS = {json.dumps(self.details)};\n"
                  + "const WRITE = " + answer + ";\nvar DETAIL = null;\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/tasks') return [200, DOC];
  var m = path.match(/^\\/api\\/tasks\\/(\\d+)$/); if (m) return typeof DETAIL === 'function' ? DETAIL(m[1]) : [200, DETAILS[m[1]]];
  return WRITE ? WRITE(path, body) : [404, { error: 'no answer' }];
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
            ["item.run", "item", "undo", "r", None, True, True],
            ["item.delete", "item", "confirm", None, None, False, False],
            ["work.relink", "item", "safe", "l", False, True, False],
            ["work.cancel", "item", "confirm", "w", False, True, False],
            ["item.recur", "item", "undo", None, None, True, True],
            ["item.recur.clear", "item", "undo", None, None, True, True],
            ["note.resolve", "note", "confirm", "v", None, True, False],
            ["asg.requeue", "assignment", "undo", "q", None, True, True],
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
        self.assertEqual(out["toasts"][1], [f"Status → Ready undone · #{ask}", False])

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
        self.assertEqual(out["toasts"], [["not changed: The item changed. Reload it.", False]])

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

    def test_relink_and_cancel_are_copy_only_lines(self):
        port, plan = self.ids["port"], self.ids["plan"]
        out = self.run_page(f"""open({port}); await flush();
R.relink = [cmd('work.relink').when(C.get('{port}')), cmd('work.relink').cli(C.get('{port}'))];
R.cancel = [cmd('work.cancel').when(C.get('{port}')), cmd('work.cancel').cli(C.get('{port}')), cmd('work.cancel').when(C.get('{plan}'))];""")
        self.assertEqual(out["R"]["relink"], [True, f"sd work relink {port} <moved path>"])
        self.assertEqual(out["R"]["cancel"], [True, f"sd work cancel {port} --reason '<why>'",
                                              "sd work cancel acts on a work item; this is a task"])
        self.assertEqual(out["posts"], [])

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
R.run = cmd('item.run').when(C.get('{ask}'));
R.recur = cmd('item.recur').when(C.get('{port}'));
R.requeue = cmd('asg.requeue').when(C.get('asg:{asg["id"]}'));
R.cancel = cmd('asg.cancel').when(C.get('asg:{asg["id"]}'));""")
        self.assertEqual(out["R"], {
            "p2": "it is already P2",
            "run": self.details[str(ask)]["run"]["reason"],
            "recur": "a work item cannot recur",
            "requeue": "the assignment is running, not blocked or cancelled",
            "cancel": "a legacy running assignment has no owned runner attempt to stop"})
        self.assertTrue(out["R"]["run"])

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

    def test_the_matrix_places_by_reads_is_urgent_and_each_quadrant_owns_its_options(self):
        ask = self.ids["ask"]
        for row in self.doc["rows"]:
            if row["id"] == ask:
                row["urgent_otherwise"] = True
        out = self.run_page("""document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'matrix' })); await flush();
R.matrix = ELS['view-matrix'].html;""")
        quads = dict(re.findall(r'data-q="(\w+)"(.*?)(?=data-q="|$)', out["R"]["matrix"], re.S))
        self.assertIn("Answer the question", quads["delegate"])
        self.assertEqual(out["R"]["matrix"].count('role="listbox"'), 4)
        # Every card (role="option") sits in a quadrant's drop list, and every drop list is a listbox.
        self.assertEqual(out["R"]["matrix"].count('<div class="drop" role="listbox"'), 4)
        self.assertNotIn('<div class="drop">', out["R"]["matrix"])

    def test_a_bulk_undo_reverses_only_the_writes_that_landed(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{ask}/status' && body.status === 'in_progress'
  ? [409, {{ error: 'The item changed. Reload it.' }}]
  : [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        # The Undo is taken at once, before any write answered: it must wait, and skip the refused one.
        out = self.run_page(f"""shellBulk(cmd('item.status.in_progress'), [C.get('{plan}'), C.get('{ask}')]);
lastUndo().undo(); await flush();""", answer)
        self.assertEqual([p[:2] for p in out["posts"]], [
            [f"/api/items/{plan}/status", {"status": "in_progress", "revision": self.row("plan")["revision"]}],
            [f"/api/items/{ask}/status", {"status": "in_progress", "revision": self.row("ask")["revision"]}],
            [f"/api/items/{plan}/status", {"status": "ready", "revision": "b" * 64}],
        ])
        self.assertIn(f"Status → In progress undone · 1 of 2 reversed · not reversed: #{ask} (its change did not land)", [t[0] for t in out["toasts"]])

    def test_a_bulk_failure_toast_offers_undo_for_the_landed_writes_only(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{ask}/status'
  ? [409, {{ error: 'The item changed. Reload it.' }}]
  : [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        out = self.run_page(f"""shellBulk(cmd('item.status.blocked'), [C.get('{plan}'), C.get('{ask}')]); await flush();
R.gets = OUT.gets.length; lastUndo().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["gets"], 2, "the refused write did not read the rows again")
        self.assertIn(["1 of 2 not changed: The item changed. Reload it. · Undo reverses the 1 that landed", True], out["toasts"])
        self.assertEqual([p[0] for p in out["posts"]].count(f"/api/items/{ask}/status"), 1, "Undo wrote the refused item")
        # The stand-in reads back the seeded rows, so the Undo sends the seeded revision; the real read sends the new one.
        self.assertEqual([out["posts"][-1][0], out["posts"][-1][1]["status"]], [f"/api/items/{plan}/status", "ready"])

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
        self.assertEqual(out["toasts"][-1], [f"Status → In progress undone · #{ask}", False])

    def test_an_edit_undo_restores_the_fields_its_own_write_changed(self):
        ask = self.ids["ask"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {ask}, status: 'planning', priority: body.priority,
  due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        # Two P2 edits sent before either answered: the second one found P2, so its Undo sets P2, not the P3 before the first.
        out = self.run_page(f"""shellRun(cmd('item.p2'), C.get('{ask}')); shellRun(cmd('item.p2'), C.get('{ask}'));
await flush(); HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();
OUT.toasts[1].undo(); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual([p[1]["priority"] for p in out["posts"]], [2, 2, 2])
        self.assertEqual(out["toasts"][-1], [f"Edit → P2 undone · #{ask}", False])

    def test_completing_a_repeating_task_is_confirmed_shows_the_next_occurrence_and_has_no_undo(self):
        plan = self.ids["plan"]
        row = self.row("plan")
        row["recurrence"] = "FREQ=WEEKLY"
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

    def repeat_plan(self):
        """Make `plan` weekly in the store, and read the documents again."""
        set_item_fields(self.connection, self.ids["plan"], recurrence="FREQ=WEEKLY")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {str(i): tasks_screen.details(self.connection, i, now=NOW) for i in self.ids.values()}

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
        self.row("plan")["next_due"] = None
        out = self.run_page(f"R.text = cmd('item.complete').consequence(C.get('{plan}'));")
        self.assertTrue(out["R"]["text"].startswith(f"Completes #{plan} and opens the next occurrence (FREQ=WEEKLY)."), out["R"]["text"])

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

    def test_a_task_that_does_not_repeat_has_no_complete_occurrence(self):
        ask = self.ids["ask"]
        out = self.run_page(f"R.when = cmd('item.complete').when(C.get('{ask}')); R.done = cmd('item.status.done').when(C.get('{ask}'));")
        self.assertEqual(out["R"], {"when": "the task does not repeat; Status → Done completes it", "done": True})

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", TASKS_JS)
        self.assertNotIn("createElement('style')", TASKS_JS)
        self.assertNotIn("setAttribute('style'", TASKS_JS)
        self.assertNotRegex(TASKS_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", TASKS_JS)), [])
        # A page-level j/k or Escape handler is drift (the shell owns them through PAGE_LIST).
        self.assertNotRegex(TASKS_JS, r"e\.key === '[jk]'")


if __name__ == "__main__":
    unittest.main()
