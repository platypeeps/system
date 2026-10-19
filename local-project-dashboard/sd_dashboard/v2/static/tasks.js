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
  { label: 'Start next ready tasks', icon: 'play', run: () => document.dispatchEvent(new CustomEvent('tasks:start-next')) },
];
// build (places): the views filter the one board, matrix and list, so they have no view-<id> panel: the switch goes in
// #viewswitch. sd:5064: Personal, then the realms the dashboard.realms row names (SHELL_REALMS, from /ui/sections.js), then HOA, Home
// and All. A realm named list, board or matrix is dropped: the shell would hide that layout panel (view-<id>, sd:5114).
// Home takes v o (g o opens the Home page), since v h is HOA's. A realm takes the first letter of its id that no
// other view, and not the list, board and matrix keys (v l, v b, v m), holds.
window.PAGE_VIEWS = [{ id: 'personal', label: 'Personal' },
  ...(window.SHELL_REALMS || []).filter(r => /^[a-z0-9][a-z0-9-]*$/.test(r.id) && !['personal', 'hoa', 'home', 'all', 'list', 'board', 'matrix'].includes(r.id)).map(r => ({ id: r.id, label: r.label })),
  { id: 'hoa', label: 'HOA', key: 'h' }, { id: 'home', label: 'Home', key: 'o' }, { id: 'all', label: 'All', key: 'a' }];
{
  const used = new Set(['l', 'b', 'm', ...window.PAGE_VIEWS.map(v => v.key).filter(Boolean)]);
  window.PAGE_VIEWS.forEach(v => { if (!v.key) { v.key = [...v.id].find(c => /[a-z]/.test(c) && !used.has(c)); used.add(v.key); } });
}
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
    let tasks = [], READ = null, AGES = [], ATTENTION = null;
  const shape = r => ({ id: r.id, key: String(r.id), title: r.title, repo: r.repo || 'no repo', repo_path: r.repo_path, p: r.priority, due: r.due,
    status: r.status, since: r.status_since, kind: r.kind, urgent: !!r.urgent, urgentOtherwise: !!r.urgent_otherwise, assignment: r.assignment, age: r.age, recurrence: r.recurrence, anchor: r.recurrence_anchor ?? null, nextDue: r.next_due ?? null, revision: r.revision, allowed: r.allowed, edit: r.edit, run: r.run, parked: r.parked ?? null,
    base: r.base ?? null, stage: r.stage ?? null, score: r.score ?? null, area: r.area ?? null, realm: r.realm ?? null, issueTargets: r.issue_targets ?? null, issueLink: r.issue_link ?? null, real: true });
  const byKey = k => tasks.find(t => t.key === k);
  // The rows every view lists: a parked row shows only under the Parked chip (sd:3012).
  const listed = () => tasks.filter(t => !t.parked);
  // build (places): the PAGE_VIEWS above. A row's area is the server's (places.area_of), the rule the HOA and Home pages'
  // Tasks views use, so the HOA and Home views hold their rows; its realm is places.realm_of (sd:5064).
  let PLACE = 'all', REALMS = null;
  // sd:5096, sd:5097: the Personal and Work sweeps, as /api/tasks' `sweeps` reads them (sweep.status): running, the last run.
  let SWEEPS = null, sweepTimer = 0;
  const SWEEP_NAME = { personal: 'Personal', work: 'Work' }, SWEEP_POLL = 15000;
  const sweepOf = o => SWEEPS?.[String(o.id).slice(6)] || null;
  const sweepText = w => !w ? '' : w.error ? `sweep not read: ${w.error}` : w.running ? `sweep running${w.last?.started ? ` since ${w.last.started.slice(11, 16)} UTC` : ''}`
    : !w.last || w.last.state === 'never' ? 'sweep not run yet' : w.last.state === 'done' ? `sweep done${w.last.finished ? ` ${w.last.finished.slice(0, 16).replace('T', ' ')} UTC` : ''}${w.last.gaps?.length ? ` · not read: ${w.last.gaps.join(', ')}` : ''}`
    : `sweep ${w.last.state}${w.last.exit != null ? `, exit ${w.last.exit}` : ''}${w.last.reason ? `: ${w.last.reason}` : ''}`;
  const inPlace = t => PLACE === 'all' || (PLACE === 'hoa' || PLACE === 'home' ? t.area === PLACE : t.realm === PLACE);
  const placed = () => listed().filter(inPlace);
  const label = t => `#${t.id}`;
  // A write readback is workflow.item_state: fold its item into the row, so the row updates where it is.
  function absorb(key, state) {
    const t = byKey(key); if (!t || !state?.item) return;
    if (t.due !== state.item.due || t.recurrence !== state.item.recurrence) t.nextDue = undefined; // not known until the rows are read again
    const dueMoved = t.due !== state.item.due;
    Object.assign(t, { status: state.item.status, p: state.item.priority, due: state.item.due, recurrence: state.item.recurrence,
      anchor: state.item.recurrence_anchor ?? null, revision: state.revision, title: state.item.title ?? t.title, stage: t.base ? state.item.stage : null });
    if ('repo' in state.item && state.item.repo !== t.repo_path) Object.assign(t, { repo_path: state.item.repo,
      repo: state.item.repo ? tasks.find(x => x.repo_path === state.item.repo)?.repo || state.item.repo.split('/').pop() : 'no repo' });
    // The server's urgency is is_urgent, and urgent_otherwise is is_urgent without its due rule; after a due edit the page
    // applies that rule to the new date until the rows are read again.
    if (dueMoved) t.urgent = t.urgentOtherwise || withinWeek(t);
    if ('body' in state.item) DET.body[t.id] = { text: bodyText(state.item.body), rev: state.revision };
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
    if (t.due && days(t) <= 2) return 'caution';
    return '';
  }
  const GLYPH = { warning: '■', caution: '▲' };

  // Page attention: the read's own badge (sd:3446), the rule every page's rail shows: v2/pages/tasks.py. build: set after each read.
  function attention() {
    window.shell.attention?.(READ && ATTENTION ? ATTENTION : { state: 'unknown', n: 0, what: 'tasks not read' });
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
    if (t.assignment === 'running') return { ok: false, reason: 'A runner assignment is running. Status is locked until it ends.' };
    if (t.assignment === 'queued') return { ok: false, reason: 'A runner assignment is queued. Status is locked until it ends.' };
    if (t.kind === 'work' && to === 'done') return { ok: false, reason: 'Work items close through verified completion (sd work), not a status write.' };
    if (!(t.allowed || []).includes(to)) return { ok: false, reason: t.allowed?.length ? `sd task status refuses ${slabel(to)} for this ${t.kind} item.` : `A ${t.kind} item takes no status write here; it has its own workflow.` };
    return { ok: true };
  }
  function quadChange(t, q) {
    const Q = QUADS.find(x => x[0] === q);
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
  // build: the row carries what sd_db.workflow.edit_item takes (tasks_screen._edit_capability): a kind in DETAIL_KINDS.
  const EDITABLE = t => !!t?.edit?.allowed;
  const noEdit = t => t.edit?.reason || `a ${t.kind} item uses its own editing workflow`;
  // build: sd_db.workflow.TASK_STATUS_KINDS; workflow._recurring refuses a rule on any other kind.
  const RECURS = ['task', 'personal', 'followup'];
  // build (sd:3012): sd_db.progress.CANCELLABLE_TASK_KINDS, the kinds task_guard lets sd task cancel close.
  const CLOSES = ['task', 'followup'];
  const repeats = t => !!t?.recurrence;
  // build (sd:2250): sd stores a rule only from sd_db.recurrence.PARTS. A rule with any other part (COUNT), written around
  // the library, cannot be walked: completing it ends the series. test_v2_tasks holds this set to the library's.
  const PARTS = new Set(['FREQ', 'INTERVAL', 'BYMONTH', 'BYMONTHDAY', 'BYDAY']);
  const walkable = t => String(t.recurrence || '').replace(/^RRULE:/i, '').split(';').every(p => PARTS.has(p.split('=')[0].trim().toUpperCase()));
  // A completion that opens nothing: a rule sd cannot walk, or one whose next date workflow.next_occurrence_due says is none.
  const ends = t => !walkable(t) || t.nextDue === null;
  const cliMove = (t, to) => `sd task status ${t.id} ${to}`;
  function cliQuad(t, ch) {
    const a = [];
    if ('p' in ch) a.push(ch.p ? `--priority ${ch.p}` : '--clear-priority');
    if ('due' in ch) a.push(ch.due ? `--due ${ch.due}` : '--clear-due');
    if ('repo' in ch) a.push(ch.repo ? `--belongs-to ${window.shell.shq(ch.repo)}` : '--no-repo');
    if ('title' in ch) a.push(`--title ${window.shell.shq(ch.title)}`);
    if ('body' in ch) a.push(`--body ${window.shell.shq(ch.body)}`);
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
  // build (sd:3012): Parked swaps the listed rows for the ones the nightly prune parked, for triage's Unpark.
  // build (sd:3977): Base narrows to one vault base's rows; it shows once a row has a base.
  const F = { kind: new Set(), base: new Set(), repo: new Set(), p: new Set(), due: new Set(), status: new Set(), age: new Set(), active: new Set(), parked: new Set() };
  const FKEYS = Object.keys(F);
  const find = document.getElementById('find-in');
  let Q = '';
  const on = () => FKEYS.reduce((n, k) => n + F[k].size, 0) + (Q ? 1 : 0);
  const dueBucket = t => t.overdue || (t.due && days(t) < 0) ? 'overdue' : !t.due ? 'none' : withinWeek(t) ? 'week' : 'later';
  const DUE_OPTS = [['overdue', 'overdue'], ['week', '≤ 7 days'], ['later', 'later'], ['none', 'no due']];
  function renderFilters() {
    const repos = [...new Set(tasks.map(t => t.repo))].sort(), kinds = [...new Set(tasks.map(t => t.kind))].sort();
    const bases = [...new Set(tasks.map(t => t.base).filter(Boolean))].sort();
    // The board's statuses, then any other a row or the address names (ready_to_send has no column), so a chip shows it.
    const statuses = [...STATUSES.map(([st]) => st), ...new Set([...tasks.map(t => t.status), ...F.status].filter(st => !SLABEL[st]))];
    // build (sd:3441, sd:5064, sd:5158): every filter is a dropdown (shell.list.pick), one value or All, and Repo a menu of
    // checkboxes, any repos at once; the active-filter chips under them are the only chips. Values the address names that no
    // row has are options too, and more than one (?p=1,2) shows as a count until one is picked. Each option is [value, label];
    // `all` names the empty choice.
    const combo = (key, name, opts, help, all = 'All', multi = false) => window.shell.list.pick(key, name,
      [...new Map([...opts, ...[...F[key]].map(v => [v, fvalue(key, v)])])], F[key], { all, help, multi });
    const same = vs => vs.map(v => [v, v]);
    put(document.getElementById('filters'), html`${combo('kind', 'Kind', same(kinds))}${bases.length || F.base.size ? combo('base', 'Base', same(bases)) : ''}${combo('repo', 'Repo', same(repos), '', 'All', true)}${
      combo('p', 'Priority', [['1', 'P1'], ['2', 'P2'], ['3', 'P3'], ['4', 'P4'], ['', 'unset']].map(([v, l]) => [v || 'unset', l]))}${
      combo('due', 'Due', DUE_OPTS, html`<button class="help" type="button" aria-label="Help: due filter" data-help="Buckets count from today. <b>≤ 7 days</b> is the same window the matrix calls urgent. An old link may name two buckets; they widen the view, and filters narrow it.">${ICON('circle-help')}</button>`)}${
      combo('status', 'Status', statuses.map(st => [st, slabel(st)]))}${
      combo('age', 'In status', AGES.map(a => [a.key, a.label]), html`<button class="help" type="button" aria-label="Help: in status filter" data-help="Days since the task's last status change, in the buckets the Operations Progress histogram counts (<code>reads.age_bucket</code>). A bar there opens its bucket here.">${ICON('circle-help')}</button>`)}${
      combo('active', 'Scope', [['1', 'open only']], html`<button class="help" type="button" aria-label="Help: scope filter" data-help="<b>open only</b> hides Done tasks, as the Operations histogram counts only open ones.">${ICON('circle-help')}</button>`, 'Done too')}${
      combo('parked', 'Parked', [['1', 'parked only']], html`<button class="help" type="button" aria-label="Help: parked filter" data-help="The nightly prune parks an open P4 task untouched for 30 days. Parked tasks stay off every other view; <b>parked only</b> lists them, and <b>Unpark</b> brings one back with its fields and history.">${ICON('circle-help')}</button>`, 'Not parked')}${
      (on() ? window.shell.list.chips(activeFilters(), visible().length, placed().length) : html`<div class="fsum">${ICON('filter')}<span>${placed().length} tasks</span></div>`)}`);
  }
  // Active filters above the list (shell.list, sd:2682): one chip per value, keyed field:value, and one for the text.
  const FNAME = { kind: 'Kind', base: 'Base', repo: 'Repo', p: 'Priority', due: 'Due', status: 'Status', age: 'In status', active: 'Scope', parked: 'Parked' };
  const fvalue = (k, v) => k === 'p' ? (v && v !== 'unset' ? 'P' + v : 'unset') : k === 'due' ? DUE_OPTS.find(o => o[0] === v)?.[1] || v : k === 'status' ? slabel(v)
    : k === 'age' ? AGES.find(a => a.key === v)?.label || v : k === 'active' ? 'open only' : k === 'parked' ? 'parked only' : v;
  const activeFilters = () => [...FKEYS.flatMap(k => [...F[k]].map(v => ({ key: `${k}:${v}`, label: `${FNAME[k]}: ${fvalue(k, v)}` }))), ...(Q ? [{ key: 'q', label: `Text: ${Q}` }] : [])];
  document.getElementById('filters').addEventListener('click', e => {
    if (e.target.closest('[data-unfilter-all]')) { clearFilters(); return; }
    const u = e.target.closest('[data-unfilter]'); if (!u) return;
    const [k, v] = u.dataset.unfilter.split(/:(.*)/);
    if (k === 'q') { Q = ''; find.value = ''; } else F[k].delete(v);
    L.page = 1; render();
  });
  document.getElementById('filters').addEventListener('change', e => window.shell.list.pickChange(e, F, () => { L.page = 1; render(); }));
  function clearFilters() { Object.values(F).forEach(s => s.clear()); Q = ''; find.value = ''; L.page = 1; render(); }
  // The text filter narrows on what a row shows: id, title, repo, status and kind, as v1's ?q= read the visible columns.
  find.addEventListener('input', () => { Q = find.value.trim().toLowerCase(); L.page = 1; render(); });
  find.addEventListener('keydown', e => { if (e.key === 'Escape' && find.value) { e.stopPropagation(); find.value = ''; Q = ''; L.page = 1; render(); } });
  const words = t => `#${t.id} ${t.title} ${t.repo} ${slabel(t.status)} ${t.kind} ${t.stage || ''}`.toLowerCase();
  const passes = t => (!F.kind.size || F.kind.has(t.kind)) && (!F.base.size || F.base.has(t.base)) && (!F.repo.size || F.repo.has(t.repo)) && (!F.p.size || F.p.has(t.p ? String(t.p) : 'unset')) && (!F.due.size || F.due.has(dueBucket(t)))
    && (!F.status.size || F.status.has(t.status)) && (!F.age.size || F.age.has(t.age)) && (!F.active.size || t.status !== 'done') && !t.parked === !F.parked.size && inPlace(t) && (!Q || words(t).includes(Q));
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
  // DET.body keeps the last description read or written back: a write drops the reading, not the description field, so a
  // redraw before the next read still draws it and keeps the operator's draft (review 2026-10-12, lane entry 225).
  // The stored body is JSON, {"text": …} (workflow._fields), and the field edits its text, as the classic item page does
  // (controls.py); null marks a body that does not parse, whose field is withheld so no save overwrites it. Each cached
  // description and reading carries the revision it was read at: a row read at another revision makes it stale (adopt).
  const bodyText = stored => { try { const b = JSON.parse(stored || '{}'); return b && typeof b === 'object' && !Array.isArray(b) ? String(b.text ?? '') : ''; } catch { return null; } };
  const DET = { items: {}, reading: {}, failed: {}, gen: {}, body: {} }, detOf = t => (t && t.id && DET.items[t.id]) || null;
  async function readDet(id, force) {
    if (!id || (!force && (DET.items[id] || DET.reading[id] === (DET.gen[id] || 0)))) return;
    const gen = DET.gen[id] || 0;
    DET.reading[id] = gen; delete DET.failed[id];
    let got, err;
    try { got = await window.shell.getJSON(`/api/tasks/${id}`); } catch (e) { err = e; }
    if ((DET.gen[id] || 0) !== gen) return; // a write landed meanwhile; the read it started is the one that counts
    delete DET.reading[id];
    if (err) DET.failed[id] = err.message; else { DET.items[id] = got; DET.body[id] = { text: bodyText(got.item.body), rev: got.revision }; }
    putAll(); if (byKey(selected)?.id === id) renderDetails();
  }
  const staleDet = id => { DET.gen[id] = (DET.gen[id] || 0) + 1; delete DET.items[id]; delete DET.reading[id]; };
  // The rows' objects are the reader's (adopt below): it puts them after each read and retires a row the read stopped
  // listing. The Details' notes and assignments follow their row: once it is not listed, they run nothing either.
  const objectOf = t => ({ id: t.key, type: 'item', label: `${label(t)} ${t.title}`, item: t.id });
  const live = key => { const o = C.get(key); return !!o && o.type !== 'not listed'; };
  function putAll() {
    Object.values(DET.items).forEach(d => {
      const as = live(String(d.item.id)) ? o => o : o => ({ id: o.id, type: 'not listed', label: `${o.label} (no longer listed)` });
      d.notes.forEach(n => C.put(as({ id: `note:${n.id}`, type: 'note', label: `${n.kind} ${n.id} on #${d.item.id}`, note: n.id, kind: n.kind, resolved: n.resolved, item: d.item.id })));
      d.assignments.forEach(a => C.put(as({ id: `asg:${a.id}`, type: 'assignment', label: `Assignment #${a.id}`, n: a.id, status: a.status, item: d.item.id, repo: d.item.repo, can: a.cancel })));
    });
    ISSUE_RUNS.forEach(r => C.put(issueObject(r)));
  }
  const redrawDet = id => { staleDet(id); readDet(id, true); };
  // ---------- File issue (sd:5103, issue_filing) ----------
  // The latest run an item's File GitHub issue or File Jira issue started on this page, read again every 3 s while it drafts
  // or files. Each run is an `issue draft` object, so its File issue and Discard are registered commands, drawn in Details.
  const TRACKER = { github: 'GitHub', jira: 'Jira' }, ISSUE_POLL = 3000, ISSUE_RUNS = new Map();
  const issueBusy = r => ['queued', 'drafting', 'filing'].includes(r.state);
  const issueWhere = x => x.kind === 'github' ? `GitHub ${x.repo}` : `Jira project ${x.project}`;
  const issueObject = r => ({ id: `issue:${r.id}`, type: 'issue draft', label: `${TRACKER[r.kind]} issue for #${r.item}`, run: r });
  function followIssue(run) {
    ISSUE_RUNS.set(run.item, run); C.put(issueObject(run));
    if (byKey(selected)?.id === run.item) renderDetails();
    if (issueBusy(run)) setTimeout(() => readIssue(run), ISSUE_POLL);
  }
  // A failed read is not known, not settled: Details says so and the next read tries again. A newer run stops the old one's.
  function readIssue(run) {
    if (ISSUE_RUNS.get(run.item)?.id !== run.id) return;
    window.shell.getJSON(`/api/issue-runs/${encodeURIComponent(run.id)}`).then(followIssue, e => followIssue({ ...run, unread: e.message }));
  }
  // The draft in Details: while it drafts, what runs; once ready, the exact target, title and body File issue sends; else
  // the reason, which names a category or a line, never the matched value, so a blocked draft shows no text.
  function issuePane(t) {
    const r = ISSUE_RUNS.get(t.id); if (!r || r.state === 'discarded') return '';
    const name = TRACKER[r.kind], unread = r.unread ? html`<p class="why">Its state was not read: ${r.unread}. This page tries again in 3 s.</p>` : '';
    const head = html`<h3>${name} issue <span class="n">run ${r.id} · ${r.state}</span></h3>`;
    if (issueBusy(r)) return html`<section aria-live="polite">${head}<p class="why">${r.state === 'filing' ? `Filing in ${issueWhere(r.target)}. Wait for the answer before you file it again.`
      : 'Claude drafts the issue with no tools; the sanitizer then strips internal names and checks the privacy patterns. Nothing is filed.'}</p>${unread}</section>`;
    if (r.state === 'linked') return html`<section aria-live="polite">${head}<p class="why">Filed: <a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.url}</a> · #${r.item} links it.</p></section>`;
    const removed = Object.entries(r.removed || {}).map(([k, n]) => `${n} ${k}`).join(', ');
    const preview = r.state === 'ready' ? html`<dl><dt>Target</dt><dd>${issueWhere(r.target)}</dd><dt>Title</dt><dd>${r.title}</dd></dl>
      <pre class="body">${r.body}</pre><p class="why">${removed ? `The sanitizer took out ${removed}.` : 'The sanitizer took nothing out.'} File issue sends exactly this text.</p>` : '';
    const filed = r.state === 'filed' ? html`<p class="why">Filed at <a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.url}</a>.</p>` : '';
    return html`<section aria-live="polite">${head}${filed}${preview}${r.reason ? html`<p class="why">${r.reason}</p>` : ''}${unread}${C.bar(`issue:${r.id}`)}</section>`;
  }
  // note.resolve: one declaration, copied on every page that lists followups (Tasks, Today; commands.md § Shared declarations).
  const NOTE_RESOLVE = done => ({ id: 'note.resolve', on: 'note', label: 'Resolve', key: 'v', risk: 'confirm', icon: 'check', primary: o => o.kind === 'followup' && !o.resolved,
    when: o => o.kind !== 'followup' ? `a ${o.kind} note has nothing to resolve` : o.resolved ? `resolved ${o.resolved.slice(0, 10)}` : true,
    cli: o => `sd note resolve ${o.note}`, consequence: () => 'Closes the followup. sd note has resolve and list only, so no verb reopens it.',
    run: o => done(o) });
  const rowOr = (o, f) => { const t = T(o); return t ? f(t) : 'no CLI: the task is no longer listed'; };
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
    id: `item.status.${s}`, on: 'item', label: `Status → ${name}`, key: String(i + 1), risk: 'undo', bulk: true, icon: 'kanban', bar: false,
    primary: o => view === 'list' && NEXT[T(o)?.status] === s && !(s === 'done' && repeats(T(o))),
    when: o => { const t = T(o); return t ? moveOn(s, t) : 'the task is no longer listed'; },
    cli: o => rowOr(o, t => cliMove(t, s)),
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
  const COMPLETE = { id: 'item.complete', on: 'item', label: 'Done → next occurrence', key: '5', risk: 'confirm', executes: true, icon: 'calendar-check', bar: false,
    primary: o => view === 'list' && NEXT[T(o)?.status] === 'done' && repeats(T(o)),
    when: o => { const t = T(o); return t ? completeOn(t) : 'the task is no longer listed'; },
    cli: o => rowOr(o, t => cliMove(t, 'done')),
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
  // revision the edit answered. A field the row does not carry (the description) takes `before(row)`, read then too.
  // `check` asks as the edit leaves whether it still changes anything: a second one queued
  // behind the first would write nothing, and its Undo would set back what the first one changed (review, PR #46).
  const FIELD = { anchor: 'recurrence_anchor', repo_path: 'repo' };
  // `before` holds a field the row does not carry (the description, read with the Details).
  function editRun(t, ch, msg, { check, keep = Object.keys(ch), before = () => ({}) } = {}) {
    let was = {};
    const sent = r => { const b = before(r); was = Object.fromEntries(keep.map(k => [FIELD[k] || k, k in r ? r[k] ?? null : b[k] ?? null])); };
    return landing(edit(t.key, ch, { sent, check }).then(v => { landed(t.key); return v; }), msg, v => edit(t.key, was, { rev: v.revision }));
  }
  // sd:5295: the Details' Status, Priority and Due fields do what Status →, Done → next occurrence, Edit → P1-4, Edit and Move
  // to do, so those stay off its Act bar (`bar: false`); 1-5, e, m, the ⋯ menu, the palette and the bulk bar still run them.
  C.register(
    ...Object.values(STATUS_CMD), COMPLETE,
    // Edit sets priority and due date in one dialog. Its save carries the Undo, so the shell's own feedback stays safe.
    { id: 'item.edit', on: 'item', label: 'Edit', key: 'e', risk: 'safe', icon: 'pen-line', bar: false,
      when: o => { const t = T(o); return !t ? 'the task is no longer listed' : EDITABLE(t) || noEdit(t); },
      cli: o => rowOr(o, t => `sd task edit ${t.id} --priority ${t.p || 'N'} ${t.due ? '--due ' + t.due : '--due YYYY-MM-DD'}`),
      run: o => { askEdit(T(o)); return `Editing ${label(T(o))}`; } },
    // Move to: the status moves without drag (touch, or anyone). One form lists every column; a refused one is off with its reason.
    { id: 'item.move', on: 'item', label: 'Move to', key: 'm', risk: 'safe', icon: 'kanban', bar: false,
      primary: () => view === 'board' && coarse.matches,
      when: o => { const t = T(o); if (!t) return 'the task is no longer listed'; const L = STATUSES.map(([s]) => legal(t, s)); return L.some(x => x.ok) || L.find(x => x.reason !== 'Already here.').reason; },
      cli: o => rowOr(o, t => `sd task status ${t.id} <status>`),
      run: o => { askMove(T(o)); return null; } },
    // Priorities 1–4, workflow.edit_item's range, each a bulk edit with Undo (sd:3012 triage re-prioritizes in bulk). A row
    // already at the priority is refused as its write leaves, not by `when`: the bar offers only what every picked row is
    // on for, so a pick that mixes priorities would otherwise offer none.
    ...[1, 2, 3, 4].map(n => ({ id: `item.p${n}`, on: 'item', label: `Edit → P${n}`, risk: 'undo', bulk: true, icon: 'flag-triangle-right', bar: false,
      when: o => { const t = T(o); return !t ? 'the task is no longer listed' : EDITABLE(t) || noEdit(t); },
      cli: o => rowOr(o, t => `sd task edit ${t.id} --priority ${n}`),
      run: o => { const t = must(o), was = t.p; return editRun(t, { p: n }, () => `${label(t)} P${was || '–'} → P${n}`, { check: r => r.p !== n || `it is already P${n}` }); },
      undo: undoOf })),
    // Close is sd task cancel (sd:3012): done, with a cancelled receipt and the reason, through progress.task_guard. A bulk
    // close asks once for one reason and runs every picked row with its own revision. No Undo: no sd verb takes it back.
    { id: 'item.close', on: 'item', label: 'Close', risk: 'confirm', executes: true, bulk: true, icon: 'ban',
      when: o => { const t = T(o); return !t ? 'the task is no longer listed' : !CLOSES.includes(t.kind) ? `sd task cancel closes a task or followup; this is a ${t.kind} item`
        : t.status === 'done' ? 'it is done' : repeats(t) ? 'a recurring task cannot be cancelled; clear its recurrence first, or complete it'
        : t.assignment === 'running' || t.assignment === 'queued' ? `a runner assignment is ${t.assignment}` : true; },
      fields: () => [{ name: 'reason', label: 'Reason', required: true, placeholder: 'why nobody will do it', help: 'Recorded on each closed task. A close without a reason is refused.' }],
      cli: (o, v = {}) => rowOr(o, t => `sd task cancel ${t.id} --reason ${v.reason ? window.shell.shq(v.reason) : "'<why>'"}`),
      sends: o => `POST /api/items/${T(o).id}/cancel-task {reason}`,
      consequence: () => 'Each task closes as done, with a cancelled receipt and your reason. No sd verb takes it back.',
      run: (o, v) => { const t = must(o);
        return landing(write(t.key, x => `/api/items/${x.id}/cancel-task`, { reason: v.reason }), () => `${label(t)} closed · sd task cancel ${t.id} --reason ${window.shell.shq(v.reason)}`); } },
    // Unpark (sd:3012) clears parked_at through the item edit route, as the item page's Unpark does (sd:3007). No Undo:
    // only the nightly prune parks, so no write takes it back.
    { id: 'item.unpark', on: 'item', label: 'Unpark', risk: 'safe', executes: true, bulk: true, icon: 'archive-restore',
      when: o => { const t = T(o); return !t ? 'the task is no longer listed' : !t.parked ? 'it is not parked' : true; },
      cli: o => rowOr(o, t => `sd task edit ${t.id} --unpark`),
      sends: o => `POST /api/items/${T(o).id} {parked_at: null}`,
      run: o => { const t = must(o);
        return landing(write(t.key, x => `/api/items/${x.id}`, { parked_at: null }), () => `${label(t)} unparked · sd task edit ${t.id} --unpark`); } },
    { id: 'item.note', on: 'item', label: 'Note', key: 'n', risk: 'safe', icon: 'notebook-pen',
      when: o => !!T(o) || 'the task is no longer listed',
      cli: o => rowOr(o, t => `sd task note ${t.id} "…" --kind comment`),
      run: o => { window.shell.capture(o); const r = document.querySelector('dialog.capture input[value="note"]'); if (r && !r.disabled) { r.checked = true; r.form.dispatchEvent(new Event('input')); } return 'Write the note'; } },
    // Fix (sd:3456): the shared declaration (commands.js). The post carries the row's revision, so a click from a page read
    // before an earlier Fix's note is stale; the Details read again after it.
    window.commands.fix('item', { item: o => T(o).id, report: o => T(o).kind === 'report',
      when: o => { const t = T(o); return !t ? 'the task is no longer listed' : t.status === 'done' ? 'it is done' : true; },
      post: (o, path) => { const t = must(o);
        return write(t.key, () => path, {}).then(v => { redrawDet(t.id); return v; }, err => { if (err.stale) refused(); throw err; }); } }),
    // Adversarial review (sd:5064): the shared declaration (commands.js); the server reviews the item in its repository,
    // so an item with none is off.
    window.commands.adversarialReview('item', { payload: o => ({ kind: 'task', target: T(o).id }),
      when: o => { const t = T(o); return !t ? 'the task is no longer listed' : !t.repo_path ? 'it has no repository to review in' : true; } }),
    // Draft email (sd:3463, email_draft): claude writes an email from the item's title and notes, and workspace-mcp saves it
    // as a Gmail draft with no recipient. Nothing sends it. It asks first, since it runs a model and writes to Gmail. An
    // answered refusal reads the rows again: a failure writes a note, so the row's revision moved. A post with no answer
    // may have saved a draft, so it says that. No Undo: the draft stays in Gmail for the operator to send or delete.
    { id: 'item.email', on: 'item', label: 'Draft email', risk: 'safe', askFirst: true, executes: true, icon: 'mail',
      when: o => { const t = T(o); return !t ? 'the task is no longer listed' : CLOSES.includes(t.kind) || `Draft email writes about a task or followup; this is a ${t.kind} item`; },
      cli: () => 'no sd verb: the dashboard runs claude -p and saves the draft through workspace-mcp',
      sends: o => `POST /api/items/${T(o).id}/draft-email {revision}`,
      consequence: o => `Claude writes an email from #${T(o).id}'s title and notes and saves it as a Gmail draft with no recipient. Nothing is sent. A note on the item names the draft.`,
      run: o => { const t = must(o);
        return write(t.key, x => `/api/items/${x.id}/draft-email`, {}).then(
          v => { redrawDet(t.id); return `Draft saved · #${t.id} · "${v.draft.subject}" · in Gmail Drafts, not sent`; },
          err => { if (!err.status) throw new Error(`no answer (${err.message}), so whether a draft was saved is not known; look in Gmail Drafts before you press Draft email again`);
            refused(); throw err; }); } },
    // File GitHub issue and File Jira issue (sd:5103, issue_filing): a tool-less claude drafts an issue about the item in the
    // background and the server strips internal names from it; nothing is filed here. Each is on where the item's repo
    // enables it (Management's github_issues and jira_project), and off with the reason otherwise.
    ...Object.entries(TRACKER).map(([kind, name]) => ({ id: `item.file-${kind}-issue`, on: 'item', label: `File ${name} issue`, risk: 'safe', executes: true, icon: 'send',
      when: o => { const t = T(o), x = t?.issueTargets?.[kind]; return !t ? 'the task is no longer listed' : !x ? "the repo's issue settings were not read" : x.ok || x.why; },
      does: () => `Drafts a public ${name} issue, internal names removed; files nothing yet.`,
      cli: () => 'no sd verb: the dashboard drafts with claude -p, then files through gh or the Jira API on File issue',
      sends: o => `POST /api/items/${T(o).id}/file-issue {revision, target: "${kind}"}`,
      run: o => { const t = must(o);
        // Not write(): the answer is a run, not the item's state, and the start changes nothing on the row.
        return window.shell.post(`/api/items/${t.id}/file-issue`, { target: kind, revision: t.revision }).then(
          run => { followIssue(run); select(t.key, true); return `Drafting ${name} issue · #${t.id} · run ${run.id}; Details shows it, and nothing is filed yet`; },
          err => { if (!err.status) throw new Error(`no answer (${err.message}), so whether a draft started is not known`); if (err.stale) refused(); throw err; }); } })),
    // The draft's commands (sd:5103). File issue posts the digest of the text Details shows, so a draft that changed is
    // refused; it asks first, naming the target, since a filed issue is public and stays. Once filed but not linked, it
    // links only. A refusal or no answer reads the run again: the server writes what is known. No Undo for either.
    { id: 'issue.file', on: 'issue draft', label: 'File issue', risk: 'confirm', executes: true, icon: 'send', primary: o => o.run.state !== 'unknown',
      when: o => ['ready', 'filed'].includes(o.run.state) || `the draft is ${o.run.state}`,
      does: o => o.run.state === 'filed' ? `Links #${o.run.item} to the filed issue; nothing is filed again.` : `Files this title and body in ${issueWhere(o.run.target)}.`,
      cli: () => 'no sd verb: the dashboard files through gh or the Jira REST API',
      sends: o => `POST /api/issue-runs/${o.run.id}/confirm {digest}`,
      consequence: o => o.run.state === 'filed' ? `Links #${o.run.item} to ${o.run.url}. Nothing is filed again.`
        : `Files the title and body shown in Details as a public issue in ${issueWhere(o.run.target)}, then links #${o.run.item} to it. The dashboard cannot delete a filed issue.`,
      run: o => window.shell.post(`/api/issue-runs/${encodeURIComponent(o.run.id)}/confirm`, { digest: o.run.digest }).then(
        run => { followIssue(run); reread(); return `${TRACKER[run.kind]} issue filed · ${run.url} · #${run.item} links it`; },
        err => { readIssue(o.run); if (!err.status) throw new Error(`no answer (${err.message}), so whether the issue was filed is not known; look in ${TRACKER[o.run.kind]} before you file it again`); throw err; }) },
    // Check again (sd:5103): a run whose outcome is not known may have filed, so it is never filed again; the server
    // lists the tracker's newest issues for this exact title and links one if found.
    { id: 'issue.check', on: 'issue draft', label: 'Check again', risk: 'safe', executes: true, icon: 'search', primary: o => o.run.state === 'unknown',
      when: o => o.run.state === 'unknown' || `the draft is ${o.run.state}`,
      does: o => `Looks for this issue in ${issueWhere(o.run.target)}; files nothing.`,
      cli: () => 'no sd verb: the dashboard lists the newest issues through gh or the Jira API',
      sends: o => `POST /api/issue-runs/${o.run.id}/check`,
      run: o => window.shell.post(`/api/issue-runs/${encodeURIComponent(o.run.id)}/check`, {}).then(
        run => { followIssue(run); reread(); return `${TRACKER[run.kind]} issue found · ${run.url} · #${run.item} links it`; },
        err => { readIssue(o.run); throw err; }) },
    { id: 'issue.discard', on: 'issue draft', label: 'Discard', risk: 'safe', executes: true, icon: 'x',
      when: o => ['ready', 'blocked', 'failed', 'unknown'].includes(o.run.state) || `the draft is ${o.run.state}`,
      does: o => o.run.state === 'unknown' ? 'Drops the run; it may have filed, so look in the tracker first.' : 'Drops the draft; nothing was filed.',
      cli: () => "no sd verb: the draft lives in the dashboard's state folder",
      sends: o => `POST /api/issue-runs/${o.run.id}/discard`,
      run: o => window.shell.post(`/api/issue-runs/${encodeURIComponent(o.run.id)}/discard`, {}).then(run => { followIssue(run); return `Draft discarded · run ${run.id}; ${o.run.state === 'unknown' ? 'whether it filed is not known' : 'nothing was filed'}`; }) },
    { id: 'item.delete', on: 'item', label: 'Delete', risk: 'confirm', icon: 'x',
      when: () => 'no CLI verb: sd task has no delete; move it to Done to keep a record',
      cli: o => rowOr(o, t => `sd task delete ${t.id}`),
      consequence: () => 'The task, its notes and its history go. Its assignments stay in the runner log. This cannot be undone; to keep a record, move it to Done instead.' },
    // Artifact and cancel are sd work verbs (review item 17). build (sd:2200, the design's sd:2199): both post to the routes v1's
    // item page uses, /api/items/<id>/(relink|cancel), with the row's revision. The text only the operator has, a moved path or a
    // reason, is a confirm field; OK stays off until it is typed. Availability is progress.work_controls, read with the Details.
    // Neither has Undo: relinking back is another relink, and no sd verb reopens a cancelled item.
    { id: 'work.relink', on: 'item', label: 'Relink', key: 'l', risk: 'safe', executes: true, icon: 'link-2',
      when: o => { const t = T(o), d = detOf(t); return !t ? 'the task is no longer listed' : t.kind !== 'work' ? `sd work relink acts on a work item; this is a ${t.kind}`
        : !d ? 'the artifact was not read for this row' : !d.work.relink ? d.work.reason : d.item.path ? true : 'the item has no artifact to relink'; },
      fields: o => { const d = detOf(T(o)); return [{ name: 'path', label: 'Moved path', required: true, placeholder: d.item.path,
        help: `Relative to ${d.item.repo}. The item keeps its history; only the artifact path changes.` }]; },
      cli: (o, v = {}) => rowOr(o, t => `sd work relink ${t.id} ${v.path ? window.shell.shq(v.path) : '<moved path>'}`),
      sends: o => `POST /api/items/${T(o).id}/relink {path}`,
      run: (o, v) => { const t = must(o);
        return landing(write(t.key, x => `/api/items/${x.id}/relink`, { path: v.path }), () => `${label(t)} relinked → ${v.path} · sd work relink ${t.id} ${window.shell.shq(v.path)}`); } },
    { id: 'work.cancel', on: 'item', label: 'Cancel', key: 'w', risk: 'confirm', executes: true, icon: 'ban',
      when: o => { const t = T(o), d = detOf(t); return !t ? 'the task is no longer listed' : t.kind !== 'work' ? `sd work cancel acts on a work item; this is a ${t.kind}`
        : t.status === 'done' ? 'the work item is done' : !d ? 'its cancel availability was not read for this row' : d.work.cancel || d.work.reason; },
      fields: () => [{ name: 'reason', label: 'Reason', required: true, placeholder: 'why the work stops', help: 'Recorded with the cancellation. A cancel without a reason is refused.' }],
      cli: (o, v = {}) => rowOr(o, t => `sd work cancel ${t.id} --reason ${v.reason ? window.shell.shq(v.reason) : "'<why>'"}`),
      sends: o => `POST /api/items/${T(o).id}/cancel {reason}`,
      consequence: () => 'Closes the work item as cancelled, with your reason. No sd verb takes it back.',
      run: (o, v) => { const t = must(o);
        return landing(write(t.key, x => `/api/items/${x.id}/cancel`, { reason: v.reason }), () => `${label(t)} cancelled · sd work cancel ${t.id} --reason ${window.shell.shq(v.reason)}`); } },
    // Recurrence: sd task edit --recur needs a due date; --clear-recur stops the series and clears its anchor, which Undo sets again.
    { id: 'item.recur', on: 'item', label: 'Edit → repeat weekly', risk: 'undo', icon: 'calendar-clock',
      when: o => { const t = T(o), d = detOf(t); if (!t) return 'the task is no longer listed'; if (!RECURS.includes(t.kind)) return `a ${t.kind} item cannot recur`;
        if (!EDITABLE(t)) return noEdit(t);
        if (!d) return 'recurrence was not read for this row';
        return d.item.recurrence ? `it repeats already: ${d.item.recurrence}` : t.due ? true : '--recur needs a due date'; },
      cli: o => rowOr(o, t => `sd task edit ${t.id} --recur FREQ=WEEKLY`),
      run: o => { const t = must(o); return editRun(t, { recurrence: 'FREQ=WEEKLY' }, () => `${label(t)} repeats weekly`,
        { check: r => !repeats(r) || `it repeats already: ${r.recurrence}` }); }, undo: undoOf },
    { id: 'item.recur.clear', on: 'item', label: 'Edit → stop repeating', risk: 'undo', icon: 'calendar-clock',
      when: o => { const t = T(o), d = detOf(t); return !d ? 'recurrence was not read for this row' : !EDITABLE(t) ? noEdit(t) : d.item.recurrence ? true : 'the task does not repeat'; },
      cli: o => rowOr(o, t => `sd task edit ${t.id} --clear-recur`),
      // The rule and anchor Undo sets again are the row's as the edit leaves, after any edit queued before it.
      run: o => { const t = must(o); return editRun(t, { recurrence: null }, () => `${label(t)} no longer repeats`,
        { check: r => repeats(r) || 'the task does not repeat', keep: ['recurrence', 'anchor'] }); },
      undo: undoOf },
    // A followup note closes with sd note resolve. sd note has resolve and list only, so nothing reopens it: confirm, no Undo.
    // build: POST /api/notes/<note>/resolve with the item's revision; the readback is the item's state.
    NOTE_RESOLVE(o => { const item = String(o.item);
      return landing(write(item, () => `/api/notes/${o.note}/resolve`, {}).then(() => redrawDet(o.item)), () => `Resolved note ${o.note} · sd note resolve ${o.note}`); }),
    // Assignments: Show assignment is the shared declaration (commands.js); Cancel is this page's alone.
    // build (sd:3041): cancel is sd assignments cancel, POST /api/assignments/<n>/cancel with the revision the Details read
    // (operations.assignment_state); the runner's cancel went with the runner. No verb reopens a cancelled row: confirm, no Undo.
    { id: 'asg.cancel', on: 'assignment', label: 'Cancel', key: 'x', risk: 'confirm',
      when: o => o.can.allowed || o.can.reason, cli: o => `sd assignments cancel ${o.n}`,
      consequence: o => `Ends assignment #${o.n} in ${o.repo} as cancelled. The item's status is unchanged.`,
      run: o => landing(cancelAsg(o), () => `Cancelled · #${o.n} · sd assignments cancel ${o.n}`) },
    window.commands.showAssignment({ primary: o => o.status !== 'blocked', run: o => `Assignment #${o.n} shown in Details` }),
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
  // build (sd:3440): a card's priority and due date are dropdowns, each the Details field's write (fieldChange). Due offers
  // the near dates; Pick a date opens Edit. A row edit_item refuses, or one not read again, keeps the plain labels.
  const tileF = t => EDITABLE(t) && live(t.key);
  function dueSelect(t, dt, dc) {
    const near = [[plus(0), 'Today'], [plus(1), 'Tomorrow'], [plus(7), 'In a week']].filter(([d]) => d !== t.due);
    return html`<select class="tile-f due ${dc}" data-edit="due" data-key="${t.key}" aria-label="Due date of ${label(t)}"><option value="${String(t.due || '')}" selected>${t.due ? dt : 'No due date'}</option>${
      near.map(([d, w]) => html`<option value="${d}">${w} · ${fmt(d)}</option>`)}${t.due && !repeats(t) ? html`<option value="">No due date</option>` : ''}<option value="pick">Pick a date…</option></select>`;
  }
  // sd:5103: the issue File issue filed and linked, from the item's ref (tasks_screen's issue_link), on its card and row.
  const issueLink = t => t.issueLink ? html`<a class="ext issue-link" href="${t.issueLink}" target="_blank" rel="noopener noreferrer" aria-label="Filed issue for ${label(t)}" title="${t.issueLink}">${ICON('link-2')}</a>` : '';
  function card(t, compact) {
    const st = state(t), [dt, dc] = dueText(t);
    return html`<article class="card" tabindex="0" data-key="${t.key}"${t.key === selected ? html` aria-current="true"` : ''}${checked.has(t.key) ? html` data-picked` : ''}${st ? html` data-state="${st}"` : ''} aria-label="${label(t) + ' ' + t.title}">
      <div class="top">
        <label class="pick"><input type="checkbox" data-check="${t.key}" aria-label="Select ${label(t)}"${checked.has(t.key) ? html` checked` : ''}><span></span></label>
        ${st ? html`<span class="g g-${st}" aria-hidden="true">${GLYPH[st]}</span>` : ''}
        <span class="id">${label(t)}</span>${issueLink(t)}
        ${tileF(t) ? html`<select class="tile-f pri" data-edit="p" data-key="${t.key}" data-p="${String(t.p || '')}" aria-label="Priority of ${label(t)}">${t.p ? '' : html`<option value="" selected>P–</option>`}${[1, 2, 3, 4].map(n => html`<option value="${n}"${t.p === n ? html` selected` : ''}>P${n}</option>`)}</select>`
          : html`<span class="pri" data-p="${String(t.p || '')}" title="Priority">${t.p ? 'P' + t.p : 'P–'}</span>`}
        ${t.assignment === 'running' ? html`<span class="lock" title="Runner assignment running">${ICON('lock')}</span>` : ''}
        ${C.rowActions(t.key)}
      </div>
      <div class="t">${t.title}</div>
      <div class="meta"><span class="repo">${t.repo}</span>${tileF(t) ? dueSelect(t, dt, dc) : html`<span class="due ${dc}">${dt}</span>`}${compact ? html`<span>${slabel(t.status).toLowerCase()}</span>` : ''}</div>
    </article>`;
  }
  // sd:5305: the board's Done column holds only what was done in the 7 days before the read; the list keeps every done item.
  // A done item with no known time stays: unknown is not old.
  const DONE_DAYS = 7, DONE_TIP = `done in the last ${DONE_DAYS} days`;
  const recent = t => t.status !== 'done' || !t.since || !READ || Date.parse(READ) - Date.parse(t.since) <= DONE_DAYS * 864e5;
  function renderBoard() {
    const v = visible().filter(recent);
    // build: backlog_items also returns statuses with no column (a message's ready_to_send); they get one lane, which
    // takes no drop and no key, so every row the page counts is on the board (review, PR #46).
    const other = v.filter(t => !SLABEL[t.status]);
    put(document.getElementById('view-board'), html`<div class="board" id="board" role="group" aria-label="Tasks by status"${other.length ? html` data-other` : ''}>${STATUSES.map(([s, name], i) => {
      const rows = v.filter(t => t.status === s);
      return html`<div class="col" data-drop="status:${s}" role="group" aria-label="${name}">
        <header><span class="label"${s === 'done' ? html` title="${DONE_TIP}"` : ''}>${name}</span><kbd>${i + 1}</kbd><span class="n">${rows.length}</span></header>
        <div><p class="refusal" role="note"></p><div class="drop">${(rows.length ? rows.map(t => card(t)) : html`<p class="empty">${on() ? 'None match the filters.' : 'Nothing ' + name.toLowerCase() + '.'}</p>`)}</div></div>
      </div>`;
    })}${other.length ? html`<div class="col" role="group" aria-label="Other statuses">
        <header><span class="label">Other</span><span class="n">${other.length}</span></header>
        <div><div class="drop">${other.map(t => card(t, true))}</div></div>
      </div>` : ''}</div>`);
  }
  function renderMatrix() {
    const v = visible().filter(t => t.status !== 'done');
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
    <p class="mnote">Done tasks stay off the matrix. A drop edits the task: into Important sets P2, out of it sets P3; crossing into or out of Urgent asks you for the due date, because urgency comes from it. Cancel changes nothing.</p>`);
  }
  // The list's sort, page and size (shell.list, sd:2682): priority first, 50 a page. Every column with an order sorts.
  const LIST = { sort: 'p', dir: 1, size: 50 }, L = { ...LIST, page: 1 };
  const ORDER = Object.fromEntries(STATUSES.map(([st], i) => [st, i]));
  const SORTS = { id: t => t.id, title: t => t.title.toLowerCase(), repo: t => t.repo.toLowerCase(), status: t => ORDER[t.status] ?? 99, p: t => t.p ?? 9,
    due: t => t.overdue && !t.due ? -99 : t.due ? days(t) : 999, stage: t => t.stage || '~',
    // A base row's score, highest first; a row with none sorts last (sd:3977).
    score: t => t.score === null || t.score === '' || isNaN(+t.score) ? Infinity : -t.score };
  // slim: a control or short cell (pick, glyph, P, actions) keeps the narrow gutter that lets the list fit its column (sd:4037).
  // id, rp and sc mark the columns that fold into a .fold line of their neighbour in a narrower column (tasks.css).
  const COLS = [[null, html`<span class="sr">Select</span>`, 'slim'], [null, html`<span class="sr">State</span>`, 'slim'], ['id', 'Id', 'label id'], ['title', 'Title', 'label'], ['repo', 'Repo', 'label rp'],
    ['status', 'Status', 'label'], ['stage', 'Stage', 'label'], ['score', 'Score', 'label sc'], ['p', 'P', 'label slim'], ['due', 'Due', 'label'], [null, html`<span class="sr">Actions</span>`, 'slim']];
  // Due's head says the word, so a cell drops it (the design source's shortDue).
  const shortDue = s => s === 'no due' ? 'none' : s.replace(/^due /, '');
  let seek = false;
  function renderList() {
    const list = window.shell.list, k = SORTS[L.sort];
    const all = visible().slice().sort((a, b) => { const x = k(a), y = k(b); return (x < y ? -1 : x > y ? 1 : 0) * L.dir; });
    // build (sd:2589): the list pages as v1 /backlog paged, 50 a page; board and matrix show every filtered task. A ?row= with
    // no ?page= opens the page that holds it.
    if (seek) { seek = false; const i = all.findIndex(t => t.key === selected); if (i >= 0) L.page = Math.floor(i / L.size) + 1; }
    const v = list.pageOf(all, L);
    // Stage and Score show once a row has a base (sd:3977), as Base does: two empty columns would push Due and Actions off.
    const based = tasks.some(t => t.base), cols = based ? COLS : COLS.filter(c => c[0] !== 'stage' && c[0] !== 'score');
    put(document.getElementById('view-list'), html`<div class="list-wrap"><table class="list" data-tfilter="page" data-toolbar="filterbar"><caption class="sr">Tasks</caption><thead>${list.sortHead(cols, L)}</thead><tbody>
      ${(v.length ? v.map(t => { const st = state(t), [dt, dc] = dueText(t); return html`<tr data-key="${t.key}"${t.key === selected ? html` aria-current="true"` : ''}${checked.has(t.key) ? html` data-picked` : ''}><td class="slim"><label class="pick"><input type="checkbox" data-check="${t.key}" aria-label="Select ${label(t)}"${checked.has(t.key) ? html` checked` : ''}><span></span></label></td><td class="g g-${st} slim">${st ? GLYPH[st] : ''}</td><td class="mono id">${label(t)}</td><td class="title"><button type="button" data-open="${t.key}">${t.title}</button>${issueLink(t)}<span class="fold mono">${label(t)} · ${t.repo}</span></td><td class="mono rp"><span title="${t.repo}">${t.repo}</span></td><td class="st">${slabel(t.status)}</td>${based ? html`<td>${t.stage || ''}${t.score != null && t.score !== '' ? html`<span class="fold mono">score ${t.score}</span>` : ''}</td><td class="mono sc">${t.score ?? ''}</td>` : ''}<td class="slim"><span class="pri" data-p="${String(t.p || '')}">${t.p ? 'P' + t.p : 'P–'}</span></td><td class="mono due ${dc}">${shortDue(dt)}</td><td class="slim">${C.rowActions(t.key)}</td></tr>`; }) : html`<tr><td colspan="${cols.length}" class="empty">None match the filters. <button class="linkbtn" type="button" id="clear-f2">Clear filters</button></td></tr>`)}
    </tbody></table></div>${list.pager(all.length, L, 'tasks')}`);
  }
  document.getElementById('view-list').addEventListener('click', e => {
    if (window.shell.list.sortBy(e, L)) { window.shell.list.keepFocus(e, renderList); writeURL(); return; }
    if (e.target.id === 'clear-f2') { clearFilters(); return; }
    if (window.shell.list.paging(e, L)) { window.shell.list.keepFocus(e, render); return; }
    const o = e.target.closest('[data-open]'); if (o) select(o.dataset.open, true);
  });

  let detKey = null; // the task whose Details are on screen, so a redraw of another one carries nothing over
  function renderDetails() {
    const t = byKey(selected), el = document.getElementById('details');
    if (!t) { detKey = null; put(el, html`<p class="note">${!READ ? 'The tasks were not read, so nothing is selected.'
      : tasks.some(x => !live(x.key)) ? 'The tasks were not read again after the change, so nothing is selected. Reload reads them.'
      : !tasks.length ? 'No open task, so nothing is selected.' : !visible().length ? 'No task matches these filters. Clear them to see the list.'
      : 'Select a task to see its fields and moves.'}</p>`); return; }
    const [dt] = dueText(t), act = C.bar(t.key), gone = !live(t.key);
    // build (sd:3439): status, priority, due, repo and quadrant are fields; a change writes at once (fieldChange below).
    const off = gone ? 'this task was not read again after the change' : EDITABLE(t) ? '' : noEdit(t);
    const dis = off ? html` disabled aria-describedby="det-off"` : '';
    // A redraw (a Details read, a refresh, another field's write) keeps the field the operator is in, as it stands: when its
    // text differs from the saved value, or a date is half typed, the live field moves into the new Details with its caret.
    // Review 2026-10-12 (high): the title and description were redrawn from saved data mid-edit.
    const a = document.activeElement, focused = a?.id, mine = detKey === t.key && focused?.startsWith('det-') && a.isConnected && a.closest?.('#details');
    const caret = mine ? (() => { try { return [a.selectionStart, a.selectionEnd]; } catch { return [null, null]; } })() : null;
    detKey = t.key;
    put(el, html`<div class="kind"><span class="label">${t.kind}</span><span class="tag real">observed</span></div>
      <h2>${t.title}</h2>
      <dl class="fields">
        <dt>Id</dt><dd>#${t.id}</dd>
        <dt><label for="det-title">Title</label></dt><dd><input id="det-title" class="fld" value="${t.title}" required${dis}></dd>
        <dt><label for="det-status">Status</label></dt><dd><select id="det-status" class="fld"${gone ? html` disabled aria-describedby="det-off"` : ''}>${STATUSES.map(([s, name]) => { const L = legal(t, s);
          return html`<option value="${s}"${s === t.status ? html` selected` : L.ok ? '' : html` disabled title="${L.reason}"`}>${name}</option>`; })}${SLABEL[t.status] ? '' : html`<option value="${t.status}" selected disabled>${slabel(t.status)}</option>`}</select></dd>
        <dt><label for="det-p">Priority</label></dt><dd><select id="det-p" class="fld"${dis}>${t.p ? '' : html`<option value="" selected>not recorded</option>`}${[1, 2, 3, 4].map(n => html`<option value="${n}"${t.p === n ? html` selected` : ''}>P${n}</option>`)}</select></dd>
        <dt><label for="det-due">Due</label></dt><dd><input id="det-due" class="fld" type="date" value="${String(t.due || '')}"${dis}>${t.due || t.overdue ? html` <span class="why">${t.due ? dt : 'overdue · date not recorded'}</span>` : ''}</dd>
        <dt><label for="det-repo">Repo</label></dt><dd><select id="det-repo" class="fld"${t.kind === 'work' && !off ? html` disabled title="A work item belongs to the checkout its folder is in."` : dis}>${repoOptions(t, null)}</select></dd>
        <dt>Urgent</dt><dd>${urgent(t) ? 'yes' : 'no'} <span class="why">(derived)</span></dd>
        <dt><label for="det-q">Quadrant</label></dt><dd>${t.status === 'done' ? 'not placed' : html`<select id="det-q" class="fld"${dis}>${QUADS.map(q => html`<option value="${q[0]}"${q[0] === quadOf(t) ? html` selected` : ''}>${q[1]}</option>`)}</select>`}</dd>
        ${t.assignment ? html`<dt>Assignment</dt><dd>${t.assignment}</dd>` : ''}
      </dl>
      ${off ? html`<p class="why" id="det-off">The fields are off: ${off}.</p>` : ''}
      <h3>Act</h3>${gone ? html`<p class="why">This task was not read again after the change, so no command runs on it. Reload reads it.</p>` : act}
      ${window.commands.reviewPane('task', t.id)}
      ${issuePane(t)}
      ${more(t, dis)}
      <p class="foot">${READ ? html`Rows read <time class="rel" datetime="${READ}"></time> from /api/tasks (the reads v1 /backlog makes).` : ''}</p>`);
    if (mine) {
      const fresh = document.getElementById(focused), keep = fresh && !fresh.disabled && (a.value !== fresh.value || a.validity?.badInput);
      const f = keep ? a : fresh;
      if (keep) fresh.replaceWith(a);
      f?.focus?.();
      if (f && caret[0] != null) try { f.setSelectionRange(caret[0], caret[1]); } catch { /* a date or select has no caret */ }
    } else if (focused?.startsWith('det-')) document.getElementById(focused)?.focus?.();
    if (!off) window.shell.repos?.().then(list => { const r = document.getElementById('det-repo'); if (r && selected === t.key && !r.disabled) put(r, repoOptions(t, list)); }, () => {});
  }
  // The repos a task can move to: every registered one (the capture's list) once read, else the ones these rows name.
  function repoOptions(t, list) {
    const all = list || [...new Map(tasks.filter(x => x.repo_path).map(x => [x.repo_path, { path: x.repo_path, name: x.repo }])).values()];
    if (t.repo_path && !all.some(r => r.path === t.repo_path)) all.push({ path: t.repo_path, name: t.repo });
    return html`<option value=""${t.repo_path ? '' : html` selected`}>No repo</option>${all.map(r => html`<option value="${r.path}"${r.path === t.repo_path ? html` selected` : ''}>${r.name}</option>`)}`;
  }
  // build (sd:3439): a Details field's change is one write through the command or edit it stands for, with its toast and
  // Undo. Status runs the status command, Quadrant the quadrant move (it asks for a date when it crosses the urgency line),
  // and the rest edit the item. `check` refuses a change a write queued before it already made. A refused change redraws,
  // so the field shows the row as it is.
  const STALE_BODY = 'the description changed since it was read. Your text was not saved; the Details are read again.';
  function fieldChange(t, id, v, base) {
    if (id === 'det-title') { const n = v.trim(); if (n === t.title) return false;
      if (!n) { toast(`${label(t)} not changed: a title is required`); return false; }
      commitEdit(t, { title: n }, `title → ${n}`, { check: r => r.title !== n || 'the title is already that' }); return true; }
    // The description is not in the row, so a write over it is refused unless the text it replaces is the row's (read at the
    // row's revision) and is the text the field was drawn from (`base`): another tool's edit is never written over.
    if (id === 'det-body') { const known = () => DET.body[t.id], text = () => known()?.text ?? '';
      if (v === text() && (base === undefined || base === text())) return false;
      if (known()?.rev !== t.revision || (base !== undefined && base !== text())) { toast(`${label(t)} not changed: ${STALE_BODY}`); redrawDet(t.id); return true; }
      commitEdit(t, { body: v }, 'description changed', { before: () => ({ body: text() }),
        check: r => known()?.rev !== r.revision ? STALE_BODY : v !== text() || 'the description is already that' }); return true; }
    if (id === 'det-status') return v !== t.status && move(t.key, v);
    // build (sd:3977): a base row's stage moves through records.edit_row; its render can fail after the move lands.
    if (id === 'det-stage') { if (v === t.stage) return false;
      write(t.key, x => `/api/items/${x.id}/stage`, { stage: v }).then(out => toast(`${label(t)} stage → ${v}${out.mirror && !out.mirror.ok
        ? ` · mirror not updated: ${out.mirror.reason || 'see Health'}` : ''}`), err => { failed(label(t), err); err.status === 409 ? refused() : redrawDet(t.id); });
      return true; }
    if (id === 'det-exp' || id === 'det-exp-row') { const x = detOf(t)?.experiment, r = v.trim();
      const spec = id === 'det-exp' ? (v && v !== x?.value ? v : null) : r === (x?.row || '') ? null : r ? `row:${r}` : 'new';
      if (!spec) return false;
      write(t.key, y => `/api/items/${y.id}/experiment`, { experiment: [spec] }).then(() => toast(`${label(t)} experiments → ${spec} · sd task edit ${t.id} --experiment ${window.shell.shq(spec)}`),
        err => { failed(label(t), err); err.status === 409 ? refused() : redrawDet(t.id); });
      return true; }
    if (id === 'det-q') return v !== quadOf(t) && toQuad(t.key, v);
    if (id === 'det-p') { const n = v ? +v : null; if (n === t.p) return false;
      commitEdit(t, { p: n }, `priority ${t.p ? 'P' + t.p : 'unset'} → ${n ? 'P' + n : 'unset'}`, { check: r => r.p !== n || `it is already ${n ? 'P' + n : 'unset'}` }); return true; }
    if (id === 'det-due') { const d = v || null; if (d === t.due) return false;
      if (!d && repeats(t)) { toast(NO_CLEAR); return false; }
      commitEdit(t, { due: d }, d ? `due ${fmt(d)}` : 'due removed', { check: r => r.due !== d || 'the date is already set' }); return true; }
    if (id === 'det-repo') { const r = v || null; if (r === (t.repo_path || null)) return false;
      commitEdit(t, { repo: r }, `repo → ${r ? r.split('/').pop() : 'none'}`, { keep: ['repo_path'], check: x => (x.repo_path || null) !== r || 'it is already there' }); return true; }
    return false;
  }
  document.getElementById('details').addEventListener('change', e => {
    const t = byKey(selected), id = e.target.id || '';
    if (!t || !id.startsWith('det-')) return;
    if (!fieldChange(t, id, e.target.value, e.target.defaultValue)) renderDetails();
  });
  // The v1 item page's sections (review item 17), each with the read it comes from. A row without a reading says so once.
  function more(t, dis = '') {
    if (!t.id) return '';
    const d = detOf(t), body = DET.body[t.id] && DET.body[t.id].text;
    // build (sd:3467): the description edits in place, as the classic item page's Details textarea did; it saves on leaving.
    // edit_item takes a body for a task or a followup only.
    const desc = body === undefined || !['task', 'followup'].includes(t.kind) ? ''
      : body === null ? html`<h3>Description</h3><p class="why">The stored description is not valid JSON, so the field is withheld and no save here can overwrite it. Repair it with <code>sd task edit ${t.id} --body</code>.</p>`
      : html`<h3><label for="det-body">Description</label></h3><textarea id="det-body" class="fld" rows="4"${dis}>${body}</textarea>`;
    if (!d) return html`${desc}<h3>History, notes and assignments</h3><p class="why">${DET.failed[t.id] ? `Not read: ${DET.failed[t.id]}` : `Reading sd task show ${t.id} --json…`}</p>`;
    const it = d.item;
    const open = d.notes.filter(n => n.kind === 'followup' && !n.resolved).length;
    const docs = d.documents || [];
    return html`${desc}${baseOf(t, d)}
      <h3>Documents ${src('Documents', `/api/tasks/${t.id} · the item's path and work folder, docs/work files naming sd:${t.id}, paths its notes name`)}</h3>
      ${docs.length ? html`<ol class="rec">${docs.map(x => html`<li><span>${x.url ? html`<a class="ext" href="${x.url}" target="_blank" rel="noopener noreferrer">${x.path}</a>` : html`<code>${x.path}</code>`} <small>${x.why}</small></span></li>`)}</ol>` : html`<p class="why">No design or document names this item.</p>`}
      ${published(t, d.published || [])}
      ${experiment(t, d.experiment)}
      <h3>Status history ${src('Status history', `sd task show ${t.id} --json · notes of kind status_change`)}</h3>
      ${d.history.length ? html`<ol class="rec">${d.history.map(n => html`<li><time class="rel" datetime="${n.at}"></time><span>${n.body}</span></li>`)}</ol>` : html`<p class="why">This item has never changed status.</p>`}
      <h3>Notes <span class="n">${plural(d.notes.length, 'note')} · ${open} open ${plural.word(open, 'followup')}</span> ${src('Notes', `sd task show ${t.id} --json · every other note kind`)}</h3>
      ${d.notes.length ? html`<ol class="rec">${d.notes.map(n => html`<li data-note="${String(n.id)}"><time class="rel" datetime="${n.at}"></time><span><b>${n.kind}</b>${n.kind === 'followup' ? (n.resolved ? ' · resolved' : ' · open') : ''} ${n.body}</span>${C.rowActions('note:' + n.id)}</li>`)}</ol>` : html`<p class="why">No notes on this item.</p>`}
      <h3>Assignments ${src('Assignments', `sd assignments list --json · item ${t.id}; not in sd task show`)}</h3>
      ${d.assignments.length ? html`<ol class="rec">${d.assignments.map(a => html`<li><span class="mono">#${String(a.id)}</span><span>${a.role} · ${a.provider || 'no provider'} · <b>${a.status}</b>${a.ended ? html` · ended <time class="rel" datetime="${a.ended.replace('+00:00', 'Z')}"></time>` : ''}${a.cancel.allowed ? '' : html`<br><small>sd assignments cancel: ${a.cancel.reason}</small>`}</span>${C.rowActions('asg:' + a.id)}</li>`)}</ol>` : html`<p class="why">No assignment has run on this item.</p>`}
      <h3>Artifact ${src('Artifact', `sd task show ${t.id} --json · item.path`)}</h3>
      <dl><dt>Path</dt><dd>${it.path || 'none'}</dd><dt>Repo</dt><dd>${it.repo || 'none'}</dd>${it.branch ? html`<dt>Branch</dt><dd>${it.branch}</dd>` : ''}</dl>
      <h3>Recurrence ${src('Recurrence', `sd task show ${t.id} --json · item.recurrence`)}</h3>
      <dl><dt>Repeats</dt><dd>${it.recurrence || 'no'}</dd>${it.recurrence_anchor ? html`<dt>Anchor</dt><dd>${it.recurrence_anchor}</dd>` : ''}<dt>Revision</dt><dd>${d.revision.slice(0, 12)}…</dd></dl>
      ${external(d)}`;
  }
  // sd:3465: where the item's work was published, each URL under its document's title; tasks_screen._published sets
  // `href` only for an address the page may open, so any other shows as text. Nothing published shows no section.
  function published(t, list) {
    if (!list.length) return '';
    return html`<h3>Published ${src('Published', `/api/tasks/${t.id} · fields.published, fields.writing.published_urls, publication claims`)}</h3>
      <ol class="rec">${list.map(p => html`<li><span><b>${p.title}</b> <small>${p.where}</small>${p.links.map(l => html`<br>${l.channel} ${l.href ? html`<a class="ext" href="${l.href}" target="_blank" rel="noopener noreferrer">${l.url}</a>` : html`<code>${l.url}</code>`}`)}</span></li>`)}</ol>`;
  }
  // sd:4639: the item's row in the team experiments database (pack sd:3982). The select and the row field post what
  // `sd task edit N --experiment` takes (fieldChange); clearing a pinned row is `new`, so the next drain creates one.
  // `off` keeps the row and is offered once the item is designated; a designation the page cannot read turns both off.
  function experiment(t, x) {
    if (!x) return '';
    const off = x.reason ? html` disabled aria-describedby="det-exp-off"` : live(t.key) ? '' : html` disabled`;
    const opt = (value, name) => html`<option value="${value}"${value === x.value ? html` selected` : ''}>${name}</option>`;
    const lost = x.value && x.value.startsWith('of:') && !x.choices.some(c => c.value === x.value);
    return html`<h3><label for="det-exp">Experiments</label> ${src('Experiments', `sd task edit ${t.id} --experiment · fields.experiments; the row's link shows under Published once a drain records it`)}</h3>
      <select id="det-exp" class="fld"${off}>${x.value ? '' : html`<option value="" selected>not projected</option>`}${opt('yes', 'experiment')}${x.choices.map(c => opt(c.value, c.label))}${lost ? opt(x.value, `task of #${x.value.slice(3)}`) : ''}${x.value ? opt('off', 'off, keep the row') : ''}</select>
      ${x.value ? html`<dl class="fields"><dt><label for="det-exp-row">Row</label></dt><dd><input id="det-exp-row" class="fld" value="${x.row || ''}" placeholder="a page id or URL"${off}></dd></dl>` : ''}
      ${x.reason ? html`<p class="why" id="det-exp-off">${x.reason}.</p>` : ''}`;
  }
  // build (sd:3977): a vault base row's stage, declared fields, body and its section of the vault mirror page.
  // The stage select is the row's own write: edit_item's refusal of an idea's other fields does not turn it off.
  function baseOf(t, d) {
    const b = d.base; if (!b) return '';
    const off = b.reason || (!live(t.key) && 'this task was not read again after the change') || (!b.stages.length && 'no move leaves this stage');
    return html`<h3>Base · ${b.kind} ${src('Base', `sd-db records render ${b.kind} · the kind's rules from sd plugin list --json`)}</h3>
      <dl class="fields"><dt><label for="det-stage">Stage</label></dt><dd><select id="det-stage" class="fld"${off ? html` disabled aria-describedby="det-base-off"` : ''}><option value="${b.stage}" selected>${b.stage}</option>${b.stages.map(s => html`<option value="${s}">${s}</option>`)}</select></dd>
        ${b.declared.map(f => html`<dt>${f}</dt><dd>${Array.isArray(b.fields[f]) ? b.fields[f].join(', ') : b.fields[f] ?? '—'}</dd>`)}
        <dt>Mirror</dt><dd>${!b.mirror ? 'no vault named' : b.mirror.url ? html`<a href="${b.mirror.url}" target="_blank" rel="noopener">its section</a>` : html`<code>${b.mirror.path}</code>`}</dd></dl>
      ${off ? html`<p class="why" id="det-base-off">${off}.</p>` : ''}
      ${b.body ? html`<pre class="body">${b.body}</pre>` : ''}`;
  }
  // Each Details section names its read in a help icon beside its heading; pressing the icon copies the command (sd:3444).
  function src(name, s) {
    const at = s.indexOf(' · '), cmd = at < 0 ? s : s.slice(0, at), why = at < 0 ? '' : s.slice(at + 3);
    return html`<button class="help src" type="button" aria-label="Help: ${name}" data-help="${`<code>${cmd}</code>${why ? ` · ${why}` : ''}. Press to copy.`}" data-copy="${cmd}">${ICON('circle-help')}</button>`;
  }
  // build: v1's External context (screens.external_context), the shadow row and its collector's freshness, when the item
  // has a tracker reference. Local status is the item's own; the tracker's state is dated context beside it.
  function external(d) {
    const x = d.external; if (!x) return '';
    return html`<h3>External context ${src('External context', `sd shadow · ${x.tracker} · progress.tracker_freshness`)}</h3>
      <dl><dt>Tracker</dt><dd>${x.tracker}</dd><dt>External state</dt><dd>${x.state || (x.last_seen ? 'unknown' : 'not in the local snapshot')}</dd>
        <dt>Reference</dt><dd>${x.url}</dd><dt>Last seen</dt><dd>${x.last_seen ? html`<time class="rel" datetime="${x.last_seen}"></time>` : 'never'}</dd>
        <dt>Sync health</dt><dd>${x.freshness.state}</dd><dt>Last sync</dt><dd>${x.freshness.last_success_at ? html`<time class="rel" datetime="${x.freshness.last_success_at}"></time>` : 'never'}</dd></dl>
      ${x.freshness.reason ? html`<p class="why">${x.freshness.reason}</p>` : ''}`;
  }
  function render() {
    putAll();
    renderFilters();
    writeURL();
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
    if (e.target.closest('.rowact, .pick, .tile-f')) return;
    const c = e.target.closest('.card'); if (c && !suppressClick) select(c.dataset.key, true);
  });
  // Selection follows focus on a card, so Enter, which the shell sends as shell:open, opens the focused one.
  document.getElementById('main').addEventListener('focusin', e => { const c = e.target.closest('.card'); if (c && e.target === c && c.dataset.key !== selected) select(c.dataset.key, false); });
  document.getElementById('main').addEventListener('change', e => {
    const { edit: f, key } = e.target.dataset, t = f && byKey(key);
    if (t) { if (f === 'due' && e.target.value === 'pick') { render(); askEdit(t); return; }
      if (!fieldChange(t, `det-${f}`, e.target.value)) render(); return; }
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
  function commitEdit(t, ch, words, opts) {
    // A page dialog, not a shell command: it toasts on its own, with the same Undo a command gets.
    editRun(t, ch, () => `${label(t)} ${words}${' · ' + cliQuad(t, ch)}`, opts).then(r => toast(r.text, () => r.undo().then(
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
        ${L.ok ? html`<code>${cliMove(t, s)}</code>` : html`<p class="why" id="mv-${s}">${L.reason}</p>`}</div>`; })}</div>
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
  // Start next (sd:5107): the server's plan first (start_next.plan), then Start posts the first N it listed, each with the
  // revision it read; the server plans again and starts nothing when the plan moved. A refusal leaves no Start button.
  const startDlg = document.createElement('dialog');
  startDlg.className = 'palette date-dlg start-dlg'; startDlg.setAttribute('aria-labelledby', 'start-h');
  document.body.append(startDlg);
  const unread = v => v === null || v === undefined ? '▨' : v;
  const startedText = out => [
    out.started.length ? `Started ${out.started.map(s => `#${s.id}`).join(', ')} · builder sessions in herdr` : 'Nothing started',
    out.failed ? `#${out.failed.id} did not start: ${out.failed.error}` : '',
    out.not_started.length ? `not tried: ${out.not_started.map(n => `#${n}`).join(', ')}` : ''].filter(Boolean).join(' · ');
  async function askStart() {
    let P;
    try { P = await window.shell.getJSON('/api/tasks/start-next'); } catch (err) { toast(`Start next not read: ${err.message}`); return; }
    put(startDlg, html`<form method="dialog" class="date-form">
      <h2 id="start-h">Start next ready tasks</h2>
      <p class="why">${unread(P.running)} of ${P.cap} builders run · five-minute load ${unread(P.load)} of ${unread(P.load_max)}</p>
      ${P.refusal ? html`<p class="why">▲ Nothing can start: ${P.refusal}.</p>
        ${P.holders?.length ? html`<p><a class="btn quiet" href="/management?view=sessions">Open Management › Sessions</a></p>` : ''}` : html`
        <label class="label" for="start-n">Builders to start (1–${P.room})</label>
        <input id="start-n" type="number" min="1" max="${P.room}" value="${P.room}" required>
        <ol class="start-list">${P.start.map((r, i) => html`<li data-i="${i}"><code>#${r.id}</code> ${r.title}</li>`)}</ol>`}
      ${P.skipped.length ? html`<details><summary>${plural(P.skipped.length, 'ready task')} left out</summary>
        <ul class="start-list">${P.skipped.map(r => html`<li><code>#${r.id}</code> ${r.title}: ${r.why}</li>`)}</ul></details>` : ''}
      <div class="actions">
        ${P.refusal ? '' : html`<button class="btn" value="start" type="submit">Start</button>`}
        <button class="btn quiet" value="cancel" type="submit" formnovalidate>Cancel</button>
      </div></form>`);
    const n = () => Math.max(1, Math.min(P.room, parseInt(startDlg.querySelector('#start-n')?.value, 10) || 0));
    // The list marks what Start would start now, so the count and the items never disagree.
    const mark = () => startDlg.querySelectorAll('.start-list li[data-i]').forEach(li => li.classList.toggle('off', +li.dataset.i >= n()));
    startDlg.querySelector('form')?.addEventListener('input', mark);
    startDlg.onclose = () => {
      if (startDlg.returnValue !== 'start' || P.refusal) return;
      const items = P.start.slice(0, n()).map(r => ({ id: r.id, revision: r.revision }));
      window.shell.post('/api/tasks/start-next/run', { items }).then(out => { toast(startedText(out)); reread(); }, err => {
        // No status: no answer, so whether a builder started is not known; the next plan says, so it asks for that.
        toast(err.status ? err.message : `Start next got no answer, so what started is not known: open Start next again (${err.message})`);
        reread();
      });
    };
    startDlg.returnValue = '';
    startDlg.showModal(); (startDlg.querySelector('#start-n') || startDlg.querySelector('[value="cancel"]'))?.focus();
  }
  document.addEventListener('tasks:start-next', askStart);
  document.getElementById('start-next')?.addEventListener('click', askStart);
  // ---------- Pointer drag (mouse and pen; touch uses Move to) ----------
  let drag = null, suppressClick = false;
  const verdict = (t, target) => {
    const [kind, v] = target.split(':');
    if (kind === 'status') { const L = legal(t, v); return { ok: L.ok, text: L.ok ? `→ ${slabel(v)}` : L.reason, same: t.status === v }; }
    const Q = quadChange(t, v); return { ok: Q.ok, text: Q.ok ? Q.words.join(', ') : Q.reason, same: quadOf(t) === v };
  };
  document.addEventListener('pointerdown', e => {
    const c = e.target.closest('.card');
    if (!c || e.button !== 0 || e.pointerType === 'touch' || e.target.closest('button, input, select')) return;
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
    const parked = tasks.length - listed().length;
    // A realm view without a readable realm list (dashboard.realms) holds only the rows with no repo; it says why (sd:5064).
    const why = REALMS && !['all', 'hoa', 'home'].includes(PLACE) && !['read', 'partial'].includes(REALMS.state)
      ? ` · repos in no realm: ${REALMS.state === 'missing' ? 'no realm list; set dashboard.realms on Settings' : `${REALMS.source} ${REALMS.problems[0] || 'was not read'}`}` : '';
    // The Personal and Work views name their sweep's last run and carry Run sweep (sd:5096, sd:5097).
    const w = SWEEP_NAME[PLACE] && SWEEPS?.[PLACE];
    put(document.getElementById('subhead'), html`${plural(listed().length, 'task')}${parked ? ` · ${parked} parked` : ''} · read ${READ ? html`<time class="rel" datetime="${READ}"></time>` : 'not yet'} · ${SUB[view]}${why}${w ? html` · <span class="sweep" data-sweep="${PLACE}">${SWEEP_NAME[PLACE]} ${sweepText(w)} ${C.rowActions('sweep:' + PLACE)}</span>` : ''}`);
  }
  // The view menu (sd:5064): one button shows the current view's icon; its menu lists the three, one checked.
  const viewBtn = document.getElementById('view-btn'), viewMenu = document.getElementById('view-menu');
  const VIEW_ICON = { list: 'list-todo', board: 'kanban', matrix: 'grid-2x2' }, VIEW_NAME = { list: 'List', board: 'Board', matrix: 'Matrix' };
  const viewItems = () => [...viewMenu.querySelectorAll('[role="menuitemradio"]')];
  function viewMenuOpen(open) {
    viewMenu.hidden = !open; viewBtn.setAttribute('aria-expanded', String(open));
    if (open) (viewItems().find(b => b.getAttribute('aria-checked') === 'true') || viewItems()[0]).focus();
  }
  viewBtn.addEventListener('click', () => viewMenuOpen(viewMenu.hidden));
  viewMenu.addEventListener('keydown', e => {
    const items = viewItems(), i = items.indexOf(document.activeElement), last = items.length - 1;
    const to = { ArrowDown: i >= last ? 0 : i + 1, ArrowUp: i < 1 ? last : i - 1, Home: 0, End: last }[e.key];
    if (to !== undefined) { e.preventDefault(); items[to].focus(); return; }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); viewMenuOpen(false); viewBtn.focus(); return; }
    if (e.key === 'Tab') { viewMenuOpen(false); return; }
    e.stopPropagation(); // a letter here is not the shell's (j, k, g); Enter and Space press the item
  });
  viewMenu.addEventListener('click', e => { const b = e.target.closest('[data-view]'); if (!b) return; setView(b.dataset.view); viewMenuOpen(false); viewBtn.focus(); });
  document.addEventListener('click', e => { if (!viewMenu.hidden && !e.target.closest?.('.viewpick')) viewMenuOpen(false); });
  function setView(v) {
    view = v;
    viewItems().forEach(b => b.setAttribute('aria-checked', String(b.dataset.view === v)));
    viewBtn.setAttribute('aria-label', `View: ${VIEW_NAME[v]}`);
    put(viewBtn, html`<svg class="i" aria-hidden="true"><use href="#i-${VIEW_ICON[v]}"/></svg>`);
    ['board', 'matrix', 'list'].forEach(x => { const el = document.getElementById('view-' + x); el.hidden = x !== v; if (x !== v) el.replaceChildren(); });
    subhead();
    render();
  }
  function readFilterURL() {
    const q = new URLSearchParams(location.search);
    FKEYS.forEach(key => (q.get(key) || '').split(',').filter(Boolean).forEach(v => F[key].add(v)));
    // An age v1 would not accept is dropped, as v1 dropped it; active takes 1 only, as v1's ?active=1 did.
    [...F.age].forEach(v => { if (!AGES.some(a => a.key === v)) F.age.delete(v); });
    [...F.active].forEach(v => { if (v !== '1') F.active.delete(v); });
    [...F.parked].forEach(v => { if (v !== '1') F.parked.delete(v); });
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
  // build (sd:3521): a capture with no #repo lands in the one repo the filter shows, else the selected task's; P3 unless typed.
  const filterRepo = () => F.repo.size === 1 ? repoFor([...F.repo][0].toLowerCase()) : null;
  window.PAGE_REPO = o => filterRepo() || (o && o.item ? byKey(String(o.item))?.repo_path : null) || null;
  function showPreview() {
    const txt = inp.value, P = parse(txt);
    ghost.hidden = !!txt;
    if (!txt.trim()) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); return; }
    const found = P.find ? tasks.filter(t => String(t.id) === P.find) : P.title.length > 2 ? tasks.filter(t => t.title.toLowerCase().includes(P.title.toLowerCase())).slice(0, 3) : [];
    const fr = !P.repo && filterRepo() && [...F.repo][0];
    const chips = [P.p ? `P${P.p}` : 'P3 (default)', P.due && `due ${fmt(P.due)}`, P.repo && `#${P.repo}${repoFor(P.repo) ? '' : ' (not shown here)'}`, fr && `#${fr} (filter)`].filter(Boolean);
    put(prev, html`${(P.find ? '' : html`<div class="as"><span class="label">Enter adds</span>${ICON('plus')}<span>${P.title || '(title needed)'}</span></div>
      <div class="chips">${chips.map(c => html`<span class="chip" aria-pressed="true">${c}</span>`)}<span class="chip">planning</span></div>`) }${
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
    const where = P.repo ? repoFor(P.repo) : filterRepo();
    const body = { title: P.title, priority: P.p || 3, ...(P.due ? { due: P.due } : {}), ...(where ? { repo: where } : {}) };
    const typed = inp.value; inp.value = ''; showPreview();
    // The new task is selected only where it shows: a filter that hides it keeps the selection on a visible one (review, PR #46).
    window.shell.post('/api/items', body).then(out => reread().then(() => { const key = String(out.item.id), t = byKey(key);
      if (!t || !passes(t)) { toast(`Added #${out.item.id} to Planning · the filters hide it; Clear all shows it`); return; }
      select(key, false); landed(key); toast(`Added #${out.item.id} to Planning`); }),
      // A refusal gives the text back as typed, unless the operator has typed the next one meanwhile.
      err => { if (!inp.value) inp.value = typed; toast(`Not added: ${err.message}`); });
  });
  prev.addEventListener('click', e => { const b = e.target.closest('[data-open-find]'); if (b) { inp.value = ''; showPreview(); select(b.dataset.openFind, true); } });
  document.addEventListener('click', e => { if (!e.target.closest('#shift')) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); } });

  // build: capture (n) files through the routes v1 uses: POST /api/items for a task or followup, POST /api/items/<id>/notes for
  // a note on the selected task, with that task's revision.
  window.SHELL_CAPTURE = async ({ kind, title, item, priority, repo }) => {
    if (kind === 'note') {
      const t = byKey(String(item)); if (!t) throw new Error('Not filed: pick a task for the note.');
      await write(t.key, x => `/api/items/${x.id}/notes`, { body: title, kind: 'comment' }); redrawDet(t.id);
      return `Note added to ${label(t)}`;
    }
    const said = await window.shell.fileCapture({ kind, title, item, priority, repo });
    await reread();
    return said;
  };

  // ---------- Start (build, sd:2484: shell.read reads /api/tasks, then draws; it rereads after each landed write) ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws; a task the read no longer
  // lists loses its pick and runs no command; a failed load leaves no row; and a failed reread after a write keeps the rows
  // and says the write landed. The Details keep their own per-item generation (DET.gen).
  let drawn = false, ISSUES = null;
  function draw() {
    // Your issues (sd:4628) are this document's too; issues.js draws them, or says the read failed.
    document.dispatchEvent(new CustomEvent('issues:read', { detail: ISSUES || { error: 'the tasks could not be read' } }));
    if (!drawn) {
      // The address is read before the first draw: that draw's reconcile selects the first shown card and rewrites ?row=.
      drawn = true;
      const q = new URLSearchParams(location.search), row = shell.row();
      if (row && byKey(row)) selected = row;
      // sd:3526: a linked item the read does not list, a quiet report or one done before the week, opens on its item page
      // rather than the first card in its place. READ is set only by a read that answered.
      else if (row && READ) { location.replace(shell.itemPage(row)); return; }
      readFilterURL();
      setView(['list', 'board', 'matrix'].includes(q.get('view')) ? q.get('view') : 'board');
    } else { subhead(); render(); }
    attention();
  }
  const reading = shell.read({
    source: '/api/tasks', what: 'the tasks',
    adopt: doc => { tasks = doc.rows.map(shape); READ = doc.read; AGES = doc.ages || []; ATTENTION = doc.attention || null; REALMS = doc.realms || null; SWEEPS = doc.sweeps || null;
      // While a sweep runs, Tasks reads again every SWEEP_POLL ms, so its words follow the run to its end.
      if (!sweepTimer && Object.values(SWEEPS || {}).some(w => w.running)) sweepTimer = setTimeout(() => { sweepTimer = 0; reread(); }, SWEEP_POLL);
      ISSUES = { issues: doc.issues || [], error: doc.issues_error || '' };
      // A row read at another revision than its cached Details changed elsewhere: they are read again (review 2026-10-12).
      tasks.forEach(t => { const d = DET.items[t.id]; if (d && d.revision !== t.revision) { staleDet(t.id); if (selected === t.key) readDet(t.id); } });
      const sweeps = Object.keys(SWEEP_NAME).filter(r => SWEEPS?.[r]).map(r => ({ id: `sweep:${r}`, type: 'sweep', label: `${SWEEP_NAME[r]} sweep` }));
      return { objects: [...tasks.map(objectOf), ...sweeps], state: tasks.length ? null : { kind: 'empty', text: 'No open task, and none done this week.', source: '/api/tasks' } }; },
    clear: () => { tasks = []; READ = null; ATTENTION = null; ISSUES = null; SWEEPS = null; },
    draw,
    // draw() has already reconciled the selection against the cards the filters show, so the reader settles on that one.
    current: () => selected,
    first: () => selected,
    select: key => select(key, false),
    unselect: () => { selected = null; renderDetails(); },
  });
  function load() { return reading.load(); }
  // ---------- Run sweep (sd:5096, sd:5097) ----------
  // POST /api/tasks/sweep {realm} starts that realm's sweep in the background (sd_dashboard/sweep.py); a 409 names the run going. It asks
  // first: the run files a brief and followups and saves drafts, and takes up to an hour.
  C.register({ id: 'realm-sweep.run', on: 'sweep', label: 'Run sweep', does: 'Runs this realm\'s sweep headless: a brief, followups and drafts; sends nothing.',
    risk: 'safe', askFirst: true, executes: true, primary: () => true,
    when: o => { const w = sweepOf(o); return !w ? 'the sweep was not read' : w.error || (w.running ? 'a sweep is running now' : true); },
    consequence: o => `Reads the ${o.id === 'sweep:work' ? 'work mail, Drive, Notion, Jira and GitHub' : 'personal mail and My Drive'} since the last run, files a dated brief and sd followups, and saves Gmail drafts. Nothing is sent or committed. Up to 60 min.`,
    cli: o => sweepOf(o)?.command || `python3 -m sd_dashboard.realm_sweep run ${String(o.id).slice(6)}`,
    sends: o => `POST /api/tasks/sweep {"realm": "${String(o.id).slice(6)}"}`,
    run: o => window.shell.post('/api/tasks/sweep', { realm: String(o.id).slice(6) }).then(
      out => reread().then(() => `${SWEEP_NAME[String(o.id).slice(6)]} sweep started ${String(out.started).slice(11, 16)} UTC · Tasks reads again until it ends`),
      err => { if ([400, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether a sweep started is not known`); }) });
  function reread() { return reading.reread(); }
  load();
  document.addEventListener('adversarial:run', e => { if (e.detail.key === `task:${byKey(selected)?.id}`) renderDetails(); });
  document.addEventListener('shell:ready', () => window.places.viewSwitch(id => { if (id === PLACE) return; PLACE = id; L.page = 1; if (READ) { subhead(); render(); } }), { once: true });
  suggest(['What is overdue across repos?', 'Which ready tasks are oldest?', 'Draft the reply that closes the selected task']);
});
