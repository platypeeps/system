// ---------- Page script: data, render, Details, commands. Read design.md § Page contract and § Commands. ----------
// Designs (sd:2126). Built from the design source's products/system/designs/pages/designs.js at 2729265.
// build: the ledger comes from /api/designs through shell.read, not from data/designs-data.js; links open /designs/<path>.
// build (sd:3447): Hide and Unhide post /api/designs/view, the workflow database's view state that Documents' Hide writes.
// A hidden row leaves the ledger, its lamps and the badge. Hidden is a page view (sd:5064): Pages, the default, lists the
// unhidden rows and Hidden the hidden ones. An older ?show=hidden or ?f=hidden link lands on Hidden. The lamps count the rows
// the view draws, and a lamp narrows them further. An unread store hides nothing and turns both commands off with its reason.
// Hidden is never the remembered view (keep: false), so the page opens on Pages.
window.PAGE_VIEWS = [{ id: 'pages', label: 'Pages' }, { id: 'hidden', label: 'Hidden', keep: false }];
{
  const p = new URLSearchParams(location.search);
  if (p.get('show') === 'hidden' || p.get('f') === 'hidden') {
    p.delete('show'); if (p.get('f') === 'hidden') p.delete('f');
    const q = p.toString();
    try { history.replaceState(history.state, '', `${location.pathname}${q ? `?${q}` : ''}#view=hidden`); } catch (_) {}
  }
}
const { html, put, cells } = window.markup;
const I = (n, cls = '') => html`<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
const DAY = 864e5;
// build: the tab serves the checkout root at /designs/, so a page and its screenshots sit at /designs/<path>.
const UP = '/designs/';
let D = null, READ = 0, ROWS = [], PRODUCTS = [], STORE = '';

// Rows: one per page, plus one per product that has a brief and no page (the classic tab lists nothing for it).
const shotStale = p => (p.shotStale || []).length;
function rowsOf(doc) {
  return [
    ...doc.pages.map(p => {
      // Caution is what a commit or a retake clears. A retake records the inputs hash again, so it clears a stale shot even
      // when it draws the same pixels.
      const why = p.dirty ? 'uncommitted change in the working tree' : !p.shots.length ? 'no screenshot'
        : shotStale(p) ? `stale screenshot: ${p.shotStale.map(f => f.split('/').pop()).join(', ')}` : '';
      return { ...p, hidden: !!p.hidden, id: p.path, type: 'page', local: p.path.slice(`products/${p.product}/`.length), state: why ? 'caution' : 'ok', why };
    }),
    ...doc.products.filter(pr => !pr.pages).map(pr => ({ id: `product:${pr.name}`, type: 'brief', hidden: !!pr.hidden, product: pr.name, local: pr.brief.split('/').pop() || '(no brief)',
      kind: 'brief only', state: 'queued', why: 'a brief and no page yet', changed: '', shots: [], dirty: false })),
  ];
}
// tip (sd:5287): the lamp's hover and focus text, what it counts and from where.
const FILTERS = {
  recent:  { lbl: 'Changed 7 d', icon: 'git-branch', test: r => r.changed && READ - Date.parse(r.changed) < 7 * DAY, tip: 'Pages whose last commit is under 7 days old. Source: git log of the ui-design checkout.' },
  stale:   { lbl: 'Shot stale', icon: 'eye', test: r => r.type === 'page' && !!shotStale(r), warn: true, tip: 'Pages whose page or a file it loads changed after the screenshot. Source: the screenshots\' recorded inputs.' },
  noshot:  { lbl: 'No screenshot', icon: 'eye-off', test: r => r.type === 'page' && !r.shots.length, warn: true, tip: 'Pages with no screenshot in the checkout. Source: the ui-design checkout.' },
  dirty:   { lbl: 'Uncommitted', icon: 'pen-line', test: r => r.dirty, warn: true, tip: 'Pages with a change not yet committed. Source: git status of the ui-design checkout.' },
  brief:   { lbl: 'Brief only', icon: 'file-text', test: r => r.type === 'brief', tip: 'Products with a design brief and no drawn page. Source: the ui-design checkout\'s products folder.' },
};
const shown = r => F.hidden === r.hidden;
const tbody = document.getElementById('rows'), details = document.getElementById('details');
let selected = null, F = { f: '', q: '', state: '', product: '', hidden: false };
let pickedIds = new Set();
const match = r => shown(r) && (!F.f || FILTERS[F.f].test(r)) && (!F.state || r.state === F.state) && (!F.product || r.product === F.product)
  && (!F.q || `${r.path || ''} ${r.title || ''} ${r.subject || ''} ${r.kind} ${r.product}`.toLowerCase().includes(F.q));
const active = () => !!(F.f || F.q || F.state || F.product);
// Order: product, then the header's column (sd:3455) inside it. Page, the default, is the design's order: the drawn pages
// before the skeleton and captures, then path; it also breaks a tie in the other columns.
const KIND = { 'v2 mockup': 0, 'v1 mockup': 1, design: 2, final: 0, reference: 3, skeleton: 4, 'brief only': 5 };
const RANK = { warning: 0, caution: 1, unknown: 2, queued: 3, ok: 4 };
const BY = {
  s: (a, b) => RANK[a.state] - RANK[b.state],
  page: (a, b) => (KIND[a.kind] ?? 9) - (KIND[b.kind] ?? 9) || a.local.localeCompare(b.local),
  at: (a, b) => (Date.parse(a.changed) || 0) - (Date.parse(b.changed) || 0),
};
const LIST = { sort: 'page', dir: 1, size: 50 }, L = { ...LIST, page: 1 };
const COLS = [['s', html`<span class="sr">State</span>`, 'g'], ['page', 'Page'], ['at', 'Changed', 'r'], [null, html`<span class="sr">Actions</span>`, 'act']];
const ranked = () => ROWS.filter(match).sort((a, b) => PRODUCTS.indexOf(a.product) - PRODUCTS.indexOf(b.product) || BY[L.sort](a, b) * L.dir || BY.page(a, b));
// Details times are cells too (design.md, Page contract): the shell fills the text and the exact local and UTC title.
const T = iso => iso ? html`<time class="rel" datetime="${iso}" data-long></time>` : '—';

function renderAnnunciator() {
  // No "all pages" lamp: the head counts the pages and a pressed lamp, pressed again, clears its filter.
  const n = k => ROWS.filter(r => shown(r) && FILTERS[k].test(r)).length;
  const cell = (k, state, val) => ({ button: true, state, pressed: F.f === k, attrs: html` data-filter="${k}"`, label: FILTERS[k].lbl, tip: FILTERS[k].tip,
    mark: state === 'caution' ? html`<span class="g-caution" aria-hidden="true">▲</span>` : I(FILTERS[k].icon), val });
  const [hh, day] = [D.read.slice(11, 16), `${+D.read.slice(8, 10)} ${'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ')[+D.read.slice(5, 7) - 1]}`];
  // build: Observed rereads /api/designs instead of reloading the page.
  put(document.getElementById('annunciator'), cells([...Object.keys(FILTERS).map(k => { const c = n(k); return cell(k, FILTERS[k].warn && c ? 'caution' : 'ok', html`<b>${c}</b> ${markup.plural.word(c, k === 'brief' ? 'product' : 'page')}`); }),
    { button: true, attrs: html` id="refresh"`, label: 'Observed', mark: I('rotate-ccw'), val: html`<span class="ph"><b>${hh}</b> UTC</span> · <span class="ph">${day} · ${D.head || 'no commit'}</span>` }]));
}
function renderShow() {
  const hid = ROWS.filter(r => r.hidden).length;
  shell.view.count('pages', ROWS.length - hid); shell.view.count('hidden', hid);
}
function renderRows() {
  const rows = ranked();
  let last = '';
  put(document.getElementById('rows-head'), shell.list.sortHead(COLS, L));
  put(tbody, html`${rows.map(r => {
    const pr = D.products.find(p => p.name === r.product);
    const head = r.product !== last ? html`<tr class="prod" aria-hidden="true"><td colspan="4">${r.product}<span class="n">${markup.plural(pr.pages, 'page')}</span></td></tr>` : ''; last = r.product;
    const slash = r.local.lastIndexOf('/');
    const name = slash < 0 ? r.local : html`<span class="dir">${r.local.slice(0, slash + 1)}</span>${r.local.slice(slash + 1)}`;
    const sub = r.type === 'page' ? [r.kind, r.why || markup.plural(r.shots.length, 'screenshot'), r.subject].filter(Boolean).join(' · ') : `brief only · ${r.why}`;
    return html`${head}<tr data-id="${r.id}"${r.changed ? html` data-changed="${r.changed}"` : ''} aria-selected="${String(r.id === selected)}"${pickedIds.has(r.id) ? html` data-picked` : ''}>
    <td class="g"><span class="g-${r.state}" aria-hidden="true">${GLYPH[r.state]}</span><span class="sr">${r.state}</span></td>
    <td class="what"><button type="button" data-open="${r.id}"><span class="sr">${r.product}: </span>${name}<small>${sub}</small></button></td>
    <td class="at">${r.changed ? html`<time class="rel" datetime="${r.changed}"></time>` : '—'}</td>
    <td class="act">${shell.commands.rowActions(r.id)}</td></tr>`;
  })}`);
  const empty = document.getElementById('empty');
  empty.hidden = rows.length > 0; empty.textContent = F.hidden && !active() ? `Nothing is hidden. ${STORE ? `${STORE}.` : 'Hide a row from its actions or Details.'}` : active() ? 'No page matches this filter. Clear it to see all pages.' : `No design pages found in ${D.root}/products.`;
  const pages = ROWS.filter(r => r.type === 'page').length;
  // The shown view's tab carries the rows it draws (sd:5524).
  shell.view.count(F.hidden ? 'hidden' : 'pages', rows.length);
  renderFilters();
  document.getElementById('sub').textContent = `${pages} pages in ${PRODUCTS.length} products`;
  document.getElementById('src').textContent = `${D.root} at ${D.head || 'no commit'}`;
}
// The filter bar (sd:5463), Activity's layout from shell.list: the state as a segment with its counts, the product as a
// pick, a pressed lamp and the query as removable tokens, then Clear all. A lamp already filters by kind, so it has no chip.
const STATES = [['caution', 'needs a look'], ['ok', 'ok'], ['queued', 'brief only']];
function renderFilters() {
  const n = (k, v) => ROWS.filter(r => shown(r) && r[k] === v).length, list = shell.list;
  put(document.getElementById('filters'), list.fbar(html`${list.fseg('state', 'State', STATES.map(([s, w]) => ({ v: s, pressed: F.state === s, title: `${w}: ${n('state', s)}`,
    label: html`<span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span><span class="n">${String(n('state', s))}</span><span class="sr"> ${w}</span>` })))}
    ${list.pick('product', 'Product', PRODUCTS.map(p => [p, p, n('product', p)]), F.product, { all: 'All products' })}
    ${F.f ? list.tok('f', F.f, FILTERS[F.f].lbl) : ''}${F.q ? list.tok('q', '', `“${F.q}”`) : ''}`, { clear: active() }));
}
function show(id) {
  const r = ROWS.find(x => x.id === id); if (!r) return;
  const pr = D.products.find(p => p.name === r.product);
  if (r.type === 'brief') {
    put(details, html`<p class="kind"><span class="g-queued" aria-hidden="true">◌</span> Product · brief only</p>
      <h2>${r.product}</h2>
      <dl><dt>Brief</dt><dd>${pr.brief || 'none'}</dd><dt>Status</dt><dd>${pr.status.replace(/`/g, '') || 'no Status line'}</dd><dt>Pages</dt><dd>0</dd></dl>
      <p class="why">The brief exists and no page is drawn yet. The classic tab lists no section for a product without a page, so this row is the only place it shows.</p>
      <h3>Act</h3>${shell.commands.bar(id)}
      ${pr.briefText ? html`<h3>Brief</h3><pre class="brief">${pr.briefText}</pre>${pr.briefCut ? html`<p class="why">The brief is longer than this. The rest is in ${pr.brief}.</p>` : ''}` : ''}`);
    shell.suggest([`What should the first ${r.product} page be?`, `Summarise the ${r.product} brief.`]);
  } else {
    const shot = r.shots.find(s => /1440/.test(s)) || r.shots[0];
    put(details, html`<p class="kind"><span class="g-${r.state}" aria-hidden="true">${GLYPH[r.state]}</span> Page · ${r.kind}</p>
      <h2>${r.local}</h2>
      <p><a class="open" href="${UP + r.path}">${I('arrow-up-right')}Open as drawn</a></p>
      <dl><dt>Product</dt><dd>${r.product}</dd><dt>Title</dt><dd>${r.title || '—'}</dd>
        <dt>Changed</dt><dd>${T(r.changed)} · ${r.sha || 'never committed'}</dd><dt>Commit</dt><dd>${r.subject || '—'}</dd>
        <dt>Size</dt><dd>${(r.bytes / 1024).toFixed(1)} KB</dd>
        <dt>Screenshots</dt><dd>${r.shots.length ? html`${r.shots.length}${shotStale(r) ? `, ${shotStale(r)} stale` : ', current'} · oldest ${T(r.shotChanged)} · ${r.shotSha || 'never committed'} (${(r.shotOldest || r.shots[0]).split('/').pop()})` : 'none'}</dd>
        <dt>Working tree</dt><dd>${r.dirty ? 'uncommitted change' : 'clean'}</dd></dl>
      ${r.why ? html`<p class="why">${r.why[0].toUpperCase() + r.why.slice(1) + '. '}${shotStale(r) && !r.dirty ? 'The page or a file it loads changed after that capture, so the picture may not match it. A retake clears it.' : ''}</p>` : ''}
      ${shot ? html`<figure class="shot"><img src="${UP + shot}" alt="Screenshot of ${r.local}" loading="lazy" width="${(r.shotSize?.[shot] || [1440])[0]}" height="${(r.shotSize?.[shot] || [0, 1000])[1]}"><figcaption>${shot.split('/').pop()} · ${T(r.shotTimes?.[shot])}</figcaption></figure>` : ''}
      <h3>Act</h3>${shell.commands.bar(id)}`);
    shell.suggest([`What changed in ${r.local} in ${r.sha || 'the working tree'}?`, 'Which pages need a new screenshot?']);
  }
  shell.commands.select(id);
  details.toggleAttribute('data-swap', true); requestAnimationFrame(() => details.removeAttribute('data-swap'));
}
function select(id, open) {
  selected = id; tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id)); show(id);
  if (open) shell.openPane('tab-details');
}
function clearSelection() {
  selected = null; shell.commands.select(null);
  put(details, html`<p class="why">No row selected · no page is visible under this filter.</p>`);
}
// The selection stays if its row is visible, moves to the first visible row otherwise, and clears when none is (shell.reconcile).
function reconcile() {
  const was = selected;
  const id = shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, select: id => select(id, false), clear: clearSelection });
  if (id && id === was) show(id);
}
// One name per filter (review 2026-09-29, item 18): the lamp and the palette entry both read FILTERS[k].lbl.
// The sort joins the filter in the URL when it is not the default, as shell.list.listQuery writes it.
function writeFilter() {
  const sort = L.sort !== LIST.sort || L.dir !== LIST.dir ? { sort: L.sort, dir: L.dir > 0 ? 'asc' : 'desc' } : {};
  shell.url({ ...(F.f && { f: F.f }), ...(F.state && { state: F.state }), ...(F.product && { product: F.product }), ...(input.value.trim() && { q: input.value.trim() }), ...sort });
}
function update() {
  if (!D) return;
  renderAnnunciator(); renderShow(); renderRows(); reconcile();
}

// Commands (commands.md). Opening a page is a link, not a command: the tab serves it, nothing runs.
// build: History and Retake are copy only; the dashboard runs no git, no shots.mjs and no pager. Read brief shows the brief.
// sd:5457: Hide or Unhide is every row's act; History and Retake stay off Details' bar (bar: false), not off the ⋯ menu,
// their keys or the palette. Read brief is not the row's act: the row's name opens the same Details.
const NO_CLI = 'no CLI: view state; the dashboard writes it (POST /api/designs/view)';
const view = (o, hidden) => shell.post('/api/designs/view', { id: o.id, hidden }).then(v => reading.reread().then(() => v));
// One Hide and one Unhide for each row type, a page and a brief-only product; the row shows the one that is on.
const hiding = on => [
  { id: 'design.hide', on, label: 'Hide', does: 'Hides this row from the list; Undo shows it again.', key: 'h', icon: 'eye-off', risk: 'undo', bulk: true, primary: () => true, when: o => o.hidden ? 'already hidden' : STORE || true,
    cli: () => NO_CLI, run: o => view(o, true).then(() => `Hidden: ${o.label}. The Hidden view lists it.`), undo: o => view(o, false) },
  { id: 'design.unhide', on, label: 'Unhide', does: 'Shows this row in the list again; Undo hides it.', key: 'h', icon: 'eye', risk: 'undo', bulk: true, primary: () => true, when: o => !o.hidden ? 'not hidden' : STORE || true,
    cli: () => NO_CLI, run: o => view(o, false).then(() => `Unhidden: ${o.label}`), undo: o => view(o, true) },
];
function registerCommands() {
  const C = shell.commands, REPO = () => D?.root || '~/repos/ui-design';
  C.register(
    // build: the brief's text comes in the ledger (designs.BRIEF_CHARS); Details shows it, the tab serving no Markdown.
    { id: 'design.brief', on: 'brief', label: 'Read brief', does: 'Shows the product\'s design brief in Details.', key: 'b', icon: 'file-text', risk: 'safe', executes: false,
      when: o => !!o.brief || 'this product has no brief', cli: o => `less ${REPO()}/${o.brief}`, run: o => { select(o.id, true); return `Brief of ${o.product} shown in Details`; } },
    ...hiding('page'), ...hiding('brief'),
  );
}
document.addEventListener('shell:open', e => { if (ROWS.some(r => r.id === e.detail)) select(e.detail, true); });
document.addEventListener('shell:picked', e => { pickedIds = new Set(e.detail); tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', pickedIds.has(tr.dataset.id))); });
let reading = null;
document.addEventListener('click', e => {
  const open = e.target.closest?.('tbody [data-open]'); if (open) { if (ROWS.some(r => r.id === open.dataset.open)) select(open.dataset.open, true); return; }
  const cell = e.target.closest?.('.annunciator .cell'); if (cell) {
    if (cell.id === 'refresh') return reading?.load();
    const k = cell.dataset.filter; F.f = F.f === k ? '' : k; writeFilter(); update(); return;
  }
  const tr = e.target.closest?.('tr[data-id]'); if (tr && !e.target.closest('button, a, time')) select(tr.dataset.id, false);
});
const input = document.getElementById('shift'), bar = document.getElementById('filters');
bar.addEventListener('change', e => shell.list.pickChange(e, F, () => { writeFilter(); update(); }));
bar.addEventListener('click', e => {
  const b = e.target.closest('[data-fclear], [data-f]'); if (!b) return;
  if (b.dataset.fclear != null) Object.assign(F, { f: '', q: '', state: '', product: '' });
  else if (b.dataset.f === 'state') F.state = F.state === b.dataset.v ? '' : b.dataset.v;
  else F[b.dataset.f] = '';
  if (!F.q) input.value = '';
  writeFilter(); update();
});
document.getElementById('rows-head').addEventListener('click', e => { if (D && shell.list.sortBy(e, L)) { shell.list.keepFocus(e, renderRows); writeFilter(); } });
input.addEventListener('input', () => { F.q = input.value.trim().toLowerCase(); writeFilter(); update(); });
document.addEventListener('keydown', e => {
  if (e.target.matches?.('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
  if (e.key === '/') { e.preventDefault(); input.focus(); }
});
// The shell walks these rows on j / k and writes ?row= when show() selects.
window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false) };

// Declared before shell.js runs: the badge, the palette's "This page" group and the key sheet read these.
// The badge is the read's own (sd:3446, designs.with_views): uncommitted pages, and pages with no or a stale screenshot.
function attention() {
  window.PAGE_ATTENTION = D && D.attention || { state: 'unknown', n: 0, what: 'designs not read' };
  shell.attention?.(window.PAGE_ATTENTION);
}
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'designs not read' };
window.PAGE_COMMANDS = [
  ...['stale', 'recent'].map(k => ({ label: `Filter: ${FILTERS[k].lbl}`, icon: 'filter', run: () => { F.f = k; writeFilter(); update(); } })),
];
window.PAGE_KEYS = [['/', 'Filter pages']];

// ---------- Start (build: read /api/designs through shell.read, then draw) ----------
// The reader (read.js) holds the guards: of overlapping reads only the newest draws, a row the read no longer lists runs no
// command, and a failed read clears the rows, the rail and Details.
function adopt(doc) {
  D = doc;
  READ = Date.parse(D.read) || Date.now();
  document.body.dataset.observed = D.read; // shell time cells count from the reading, not from now
  // Products with drawn pages first, then brief-only ones; the first visible row is then a page, not an empty product.
  PRODUCTS = [...D.products].sort((a, b) => !a.pages - !b.pages || a.name.localeCompare(b.name)).map(p => p.name);
  ROWS = rowsOf(D);
  STORE = D.store && !D.store.available ? `Hide is off: ${D.store.reason}` : '';
  const objects = ROWS.map(r => ({ id: r.id, type: r.type, label: r.local, path: r.path || '', product: r.product, kind: r.kind, hidden: r.hidden,
    brief: D.products.find(p => p.name === r.product).brief }));
  return { objects, state: ROWS.length ? null : { kind: 'empty', title: 'No designs', source: '/api/designs', text: `No design pages found in ${D.root}/products.` } };
}
function clear() {
  D = null; ROWS = []; PRODUCTS = []; STORE = ''; delete document.body.dataset.observed;
  put(document.getElementById('annunciator'), html``); shell.view.count('pages', null); shell.view.count('hidden', null); put(tbody, html``);
  put(document.getElementById('filters'), html``);
  document.getElementById('sub').textContent = 'Not read';
  document.getElementById('src').textContent = 'ui-design';
  put(details, html`<p class="why">The designs were not read. Reload retries it.</p>`);
}
document.addEventListener('DOMContentLoaded', () => {
  registerCommands();
  const p = new URLSearchParams(location.search);
  F.f = FILTERS[p.get('f')] ? p.get('f') : '';
  F.state = STATES.some(([s]) => s === p.get('state')) ? p.get('state') : '';
  F.product = p.get('product') || ''; // a product the read does not name stays a pick option, so it can be lifted
  F.hidden = shell.view.current() === 'hidden';
  if (p.get('q')) { input.value = p.get('q'); F.q = p.get('q').trim().toLowerCase(); }
  shell.list.listParams(p, L, { sorts: Object.keys(BY), size: LIST.size });
  put(document.getElementById('rows-head'), shell.list.sortHead(COLS, L)); // drawn before the read, so a failed one keeps the columns
  reading = shell.read({
    source: '/api/designs', what: 'the designs', adopt, clear,
    draw: () => { if (D) { renderAnnunciator(); renderShow(); renderRows(); } attention(); },
    current: () => selected,
    first: () => ranked()[0]?.id ?? null,
    select: id => (ranked().some(r => r.id === id) ? select(id, false) : reconcile()),
    unselect: clearSelection,
  });
  reading.load();
});
// The view switch (shell.js, view block) picks unhidden or hidden rows; the selection follows (shell.reconcile).
document.addEventListener('shell:view', e => { F.hidden = e.detail.id === 'hidden'; update(); });
