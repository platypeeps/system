// Places: the HOA and Home pages' views (Overview | Tasks | Briefs | Reports; HOA adds Alarms), a realm place's (sd:5663,
// place.js: Tasks | Briefs | Reports) and Tasks' place views.
// Data: a place view with data-api reads its own document when it is shown, /api/<place>/<view> (sd_dashboard/places.py).
// Overview is the page's own read, and so is HOA's Tasks: hoa.js draws it from /api/hoa, which the annunciator and the badge
// read anyway. Which rows are a place's is decided once (places.area_of): Tasks' HOA and Home views filter on the `area` each
// /api/tasks row carries, so they hold the place pages' Tasks rows. A view that did not read is unknown with its reason, never
// an empty list. Nothing here writes; a brief's Details (brief-details.js) carry the Briefs page's commands. Markup is
// html`…` from markup.js: every value is escaped.
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
  const unknown = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;
  const day = s => String(s || '').slice(0, 10);
  const table = (head, rows, empty) => html`<table class="ledger place-rows"><thead><tr>${head.map(([label, cls = '']) => html`<th scope="col" class="${cls}">${label}</th>`)}</tr></thead>
    <tbody>${rows.length ? rows : html`<tr class="empty"><td colspan="${head.length}">${empty}</td></tr>`}</tbody></table>`;

  // Each place view: its table head, one row, the empty text and the tally. The rows open where their page acts on them.
  // The view's tab carries the row count (sd:5524, shell.view.auto), so a tally says only what the count does not.
  const DRAW = {
    tasks: doc => [table([['ID', 'num'], ['What'], ['Kind', 'hide-sm'], ['Status'], ['Due', 'hide-sm']],
      doc.rows.map(r => html`<tr><td class="ref num"><a href="${window.shell.itemHref(r.id)}">sd:${r.id}</a></td><td class="what">${r.title}</td>
        <td class="hide-sm">${r.kind}</td><td>${r.status}</td><td class="hide-sm">${day(r.due) || '—'}</td></tr>`),
      $('view-tasks')?.dataset.empty || 'No open item without a repo.'), ''],
    // sd:5620: a brief row is selected in place; Details is the Briefs page's (brief-details.js), with a link to Briefs.
    briefs: doc => [table([['Day'], ['Brief'], ['Words', 'num hide-sm'], ['Lead', 'hide-sm']],
      doc.rows.map(r => html`<tr data-id="${r.id}" aria-selected="${String(r.id === sel.briefs)}"><td class="when">${r.day}</td><td class="what"><button type="button">${r.subj}</button></td>
        <td class="num hide-sm">${r.words}</td><td class="lead hide-sm">${r.lead}</td></tr>`),
      'No brief is filed for this place.'), ''],
    reports: doc => {
      const state = r => r.status === 'done' ? 'ok' : r.attention ? 'warning' : 'queued';
      return [table([[html`<span class="sr">State</span>`], ['Job'], ['Report'], ['Status', 'hide-sm'], ['When']],
        doc.rows.map(r => html`<tr><td class="g ${state(r)}"><span aria-hidden="true">${GLYPH[state(r)]}</span><span class="sr">${state(r)}</span></td>
          <td class="job">${r.job}</td><td class="what"><a href="/reports?row=r${r.id}">${r.title}</a></td><td class="hide-sm">${r.status}</td><td class="when">${day(r.at)}</td></tr>`),
        `No report from ${doc.jobs.join(', ')} among the newest ${doc.limit}.`), `${plural(doc.rows.filter(r => r.status !== 'done').length, 'open report')} · ${doc.jobs.length} jobs`];
    },
  };

  // sd:5481: a click anywhere on a row opens what its link opens (Tasks or Reports with the row's Details and its actions);
  // a click on the link itself, or with a modifier, stays the browser's. sd:5620: a row with an id (a brief) is selected
  // here instead, and its Details fill the pane.
  document.addEventListener('click', e => {
    const tr = e.target.closest('.place-rows tbody tr');
    if (tr?.dataset?.id && ROWS[shownView()]?.has(tr.dataset.id)) { if (!e.target.closest('a, .rowact')) pick(tr.dataset.id, true); return; }
    const a = tr?.querySelector('a[href]');
    if (!a || e.target.closest('a, button') || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || getSelection().toString()) return;
    location.assign(a.href);
  });

  // ---------- Selection in place (sd:5620) ----------
  // A view whose rows have a shared Details renderer (DETAILS) puts each row as a command object, selects it on a click, on
  // j / k and on Enter (the shell's grammar, through PAGE_LIST), and draws its Details in the pane. ROWS holds the view's
  // rows by id from its last read; sel the selected id per view. A row the next read no longer lists is put again as not
  // listed, so no command runs on it, and Details says it is gone.
  const ROWS = {}, sel = {}, RAW = {}, DETAILS = {};
  const shownView = () => window.shell?.view?.current?.();
  const details = () => $('details');
  // The Briefs page's Details (brief-details.js), with a link that opens the brief on Briefs.
  function briefs(view) {
    const B = window.briefDetails; if (!B) return null;
    const d = B.attach({ details: details(), reread: () => window.places.reread(view), read: () => RAW[view]?.read || new Date().toISOString(),
      watch: () => RAW[view]?.watchdog?.available ? 'read' : (RAW[view]?.watchdog?.reason || 'no watchdog reader'),
      more: b => html`<p class="why"><a href="/briefs?row=${encodeURIComponent(b.id)}">Open on Briefs</a>: the cadence strip and every source.</p>` });
    return { object: B.object, show: d.show, forget: d.forget };
  }
  function adopt(view, doc) {
    const D = DETAILS[view]; if (!D) return;
    const C = window.shell.commands, before = ROWS[view] || new Map();
    RAW[view] = doc; ROWS[view] = new Map(doc.rows.map(r => [r.id, r]));
    before.forEach((r, id) => { if (!ROWS[view].has(id)) C.put({ id, type: 'not listed', label: `${C.get(id)?.label || id} (no longer listed)` }); });
    ROWS[view].forEach(r => C.put(D.object(r)));
  }
  function mark(view) {
    document.querySelectorAll(`#${view}-rows tbody tr[data-id]`).forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === sel[view])));
  }
  function pick(id, open) {
    const view = shownView(), r = ROWS[view]?.get(id), D = DETAILS[view]; if (!r || !D) return;
    sel[view] = id; mark(view); D.show(r);
    if (open) window.shell.openPane('tab-details');
  }
  // After a read: the selected row again, or the ?row= the page opened with; a row gone says so in Details.
  function settle(view, first) {
    const D = DETAILS[view]; if (!D || view !== shownView()) return;
    const want = sel[view] ?? (first ? window.shell.row?.() : null);
    if (want != null && ROWS[view].has(want)) { pick(want, false); return; }
    if (sel[view] != null) { D.forget(); window.shell.commands.select(null); put(details(), html`<p class="why">The selected row is not in the last read of ${view}.</p>`); }
    sel[view] = null;
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
    adopt(view, doc);
    const [body, count] = DRAW[view](doc);
    put(box, html`${doc.reason ? html`<p class="note">${doc.reason}</p>` : ''}${body}`);
    put(tally, count ? html`<span>${count}</span>` : html``);
    settle(view, !loaded[view]++);
  }

  // A place page: <body data-place="hoa|home|<realm id>"> and one block per view, id="view-<id>" with data-place-view, its data-label,
  // and data-api when the view reads its own document. A view reads each time it shows, not only the first ('shell:view'
  // first), since nothing else reads it again. The wall display (data-kiosk on the root element, home-kiosk.js) stays on
  // the Overview: it declares no views.
  const blocks = [...document.querySelectorAll('[data-place-view]')];
  function place() {
    if (blocks.some(b => b.dataset.placeView === 'briefs')) DETAILS.briefs = briefs('briefs');
    if (!DETAILS.briefs) delete DETAILS.briefs;
    list();
    viewSwitch(id => {
      blocks.forEach(b => { b.hidden = b.dataset.placeView !== id; });
      const v = window.PAGE_VIEWS.find(x => x.id === id);
      if (v?.api) load(v);
    });
  }

  // reread(view): read a place view again after a write its Details made (a brief's Publish).
  const reread = view => { const v = window.PAGE_VIEWS?.find(x => x.id === view); return v?.api ? load(v) : Promise.resolve(); };
  window.places = { viewSwitch, place, reread };
  // Top level: places.js loads before shell.js, which reads PAGE_VIEWS when it loads.
  if (document.body.dataset.place && !document.documentElement.hasAttribute('data-kiosk')) {
    window.PAGE_VIEWS = blocks.map(b => ({ id: b.dataset.placeView, label: b.dataset.label, ...(b.dataset.api ? { api: b.dataset.api } : {}) }));
    document.addEventListener('shell:ready', place, { once: true });
  }
})();
