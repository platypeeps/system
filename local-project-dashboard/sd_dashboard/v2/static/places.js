// Places: the HOA and Home pages' views (Overview | Tasks | Briefs | Reports; HOA adds Alarms), a realm place's (sd:5663,
// place.js: Tasks | Briefs | Reports) and Tasks' place views.
// Data: a place view with data-api reads its own document when it is shown, /api/<place>/<view> (sd_dashboard/places.py).
// Overview is the page's own read, and so is HOA's Tasks: hoa.js draws it from /api/hoa, which the annunciator and the badge
// read anyway, with the Tasks page's Details (task-details.js, sd:5671). Which rows are a place's is decided once (places.area_of): Tasks' HOA and Home views filter on the `area` each
// /api/tasks row carries, so they hold the place pages' Tasks rows. A view that did not read is unknown with its reason, never
// an empty list. Nothing here writes; a brief's Details (brief-details.js) carry the Briefs page's commands, a report's
// (report-details.js) the Reports page's, and a task's (task-details.js) the Tasks page's. Markup is html`…` from markup.js:
// every value is escaped.
// Open | All (sd:5752): Tasks, Briefs and Reports show their open rows, and each row's primary act is its acknowledge: a
// task's Done (task-details.js, view 'place'), a brief's Seen (Today's mark, below), a report's Acknowledge (report.ack).
// All adds the week's done tasks (places.task_rows), the seen briefs or the acknowledged reports. The choice is the URL's
// ?<view>=all.
//
// Adapter: viewSwitch(show) is the one place a page meets the shell's view switch (shell.js view:start). The page declares
// window.PAGE_VIEWS = [{ id, label, key?, api? }] before shell.js; show(id) runs for the view shown, at shell:ready and on
// each change. The shell draws the switch into #viewswitch, hides the view-<id> panels and fires 'shell:view'.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);

  // Called at shell:ready, after the shell's first 'shell:view': the view already shown is shown here.
  function viewSwitch(show) {
    document.addEventListener('shell:view', e => show(e.detail.id));
    if (window.shell.view.current()) show(window.shell.view.current());
  }

  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  // sd:5752: the views with Open | All, each a row's open test; SHOW the choice per view, from the URL.
  const OPEN = { tasks: r => r.status !== 'done', briefs: r => !r.marked, reports: r => r.status !== 'done' };
  const SHOW = Object.fromEntries(Object.keys(OPEN).map(v => [v, new URLSearchParams(location.search).get(v) === 'all' ? 'all' : 'open']));
  const act = id => html`<td class="act">${window.shell.commands.rowActions(id)}</td>`, ACT = [html`<span class="sr">Actions</span>`];
  const unknown = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;
  const day = s => String(s || '').slice(0, 10);
  const table = (head, rows, empty) => html`<table class="ledger place-rows"><thead><tr>${head.map(([label, cls = '']) => html`<th scope="col" class="${cls}">${label}</th>`)}</tr></thead>
    <tbody>${rows.length ? rows : html`<tr class="empty"><td colspan="${head.length}">${empty}</td></tr>`}</tbody></table>`;

  // Each place view: its table head, one row, the empty text and the tally.
  // The view's tab carries the row count (sd:5524, shell.view.auto), so a tally says only what the count does not.
  const DRAW = {
    // sd:5635: a task row is selected in place; Details is the Tasks page's (task-details.js), with a link to Tasks.
    tasks: (doc, rows) => [table([['ID', 'num'], ['What'], ['Kind', 'hide-sm'], ['Status'], ['Due', 'hide-sm'], ACT],
      rows.map(r => html`<tr data-id="${r.id}" aria-selected="${String(String(r.id) === sel.tasks)}"><td class="ref num">sd:${r.id}</td><td class="what"><button type="button">${r.title}</button></td>
        <td class="hide-sm">${r.kind}</td><td>${r.status}</td><td class="hide-sm">${day(r.due) || '—'}</td>${act(String(r.id))}</tr>`),
      rows.length || !doc.done?.length ? $('view-tasks')?.dataset.empty || 'No open item without a repo.'
        : `No open item. All lists the ${plural(doc.done.length, 'item')} done this week.`), ''],
    // sd:5620: a brief row is selected in place; Details is the Briefs page's (brief-details.js), with a link to Briefs.
    briefs: (doc, rows) => [table([['Day'], ['Brief'], ['Words', 'num hide-sm'], ['Lead', 'hide-sm'], ACT],
      rows.map(r => html`<tr data-id="${r.id}" aria-selected="${String(r.id === sel.briefs)}"><td class="when">${r.day}</td><td class="what"><button type="button">${r.subj}</button></td>
        <td class="num hide-sm">${r.words}</td><td class="lead hide-sm">${r.lead}</td>${act(r.id)}</tr>`),
      rows.length || !doc.rows.length ? 'No brief is filed for this place.' : `No unseen brief. All lists the ${plural(doc.rows.length, 'seen brief')}.`), ''],
    // sd:5636: a report row is selected in place too; Details is the Reports page's (report-details.js), with a link to Reports.
    reports: (doc, rows) => {
      const state = r => r.status === 'done' ? 'ok' : r.attention ? 'warning' : 'queued';
      return [table([[html`<span class="sr">State</span>`], ['Job'], ['Report'], ['Status', 'hide-sm'], ['When'], ACT],
        rows.map(r => html`<tr data-id="r${r.id}" aria-selected="${String('r' + r.id === sel.reports)}"><td class="g ${state(r)}"><span aria-hidden="true">${GLYPH[state(r)]}</span><span class="sr">${state(r)}</span></td>
          <td class="job">${r.job}</td><td class="what"><button type="button">${r.title}</button></td><td class="hide-sm">${r.status}</td><td class="when">${day(r.at)}</td>${act('r' + r.id)}</tr>`),
        rows.length || !doc.rows.length ? `No report from ${doc.jobs.join(', ')} among the newest ${doc.limit}.`
          : `No open report from ${doc.jobs.join(', ')}. All lists the ${plural(doc.rows.length, 'acknowledged report')}.`),
        `${plural(doc.rows.filter(r => r.status !== 'done').length, 'open report')} · ${doc.jobs.length} jobs`];
    },
  };

  // sd:5481: a click anywhere on a row opens what its link opens; a click on the link itself, or with a modifier, stays the
  // browser's. sd:5620, sd:5636, sd:5635: a row with an id (a brief, a report or a task) is selected here instead, and its
  // Details fill the pane.
  document.addEventListener('click', e => {
    // A view that reads its own document (data-api) has its Open | All here; HOA's Tasks is hoa.js's.
    const f = e.target.closest('.place-view[data-api] [data-f]');
    if (OPEN[f?.dataset?.f]) { show(f.dataset.f, f.dataset.v); return; }
    const tr = e.target.closest('.place-rows tbody tr');
    if (tr?.dataset?.id && ROWS[shownView()]?.has(tr.dataset.id)) { if (!e.target.closest('a, .rowact')) pick(tr.dataset.id, true); return; }
    const a = tr?.querySelector('a[href]');
    if (!a || e.target.closest('a, button') || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || getSelection().toString()) return;
    location.assign(a.href);
  });

  // ---------- Selection in place (sd:5620) ----------
  // A view whose rows have a shared Details renderer (DETAILS) puts each row as a command object, selects it on a click, on
  // j / k and on Enter (the shell's grammar, through PAGE_LIST), and draws its Details in the pane. ROWS holds the view's
  // rows by id from its last read; PUT the ids of the objects it put; sel the selected id per view. An object the next read
  // no longer puts is put again as not listed, so no command runs on it, and Details says a row gone is gone.
  // A renderer is { rows(doc), objects(rows), show(row), forget(), key? }: rows(doc) are the read's rows, as Details reads them,
  // by key(row), else by id.
  const ROWS = {}, PUT = {}, sel = {}, RAW = {}, DETAILS = {};
  const shownView = () => window.shell?.view?.current?.();
  const details = () => $('details');
  // The Briefs page's Details (brief-details.js), with a link that opens the brief on Briefs.
  // sd:5752: Seen is Today's (today-reports.js): the Today snooze under the brief's Today row id (`mark`) and fingerprint
  // (`seen`), until it changes (snooze.js FOREVER), so a brief seen here or on Today is seen in both. Not seen clears it;
  // each is the other's Undo. Registered before the Briefs page's commands, so Seen, not Read, is an open row's act.
  function briefs(view) {
    const B = window.briefDetails; if (!B) return null;
    const mark = (o, until) => window.shell.post('/api/snooze', { page: 'today', row: o.mark, until, seen: o.seen }).then(() => window.places.reread(view));
    const cli = () => 'no sd verb: the dashboard writes the Today snooze (POST /api/snooze)';
    window.shell.commands.register(
      { id: 'brief.seen', on: 'brief', label: 'Seen', does: 'Marks the brief seen here and on Today until it changes; Undo clears it.', icon: 'check', risk: 'undo', bulk: true,
        primary: o => !o.marked, when: o => !o.marked || 'already seen', cli, run: o => mark(o, 'forever').then(() => `Seen · ${o.label}`), undo: o => mark(o, null) },
      { id: 'brief.unseen', on: 'brief', label: 'Not seen', does: 'Clears the Seen mark here and on Today; Undo marks it again.', icon: 'eye', risk: 'undo',
        when: o => o.marked || 'not marked seen', cli, run: o => mark(o, null).then(() => `Not seen · ${o.label}`), undo: o => mark(o, 'forever') });
    const d = B.attach({ details: details(), reread: () => window.places.reread(view), read: () => RAW[view]?.read || new Date().toISOString(),
      watch: () => RAW[view]?.watchdog?.available ? 'read' : (RAW[view]?.watchdog?.reason || 'no watchdog reader'),
      more: b => html`<p class="why"><a href="/briefs?row=${encodeURIComponent(b.id)}">Open on Briefs</a>: the cadence strip and every source.</p>` });
    return { rows: doc => doc.rows, objects: rows => rows.map(b => ({ ...B.object(b), mark: b.mark, seen: b.seen, marked: b.marked })), show: d.show, forget: d.forget };
  }
  // The Reports page's Details (report-details.js), read from the view's `details`: the Reports page's document for the
  // place's jobs. A write posts, then reads the view again; a refused stale one reads it again too, as Reports does.
  function reports(view) {
    const R = window.reportDetails; if (!R) return null;
    let M = R.model({});
    const again = () => window.places.reread(view);
    const write = async (path, body) => {
      let out;
      try { out = await window.shell.post(path, body); } catch (err) { if (err.stale) again(); throw err; }
      await again();
      return out;
    };
    // HOA's source rows are 'job' objects with HOA's own Run now (hoa.js), so a report's job is a 'report-job' here.
    const d = R.attach({ details: details(), model: () => M, write, load: again, job: 'report-job',
      more: r => html`<p class="why"><a href="/reports?row=${encodeURIComponent(r.id)}">Open on Reports</a>: the run cadence, the family lamps and every report.</p>` });
    return { rows: doc => (M = R.model(doc.details || {})).rows, objects: () => d.objects(), show: d.show, forget: () => {} };
  }
  // The Tasks page's Details (task-details.js), from the view's rows, which are /api/tasks rows (places.tasks). A write
  // folds into its row, then reads the view again; a refused one reads it again too, as Tasks does.
  function tasks(view) {
    const TD = window.taskDetails; if (!TD) return null;
    let T = [];
    const shown = () => shownView() === view ? sel[view] ?? null : null, api = () => window.PAGE_VIEWS?.find(x => x.id === view)?.api;
    const d = TD.attach({ details: details(), rows: () => T, selected: shown, read: () => RAW[view]?.read, view: () => 'place',
      render: () => { if (shown() != null) d.renderDetails(); }, reread: () => window.places.reread(view),
      load: () => window.places.reread(view).then(() => RAW[view]?.state === 'read'), select: (key, open) => pick(key, open), lead: () => !!RAW[view]?.lead,
      foot: () => RAW[view]?.read ? html`Rows read <time class="rel" datetime="${RAW[view].read}"></time> from ${api()}.` : '',
      more: t => html`<p class="why"><a href="${window.shell.itemHref(t.id)}">Open on Tasks</a>: the board, the matrix and every task.</p>` });
    // sd:5752: the week's done rows are objects too, so a row Done just moved out of Open still has its Undo.
    return { key: t => t.key, rows: doc => { T = [...doc.rows || [], ...doc.done || []].map(TD.shape); d.adopt(T); return T; }, objects: rows => rows.map(d.objectOf),
      show: t => { window.shell.commands.select(t.key); d.readDet(t.id); d.renderDetails(); }, forget: () => {} };
  }
  // The shell's picked rows, so a retired row's pick is dropped first, as read.js retires: the shell refuses to toggle a
  // pick on a type no bulk command is on, and the bulk bar would run on the retired object.
  let picks = [];
  document.addEventListener('shell:picked', e => { picks = e.detail || []; });
  function adopt(view, doc) {
    const D = DETAILS[view]; if (!D) return;
    const C = window.shell.commands, before = PUT[view] || new Set(), rows = D.rows(doc), objs = D.objects(rows);
    RAW[view] = doc; ROWS[view] = new Map(rows.map(r => [(D.key || (x => x.id))(r), r])); PUT[view] = new Set(objs.map(o => o.id));
    const gone = [...before].filter(id => !PUT[view].has(id));
    picks.filter(id => gone.includes(id)).forEach(id => C.pick(id));
    gone.forEach(id => C.put({ id, type: 'not listed', label: `${C.get(id)?.label || id} (no longer listed)` }));
    objs.forEach(o => C.put(o));
  }
  function mark(view) {
    document.querySelectorAll(`#${view}-rows tbody tr[data-id]`).forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === sel[view])));
  }
  function pick(id, open) {
    const view = shownView(), r = ROWS[view]?.get(id), D = DETAILS[view]; if (!r || !D) return;
    sel[view] = id; mark(view); D.show(r);
    if (open) window.shell.openPane('tab-details');
  }
  // After a read: the selected row again, or the ?row= the page opened with; a row gone says so in Details. A read that
  // failed says nothing about the row: it stays selected for the next read, runs no command, and Details says it is unknown.
  function settle(view, first) {
    const D = DETAILS[view]; if (!D || view !== shownView()) return;
    const want = sel[view] ?? (first ? window.shell.row?.() : null);
    if (want != null && ROWS[view].has(want)) { pick(want, false); return; }
    const read = RAW[view]?.state === 'read';
    if (sel[view] != null) { D.forget(); window.shell.commands.select(null); put(details(), read ? html`<p class="why">The selected row is not in the last read of ${view}.</p>`
      : html`<p class="why">The ${view} were not read again, so the selected row is not known: ${RAW[view]?.reason || 'no reason given'}.</p>`); }
    if (read || sel[view] == null) sel[view] = null;
  }
  // The shell walks rows through PAGE_LIST (shell.js walk). The page's own list stays, and the shown place view's rows
  // follow it: a row j / k reaches in a hidden view is skipped by the shell.
  function list() {
    const own = window.PAGE_LIST, owns = id => !!ROWS[shownView()]?.has(id) && !!DETAILS[shownView()];
    window.PAGE_LIST = { ...own,
      rows: () => [...(own?.rows?.() || []), ...document.querySelectorAll('.place-rows tbody tr[data-id]')],
      current: () => DETAILS[shownView()] ? sel[shownView()] ?? null : own?.current?.() ?? null,
      select: (id, el) => owns(id) ? pick(id, false) : own?.select?.(id, el) };
    document.addEventListener('shell:open', e => { if (owns(e.detail)) pick(e.detail, true); });
  }

  const seen = {}, loaded = {};
  async function load({ id: view, api }) {
    const box = $(`${view}-rows`), tally = $(`${view}-tally`), mine = seen[view] = (seen[view] || 0) + 1;
    put(box, html`<p class="note">Reading ${view}…</p>`); put(tally, html``);
    let doc;
    try { doc = await window.shell.getJSON(api); } catch (e) { doc = { state: 'unknown', reason: `${api} did not answer: ${e.message}`, rows: [] }; }
    if (mine !== seen[view]) return;
    if (doc.state !== 'read') { adopt(view, { ...doc, rows: [] }); put(box, unknown(doc.reason)); settle(view, !loaded[view]++); return; }
    adopt(view, doc); RAW[view] = doc;
    draw(view);
    settle(view, !loaded[view]++);
  }
  // A read view's rows: the open ones, or with All every row it read (sd:5752), under the filter bar's Open | All.
  function draw(view) {
    const doc = RAW[view], box = $(`${view}-rows`), tally = $(`${view}-tally`), open = OPEN[view];
    const every = view === 'tasks' ? [...doc.rows, ...doc.done || []] : doc.rows, rows = !open || SHOW[view] === 'all' ? every : every.filter(open);
    const seg = (v, word, n) => ({ v, pressed: SHOW[view] === v, title: `${word}: ${n}`, label: html`${word} <span class="n">${String(n)}</span>` });
    const bar = open ? html`<div class="filters">${window.shell.list.fbar(window.shell.list.fseg(view, 'Shown', [seg('open', 'Open', every.filter(open).length), seg('all', 'All', every.length)]))}</div>` : '';
    const [body, count] = DRAW[view](doc, rows);
    put(box, html`${doc.reason ? html`<p class="note">${doc.reason}</p>` : ''}${bar}${body}`);
    put(tally, count ? html`<span>${count}</span>` : html``);
  }
  // Open | All: the choice goes in the URL (?<view>=all; Open is the default and leaves none), and the view draws again.
  function show(view, v) {
    if (!['open', 'all'].includes(v) || SHOW[view] === v) return;
    SHOW[view] = v;
    const p = new URLSearchParams(location.search);
    if (v === 'all') p.set(view, 'all'); else p.delete(view);
    window.shell.url(p);
    if (RAW[view]?.state === 'read') draw(view);
  }

  // A place page: <body data-place="hoa|home|<realm id>"> and one block per view, id="view-<id>" with data-place-view, its data-label,
  // and data-api when the view reads its own document. A view reads each time it shows, not only the first ('shell:view'
  // first), since nothing else reads it again. The wall display (data-kiosk on the root element, home-kiosk.js) stays on
  // the Overview: it declares no views.
  const blocks = [...document.querySelectorAll('[data-place-view]')];
  function place() {
    if (blocks.some(b => b.dataset.placeView === 'briefs')) DETAILS.briefs = briefs('briefs');
    if (blocks.some(b => b.dataset.placeView === 'reports')) DETAILS.reports = reports('reports');
    // HOA's Tasks is hoa.js's table from /api/hoa, which attaches task-details.js itself (sd:5671): only a Tasks view that reads its rows.
    if (blocks.some(b => b.dataset.placeView === 'tasks' && b.dataset.api)) DETAILS.tasks = tasks('tasks');
    Object.keys(DETAILS).forEach(v => { if (!DETAILS[v]) delete DETAILS[v]; });
    list();
    viewSwitch(id => {
      blocks.forEach(b => { b.hidden = b.dataset.placeView !== id; });
      const v = window.PAGE_VIEWS.find(x => x.id === id);
      if (v?.api) load(v);
    });
  }

  // reread(view): read a place view again after a write its Details made (a brief's Publish, a report's Acknowledge).
  const reread = view => { const v = window.PAGE_VIEWS?.find(x => x.id === view); return v?.api ? load(v) : Promise.resolve(); };
  // again(): Read again (sd:5638) reads the shown place view again; a view without its own read has nothing to read.
  const again = () => reread(window.shell.view.current());
  window.places = { viewSwitch, place, reread, again };
  // Top level: places.js loads before shell.js, which reads PAGE_VIEWS when it loads.
  if (document.body.dataset.place && !document.documentElement.hasAttribute('data-kiosk')) {
    window.PAGE_VIEWS = blocks.map(b => ({ id: b.dataset.placeView, label: b.dataset.label, ...(b.dataset.api ? { api: b.dataset.api } : {}) }));
    document.addEventListener('shell:ready', place, { once: true });
  }
})();
