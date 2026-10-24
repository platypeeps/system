// Research (sd:2122): the design source's products/system/designs/v2/research.js at 7f50ad4, ported. Each change from the reference is
// marked "build:". The board is /api/research (research_screen.py: collectors.collect_research, every checkout with a research.conf.py);
// a project's ledger and claim map are /api/research/<checkout>, read when the project is selected. Never sample data. Nothing reads
// review rounds yet, so they are unknown with the document's reason. Render runs in the checkout (sd:5632); Review runs the kit's checks (sd:5655); Run files the request (sd:5887).
const { html, put, plural } = window.markup;
const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
// build: a path under the home folder keeps its ~ outside the quotes, so the shell still expands it.
const shPath = p => (p.startsWith('~/') ? `~/${shq(p.slice(2))}` : shq(p));
const GLYPH = { caution: '▲', unknown: '▨', ok: '●', queued: '◌', warning: '■' };
let DIRS = ['00-overview', '05-architecture', '10-sources', '20-map', '30-brief', '40-docs']; // until /api/research names them
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
window.PAGE_VIEWS = [{ id: 'projects', label: 'Projects' }, { id: 'sources', label: 'Sources' }, { id: 'claims', label: 'Claim map' },
  { id: 'experiments', label: 'Experiments' }];
// With no project selected, Sources and Claim map send the viewer back to the board.
const PICK = () => html`<p class="why">No project is selected. <button class="linkbtn" type="button" data-view="projects">Pick one in Projects</button>.</p>`;
// The tally filters the board by state; the URL carries it (?state=), and the tally's Clear or a second tap removes it.
const STATE_WORDS = { caution: 'render-stale', unknown: 'config or render unread', ok: 'current' };
let stateFilter = null;

// build (sd:5657): a populated stage whose page Documents serves is a link to it; /api/research names the address
// (research_screen.linked). Every other pip stays inert, and its title says why it opens nothing.
function pip(p, d, group) {
  const on = p.dirs.includes(d), s = on ? p.stages?.[d] : null;
  if (s?.href) return html`<a class="pip" href="${s.href}" aria-label="Open ${d}: ${s.title}" title="${d}: ${s.title}${s.state === 'stale' ? ', stale since its source changed' : ''}"><i class="on"></i></a>`;
  return html`<span class="pip" title="${d}${s?.why ? `: ${s.why}` : ''}"${group ? html` aria-hidden="true"` : ''}><i class="${on ? 'on' : ''}"></i></span>`;
}
function stageCell(p) {
  if (!p.dirs) return html`<span class="track" aria-hidden="true">${DIRS.map(() => html`<span class="pip"><i class="hatch"></i></span>`)}</span><span class="stage-lbl"><span class="g-unknown">▨</span> no numbered layout</span>`;
  const last = DIRS.filter(d => p.dirs.includes(d)).pop();
  const group = p.dirs.some(d => p.stages?.[d]?.href);
  // build: a numbered layout with no Markdown yet says so; the reference always had a last directory.
  return html`<span class="track" role="${group ? 'group' : 'img'}" aria-label="Populated: ${p.dirs.join(', ') || 'none'}">${DIRS.map(d => pip(p, d, group))}</span><span class="stage-lbl">${last ? last.slice(3) : 'no Markdown yet'}</span>`;
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
  renderExperiments();
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
  selected = null; curSource = null; shownProject = null; resetClaims(); shell.commands.select(null);
  ['reader-for', 'reader-tally', 'claims-for'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = ''; });
  put(reader, PICK()); put(claimsEl, PICK()); drawPickers();
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

// ---------- Experiments (pack sd:5525) ----------
// Each checkout's status brief (research_screen.experiment), read with the board: its State word, Updated day, line, Asks and
// links. Over 7 days since Updated is stale (research_screen.stale). Each row's Build deck posts /api/research/deck, which runs
// sd-research-kit deck in that checkout (research_deck.py), reads the run every 5 s until it settles, then reads the board
// again, so the row links the deck it wrote: /research/deck/<key> downloads docs/dashboard/deck.pptx. Import to Slides
// (#25693, option A) posts /api/research/slides, which imports that deck through workspace-mcp on the work account
// (research_slides.py); the row then links the Slides file. /api/research/slides/status says whether it can.
const xpBody = document.getElementById('xp-rows');
const XP_WORDS = { ok: 'running, decided or done', caution: 'waiting', warning: 'blocked', unknown: 'state not in the standard' };
const DECK_POLL = 5000;
let BUILD = null, BUILD_UNREAD = '', buildTimer = 0;
let SLIDES = null, IMPORTING = '';
// The page's filter field reaches this ledger too, by the same words as the board.
const briefs = () => PROJECTS.filter(p => p.experiment && (!filterText || `${p.label} ${p.key} ${p.path}`.toLowerCase().includes(filterText)));
// A brief's row acts on its research project, as Details do (sd:5747): one object, so the row's icons and Details match.
const deckHref = key => `/research/deck/${key.split('/').map(encodeURIComponent).join('/')}`;
function updatedCell(x) {
  if (!x.updated) return html`<span class="d"><span class="g-unknown">▨</span> no Updated day</span>`;
  return html`<span class="d">${x.updated}${x.stale ? html`<span class="stale"><span class="g-caution" aria-hidden="true">▲</span> stale: over 7 days</span>` : ''}</span>`;
}
function asksCell(x) {
  if (!x.asks.length) return html`<span class="d">none</span>`;
  return html`<ul class="asks">${x.asks.slice(0, 2).map(a => html`<li><b>${a.who}</b>: ${a.what}</li>`)}${x.asks_total > 2 ? html`<li>${x.asks_total - 2} more in the brief</li>` : ''}</ul>`;
}
// The deck: the one built (a download), else whether a deck.json waits, and this row's run while it builds or once it failed.
function deckLine(p) {
  const x = p.experiment, r = BUILD?.repo === p.key ? BUILD : null;
  if (r && !settled(r)) return html`<span class="d"><span class="g-queued" aria-hidden="true">◌</span> Building: run ${r.id} is ${r.state}${r.unread ? `; its state was not read: ${r.unread}` : ''}</span>`;
  const failed = r?.state === 'failed' ? html`<span class="d"><span class="g-warning" aria-hidden="true">■</span> Last build failed: ${r.reason || 'no reason recorded'}</span>` : '';
  if (x.built) return html`${failed}<a href="${deckHref(p.key)}" download>deck.pptx</a><span class="d">built <time class="rel" datetime="${x.built}"></time></span>`;
  return html`${failed}<span class="d">${x.deck ? 'deck.json ●, no deck built yet' : 'no deck.json: Build deck is off'}</span>`;
}
const ext = (url, text) => html`<a class="ext" href="${url}" target="_blank" rel="noopener">${text} ${I('arrow-up-right')}</a>`;
// The Slides file the deck was imported as, and whether the deck changed since.
function slidesLine(p) {
  if (IMPORTING === p.key) return html`<span class="d"><span class="g-queued" aria-hidden="true">◌</span> Importing to Slides</span>`;
  const s = SLIDES?.imports?.[p.key];
  if (!s) return '';
  return html`${ext(s.link, 'Slides')}<span class="d">${s.built === p.experiment.built ? 'current deck' : 'an older deck: import again'}</span>`;
}
function linksCell(p) {
  const x = p.experiment;
  return html`<span class="links">${x.links.map(l => ext(l.url, l.text))}${x.deck_url ? ext(x.deck_url, 'Deck') : ''}${deckLine(p)}${slidesLine(p)}</span>`;
}
// The selected experiment's Details carry its deck line and Act bar (sd:5747), so they redraw with the row.
function renderExperiments() {
  if (shownProject && shownProject === selected && PROJECTS.find(p => p.key === shownProject)?.experiment) showProject(shownProject, false);
  const list = briefs();
  put(xpBody, html`${list.map(p => { const x = p.experiment;
    if (x.error) return html`<tr data-id="${p.key}" aria-selected="${String(p.key === selected)}"><td class="g g-unknown"><span aria-hidden="true">▨</span><span class="sr">unknown</span></td>
      <td class="what" colspan="3"><button type="button">${p.label}</button><span class="line">The status brief was not read: ${x.error.replace(/\.$/, '')}.</span></td>
      <td class="lnk" data-l="Links"><span class="links">${deckLine(p)}</span></td><td class="acts">${shell.commands.rowActions(p.key, { icons: true })}</td></tr>`;
    return html`<tr data-id="${p.key}" aria-selected="${String(p.key === selected)}">
      <td class="g g-${x.tone}" title="${XP_WORDS[x.tone]}"><span aria-hidden="true">${GLYPH[x.tone]}</span><span class="sr">${XP_WORDS[x.tone]}</span></td>
      <td class="what"><button type="button">${p.label}</button><span class="path">${x.state || 'no State row'}</span><span class="line">${x.line}</span></td>
      <td class="upd" data-l="Updated">${updatedCell(x)}</td>
      <td class="ask" data-l="Asks">${asksCell(x)}</td>
      <td class="lnk" data-l="Links">${linksCell(p)}</td>
      <td class="acts">${shell.commands.rowActions(p.key, { icons: true })}</td></tr>`; })}`);
  const stale = list.filter(p => p.experiment.stale).length, asks = list.reduce((n, p) => n + (p.experiment.asks_total || 0), 0);
  put(document.getElementById('xp-tally'), list.length ? html`<span>${plural(list.length, 'brief')}</span><span>${stale} stale</span><span>${plural(asks, 'ask')}</span>${BUILD_UNREAD ? html`<span>last deck build not read: ${BUILD_UNREAD}</span>` : ''}` : html``);
  const empty = document.getElementById('xp-empty');
  empty.hidden = !!list.length || !LOADED;
  put(empty, html`${PROJECTS.some(p => p.experiment) ? 'No brief matches. Clear the filter or type another word.' : 'No checkout keeps a status brief: 30-brief/BRIEF-status.md, in the research standard\'s six sections.'}`);
  shell.view.count('experiments', LOADED ? list.length : null);
}
const settled = r => !r || r.state === 'done' || r.state === 'failed';
function followBuild(run, told) {
  BUILD = run; BUILD_UNREAD = ''; clearTimeout(buildTimer);
  if (settled(run)) {
    if (told && run) shell.toast(run.state === 'done' ? `Deck built · ${run.repo}` : `Build deck failed: ${run.reason || 'no reason recorded'}`);
    // A finished build changed the checkout's deck.pptx: the board reads it again, and the row links the new file.
    if (told && run?.state === 'done') load(); else renderExperiments();
    return;
  }
  renderExperiments();
  // A failed read is not known, not settled: the row says so and the next poll tries again.
  buildTimer = setTimeout(() => window.shell.getJSON(`/api/research/deck/${encodeURIComponent(run.id)}`).then(next => followBuild(next, told),
    e => followBuild({ ...run, unread: e.message }, told)), DECK_POLL);
}
const NO_BRIEF = 'this project keeps no status brief (30-brief/BRIEF-status.md)';
function deckCommand() {
  return { id: 'research.deck', on: 'research project', label: 'Build deck', icon: 'file-text', risk: 'safe', executes: true,
    does: 'Builds this experiment\'s status deck from its deck.json; nothing uploads.',
    when: o => (!o.experiment ? NO_BRIEF : !o.experiment.deck ? 'this checkout keeps no 30-brief/deck.json' : !settled(BUILD) ? `run ${BUILD.id} is building ${BUILD.repo}'s deck` : true),
    cli: o => `cd ${shPath(o.path)} && sd-research-kit deck`,
    sends: o => `POST /api/research/deck {"repo": "${o.key}"}`,
    run: o => window.shell.post('/api/research/deck', { repo: o.key }).then(run => { followBuild(run, true); return `Building ${o.label}'s deck · run ${run.id}`; }, refused) };
}
function readSlides() {
  return window.shell.getJSON('/api/research/slides/status').then(d => { SLIDES = d; },
    e => { SLIDES = { ready: false, why: `the Slides state was not read: ${e.message}`, imports: SLIDES?.imports || {} }; }).then(renderExperiments);
}
function slidesCommand() {
  return { id: 'research.slides', on: 'research project', label: 'Import to Slides', icon: 'send', risk: 'safe', askFirst: true, executes: true,
    does: 'Imports the built deck into Google Slides on the work account; shares nothing.',
    when: o => (!o.experiment ? NO_BRIEF : !o.experiment.built ? 'no deck is built: press Build deck first' : IMPORTING ? `${IMPORTING}'s deck is importing now`
      : !SLIDES ? 'whether workspace-mcp answers is not read yet' : SLIDES.why || true),
    cli: () => 'no sd verb: the dashboard imports the deck through workspace-mcp',
    sends: o => `POST /api/research/slides {"repo": "${o.key}"}`,
    consequence: o => `${o.label}'s deck.pptx goes to Google Slides on the work account. An unchanged deck reuses its Slides file; a changed one replaces that file's content, keeping its link and sharing. Nothing new is shared.`,
    run: o => { IMPORTING = o.key; renderExperiments();
      return window.shell.post('/api/research/slides', { repo: o.key }).then(d => {
        IMPORTING = ''; SLIDES = { ...SLIDES, imports: { ...SLIDES?.imports, [o.key]: d.slides } }; renderExperiments();
        return d.reused ? `Slides unchanged · ${o.label}: this deck is already imported` : d.updated ? `Updated in Slides · ${o.label}: same file and link` : `Imported to Slides · ${o.label}${d.replaces ? '; the earlier Slides file stays in Drive' : ''}`;
      }, err => { IMPORTING = ''; renderExperiments(); return refused(err); }); } };
}
xpBody.addEventListener('click', e => { if (e.target.closest('a, .acts')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

// ---------- Data (build) ----------
// The board reads through shell.read (the Start block); the ledger is read here, per project.
// build: the ledger is read per project, when it is selected, and kept for the page's life.
const sourcesPath = key => `/api/research/${key.split('/').map(encodeURIComponent).join('/')}`;
// A refusal for a dead page session already says to reload (sd:5664): reselecting reads with the same session.
const retry = e => (/^Reload the page/.test(e || '') ? '' : ' Reselect the project to retry.');
function loadSources(key) {
  if (!SOURCES.has(key)) SOURCES.set(key, window.shell.getJSON(sourcesPath(key)).then(s => (SOURCES.set(key, s), s), err => { const s = { error: err.message, files: [], rows: [], total: 0 }; SOURCES.set(key, s); return s; }));
  return Promise.resolve(SOURCES.get(key));
}

// ---------- Add source (sd:5112) ----------
// Pasted URLs and dropped files become grade U ledger rows on a fresh branch the lane lands (source_intake.py); nothing here
// writes the checkout. A file is staged first, as bytes, since the JSON bound is 64 KiB. Paste works from the keyboard alone.
const READING = 'research/reading', FILE_BYTES = 5 * 1024 * 1024, SUFFIX = /\.(pdf|md|markdown|htm[l]?|txt)$/i;
const intakeEl = document.getElementById('intake'), urlsEl = document.getElementById('intake-urls'), toEl = document.getElementById('intake-to');
let dropped = [], intakeBusy = false;
const pasted = () => urlsEl.value.split(/\s+/).filter(Boolean);
const goodUrl = u => /^https?:\/\/[^\s/|<>]+[^\s|<>]*$/.test(u) && u.length <= 400;
const bytes = n => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);
function drawIntake(said) {
  const keys = [...new Set([READING, ...PROJECTS.map(p => p.key)])], want = toEl.value || READING;
  put(toEl, html`${keys.map(k => html`<option value="${k}"${k === want ? html` selected` : ''}>${k === READING ? 'reading · no topic repo' : k}</option>`)}`);
  toEl.value = want;
  const urls = pasted(), bad = urls.filter(u => !goodUrl(u)), odd = dropped.filter(f => !SUFFIX.test(f.name));
  put(document.getElementById('intake-list'), html`${urls.map(u => html`<li${goodUrl(u) ? '' : html` data-bad`}><span class="k">URL</span><span class="t">${u}</span><span class="w">${goodUrl(u) ? 'a U row; its title is read on Add' : 'not an http or https address'}</span></li>`)}${
    dropped.map((f, i) => html`<li${SUFFIX.test(f.name) ? '' : html` data-bad`}><span class="k">File</span><span class="t">${f.name} · ${bytes(f.size)}</span><span class="w">${!SUFFIX.test(f.name) ? 'not added: PDF, Markdown, HTML or text only' : f.size > FILE_BYTES ? 'over 5 MB: kept under the bulk storage root, linked' : 'committed in 10-sources/files'}</span><button class="icon-btn" type="button" data-unfile="${i}" aria-label="Remove ${f.name}" title="Remove">${I('x')}</button></li>`)}`);
  const n = urls.length + dropped.length;
  document.getElementById('intake-add').disabled = intakeBusy || !n || bad.length > 0 || odd.length > 0;
  document.getElementById('intake-add').textContent = intakeBusy ? 'Adding…' : n ? `Add ${plural(n, 'source')}` : 'Add';
  if (said !== undefined) put(document.getElementById('intake-said'), html`${said}`);
}
function addFiles(list) { dropped = [...dropped, ...list]; drawIntake(''); }
async function stage(f) {
  try {
    const out = await window.shell.post('/api/research/intake/file', f, { 'X-SD-File-Name': encodeURIComponent(f.name) });
    return { name: out.name, sha256: out.sha256 };
  } catch (err) { err.message = `${f.name}: ${err.message}`; throw err; }
}
intakeEl.addEventListener('submit', async e => {
  e.preventDefault();
  const target = toEl.value || READING, urls = pasted();
  intakeBusy = true; drawIntake('');
  try {
    const files = [];
    for (const f of dropped) files.push(await stage(f));
    const out = await window.shell.post('/api/research/intake', { target, urls, files });
    dropped = []; urlsEl.value = '';
    intakeBusy = false;
    drawIntake(html`${out.enqueued ? 'Queued' : 'Committed, not queued'}: ${plural(out.added.length, 'row')} on ${out.branch} as <a href="${shell.itemPage(out.item)}">sd:${out.item}</a>. ${
      out.enqueued ? 'Each row shows here, with Adversarial review to grade it, once the lane merges it and the checkout syncs.' : out.error}`);
  } catch (err) {
    intakeBusy = false;
    drawIntake(html`<span class="no">Not added: ${err.message.replace(/\.$/, '')}.</span>${err.status ? '' : ' Whether anything was written is not known; check the queue before you add again.'}`);
  }
});
urlsEl.addEventListener('input', () => drawIntake());
toEl.addEventListener('change', () => drawIntake());
document.getElementById('intake-files').addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });
document.getElementById('intake-list').addEventListener('click', e => {
  const b = e.target.closest('[data-unfile]'); if (b) { dropped.splice(+b.dataset.unfile, 1); drawIntake(); urlsEl.focus(); } });
// The whole Sources panel takes a drop: files are listed, and a dragged link is pasted as a URL line.
const sourcesPanel = document.getElementById('view-sources');
sourcesPanel.addEventListener('dragover', e => { e.preventDefault(); sourcesPanel.toggleAttribute('data-drop', true); });
sourcesPanel.addEventListener('dragleave', e => { if (!sourcesPanel.contains(e.relatedTarget)) sourcesPanel.removeAttribute('data-drop'); });
sourcesPanel.addEventListener('drop', e => {
  e.preventDefault(); sourcesPanel.removeAttribute('data-drop');
  const link = e.dataTransfer.getData('text/uri-list');
  if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  else if (link) { urlsEl.value = `${urlsEl.value.trim()}\n${link.split(/\r?\n/).filter(l => l && !l.startsWith('#')).join('\n')}`.trim(); drawIntake(''); }
});

// ---------- Source reader ----------
const reader = document.getElementById('reader');
let curSource = null;
async function readerFor(key) {
  const p = PROJECTS.find(x => x.key === key);
  document.getElementById('reader-for').textContent = '';
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
    put(reader, html`<p class="unknown"><span class="g-unknown">▨</span>The ledger was not read: ${s.error.replace(/\.$/, '')}.${retry(s.error)}</p>`);
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
        <button type="button"><span class="id" data-grade="${r.grade || r.id}">${r.id}</span><span class="t">${r.title}<span class="u">${r.c3}</span></span></button>
        ${r.cited ? html`<button class="cites" type="button" data-cites="${i}" aria-label="Show the ${plural(r.cited, 'claim')} citing ${r.title}" title="Claims citing it">◆ ${r.cited}</button>` : html`<span></span>`}
        ${r.url ? html`<a class="icon-btn" href="${r.url}" target="_blank" rel="noopener" aria-label="Open source: ${r.title}" title="Open source">${I('arrow-up-right')}</a>` : html`<span></span>`}${latest(s, r)}</li>`;
    })}</ul>` : html`<p class="why">No table rows in the ledger: the reader reads Markdown tables.</p>`}${
    // build: the reader does not page; the document carries the first rows and the whole count.
    s.total > s.rows.length ? html`<p class="more">${s.rows.length} of ${s.total} rows shown. Open the ledger in the checkout for the rest.</p>` : ''}`);
  const grades = {}; s.rows.forEach(r => { const g = r.grade || r.id; if (/^[ABCUM]$/.test(g)) grades[g] = (grades[g] || 0) + 1; });
  put(document.getElementById('reader-tally'), html`${['A', 'B', 'C', 'U', 'M'].filter(g => grades[g]).map(g => html`<span>${g} ${grades[g]}</span>`)}<span>${plural(s.total, 'row')}</span>`);
  shell.view.count('sources', s.total);
}
// sd:5101: a source's adversarial reviews are briefs filed with its key (adversarial.source_reviews), newest first.
const reviewsOf = (s, r) => (r.key && s.reviews?.[r.key]) || [];
const day = b => b.filed.slice(0, 10);
const briefLink = b => html`<a href="${shell.itemPage(b.item)}">brief sd:${b.item}</a>`;
function latest(s, r) {
  const [b, ...older] = reviewsOf(s, r); if (!b) return '';
  return html`<p class="rev">Reviewed ${day(b)} · ${b.verdict || 'no verdict line'} · ${briefLink(b)}${older.length ? ` · ${older.length} older in Details` : ''}</p>`;
}
function reviewsSection(s, r) {
  const list = reviewsOf(s, r);
  const none = !s.reviews ? `Not known: the reviews were not read (${s.reviews_error || 'no reason given'}).` : 'None yet. Adversarial review files one; a re-run adds a brief and keeps the last.';
  return html`<h3>Adversarial reviews</h3>${list.length ? html`<ul class="claims">${list.map(b => html`<li class="claim"><span class="g" aria-hidden="true">◇</span><span>${day(b)} · ${b.verdict || 'no verdict line'} · ${briefLink(b)}</span></li>`)}</ul>`
    : html`<p class="why">${none}</p>`}${window.commands.reviewPane('source', `${selected} ${r.key}`)}`;
}
const sourceId = (key, r) => `source:${key} ${r.key || r.id}`;
reader.addEventListener('click', e => {
  const to = e.target.closest('[data-view]'); if (to) { shell.view.show(to.dataset.view); return; }
  const cites = e.target.closest('[data-cites]'); if (cites) { claimsOf(+cites.dataset.cites); return; }
  const li = e.target.closest('.src'); if (!li || e.target.closest('a')) return;
  selectSource(+li.dataset.i, true);
});

// ---------- Claim map ----------
// Read with the ledger (research_screen.claims, the sd:5064 ruling): each sentence in the map, brief and published documents that
// cites a ledger row by its URL, a deeper path under it, or its ID. Supported cites an A, B or M row; unverified cites only C, U
// or ungraded rows, so a ledger without grades leaves every claim unverified, and the legend says so with the count.
// build: the reference read claims by hand from one project's Status section, with inferred and corrected states; neither is read.
const claimsEl = document.getElementById('claims');
const VERDICT = { supported: ['●', 'ok'], unverified: ['▨', 'unknown'] };
// sd:5297: a claim type's hover text says what it means and how it is graded (the document's types, research_screen.CLAIM_TYPES),
// then what the ledger itself says each of those grades means (its grades). The page keeps no copy of either.
// build: title and aria-describedby until the shared tooltip helper (sd:5287) lands; then the legend, filter and marks move to it.
// A mark is not focusable, so its claim's button carries the description: a keyboard reaches the type there.
const typeWord = v => v[0].toUpperCase() + v.slice(1);
const ruleOf = (c, v) => c.types?.[v]?.rule || v;
const both = gs => (gs.length > 1 ? `${gs.slice(0, -1).join(', ')} and ${gs[gs.length - 1]}` : gs[0]);
function typeTip(c, v) {
  const defined = c.grades || {};
  const said = (c.types?.[v]?.grades || '').split('').filter(g => defined[g]).map(g => `${g} = ${defined[g]}`);
  return `${typeWord(v)}: ${ruleOf(c, v)}.${Object.keys(defined).length ? said.map(x => ` ${x}`).join('') : ' This ledger does not define its grades.'}`;
}
// The legend's help button (the shell's .help popover): what each grade letter the ledger defines means.
const gradeHelp = c => { const g = Object.entries(c.grades || {});
  return g.length ? `<b>Source grades.</b> ${g.map(([k, v]) => `<b>${k}</b> = ${v}`).join(' ')}` : '<b>Source grades.</b> This ledger does not define its grades.'; };
const claimTip = (c, x) => `${typeWord(x.s)}: ${ruleOf(c, x.s)}. This claim cites ${x.grades ? both(x.grades.split('')) : 'only ungraded sources'}.`;
let verdict = null, curClaim = null, litKeys = [], litClaims = [];
function resetClaims() { verdict = null; curClaim = null; litKeys = []; litClaims = []; }
async function renderClaims(key) {
  const p = PROJECTS.find(x => x.key === key);
  document.getElementById('claims-for').textContent = '';
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
    put(claimsEl, html`<p class="unknown"><span class="g-unknown">▨</span>Claims not read for ${p.label}: ${s.error ? s.error.replace(/\.$/, '') : 'the reader sent no claim map'}.${retry(s.error)}</p>`);
    shell.view.count('claims', null); return;
  }
  // A document the reader skipped is named, so an empty map is never mistaken for a read one.
  const unread = html`${c.unread.map(u => html`<p class="unknown"><span class="g-unknown">▨</span>${u.doc} not read: ${u.error}.</p>`)}`;
  shell.view.count('claims', c.total);
  if (!c.total) {
    put(claimsEl, html`<p class="why">No claims: no sentence in ${c.dirs.join(', ')} links a URL in the ledger. ${plural(c.read, 'document')} read.</p>${unread}`);
    return;
  }
  // The verdict filter: All and both verdicts always, so it reads as a control; a verdict with no claims is off.
  put(claimsEl, html`<div class="vbar" role="group" aria-label="Show claims by verdict"><span class="vlabel">Show</span><button type="button" class="v-all" data-verdict="" aria-pressed="${String(!verdict)}">All ${c.total}</button>${['supported', 'unverified'].map(v => html`<button type="button" class="v-${v}" data-verdict="${v}" aria-pressed="${String(verdict === v)}"${c.tally[v] ? '' : html` disabled`} title="${typeTip(c, v)}" aria-describedby="claim-type-${v}">${VERDICT[v][0]} ${c.tally[v] || 0} ${v}</button>`)}</div>
    <p class="vlegend">${['supported', 'unverified'].map(v => html`<span title="${typeTip(c, v)}" aria-describedby="claim-type-${v}">${VERDICT[v][0]} ${v}: ${ruleOf(c, v)}</span><span class="sr" id="claim-type-${v}">${typeTip(c, v)}</span>`)}<button class="help" type="button" aria-label="Help: source grades" data-help="${gradeHelp(c)}">${I('circle-help')}</button>${
      c.tally.ungraded ? html`<span>${!c.graded ? `The ledger has no grades, so all ${plural(c.tally.ungraded, 'claim')} are unverified` : `${c.tally.ungraded} of the unverified cite only ungraded sources`}</span>` : ''}</p>
    <ul class="claims">${c.rows.map((x, i) => html`<li class="claim" data-c="${i}" aria-current="${String(curClaim === i)}"${litClaims.includes(i) ? html` data-lit` : ''}${verdict && x.s !== verdict ? html` hidden` : ''}>
      <span class="g g-${VERDICT[x.s][1]}" title="${claimTip(c, x)}"><span aria-hidden="true">${VERDICT[x.s][0]}</span><span class="sr">${x.s}</span></span>
      <button type="button" aria-describedby="claim-type-${x.s}">${x.text}</button>
      <span class="meta"><span>${x.doc}:${x.line}</span>${x.keys.map(k => chip(s, k))}</span></li>`)}</ul>${
    c.total > c.rows.length ? html`<p class="more">${c.rows.length} of ${c.total} claims shown. ${c.moreError ? `The next claims were not read: ${c.moreError}. ` : ''}<button type="button" class="linkbtn" data-more-claims${c.loading ? html` disabled` : ''}>${c.loading ? 'Reading…' : 'Show more'}</button></p>` : ''}${unread}`);
  document.getElementById('claims-for').textContent = `· ${plural(c.total, 'claim')}`;
}
// Show more (past the reader's print bound): the next claims, from the count already held, appended to the cached map.
function moreClaims() {
  const key = selected, s = SOURCES.get(key), c = s?.claims; if (!c || c.loading) return;
  c.loading = true; c.moreError = ''; redraw();
  window.shell.getJSON(`${sourcesPath(key)}?from=${c.rows.length}`).then(n => {
    const rows = n.claims?.rows || [];
    if (!rows.length) c.moreError = n.error || 'the reader sent no further claims';
    c.rows.push(...rows);
  }, err => { c.moreError = err.message.replace(/\.$/, ''); }).finally(() => { c.loading = false; if (selected === key && SOURCES.get(key) === s) redraw(); });
}
claimsEl.addEventListener('click', e => {
  if (e.target.closest('[data-more-claims]')) { moreClaims(); return; }
  const to = e.target.closest('[data-view]'); if (to) { shell.view.show(to.dataset.view); return; }
  const v = e.target.closest('[data-verdict]');
  if (v) { verdict = !v.dataset.verdict || verdict === v.dataset.verdict ? null : v.dataset.verdict; redraw(); claimsEl.querySelector(`[data-verdict="${v.dataset.verdict}"]`)?.focus(); return; }
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
// The architecture overview (sd:5661, pack sd:5660): its page, or why there is none. Render is offered only where a render
// changes that: the overview is declared and its page is not built or stale.
function architecture(p) {
  const s = p.dirs?.includes('05-architecture') ? p.stages?.['05-architecture'] : null;
  if (!s) return 'none yet: 05-architecture/architecture.md starts from the sd-research-repo template';
  const render = ['not built', 'stale'].includes(s.state) ? html` <button class="btn quiet sm" type="button" data-cmd="research.render" data-obj="${p.key}">Render</button>` : '';
  return s.href ? html`<a href="${s.href}">${s.title}</a>${s.state === 'stale' ? ' · stale since its source changed' : ''}${render}` : html`${s.why}${render}`;
}
// read: false redraws from the experiment read already held, so the Act bar follows it without a second read.
function showProject(key, read = true) {
  const p = PROJECTS.find(x => x.key === key); if (!p) return;
  shownProject = key;
  const notFresh = p.docs.filter(d => d.state !== 'fresh');
  put(details, html`<p class="kind"><span class="g-${p.s}" aria-hidden="true">${GLYPH[p.s]}</span> Research project · ${kindWord(p.s)}</p>
    <h2>${p.label}</h2>
    <dl><dt>Checkout</dt><dd>${p.path}</dd><dt>Branch</dt><dd>${p.git ? `${p.git.branch || '?'}${p.git.dirty ? ` · ${p.git.dirty} dirty` : ' · clean'}${p.git.behind ? ` · ${p.git.behind} behind` : ''}` : 'not a git checkout'}</dd>
      <dt>Last commit</dt><dd>${p.git?.last_iso ? html`<time class="rel" datetime="${p.git.last_iso}"></time> · ${p.git.subject}` : 'not read'}</dd>
      <dt>Stage</dt><dd>${p.dirs ? (p.dirs.join(' · ') || 'numbered layout, no Markdown yet') : 'no numbered layout'}</dd>
      <dt>Render</dt><dd>${p.render.txt}${p.render.sub ? ` · ${p.render.sub}` : ''}</dd>
      <dt>Documents</dt><dd>${plural(p.docs_total ?? p.docs.length, 'document')} in research.conf.py${p.docs.length < (p.docs_total ?? 0) ? ` · ${p.docs.length} listed: the board passed its print bound` : ''} · <a href="/documents">Documents</a></dd>
      <dt>Architecture</dt><dd>${architecture(p)}</dd>
      <dt>Rounds</dt><dd>${ROUNDS}</dd></dl>
    ${p.conf ? html`<p class="unknown"><span class="g-unknown">▨</span>The dashboard collector could not read research.conf.py: “${p.conf}”. Per-document freshness stays unknown until it is read.</p>` : ''}
    ${notFresh.length ? html`<h3>Not fresh</h3><ul class="claims">${notFresh.map(d => html`<li class="claim"><span class="g g-caution" aria-hidden="true">▲</span><span>${d.title} · ${d.state} · ${d.src}</span></li>`)}</ul>` : ''}
    <h3>Act</h3>${shell.commands.bar(p.key)}
    ${p.experiment ? html`<h3>Deck</h3><p class="links">${deckLine(p)}${slidesLine(p)}</p>` : ''}
    ${REVIEWS[p.key] ? html`<h3>Last review</h3><p class="why">${REVIEWS[p.key].state === 'done' ? 'Every check passed' : REVIEWS[p.key].reason || 'failed'} · <time class="rel" datetime="${REVIEWS[p.key].finished || ''}"></time></p>
      <details class="report"><summary>Report</summary><pre>${REVIEWS[p.key].output || 'It printed nothing.'}</pre></details>` : ''}
    <p class="why">Render, Review and Build deck run one at a time. The kit runs research.conf.py only after you run sd-research-kit render once by hand in this checkout.</p>
    <section aria-live="polite">${experimentSection(p)}</section>`);
  shell.commands.select(p.key);
  if (read) loadExperiment(p.key);
  // build: the reference's suggestions named one project's open questions; these name the selected project only.
  shell.suggest([`What is ${p.label} waiting on?`, 'Which sources are graded C or U?', 'Summarise the START HERE document']);
  swap();
}
// ---------- Experiment (sd:5296) ----------
// The project's row in the team's experiments database (research_experiments.py), read for the selected project. Every Notion
// write is two runs: a draft that reads only, then Apply, which asks first and shows that draft; the POST carries the
// draft's SHA-256, so the server refuses an apply of anything but what was shown. Refresh and the drafts write nothing outward.
let EXP = null, expPolling = 0, shownProject = null;
const EXP_POLL = 5000;
const expPath = key => `/api/research/experiments?project=${encodeURIComponent(key)}`;
function loadExperiment(key) {
  return window.shell.getJSON(expPath(key)).catch(err => ({ error: err.message })).then(d => {
    if (selected !== key) return; // a newer selection owns the section
    EXP = { ...d, key };
    if (shownProject === key) showProject(key, false); // a source's or claim's Details keep the pane
    if (EXP.running && !expPolling) expPolling = setTimeout(() => { expPolling = 0; loadExperiment(selected); }, EXP_POLL);
  });
}
const mine = key => (EXP?.runs || []).filter(r => r.project === key);
// The newest finished draft of this project with no apply running or done; `unknown` when its last apply found no trace.
function pending(key) {
  const runs = EXP?.runs || [];
  for (const r of mine(key)) {
    if (r.verb !== 'draft' || r.state !== 'done' || !r.draft || !ACTION_WORD[r.action]) continue; // an older deck draft has no Apply
    const applies = runs.filter(a => a.verb === 'apply' && a.draft === r.run);
    if (applies.some(a => a.state === 'running' || a.state === 'done')) continue;
    return { ...r, unknown: applies[0]?.state === 'unknown' };
  }
  return null;
}
const ACTION_WORD = { create: 'new experiment', status: 'status update' };
const stampTime = s => `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)} ${s.slice(9, 11)}:${s.slice(11, 13)} UTC`;
const listed = page => (EXP?.cache?.rows || []).find(r => r.page === page);
function experimentSection(p) {
  if (!EXP || EXP.key !== p.key) return html`<h3>Experiment</h3><p class="why">Reading the experiment link…</p>`;
  if (EXP.error) return html`<h3>Experiment</h3><p class="unknown"><span class="g-unknown">▨</span>Not read: ${EXP.error.replace(/\.$/, '')}.${retry(EXP.error)}</p>`;
  const link = EXP.link, row = link && listed(link.page), c = EXP.cache, last = (EXP.runs || []).find(r => r.project === p.key || r.verb === 'list');
  const draft = pending(p.key);
  const problems = Object.entries(EXP.problems || {}).filter(([, why]) => why);
  return html`<h3>Experiment</h3>${EXP.link_error ? html`<p class="unknown"><span class="g-unknown">▨</span>The link was not read: ${EXP.link_error.replace(/\.$/, '')}.</p>`
    : link ? html`<dl><dt>Linked</dt><dd>${link.url ? html`<a class="ext" href="${link.url}" target="_blank" rel="noopener">${link.title || link.page} ${I('arrow-up-right')}</a>` : link.title || link.page}</dd>${
      row ? html`<dt>Stage</dt><dd>${row.stage || 'none'} · Status ${row.status || 'none'}</dd>` : ''}</dl>`
    : html`<p class="why">Not linked. Attach one, or use Draft experiment on the Experiments tab.</p>`}
    <p class="why">${c ? html`List: ${plural(c.rows.length, 'experiment')}, read <time class="rel" datetime="${c.read}"></time>${c.current ? '' : ' from another data source: refresh it before you attach'}.` : 'List: never read. Refresh experiments, in the toolbar, reads it.'}${EXP.cache_error ? ` The list did not read: ${EXP.cache_error}.` : ''}</p>
    ${problems.map(([, why]) => html`<p class="unknown"><span class="g-unknown">▨</span>${why.replace(/\.$/, '')}.</p>`)}
    ${draft ? html`<p class="why">Waiting for Apply: the ${ACTION_WORD[draft.action]} drafted ${stampTime(draft.run)}${draft.unknown ? ', whose last apply found no trace of its write' : ''}. Apply shows it first.</p>` : ''}
    ${EXP.running ? html`<p class="why">A run is going, started ${EXP.running}. This reads again until it ends.</p>`
      : last ? html`<p class="why">Last run: ${last.verb} ${last.action || 'experiments'} · ${last.state}${last.reason ? ` · ${last.reason}` : ''} · log ${last.log}</p>` : ''}`;
}
// What Apply writes, from the draft the server holds: the confirm shows all of it before anything leaves the machine,
// and Apply posts the draft the confirm showed (`shownDraft`), never one a later read found.
let shownDraft = null;
function applyText(o) {
  const d = pending(o.key); shownDraft = d && { key: o.key, run: d.run, fingerprint: d.fingerprint, unknown: d.unknown };
  if (!d) return '';
  const dup = d.unknown ? 'An earlier apply found no trace of its write, so this one may make a duplicate. ' : '';
  const x = d.draft, title = EXP.link?.title || 'the linked experiment';
  if (d.action === 'create') return `${dup}Creates one row in the experiments database, “${x.title}”, Stage Idea, Problem “${x.problem}”, its body “${x.body}”, and links ${o.label} to it. Nothing else is written.`;
  return `${dup}Writes the status section of ${title}. Now: “${x.before.section}”. After: “${x.after.section}”. Status ${x.before.status || 'none'} becomes ${x.after.status}. If either changed since the draft, nothing is written.`;
}
// The answer to a start, as a toast: a refusal names its reason; no answer means the outcome is not known.
const started = (what, key) => out => (key ? loadExperiment(key) : Promise.resolve()).then(() => (out.linked ? `Linked to ${out.linked.title || out.linked.page}` : `${what} started · this reads again until it ends`));
const refused = err => { if ([400, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether it started is not known`); };
const expPost = (body, what, key) => window.shell.post('/api/research/experiments/act', body).then(started(what, key), refused);
const expOff = o => (EXP?.key !== o.key ? 'the experiment link is not read yet' : EXP.error ? `not read: ${EXP.error}` : EXP.running ? 'a run is going now' : EXP.problems?.source || true);
function experimentCommands() {
  const on = 'research project', post = (o, body, what) => expPost({ ...body, project: o.key }, what, o.key);
  const linkedOff = o => { const w = expOff(o); return w !== true ? w : EXP.link_error ? `the link was not read: ${EXP.link_error}` : true; };
  return [
    { id: 'experiment.attach', on, label: 'Attach experiment', does: 'Links this project to a listed experiment, in the workflow database only.', risk: 'safe', executes: true,
      when: o => { const w = linkedOff(o); return w !== true ? w : !EXP.cache?.current ? 'refresh the experiments list first' : !EXP.cache.rows.length ? 'the list holds no experiments' : true; },
      fields: () => [{ name: 'page', label: 'Experiment', options: (EXP?.cache?.rows || []).map(r => ({ value: r.page, label: `${r.title} · ${r.stage || 'no stage'}` })) }],
      consequence: o => `Links ${o.label} to the chosen experiment in the workflow database${EXP?.link ? `, in place of ${EXP.link.title || EXP.link.page}` : ''}. Notion is not written.`,
      sends: () => 'POST /api/research/experiments/act {"verb":"attach"}',
      run: (o, v) => post(o, { verb: 'attach', page: v.page, replaces: EXP?.link?.page ?? null }, 'Attach') },
    { id: 'experiment.status', on, label: 'Draft status update', does: 'Drafts a dated status entry; Apply writes it after you read it.', risk: 'safe', executes: true,
      when: o => { const w = linkedOff(o); return w !== true ? w : !EXP.link ? 'not linked: attach or create an experiment first' : true; },
      sends: () => 'POST /api/research/experiments/act {"verb":"draft","action":"status"}',
      run: o => post(o, { verb: 'draft', action: 'status' }, 'Draft') },
    // Make experiment (sd:5662): this project as a new experiment, from its row or its Details. A project with a status
    // brief is one already. The link is read only for the project shown, so another row offers it too; the server
    // refuses a project already linked (409).
    { id: 'experiment.make', on, label: 'Make experiment', does: 'Drafts a new experiment for this project; Apply writes it after you read it.', icon: 'flask-conical', risk: 'safe', executes: true,
      when: o => (o.experiment ? 'already an experiment: it has a status brief' : EXP?.key !== o.key ? true : EXP.link ? 'already linked to an experiment' : EXP.running ? 'a run is going now' : EXP.problems?.source || true),
      fields: () => [{ name: 'description', label: 'What the experiment tests', required: true, placeholder: 'One or two sentences' }],
      sends: () => 'POST /api/research/experiments/act {"verb":"draft","action":"create"}',
      run: (o, v) => post(o, { verb: 'draft', action: 'create', description: v.description }, 'Draft') },
    { id: 'experiment.apply', on, label: 'Apply draft', does: o => { const d = EXP?.key === o.key && pending(o.key); return d ? `Writes the ${ACTION_WORD[d.action]} drafted ${stampTime(d.run)}, after you read it.` : 'Writes a finished draft, after you read it.'; },
      risk: 'safe', askFirst: true, executes: true,
      when: o => { const w = expOff(o); return w !== true ? w : pending(o.key) ? true : 'no finished draft waits for Apply'; },
      consequence: applyText, sends: () => 'POST /api/research/experiments/act {"verb":"apply"}',
      run: o => { const d = shownDraft?.key === o.key ? shownDraft : null; shownDraft = null;
        if (!d) return 'Apply writes only a draft its confirm showed: choose Apply draft again';
        return expPost({ verb: 'apply', run: d.run, fingerprint: d.fingerprint, recreate: d.unknown }, 'Apply', o.key); } },
  ];
}
function showSource(i) {
  const s = SOURCES.get(selected)?.rows?.[i]; if (!s) return;
  shownProject = null;
  shell.commands.put({ id: sourceId(selected, s), type: 'research source', label: s.title, project: selected, key: s.key });
  put(details, html`<p class="kind">${I('book-open')} Source · ${s.h[0] || 'grade'} ${s.id}</p><h2>${s.title}</h2>
    <dl><dt>Ledger</dt><dd>${s.f}</dd><dt>Section</dt><dd>${s.sec || 'none'}</dd><dt>${s.h[2] || 'Used for'}</dt><dd>${s.c3 || '—'}</dd>${s.c4 ? html`<dt>${s.h[3] || 'Note'}</dt><dd>${s.c4}</dd>` : ''}
      ${s.url ? html`<dt>Link</dt><dd><a class="ext" href="${s.url}" target="_blank" rel="noopener">${s.url.replace(/^https?:\/\//, '').slice(0, 60)} ${I('arrow-up-right')}</a></dd>` : ''}</dl>
    <h3>Cited by</h3>${citedBy(s)}${reviewsSection(SOURCES.get(selected), s)}
    <h3>Act</h3>${shell.commands.bar(sourceId(selected, s))}`);
  shell.commands.select(sourceId(selected, s));
  shell.setContext(s.title.slice(0, 40)); swap();
}
function citedBy(r) {
  if (!r.key) return html`<p class="why">No link or ID in this ledger row, so no claim can cite it.</p>`;
  const map = SOURCES.get(selected)?.claims;
  if (!map) return html`<p class="why">Not known: the claim map was not read.</p>`;
  const by = map.rows.filter(x => x.keys.includes(r.key));
  if (!r.cited) return html`<p class="why">No sentence in ${map.dirs.join(', ')} cites this source.</p>`;
  return html`<ul class="claims">${by.map(x => html`<li class="claim"><span class="g g-${VERDICT[x.s][1]}" aria-hidden="true">${VERDICT[x.s][0]}</span><span>${x.text} · ${x.doc}:${x.line}</span></li>`)}</ul>${
    r.cited > by.length ? html`<p class="more">${by.length} of ${plural(r.cited, 'claim')} listed: the reading passed its print bound.</p>` : ''}`;
}
function showClaim(s, x) {
  shownProject = null;
  put(details, html`<p class="kind"><span class="g-${VERDICT[x.s][1]}" aria-hidden="true">${VERDICT[x.s][0]}</span> Claim · ${x.s}</p><h2>${x.text}</h2>
    <dl><dt>Document</dt><dd>${x.doc}, line ${x.line}</dd><dt>Verdict</dt><dd>${x.s === 'supported' ? `${ruleOf(s.claims, 'supported')} (grades ${x.grades})` : x.grades ? `cites only ${x.grades.split('').join(' and ')} sources` : 'cites only ungraded sources'}</dd></dl>
    <h3>Sources</h3><ul class="claims">${x.keys.map(k => { const r = s.rows.find(y => y.key === k);
      return html`<li class="claim"><span class="g" aria-hidden="true">${r ? r.id : '?'}</span><span>${r ? r.title : `${k}: past the rows shown`}</span></li>`; })}</ul>`);
  shell.setContext(x.text.slice(0, 40)); swap();
}
// Sources and Claim map name their project in a picker in their heads (sd:5747), so either picks another without Projects.
function drawPickers() {
  for (const id of ['reader-pick', 'claims-pick']) {
    const el = document.getElementById(id);
    put(el, html`${selected ? '' : html`<option value="" selected>Pick a project</option>`}${PROJECTS.map(p => html`<option value="${p.key}"${p.key === selected ? html` selected` : ''}>${p.label}</option>`)}`);
    el.value = selected || '';
  }
}
['reader-pick', 'claims-pick'].forEach(id => document.getElementById(id).addEventListener('change', e => { if (e.target.value) select(e.target.value, false); }));
function select(key, open) {
  if (!PROJECTS.some(p => p.key === key)) return;
  selected = key; curSource = null; resetClaims(); drawPickers();
  [tbody, xpBody].forEach(b => b.querySelectorAll('tr').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === key)));
  readerFor(key); renderClaims(key); showProject(key);
  writeURL();
  // Only a project the viewer opens becomes the Add source target: the board selects its first row by itself (sd:5112).
  if (open) { toEl.value = key; drawIntake(); shell.openPane('tab-details'); }
}
function selectSource(i, open) {
  curSource = i;
  document.querySelectorAll('.src[data-i]').forEach(li => li.setAttribute('aria-current', +li.dataset.i === i));
  showSource(i); if (open) shell.openPane('tab-details');
}
// A stage bar is a link: it opens its document, not the row's Details.
tbody.addEventListener('click', e => { if (e.target.closest('.rowact, a')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

// ---------- Commands (products/system/commands.md) ----------
// Each board row is a research project. Labels are CLI verbs; the shell renders every button, menu entry and CLI line.
// Render (sd:5632) and Review (sd:5655) run through the deck runner's lock, as dashboard runs.
// One poll per run until it settles; a render that is done reads the board again, a failed one says the kit's reason.
// A review keeps its report, pass or fail, and the project's Details show it under Last review.
const REVIEWS = {};
const followRun = (run, o, verb) => (['done', 'failed'].includes(run.state)
  ? (verb === 'review' ? (REVIEWS[o.key] = run, shownProject === o.key && showProject(o.key, false),
      shell.toast(run.state === 'done' ? `Reviewed · ${o.label}: every check passed` : `Review · ${o.label}: ${run.reason || 'no reason recorded'}`))
    : (shell.toast(run.state === 'done' ? `Rendered · ${o.label}` : `Render failed · ${o.label}: ${run.reason || 'no reason recorded'}`), run.state === 'done' && load()))
  : setTimeout(() => window.shell.getJSON(`/api/research/deck/${encodeURIComponent(run.id)}`).then(next => followRun(next, o, verb),
    e => shell.toast(`${verb === 'review' ? 'Review' : 'Render'} ${run.id}: not read (${e.message}); whether it ended is not known`)), DECK_POLL));
// Draft experiment is a page action (the Experiments tab's header, sd:5631): it names its project, the one shown first. The server refuses a linked one (409).
const PAGE = { id: 'research:page', type: 'research page', label: 'Research' };
const CREATE = { id: 'experiment.create', on: 'research page', label: 'Draft experiment', does: 'Drafts a new experiment from a description; Apply writes it after you read it.', risk: 'safe', executes: true,
  when: () => PROJECTS.length ? true : 'no research project to draft for',
  fields: () => [{ name: 'project', label: 'Project', options: [...PROJECTS].sort((a, b) => (b.key === selected) - (a.key === selected)).map(p => ({ value: p.key, label: p.label })) },
    { name: 'description', label: 'What the experiment tests', required: true, placeholder: 'One or two sentences' }],
  sends: () => 'POST /api/research/experiments/act {"verb":"draft","action":"create"}',
  run: (o, v) => { select(v.project, true); return expPost({ verb: 'draft', action: 'create', description: v.description, project: v.project }, 'Draft', v.project); } };
document.getElementById('draft-exp').addEventListener('click', () => shell.commands.run(CREATE, PAGE));
// Refresh experiments reads the one team list every project shares (sd:5656): a page action, so it needs no selection.
const REFRESH = { id: 'experiment.refresh', on: 'research page', label: 'Refresh experiments', does: 'Reads the team\'s experiments list from Notion; writes nothing there.', risk: 'safe', executes: true,
  when: () => (EXP?.running ? 'a run is going now' : EXP?.problems?.source || true), sends: () => 'POST /api/research/experiments/act {"verb":"list"}',
  run: () => expPost({ verb: 'list' }, 'Refresh', selected) };
document.getElementById('refresh-exp').addEventListener('click', () => shell.commands.run(REFRESH, PAGE));
const DRAFT = { id: 'draft:research', type: 'research request', label: 'new research', q: '', cmd: '' };
// sd:5887: Run files the task the line names through /api/items, as Capture does; Undo closes it, as sd task cancel does.
const START = { id: 'research.start', on: 'research request', label: 'Run', does: 'Files this research request as a task; Undo closes it.', key: 'r', icon: 'flask-conical', risk: 'undo', executes: true, primary: () => true,
  when: o => (!o.q ? 'type the question first' : !o.repo ? 'name the project: add repo:<name>' : true), cli: o => o.cmd,
  sends: o => `POST /api/items {title, body${o.due ? ', due' : ''}}`,
  run: o => window.shell.post('/api/items', { title: `Research: ${o.q}`, body: `repo=${scopeOf(o.repo)} depth=${o.depth} skill=sd-research-repo stage=draft`, ...(o.due ? { due: o.due } : {}) })
    .then(out => ({ text: `Research filed · #${out.item.id} · ${o.q}`, item: out.item.id, revision: out.revision })),
  undo: (o, v) => window.shell.post(`/api/items/${v.item}/cancel-task`, { reason: 'withdrawn on Research right after filing', revision: v.revision }) };
function registerCommands() {
  const C = shell.commands;
  C.put(DRAFT); C.put(PAGE);
  C.register(
    { id: 'research.render', on: 'research project', label: 'Render', does: 'Renders this research project again in its checkout.', key: 'r', icon: 'rotate-ccw', risk: 'safe', executes: true, primary: o => o.s === 'caution',
      cli: o => `cd ${shPath(o.path)} && sd-research-kit render`, sends: o => `POST /api/research/render {"repo": "${o.key}"}`,
      run: o => window.shell.post('/api/research/render', { repo: o.key }).then(run => { followRun(run, o, 'render'); return `Rendering ${o.label} · run ${run.id}`; }, refused) },
    { id: 'research.review', on: 'research project', label: 'Review', does: 'Runs the research kit\'s mechanical pre-publication checks here; Details show the report.', key: 'v', icon: 'shield-check', risk: 'safe', executes: true,
      cli: o => `cd ${shPath(o.path)} && sd-research-kit review`, sends: o => `POST /api/research/review {"repo": "${o.key}"}`,
      run: o => window.shell.post('/api/research/review', { repo: o.key }).then(run => { followRun(run, o, 'review'); return `Reviewing ${o.label} · run ${run.id}`; }, refused) },
    START,
    CREATE,
    REFRESH,
    ...experimentCommands(),
    deckCommand(),
    slidesCommand(),
    // sd:5101: codex checks the source's grade and the claims citing it against the source as the runner fetches it.
    window.commands.adversarialReview('research source', { id: 'source.adversarial-review',
      does: 'Starts a codex review of this source against the claims citing it; its brief lists fixes to pick.',
      payload: o => ({ kind: 'source', target: o.project, source: o.key }),
      when: o => { const s = SOURCES.get(o.project);
        return !o.key ? 'no link or ID in this ledger row, so no claim can cite it' : s?.reviews ? true : `briefs not read: ${s?.reviews_error || 'the ledger was not read'}`; } }),
  );
}
document.addEventListener('shell:open', e => select(e.detail, true));
// A review's progress redraws the shown source's Details; a filed one reads the ledger again, so its brief lists.
document.addEventListener('adversarial:run', e => { const r = SOURCES.get(selected)?.rows?.[curSource];
  if (!r || e.detail.key !== `source:${selected} ${r.key}`) return;
  if (e.detail.run.filed === 'written') { SOURCES.delete(selected); loadSources(selected).then(() => { redraw(); showSource(curSource); }); } else showSource(curSource); });
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
  put(reqEl, html`<p class="why">Creates an item in ${scope}: Run files it, and Undo closes it. Review rounds start at 0 of ${CAP}.</p>
    ${o.q ? html`<div class="cli"><code>${DRAFT.cmd}</code><button class="icon-btn" type="button" data-copy="${DRAFT.cmd}" aria-label="Copy command">${I('copy')}</button></div>
    <div class="actions">${shell.commands.rowActions(DRAFT.id)}</div>` : ''}`);
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
  drawIntake(); // the Add source targets are the board's checkouts, reading first
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
    : unknown ? { kind: 'partial', text: `${plural(unknown, 'project')} with a config or render the collector could not read.${cut}`, source: '/api/research' }
    : cut ? { kind: 'partial', text: cut.trim(), source: '/api/research' } : null; // rounds stay unread: the Details' Rounds line says why
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
// Read again (sd:5638): the shell's head button reads the board, the cached ledgers behind Sources and Claim map, and the
// shown project's experiment link again.
window.PAGE_AGAIN = { title: "Read the checkouts, the selected project's ledgers and its experiment link again", run: () => {
  SOURCES.clear(); load();
  if (selected) { loadExperiment(selected); readerFor(selected); renderClaims(selected); }
} };
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
  // A build already running when the page opens is followed too.
  window.shell.getJSON('/api/research/deck/status').then(d => followBuild(/^(render|review)-/.test(d.run?.id || '') ? null : d.run, false), e => { BUILD_UNREAD = e.message; renderExperiments(); });
  readSlides();
});
