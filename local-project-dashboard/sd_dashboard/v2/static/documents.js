// Documents (sd:2114): the design source's products/system/designs/v2/documents.js at 7f50ad4, ported. Each change from the
// reference is marked "build:". The rows are /api/documents (documents_screen.py): the files /documents/<key>/<file> serves,
// never sample data. Pins, hidden documents and tags have no store yet, so those commands are off with the document's reason,
// and nothing on this page writes.
const { html, put, plural } = window.markup;
const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
// build: the page claims nothing for the rail until /api/documents answers; load() sets it from the rows.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'documents not read' };
// build: the reading arrives by fetch, not as data/documents-data.js globals. OBSERVED is its time; ROOTS and DOCS its rows.
let OBSERVED = Date.now(), ROOTS = [], LABEL = {}, DOCS = [], STORE = '', DOC = null;
const KINDS = ['report', 'research', 'design', 'dashboard'];
const FRESH = { today: 'modified today', week: '1–7 days', older: 'older than 7 days', stale: 'render-stale' };
function days(iso) { return Math.floor((Date.parse(new Date(OBSERVED).toISOString().slice(0, 10)) - Date.parse(iso.slice(0, 10))) / 864e5); }
function fresh(d) { const n = days(d.mod); return n <= 0 ? 'today' : n <= 7 ? 'week' : 'older'; }
const kb = b => b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.round(b / 1024).toLocaleString()} KB`;
const rootOf = d => ROOTS.find(x => x.key === d.key);
// build: a root is a checkout's docs/dashboard; the checkout is where its render runs.
const checkoutOf = d => rootOf(d).path.replace(/\/docs\/dashboard$/, '');

// ---------- State ----------
const F = { repo: new Set(), kind: new Set(), fresh: new Set(), text: '', pinned: false, hidden: false };
let sort = { col: 'mod', dir: -1 }, pageNo = 1, size = 50, selected = null, picked = [];

function matches(d) {
  if (d.hidden !== F.hidden) return false;
  if (F.pinned && !d.pinned) return false;
  if (F.repo.size && !F.repo.has(d.key)) return false;
  if (F.kind.size && !F.kind.has(d.kind)) return false;
  if (F.fresh.size && !([...F.fresh].some(f => f === 'stale' ? d.stale : fresh(d) === f))) return false;
  if (F.text) { const hay = `${d.title} ${d.h1} ${d.desc} ${d.file} ${d.tags.join(' ')}`.toLowerCase(); if (!F.text.split(/\s+/).every(w => hay.includes(w))) return false; }
  return true;
}
const cmp = { title: d => d.title.toLowerCase(), repo: d => (LABEL[d.key] || d.key).toLowerCase(), mod: d => d.mod, size: d => d.bytes };
function sorted(list) {
  const k = cmp[sort.col];
  return [...list].sort((a, b) => (b.pinned - a.pinned) || (k(a) < k(b) ? -1 : k(a) > k(b) ? 1 : 0) * sort.dir);
}

// ---------- Facets ----------
const facets = document.getElementById('facets');
function facetCount(field, value) {
  const save = F[field]; F[field] = new Set([value]);
  const n = DOCS.filter(matches).length; F[field] = save; return n;
}
// build: no reader tells a dashboard or a design document yet; kind is research (a checkout with research.conf.py) or report.
const KIND_OFF = k => (k === 'design' || k === 'dashboard') && !DOCS.some(d => d.kind === k) ? `No ${k} documents: kind is derived as research (a checkout with research.conf.py) or report until the document index stores it.` : '';
function renderFacets() {
  let offs = 0;
  const chip = (field, v, label, extra = '') => {
    const on = F[field].has(v), n = facetCount(field, v);
    const root = field === 'repo' ? ROOTS.find(r => r.key === v) : null;
    const why = field === 'kind' ? KIND_OFF(v) : '';
    const off = why || (root && root.n === 0 ? `Nothing generated yet in ${root.path}` : '');
    // build: aria-disabled, not disabled: a disabled button leaves the tab order, and its reason with it. The reason is
    // an element the chip describes itself by, shown on focus; a title alone is a hover tooltip.
    const id = off ? `facet-off-${++offs}` : '';
    const dis = off ? html` aria-disabled="true" aria-describedby="${id}" title="${off}"` : '';
    const chipEl = html`<button class="chip" type="button" data-f="${field}" data-v="${v}" aria-pressed="${String(on)}"${dis}>${extra}${label} <span class="n">${n}</span></button>`;
    return off ? html`${chipEl}<span class="offwhy" id="${id}">${off}</span>` : chipEl;
  };
  put(facets, html`
    <div class="facet"><span class="label">Repo</span><div class="chips">${ROOTS.map(r => chip('repo', r.key, LABEL[r.key] || r.key))}</div></div>
    <div class="facet"><span class="label">Kind <button class="help" type="button" aria-label="Help: Kind" data-help="<b>Derived until the index exists.</b> A checkout with research.conf.py makes research; the rest are reports. The document index (backend work) stores kind per file.">${I('circle-help')}</button></span><div class="chips">${KINDS.map(k => chip('kind', k, k))}</div></div>
    <div class="facet"><span class="label">Freshness</span><div class="chips">${Object.entries(FRESH).map(([k, l]) => chip('fresh', k, l, k === 'stale' ? html`<span class="g-caution" aria-hidden="true">▲</span>` : ''))}</div></div>
    <div class="facet"><span class="label">Show</span><div class="chips">
      <button class="chip" type="button" id="f-pinned" aria-pressed="${String(F.pinned)}">${I('pin')}Pinned only <span class="n">${DOCS.filter(d => d.pinned && !d.hidden).length}</span></button>
      <button class="chip" type="button" id="f-hidden" aria-pressed="${String(F.hidden)}">${I('eye-off')}Hidden <span class="n">${DOCS.filter(d => d.hidden).length}</span></button></div></div>`);
}
facets.addEventListener('click', e => {
  const c = e.target.closest('.chip'); if (!c || c.getAttribute('aria-disabled') === 'true') return;
  if (c.id === 'f-pinned') F.pinned = !F.pinned;
  else if (c.id === 'f-hidden') F.hidden = !F.hidden;
  else { const s = F[c.dataset.f]; s.has(c.dataset.v) ? s.delete(c.dataset.v) : s.add(c.dataset.v); }
  pageNo = 1; render();
});

// ---------- List ----------
const tbody = document.getElementById('rows');
function row(d) {
  // Every state cell carries its glyph and its words; no source means the render state is unknown, and the cell says why.
  const st = d.stale ? ['caution', '▲', 'render-stale: source newer than page'] : d.src ? ['ok', '●', 'rendered after its source'] : ['unknown', '▨', 'unknown: no source Markdown to compare'];
  const g = html`<td class="g g-${st[0]}" title="${st[2]}"><span aria-hidden="true">${st[1]}</span><span class="sr">${st[2]}</span></td>`;
  const meta = ` · ${d.kind} · ${days(d.mod) <= 0 ? 'today' : days(d.mod) + 'd'} · ${kb(d.bytes)}`;
  return html`<tr data-id="${d.id}"${d.hidden ? html` class="hid"` : ''} aria-selected="${String(d.id === selected)}"${picked.includes(d.id) ? html` data-picked` : ''}>
    ${g}
    <td class="what"><button type="button">${d.title}</button>${d.pinned ? html`<span class="pinmark" title="Pinned">${I('pin')}<span class="sr">pinned</span></span>` : ''}${d.tags.length ? html`<span class="tags">${d.tags.map(t => html`<span class="tagc">${t}</span>`)}</span>` : ''}<span class="file">${d.file}</span></td>
    <td class="repo" data-meta="${meta}">${LABEL[d.key] || d.key}</td>
    <td class="kind">${d.kind}</td>
    <td class="mod repo"><time class="rel" datetime="${d.mod}"></time></td>
    <td class="num">${kb(d.bytes)}</td>
    <td class="acts">${shell.commands.rowActions(d.id)}</td></tr>`;
}
// build: no request is filed (document.request is copy only), so the list has no queued request rows.
function render() {
  renderFacets();
  const list = sorted(DOCS.filter(matches));
  const pages = Math.max(1, Math.ceil(list.length / size)); pageNo = Math.min(pageNo, pages);
  const slice = list.slice((pageNo - 1) * size, pageNo * size);
  put(tbody, html`${slice.map(row)}`);
  const empty = document.getElementById('empty');
  empty.hidden = list.length > 0;
  // build: nothing can be hidden until a store keeps it, and the empty Hidden view says so.
  put(empty, html`${F.hidden ? html`Nothing is hidden: ${STORE || 'no document store yet'}.` : html`No document matches these filters. <button class="linkish" type="button" data-clear>Clear filters</button>`}`);
  // Active filter chips
  const act = [];
  F.repo.forEach(v => act.push(['repo', v, `repo:${v}`])); F.kind.forEach(v => act.push(['kind', v, `kind:${v}`]));
  F.fresh.forEach(v => act.push(['fresh', v, v === 'stale' ? 'is:stale' : `age:${v}`]));
  if (F.pinned) act.push(['pinned', 1, 'is:pinned']); if (F.hidden) act.push(['hidden', 1, 'is:hidden']);
  if (F.text) act.push(['text', F.text, `“${F.text}”`]);
  put(document.getElementById('active'), html`<span>${list.length.toLocaleString()} of ${DOCS.filter(d => d.hidden === F.hidden).length} ${F.hidden ? 'hidden' : 'shown'}</span>${
    act.map(([f, v, l]) => html`<button class="chip" type="button" data-rm="${f}" data-v="${v}" aria-label="Remove filter ${l}">${l}${I('x')}</button>`)}${
    act.length ? html`<button class="linkish" type="button" data-clear>Clear all</button>` : ''}`);
  // Pager
  const from = list.length ? (pageNo - 1) * size + 1 : 0, to = Math.min(pageNo * size, list.length);
  put(document.getElementById('pager'), html`<span>${from}–${to} of ${list.length}</span>
    <span class="pages">${Array.from({ length: pages }, (_, i) => html`<button class="chip" type="button" data-page="${i + 1}" aria-pressed="${String(i + 1 === pageNo)}"${i + 1 === pageNo ? html` aria-current="page"` : ''}>${i + 1}</button>`)}</span>
    <span class="sizes"><span class="label">Per page</span>${[25, 50, 100, 200].map(s => html`<button class="chip" type="button" data-size="${s}" aria-pressed="${String(s === size)}">${s}</button>`)}</span>`);
  // A filter that hides the selected row moves the selection to the first shown row, or clears it (design.md, Page contract).
  // No selection counts too: after an empty filter clears it, the next non-empty list selects its first row again.
  shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, select: id => show(id),
    clear: () => { selected = null; put(details, html`<p class="why">No document matches these filters, so nothing is selected.</p>`); } });
  writeURL();
  const stale = DOCS.filter(d => d.stale && !d.hidden).length;
  const nosrc = DOCS.filter(d => !d.src && !d.hidden).length;
  put(document.getElementById('tally'), html`<span class="g-caution">▲ ${stale} render-stale</span><span class="g-unknown">▨ ${nosrc} no source</span><span>${DOCS.filter(d => d.pinned).length} pinned</span>`);
  // build: the subhead names the reading's time from /api/documents, not a fixed observation.
  put(document.getElementById('subhead'), html`${plural(DOCS.length, 'document')} in ${plural(ROOTS.length, 'root')} · read ${DOC ? html`<time class="rel" datetime="${DOC.read}"></time>` : 'not yet'}`);
  document.querySelectorAll('.ledger th[data-sort]').forEach(th => {
    const on = th.dataset.sort === sort.col;
    on ? th.setAttribute('aria-sort', sort.dir < 0 ? 'descending' : 'ascending') : th.removeAttribute('aria-sort');
    th.querySelector('use').setAttribute('href', on ? (sort.dir < 0 ? '#i-arrow-down' : '#i-arrow-up') : '#i-arrow-up-down');
  });
}
document.getElementById('thead').addEventListener('click', e => {
  const th = e.target.closest('th[data-sort]'); if (!th) return;
  sort = sort.col === th.dataset.sort ? { col: sort.col, dir: -sort.dir } : { col: th.dataset.sort, dir: th.dataset.sort === 'mod' || th.dataset.sort === 'size' ? -1 : 1 };
  render();
});
function clearAll() { F.repo.clear(); F.kind.clear(); F.fresh.clear(); F.text = ''; F.pinned = false; F.hidden = false; input.value = ''; renderShift(); render(); }
document.getElementById('main').addEventListener('click', e => {
  if (e.target.closest('[data-clear]')) return clearAll();
  const rm = e.target.closest('[data-rm]');
  if (rm) { const f = rm.dataset.rm; if (f === 'pinned' || f === 'hidden') F[f] = false; else if (f === 'text') { F.text = ''; input.value = ''; } else F[f].delete(rm.dataset.v); render(); return; }
  const pg = e.target.closest('.pager [data-page]'); if (pg) { pageNo = +pg.dataset.page; render(); return; }
  const sz = e.target.closest('[data-size]'); if (sz) { size = +sz.dataset.size; pageNo = 1; render(); return; }
});

tbody.addEventListener('click', e => {
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
});

// ---------- Commands (products/system/commands.md) ----------
// Each row is a document; each tag on the open document is a document tag. Pin, hide, tag and untag are view state.
// build: no document store exists, so those six are off with the store's reason and declare no Undo; the reference kept them in
// page memory, which a reload loses.
const NO_STORE = 'no CLI: view state; no document store yet';
const storeOff = () => STORE || 'no document store yet';
// build: Disable render has no switch to set. SD_SKIP_RENDER skips every render, and a skip|<key>|<file> or skip|<key> line in
// documents.conf hides a document or a root from this list without stopping its render (sd:1904). Both are off with that reason.
const NO_SWITCH = 'no render switch: SD_SKIP_RENDER skips every render';
const DRAFT = { id: 'draft:request', type: 'document request draft', label: 'new document request', title: '', cmd: '' };
// build: copy only. Filing the item and queueing the runner are two writes, and no verb deletes an item, so no Undo is declared.
const REQUEST = { id: 'document.request', on: 'document request draft', label: 'Run', key: 'r', icon: 'plus', risk: 'undo', primary: () => true, executes: false,
  when: o => (o.repoError || (!o.title ? 'type what you need first' : !o.repo ? 'name the repository: add repo:<name>' : true)), cli: o => o.cmd,
  run: () => 'Copy the two lines: the dashboard does not file document requests yet' };
const TAG = { id: 'document.tag', on: 'document', label: 'Tag', key: 't', icon: 'tag', risk: 'undo', bulk: true,
  when: storeOff, cli: () => 'no CLI: tags live in the document store, which does not exist yet' };
function registerCommands() {
  const C = shell.commands;
  C.register(
    // build: copy only; the dashboard does not run a render. The reference's run said "Mockup: nothing ran."
    { id: 'document.render', on: 'document', label: 'Render', key: 'r', icon: 'rotate-ccw', risk: 'safe', executes: false, primary: o => o.stale && rootOf(o).research,
      when: o => rootOf(o).research || 'not a research repo: no source renders this page',
      cli: o => `cd ${shq(checkoutOf(o))} && sd-research-kit render`, run: o => `Copy the line to render ${o.title}; the dashboard does not run it` },
    // build: the document's own address on this dashboard, not a fixed host.
    { id: 'document.open', on: 'document', label: 'Open document', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: o => !(o.stale && rootOf(o).research),
      cli: o => `open ${location.origin}${o.href}`, run: o => { window.open(o.href, '_blank', 'noopener'); return `Opened in a new tab · ${o.title}`; } },
    { id: 'document.pin', on: 'document', label: 'Pin', key: 'p', icon: 'pin', risk: 'undo', when: o => o.pinned ? 'already pinned' : storeOff(), cli: () => NO_STORE },
    { id: 'document.unpin', on: 'document', label: 'Unpin', key: 'p', icon: 'pin-off', risk: 'undo', when: o => !o.pinned ? 'not pinned' : storeOff(), cli: () => NO_STORE },
    { id: 'document.hide', on: 'document', label: 'Hide', key: 'h', icon: 'eye-off', risk: 'undo', bulk: true, when: o => o.hidden ? 'already hidden' : storeOff(), cli: () => NO_STORE },
    { id: 'document.unhide', on: 'document', label: 'Unhide', key: 'h', icon: 'eye', risk: 'undo', bulk: true, when: o => !o.hidden ? 'not hidden' : storeOff(), cli: () => NO_STORE },
    TAG,
    { id: 'document.untag', on: 'document tag', label: 'Untag', key: 'u', icon: 'x', risk: 'undo', when: storeOff,
      cli: () => 'no CLI: tags live in the document store, which does not exist yet' },
    { id: 'document.skip', on: 'document', label: 'Disable render', key: 's', icon: 'ban', risk: 'safe',
      when: () => `${NO_SWITCH}, and skip|<key>|<file> in documents.conf hides a document from this list without stopping its render`,
      cli: o => `no CLI: proposes skip|${o.key}|${o.file} in documents.conf` },
    { id: 'document.skip-repo', on: 'document', label: 'Disable render for repo', key: 'e', icon: 'ban', risk: 'safe',
      when: () => `${NO_SWITCH}, and skip|<key> in documents.conf hides a root from this list without stopping its render`,
      cli: o => `no CLI: proposes skip|${o.key} in documents.conf` },
    REQUEST,
  );
}
document.addEventListener('shell:open', e => { if (DOCS.some(d => d.id === e.detail)) select(e.detail, true); });
document.addEventListener('shell:picked', e => { picked = e.detail; tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', picked.includes(tr.dataset.id))); });

// ---------- Details ----------
const details = document.getElementById('details');
function show(id) {
  const d = DOCS.find(x => x.id === id); if (!d) return;
  selected = id;
  tbody.querySelectorAll('tr').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id));
  const root = rootOf(d);
  put(details, html`<p class="kind">${d.stale ? html`<span class="g-caution" aria-hidden="true">▲</span> render-stale · ` : d.src ? '' : html`<span class="g-unknown" aria-hidden="true">▨</span> render unknown · `}${d.kind} · ${LABEL[d.key] || d.key}</p>
    <h2>${d.title}</h2>
    <div class="preview-doc" aria-label="Preview">
      <p class="ttl">${d.h1 || d.title}</p>${d.h1 ? html`<p class="h1">&lt;title&gt; ${d.title}</p>` : ''}
      ${d.desc ? html`<p>${d.desc}</p>` : html`<p>No stand line in the page.</p>`}
      <p class="src">Read from the file's &lt;h1&gt; and stand line. The document opens whole, as its author rendered it.</p>
    </div>
    <h3>Act</h3><div id="act">${shell.commands.bar(d.id)}</div>
    <p class="why">Disable render has no switch yet: <code>SD_SKIP_RENDER</code> skips every render.</p>
    <h3>Facts</h3>
    <dl><dt>File</dt><dd>${d.file}</dd><dt>Root</dt><dd>${root.path}</dd><dt>Size</dt><dd>${d.bytes.toLocaleString()} bytes</dd>
      <dt>Modified</dt><dd><time class="rel" datetime="${d.mod}"></time></dd><dt>Source</dt><dd>${d.src ? d.src : '— none matched'}</dd>
      <dt>Render</dt><dd>${d.stale ? html`<span class="g-caution">▲</span> source newer than page` : d.src ? html`<span class="g-ok">●</span> rendered after source` : html`<span class="g-unknown">▨</span> unknown: no source Markdown to compare`}</dd>
      <dt>Label</dt><dd>${LABEL[d.key] || d.key}${root.research ? ' · research repo' : ''}</dd></dl>
    <h3>Tags</h3>
    <div class="tagedit" id="tagedit">${d.tags.map(t => { const tid = `tag:${d.id}:${t}`; shell.commands.put({ id: tid, type: 'document tag', label: `${t} on ${d.title}`, doc: d, tag: t });
      return html`<button class="chip" type="button" data-cmd="document.untag" data-obj="${tid}" aria-label="Untag ${t}">${t}${I('x')}</button>`; })}</div>
    <p class="why">No tags: ${storeOff()}.</p>`);
  // build: no tag field, since Tag is off until a store keeps tags; the reason stands where the field was.
  shell.commands.select(d.id);
  shell.setContext(d.file);
  shell.suggest([`Summarise ${d.title} in five lines`, `What changed since the previous ${LABEL[d.key] || d.key} render?`, 'Which documents here have not been opened in a month?']);
  details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap'));
}
function select(id, open) { show(id); writeURL(); if (open) shell.openPane('tab-details'); }
// The URL carries facets, search, sort, page and the row, as on Tasks and Reports, so a filtered list can be bookmarked or sent.
// Lists join with commas; defaults are left out. Unknown values are dropped on read.
const SORTS = Object.keys(cmp), SIZES = [25, 50, 100, 200];
function readURL() {
  const q = new URLSearchParams(location.search), list = k => (q.get(k) || '').split(',').filter(Boolean);
  F.repo = new Set(list('repo').filter(v => ROOTS.some(r => r.key === v)));
  F.kind = new Set(list('kind').filter(v => KINDS.includes(v)));
  F.fresh = new Set(list('fresh').filter(v => v in FRESH));
  F.text = (q.get('q') || '').trim().toLowerCase(); F.pinned = q.get('pinned') === '1'; F.hidden = q.get('hidden') === '1';
  const [col, dir] = (q.get('sort') || '').split('.'); if (SORTS.includes(col)) sort = { col, dir: dir === 'asc' ? 1 : -1 };
  if (SIZES.includes(+q.get('size'))) size = +q.get('size');
  // A page is decimal digits: ?page=1.5 sliced mid-page and pressed no pager button, and Number('0x2') is 2 (sd:2427).
  const page = q.get('page') || ''; if (/^[1-9]\d*$/.test(page) && Number.isSafeInteger(+page)) pageNo = +page;
  if (F.text) input.value = F.text;
}
function writeURL() {
  const q = {};
  ['repo', 'kind', 'fresh'].forEach(k => { if (F[k].size) q[k] = [...F[k]].join(','); });
  if (F.text) q.q = F.text; if (F.pinned) q.pinned = '1'; if (F.hidden) q.hidden = '1';
  if (sort.col !== 'mod' || sort.dir !== -1) q.sort = `${sort.col}.${sort.dir < 0 ? 'desc' : 'asc'}`;
  if (size !== 50) q.size = size; if (pageNo > 1) q.page = pageNo;
  shell.url(q); // the shell keeps ?row=
}
// build: no proposal card. The reference drafted a documents.conf line in chat; neither line it proposed stops a render.

// ---------- Shapeshift: search + filters, or a request ----------
const input = document.getElementById('shift'), prev = document.getElementById('shift-preview'), as = document.getElementById('shift-as'), ghost = document.getElementById('ghost');
const chipsEl = document.getElementById('shift-chips'), reqEl = document.getElementById('req');
let mode = 'search', lastGuess = 'search', streak = 0;
// build: repo: names a root by its key or label exactly, else by a prefix only one key has. The design took the first
// prefix match, and an unknown name fell back to the selected row's repository: a request for the wrong repository.
function repoOf(name) {
  const n = name.toLowerCase(), keys = ROOTS.map(r => r.key);
  const exact = keys.find(k => k.toLowerCase() === n || (LABEL[k] || '').toLowerCase() === n);
  if (exact) return { key: exact };
  const some = keys.filter(k => k.toLowerCase().startsWith(n));
  if (some.length === 1) return { key: some[0] };
  return { why: some.length ? `repo:${name} matches ${some.join(', ')}: name one` : `repo:${name} names no document root` };
}
function parse(v) {
  const out = { repo: [], kind: [], fresh: [], pinned: false, hidden: false, words: [], due: '', skill: '', repoError: '' };
  v.split(/\s+/).filter(Boolean).forEach(t => {
    let m;
    if ((m = t.match(/^repo:(.+)$/i))) { const k = repoOf(m[1]); if (k.key) out.repo.push(k.key); else if (mode === 'request') out.repoError ||= k.why; else out.words.push(t); }
    else if ((m = t.match(/^kind:(\w+)$/i)) && KINDS.includes(m[1].toLowerCase())) out.kind.push(m[1].toLowerCase());
    else if (/^(stale|is:stale)$/i.test(t)) out.fresh.push('stale');
    else if ((m = t.match(/^age[:>](today|week|older|\d+d)$/i))) out.fresh.push(/^\d+d$/.test(m[1]) ? (parseInt(m[1]) > 7 ? 'older' : 'week') : m[1].toLowerCase());
    else if (/^is:pinned$/i.test(t)) out.pinned = true;
    else if (/^is:hidden$/i.test(t)) out.hidden = true;
    else if ((m = t.match(/^due:(\S+)$/i))) out.due = m[1];
    // build: the hint says to name a skill with skill:, so a request reads it; search leaves it a word.
    else if ((m = t.match(/^skill:([\w.-]+)$/i)) && mode === 'request') out.skill = m[1];
    else if (KINDS.includes(t.toLowerCase()) && out.kind.length === 0 && mode === 'request') out.kind.push(t.toLowerCase());
    else out.words.push(t);
  });
  return out;
}
function guess(v) { return /^(request|need|write|generate|make)\b/i.test(v) ? 'request' : 'search'; }
const SKILL = { research: 'sd-research-repo', design: 'hallmark', report: 'choose', dashboard: 'choose' };
function renderShift() {
  const v = input.value.trim();
  prev.hidden = !v;
  prev.querySelectorAll('[data-as]').forEach(c => c.setAttribute('aria-pressed', c.dataset.as === mode));
  ghost.textContent = v ? `→ ${mode}` : '';
  const p = parse(v);
  if (mode === 'search') {
    put(as, html`${I('filter')} ${p.words.length ? `search “${p.words.join(' ')}”` : 'filter the list'}`);
    put(chipsEl, html`${[...p.repo.map(r => `repo:${r}`), ...p.kind.map(k => `kind:${k}`), ...p.fresh.map(f => f === 'stale' ? 'is:stale' : `age:${f}`), p.pinned ? 'is:pinned' : '', p.hidden ? 'is:hidden' : ''].filter(Boolean).map(c => html`<span class="chip" aria-pressed="true">${c}</span>`)}`);
    reqEl.hidden = true;
    F.text = p.words.join(' ').toLowerCase();
    F.repo = new Set(p.repo); F.kind = new Set(p.kind); F.fresh = new Set(p.fresh); F.pinned = p.pinned; F.hidden = p.hidden;
    pageNo = 1; render();
  } else {
    const title = p.words.filter((w, i) => !(i === 0 && /^(request|need|write|generate|make)$/i.test(w))).join(' ').replace(/^(a|an)\s+/i, '');
    // A write names where it runs: repo:, else the row selected when typing began, never a silent default.
    if (p.repoError) {
      put(as, html`${I('plus')} request “${title || '…'}”`);
      put(chipsEl, html`<span class="chip" aria-pressed="false">repo: none</span>`); reqEl.hidden = false;
      Object.assign(DRAFT, { title, repo: null, cmd: '', repoError: p.repoError });
      put(reqEl, html`<p class="why">${p.repoError}. The request goes nowhere until <code>repo:</code> names one root.</p>`);
      F.text = ''; render(); return;
    }
    DRAFT.repoError = '';
    const repo = p.repo[0] || reqScope;
    if (!repo) {
      put(as, html`${I('plus')} request “${title || '…'}”`);
      put(chipsEl, html`<span class="chip" aria-pressed="false">repo: none</span>`); reqEl.hidden = false;
      Object.assign(DRAFT, { title, repo: null, cmd: '' });
      put(reqEl, html`<p class="why">No document was selected when you started typing. Add <code>repo:&lt;name&gt;</code> to say which repository gets the request.</p>`);
      F.text = ''; render(); return;
    }
    const kind = p.kind[0] || (ROOTS.find(r => r.key === repo)?.research ? 'research' : 'report');
    const skill = p.skill || SKILL[kind];
    put(as, html`${I('plus')} request “${title || '…'}”`);
    put(chipsEl, html`${[`repo:${repo}`, `kind:${kind}`, `skill:${skill}`, 'budget:30m', p.due ? `due:${p.due}` : ''].filter(Boolean).map(c => html`<span class="chip" aria-pressed="true">${c}</span>`)}`);
    reqEl.hidden = false;
    Object.assign(DRAFT, { title, repo, kind, skill, due: p.due,
      // build: the run gets the id the add printed (ITEM, set here); `<item>` would be a redirect in a shell.
      cmd: `ITEM=$(sd task add ${shq('Document request: ' + title)} --body ${shq(`repo=${repo} kind=${kind} skill=${skill}`)}${p.due ? ` --due ${shq(p.due)}` : ''} --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["item"]["id"])') &&\nsd run --sequential --role author --scope ${shq(repo)} --budget-minutes 30 "$ITEM"` });
    // build: the line is to copy; nothing is filed or queued from here.
    put(reqEl, html`<p class="why">Copy the lines: they create an item, then queue one runner assignment in ${repo}. ${skill === 'choose' ? 'No skill matches this kind yet; name one with skill:.' : `The runner uses ${skill}.`} The dashboard does not file them.</p>
      <div class="actions">${shell.commands.rowActions(DRAFT.id)}</div>`);
    F.text = ''; render();
  }
}
// The request's repository is the row selected when typing began. Search may move or clear the selection while the
// viewer types; that must never change where a write goes.
let reqScope = null;
const scopeNow = () => (selected && DOCS.find(d => d.id === selected)?.key) || null;
input.addEventListener('beforeinput', () => { if (!input.value.trim()) reqScope = scopeNow(); });
input.addEventListener('input', () => {
  const g = guess(input.value.trim());
  if (g === lastGuess) streak++; else { lastGuess = g; streak = 1; }
  if (streak >= 2 || !input.value.trim()) mode = g; // change the reading only when it wins twice in a row
  renderShift();
});
input.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); mode = mode === 'search' ? 'request' : 'search'; renderShift(); }
  if (e.key === 'Enter') { e.preventDefault(); if (mode === 'request') { if (DRAFT.title && DRAFT.repo) shell.commands.run(REQUEST, DRAFT); } else prev.hidden = true; }
  if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; mode = 'search'; renderShift(); }
});
prev.addEventListener('click', e => {
  const c = e.target.closest('[data-as]'); if (c) { mode = c.dataset.as; renderShift(); input.focus(); return; }
});
document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest('.actmenu')) return;
  if (e.key === '/') { e.preventDefault(); input.focus(); }
});
// The shell walks these rows on j / k.
window.PAGE_KEYS = [['/', 'Search documents or request one'], ['→', 'In the field: search or request']];
window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false) };

window.PAGE_COMMANDS = [
  { label: 'Request a document', icon: 'plus', run: () => { reqScope = scopeNow(); input.value = 'request '; mode = 'request'; renderShift(); input.focus(); } },
  { label: 'Show render-stale documents', icon: 'clock', run: () => { clearAll(); F.fresh.add('stale'); render(); } },
  { label: 'Show hidden documents', icon: 'eye-off', run: () => { F.hidden = true; render(); } },
];

// ---------- Start (build: read /api/documents through shell.read, then draw) ----------
// The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a document the read no longer
// lists runs no command, and a failed read clears the rows, the rail and Details.
function attention() {
  const n = DOCS.filter(d => d.stale && !d.hidden).length;
  window.PAGE_ATTENTION = !DOC ? { state: 'unknown', n: 0, what: 'documents not read' }
    : n ? { state: 'caution', n, what: 'render-stale documents' } : { state: 'ok', n: 0, what: 'render-stale documents' };
  window.shell.attention?.(window.PAGE_ATTENTION);
}
const shownPage = () => sorted(DOCS.filter(matches)).slice((pageNo - 1) * size, pageNo * size);
let reading = null, linked = false;
function adopt(doc) {
  DOC = doc;
  OBSERVED = Date.parse(DOC.read) || Date.now();
  document.body.dataset.observed = DOC.read; // shell time cells count from the reading, not from now
  ROOTS = DOC.roots || []; LABEL = Object.fromEntries(ROOTS.map(r => [r.key, r.label])); STORE = DOC.store?.available ? '' : (DOC.store?.reason || 'no document store yet');
  DOCS = (DOC.documents || []).map(r => ({ id: `${r.key}/${r.file}`, type: 'document', label: r.title || r.file, key: r.key, file: r.file, href: r.href,
    bytes: r.bytes, mod: r.modified, kind: r.kind, src: r.src, stale: !!r.stale, title: r.title || r.file, h1: r.h1, desc: r.desc, tags: [], pinned: false, hidden: false }));
  if (!linked) {
    // The address is read before the first draw: that draw's reconcile selects the first shown row, and selecting rewrites
    // ?row=, so a link to any other row would be lost. A linked row the filters show moves the list to its page.
    linked = true;
    const q = shell.row();
    readURL();
    const at = sorted(DOCS.filter(matches)).findIndex(d => d.id === q);
    if (at >= 0) { pageNo = Math.floor(at / size) + 1; selected = q; }
  }
  const contested = DOC.contested || [];
  // build: a key two checkouts claim is served by neither (documents.py, contested); the state slot names each one.
  const state = !ROOTS.length && !contested.length ? { kind: 'empty', title: 'No document roots', text: `Publish into ${DOC.published} in a checkout under ${DOC.base}, or add a root| line to ${DOC.config}.`, source: DOC.config }
    : contested.length ? { kind: 'partial', text: `${contested.map(c => `${plural(c.paths.length, 'checkout')} claim the key ${c.key} (${c.paths.join(', ')}), so none is served`).join('; ')}. Name the one you mean with a root| line in ${DOC.config}.`, source: DOC.config }
    : null;
  return { objects: [...DOCS, DRAFT], state };
}
function unselect() {
  selected = null;
  put(details, html`<p class="why">${!DOC ? 'The documents were not read, so nothing is selected.'
    : !DOCS.length ? 'No document is published yet, so nothing is selected.' : 'No document matches these filters. Clear them to see the list.'}</p>`);
}
// Only the newest of overlapping loads draws; nothing on this page writes, so it never rereads.
const load = () => reading.load();
document.addEventListener('DOMContentLoaded', () => {
  registerCommands();
  reading = shell.read({
    source: '/api/documents', what: 'the documents', adopt,
    clear: () => { DOC = null; ROOTS = []; LABEL = {}; DOCS = []; delete document.body.dataset.observed; },
    draw: () => { render(); attention(); },
    current: () => selected,
    first: () => shownPage()[0]?.id,
    select: id => select(id, false),
    unselect,
  });
  load();
});
