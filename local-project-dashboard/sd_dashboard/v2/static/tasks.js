// Tasks (sd:2124): the design source's products/system/designs/v2/tasks.js at d82daa1, ported. Each change from the reference is marked
// "build:". The rows are /api/tasks and the Details reading /api/tasks/<id> (tasks_screen.py), never sample data. A command
// that executes posts to the route v1 already answers, and its toast comes after the write lands, not before.
// Palette "This page" group. shell.js reads it at start, so it is set before the shell runs.
const { html, put, plural } = window.markup;
const tasksView = v => () => document.dispatchEvent(new CustomEvent('tasks:view', { detail: v }));
window.PAGE_COMMANDS = [
  { label: 'Add or find a task', icon: 'plus', run: () => document.getElementById('shift-in').focus() },
  { label: 'Show the list', icon: 'list-todo', key: 'v l', run: tasksView('list') },
  { label: 'Show the board', icon: 'kanban', key: 'v b', run: tasksView('board') },
  { label: 'Show the matrix', icon: 'grid-2x2', key: 'v m', run: tasksView('matrix') },
];
addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest, openPane } = window.shell;
  // build: today is the viewer's day, not the mockup's observation day.
  const DAY = 86400000;
  const now0 = new Date(), TODAY = new Date(now0.getFullYear(), now0.getMonth(), now0.getDate());
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  // Calendar days, not 24-hour steps: across a DST change a fixed step lands on the wrong local date (review, PR #46).
  const plus = n => iso(new Date(TODAY.getFullYear(), TODAY.getMonth(), TODAY.getDate() + n));
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const WD = ['sun','mon','tue','wed','thu','fri','sat'];
  const fmt = s => { const d = new Date(s + 'T00:00'); return `${MON[d.getMonth()]} ${d.getDate()}`; };

  // Statuses: sd_db.workflow.TASK_STATUSES, in this order. Keys 1–5 follow it.
  const STATUSES = [['planning', 'Planning'], ['ready', 'Ready'], ['in_progress', 'In progress'], ['blocked', 'Blocked'], ['done', 'Done']];
  const SLABEL = Object.fromEntries(STATUSES);
  const slabel = s => SLABEL[s] || String(s || '').replace(/_/g, ' '); // build: a message item's ready_to_send has no column
  const QUADS = [
    ['do', 'Do', true, true, 'urgent · important'],
    ['schedule', 'Schedule', false, true, 'not urgent · important'],
    ['delegate', 'Delegate', true, false, 'urgent · not important'],
    ['drop', 'Drop', false, false, 'not urgent · not important'],
  ];

  // ---------- Data (build) ----------
  // /api/tasks rows become the reference's task shape: p is priority, repo the short label v1 shows, key the id.
    let tasks = [], READ = null, AGES = [];
  const shape = r => ({ id: r.id, key: String(r.id), title: r.title, repo: r.repo || 'no repo', repo_path: r.repo_path, p: r.priority, due: r.due,
    status: r.status, kind: r.kind, urgent: !!r.urgent, urgentOtherwise: !!r.urgent_otherwise, assignment: r.assignment, age: r.age, recurrence: r.recurrence, anchor: r.recurrence_anchor ?? null, nextDue: r.next_due ?? null, revision: r.revision, allowed: r.allowed, edit: r.edit, run: r.run, real: true });
  const byKey = k => tasks.find(t => t.key === k);
  const label = t => t.id ? `#${t.id}` : (t.kind === 'ops' ? 'ops' : 'no id');
  // A write readback is workflow.item_state: fold its item into the row, so the row updates where it is.
  function absorb(key, state) {
    const t = byKey(key); if (!t || !state?.item) return;
    if (t.due !== state.item.due || t.recurrence !== state.item.recurrence) t.nextDue = undefined; // not known until the rows are read again
    const dueMoved = t.due !== state.item.due;
    Object.assign(t, { status: state.item.status, p: state.item.priority, due: state.item.due, recurrence: state.item.recurrence,
      anchor: state.item.recurrence_anchor ?? null, revision: state.revision });
    // The server's urgency is is_urgent, and urgent_otherwise is is_urgent without its due rule; after a due edit the page
    // applies that rule to the new date until the rows are read again.
    if (dueMoved) t.urgent = t.urgentOtherwise || withinWeek(t);
    staleDet(t.id);
    if (selected === key) readDet(t.id); // the open Details read again, so they show the write
  }

  const days = t => t.due ? Math.round((new Date(t.due + 'T00:00') - TODAY) / DAY) : null;
  // Urgency is the server's reads.is_urgent decision for the row (review, PR #50): due within 7 days, a message ready to
  // send for over 3 days, or a report that needs attention. withinWeek is its due rule, for a due edit before the re-read
  // and for the ≤ 7 days filter: (due at UTC midnight − the document's read stamp).days <= 7, timedelta.days flooring. Not
  // the viewer's calendar days, which put a task in another quadrant than the server's until the next read (review, PR #46).
  const withinWeek = t => t.due !== null && t.due !== undefined
    && Math.floor((Date.parse(t.due + 'T00:00:00Z') - Date.parse(READ || new Date().toISOString())) / DAY) <= 7;
  const urgent = t => t.overdue || t.urgent;
  const important = t => t.p !== null && t.p !== undefined && t.p <= 2;
  const quadOf = t => QUADS.find(q => q[2] === urgent(t) && q[3] === important(t))[0];
  function state(t) {
    if (t.status === 'done') return '';
    if (t.overdue || (t.due && days(t) < 0)) return 'warning';
    if (t.kind === 'ops') return 'warning';
    if (t.due && days(t) <= 2) return 'caution';
    return '';
  }
  const GLYPH = { warning: '■', caution: '▲' };

  // Page attention: the worst lit state among open rows, and how many rows carry it. build: set after each read.
  function attention() {
    if (!READ) { window.shell.attention?.({ state: 'unknown', n: 0, what: 'tasks not read' }); return; }
    const lit = tasks.filter(t => t.real).map(t => [t, state(t)]).filter(([, st]) => st);
    const worst = lit.some(([, st]) => st === 'warning') ? 'warning' : lit.length ? 'caution' : 'ok';
    const hits = lit.filter(([, st]) => st === worst).map(([t]) => t);
    const ops = hits.filter(t => t.kind === 'ops').length;
    const what = worst === 'ok' ? 'nothing due' : worst === 'caution' ? 'tasks due within 2 days'
      : ops === hits.length ? 'failed syncs' : ops ? 'overdue tasks and failed syncs' : 'overdue tasks';
    window.shell.attention?.({ state: worst, n: hits.length, what });
  }
  function dueText(t) {
    if (t.overdue && !t.due) return ['overdue', 'warning'];
    if (!t.due) return ['no due', ''];
    const n = days(t);
    if (n < 0) return [`${-n}d overdue`, 'warning'];
    if (n === 0) return ['due today', 'caution'];
    if (n === 1) return ['due tomorrow', 'caution'];
    return [`due ${fmt(t.due)}`, n <= 2 ? 'caution' : ''];
  }

  // Legal moves: sd_db.workflow.allowed_statuses. build: the row carries what the library allows (t.allowed); the reasons
  // name why a move is off, in the reference's words where it had them.
  function legal(t, to) {
    if (t.status === to) return { ok: false, reason: 'Already here.' };
    if (t.kind === 'ops') return { ok: false, reason: 'An ops row has no status to move. It clears when the next sync succeeds.' };
    if (t.assignment === 'running') return { ok: false, reason: 'A runner assignment is running. Status is locked until it ends.' };
    if (t.assignment === 'queued') return { ok: false, reason: 'A runner assignment is queued. Status is locked until it ends.' };
    if (t.kind === 'work' && to === 'done') return { ok: false, reason: 'Work items close through verified completion (sd work), not a status write.' };
    if (!(t.allowed || []).includes(to)) return { ok: false, reason: t.allowed?.length ? `sd task status refuses ${slabel(to)} for this ${t.kind} item.` : `A ${t.kind} item takes no status write here; it has its own workflow.` };
    return { ok: true };
  }
  function quadChange(t, q) {
    const Q = QUADS.find(x => x[0] === q);
    if (t.kind === 'ops') return { ok: false, reason: 'An ops row has no priority or due date to set.' };
    if (!EDITABLE(t)) return { ok: false, reason: noEdit(t) };
    if (quadOf(t) === q) return { ok: false, reason: 'Already here.' };
    if (!Q[2] && t.urgentOtherwise) return { ok: false, reason: 'It is urgent for a reason a due date does not change (a waiting message or a report that needs attention).' };
    const ch = {}, words = [];
    if (Q[3] && !important(t)) { ch.p = 2; words.push(`priority ${t.p ? 'P' + t.p : 'unset'}→P2`); }
    if (!Q[3] && important(t)) { ch.p = 3; words.push(`priority P${t.p}→P3`); }
    // Urgency is derived from the due date, so crossing it means choosing a date. Never invent or erase one silently.
    let needsDate = null;
    if (Q[2] && !urgent(t)) { needsDate = 'into'; words.push('you pick a due date within 7 days'); }
    if (!Q[2] && urgent(t)) { needsDate = 'out'; words.push(`you pick a later due date${t.due ? ` (now ${fmt(t.due)})` : ''}`); }
    return { ok: true, ch, words, needsDate };
  }
  // build: the row carries what sd_db.workflow.edit_item takes (tasks_screen._edit_capability): a kind in DETAIL_KINDS, and
  // for a work item a repository whose status_source is row. A file-owned work row's every edit fails (review, PR #46).
  const EDITABLE = t => !!t?.edit?.allowed;
  const noEdit = t => t.edit?.reason || `a ${t.kind} item uses its own editing workflow`;
  // build: sd_db.workflow.TASK_STATUS_KINDS; workflow._recurring refuses a rule on any other kind.
  const RECURS = ['task', 'personal', 'followup'];
  // build (sd:3012): sd_db.progress.CANCELLABLE_TASK_KINDS, the kinds task_guard lets sd task cancel close.
  const CLOSES = ['task', 'followup'];
  const repeats = t => !!t?.recurrence;
  // build (sd:2250): sd stores a rule only from sd_db.recurrence.PARTS. A rule with any other part (BYDAY), written around
  // the library, cannot be walked: completing it ends the series. test_v2_tasks holds this set to the library's.
  const PARTS = new Set(['FREQ', 'INTERVAL', 'BYMONTH', 'BYMONTHDAY']);
  const walkable = t => String(t.recurrence || '').replace(/^RRULE:/i, '').split(';').every(p => PARTS.has(p.split('=')[0].trim().toUpperCase()));
  // A completion that opens nothing: a rule sd cannot walk, or one whose next date workflow.next_occurrence_due says is none.
  const ends = t => !walkable(t) || t.nextDue === null;
  const cliMove = (t, to) => t.id ? `sd task status ${t.id} ${to}` : '';
  function cliQuad(t, ch) {
    if (!t.id) return '';
    const a = [];
    if ('p' in ch) a.push(`--priority ${ch.p}`);
    if ('due' in ch) a.push(ch.due ? `--due ${ch.due}` : '--clear-due');
    return `sd task edit ${t.id} ${a.join(' ')}`;
  }

  // ---------- Writes (build) ----------
  // One write per task at a time: each waits for the one before it, then sends the row's revision as it is then. A stale
  // revision (409) re-reads the rows. `sent` sees the row as the write leaves: the state this write changes, after every
  // write queued before it (review, PR #46). `check` asks again, as the write leaves, whether the command is still on: a
  // write queued behind another chose its command before that one landed (review round 3). `rev` is an Undo's: the revision
  // its own write answered. A row whose revision moved since, by a re-read or by any other write, refuses the Undo, so it
  // never overwrites a change it did not make (review round 3). A landed write rereads the rows (shell.read's barrier);
  // `wait` holds the write's promise until that read ends, for a toast that names what the read brings.
  const CHANGED = 'it changed after this write, and Undo would overwrite that change';
  const pending = new Map();
  function write(key, path, body, { sent, check, rev, wait } = {}) {
    const p = (pending.get(key) || Promise.resolve()).then(() => { const t = byKey(key); if (!t) throw new Error('the task is no longer listed');
      if (rev !== undefined && t.revision !== rev) throw new Error(CHANGED);
      const on = check ? check(t) : true; if (on !== true) throw new Error(on);
      sent?.(t); return window.shell.post(path(t), { ...body, revision: t.revision }); })
      .then(out => { absorb(key, out); render(); const again = reread(); return wait ? again.then(() => out) : out; });
    pending.set(key, p.catch(() => {}));
    return p;
  }
  // The item edit route takes workflow field names: priority, due, recurrence, recurrence_anchor.
  const edit = (key, ch, opts) => write(key, t => `/api/items/${t.id}`, Object.fromEntries(Object.entries(ch).map(([k, v]) => [k === 'p' ? 'priority' : k, v])), opts);
  const moveTo = (key, to, opts) => write(key, t => `/api/items/${t.id}/status`, { status: to }, opts);
  function failed(name, err) { toast(`${name ? name + ' ' : ''}not changed: ${err.message}`); }
  // A refused stale write loads the rows again (load(), not reread(): only a reread says a change landed), once however
  // many writes in a group were refused: a refusal joins the load it started. A landed write's reread never joins a read
  // sent before it landed (read.js), so a completion's next occurrence is never missed (review, PR #46). The open Details
  // are read again with the rows: a conflict means the item changed elsewhere, and they show it (review, PR #46).
  let refusing = null;
  function refused() {
    if (refusing) return refusing;
    const p = refusing = load().then(ok => { const id = ok && byKey(selected)?.id; if (id) redrawDet(id); })
      .finally(() => { if (refusing === p) refusing = null; });
    return p;
  }
  // ---------- Undo (build, review of PR #46 and #50) ----------
  // A command's run returns landing(...): the shell contract in shell.js (bulk:start) waits for it, toasts its text, and
  // offers Undo only when it landed, for a bulk group too. The Undo it resolves to belongs to this operation: `inverse` gets
  // what the write answered and reverses this write, no later one, once, with the revision that answer carried. A write
  // that failed has no Undo, so a refused row is never written again with the revision the re-read brought.
  function landing(promise, text, inverse) {
    return promise.then(v => {
      let spent = false;
      const undo = inverse && (() => { if (spent) return Promise.resolve(false); spent = true;
        return inverse(v).then(() => true, err => { if (err.stale) refused(); throw err; }); });
      return { text: text(v), undo };
    }, err => { if (err.stale) refused(); throw err; });
  }
  // The command's undo: the shell passes what the run answered.
  const undoOf = (o, r) => r && r.undo ? r.undo() : false;

  // ---------- Filters ----------
  // build: Kind joins Repo, Priority and Due, as v1 /backlog filters by kind and repo (review 2026-09-29, item 17).
  // build (sd:2589): Status, In status (the reads.age_bucket key each row carries), Open only, the text filter and the list's
  // page take the query v1 /backlog took (status, age, active=1, q, page), so Operations' histogram bars and the capture
  // check land here with their filter.
  const F = { kind: new Set(), repo: new Set(), p: new Set(), due: new Set(), status: new Set(), age: new Set(), active: new Set() };
  const FKEYS = Object.keys(F);
  const find = document.getElementById('find-in');
  let Q = '';
  const on = () => FKEYS.reduce((n, k) => n + F[k].size, 0) + (Q ? 1 : 0);
  const dueBucket = t => t.overdue || (t.due && days(t) < 0) ? 'overdue' : !t.due ? 'none' : withinWeek(t) ? 'week' : 'later';
  const DUE_OPTS = [['overdue', 'overdue'], ['week', '≤ 7 days'], ['later', 'later'], ['none', 'no due']];
  function renderFilters() {
    const repos = [...new Set(tasks.map(t => t.repo))].sort(), kinds = [...new Set(tasks.map(t => t.kind))].sort();
    // The board's statuses, then any other a row or the address names (ready_to_send has no column), so a chip shows it.
    const statuses = [...STATUSES.map(([st]) => st), ...new Set([...tasks.map(t => t.status), ...F.status].filter(st => !SLABEL[st]))];
    const grp = (key, name, opts, help) => html`<div class="fgroup"><span class="label">${name}</span><div class="chips">${opts.map(([v, l]) => html`<button type="button" class="chip" data-f="${key}" data-v="${v}" aria-pressed="${String(F[key].has(v))}">${l}</button>`)}</div>${help || ''}</div>`;
    put(document.getElementById('filters'), html`${grp('kind', 'Kind', kinds.map(k => [k, k]))}${grp('repo', 'Repo', repos.map(r => [r, r]))}${
      grp('p', 'Priority', [['1', 'P1'], ['2', 'P2'], ['3', 'P3'], ['4', 'P4'], ['', 'unset']])}${
      grp('due', 'Due', DUE_OPTS, html`<button class="help" type="button" aria-label="Help: due filter" data-help="Buckets count from today. <b>≤ 7 days</b> is the same window the matrix calls urgent. Chips in one group widen the view; groups narrow it.">${ICON('circle-help')}</button>`)}${
      grp('status', 'Status', statuses.map(st => [st, slabel(st)]))}${
      grp('age', 'In status', AGES.map(a => [a.key, a.label]), html`<button class="help" type="button" aria-label="Help: in status filter" data-help="Days since the task's last status change, in the buckets the Operations Progress histogram counts (<code>reads.age_bucket</code>). A bar there opens its bucket here.">${ICON('circle-help')}</button>`)}${
      grp('active', 'Scope', [['1', 'open only']], html`<button class="help" type="button" aria-label="Help: scope filter" data-help="<b>open only</b> hides Done tasks, as the Operations histogram counts only open ones.">${ICON('circle-help')}</button>`)}${
      (on() ? window.shell.list.chips(activeFilters(), visible().length, tasks.length) : html`<div class="fsum">${ICON('filter')}<span>${tasks.length} tasks</span></div>`)}`);
  }
  // Active filters above the list (shell.list, sd:2682): one chip per value, keyed field:value, and one for the text.
  const FNAME = { kind: 'Kind', repo: 'Repo', p: 'Priority', due: 'Due', status: 'Status', age: 'In status', active: 'Scope' };
  const fvalue = (k, v) => k === 'p' ? (v ? 'P' + v : 'unset') : k === 'due' ? DUE_OPTS.find(o => o[0] === v)?.[1] || v : k === 'status' ? slabel(v)
    : k === 'age' ? AGES.find(a => a.key === v)?.label || v : k === 'active' ? 'open only' : v;
  const activeFilters = () => [...FKEYS.flatMap(k => [...F[k]].map(v => ({ key: `${k}:${v}`, label: `${FNAME[k]}: ${fvalue(k, v)}` }))), ...(Q ? [{ key: 'q', label: `Text: ${Q}` }] : [])];
  document.getElementById('filters').addEventListener('click', e => {
    const c = e.target.closest('[data-f]');
    if (c) { const s = F[c.dataset.f]; s.has(c.dataset.v) ? s.delete(c.dataset.v) : s.add(c.dataset.v); L.page = 1; render(); return; }
    if (e.target.closest('[data-unfilter-all]')) { clearFilters(); return; }
    const u = e.target.closest('[data-unfilter]'); if (!u) return;
    const [k, v] = u.dataset.unfilter.split(/:(.*)/);
    if (k === 'q') { Q = ''; find.value = ''; } else F[k].delete(v);
    L.page = 1; render();
  });
  function clearFilters() { Object.values(F).forEach(s => s.clear()); Q = ''; find.value = ''; L.page = 1; render(); }
  // The text filter narrows on what a row shows: id, title, repo, status and kind, as v1's ?q= read the visible columns.
  find.addEventListener('input', () => { Q = find.value.trim().toLowerCase(); L.page = 1; render(); });
  find.addEventListener('keydown', e => { if (e.key === 'Escape' && find.value) { e.stopPropagation(); find.value = ''; Q = ''; L.page = 1; render(); } });
  const words = t => `#${t.id ?? ''} ${t.title} ${t.repo} ${slabel(t.status)} ${t.kind}`.toLowerCase();
  const passes = t => (!F.kind.size || F.kind.has(t.kind)) && (!F.repo.size || F.repo.has(t.repo)) && (!F.p.size || F.p.has(t.p ? String(t.p) : '')) && (!F.due.size || F.due.has(dueBucket(t)))
    && (!F.status.size || F.status.has(t.status)) && (!F.age.size || F.age.has(t.age)) && (!F.active.size || t.status !== 'done') && (!Q || words(t).includes(Q));
  const visible = () => tasks.filter(passes);

  // ---------- Commands (products/system/commands.md) ----------
  // Each task is an item object. Each command is declared once; the shell renders the row button, the
  // action menu (. or ⋯), the Details bar with its CLI lines, the palette and the bulk bar from it.
  const C = window.shell.commands;
  const T = o => byKey(o.id);
  // A run's row: a picked row a refresh removed fails that row's run with this reason (review, PR #46).
  const must = o => { const t = T(o); if (!t) throw new Error('the task is no longer listed'); return t; };
  // Details beyond the row (sd:2180). build: /api/tasks/<id>, read when a task is selected, kept until a write changes it.
  // Each read carries the item's generation; a write bumps it, so an answer sent before the write is dropped, not cached.
  const DET = { items: {}, reading: {}, failed: {}, gen: {} }, detOf = t => (t && t.id && DET.items[t.id]) || null;
  async function readDet(id, force) {
    if (!id || (!force && (DET.items[id] || DET.reading[id] === (DET.gen[id] || 0)))) return;
    const gen = DET.gen[id] || 0;
    DET.reading[id] = gen; delete DET.failed[id];
    let got, err;
    try { got = await window.shell.getJSON(`/api/tasks/${id}`); } catch (e) { err = e; }
    if ((DET.gen[id] || 0) !== gen) return; // a write landed meanwhile; the read it started is the one that counts
    delete DET.reading[id];
    if (err) DET.failed[id] = err.message; else DET.items[id] = got;
    putAll(); if (byKey(selected)?.id === id) renderDetails();
  }
  const staleDet = id => { DET.gen[id] = (DET.gen[id] || 0) + 1; delete DET.items[id]; delete DET.reading[id]; };
  // The rows' objects are the reader's (adopt below): it puts them after each read and retires a row the read stopped
  // listing. The Details' notes and assignments follow their row: once it is not listed, they run nothing either.
  const objectOf = t => ({ id: t.key, type: t.kind === 'ops' ? 'ops row' : 'item', label: `${label(t)} ${t.title}`, item: t.id });
  const live = key => { const o = C.get(key); return !!o && o.type !== 'not listed'; };
  function putAll() {
    Object.values(DET.items).forEach(d => {
      const as = live(String(d.item.id)) ? o => o : o => ({ id: o.id, type: 'not listed', label: `${o.label} (no longer listed)` });
      d.notes.forEach(n => C.put(as({ id: `note:${n.id}`, type: 'note', label: `${n.kind} ${n.id} on #${d.item.id}`, note: n.id, kind: n.kind, resolved: n.resolved, item: d.item.id })));
      d.assignments.forEach(a => C.put(as({ id: `asg:${a.id}`, type: 'assignment', label: `Assignment #${a.id}`, n: a.id, status: a.status, item: d.item.id, repo: d.item.repo, can: a.cancel })));
    });
  }
  const redrawDet = id => { staleDet(id); readDet(id, true); };
  // note.resolve: one declaration, copied on every page that lists followups (Tasks, Today; commands.md § Shared declarations).
  const NOTE_RESOLVE = done => ({ id: 'note.resolve', on: 'note', label: 'Resolve', key: 'v', risk: 'confirm', icon: 'check', primary: o => o.kind === 'followup' && !o.resolved,
    when: o => o.kind !== 'followup' ? `a ${o.kind} note has nothing to resolve` : o.resolved ? `resolved ${o.resolved.slice(0, 10)}` : true,
    cli: o => `sd note resolve ${o.note}`, consequence: () => 'Closes the followup. sd note has resolve and list only, so no verb reopens it.',
    run: o => done(o) });
  const noId = 'no CLI: this row has no sd id';
  const idOr = (o, f) => { const t = T(o); return t && t.id ? f(t) : noId; };
  const coarse = matchMedia('(pointer: coarse)');
  const NEXT = { planning: 'ready', ready: 'in_progress', in_progress: 'done', blocked: 'ready' };
  // Status moves: keys 1–5 in the menu, as on the board. The list row shows the next status as its button.
  // build: run returns the move's landing, so the toast (with Undo) comes when the write lands; undo moves it back to
  // the status this move left, read as the write is sent. The `when` test runs again as the write leaves.
  const moveOn = (s, t) => { if (s === 'done' && repeats(t)) return !walkable(t) ? 'it repeats: 5 completes it and ends the series, since sd cannot walk its rule'
      : t.nextDue === null ? 'it repeats: 5 completes it and ends the series, since its rule gives no next date'
      : 'it repeats: 5 completes it and opens the next occurrence';
    const L = legal(t, s); return L.ok || L.reason; };
  const STATUS_CMD = Object.fromEntries(STATUSES.map(([s, name], i) => [s, {
    id: `item.status.${s}`, on: 'item', label: `Status → ${name}`, key: String(i + 1), risk: 'undo', bulk: true, icon: 'kanban',
    primary: o => view === 'list' && NEXT[T(o)?.status] === s && !(s === 'done' && repeats(T(o))),
    when: o => { const t = T(o); return t ? moveOn(s, t) : 'the task is no longer listed'; },
    cli: o => idOr(o, t => cliMove(t, s)),
    run: o => { const t = must(o); let from = t.status;
      return landing(moveTo(t.key, s, { sent: r => { from = r.status; }, check: r => moveOn(s, r) }).then(v => { landed(t.key); return v; }),
        () => `${label(t)} ${slabel(from)} → ${name} · ${cliMove(t, s)}`, v => moveTo(t.key, from, { rev: v.revision })); },
    undo: undoOf,
  }]));
  // A repeating task's completion makes workflow.change_status open the next occurrence and clear the rule on this one. A
  // move back would leave both open, so completing it is confirmed and has no Undo (review, PR #46). The rows are read
  // again when it lands, so the next occurrence is listed at once. Key 5 is Done's too: the shell runs the first command
  // on a key that is on, so the two `when` tests split on repeats() and never are both on. Not bulk: a picked repeating
  // row is skipped with its reason (keys below).
  const completeOn = t => { if (!repeats(t)) return 'the task does not repeat; Status → Done completes it'; const L = legal(t, 'done'); return L.ok || L.reason; };
  const COMPLETE = { id: 'item.complete', on: 'item', label: 'Done → next occurrence', key: '5', risk: 'confirm', executes: true, icon: 'calendar-check',
    primary: o => view === 'list' && NEXT[T(o)?.status] === 'done' && repeats(T(o)),
    when: o => { const t = T(o); return t ? completeOn(t) : 'the task is no longer listed'; },
    cli: o => idOr(o, t => cliMove(t, 'done')),
    // build: next_due is workflow.next_occurrence_due, the date the completion computes. null is its "none": the
    // completion then ends the series and opens nothing, so the confirm says that (review, PR #46). The outcome is stated
    // in the design owner's three cases (sd:2250): a date the page has; a valid rule whose date the page has not read yet
    // since an edit (undefined), which sd sets; a rule sd cannot walk. A completion that opens nothing is a danger confirm.
    consequence: o => { const t = T(o), rule = t.recurrence;
      if (!walkable(t)) return `Completes ${label(t)} and ends the series: sd cannot walk ${rule}, so no next occurrence opens. There is no Undo.`;
      if (t.nextDue === null) return `Completes ${label(t)} and ends the series: its rule (${rule}) gives no next date, so no next occurrence opens. There is no Undo.`;
      return `Completes ${label(t)} and opens the next occurrence${t.nextDue ? `, due ${fmt(t.nextDue)} (${rule}).` : ` (${rule}); sd sets its date.`} A move back would leave two open tasks, so there is no Undo.`; },
    danger: o => ends(T(o)),
    run: o => { const t = T(o); let from = t.status;
      const next = v => v?.next_occurrence ? `next occurrence #${v.next_occurrence}${byKey(String(v.next_occurrence))?.due ? ` due ${fmt(byKey(String(v.next_occurrence)).due)}` : ''}`
        : `the series ended: ${v?.next_occurrence_reason || 'no next occurrence was made'}`;
      return landing(moveTo(t.key, 'done', { sent: r => { from = r.status; }, check: completeOn, wait: true }),
        v => `${label(t)} ${slabel(from)} → Done · ${next(v)} · ${cliMove(t, 'done')}`); } };
  // An edit's Undo sets back the fields it changed (`keep`, the row's names), as they were when the edit was sent, at the
  // revision the edit answered. `check` asks as the edit leaves whether it still changes anything: a second one queued
  // behind the first would write nothing, and its Undo would set back what the first one changed (review, PR #46).
  const FIELD = { anchor: 'recurrence_anchor' };
  function editRun(t, ch, msg, { check, keep = Object.keys(ch) } = {}) {
    let was = {};
    const sent = r => { was = Object.fromEntries(keep.map(k => [FIELD[k] || k, r[k] ?? null])); };
    return landing(edit(t.key, ch, { sent, check }).then(v => { landed(t.key); return v; }), msg, v => edit(t.key, was, { rev: v.revision }));
  }
  C.register(
    ...Object.values(STATUS_CMD), COMPLETE,
    // Edit sets priority and due date in one dialog. Its save carries the Undo, so the shell's own feedback stays safe.
    { id: 'item.edit', on: 'item', label: 'Edit', key: 'e', risk: 'safe', icon: 'pen-line',
      when: o => { const t = T(o); return !t?.id ? 'this row has no sd id to edit' : EDITABLE(t) || noEdit(t); },
      cli: o => idOr(o, t => `sd task edit ${t.id} --priority ${t.p || 'N'} ${t.due ? '--due ' + t.due : '--due YYYY-MM-DD'}`),
      run: o => { askEdit(T(o)); return `Editing ${label(T(o))}`; } },
    // Move to: the status moves without drag (touch, or anyone). One form lists every column; a refused one is off with its reason.
    { id: 'item.move', on: 'item', label: 'Move to', key: 'm', risk: 'safe', icon: 'kanban',
      primary: () => view === 'board' && coarse.matches,
      when: o => { const t = T(o); if (!t) return 'the task is no longer listed'; const L = STATUSES.map(([s]) => legal(t, s)); return L.some(x => x.ok) || L.find(x => x.reason !== 'Already here.').reason; },
      cli: o => idOr(o, t => `sd task status ${t.id} <status>`),
      run: o => { askMove(T(o)); return null; } },
    // Priorities 1–4, workflow.edit_item's range, each a bulk edit with Undo (sd:3012 triage re-prioritizes in bulk).
    ...[1, 2, 3, 4].map(n => ({ id: `item.p${n}`, on: 'item', label: `Edit → P${n}`, risk: 'undo', bulk: true, icon: 'flag-triangle-right',
      when: o => { const t = T(o); return !t?.id ? 'this row has no sd id to edit' : !EDITABLE(t) ? noEdit(t) : t.p === n ? `it is already P${n}` : true; },
      cli: o => idOr(o, t => `sd task edit ${t.id} --priority ${n}`),
      run: o => { const t = must(o), was = t.p; return editRun(t, { p: n }, () => `${label(t)} P${was || '–'} → P${n}`); },
      undo: undoOf })),
    // Close is sd task cancel (sd:3012): done, with a cancelled receipt and the reason, through progress.task_guard. A bulk
    // close asks once for one reason and runs every picked row with its own revision. No Undo: no sd verb takes it back.
    { id: 'item.close', on: 'item', label: 'Close', risk: 'confirm', executes: true, bulk: true, icon: 'ban',
      when: o => { const t = T(o); return !t?.id ? 'this row has no sd id' : !CLOSES.includes(t.kind) ? `sd task cancel closes a task or followup; this is a ${t.kind} item`
        : t.status === 'done' ? 'it is done' : repeats(t) ? 'a recurring task cannot be cancelled; clear its recurrence first, or complete it'
        : t.assignment === 'running' || t.assignment === 'queued' ? `a runner assignment is ${t.assignment}` : true; },
      fields: () => [{ name: 'reason', label: 'Reason', required: true, placeholder: 'why nobody will do it', help: 'Recorded on each closed task. A close without a reason is refused.' }],
      cli: (o, v = {}) => idOr(o, t => `sd task cancel ${t.id} --reason ${v.reason ? window.shell.shq(v.reason) : "'<why>'"}`),
      sends: o => `POST /api/items/${T(o).id}/cancel-task {reason}`,
      consequence: () => 'Closes it as done with a cancelled receipt and your reason. No sd verb takes it back.',
      run: (o, v) => { const t = must(o);
        return landing(write(t.key, x => `/api/items/${x.id}/cancel-task`, { reason: v.reason }), () => `${label(t)} closed · sd task cancel ${t.id} --reason ${window.shell.shq(v.reason)}`); } },
    { id: 'item.note', on: 'item', label: 'Note', key: 'n', risk: 'safe', icon: 'notebook-pen',
      when: o => !!T(o)?.id || 'this row has no sd id to attach a note to',
      cli: o => idOr(o, t => `sd task note ${t.id} --kind comment --body "…"`),
      run: o => { window.shell.capture(o); const r = document.querySelector('dialog.capture input[value="note"]'); if (r && !r.disabled) { r.checked = true; r.form.dispatchEvent(new Event('input')); } return 'Write the note'; } },
    { id: 'item.delete', on: 'item', label: 'Delete', risk: 'confirm', icon: 'x',
      when: () => 'no CLI verb: sd task has no delete; move it to Done to keep a record',
      cli: o => idOr(o, t => `sd task delete ${t.id}`),
      consequence: () => 'The task, its notes and its history go. Its assignments stay in the runner log. This cannot be undone; to keep a record, move it to Done instead.' },
    // Artifact and cancel are sd work verbs (review item 17). build (sd:2200, the design's sd:2199): both post to the routes v1's
    // item page uses, /api/items/<id>/(relink|cancel), with the row's revision. The text only the operator has, a moved path or a
    // reason, is a confirm field; OK stays off until it is typed. Availability is progress.work_controls, read with the Details.
    // Neither has Undo: relinking back is another relink, and no sd verb reopens a cancelled item.
    { id: 'work.relink', on: 'item', label: 'Relink', key: 'l', risk: 'safe', executes: true, icon: 'link-2',
      when: o => { const t = T(o), d = detOf(t); return !t?.id ? 'this row has no sd id' : t.kind !== 'work' ? `sd work relink acts on a work item; this is a ${t.kind}`
        : !d ? 'the artifact was not read for this row' : !d.work.relink ? d.work.reason : d.item.path ? true : 'the item has no artifact to relink'; },
      fields: o => { const d = detOf(T(o)); return [{ name: 'path', label: 'Moved path', required: true, placeholder: d.item.path,
        help: `Relative to ${d.item.repo}. The item keeps its history; only the artifact path changes.` }]; },
      cli: (o, v = {}) => idOr(o, t => `sd work relink ${t.id} ${v.path ? window.shell.shq(v.path) : '<moved path>'}`),
      sends: o => `POST /api/items/${T(o).id}/relink {path}`,
      run: (o, v) => { const t = must(o);
        return landing(write(t.key, x => `/api/items/${x.id}/relink`, { path: v.path }), () => `${label(t)} relinked → ${v.path} · sd work relink ${t.id} ${window.shell.shq(v.path)}`); } },
    { id: 'work.cancel', on: 'item', label: 'Cancel', key: 'w', risk: 'confirm', executes: true, icon: 'ban',
      when: o => { const t = T(o), d = detOf(t); return !t?.id ? 'this row has no sd id' : t.kind !== 'work' ? `sd work cancel acts on a work item; this is a ${t.kind}`
        : t.status === 'done' ? 'the work item is done' : !d ? 'its cancel availability was not read for this row' : d.work.cancel || d.work.reason; },
      fields: () => [{ name: 'reason', label: 'Reason', required: true, placeholder: 'why the work stops', help: 'Recorded with the cancellation. A cancel without a reason is refused.' }],
      cli: (o, v = {}) => idOr(o, t => `sd work cancel ${t.id} --reason ${v.reason ? window.shell.shq(v.reason) : "'<why>'"}`),
      sends: o => `POST /api/items/${T(o).id}/cancel {reason}`,
      consequence: () => 'Closes the work item as cancelled, with your reason. No sd verb takes it back.',
      run: (o, v) => { const t = must(o);
        return landing(write(t.key, x => `/api/items/${x.id}/cancel`, { reason: v.reason }), () => `${label(t)} cancelled · sd work cancel ${t.id} --reason ${window.shell.shq(v.reason)}`); } },
    // Recurrence: sd task edit --recur needs a due date; --clear-recur stops the series and clears its anchor, which Undo sets again.
    { id: 'item.recur', on: 'item', label: 'Edit → repeat weekly', risk: 'undo', icon: 'calendar-clock',
      when: o => { const t = T(o), d = detOf(t); if (!t?.id) return 'this row has no sd id to edit'; if (!RECURS.includes(t.kind)) return `a ${t.kind} item cannot recur`;
        if (!EDITABLE(t)) return noEdit(t);
        if (!d) return 'recurrence was not read for this row';
        return d.item.recurrence ? `it repeats already: ${d.item.recurrence}` : t.due ? true : '--recur needs a due date'; },
      cli: o => idOr(o, t => `sd task edit ${t.id} --recur FREQ=WEEKLY`),
      run: o => { const t = must(o); return editRun(t, { recurrence: 'FREQ=WEEKLY' }, () => `${label(t)} repeats weekly`,
        { check: r => !repeats(r) || `it repeats already: ${r.recurrence}` }); }, undo: undoOf },
    { id: 'item.recur.clear', on: 'item', label: 'Edit → stop repeating', risk: 'undo', icon: 'calendar-clock',
      when: o => { const t = T(o), d = detOf(t); return !d ? 'recurrence was not read for this row' : !EDITABLE(t) ? noEdit(t) : d.item.recurrence ? true : 'the task does not repeat'; },
      cli: o => idOr(o, t => `sd task edit ${t.id} --clear-recur`),
      // The rule and anchor Undo sets again are the row's as the edit leaves, after any edit queued before it.
      run: o => { const t = must(o); return editRun(t, { recurrence: null }, () => `${label(t)} no longer repeats`,
        { check: r => repeats(r) || 'the task does not repeat', keep: ['recurrence', 'anchor'] }); },
      undo: undoOf },
    // A followup note closes with sd note resolve. sd note has resolve and list only, so nothing reopens it: confirm, no Undo.
    // build: POST /api/notes/<note>/resolve with the item's revision; the readback is the item's state.
    NOTE_RESOLVE(o => { const item = String(o.item);
      return landing(write(item, () => `/api/notes/${o.note}/resolve`, {}).then(() => redrawDet(o.item)), () => `Resolved note ${o.note} · sd note resolve ${o.note}`); }),
    // Assignments: the same declarations as Management (commands.md § Shared declarations).
    // build (sd:3041): cancel is sd assignments cancel, POST /api/assignments/<n>/cancel with the revision the Details read
    // (operations.assignment_state); the runner's cancel went with the runner. No verb reopens a cancelled row: confirm, no Undo.
    { id: 'asg.cancel', on: 'assignment', label: 'Cancel', key: 'x', risk: 'confirm',
      when: o => o.can.allowed || o.can.reason, cli: o => `sd assignments cancel ${o.n}`,
      consequence: o => `Ends assignment #${o.n} in ${o.repo} as cancelled. The item's status is unchanged.`,
      run: o => landing(cancelAsg(o), () => `Cancelled · #${o.n} · sd assignments cancel ${o.n}`) },
    { id: 'asg.get', on: 'assignment', label: 'Show assignment', key: 'o', risk: 'safe', primary: o => o.status !== 'blocked', cli: o => `sd assignments get ${o.n}`, run: o => `Assignment #${o.n} shown in Details` },
  );
  // A refused cancel reads the Details again too: they hold the revision a retry sends.
  async function cancelAsg(o) {
    const a = Object.values(DET.items).flatMap(d => d.assignments).find(x => x.id === o.n);
    if (!a) throw new Error(`assignment #${o.n} was not read`);
    try { await window.shell.post(`/api/assignments/${o.n}/cancel`, { revision: a.revision }); } catch (err) { if (err.stale) redrawDet(o.item); throw err; }
    redrawDet(o.item); await reread();
  }

  // ---------- Rendering ----------
  let view = 'board', selected = null;
  let checked = new Set(); // mirror of the shell's picked rows
  function card(t, compact) {
    const st = state(t), [dt, dc] = dueText(t);
    return html`<article class="card" tabindex="0" data-key="${t.key}"${t.key === selected ? html` aria-current="true"` : ''}${checked.has(t.key) ? html` data-picked` : ''}${st ? html` data-state="${st}"` : ''} aria-label="${label(t) + ' ' + t.title}">
      <div class="top">
        <label class="pick"><input type="checkbox" data-check="${t.key}" aria-label="Select ${label(t)}"${checked.has(t.key) ? html` checked` : ''}><span></span></label>
        ${st ? html`<span class="g g-${st}" aria-hidden="true">${GLYPH[st]}</span>` : ''}
        <span class="id">${label(t)}</span>
        <span class="pri" data-p="${String(t.p || '')}" title="Priority">${t.p ? 'P' + t.p : 'P–'}</span>
        ${t.assignment === 'running' ? html`<span class="lock" title="Runner assignment running">${ICON('lock')}</span>` : ''}
        ${C.rowActions(t.key)}
      </div>
      <div class="t">${t.title}</div>
      <div class="meta"><span class="repo">${t.repo}</span><span class="due ${dc}">${dt}</span>${compact ? html`<span>${slabel(t.status).toLowerCase()}</span>` : ''}</div>
    </article>`;
  }
  function renderBoard() {
    const v = visible();
    // build: backlog_items also returns statuses with no column (a message's ready_to_send); they get one lane, which
    // takes no drop and no key, so every row the page counts is on the board (review, PR #46).
    const other = v.filter(t => !SLABEL[t.status]);
    put(document.getElementById('view-board'), html`<div class="board" id="board" role="group" aria-label="Tasks by status"${other.length ? html` data-other` : ''}>${STATUSES.map(([s, name], i) => {
      const rows = v.filter(t => t.status === s);
      return html`<div class="col" data-drop="status:${s}" role="group" aria-label="${name}">
        <header><span class="label">${name}</span><kbd>${i + 1}</kbd><span class="n">${rows.length}</span></header>
        <div><p class="refusal" role="note"></p><div class="drop">${(rows.length ? rows.map(t => card(t)) : html`<p class="empty">${on() ? 'None match the filters.' : 'Nothing ' + name.toLowerCase() + '.'}</p>`)}</div></div>
      </div>`;
    })}${other.length ? html`<div class="col" role="group" aria-label="Other statuses">
        <header><span class="label">Other</span><span class="n">${other.length}</span></header>
        <div><div class="drop">${other.map(t => card(t, true))}</div></div>
      </div>` : ''}</div>`);
  }
  function renderMatrix() {
    const v = visible().filter(t => t.status !== 'done' && t.kind !== 'ops');
    const q = QUADS.map(([k, name, , , sub]) => {
      const rows = v.filter(t => quadOf(t) === k);
      const worst = rows.some(t => state(t) === 'warning') ? 'warning' : rows.some(t => state(t) === 'caution') ? 'caution' : '';
      return html`<div class="quad" data-q="${k}" data-drop="quad:${k}">
        <header><h3>${name}</h3><span class="sub">${sub}</span><span class="n">${rows.length}</span><span class="lamp"${worst ? html` data-state="${worst}"` : ''}></span></header>
        <div><div class="drop" role="group" aria-label="${name}: ${sub}">${(rows.length ? rows.map(t => card(t, true)) : html`<p class="empty">Empty.</p>`)}</div><p class="refusal" role="note"></p></div>
        ${k === 'do' ? html`<span class="reticle" aria-hidden="true"></span>` : ''}
      </div>`;
    });
    put(document.getElementById('view-matrix'), html`<div class="matrix" id="matrix">
      <span></span>
      <div class="axis x">Urgent <span class="rule">due ≤ 7d or overdue</span></div>
      <div class="axis x">Not urgent</div>
      <div class="axis y">Important <span class="rule">P1–P2</span></div>${q[0]}${q[1]}
      <div class="axis y">Not important</div>${q[2]}${q[3]}
    </div>
    <p class="mnote">Done tasks and ops rows stay off the matrix. A drop edits the task: into Important sets P2, out of it sets P3; crossing into or out of Urgent asks you for the due date, because urgency comes from it. Cancel changes nothing.</p>`);
  }
  // The list's sort, page and size (shell.list, sd:2682): priority first, 50 a page. Every column with an order sorts.
  const LIST = { sort: 'p', dir: 1, size: 50 }, L = { ...LIST, page: 1 };
  const ORDER = Object.fromEntries(STATUSES.map(([st], i) => [st, i]));
  const SORTS = { id: t => t.id ?? Infinity, title: t => t.title.toLowerCase(), repo: t => t.repo.toLowerCase(), status: t => ORDER[t.status] ?? 99, p: t => t.p ?? 9,
    due: t => t.overdue && !t.due ? -99 : t.due ? days(t) : 999 };
  const COLS = [[null, html`<span class="sr">Select</span>`], [null, html`<span class="sr">State</span>`], ['id', 'Id', 'label'], ['title', 'Title', 'label'], ['repo', 'Repo', 'label'],
    ['status', 'Status', 'label'], ['p', 'P', 'label'], ['due', 'Due', 'label'], [null, html`<span class="sr">Actions</span>`]];
  let seek = false;
  function renderList() {
    const list = window.shell.list, k = SORTS[L.sort];
    const all = visible().slice().sort((a, b) => { const x = k(a), y = k(b); return (x < y ? -1 : x > y ? 1 : 0) * L.dir; });
    // build (sd:2589): the list pages as v1 /backlog paged, 50 a page; board and matrix show every filtered task. A ?row= with
    // no ?page= opens the page that holds it.
    if (seek) { seek = false; const i = all.findIndex(t => t.key === selected); if (i >= 0) L.page = Math.floor(i / L.size) + 1; }
    const v = list.pageOf(all, L);
    put(document.getElementById('view-list'), html`<div class="list-wrap"><table class="list"><caption class="sr">Tasks</caption><thead>${list.sortHead(COLS, L)}</thead><tbody>
      ${(v.length ? v.map(t => { const st = state(t), [dt, dc] = dueText(t); return html`<tr data-key="${t.key}"${t.key === selected ? html` aria-current="true"` : ''}${checked.has(t.key) ? html` data-picked` : ''}><td><label class="pick"><input type="checkbox" data-check="${t.key}" aria-label="Select ${label(t)}"${checked.has(t.key) ? html` checked` : ''}><span></span></label></td><td class="g g-${st}">${st ? GLYPH[st] : ''}</td><td class="mono">${label(t)}</td><td class="title"><button type="button" data-open="${t.key}">${t.title}</button></td><td class="mono">${t.repo}</td><td>${slabel(t.status)}</td><td><span class="pri" data-p="${String(t.p || '')}">${t.p ? 'P' + t.p : 'P–'}</span></td><td class="mono due ${dc}">${dt}</td><td>${C.rowActions(t.key)}</td></tr>`; }) : html`<tr><td colspan="9" class="empty">None match the filters. <button class="linkbtn" type="button" id="clear-f2">Clear filters</button></td></tr>`)}
    </tbody></table></div>${list.pager(all.length, L, 'tasks')}`);
  }
  document.getElementById('view-list').addEventListener('click', e => {
    if (window.shell.list.sortBy(e, L)) { window.shell.list.keepFocus(e, renderList); writeURL(); return; }
    if (e.target.id === 'clear-f2') { clearFilters(); return; }
    if (window.shell.list.paging(e, L)) { window.shell.list.keepFocus(e, render); return; }
    const o = e.target.closest('[data-open]'); if (o) select(o.dataset.open, true);
  });

  function renderDetails() {
    const t = byKey(selected), el = document.getElementById('details');
    if (!t) { put(el, html`<p class="note">${!READ ? 'The tasks were not read, so nothing is selected.'
      : tasks.some(x => !live(x.key)) ? 'The tasks were not read again after the change, so nothing is selected. Reload reads them.'
      : !tasks.length ? 'No open task, so nothing is selected.' : !visible().length ? 'No task matches these filters. Clear them to see the list.'
      : 'Select a task to see its fields and moves.'}</p>`); return; }
    const [dt] = dueText(t), act = C.bar(t.key), gone = !live(t.key);
    put(el, html`<div class="kind"><span class="label">${t.kind === 'ops' ? 'Ops row' : t.kind}</span><span class="tag real">observed</span></div>
      <h2>${t.title}</h2>
      <dl>
        <dt>Id</dt><dd>${t.id ? '#' + t.id : 'not recorded'}</dd>
        <dt>Status</dt><dd>${t.kind === 'ops' ? 'none (ops)' : slabel(t.status)}</dd>
        <dt>Priority</dt><dd>${t.p ? 'P' + t.p : 'not recorded'}</dd>
        <dt>Due</dt><dd>${t.due ? `${t.due} · ${dt}` : t.overdue ? 'overdue · date not recorded' : 'none'}</dd>
        <dt>Repo</dt><dd>${t.repo_path || t.repo}</dd>
        <dt>Urgent</dt><dd>${urgent(t) ? 'yes' : 'no'} <span class="why">(derived)</span></dd>
        <dt>Quadrant</dt><dd>${t.kind === 'ops' || t.status === 'done' ? 'not placed' : QUADS.find(q => q[0] === quadOf(t))[1]}</dd>
        ${t.assignment ? html`<dt>Assignment</dt><dd>${t.assignment}</dd>` : ''}
      </dl>
      <h3>Act</h3>${gone ? html`<p class="why">This task was not read again after the change, so no command runs on it. Reload reads it.</p>`
        : String(act) ? act : html`<p class="why">No sd item behind this row; nothing to run. It clears when the next sync succeeds.</p>`}
      ${more(t)}
      ${t.id ? html`<p class="why">Delete asks first and cannot be undone; to keep a record, move the task to Done. <code>sd task</code> has no delete verb yet (sd:1899).</p>` : ''}
      <p class="foot">${READ ? html`Rows read <time class="rel" datetime="${READ}"></time> from /api/tasks (the reads v1 /backlog makes).` : ''}</p>`);
  }
  // The v1 item page's sections (review item 17), each with the read it comes from. A row without a reading says so once.
  function more(t) {
    if (!t.id) return '';
    const d = detOf(t);
    if (!d) return html`<h3>History, notes and assignments</h3><p class="why">${DET.failed[t.id] ? `Not read: ${DET.failed[t.id]}` : `Reading sd task show ${t.id} --json…`}</p>`;
    const it = d.item, src = s => html`<p class="src">${s}</p>`;
    const open = d.notes.filter(n => n.kind === 'followup' && !n.resolved).length;
    return html`<h3>Status history</h3>${src(`sd task show ${t.id} --json · notes of kind status_change`)}
      ${d.history.length ? html`<ol class="rec">${d.history.map(n => html`<li><time class="rel" datetime="${n.at}"></time><span>${n.body}</span></li>`)}</ol>` : html`<p class="why">This item has never changed status.</p>`}
      <h3>Notes <span class="n">${plural(d.notes.length, 'note')} · ${open} open ${plural.word(open, 'followup')}</span></h3>${src(`sd task show ${t.id} --json · every other note kind`)}
      ${d.notes.length ? html`<ol class="rec">${d.notes.map(n => html`<li data-note="${String(n.id)}"><time class="rel" datetime="${n.at}"></time><span><b>${n.kind}</b>${n.kind === 'followup' ? (n.resolved ? ' · resolved' : ' · open') : ''} ${n.body}</span>${C.rowActions('note:' + n.id)}</li>`)}</ol>` : html`<p class="why">No notes on this item.</p>`}
      <h3>Assignments</h3>${src(`sd assignments list --json · item ${t.id}; not in sd task show`)}
      ${d.assignments.length ? html`<ol class="rec">${d.assignments.map(a => html`<li><span class="mono">#${String(a.id)}</span><span>${a.role} · ${a.provider || 'no provider'} · <b>${a.status}</b>${a.ended ? html` · ended <time class="rel" datetime="${a.ended.replace('+00:00', 'Z')}"></time>` : ''}${a.cancel.allowed ? '' : html`<br><small>sd assignments cancel: ${a.cancel.reason}</small>`}</span>${C.rowActions('asg:' + a.id)}</li>`)}</ol>` : html`<p class="why">No assignment has run on this item.</p>`}
      <h3>Artifact</h3>${src(`sd task show ${t.id} --json · item.path`)}
      <dl><dt>Path</dt><dd>${it.path || 'none'}</dd><dt>Repo</dt><dd>${it.repo || 'none'}</dd>${it.branch ? html`<dt>Branch</dt><dd>${it.branch}</dd>` : ''}</dl>
      <h3>Recurrence</h3>${src(`sd task show ${t.id} --json · item.recurrence`)}
      <dl><dt>Repeats</dt><dd>${it.recurrence || 'no'}</dd>${it.recurrence_anchor ? html`<dt>Anchor</dt><dd>${it.recurrence_anchor}</dd>` : ''}<dt>Revision</dt><dd>${d.revision.slice(0, 12)}…</dd></dl>
      ${external(d)}`;
  }
  // build: v1's External context (screens.external_context), the shadow row and its collector's freshness, when the item
  // has a tracker reference. Local status is the item's own; the tracker's state is dated context beside it.
  function external(d) {
    const x = d.external; if (!x) return '';
    return html`<h3>External context</h3>${html`<p class="src">sd shadow · ${x.tracker} · progress.tracker_freshness</p>`}
      <dl><dt>Tracker</dt><dd>${x.tracker}</dd><dt>External state</dt><dd>${x.state || (x.last_seen ? 'unknown' : 'not in the local snapshot')}</dd>
        <dt>Reference</dt><dd>${x.url}</dd><dt>Last seen</dt><dd>${x.last_seen ? html`<time class="rel" datetime="${x.last_seen}"></time>` : 'never'}</dd>
        <dt>Sync health</dt><dd>${x.freshness.state}</dd><dt>Last sync</dt><dd>${x.freshness.last_success_at ? html`<time class="rel" datetime="${x.freshness.last_success_at}"></time>` : 'never'}</dd></dl>
      ${x.freshness.reason ? html`<p class="why">${x.freshness.reason}</p>` : ''}`;
  }
  function render() {
    putAll();
    renderFilters();
    writeURL(); window.shell.views(VIEWS);
    if (view === 'board') renderBoard(); else if (view === 'matrix') renderMatrix(); else renderList();
    // design.md § Selection: a filter or view change that hides the selected task moves the selection to the first visible
    // one, so x, . and the palette never act on a hidden card (review 2026-09-29, item 10).
    shell.reconcile({ rows: document.querySelectorAll('main [data-key]'), id: n => n.dataset.key, current: selected,
      select: key => select(key, false), clear: () => { selected = null; writeURL(); } });
    renderDetails();
  }

  // ---------- Selection ----------
  function select(key, open) {
    selected = key;
    // The selected card or row is aria-current: a card holds a checkbox and buttons, so it is an article, not a listbox
    // option, whose descendants assistive technology treats as presentational (review, PR #46).
    document.querySelectorAll('[data-key]').forEach(n => n.dataset.key === key ? n.setAttribute('aria-current', 'true') : n.removeAttribute('aria-current'));
    C.select(key);
    readDet(byKey(key)?.id); // build: the Details reading, once per task until a write changes it
    renderDetails();
    writeURL();
    if (open && innerWidth < 1240) openPane('tab-details');
  }
  document.getElementById('main').addEventListener('click', e => {
    if (e.target.closest('.rowact, .pick')) return;
    const c = e.target.closest('.card'); if (c && !suppressClick) select(c.dataset.key, true);
  });
  // Selection follows focus on a card, so Enter, which the shell sends as shell:open, opens the focused one.
  document.getElementById('main').addEventListener('focusin', e => { const c = e.target.closest('.card'); if (c && e.target === c && c.dataset.key !== selected) select(c.dataset.key, false); });
  document.getElementById('main').addEventListener('change', e => {
    const k = e.target.dataset.check; if (!k) return;
    C.pick(k);
  });
  // The shell owns the picked set and the bulk bar; mirror it on rows and boxes.
  document.addEventListener('shell:picked', e => {
    checked = new Set(e.detail);
    document.querySelectorAll('main [data-key]').forEach(n => n.toggleAttribute('data-picked', checked.has(n.dataset.key)));
    document.querySelectorAll('main [data-check]').forEach(b => { b.checked = checked.has(b.dataset.check); });
  });
  // A note or an assignment opens its item: they are listed in the item's Details, not as rows of their own.
  document.addEventListener('shell:open', e => { const id = /^(note|asg):/.test(e.detail) ? String(C.get(e.detail)?.item ?? '') : e.detail; if (byKey(id)) select(id, true); });
  document.addEventListener('tasks:view', e => setView(e.detail));

  // ---------- Moves with Undo ----------
  function landed(key) { requestAnimationFrame(() => document.querySelector(`.card[data-key="${key}"]`)?.classList.add('landed')); }
  function move(key, to) {
    const t = byKey(key), L = legal(t, to);
    if (!L.ok) { toast(`${label(t)} not moved: ${L.reason}`); return false; }
    C.run(to === 'done' && repeats(t) ? COMPLETE : STATUS_CMD[to], C.get(key));
    return true;
  }
  // Picked rows and 1–5: the status command runs as one bulk group on each picked row it is on for, as the shell's bulk bar
  // runs it; each row it is off for is skipped and named with its reason (a repeating task's Done is item.complete, not bulk).
  function movePicked(to) {
    const c = STATUS_CMD[to], objs = [...checked].map(k => C.get(k)).filter(Boolean);
    const on = objs.filter(o => c.when(o) === true), off = objs.filter(o => c.when(o) !== true);
    if (on.length) C.runBulk(c, on);
    if (off.length) toast(`Skipped ${off.map(o => `${label(T(o))}: ${c.when(o)}`).join('; ')}`);
  }
  // build: a quadrant drop is an edit; it lands through the same write, toasts after, and Undo edits the fields back.
  function commitEdit(t, ch, words) {
    // A page dialog, not a shell command: it toasts on its own, with the same Undo a command gets.
    editRun(t, ch, () => `${label(t)} ${words}${t.id ? ' · ' + cliQuad(t, ch) : ''}`).then(r => toast(r.text, () => r.undo().then(
      ok => { if (ok) toast(`Edit undone · ${label(t)}`); }, err => failed(label(t), err))), err => failed(label(t), err));
  }
  function toQuad(key, q) {
    const t = byKey(key), Q = quadChange(t, q);
    if (!Q.ok) { if (Q.reason !== 'Already here.') toast(`${label(t)} not moved: ${Q.reason}`); return false; }
    const name = QUADS.find(x => x[0] === q)[1];
    const commit = ch => {
      const w = [];
      if ('p' in ch) w.push(Q.words[0]);
      if ('due' in ch) w.push(`due ${ch.due ? fmt(ch.due) : 'removed'}`);
      commitEdit(t, ch, `→ ${name}: ${w.join(', ')}`);
    };
    if (!Q.needsDate) { commit(Q.ch); return true; }
    askDate(t, Q.needsDate, name, due => commit({ ...Q.ch, due }));
    return false;
  }
  // build: workflow._recurring refuses a repeating item with no due date, so neither dialog offers Remove due date on one;
  // it says why instead (review, PR #46).
  const NO_CLEAR = 'Remove due date is off: a repeating task needs a due date. Stop repeating first to remove it.';
  // Edit: priority and due date. Save applies both with Undo; Cancel changes nothing.
  function askEdit(t) {
    put(dateDlg, html`<form method="dialog" class="date-form">
      <h2 id="date-h">Edit ${label(t)}</h2>
      <label class="label" for="pri-in">Priority</label>
      <select id="pri-in">${t.p ? '' : html`<option value="" selected>not recorded</option>`}${[1, 2, 3, 4].map(v => html`<option value="${v}"${t.p === v ? html` selected` : ''}>P${v}</option>`)}</select>
      <label class="label" for="due-in">Due date</label>
      <input id="due-in" type="date" value="${String(t.due || '')}">
      ${t.due && repeats(t) ? html`<p class="why">${NO_CLEAR}</p>` : ''}
      <div class="cli"><code id="edit-cli"></code></div>
      <div class="actions">
        <button class="btn" value="set" type="submit">Save</button>
        ${t.due && !repeats(t) ? html`<button class="btn quiet" value="clear" type="submit" formnovalidate>Remove due date</button>` : ''}
        <button class="btn quiet" value="cancel" type="submit" formnovalidate>Cancel</button>
      </div></form>`);
    const f = dateDlg.querySelector('form');
    const change = clear => { const ch = {}, p = f.querySelector('#pri-in').value, d = f.querySelector('#due-in').value; if (p && +p !== t.p) ch.p = +p; if (clear) ch.due = null; else if (d && d !== t.due) ch.due = d; return ch; };
    const upd = () => { const ch = change(false); f.querySelector('#edit-cli').textContent = Object.keys(ch).length ? cliQuad(t, ch) : 'no change yet'; };
    f.addEventListener('input', upd); upd();
    dateDlg.onclose = () => {
      const v = dateDlg.returnValue; if (v !== 'set' && v !== 'clear') return;
      const ch = change(v === 'clear'); if (!Object.keys(ch).length) { toast(`${label(t)} unchanged.`); return; }
      commitEdit(byKey(t.key), ch, 'edited');
    };
    dateDlg.returnValue = '';
    dateDlg.showModal(); f.querySelector('#pri-in').focus();
  }
  // Date prompt for a move across the urgency line. Cancel changes nothing.
  const dateDlg = document.createElement('dialog');
  dateDlg.className = 'palette date-dlg'; dateDlg.setAttribute('aria-labelledby', 'date-h'); put(dateDlg, html`<h2 id="date-h"></h2>`);
  document.body.append(dateDlg);
  function askDate(t, dir, name, done) {
    const into = dir === 'into';
    // The boundary is withinWeek's, the server's rule from the read stamp, not 7 local days: the last urgent date is the day
    // before the first one withinWeek calls not urgent, so a picked date always lands in the quadrant named (review, PR #46).
    let n = 0; while (withinWeek({ due: plus(n + 1) }) && n < 400) n++;
    const last = plus(n), min = into ? plus(0) : plus(n + 1), max = into ? last : '';
    put(dateDlg, html`<form method="dialog" class="date-form">
      <h2 id="date-h">${label(t)} → ${name}</h2>
      <p class="why">${into ? 'Urgent means due within 7 days. Pick the real deadline.' : `Not urgent means due after ${fmt(last)}. ${t.due ? `The current deadline is ${fmt(t.due)}; moving it changes the obligation.` : ''}`}</p>
      ${!into && repeats(t) ? html`<p class="why">${NO_CLEAR}</p>` : ''}
      <label class="label" for="due-in">Due date</label>
      <input id="due-in" type="date" required min="${min}"${max ? html` max="${max}"` : ''} value="">
      <div class="actions">
        <button class="btn" value="set" type="submit">Set date and move</button>
        ${!into && !repeats(t) ? html`<button class="btn quiet" value="none" type="submit" formnovalidate>Remove due date</button>` : ''}
        <button class="btn quiet" value="cancel" type="submit" formnovalidate>Cancel</button>
      </div></form>`);
    dateDlg.onclose = () => {
      const v = dateDlg.returnValue, d = dateDlg.querySelector('#due-in').value;
      if (v === 'set' && d) done(d);
      else if (v === 'none') done(null);
      else toast(`${label(t)} not moved.`);
    };
    dateDlg.returnValue = '';
    dateDlg.showModal(); dateDlg.querySelector('#due-in').focus();
  }
  // Move to: one button per status in board order, keys 1–5 as on the board. The move runs the status command, so it keeps Undo.
  function askMove(t) {
    put(dateDlg, html`<form method="dialog" class="date-form">
      <h2 id="date-h">Move ${label(t)} to</h2>
      <p class="why">Now in ${slabel(t.status)}. A column this task cannot take is off and says why.</p>
      <div class="moves">${STATUSES.map(([s, name], i) => { const L = legal(t, s); return html`<div class="mv">
        <button class="btn${L.ok ? '' : ' quiet'}" type="submit" value="${s}" data-move="${s}"${L.ok ? '' : html` disabled aria-describedby="mv-${s}"`}><span>${name}</span><kbd>${i + 1}</kbd></button>
        ${L.ok ? html`<code>${cliMove(t, s) || noId}</code>` : html`<p class="why" id="mv-${s}">${L.reason}</p>`}</div>`; })}</div>
      <div class="actions"><button class="btn quiet" value="cancel" type="submit" formnovalidate>Cancel</button></div></form>`);
    const f = dateDlg.querySelector('form');
    f.addEventListener('keydown', e => { const S = STATUSES[parseInt(e.key, 10) - 1], b = S && f.querySelector(`[data-move="${S[0]}"]`); if (b && !b.disabled) { e.preventDefault(); b.click(); } });
    dateDlg.onclose = () => {
      const v = dateDlg.returnValue; if (!SLABEL[v]) return;
      if (move(t.key, v)) document.querySelector(`.card[data-key="${t.key}"]`)?.focus();
    };
    dateDlg.returnValue = '';
    dateDlg.showModal(); (f.querySelector('[data-move]:not(:disabled)') || f.querySelector('[value="cancel"]')).focus();
  }
  // ---------- Pointer drag (mouse and pen; touch uses Move to) ----------
  let drag = null, suppressClick = false;
  const verdict = (t, target) => {
    const [kind, v] = target.split(':');
    if (kind === 'status') { const L = legal(t, v); return { ok: L.ok, text: L.ok ? `→ ${slabel(v)}` : L.reason, same: t.status === v }; }
    const Q = quadChange(t, v); return { ok: Q.ok, text: Q.ok ? Q.words.join(', ') : Q.reason, same: quadOf(t) === v };
  };
  document.addEventListener('pointerdown', e => {
    const c = e.target.closest('.card');
    if (!c || e.button !== 0 || e.pointerType === 'touch' || e.target.closest('button, input')) return;
    drag = { key: c.dataset.key, el: c, x: e.clientX, y: e.clientY, on: false, over: null };
  });
  document.addEventListener('pointermove', e => {
    if (!drag) return;
    if (!drag.on) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5) return;
      drag.on = true;
      const t = byKey(drag.key), r = drag.el.getBoundingClientRect();
      drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top;
      drag.ghost = drag.el.cloneNode(true);
      drag.ghost.classList.add('ghost-card'); drag.ghost.removeAttribute('tabindex'); drag.ghost.setAttribute('aria-hidden', 'true');
      drag.ghost.style.width = r.width + 'px';
      put(drag.ghost, html`<div class="verdict"></div>`, 'append');
      document.body.append(drag.ghost);
      drag.el.classList.add('lifted');
      document.getElementById('main').setAttribute('data-dragging', '');
      document.querySelectorAll('[data-drop]').forEach(z => {
        const V = verdict(t, z.dataset.drop);
        z.dataset.legal = V.ok || V.same ? 'yes' : 'no';
        const r2 = z.querySelector('.refusal'); if (r2) put(r2, html`${V.ok || V.same ? html`` : html`<b>■ Refused.</b> ${V.text}`}`);
      });
    }
    drag.ghost.style.left = e.clientX - drag.dx + 'px';
    drag.ghost.style.top = e.clientY - drag.dy + 'px';
    const z = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-drop]');
    if (z !== drag.over) {
      drag.over?.removeAttribute('data-over'); drag.over = z;
      z?.setAttribute('data-over', 'yes');
      const V = z ? verdict(byKey(drag.key), z.dataset.drop) : null, vd = drag.ghost.querySelector('.verdict');
      vd.textContent = V ? (V.same ? 'no change' : V.ok ? V.text : '■ refused: ' + V.text) : '';
      vd.classList.toggle('no', !!V && !V.ok && !V.same);
    }
  });
  function endDrag(commit) {
    if (!drag) return;
    const d = drag; drag = null;
    if (!d.on) return;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 0);
    d.ghost.remove(); d.el.classList.remove('lifted');
    document.getElementById('main').removeAttribute('data-dragging');
    document.querySelectorAll('[data-drop]').forEach(z => { z.removeAttribute('data-over'); z.removeAttribute('data-legal'); });
    if (!commit || !d.over) return;
    const [kind, v] = d.over.dataset.drop.split(':');
    if (kind === 'status') { if (byKey(d.key).status !== v) move(d.key, v); } else toQuad(d.key, v);
  }
  document.addEventListener('pointerup', () => endDrag(true));
  document.addEventListener('pointercancel', () => endDrag(false));

  // ---------- Keys: 1–5 run the status commands on the selected card (board), 1–4 place it (matrix); v l/b/m switch view.
  // The shell owns j/k (PAGE_LIST below), Enter (shell:open), Esc, ., x, n, c, r, g and the action menu; a key that ends the
  // shell's g-chord is the shell's.
  let vChord = 0;
  document.addEventListener('keydown', e => {
    if (e.target.matches('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest?.('.actmenu') || shell.chording()) return;
    if (vChord && Date.now() - vChord < 1500) {
      const m = { l: 'list', b: 'board', m: 'matrix' }[e.key]; vChord = 0;
      if (m) { e.preventDefault(); setView(m); return; }
    }
    if (e.key === 'v') { vChord = Date.now(); return; }
    const n = parseInt(e.key, 10);
    if (checked.size && view !== 'matrix' && n >= 1 && n <= 5) { e.preventDefault(); movePicked(STATUSES[n - 1][0]); return; }
    if (!selected || !byKey(selected) || !(n >= 1)) return;
    if (view === 'board' && n <= 5) { e.preventDefault(); move(selected, STATUSES[n - 1][0]); document.querySelector(`.card[data-key="${selected}"]`)?.focus(); }
    if (view === 'matrix' && n <= 4) { e.preventDefault(); toQuad(selected, QUADS[n - 1][0]); document.querySelector(`.card[data-key="${selected}"]`)?.focus(); }
  });
  // The shell walks rows and cards on j / k (a card takes focus) and sends Esc here to cancel a drag.
  window.PAGE_KEYS = [['1 – 5', 'Board: move the selected task to that column; with rows picked, move the picked rows'], ['1 – 4', 'Matrix: place the selected task in that quadrant'],
    ['v l / v b / v m', 'List, board or matrix view']];
  window.PAGE_LIST = { rows: () => document.querySelectorAll('main [data-key]'), id: n => n.dataset.key, current: () => selected,
    select: (key, n) => { select(key, false); if (n.matches('.card')) n.focus(); }, clear: () => !!drag && (endDrag(false), true) };

  // ---------- Views ----------
  const SUB = {
    board: 'Board of the framework statuses. Drag a card, or select it and press 1–5 or use Move to.',
    matrix: 'Importance is priority 1–2; urgency is derived from the due date. Drag, or press 1–4.',
    list: 'Every task, sortable by any column.',
  };
  function subhead() {
    put(document.getElementById('subhead'), html`${plural(tasks.length, 'task')} · read ${READ ? html`<time class="rel" datetime="${READ}"></time>` : 'not yet'} · ${SUB[view]}`);
  }
  function setView(v) {
    view = v;
    document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', b.dataset.view === v));
    ['board', 'matrix', 'list'].forEach(x => { const el = document.getElementById('view-' + x); el.hidden = x !== v; if (x !== v) el.replaceChildren(); });
    subhead();
    render();
  }
  // Saved views: each is a URL. The shell renders the chips and lists them in the palette.
  const VIEWS = [
    { name: 'Overdue', params: { due: 'overdue' } },
    { name: 'Due this week', params: { due: 'overdue,week' } },
    { name: 'P1 and P2', params: { p: '1,2' } },
    { name: 'No due date', params: { due: 'none' } },
  ];
  function readFilterURL() {
    const q = new URLSearchParams(location.search);
    FKEYS.forEach(key => (q.get(key) || '').split(',').filter(Boolean).forEach(v => F[key].add(v)));
    // An age v1 would not accept is dropped, as v1 dropped it; active takes 1 only, as v1's ?active=1 did.
    [...F.age].forEach(v => { if (!AGES.some(a => a.key === v)) F.age.delete(v); });
    [...F.active].forEach(v => { if (v !== '1') F.active.delete(v); });
    find.value = (q.get('q') || '').trim(); Q = find.value.toLowerCase();
    // A page is decimal digits, as on Documents (sd:2427).
    window.shell.list.listParams(q, L, { sorts: Object.keys(SORTS), size: LIST.size });
    if (!/^[1-9]\d*$/.test(q.get('page') || '')) seek = !!selected;
  }
  function writeURL() {
    const q = new URLSearchParams(); q.set('view', view);
    FKEYS.forEach(key => { if (F[key].size) q.set(key, [...F[key]].join(',')); });
    if (Q) q.set('q', Q);
    if (view === 'list') window.shell.list.listQuery(q, L, LIST);
    shell.url(q); // the shell keeps ?row=
  }
  document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));

  // ---------- Shapeshift: add or find ----------
  const inp = document.getElementById('shift-in'), prev = document.getElementById('shift-preview'), ghost = document.getElementById('shift-ghost');
  function nextWeekday(w) { const d = new Date(TODAY); let n = (w - d.getDay() + 7) % 7 || 7; return plus(n); }
  function parse(text) {
    const out = { title: [], p: null, due: null, repo: null, find: null };
    const toks = text.trim().split(/\s+/).filter(Boolean);
    for (let i = 0; i < toks.length; i++) {
      const w = toks[i], lw = w.toLowerCase();
      let m;
      if ((m = lw.match(/^p([1-4])$/))) { out.p = +m[1]; continue; }
      if ((m = lw.match(/^#(\d{1,})$/))) { out.find = m[1]; continue; }
      if ((m = lw.match(/^#([a-z][\w./-]*)$/))) { out.repo = m[1]; continue; }
      if (lw === 'due' && toks[i + 1]) {
        const n = toks[i + 1].toLowerCase();
        const wd = WD.findIndex(d => n.startsWith(d));
        if (n === 'today') out.due = plus(0);
        else if (n === 'tomorrow' || n === 'tmrw') out.due = plus(1);
        else if (/^\d{4}-\d{2}-\d{2}$/.test(n)) out.due = n;
        else if (wd >= 0) out.due = nextWeekday(wd);
        if (out.due) { i++; continue; }
      }
      out.title.push(w);
    }
    out.title = out.title.join(' ');
    return out;
  }
  // build: #repo names a repository by the label this page shows; the write needs its path.
  const repoFor = name => tasks.find(t => t.repo.toLowerCase() === name)?.repo_path || null;
  function showPreview() {
    const txt = inp.value, P = parse(txt);
    ghost.hidden = !!txt;
    if (!txt.trim()) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); return; }
    const found = P.find ? tasks.filter(t => String(t.id) === P.find) : P.title.length > 2 ? tasks.filter(t => t.title.toLowerCase().includes(P.title.toLowerCase())).slice(0, 3) : [];
    const chips = [P.p && `P${P.p}`, P.due && `due ${fmt(P.due)}`, P.repo && `#${P.repo}${repoFor(P.repo) ? '' : ' (not shown here)'}`].filter(Boolean);
    put(prev, html`${(P.find ? '' : html`<div class="as"><span class="label">Enter adds</span>${ICON('plus')}<span>${P.title || '(title needed)'}</span></div>
      <div class="chips">${(chips.length ? chips.map(c => html`<span class="chip" aria-pressed="true">${c}</span>`) : html`<span class="hint">No fields parsed; it lands in Planning with no priority.</span>`)}<span class="chip">planning</span></div>`) }${
      (found.length ? html`<p class="hint">${P.find ? 'Enter opens' : 'Matching'}</p><div class="suggest">${found.map(t => html`<button type="button" data-open-find="${t.key}">${ICON('search')}<span>${label(t)} · ${t.title}</span></button>`)}</div>` : P.find ? html`<p class="hint">No task #${P.find} here.</p>` : '') }${
      html`<p class="hint">Enter commits · Esc clears</p>`}`);
    prev.hidden = false; inp.setAttribute('aria-expanded', 'true');
  }
  inp.addEventListener('input', showPreview);
  inp.addEventListener('focus', showPreview);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Escape') { if (inp.value) { e.stopPropagation(); inp.value = ''; showPreview(); } return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const P = parse(inp.value);
    if (P.find) { const t = tasks.find(x => String(x.id) === P.find); if (t) { inp.value = ''; showPreview(); select(t.key, true); } return; }
    if (!P.title) return;
    if (P.repo && !repoFor(P.repo)) { toast(`Not added: no repo #${P.repo} is shown on this page.`); return; }
    // build: files it with POST /api/items (workflow.capture_task, sd task add). No verb deletes a task, so no Undo.
    const body = { title: P.title, ...(P.p ? { priority: P.p } : {}), ...(P.due ? { due: P.due } : {}), ...(P.repo ? { repo: repoFor(P.repo) } : {}) };
    inp.value = ''; showPreview();
    // The new task is selected only where it shows: a filter that hides it keeps the selection on a visible one (review, PR #46).
    window.shell.post('/api/items', body).then(out => reread().then(() => { const key = String(out.item.id), t = byKey(key);
      if (!t || !passes(t)) { toast(`Added #${out.item.id} to Planning · the filters hide it; Clear all shows it`); return; }
      select(key, false); landed(key); toast(`Added #${out.item.id} to Planning`); }),
      err => { inp.value = [P.title, P.p && `p${P.p}`, P.due && `due ${P.due}`, P.repo && `#${P.repo}`].filter(Boolean).join(' '); toast(`Not added: ${err.message}`); });
  });
  prev.addEventListener('click', e => { const b = e.target.closest('[data-open-find]'); if (b) { inp.value = ''; showPreview(); select(b.dataset.openFind, true); } });
  document.addEventListener('click', e => { if (!e.target.closest('#shift')) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); } });

  // build: capture (n) files through the routes v1 uses: POST /api/items for a task or followup, POST /api/items/<id>/notes for
  // a note on the selected task, with that task's revision.
  window.SHELL_CAPTURE = async ({ kind, title, item }) => {
    if (kind === 'note') {
      const t = byKey(String(item)); if (!t) throw new Error('Not filed: pick a task for the note.');
      await write(t.key, x => `/api/items/${x.id}/notes`, { body: title, kind: 'comment' }); redrawDet(t.id);
      return `Note added to ${label(t)}`;
    }
    const out = await window.shell.post('/api/items', { title, ...(kind === 'followup' ? { kind: 'followup', ...(item ? { followup_of: item } : {}) } : {}) });
    await reread();
    return `Captured #${out.item?.id}: ${out.item?.title || title}`;
  };

  // ---------- Start (build, sd:2484: shell.read reads /api/tasks, then draws; it rereads after each landed write) ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws; a task the read no longer
  // lists loses its pick and runs no command; a failed load leaves no row; and a failed reread after a write keeps the rows
  // and says the write landed. The Details keep their own per-item generation (DET.gen).
  let drawn = false;
  function draw() {
    if (!drawn) {
      // The address is read before the first draw: that draw's reconcile selects the first shown card and rewrites ?row=.
      drawn = true;
      const q = new URLSearchParams(location.search), row = shell.row();
      if (row && byKey(row)) selected = row;
      readFilterURL();
      setView(['list', 'board', 'matrix'].includes(q.get('view')) ? q.get('view') : 'board');
    } else { subhead(); render(); }
    attention();
  }
  const reading = shell.read({
    source: '/api/tasks', what: 'the tasks',
    adopt: doc => { tasks = doc.rows.map(shape); READ = doc.read; AGES = doc.ages || [];
      return { objects: tasks.map(objectOf), state: tasks.length ? null : { kind: 'empty', text: 'No open task, and none done this week.', source: '/api/tasks' } }; },
    clear: () => { tasks = []; READ = null; },
    draw,
    // draw() has already reconciled the selection against the cards the filters show, so the reader settles on that one.
    current: () => selected,
    first: () => selected,
    select: key => select(key, false),
    unselect: () => { selected = null; renderDetails(); },
  });
  function load() { return reading.load(); }
  function reread() { return reading.reread(); }
  load();
  suggest(['What is overdue across repos?', 'Which ready tasks are oldest?', 'Draft the reply that closes the selected task']);
});
