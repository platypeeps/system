// Research (sd:2122): the design source's products/system/designs/v2/research.js at 7f50ad4, ported. Each change from the reference is
// marked "build:". The board is /api/research (research_screen.py: collectors.collect_research, every checkout with a research.conf.py);
// a project's ledger and claim map are /api/research/<checkout>, read when the project is selected. Never sample data. Nothing reads
// review rounds yet, so they are unknown with the document's reason. Render and Review are copy only; Start research is off.
const { html, put, plural } = window.markup;
const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
// build: a path under the home folder keeps its ~ outside the quotes, so the shell still expands it.
const shPath = p => (p.startsWith('~/') ? `~/${shq(p.slice(2))}` : shq(p));
const GLYPH = { caution: '▲', unknown: '▨', ok: '●', queued: '◌', warning: '■' };
let DIRS = ['00-overview', '10-sources', '20-map', '30-brief', '40-docs'];
let CAP = 2;
// build: the reference's project list, data/research-sources.js and claims were sample data. These fill from the two documents.
let PROJECTS = [], ROUNDS = '', READ = null, CUT = 0, LOADED = false;
const SOURCES = new Map(); // a Map, so a checkout named like an object property (constructor) is not already cached
const RANK = { caution: 0, unknown: 1, queued: 2, ok: 3 };
// Page attention, the read's own (sd:3446, research_screen._attention): render-stale projects.
// build: nothing is claimed until /api/research answers.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'research not read yet' };
let ATTENTION = null;
function attention() {
  window.shell?.attention?.(LOADED && ATTENTION || { state: 'unknown', n: 0, what: 'research not read' });
}
// The page's views (sd:5064), drawn by the shell's view switch: the board, then the selected project's ledger and claim map.
// The selection carries across them; a claim opens Sources with its sources lit, and a source's ◆ count opens Claim map.
window.PAGE_VIEWS = [{ id: 'projects', label: 'Projects' }, { id: 'sources', label: 'Sources' }, { id: 'claims', label: 'Claim map' }];
// With no project selected, Sources and Claim map send the viewer back to the board.
const PICK = () => html`<p class="why">No project is selected. <button class="linkbtn" type="button" data-view="projects">Pick one in Projects</button>.</p>`;
// The tally filters the board by state; the URL carries it (?state=), and the tally's Clear or a second tap removes it.
const STATE_WORDS = { caution: 'render-stale', unknown: 'config or render unread', ok: 'current' };
let stateFilter = null;

function stageCell(p) {
  if (!p.dirs) return html`<span class="track" aria-hidden="true">${DIRS.map(() => html`<i class="hatch"></i>`)}</span><span class="stage-lbl"><span class="g-unknown">▨</span> no numbered layout</span>`;
  const last = DIRS.filter(d => p.dirs.includes(d)).pop();
  // build: a numbered layout with no Markdown yet says so; the reference always had a last directory.
  return html`<span class="track" role="img" aria-label="Populated: ${p.dirs.join(', ') || 'none'}">${DIRS.map(d => html`<i class="${p.dirs.includes(d) ? 'on' : ''}" title="${d}"></i>`)}</span><span class="stage-lbl">${last ? last.slice(3) : 'no Markdown yet'}</span>`;
}
// build: no round reader, so every project's rounds are unknown with the document's reason.
function roundsCell() {
  return html`<span class="d" title="${ROUNDS}"><span class="g-unknown">▨</span> not recorded</span>`;
}
function gitSub(g) {
  return g ? `${g.branch || '?'}${g.dirty ? ` · ${g.dirty} dirty` : ''}${g.behind ? ` · ${g.behind} behind` : ''}` : 'not a git checkout';
}
const tbody = document.getElementById('rows');
let selected = null, filterText = '', picked = [];
// The header sorts the board (sd:3455, the shell's list grammar); State is the default, and a tie keeps the board's order.
// Stage is how far the numbered layout reaches; Rounds has no reader, so it has no order.
const BY = {
  s: p => RANK[p.s], what: p => p.label.toLowerCase(), stage: p => (p.dirs ? DIRS.findLastIndex(d => p.dirs.includes(d)) : -2),
  render: p => RANK[p.render.s] ?? 9, last: p => Date.parse(p.git?.last_iso) || 0,
};
const LIST = { sort: 's', dir: 1, size: 50 }, L = { ...LIST, page: 1 };
const COLS = [['s', html`<span class="sr">State</span>`, 'g'], ['what', 'Project'], ['stage', 'Stage'], ['render', 'Render'], [null, 'Rounds / cap'],
  ['last', 'Last commit', 'last'], [null, html`<span class="sr">Actions</span>`, 'acts']];
const order = (a, b) => { const x = BY[L.sort](a), y = BY[L.sort](b); return (x < y ? -1 : x > y ? 1 : 0) * L.dir; };
function visible() {
  return PROJECTS.filter(p => (!filterText || `${p.label} ${p.key} ${p.path}`.toLowerCase().includes(filterText)) && (!stateFilter || p.s === stateFilter)).sort(order);
}
function renderBoard() {
  const list = visible();
  put(document.getElementById('rows-head'), shell.list.sortHead(COLS, L));
  // A state band starts only while the board is in state order.
  put(tbody, html`${list.map((p, i) => html`<tr data-id="${p.key}"${i && L.sort === 's' && list[i - 1].s !== p.s ? html` class="band-start"` : ''} aria-selected="${String(p.key === selected)}"${picked.includes(p.key) ? html` data-picked` : ''}>
      <td class="g g-${p.s}" title="${p.s}: ${STATE_WORDS[p.s]}"><span aria-hidden="true">${GLYPH[p.s]}</span><span class="sr">${p.s}: ${STATE_WORDS[p.s]}</span></td>
      <td class="what"><button type="button">${p.label}</button><span class="path">${p.path}</span></td>
      <td class="stage" data-l="Stage">${stageCell(p)}</td>
      <td class="render" data-l="Render"><span class="d"><span class="g-${p.render.s}">${GLYPH[p.render.s]}</span> ${p.render.txt}<span class="sub">${p.render.sub}</span></span></td>
      <td class="rnd" data-l="Rounds">${roundsCell(p)}</td>
      <td class="last"><span class="d">${p.git?.last_iso ? html`<time class="rel" datetime="${p.git.last_iso}"></time>` : 'no commit read'}<span class="sub">${gitSub(p.git)}</span></span></td>
      <td class="acts">${shell.commands.rowActions(p.key)}</td></tr>`)}`);
  document.getElementById('empty').hidden = !!list.length || !PROJECTS.length;
  const n = s => PROJECTS.filter(p => p.s === s).length;
  const states = ['caution', 'unknown', 'ok'];
  put(document.getElementById('tally'), html`${states.map(s => html`<button class="chip" type="button" data-state="${s}" aria-pressed="${String(stateFilter === s)}"${n(s) || stateFilter === s ? '' : html` disabled`} aria-label="${stateFilter === s ? 'Showing' : 'Show'} ${n(s)} ${STATE_WORDS[s]}"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> ${n(s)} ${STATE_WORDS[s]}</button>`)}${
    stateFilter ? html`<button class="linkbtn" type="button" data-clear-state>Clear</button>` : ''}`);
}
function setState(s) {
  stateFilter = s && Object.hasOwn(STATE_WORDS, s) ? s : null;
  renderBoard(); reconcile();
}
// Keep the selection on a row the viewer can see: the first shown row, or none (Details says so).
// The state chips and the text filter both call it, so a selection cleared by one comes back when rows return.
function reconcile() {
  shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, select: id => select(id, false),
    clear: () => { clearSelection(); put(details, html`<p class="why">No project matches the filter. Clear the search or the state filter to see the board.</p>`); } });
  writeURL();
}
// With no project selected, the reader and the claims must not keep the last project's rows: they would open its sources.
function clearSelection() {
  selected = null; curSource = null; resetClaims(); shell.commands.select(null);
  ['reader-for', 'reader-tally', 'claims-for'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = ''; });
  put(reader, PICK()); put(claimsEl, PICK());
  shell.view.count('sources', null); shell.view.count('claims', null);
}
function writeURL() {
  const q = new URLSearchParams(); if (stateFilter) q.set('state', stateFilter); if (filterText) q.set('q', filterText);
  shell.url(shell.list.listQuery(q, L, LIST)); // the shell keeps ?row=
}
document.getElementById('rows-head').addEventListener('click', e => { if (shell.list.sortBy(e, L)) { shell.list.keepFocus(e, renderBoard); writeURL(); } });
document.getElementById('tally').addEventListener('click', e => {
  if (e.target.closest('[data-clear-state]')) { setState(null); document.querySelector('#tally button')?.focus(); return; }
  const b = e.target.closest('button[data-state]'); if (!b || b.disabled) return;
  const s = b.dataset.state; setState(stateFilter === s ? null : s);
  document.querySelector(`#tally button[data-state="${s}"]`)?.focus();
});

// ---------- Data (build) ----------
// The board reads through shell.read (the Start block); the ledger is read here, per project.
// build: the ledger is read per project, when it is selected, and kept for the page's life.
const sourcesPath = key => `/api/research/${key.split('/').map(encodeURIComponent).join('/')}`;
function loadSources(key) {
  if (!SOURCES.has(key)) SOURCES.set(key, window.shell.getJSON(sourcesPath(key)).then(s => (SOURCES.set(key, s), s), err => { const s = { error: err.message, files: [], rows: [], total: 0 }; SOURCES.set(key, s); return s; }));
  return Promise.resolve(SOURCES.get(key));
}

// ---------- Source reader ----------
const reader = document.getElementById('reader');
let curSource = null;
async function readerFor(key) {
  const p = PROJECTS.find(x => x.key === key);
  document.getElementById('reader-for').textContent = p ? `· ${p.label}` : '';
  document.getElementById('reader-tally').textContent = '';
  if (!p) return;
  if (!(SOURCES.has(key) && !SOURCES.get(key).then)) put(reader, html`<p class="why">Reading the ledger in ${p.path}…</p>`);
  const s = await loadSources(key);
  if (selected !== key) return; // a newer selection owns the reader
  drawReader(p, s);
}
function drawReader(p, s) {
  const key = p.key;
  if (s.error) {
    // An error that already ends its sentence takes no second period (sd:2429).
    put(reader, html`<p class="unknown"><span class="g-unknown">▨</span>The ledger was not read: ${s.error.replace(/\.$/, '')}. Reselect the project to retry.</p>`);
    SOURCES.delete(key); document.getElementById('reader-tally').textContent = 'not read'; shell.view.count('sources', null); return;
  }
  if (!s.files.length) {
    put(reader, html`<p class="unknown"><span class="g-unknown">▨</span>No source registry in ${p.path}: neither SOURCES.md nor 10-sources/registry.md exists. The standard expects one; the reader cannot show provenance without it.</p>`);
    document.getElementById('reader-tally').textContent = 'registry missing'; shell.view.count('sources', 0); return;
  }
  // A row the claim map cites carries its count (◆ n); tapping it lights those claims. A lit row is a tapped claim's source.
  let lastSec = null;
  put(reader, html`${s.files.map(f => html`<p class="prov"><b>${f.file}</b> · modified ${f.mtime.replace('T', ' ')}${f.error ? ` · not read: ${f.error}` : f.prov ? `\n${f.prov}` : ''}</p>`)}${
    s.rows.length ? html`<ul class="srcs">${s.rows.map((r, i) => {
      const head = r.sec !== lastSec ? html`<li class="grp label">${r.sec || r.f}</li>` : ''; lastSec = r.sec;
      return html`${head}<li class="src" data-i="${i}" aria-current="${String(curSource === i)}"${r.cited ? html` data-cited` : ''}${r.key && litKeys.includes(r.key) ? html` data-lit` : ''}>
        <button type="button"><span class="id" data-grade="${r.id}">${r.id}</span><span class="t">${r.title}<span class="u">${r.c3}</span></span></button>
        ${r.cited ? html`<button class="cites" type="button" data-cites="${i}" aria-label="Show the ${plural(r.cited, 'claim')} citing ${r.title}" title="Claims citing it">◆ ${r.cited}</button>` : html`<span></span>`}
        ${r.url ? html`<a class="icon-btn" href="${r.url}" target="_blank" rel="noopener" aria-label="Open source: ${r.title}" title="Open source">${I('arrow-up-right')}</a>` : html`<span></span>`}</li>`;
    })}</ul>` : html`<p class="why">No table rows in the ledger: the reader reads Markdown tables.</p>`}${
    // build: the reader does not page; the document carries the first rows and the whole count.
    s.total > s.rows.length ? html`<p class="more">${s.rows.length} of ${s.total} rows shown. Open the ledger in the checkout for the rest.</p>` : ''}`);
  const grades = {}; s.rows.forEach(r => { if (/^[ABCUM]$/.test(r.id)) grades[r.id] = (grades[r.id] || 0) + 1; });
  put(document.getElementById('reader-tally'), html`${['A', 'B', 'C', 'U', 'M'].filter(g => grades[g]).map(g => html`<span>${g} ${grades[g]}</span>`)}<span>${plural(s.total, 'row')}</span>`);
  shell.view.count('sources', s.total);
}
reader.addEventListener('click', e => {
  const to = e.target.closest('[data-view]'); if (to) { shell.view.show(to.dataset.view); return; }
  const cites = e.target.closest('[data-cites]'); if (cites) { claimsOf(+cites.dataset.cites); return; }
  const li = e.target.closest('.src'); if (!li || e.target.closest('a')) return;
  selectSource(+li.dataset.i, true);
});

// ---------- Claim map ----------
// Read with the ledger (research_screen.claims, the sd:5064 ruling): each sentence in the map, brief and published documents that
// links a ledger row's URL. Supported cites an A, B or M row; unverified cites only C, U or ungraded rows.
// build: the reference read claims by hand from one project's Status section, with inferred and corrected states; neither is read.
const claimsEl = document.getElementById('claims');
const VERDICT = { supported: ['●', 'ok'], unverified: ['▨', 'unknown'] };
let verdict = null, curClaim = null, litKeys = [], litClaims = [];
function resetClaims() { verdict = null; curClaim = null; litKeys = []; litClaims = []; }
async function renderClaims(key) {
  const p = PROJECTS.find(x => x.key === key);
  document.getElementById('claims-for').textContent = p ? `· ${p.label}` : '';
  if (!p) { put(claimsEl, PICK()); return; }
  if (!(SOURCES.has(key) && !SOURCES.get(key).then)) put(claimsEl, html`<p class="why">Reading the claims in ${p.path}…</p>`);
  const s = await loadSources(key);
  if (selected !== key) return; // a newer selection owns the map
  drawClaims(p, s);
}
// A ledger row's short name on a claim: its grade or ID and the start of its title.
function chip(s, key) {
  const r = s.rows.find(x => x.key === key);
  return r ? html`<span class="chip"><b>${r.id}</b> ${r.title.length > 40 ? `${r.title.slice(0, 39)}…` : r.title}</span>` : html`<span class="chip">a source past the rows shown</span>`;
}
function drawClaims(p, s) {
  const c = s.claims;
  if (s.error || !c) {
    put(claimsEl, html`<p class="unknown"><span class="g-unknown">▨</span>Claims not read for ${p.label}: ${s.error ? s.error.replace(/\.$/, '') : 'the reader sent no claim map'}. Reselect the project to retry.</p>`);
    shell.view.count('claims', null); return;
  }
  // A document the reader skipped is named, so an empty map is never mistaken for a read one.
  const unread = html`${c.unread.map(u => html`<p class="unknown"><span class="g-unknown">▨</span>${u.doc} not read: ${u.error}.</p>`)}`;
  shell.view.count('claims', c.total);
  if (!c.total) {
    put(claimsEl, html`<p class="why">No claims: no sentence in ${c.dirs.join(', ')} links a URL in the ledger. ${plural(c.read, 'document')} read.</p>${unread}`);
    return;
  }
  put(claimsEl, html`<div class="vbar" role="group" aria-label="Show claims by verdict">${['supported', 'unverified'].filter(v => c.tally[v]).map(v => html`<button type="button" class="v-${v}" data-verdict="${v}" aria-pressed="${String(verdict === v)}">${VERDICT[v][0]} ${c.tally[v]} ${v}</button>`)}</div>
    <p class="vlegend"><span>● supported: cites an A, B or M source</span><span>▨ unverified: cites only C, U or ungraded sources</span></p>
    <ul class="claims">${c.rows.map((x, i) => html`<li class="claim" data-c="${i}" aria-current="${String(curClaim === i)}"${litClaims.includes(i) ? html` data-lit` : ''}${verdict && x.s !== verdict ? html` hidden` : ''}>
      <span class="g g-${VERDICT[x.s][1]}" title="${x.s}"><span aria-hidden="true">${VERDICT[x.s][0]}</span><span class="sr">${x.s}</span></span>
      <button type="button">${x.text}</button>
      <span class="meta"><span>${x.doc}:${x.line}</span>${x.keys.map(k => chip(s, k))}</span></li>`)}</ul>${
    c.total > c.rows.length ? html`<p class="more">${c.rows.length} of ${c.total} claims shown: the reading passed its print bound.</p>` : ''}${unread}`);
  document.getElementById('claims-for').textContent = `· ${p.label} · ${plural(c.total, 'claim')}`;
}
claimsEl.addEventListener('click', e => {
  const to = e.target.closest('[data-view]'); if (to) { shell.view.show(to.dataset.view); return; }
  const v = e.target.closest('[data-verdict]');
  if (v) { verdict = verdict === v.dataset.verdict ? null : v.dataset.verdict; redraw(); claimsEl.querySelector(`[data-verdict="${v.dataset.verdict}"]`)?.focus(); return; }
  const li = e.target.closest('.claim[data-c]'); if (li && e.target.closest('button')) selectClaim(+li.dataset.c);
});
// Draw the reader and the map again from the cache, keeping what is lit.
function redraw() {
  const p = PROJECTS.find(x => x.key === selected), s = SOURCES.get(selected);
  if (!p || !s || s.then) return;
  drawReader(p, s); drawClaims(p, s);
}
// A tapped claim opens Sources with its sources lit, and its Details stay beside them.
function selectClaim(i) {
  const s = SOURCES.get(selected), x = s?.claims?.rows?.[i]; if (!x) return;
  curClaim = i; litKeys = x.keys; litClaims = [];
  redraw(); showClaim(s, x);
  shell.view.show('sources'); reader.querySelector('[data-lit]')?.scrollIntoView?.({ block: 'nearest' });
}
// A source's ◆ count opens Claim map with the claims that cite it lit; a verdict filter that would hide one is cleared.
function claimsOf(i) {
  const s = SOURCES.get(selected), r = s?.rows?.[i]; if (!r?.key) return;
  litClaims = s.claims.rows.flatMap((x, n) => (x.keys.includes(r.key) ? [n] : []));
  litKeys = []; curClaim = null; verdict = null;
  redraw();
  shell.view.show('claims'); claimsEl.querySelector('[data-lit]')?.scrollIntoView?.({ block: 'nearest' });
}

// ---------- Details ----------
const details = document.getElementById('details');
function swap() { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); }
const kindWord = s => (s === 'unknown' ? 'config or render unread' : s === 'caution' ? 'render-stale' : 'current');
function showProject(key) {
  const p = PROJECTS.find(x => x.key === key); if (!p) return;
  const notFresh = p.docs.filter(d => d.state !== 'fresh');
  put(details, html`<p class="kind"><span class="g-${p.s}" aria-hidden="true">${GLYPH[p.s]}</span> Research project · ${kindWord(p.s)}</p>
    <h2>${p.label}</h2>
    <dl><dt>Checkout</dt><dd>${p.path}</dd><dt>Branch</dt><dd>${p.git ? `${p.git.branch || '?'}${p.git.dirty ? ` · ${p.git.dirty} dirty` : ' · clean'}${p.git.behind ? ` · ${p.git.behind} behind` : ''}` : 'not a git checkout'}</dd>
      <dt>Last commit</dt><dd>${p.git?.last_iso ? html`<time class="rel" datetime="${p.git.last_iso}"></time> · ${p.git.subject}` : 'not read'}</dd>
      <dt>Stage</dt><dd>${p.dirs ? (p.dirs.join(' · ') || 'numbered layout, no Markdown yet') : 'no numbered layout'}</dd>
      <dt>Render</dt><dd>${p.render.txt}${p.render.sub ? ` · ${p.render.sub}` : ''}</dd>
      <dt>Documents</dt><dd>${plural(p.docs_total ?? p.docs.length, 'document')} in research.conf.py${p.docs.length < (p.docs_total ?? 0) ? ` · ${p.docs.length} listed: the board passed its print bound` : ''} · <a href="/documents">Documents</a></dd>
      <dt>Rounds</dt><dd>${ROUNDS}</dd></dl>
    ${p.conf ? html`<p class="unknown"><span class="g-unknown">▨</span>The dashboard collector could not read research.conf.py: “${p.conf}”. Per-document freshness stays unknown until the config declares its data.</p>` : ''}
    ${notFresh.length ? html`<h3>Not fresh</h3><ul class="claims">${notFresh.map(d => html`<li class="claim"><span class="g g-caution" aria-hidden="true">▲</span><span>${d.title} · ${d.state} · ${d.src}</span></li>`)}</ul>` : ''}
    <h3>Act</h3>${shell.commands.bar(p.key)}
    <p class="why">Render and the mechanical review run in the checkout; neither writes outside it. The dashboard runs neither: copy the line.</p>`);
  shell.commands.select(p.key);
  // build: the reference's suggestions named one project's open questions; these name the selected project only.
  shell.suggest([`What is ${p.label} waiting on?`, 'Which sources are graded C or U?', 'Summarise the START HERE document']);
  swap();
}
function showSource(i) {
  const s = SOURCES.get(selected)?.rows?.[i]; if (!s) return;
  put(details, html`<p class="kind">${I('book-open')} Source · ${s.h[0] || 'grade'} ${s.id}</p><h2>${s.title}</h2>
    <dl><dt>Ledger</dt><dd>${s.f}</dd><dt>Section</dt><dd>${s.sec || 'none'}</dd><dt>${s.h[2] || 'Used for'}</dt><dd>${s.c3 || '—'}</dd>${s.c4 ? html`<dt>${s.h[3] || 'Note'}</dt><dd>${s.c4}</dd>` : ''}
      ${s.url ? html`<dt>Link</dt><dd><a class="ext" href="${s.url}" target="_blank" rel="noopener">${s.url.replace(/^https?:\/\//, '').slice(0, 60)} ${I('arrow-up-right')}</a></dd>` : ''}</dl>
    <h3>Cited by</h3>${citedBy(s)}`);
  shell.setContext(s.title.slice(0, 40)); swap();
}
function citedBy(r) {
  if (!r.key) return html`<p class="why">No link in this ledger row, so no claim can cite it.</p>`;
  const map = SOURCES.get(selected)?.claims;
  if (!map) return html`<p class="why">Not known: the claim map was not read.</p>`;
  const by = map.rows.filter(x => x.keys.includes(r.key));
  if (!r.cited) return html`<p class="why">No sentence in ${map.dirs.join(', ')} links this source.</p>`;
  return html`<ul class="claims">${by.map(x => html`<li class="claim"><span class="g g-${VERDICT[x.s][1]}" aria-hidden="true">${VERDICT[x.s][0]}</span><span>${x.text} · ${x.doc}:${x.line}</span></li>`)}</ul>${
    r.cited > by.length ? html`<p class="more">${by.length} of ${plural(r.cited, 'claim')} listed: the reading passed its print bound.</p>` : ''}`;
}
function showClaim(s, x) {
  put(details, html`<p class="kind"><span class="g-${VERDICT[x.s][1]}" aria-hidden="true">${VERDICT[x.s][0]}</span> Claim · ${x.s}</p><h2>${x.text}</h2>
    <dl><dt>Document</dt><dd>${x.doc}, line ${x.line}</dd><dt>Verdict</dt><dd>${x.s === 'supported' ? 'cites an A, B or M source' : 'cites only C, U or ungraded sources'}</dd></dl>
    <h3>Sources</h3><ul class="claims">${x.keys.map(k => { const r = s.rows.find(y => y.key === k);
      return html`<li class="claim"><span class="g" aria-hidden="true">${r ? r.id : '?'}</span><span>${r ? r.title : `${k}: past the rows shown`}</span></li>`; })}</ul>`);
  shell.setContext(x.text.slice(0, 40)); swap();
}
function select(key, open) {
  if (!PROJECTS.some(p => p.key === key)) return;
  selected = key; curSource = null; resetClaims();
  tbody.querySelectorAll('tr').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === key));
  readerFor(key); renderClaims(key); showProject(key);
  writeURL();
  if (open) shell.openPane('tab-details');
}
function selectSource(i, open) {
  curSource = i;
  document.querySelectorAll('.src[data-i]').forEach(li => li.setAttribute('aria-current', +li.dataset.i === i));
  showSource(i); if (open) shell.openPane('tab-details');
}
tbody.addEventListener('click', e => { if (e.target.closest('.rowact')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

// ---------- Commands (products/system/commands.md) ----------
// Each board row is a research project. Labels are CLI verbs; the shell renders every button, menu entry and CLI line.
// build: Render and Review are copy only (executes: false): the dashboard runs no command in a research checkout.
const COPY = 'Copy it into a terminal: the dashboard does not run sd-research-kit';
// build: Start research is off with this reason, and its risk is confirm, not undo: there is no queued run to take back.
const START_OFF = 'the dashboard does not create items yet: copy the line into a terminal';
const DRAFT = { id: 'draft:research', type: 'research request', label: 'new research', q: '', cmd: '' };
const START = { id: 'research.start', on: 'research request', label: 'Run', does: 'Gives the line that files this research request; the dashboard files nothing.', key: 'r', icon: 'flask-conical', risk: 'confirm', executes: false, primary: () => true,
  when: o => (!o.q ? 'type the question first' : !o.repo ? 'name the project: add repo:<name>' : START_OFF), cli: o => o.cmd, run: () => START_OFF };
function registerCommands() {
  const C = shell.commands;
  C.put(DRAFT);
  C.register(
    { id: 'research.render', on: 'research project', label: 'Render', does: 'Gives the line that renders this research project again.', key: 'r', icon: 'rotate-ccw', risk: 'safe', bulk: true, executes: false, primary: o => o.s === 'caution',
      cli: o => `cd ${shPath(o.path)} && sd-research-kit render`, run: () => COPY },
    { id: 'research.review', on: 'research project', label: 'Review', does: 'Gives the line that runs the research kit\'s review.', key: 'v', icon: 'shield-check', risk: 'safe', bulk: true, executes: false,
      cli: o => `cd ${shPath(o.path)} && sd-research-kit review`, run: () => COPY },
    START,
  );
}
document.addEventListener('shell:open', e => select(e.detail, true));
document.addEventListener('shell:picked', e => { picked = e.detail; tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', picked.includes(tr.dataset.id))); });

// ---------- Shapeshift: filter projects, or start research ----------
const input = document.getElementById('shift'), prev = document.getElementById('shift-preview'), as = document.getElementById('shift-as'), ghost = document.getElementById('ghost');
const chipsEl = document.getElementById('shift-chips'), reqEl = document.getElementById('req'), hint = document.getElementById('shift-hint');
let mode = 'filter', lastGuess = 'filter', streak = 0;
// A question starts research: it opens with a start word, or a ? is followed only by key:value tags.
// build: the reference's one regex backtracked exponentially on a long tag run; this walks the words once, with the same answers.
const START_WORD = /^(start|research|investigate|how|why|what|which|does|is|can)\b/i;
const tag = w => w.slice(1, -1).includes(':'); // \S+:\S+: a colon with a character on each side
// The rest of the word after its first ? is empty or a tag.
const asked = w => w.endsWith('?') || (w.includes('?') && w.indexOf('?') + 2 <= w.lastIndexOf(':', w.length - 2));
function guess(v) {
  if (START_WORD.test(v)) return 'start';
  const words = v.split(/\s+/);
  let i = words.length;
  while (i && tag(words[i - 1])) i--;
  return words.slice(Math.max(i - 1, 0)).some(asked) ? 'start' : 'filter';
}
// build: a checkout's sd scope is its folder name, the last part of its key.
const scopeOf = key => key.replace(/^new:/, '').split('/').pop();
function parse(v) {
  const o = { repo: '', depth: 'standard', due: '', budget: 60, words: [] };
  v.split(/\s+/).filter(Boolean).forEach(t => {
    let m;
    if ((m = t.match(/^repo:(\S+)$/i))) { const k = m[1].toLowerCase(); o.repo = PROJECTS.find(p => scopeOf(p.key).toLowerCase().startsWith(k) || p.label.toLowerCase() === k)?.key || `new:${k}`; }
    else if ((m = t.match(/^depth:(standard|deep)$/i))) o.depth = m[1].toLowerCase();
    else if (/^deep$/i.test(t)) o.depth = 'deep';
    else if ((m = t.match(/^due:(\S+)$/i))) o.due = m[1];
    else if ((m = t.match(/^budget:(\d+)m?$/i))) o.budget = +m[1];
    else o.words.push(t);
  });
  if (/^start$/i.test(o.words[0] || '')) o.words.shift();
  o.q = o.words.join(' ').replace(/:$/, '');
  return o;
}
function renderShift() {
  const v = input.value.trim();
  prev.hidden = !v;
  prev.querySelectorAll('[data-as]').forEach(c => c.setAttribute('aria-pressed', c.dataset.as === mode));
  ghost.textContent = v ? `→ ${mode}` : '';
  // build: the reference's hint said Enter commits; Start is off, so the hint says what Enter does in each reading.
  put(hint, html`→ switches reading · ${mode === 'filter' ? 'Enter keeps the filter' : 'Enter starts nothing: Start is off'} · Esc clears · Tab moves on`);
  if (mode === 'filter') {
    put(as, html`${I('filter')} filter the board`); chipsEl.replaceChildren(); reqEl.hidden = true;
    filterText = v.toLowerCase(); renderBoard(); reconcile(); writeURL(); return;
  }
  filterText = ''; renderBoard(); reconcile(); // the shown rows changed: a selection an empty filter cleared comes back
  const o = parse(v), repo = o.repo || selected;
  put(as, html`${I('flask-conical')} start research “${o.q || '…'}”`);
  // With no row selected and no repo: token there is no project to research; say so instead of building a command.
  if (!repo) {
    put(chipsEl, html`<span class="chip" aria-pressed="false">repo: none</span>`); reqEl.hidden = false;
    Object.assign(DRAFT, { q: o.q, repo: null, cmd: '' });
    put(reqEl, html`<p class="why">No project is selected. Add <code>repo:&lt;name&gt;</code> to say where the research runs.</p>`);
    return;
  }
  const scope = scopeOf(repo);
  put(chipsEl, html`${[`repo:${scope}`, `depth:${o.depth}`, 'stage:draft', 'skill:sd-research-repo', `budget:${o.budget}m`, o.due ? `due:${o.due}` : ''].filter(Boolean).map(c => html`<span class="chip" aria-pressed="true">${c}</span>`)}`);
  reqEl.hidden = false;
  Object.assign(DRAFT, { q: o.q, repo, depth: o.depth, budget: o.budget, due: o.due,
    cmd: `sd task add ${shq('Research: ' + o.q)} --body ${shq(`repo=${scope} depth=${o.depth} skill=sd-research-repo stage=draft`)}${o.due ? ` --due ${shq(o.due)}` : ''}` });
  // build: Start is off, so the preview shows the line with Copy in place of the Run button.
  const why = START.when(DRAFT);
  put(reqEl, html`<p class="why">Creates an item in ${scope}. Review rounds start at 0 of ${CAP}.</p>
    ${o.q ? html`<div class="cli"><code>${DRAFT.cmd}</code><button class="icon-btn" type="button" data-copy="${DRAFT.cmd}" aria-label="Copy command">${I('copy')}</button></div>` : ''}
    <p class="why">Not started here: ${why}.</p>`);
}
input.addEventListener('input', () => {
  const g = guess(input.value.trim());
  if (g === lastGuess) streak++; else { lastGuess = g; streak = 1; }
  if (streak >= 2 || !input.value.trim()) mode = g;
  renderShift();
});
input.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); mode = mode === 'filter' ? 'start' : 'filter'; renderShift(); }
  // build: Enter in start mode says why nothing starts; shell.commands.run does not read when().
  if (e.key === 'Enter') { e.preventDefault(); if (mode === 'start') { const why = START.when(DRAFT); if (why === true) shell.commands.run(START, DRAFT); else shell.toast(`Not started: ${why}`); } else prev.hidden = true; }
  if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; mode = 'filter'; renderShift(); }
});
prev.addEventListener('click', e => {
  const c = e.target.closest('[data-as]'); if (c) { mode = c.dataset.as; renderShift(); input.focus(); return; }
});
document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest('.actmenu')) return;
  if (e.key === '/') { e.preventDefault(); input.focus(); }
});
// The shell walks these rows on j / k.
window.PAGE_KEYS = [['/', 'Filter research or start a run'], ['→', 'In the field: filter or start']];
window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false) };

window.PAGE_COMMANDS = [
  { label: 'Start research', icon: 'flask-conical', run: () => { input.value = 'start '; mode = 'start'; renderShift(); input.focus(); } },
  { label: 'Show render-stale projects', icon: 'clock', run: () => { input.value = ''; filterText = ''; setState('caution'); shell.openPane('tab-details'); } },
];

// ---------- Start (build: read /api/research through shell.read, then draw) ----------
// The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a project the read no longer
// lists runs no command, and a failed read clears the board, the ledger and the rail. The ledger keeps its own cache.
let board = null, linked = false;
function adopt(doc) {
  READ = doc.read; CAP = doc.cap || CAP; DIRS = doc.dirs || DIRS; CUT = doc.cut || 0; LOADED = true; ATTENTION = doc.attention || null;
  ROUNDS = doc.rounds?.reason || 'not read';
  if (READ) document.body.dataset.observed = READ; // shell time cells count from the reading, not from now
  PROJECTS = (doc.projects || []).map(p => ({ ...p, id: p.key, type: 'research project', s: p.render.s === 'caution' ? 'caution' : p.render.s === 'ok' && !p.conf ? 'ok' : 'unknown' }));
  if (!linked) {
    // The address's filters are read before the first draw, so the board and the first selection follow them.
    linked = true;
    const u = new URLSearchParams(location.search);
    stateFilter = Object.hasOwn(STATE_WORDS, u.get('state') ?? '') ? u.get('state') : null; // own keys only: ?state=constructor is no state
    if (u.get('q')) { input.value = u.get('q'); filterText = u.get('q').toLowerCase(); }
    shell.list.listParams(u, L, { sorts: Object.keys(BY), size: LIST.size });
  }
  const unknown = PROJECTS.filter(p => p.s === 'unknown').length;
  // build: past its print bound the board lists fewer projects (research_screen.board); say how many are missing.
  const cut = CUT ? ` ${plural(CUT, 'project')} not listed: the board passed its print bound.` : '';
  const state = doc.error ? { kind: 'error', text: `The checkouts were not read: ${doc.error}. Reload retries it.`, source: 'collectors.collect_research' }
    : !PROJECTS.length ? { kind: 'empty', title: 'No research checkouts', text: `No checkout under ${doc.root || 'REPO_ROOT'} carries a research.conf.py.`, source: 'collectors.collect_research' }
    : unknown ? { kind: 'partial', text: `${plural(unknown, 'project')} with a config or render the collector could not read. Review rounds are not read: ${ROUNDS}.${cut}`, source: '/api/research' }
    : { kind: 'partial', title: 'No rounds read', text: `${ROUNDS}.${cut}`, source: '/api/research' };
  return { objects: [...PROJECTS, DRAFT], state };
}
function draw() {
  renderBoard();
  put(document.getElementById('sum'), html`${plural(PROJECTS.length, 'project')}${CUT ? ` · ${plural(CUT, 'project')} not listed` : ''} · read ${READ ? html`<time class="rel" datetime="${READ}"></time>` : 'not yet'}`);
  attention();
}
function unselect() {
  clearSelection();
  put(details, html`<p class="why">${!LOADED ? 'The checkouts were not read, so no project is selected.'
    : !PROJECTS.length ? 'No research checkout to select.' : 'No project matches the filter. Clear the search or the state filter to see the board.'}</p>`);
  writeURL();
}
// Only the newest of overlapping loads draws; nothing on this page writes, so it never rereads.
const load = () => board.load();
document.addEventListener('DOMContentLoaded', () => {
  put(document.getElementById('rows-head'), shell.list.sortHead(COLS, L)); // drawn before the read, so a failed one keeps the columns
  registerCommands();
  board = shell.read({
    source: '/api/research', what: 'the research checkouts', adopt, draw,
    clear: () => { PROJECTS = []; READ = null; CUT = 0; LOADED = false; delete document.body.dataset.observed; },
    current: () => selected,
    first: () => visible()[0]?.key,
    // A row the filters hide is not selected: the first shown row is, or none.
    select: id => (visible().some(p => p.key === id) ? select(id, false) : reconcile()),
    unselect,
  });
  load();
});
