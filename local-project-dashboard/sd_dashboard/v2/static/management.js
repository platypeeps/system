// Management (sd:2118): the design source's products/system/designs/v2/management.js at d82daa1, ported. Each change from the
// reference is marked "build:". The rows are /api/management (management_screen.py), never sample data. A command that
// executes posts to a route server.action_route answers, and its toast comes after the write lands, not before.
const { html, put, plural } = window.markup;
// Chat scope = the selected repo. Set before shell.js reads it; a repo page is its own URL, so the scope is right on load.
(() => { const r = new URLSearchParams(location.search).get('repo'); if (r) document.body.dataset.scope = r.replace(/^~\/(repos\/)?/, ''); })();

const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
const SDDB = '~/repos/system/local-sd-db/sd-db.sh';

// ---------- Data (build): /api/management, read on load and after each write ----------
let DOC = null, READ = '';
let REPOS = [], ALL = [], GIT = null, LANE = null, ASG = null, SESS = null, SERVICES = [], CRON = [];
const csrf = () => document.querySelector('meta[name="sd-csrf"]')?.content || '';
async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-SD-CSRF': csrf() }, body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(out.error || `HTTP ${r.status}`); e.stale = r.status === 409; throw e; }
  return out;
}
async function getJSON(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
  return out;
}
const tilde = p => { const h = GIT?.root ? GIT.root.replace(/\/repos\/?$/, '') : ''; return h && p.startsWith(h + '/') ? '~' + p.slice(h.length) : p; };
const nameOf = p => p.replace(/^~\/(repos\/)?/, '');
const hhmm = iso => iso ? iso.slice(11, 16) : '';
// build: the document's sources say which reads failed; a failed one renders as unknown with its reason, never as empty.
const why = s => DOC?.sources?.[s] || '';
function absorb(doc) {
  DOC = doc; READ = doc.read || new Date().toISOString();
  document.body.dataset.observed = READ;
  GIT = doc.git;
  const gitBy = Object.fromEntries((GIT?.repos || []).map(g => [tilde(g.path), g]));
  REPOS = (doc.repos || []).map(r => {
    const m = (r.remote || '').match(/github\.com[:/]([^/]+)\/(.+?)(\.git)?$/);
    return { path: r.path, remote: r.remote, source: r.status_source, managed: r.managed, merge: r.runner_merge, mode: r.mode, ci: r.ci,
      pieces: r.pieces_source, created: r.created_at, updated: r.updated_at, review: r.review, protection: r.protection,
      owner: m ? m[1] : '?', slug: m ? `${m[1]}/${m[2]}` : '', name: nameOf(r.path), registered: true, git: gitBy[r.path] || null };
  });
  const unreg = (GIT?.repos || []).filter(g => !REPOS.some(r => r.path === tilde(g.path)))
    .map(g => ({ path: tilde(g.path), remote: '', source: '—', managed: '—', merge: '—', mode: '—', owner: '—', slug: '', name: nameOf(tilde(g.path)), registered: false, git: g }));
  ALL = [...REPOS, ...unreg];
  ALL.forEach(r => { const g = r.git; r.branch = g ? g.branch : '—'; r.lag = g && g.behind_default !== null ? g.behind_default : -1; r.fetched = g ? g.fetched_iso || '' : ''; });
  LANE = doc.lane; ASG = doc.assignments; SESS = doc.sessions; SERVICES = doc.services || [];
  CRON = (doc.jobs || []).map(j => ({ ...j, next: nextRun(j.schedule), rank: stateOf(j) }));
}
// Age of the last fetch at the reading, as v1 words it (repos_screen._age).
const age = iso => { const m = Math.max(0, Math.floor((Date.parse(READ) - Date.parse(iso)) / 60000)); return isNaN(m) ? 'no fetch' : m < 60 ? `${m} min` : m < 1440 ? `${Math.floor(m / 60)} h` : `${Math.floor(m / 1440)} d`; };
// Why no pull is offered, or true. build: the refusal is repos_screen.primary()'s remedy, read by the server, not rebuilt here.
const pullWhy = o => { const g = o.row.git;
  if (!g) return why('git') ? `git state was not read: ${why('git')}` : 'this checkout is not under the fleet root, and the fleet reads git state there only';
  if (g.error) return "the tree's state was not fully read";
  if (g.state === 'behind') return g.remedy.startsWith('git ') ? true : g.remedy.replace(/^No pull offered: /, '').replace(/\.$/, '');
  return g.state === 'current' ? `nothing to pull: ${g.detail.replace(/\.$/, '')}` : (g.detail || g.headline).replace(/\.$/, ''); };

// ---------- Time ----------
// build: launchd calendars (StartCalendarInterval dicts) in the viewer's clock, which is the Mac's when the dashboard runs there.
// A calendar entry names any of Month, Day, Weekday, Hour and Minute; the next run is the earliest entry's next match.
function nextRun(schedule, now = Date.now()) {
  if (!Array.isArray(schedule) || !schedule.length) return '';
  const from = new Date(now); from.setSeconds(0, 0); from.setMinutes(from.getMinutes() + 1);
  const next = schedule.map(e => nextOf(e, from)).filter(Boolean).sort((a, b) => a - b)[0];
  return next ? next.toISOString().replace('.000', '') : '';
}
// build: day by day with no one-week horizon, so a monthly or yearly entry has a next run; eight years reach a 29 February.
function nextOf(e, from) {
  const hours = e.Hour != null ? [e.Hour] : [...Array(24).keys()], minutes = e.Minute != null ? [e.Minute] : [...Array(60).keys()];
  for (let d = 0; d <= 8 * 366; d++) {
    const day = new Date(from.getFullYear(), from.getMonth(), from.getDate() + d);
    if ((e.Month != null && e.Month !== day.getMonth() + 1) || (e.Day != null && e.Day !== day.getDate())
      || (e.Weekday != null && e.Weekday % 7 !== day.getDay())) continue;
    for (const h of hours) for (const m of minutes) {
      const t = new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m);
      if (t >= from) return t;
    }
  }
  return null;
}
function human(schedule) {
  if (!Array.isArray(schedule) || !schedule.length) return 'no calendar';
  const W = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const two = n => String(n).padStart(2, '0');
  const M = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  // build (review, PR #50): an entry pinned to a Month, Day or Weekday names it; daily and hourly mean no such field is set.
  const pinned = e => e.Month != null || e.Day != null || e.Weekday != null;
  if (schedule.every(e => e.Hour == null && e.Minute != null && !pinned(e))) {
    const mins = schedule.map(e => e.Minute);
    return mins.length > 1 ? `every ${60 / mins.length} min` : `hourly at :${two(mins[0])}`;
  }
  const date = e => e.Month != null ? `${M[e.Month - 1]}${e.Day != null ? ` ${e.Day}` : ''}` : e.Day != null ? `day ${e.Day}` : '';
  const one = e => [date(e), e.Weekday != null ? W[e.Weekday] : '', `${e.Hour != null ? two(e.Hour) : '*'}:${e.Minute != null ? two(e.Minute) : '*'}`].filter(Boolean).join(' ');
  const daily = schedule.some(pinned) ? '' : 'daily ';
  return schedule.length > 2 ? `${daily}${schedule.length}× (${one(schedule[0])}…${one(schedule.at(-1))})` : `${daily}${schedule.map(one).join(', ')}`;
}
// build: a job's state is operations.inventory's; failed is warning, unknown and unloaded are unknown, the rest ok.
const stateOf = j => j.state === 'failed' ? 'warning' : ['unknown', 'unloaded'].includes(j.state) ? 'unknown' : j.state === 'interrupted' ? 'caution' : 'ok';

// ---------- Selection + URL ----------
const params = new URLSearchParams(location.search);
const details = document.getElementById('details');
const repoParam = params.get('repo');
// build: the view comes from the URL, so it is checked against the five names before anything indexes by it.
const VIEWS = ['repos', 'lane', 'sessions', 'deploys', 'schedules'];
let view = VIEWS.includes(params.get('view')) ? params.get('view') : 'repos';
const LIST_STATE = () => ({ repos: [RS, 'name', 1], lane: [LS, 'id', -1], schedules: [SS, 'rank', 1] })[view];
function setURL() {
  const p = new URLSearchParams();
  p.set('view', view);
  if (repoParam) p.set('repo', repoParam);
  if (text.q) p.set('q', text.q);
  const L = LIST_STATE();
  if (L) { const [st, sort, dir] = L;
    if (st.sort !== sort || st.dir !== dir) { p.set('sort', st.sort); p.set('dir', st.dir > 0 ? 'asc' : 'desc'); }
    if (st.page > 1) p.set('p', st.page);
    if (st.size && st.size !== 25) p.set('n', st.size); }
  if (view === 'repos') ['managed', 'merge'].forEach(k => RS[k] && p.set(k, RS[k]));
  shell.url(p);
}
function readURL() {
  if (params.get('q')) { text.q = params.get('q'); input.value = text.q; }
  const L = LIST_STATE(); if (!L) return;
  const [st] = L;
  if (params.get('sort')) { st.sort = params.get('sort'); st.dir = params.get('dir') === 'desc' ? -1 : 1; }
  if (+params.get('p') > 1 && 'page' in st) st.page = +params.get('p');
  if ([25, 50, 100].includes(+params.get('n')) && 'size' in st) st.size = +params.get('n');
  if (view === 'repos') ['managed', 'merge'].forEach(k => { if (params.get(k)) RS[k] = params.get(k); });
}
function swap() { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); }
const ACT = id => html`<td class="act end">${shell.commands.rowActions(id)}</td>`;
const ACTH = html`<th scope="col" class="act"><span class="sr">Actions</span></th>`;
function markRow(id) { document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id)); }
const unknown = (what, reason, read) => html`<div class="unknown"><span><span class="g-unknown" aria-hidden="true">▨</span> <b>${what}</b></span><span>${reason}</span>${read ? html`<span class="mono">${read}</span>` : ''}</div>`;

// ---------- Views: tabs ----------
const tabs = [...document.querySelectorAll('.sv')];
function showView(v, focus) {
  view = v;
  tabs.forEach(t => { const on = t.dataset.view === v; t.setAttribute('aria-selected', on); t.tabIndex = on ? 0 : -1; document.getElementById(t.getAttribute('aria-controls')).hidden = !on; });
  if (focus) tabs.find(t => t.dataset.view === v).focus();
  document.getElementById('shift').placeholder = { repos: 'Filter repos, e.g. hoa or merge:auto', lane: 'Filter merges by repo', sessions: 'Filter assignments by repo or agent', deploys: 'Filter services', schedules: 'Filter jobs, e.g. state:failed or backup' }[v];
  const row = shell.row(); if (row && !document.querySelector(`#view-${v} tr[data-id="${CSS.escape(row)}"]`)) shell.row(null);
  applyText();
  defaultDetails();
}
document.getElementById('subviews').addEventListener('click', e => { const t = e.target.closest('.sv'); if (t) showView(t.dataset.view); });
document.getElementById('subviews').addEventListener('keydown', e => {
  const i = tabs.findIndex(t => t.dataset.view === view);
  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); showView(tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length].dataset.view, true); }
});
// build: the five lamps count from the reading; a source that failed is unknown and says so.
function lamps() {
  const set = (v, state, val) => { const t = document.getElementById(`sv-${v}`); t.dataset.state = state; put(t.querySelector('.val'), val); };
  if (DOC.repos) set('repos', 'ok', html`<b>${REPOS.length}</b> rows · ${REPOS.filter(r => r.merge === 'auto').length} auto`);
  else set('repos', 'unknown', html`▨ not read`);
  const live = LANE ? LANE.live.length : 0;
  if (LANE) set('lane', LANE.heartbeat.ok ? 'ok' : 'caution', html`<b>${LANE.live.filter(a => a.status === 'queued').length}</b> queued · ${LANE.live.some(a => a.status === 'running') ? 'running' : live ? 'waiting' : 'idle'}${LANE.heartbeat.ok ? '' : ' · ▲ runner'}`);
  else set('lane', 'unknown', html`▨ not read`);
  if (SESS) set('sessions', SESS.abandoned ? 'caution' : 'ok', html`<b>${SESS.processes.length}</b> sd-* · ${SESS.abandoned ? `▲ ${SESS.abandoned} abandoned` : 'no abandoned'}`);
  else set('sessions', 'unknown', html`▨ not read`);
  set('deploys', 'unknown', html`▨ no deploy source`);
  if (DOC.jobs) { const failed = CRON.filter(c => c.rank === 'warning').length; set('schedules', failed ? 'warning' : 'ok', html`<b>${failed}</b> failed ${failed ? '■ ' : ''}of ${CRON.length}`); }
  else set('schedules', 'unknown', html`▨ not read`);
}

// ---------- Lists: sort, filter, page ----------
// build: a page from the URL or an older reading can pass the last page; clamp it before slicing, and say so in the URL.
function clampPage(total, st) {
  const page = Math.min(Math.max(1, st.page), Math.max(1, Math.ceil(total / st.size)));
  if (page === st.page) return;
  st.page = page;
  setURL();
}
function pager(total, st, onChange) {
  const pages = Math.max(1, Math.ceil(total / st.size));
  st.page = Math.min(st.page, pages);
  const from = total ? (st.page - 1) * st.size + 1 : 0, to = Math.min(total, st.page * st.size);
  const el = document.createElement('div');
  el.className = 'pager';
  put(el, html`<span>${from}–${to} of ${total.toLocaleString()}</span>
    <span class="pages" role="group" aria-label="Pages">${Array.from({ length: pages }, (_, i) => html`<button type="button" data-page="${i + 1}" aria-current="${String(st.page === i + 1)}">${i + 1}</button>`)}</span>
    <span class="sizes" role="group" aria-label="Rows per page"><span>per page</span>${[25, 50, 100].map(s => html`<button type="button" data-size="${s}" aria-pressed="${String(st.size === s)}">${s}</button>`)}</span>`);
  el.addEventListener('click', e => {
    const p = e.target.closest('[data-page]'), s = e.target.closest('[data-size]');
    if (p) st.page = +p.dataset.page; else if (s) { st.size = +s.dataset.size; st.page = 1; } else return;
    onChange(); setURL();
  });
  return el;
}
function sortHead(cols, st) {
  return html`<tr>${cols.map(([key, label, cls]) => key ? html`<th scope="col" class="${cls || ''}"${st.sort === key ? html` aria-sort="${st.dir > 0 ? 'ascending' : 'descending'}"` : ''}><button type="button" data-sort="${key}">${label}${I(st.sort === key ? (st.dir > 0 ? 'arrow-up' : 'arrow-down') : 'arrow-up-down')}</button></th>` : html`<th scope="col" class="${cls || ''}">${label}</th>`)}</tr>`;
}
const text = { q: '' };
function tokens() {
  const chips = {}, free = [];
  text.q.split(/\s+/).filter(Boolean).forEach(w => { const m = w.match(/^(managed|merge|source|state|agent):(\w+)$/i); m ? chips[m[1].toLowerCase()] = m[2].toLowerCase() : free.push(w.toLowerCase()); });
  return { chips, free };
}

// ---------- Repos list ----------
const RS = { sort: 'name', dir: 1, page: 1, size: 25, managed: null, merge: null };
function repoRows() {
  const { chips, free } = tokens();
  const managed = chips.managed || RS.managed, merge = chips.merge || RS.merge, source = chips.source;
  return ALL.filter(r => (!managed || r.managed === managed) && (!merge || r.merge === merge) && (!source || r.source === source) && free.every(f => (r.path + ' ' + r.remote).toLowerCase().includes(f)))
    .sort((a, b) => String(a[RS.sort]).localeCompare(String(b[RS.sort]), 'en', { numeric: true }) * RS.dir);
}
function gitCells(r) {
  const g = r.git;
  if (!g) return html`<td class="d mono unk" data-k="git" colspan="3"><span class="g-unknown" aria-hidden="true">▨</span> ${why('git') ? 'git state not read' : 'not read: outside the fleet root'}</td>`;
  const lag = g.state === 'behind' ? html`<span class="g-caution">+${g.ahead}/−${g.behind_default}</span>`
    : g.state === 'current' ? `+${g.ahead}/−0` : html`<span class="unk"><span class="g-unknown" aria-hidden="true">▨</span> ${g.headline}</span>`;
  return html`<td class="d mono" data-k="branch">${g.branch || '?'}${g.dirty === null ? html` <span class="unk">· dirty ?</span>` : g.dirty ? html` <span class="dirty">· ${g.dirty} dirty</span>` : ''}</td>
    <td class="d mono" data-k="±">${lag}</td><td class="d mono" data-k="fetched">${age(g.fetched_iso)}</td>`;
}
function renderRepos() {
  const el = document.getElementById('view-repos');
  if (!DOC.repos) { put(el, unknown('The repo table was not read', why('repos'), 'sd-db.sh repo list')); return; }
  if (repoParam) return renderRepoPage(el);
  const rows = repoRows();
  clampPage(rows.length, RS);
  const start = (RS.page - 1) * RS.size;
  const auto = REPOS.filter(r => r.merge === 'auto').length, managed = REPOS.filter(r => r.managed === 'yes').length;
  const c = GIT?.counts || {};
  put(el, html`<div class="sec-head"><h2 id="repos-h">Repos <button class="help" type="button" aria-label="Help: Repos" data-help="<b>The sd-db repo table is the enumeration.</b> One row per registered checkout. Open a repo for its settings page: the sd-db row, <code>.github/sd-review.json</code> and GitHub protection, each with the route an edit takes.">${I('circle-help')}</button></h2>
      <p class="tally"><span>${REPOS.length} registered</span><span>${managed} managed</span><span>${auto} runner merge auto</span></p>
      <p class="tally" id="git-tally">${GIT ? html`${c.repos ?? 0} checkouts under ${tilde(GIT.root || '') || 'the fleet root'} · ${c.dirty ?? 0} dirty · ${c.ahead ?? 0} ahead · ${GIT.repos.filter(g => g.state === 'behind').length} behind default · git read ${hhmm(READ)} UTC` : html`<span class="g-unknown">▨</span> git state not read: ${why('git')}`}</p></div>
    <div class="filters" aria-label="Filters">
      <div class="fgroup" role="group" aria-label="Managed"><span class="label">Managed</span>${['yes', 'no'].map(v => html`<button class="chip" type="button" data-f="managed" data-v="${v}" aria-pressed="${String(RS.managed === v)}">${v}</button>`)}</div>
      <div class="fgroup" role="group" aria-label="Runner merge"><span class="label">Runner merge</span>${['auto', 'manual'].map(v => html`<button class="chip" type="button" data-f="merge" data-v="${v}" aria-pressed="${String(RS.merge === v)}">${v}</button>`)}</div>
      <p class="fsum"><span>${rows.length} of ${ALL.length}</span>${RS.managed || RS.merge || text.q ? html`<button class="linkbtn" type="button" data-clear>Clear all</button>` : ''}</p>
    </div>
    ${rows.length ? html`<table class="ledger" aria-labelledby="repos-h"><thead>${sortHead([[null, html`<span class="sr">Settings</span>`, 'g'], ['name', 'Repo'], ['managed', 'Managed'], ['merge', html`Merge<span class="sr"> (runner merge)</span>`], ['branch', html`Branch<span class="sr"> and uncommitted files</span>`], ['lag', html`<span aria-hidden="true">+/−</span><span class="sr">Ahead and behind default</span>`], ['fetched', 'Fetched'], [null, html`<span class="sr">Actions</span>`, 'act']], RS)}</thead>
      <tbody>${rows.slice(start, start + RS.size).map(r => html`<tr data-id="${'repo:' + r.path}" data-repo="${r.path}">
        <td class="g" aria-hidden="true">${I(r.registered ? 'settings' : 'folder-code')}</td>
        <td class="what"><button type="button">${r.name}</button>${r.registered ? '' : html`<span class="unreg">not registered</span>`}</td>
        <td class="d mono ${r.managed}" data-k="managed">${r.managed}</td>
        <td class="d mono ${r.merge === 'auto' ? 'yes' : 'no'}" data-k="merge">${r.merge}</td>
        ${gitCells(r)}
        <td class="act end">${shell.commands.rowActions('repo:' + r.path)}</td></tr>`)}</tbody></table>`
      : html`<p class="empty">No repo matches ${text.q ? `“${text.q}”` : 'these filters'}. <button class="linkbtn" type="button" data-clear>Clear filters</button></p>`}
    <p class="note">${I('settings')} opens a registered repo's settings. Owner and status source sit in Details. Git state is as read at ${hhmm(READ)} UTC; nothing here fetches.</p>`);
  if (rows.length > RS.size || RS.size !== 25) el.append(pager(rows.length, RS, renderRepos));
}
document.getElementById('view-repos').addEventListener('click', e => {
  if (e.target.closest('.rowact')) return;
  const s = e.target.closest('[data-sort]'), f = e.target.closest('[data-f]'), tr = e.target.closest('tr[data-repo]');
  if (e.target.closest('[data-clear]')) { RS.managed = RS.merge = null; text.q = ''; document.getElementById('shift').value = ''; RS.page = 1; renderRepos(); return setURL(); }
  if (s) { RS.dir = RS.sort === s.dataset.sort ? -RS.dir : 1; RS.sort = s.dataset.sort; renderRepos(); return setURL(); }
  if (f) { RS[f.dataset.f] = RS[f.dataset.f] === f.dataset.v ? null : f.dataset.v; RS.page = 1; renderRepos(); return setURL(); }
  if (tr) selectRow(tr.dataset.id, true);
});

// ---------- One settings page per repo ----------
const pending = [];
function setRow(k, v, ctl = '', attrs = '') { return html`<div class="row"${attrs}><span class="k">${k}</span><span class="v">${v}</span><span class="ctl">${ctl}</span></div>`; }
const lock = w => html`<span class="lock">${I('lock')}${w}</span>`;
const seg = (key, opts, cur) => html`<span class="segctl" role="group" aria-label="${key}">${opts.map(o => html`<button type="button" data-set="${key}" data-v="${o}" aria-pressed="${String(o === cur)}">${o}</button>`)}</span>`;

function renderRepoPage(el) {
  const r = REPOS.find(x => x.path === repoParam);
  if (!r) { put(el, html`<p class="empty">No registered repo at <code>${repoParam}</code>. <a href="?view=repos">All repos</a></p>`); return; }
  const url = r.slug ? `https://github.com/${r.slug}` : null;
  const rv = r.review, pr = r.protection;
  put(el, html`
    <div class="crumb"><a class="btn quiet sm" href="?view=repos">${I('chevron-left')}All repos</a><h2>${r.name}</h2>${url ? html`<a class="ext" href="${url}" target="_blank" rel="noopener">${r.slug}${I('arrow-up-right')}</a>` : ''}</div>
    <ul class="routes" aria-label="How edits land">
      <li><span class="route">${I('terminal')}sd-db · local</span> runs one sd-db verb here</li>
      <li><span class="route">${I('git-pull-request')}file · PR</span> ships through sd-ship and review</li>
      <li><span class="route">${I('shield-check')}GitHub · API</span> read here; edits stay on the classic Protection screen</li>
    </ul>

    <section class="set" aria-labelledby="set-db"><header><h3 id="set-db">sd-db row <button class="help" type="button" aria-label="Help: sd-db row" data-help="<b>The row the runner and dashboard read.</b> Two fields have sd-db verbs: <code>repo runner-merge</code> and <code>repo managed</code>. The rest change only through registration or the docs/work migration.">${I('circle-help')}</button></h3><span class="route">${I('terminal')}sd-db · local</span>
      <p class="src">repo table · read ${hhmm(READ)} UTC</p></header>
      ${setRow('path', r.path, lock('primary key'))}
      ${setRow('remote', r.remote || html`<span class="no">none</span>`, lock('from the checkout'))}
      ${setRow(html`<code>runner_merge</code> <button class="help" type="button" aria-label="Help: runner_merge" data-help="<b>auto</b> lets the runner queue a merge after a done author run with a reviewed head, through the exclusive merge lane. <b>manual</b> ends the item at ready_to_send for you. Every row starts at manual.">${I('circle-help')}</button>`, r.merge, seg('runner_merge', ['manual', 'auto'], r.merge), html` data-key="runner_merge"`)}
      ${setRow(html`<code>managed</code>`, r.managed, seg('managed', ['yes', 'no'], r.managed), html` data-key="managed"`)}
      ${setRow(html`<code>mode</code>`, r.mode || html`<span class="no">unset</span>`, lock('no sd-db verb writes it'))}
      ${setRow(html`<code>ci</code>`, r.ci, lock('sd-db.sh repo ci'))}
      ${setRow(html`<code>status_source</code>`, r.source, lock('moves only by the docs/work migration'))}
      ${setRow(html`<code>pieces_source</code>`, r.pieces, lock('moves only by migration'))}
      ${setRow('registered', html`<time class="rel" datetime="${r.created}"></time> <span class="no">· updated</span> <time class="rel" datetime="${r.updated}"></time>`)}
    </section>

    <section class="set" aria-labelledby="set-review"><header><h3 id="set-review"><code>.github/sd-review.json</code> <button class="help" type="button" aria-label="Help: sd-review.json" data-help="<b>The review lane's policy for this repo.</b> Tiers, categories, severity floor and the Copilot override. A change here is itself reviewed: it ships as a pull request through sd-ship.">${I('circle-help')}</button></h3><span class="route">${I('git-pull-request')}file · PR</span>
      <p class="src">${rv ? `read from the checkout · ${hhmm(READ)} UTC` : 'not read'}</p></header>
      ${!rv ? unknown('Checkout not on disk', `The build reads the file from ${r.path}; no directory is there on this machine.`)
        : !rv.file ? setRow('file', html`<span class="no">${rv.error || 'absent'}</span><p class="why">The repo inherits the machine's review settings.</p>`, html`<button class="btn quiet sm" type="button" data-create-review>${I('plus')}Add file</button>`)
        : rv.error ? html`${setRow('file', html`<span class="no">${rv.error}</span>`)}<details class="srcfile"><summary>Show the file</summary><pre class="code">${rv.file}</pre></details>`
        : html`${setRow(html`<code>severity_floor</code> <button class="help" type="button" aria-label="Help: severity_floor" data-help="The severity at which a finding is blocking and sd-review exits 1. Schema values: high, medium, low, unspecified.">${I('circle-help')}</button>`, rv.severity_floor || html`<span class="no">unset</span>`, html`<select class="fld" data-set="severity_floor" aria-label="severity_floor">${['', 'high', 'medium', 'low', 'unspecified'].map(v => html`<option value="${v}"${(rv.severity_floor || '') === v ? html` selected` : ''}>${v || 'unset'}</option>`)}</select>`, html` data-key="severity_floor"`)}
           ${rv.automatic_deep == null ? setRow(html`<code>copilot_review.automatic_deep</code>`, html`<span class="no">unset</span>`) : setRow(html`<code>copilot_review.automatic_deep</code>`, String(rv.automatic_deep), seg('automatic_deep', ['true', 'false'], String(rv.automatic_deep)), html` data-key="automatic_deep"`)}
           <details class="srcfile"><summary>Show the file</summary><pre class="code">${rv.file}</pre></details>`}
    </section>

    <section class="set" aria-labelledby="set-local"><header><h3 id="set-local">CLAUDE.local.md overrides</h3><span class="route">${I('terminal')}local file</span>
      <p class="src">not read</p></header>
      ${unknown('Not read by the dashboard', 'The file is gitignored and can hold personal notes, so the build does not read it. Open it in the checkout to see its sd block.')}
    </section>

    <section class="set" aria-labelledby="set-gh"><header><h3 id="set-gh">GitHub · <code>${pr?.default_branch || 'default branch'}</code> <button class="help" type="button" aria-label="Help: GitHub protection" data-help="<b>Protection on the default branch,</b> as the nightly collector last read it into repo_protection. Nothing here calls GitHub. The gap sentences are the ones sd-status prints.">${I('circle-help')}</button></h3><span class="route">${I('shield-check')}GitHub · API</span>
      <p class="src">${pr?.observed_at ? html`repo_protection · observed <time class="rel" datetime="${pr.observed_at}"></time>` : 'not observed'}</p></header>
      ${!pr || pr.status === 'unknown' ? unknown(pr ? 'Protection unknown' : 'Not read', pr?.reason || why('repos') || 'no protection reading for this repo', r.slug ? `gh api repos/${r.slug}/rulesets` : '')
        : html`${setRow('status', pr.status)}
           ${pr.gaps.length ? pr.gaps.map(g => setRow(html`<code>${g.id}</code>`, html`<span class="g-caution" aria-hidden="true">▲</span> gap<p class="why">${g.gap || ''}</p>`)) : setRow('gaps', 'none found')}
           <p class="note">Every repo's gaps side by side: <a href="/protection">Protection (classic)</a>.</p>`}
    </section>`);
  pending.forEach(p => markPending(p.key));
}
function markPending(key) {
  const row = document.querySelector(`.row[data-key="${key}"]`), p = pending.find(x => x.key === key); if (!row || !p) return;
  row.setAttribute('data-pending', ''); row.querySelector('.v').dataset.proposed = p.value === '' || p.value == null ? 'unset' : String(p.value);
  row.querySelectorAll('.segctl [data-v]').forEach(b => b.toggleAttribute('data-proposed', b.dataset.v === String(p.value)));
}

// ---------- Proposals: two routes (build: CLAUDE.local.md and GitHub edits are not ported) ----------
function withdraw(key, quiet) {
  const p = pending.find(x => x.key === key); if (!p) return;
  const row = document.querySelector(`.row[data-key="${key}"]`);
  row?.removeAttribute('data-pending'); row?.querySelectorAll('[data-proposed]').forEach(b => b.removeAttribute('data-proposed'));
  dropProposal(p);
  if (!quiet) shell.toast(`Proposal withdrawn · ${p.title}`);
}
function propose(key, value) {
  const r = REPOS.find(x => x.path === repoParam), rv = r.review || {};
  let p;
  if (key === 'runner_merge' || key === 'managed') {
    const before = key === 'runner_merge' ? r.merge : r.managed;
    if (value === before) return withdraw(key);
    const verb = key === 'runner_merge' ? 'runner-merge' : 'managed';
    p = { key, value, before, verb, route: 'sd-db', icon: 'terminal', title: `${key} ${before} → ${value}`, cmd: `${SDDB} repo ${verb} ${shq(r.path)} ${value}`,
      note: key === 'runner_merge' && value === 'manual' ? 'Items for this repo will stop at ready_to_send for you.' : key === 'runner_merge' ? 'The runner may queue merges for this repo through the exclusive lane.' : 'Writes one column; no new row.' };
  } else if (key === 'severity_floor' || key === 'automatic_deep') {
    if (value === String(key === 'severity_floor' ? rv.severity_floor || '' : rv.automatic_deep)) return withdraw(key);
    const diff = reviewDiff(rv.file || '', body => {
      if (key === 'severity_floor') { if (value) body.severity_floor = value; else delete body.severity_floor; return; }
      const copilot = body.copilot_review && typeof body.copilot_review === 'object' && !Array.isArray(body.copilot_review) ? body.copilot_review : {};
      body.copilot_review = { ...copilot, automatic_deep: value === 'true' };
    });
    if (!diff) { shell.toast('Not proposed: .github/sd-review.json is not valid JSON; fix it in the checkout first'); return; }
    p = { key, value, route: 'file', icon: 'git-pull-request', title: `.github/sd-review.json · ${key} → ${value || 'unset'}`, diff, branch: `settings/sd-review-${key.replace('_', '-')}`,
      note: 'Ships as a pull request through sd-ship prepare, then the review lane. Nothing changes until it merges.' };
  }
  if (p) addProposal(p);
}
// build (review, PR #50): a review-file change is made on the parsed file, checked to parse again, and shown as a line
// diff of the two texts, so a proposal is always valid JSON. The new text is JSON.stringify's two-space layout, the
// layout these files use. null when the file is not a JSON object.
function reviewDiff(text, change) {
  let body;
  try { body = JSON.parse(text); } catch { return null; }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
  change(body);
  const after = JSON.stringify(body, null, 2);
  JSON.parse(after);
  return lineDiff(text.replace(/\n$/, '').split('\n'), after.split('\n'));
}
// The longest common run of lines is kept; the rest is a del or an add (files are at most 16 KiB, REVIEW_BYTES).
function lineDiff(a, b) {
  const n = a.length, m = b.length, L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(['', a[i]]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) out.push(['del', a[i++]]); else out.push(['add', b[j++]]);
  }
  while (i < n) out.push(['del', a[i++]]);
  while (j < m) out.push(['add', b[j++]]);
  return out;
}
// build: the starter file names the schema another checkout's file names, read by the server, rather than a URL typed here.
const SCHEMA = () => REPOS.map(r => r.review?.schema).find(Boolean) || '';
const ROUTE = { 'sd-db': ['sd-db change', 'sd-db · local'], file: ['file change', 'file · PR'] };
function addProposal(p) {
  const r = REPOS.find(x => x.path === repoParam);
  p.id = `prop:${p.key}`; p.type = ROUTE[p.route][0]; p.label = p.title; p.repo = r;
  const i = pending.findIndex(x => x.key === p.key);
  if (i >= 0 && pending[i].title === p.title) return;
  if (i >= 0) pending.splice(i, 1);
  shell.commands.put(p); pending.unshift(p);
  markPending(p.key);
  showProposals(true);
}
function dropProposal(p) {
  const i = pending.indexOf(p); if (i >= 0) pending.splice(i, 1);
  renderRepos(); pending.length ? showProposals() : defaultDetails();
}
function diffHTML(d) { return html`<pre class="diff">${d.map(([k, l]) => html`<span class="${k}">${k === 'add' ? '+ ' : k === 'del' ? '− ' : k === 'hd' ? '' : '  '}${l}</span>`)}</pre>`; }
function showProposals(open) {
  const r = REPOS.find(x => x.path === repoParam);
  put(details, html`<p class="kind">${I('settings')} Repo settings · ${r.name}</p><h2>${plural(pending.length, 'proposal')}</h2>
    ${pending.map(p => html`<div class="proposal"><header>${I(p.icon)}<span class="label">${ROUTE[p.route][1]}</span></header>
      <div class="body"><b>${p.title}</b>${p.diff ? diffHTML(p.diff) : ''}<span class="why">${p.note}</span>${shell.commands.bar(p.id)}</div></div>`)}`);
  swap();
  shell.commands.select(pending[0]?.id);
  shell.setContext(`${r.name} settings`);
  if (open) shell.openPane('tab-details');
}
document.getElementById('view-repos').addEventListener('change', e => {
  const c = e.target.closest('select[data-set], input[data-set]'); if (c) propose(c.dataset.set, c.value.trim());
});
document.getElementById('view-repos').addEventListener('click', e => {
  const b = e.target.closest('.segctl [data-set]');
  if (b) propose(b.dataset.set, b.dataset.v);
  if (e.target.closest('[data-create-review]')) {
    addProposal({ key: 'review-file', route: 'file', icon: 'git-pull-request', title: '.github/sd-review.json · new file', branch: 'settings/sd-review-json',
      diff: [['hd', '@@ new file .github/sd-review.json @@'], ['add', '{'], ...(SCHEMA() ? [['add', `  "$schema": "${SCHEMA()}",`]] : []), ['add', '  "severity_floor": "high"'], ['add', '}']],
      note: 'A starting file with the floor the managed repos use. Review it in the PR; nothing else changes until it merges.' });
  }
});

// ---------- Writes (build) ----------
// landing(): the run returns the write's promise, the shell's run contract (shell.js, bulk:start; sd:2124). The shell toasts
// the text once the write landed, offers Undo only then, and folds a bulk group into one toast. `inverse` gets what the
// write answered and reverses this write, once. A 409 reads the document again, since the row it named has moved.
function landing(promise, msg, inverse) {
  const stale = err => { if (err.stale) load(); throw err; };
  return promise.then(v => {
    let spent = false;
    const undo = inverse && (() => { if (spent) return Promise.resolve(false); spent = true; return inverse(v).catch(stale); });
    return { text: msg(), undo };
  }, stale);
}
// The command's undo: the shell passes what the run answered.
const undoOf = (o, r) => r && r.undo ? r.undo() : false;
async function setRepo(field, r, value, before) {
  await post(`/api/repos/${field}`, { path: r.path, value, before });
  await load();
}
const wordOf = (f, r) => f === 'runner-merge' ? r.merge : r.managed;
const other = (f, v) => f === 'runner-merge' ? (v === 'auto' ? 'manual' : 'auto') : (v === 'yes' ? 'no' : 'yes');
// A flip's Undo writes the old value back, with the value this flip wrote as its before.
function flipRepo(field, o) {
  const r = o.row, before = wordOf(field, r), value = other(field, before);
  return landing(setRepo(field, r, value, before), () => `${colOf(field)} ${value} · ${o.label}`,
    () => setRepo(field, REPOS.find(x => x.path === o.path) || r, before, value));
}
const colOf = field => field === 'runner-merge' ? 'runner_merge' : 'managed';
const asgOf = n => [...(LANE?.live || []), ...(LANE?.merges || []), ...(ASG?.latest || [])].find(a => a.id === n);
async function runner(o, verb) {
  const a = asgOf(o.n); if (!a) throw new Error(`assignment #${o.n} was not read`);
  const answer = await post(`/api/runner/${o.n}/${verb}`, { revision: a.revision });
  await load();
  return answer;
}
// build (local review of PR #50): requeue's Undo cancels the queued attempt the requeue made, with the revision the requeue
// answered and never a later read's. Once the runner claimed it, or anything else moved it, Undo refuses and Cancel (which
// asks first) is the way to stop it; the server refuses that revision too.
async function unrequeue(o, answer) {
  if (!answer || answer.status !== 'queued' || !answer.revision) throw new Error('the requeue answered no queued attempt to reverse');
  const now = asgOf(o.n);
  if (!now || now.status !== 'queued' || now.revision !== answer.revision)
    throw new Error(`assignment #${o.n} is ${now ? now.status : 'not read'} now; Undo cancels only the queued attempt the requeue made, so use Cancel`);
  await post(`/api/runner/${o.n}/cancel`, { revision: answer.revision });
  await load();
}

// ---------- Commands (products/system/commands.md) ----------
function putObjects() {
  const C = shell.commands;
  ALL.forEach(r => C.put({ id: `repo:${r.path}`, type: 'repo', label: r.name, path: r.path, row: r }));
  const asg = a => C.put({ id: `${a.role === 'merge' ? 'merge' : 'asg'}:${a.id}`, type: 'assignment', label: `#${a.id} ${a.title || a.role}`, n: a.id, status: a.status, repo: a.repo || 'no repo', item: a.item });
  [...(LANE?.merges || []), ...(LANE?.live || []), ...(ASG?.latest || [])].forEach(asg);
  (LANE?.ready || []).forEach(t => C.put({ id: `ready:${t.id}`, type: 'item', label: `#${t.id} ${t.title}`, item: t.id }));
  if (SESS) C.put({ id: 'wt:abandoned', type: 'worktrees', label: `${SESS.abandoned} abandoned worktrees`, n: SESS.abandoned });
  SERVICES.forEach(s => C.put({ id: `svc:${s.label}`, type: 'service', label: s.name, name: s.label, running: s.state === 'running', revision: s.revision, caps: s.capabilities || {} }));
  CRON.forEach(c => C.put({ id: `cron:${c.name}`, type: 'job', label: c.name, job: c.name, name: c.name, service: c.service, failed: c.rank === 'warning', exit: c.last_exit, revision: c.revision, caps: c.capabilities || {} }));
}
const capWhy = (o, action, fallback) => { const c = o.caps?.[action]; return !c ? fallback : c.allowed || c.reason || fallback; };
function registerCommands() {
  const C = shell.commands;
  C.register(
    // Repo rows: the two sd-db verbs, each an Undo-able flip of one column. build: they post /api/repos/<verb>.
    { id: 'repo.runner-merge', on: 'repo', label: 'Switch runner-merge', key: 'm', risk: 'undo', bulk: true, primary: o => o.row.managed === 'yes', when: o => o.row.registered || 'not registered in sd-db: sd-db.sh repo add registers it first',
      cli: o => `${SDDB} repo runner-merge ${shq(o.path)} ${o.row.merge === 'auto' ? 'manual' : 'auto'}`, executes: true,
      run: o => flipRepo('runner-merge', o), undo: undoOf },
    { id: 'repo.managed', on: 'repo', label: 'Switch managed', key: 'g', risk: 'undo', bulk: true, primary: o => o.row.managed !== 'yes', when: o => o.row.registered || 'not registered in sd-db: sd-db.sh repo add registers it first',
      cli: o => `${SDDB} repo managed ${shq(o.path)} ${o.row.managed === 'yes' ? 'no' : 'yes'}`, executes: true,
      run: o => flipRepo('managed', o), undo: undoOf },
    // Pull is copy only (repos_screen.py): the dashboard never pulls a checkout it did not open. when() carries v1's refusals.
    { id: 'repo.pull', on: 'repo', label: 'Pull', key: 'l', risk: 'safe', executes: false, primary: o => o.row.git?.state === 'behind',
      when: pullWhy, cli: o => `git -C ${shq(o.row.git ? o.row.git.path : o.path)} pull --ff-only`, run: () => 'Copy it into a terminal: the dashboard never pulls' },
    { id: 'sddb.run', on: 'sd-db change', label: 'Run', key: 'u', risk: 'undo', primary: () => true, cli: o => o.cmd, executes: true,
      run: o => { dropProposal(o);
        return landing(setRepo(o.verb, o.repo, o.value, o.before), () => `Ran locally · ${o.title}`, () => setRepo(o.verb, o.repo, o.before, o.value)); },
      undo: undoOf },
    // build: copy only. sd-ship prepare opens a branch and a pull request, which the dashboard does not start.
    { id: 'file.prepare', on: 'file change', label: 'Prepare', key: 'p', risk: 'safe', primary: () => true, executes: false,
      cli: o => `sd-ship prepare --no-item --review-id ${o.branch.replace('/', '-')} --path .github/sd-review.json --title ${shq(o.title)}`,
      run: () => 'Copy it into a terminal in the checkout: the dashboard does not open pull requests' },
    ...Object.values(ROUTE).map(([type]) => ({ id: `${type.split(' ')[0].toLowerCase().replace('-', '')}.withdraw`, on: type, label: 'Withdraw', risk: 'safe', executes: false,
      run: o => { withdraw(o.key, true); return `Proposal withdrawn · ${o.title}`; } })),
    // Assignments. build: requeue and cancel post the runner routes with the assignment's queue revision.
    // Requeue's Undo is sd runner cancel while the run is still queued (commands.md).
    { id: 'asg.requeue', on: 'assignment', label: 'Requeue', key: 'q', risk: 'undo', bulk: true, primary: o => o.status === 'blocked',
      when: o => o.status === 'blocked' || `the assignment is ${o.status}`, cli: o => `sd runner requeue ${o.n}`,
      run: o => landing(runner(o, 'requeue'), () => `Requeued · #${o.n}. The runner starts it on its next tick.`, answer => unrequeue(o, answer)),
      undo: undoOf },
    { id: 'asg.cancel', on: 'assignment', label: 'Cancel', key: 'x', risk: 'confirm',
      when: o => ['queued', 'running'].includes(o.status) || `the assignment is ${o.status}, not queued or running`, cli: o => `sd runner cancel ${o.n}`,
      consequence: o => `Stops assignment #${o.n} in ${o.repo} and releases its lease.`,
      run: o => landing(runner(o, 'cancel'), () => `Cancel requested · #${o.n}`) },
    { id: 'asg.get', on: 'assignment', label: 'Show assignment', key: 'o', risk: 'safe', primary: o => o.status !== 'blocked', cli: o => `sd assignments get ${o.n}`, executes: false,
      run: o => { selectRow(`${o.id.split(':')[0]}:${o.n}`, true); return `Assignment #${o.n} shown in Details`; } },
    // build: the item opens in Tasks, whose Details read it.
    { id: 'item.show', on: 'item', label: 'Open item', key: 'o', risk: 'safe', primary: () => true, cli: o => `sd task show ${o.item}`, executes: false,
      run: o => { location.href = `/tasks?row=${o.item}`; return `#${o.item} opens in Tasks`; } },
    { id: 'wt.prune', on: 'worktrees', label: 'Prune', key: 'p', risk: 'confirm', primary: () => true, when: () => 'no CLI verb: sd worktree has restore and resume only', cli: () => 'sd sessions prune --abandoned',
      consequence: o => `Removes ${o.n} worktree registrations whose directories are gone. Branches and files stay.` },
    // Services on this Mac. build: each posts /api/services/<label>/<action> with the service's revision; when() is its capability.
    { id: 'svc.restart', on: 'service', label: 'Restart', key: 't', risk: 'confirm', primary: () => true, when: o => capWhy(o, 'restart', 'not read'), cli: o => `sd services restart ${o.name}`,
      consequence: o => `Stops and starts ${o.name}. Anything it serves drops for a few seconds.`, run: o => landing(service(o, 'restart'), () => `Restart requested · ${o.label}`) },
    { id: 'svc.stop', on: 'service', label: 'Stop', key: 's', risk: 'confirm', when: o => capWhy(o, 'stop', o.running || 'not running'), cli: o => `sd services stop ${o.name}`,
      consequence: o => `Stops ${o.name} through launchd. Start brings it back.`, run: o => landing(service(o, 'stop'), () => `Stop requested · ${o.label}`) },
    { id: 'svc.start', on: 'service', label: 'Start', key: 'a', risk: 'safe', when: o => capWhy(o, 'start', !o.running || 'already running'), cli: o => `sd services start ${o.name}`,
      run: o => landing(service(o, 'start'), () => `Start requested · ${o.label}`) },
    // launchd calendar jobs. Retry is `sd jobs retry`, which sends the kickstart (commands.md, one declaration). build: it posts
    // /api/jobs/<name>/retry; operations.inventory's capability is the off reason. Exit 127 is "command not found": Retry is off.
    { id: 'jobs.retry', on: 'job', label: 'Retry', key: 't', risk: 'safe', bulk: true, primary: o => o.failed,
      when: o => o.exit === 127 ? 'exit 127: command not found; fix the path first' : capWhy(o, 'retry', o.failed || 'no failed run to retry'),
      cli: o => `sd jobs retry ${o.job}`, sends: o => `launchctl kickstart ${o.service}`,
      run: o => landing(job(o, 'retry'), () => `Retry started · ${o.job}`) },
    { id: 'job.print', on: 'job', label: 'Print', key: 'l', risk: 'safe', primary: o => o.exit === 127, executes: false, cli: o => `launchctl print ${o.service}`,
      run: () => 'Copy it into a terminal: the dashboard shows the launchd record in Operations › Jobs' },
  );
}
async function service(o, action) { await post(`/api/services/${encodeURIComponent(o.name)}/${action}`, { revision: o.revision }); await load(); }
async function job(o, action) { await post(`/api/jobs/${encodeURIComponent(o.job)}/${action}`, { revision: o.revision }); await load(); }
document.addEventListener('shell:open', e => {
  const id = e.detail;
  if (id.startsWith('repo:') && repoParam) shell.openPane('tab-details');
  else if (!id.startsWith('prop:')) selectRow(id, true);
});
document.addEventListener('shell:picked', e => document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))));

// ---------- Ship lane ----------
const LS = { sort: 'id', dir: -1 };
const asgRepo = a => (a.repo || 'no repo').replace(/^~\/repos\//, '');
function renderLane() {
  const el = document.getElementById('view-lane');
  if (!LANE) { put(el, unknown('The lane was not read', why('lane'), 'sd runner status')); return; }
  const { free } = tokens();
  const hb = LANE.heartbeat, queued = LANE.live.filter(a => a.status === 'queued'), running = LANE.live.filter(a => a.status !== 'queued');
  const rows = LANE.merges.filter(m => free.every(f => (asgRepo(m) + ' ' + (m.title || '')).toLowerCase().includes(f)));
  const lease = running.filter(a => a.role === 'merge');
  put(el, html`
    <ul class="strip" aria-label="Lane state">
      <li><span class="label">Runner</span><span class="val">${hb.ok ? html`<span class="g-ok">●</span> healthy${hb.interval_seconds ? ` · ${hb.interval_seconds} s tick` : ''}` : html`<span class="g-caution">▲</span> ${hb.reason || 'heartbeat stale'}`}</span></li>
      <li><span class="label">Queue</span><span class="val">${queued.length} queued · ${running.length} running</span></li>
      <li><span class="label">Claim</span><span class="val">${lease.length ? `${lease.length} merge running` : 'no merge running'}</span></li>
      <li><span class="label">Runner merge</span><span class="val">${REPOS.filter(r => r.merge === 'auto').length} repos auto</span></li>
    </ul>
    <div class="sec-head"><h2 id="queue-h">Queue <button class="help" type="button" aria-label="Help: merge lane" data-help="<b>One writer per repo.</b> A merge or serial assignment takes an exclusive lease on its repo; nothing else in that repo starts until the lease is released. The runner queues a merge only for runner_merge=auto repos, after a done author run with a reviewed head.">${I('circle-help')}</button></h2><p class="tally">assignment table · ${hhmm(READ)} UTC</p></div>
    ${LANE.live.length ? html`<table class="ledger" aria-labelledby="queue-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Assignment</th><th scope="col">Repo</th><th scope="col">Role</th><th scope="col">Since</th>${ACTH}</tr></thead>
      <tbody>${LANE.live.map(a => html`<tr data-id="asg:${a.id}"><td class="g g-queued">◌<span class="sr">${a.status}</span></td><td class="what"><button type="button">#${a.id} ${a.title || a.role}</button></td><td class="d mono" data-k="repo">${asgRepo(a)}</td><td class="d mono">${a.role} · ${a.status}</td><td class="d mono"><time class="rel" datetime="${a.started || a.queued_at || ''}"></time></td>${ACT('asg:' + a.id)}</tr>`)}</tbody></table>`
      : html`<p class="empty"><span class="g-queued" aria-hidden="true">◌</span> Nothing queued or running.${LANE.merges[0]?.ended ? html` The last merge ended <time class="rel" datetime="${LANE.merges[0].ended}"></time>.` : ''}</p>`}
    <div class="sec-head"><h2 id="ready-h">Waiting on you</h2><p class="tally">${LANE.ready.length} ready_to_send · not lane candidates</p></div>
    ${LANE.ready.length ? html`<table class="ledger" aria-labelledby="ready-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Item</th><th scope="col">Repo</th>${ACTH}</tr></thead>
      <tbody>${LANE.ready.map(t => html`<tr data-id="ready:${t.id}"><td class="g g-queued">◌<span class="sr">queued</span></td><td class="what"><button type="button">#${t.id} ${t.title}</button></td><td class="d mono" data-k="repo">${asgRepo(t)}</td>${ACT('ready:' + t.id)}</tr>`)}</tbody></table>`
      : html`<p class="empty">No item waits at ready_to_send.</p>`}
    <div class="sec-head"><h2 id="merged-h">Recent merges</h2><p class="tally">latest ${LANE.merges.length} merge assignments</p></div>
    ${rows.length ? html`<table class="ledger" aria-labelledby="merged-h"><thead>${sortHead([[null, html`<span class="sr">State</span>`, 'g'], ['id', 'Assignment'], [null, 'Repo'], ['at', 'Ended'], [null, 'Status'], [null, html`<span class="sr">Actions</span>`, 'act']], LS)}</thead>
      <tbody>${rows.slice().sort((a, b) => (LS.sort === 'id' ? a.id - b.id : String(a.ended || '').localeCompare(String(b.ended || ''))) * LS.dir).map(m => { const st = m.status === 'done' ? 'ok' : m.status === 'blocked' ? 'caution' : 'queued'; return html`<tr data-id="merge:${m.id}"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${st}</span></td>
        <td class="what"><button type="button">#${m.id} ${m.title || 'merge'}</button></td><td class="d mono" data-k="repo">${asgRepo(m)}</td><td class="d mono"><time class="rel" datetime="${m.ended || ''}" data-empty="never"></time></td>
        <td class="d mono">${m.status}</td>${ACT('merge:' + m.id)}</tr>`; })}</tbody></table>`
      : html`<p class="empty">${LANE.merges.length ? html`No merge matches “${text.q}”. <button class="linkbtn" type="button" data-clear-q>Clear filters</button>` : 'No merge assignment has run.'}</p>`}`);
}
document.getElementById('view-lane').addEventListener('click', e => {
  if (e.target.closest('a')) return;
  const s = e.target.closest('[data-sort]'); if (s) { LS.dir = LS.sort === s.dataset.sort ? -LS.dir : -1; LS.sort = s.dataset.sort; renderLane(); return setURL(); }
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) selectRow(tr.dataset.id, true);
});

// ---------- Sessions and agents ----------
let histFilter = null;
const ASTATE = { blocked: 'caution', queued: 'queued', running: 'queued', ending: 'queued', done: 'ok', cancelled: 'ok' };
function renderSessions() {
  const el = document.getElementById('view-sessions');
  const { chips, free } = tokens();
  const latest = ASG ? ASG.latest : [];
  const rows = latest.filter(a => (!histFilter || a.status === histFilter) && (!chips.agent || a.provider === chips.agent)
    && free.every(f => (asgRepo(a) + ' ' + (a.provider || '') + ' ' + (a.title || '')).toLowerCase().includes(f)));
  const hist = ASG ? Object.entries(ASG.history) : [], total = hist.reduce((s, h) => s + h[1], 0), max = Math.max(1, ...hist.map(h => h[1]));
  put(el, html`
    <div class="sec-head"><h2 id="agents-h">sd-* processes <button class="help" type="button" aria-label="Help: processes" data-help="<b>The fleet's process read.</b> The sd-* commands running on this Mac, as Operations › Sessions lists them. Agent processes by name (claude, codex) are not read yet.">${I('circle-help')}</button></h2><p class="tally">${SESS ? html`<span>${SESS.processes.length} running</span><span>fleet · ${hhmm(READ)} UTC</span>` : ''}</p></div>
    ${!SESS ? unknown('Sessions were not read', why('sessions'), 'fleet sessions')
      : SESS.processes.length ? html`<table class="ledger" aria-labelledby="agents-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Command</th><th scope="col">Running for</th><th scope="col">PID</th></tr></thead>
      <tbody>${SESS.processes.map((p, i) => html`<tr data-id="proc:${i}"><td class="g g-ok">●<span class="sr">ok</span></td><td class="what"><button type="button">${p.command}</button></td><td class="d mono" data-k="up">${p.elapsed}</td><td class="d num">${p.pid}</td></tr>`)}</tbody></table>`
      : html`<p class="empty">${SESS.processes_error ? html`<span class="g-unknown" aria-hidden="true">▨</span> ${SESS.processes_error}` : 'No sd-* command is running.'}</p>`}

    <div class="sec-head"><h2 id="runner-h">Runner assignments</h2><p class="tally">${ASG ? html`<span>${total} in history</span><span>latest ${latest.length} shown</span><span>assignment table · ${hhmm(READ)} UTC</span>` : ''}</p></div>
    ${!ASG ? unknown('Assignments were not read', why('assignments'), 'sd runner list') : html`
    <div class="bars" role="group" aria-label="Assignment history by outcome. Select a bar to filter.">
      ${hist.map(([k, n]) => html`<button class="bar" type="button" data-hist="${k}" aria-pressed="${String(histFilter === k)}"><span>${k}</span><svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true"><rect class="track" x="0" y="0" width="100" height="10"/><rect class="fill" x="0" y="0" width="${(n / max * 100).toFixed(1)}" height="10"/></svg><span class="n">${n}</span></button>`)}
    </div>
    <table class="sr"><caption>Assignment history</caption><tbody>${hist.map(([k, n]) => html`<tr><th scope="row">${k}</th><td>${n}</td></tr>`)}</tbody></table>
    ${rows.length ? html`<table class="ledger" aria-labelledby="runner-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Assignment</th><th scope="col">Repo</th><th scope="col">Agent</th><th scope="col">Ended</th>${ACTH}</tr></thead>
      <tbody>${rows.map(a => { const st = ASTATE[a.status] || 'unknown'; return html`<tr data-id="asg:${a.id}"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${a.status}</span></td><td class="what"><button type="button">#${a.id} ${a.title || a.role}</button></td><td class="d mono" data-k="repo">${asgRepo(a)}</td><td class="d mono" data-k="agent">${a.provider || '—'}</td><td class="d mono"><time class="rel" datetime="${a.ended || ''}" data-empty="never"></time></td>${ACT('asg:' + a.id)}</tr>`; })}</tbody></table>`
      : html`<p class="empty">${histFilter ? `No ${histFilter} assignment among the latest ${latest.length}.` : text.q ? `No assignment matches “${text.q}”.` : 'No assignment has run.'}</p>`}`}

    <div class="sec-head"><h2 id="wt-h">Worktrees</h2><p class="tally">fleet sessions · ${hhmm(READ)} UTC</p></div>
    ${SESS ? html`<table class="ledger" aria-labelledby="wt-h"><tbody>
      <tr data-id="wt:abandoned"><td class="g g-${SESS.abandoned ? 'caution' : 'ok'}">${SESS.abandoned ? '▲' : '●'}<span class="sr">${SESS.abandoned ? 'caution' : 'ok'}</span></td><td class="what"><button type="button">${SESS.abandoned} abandoned worktrees</button></td><td class="d mono">registered, directory gone</td>${ACT('wt:abandoned')}</tr>
      <tr data-id="wt:live"><td class="g g-ok">●<span class="sr">ok</span></td><td class="what"><button type="button">${SESS.registered - SESS.abandoned} live worktrees</button></td><td class="d mono">of ${SESS.registered} registered</td><td class="act end"></td></tr>
    </tbody></table>` : unknown('Worktrees were not read', why('sessions'))}`);
}
document.getElementById('view-sessions').addEventListener('click', e => {
  const b = e.target.closest('[data-hist]'); if (b) { histFilter = histFilter === b.dataset.hist ? null : b.dataset.hist; return renderSessions(); }
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) selectRow(tr.dataset.id, true);
});

// ---------- Deploys ----------
const SVCSTATE = s => s.state === 'running' ? 'ok' : s.state === 'failed' ? 'warning' : ['unknown'].includes(s.state) ? 'unknown' : 'queued';
function renderDeploys() {
  const el = document.getElementById('view-deploys');
  const shown = SERVICES.filter(s => tokens().free.every(f => (s.name + ' ' + s.label + ' ' + s.category).toLowerCase().includes(f)));
  put(el, html`
    <div class="sec-head"><h2 id="dep-h">Deploys <button class="help" type="button" aria-label="Help: Deploys" data-help="<b>Unknown, not empty.</b> Nothing in the fleet records a deploy yet, and the dashboard has no deploy collector. The services below are what is installed on this Mac now.">${I('circle-help')}</button></h2></div>
    ${unknown('No deploy source', 'The dashboard has no deploy collector, so this list cannot fill. The services below are what runs on this Mac now.')}
    <div class="sec-head"><h2 id="svc-h">Services on this Mac</h2><p class="tally">launchd · ${hhmm(READ)} UTC</p></div>
    ${!DOC.services ? unknown('Services were not read', why('services'), 'sd services list')
      : shown.length ? html`<table class="ledger" aria-labelledby="svc-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Service</th><th scope="col">What</th><th scope="col">PID</th><th scope="col">Previous exit</th>${ACTH}</tr></thead>
      <tbody>${shown.map(s => { const st = SVCSTATE(s); return html`<tr data-id="svc:${s.label}"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${s.state}</span></td><td class="what"><button type="button">${s.name}</button></td><td class="d mono">${s.category} · ${s.scope}</td><td class="d num" data-k="pid">${s.pid ?? '—'}</td><td class="d num" data-k="exit">${s.last_signal != null ? `signal ${s.last_signal}` : s.last_exit ?? '—'}</td>${ACT('svc:' + s.label)}</tr>`; })}</tbody></table>`
      : html`<p class="empty">${SERVICES.length ? `No service matches “${text.q}”.` : 'No installed service was found.'}</p>`}`);
}
document.getElementById('view-deploys').addEventListener('click', e => { if (e.target.closest('.rowact')) return; const tr = e.target.closest('tr[data-id]'); if (tr) selectRow(tr.dataset.id, true); });

// ---------- Schedules ----------
const SS = { sort: 'rank', dir: 1, page: 1, size: 25 };
const RANK = { warning: 0, caution: 1, unknown: 2, ok: 3 };
function pageAttention() {
  const failed = CRON.filter(c => c.rank === 'warning').length;
  window.PAGE_ATTENTION = { state: failed ? 'warning' : 'ok', n: failed, what: 'scheduled jobs failed' };
  window.shell?.attention?.(window.PAGE_ATTENTION);
}
function renderSchedules() {
  const el = document.getElementById('view-schedules');
  if (!DOC.jobs) { put(el, unknown('Jobs were not read', why('jobs'), 'sd jobs list')); return; }
  const { chips, free } = tokens();
  const rows = CRON.filter(c => (!chips.state || (chips.state === 'failed' ? c.rank === 'warning' : c.state === chips.state)) && free.every(f => c.name.toLowerCase().includes(f)))
    .sort((a, b) => SS.sort === 'rank' ? (RANK[a.rank] - RANK[b.rank]) || a.next.localeCompare(b.next)
      : SS.sort === 'name' ? a.name.localeCompare(b.name) * SS.dir : a.next.localeCompare(b.next) * SS.dir);
  const failed = CRON.filter(c => c.rank === 'warning').length;
  clampPage(rows.length, SS);
  const start = (SS.page - 1) * SS.size;
  put(el, html`
    <div class="sec-head"><h2 id="sch-h">Schedules <button class="help" type="button" aria-label="Help: Schedules" data-help="<b>launchd calendar jobs, ranked.</b> Failed first, then by next run. Times are this browser's clock; launchd reads the Mac's.">${I('circle-help')}</button></h2>
      <p class="tally"><span class="g-warning">■ ${failed} failed</span><span class="g-ok">● ${CRON.length - failed} not failed</span><span>launchd · ${hhmm(READ)} UTC</span></p></div>
    <div class="filters"><div class="fgroup" role="group" aria-label="State"><span class="label">State</span><button class="chip" type="button" data-state-f="failed" aria-pressed="${String(chips.state === 'failed')}">failed</button></div>
      <p class="fsum"><span>${rows.length} of ${CRON.length}</span><span class="g-unknown">▨ cloud routines not read</span></p></div>
    ${rows.length ? html`<table class="ledger" aria-labelledby="sch-h"><thead>${sortHead([[null, html`<span class="sr">State</span>`, 'g'], ['name', 'Job'], [null, 'Schedule'], [null, 'State'], ['next', 'Next run'], [null, 'Exit', 'num'], [null, html`<span class="sr">Actions</span>`, 'act']], SS)}</thead>
      <tbody>${rows.slice(start, start + SS.size).map((c, i, a) => html`<tr data-id="cron:${c.name}"${i && a[i - 1].rank !== c.rank ? html` class="band-start"` : ''}><td class="g g-${c.rank}">${GLYPH[c.rank]}<span class="sr">${c.rank}</span></td>
        <td class="what"><button type="button">${c.name}</button></td><td class="d mono">${human(c.schedule)}</td><td class="d mono">${c.state}</td><td class="d mono" data-k="next"><time class="rel" datetime="${c.next}" data-future data-empty="never"></time></td>
        <td class="d num" data-k="exit">${c.last_signal != null ? `sig ${c.last_signal}` : c.last_exit === null || c.last_exit === undefined ? html`<span class="no">—</span>` : c.last_exit}</td>${ACT('cron:' + c.name)}</tr>`)}</tbody></table>`
      : html`<p class="empty">${CRON.length ? html`No job matches. <button class="linkbtn" type="button" data-clear-q>Clear filters</button>` : 'No launchd calendar job is installed.'}</p>`}`);
  if (rows.length > SS.size || SS.size !== 25) el.querySelector('table').after(pager(rows.length, SS, renderSchedules));
}
document.getElementById('view-schedules').addEventListener('click', e => {
  const s = e.target.closest('[data-sort]'); if (s) { SS.dir = SS.sort === s.dataset.sort ? -SS.dir : 1; SS.sort = s.dataset.sort; renderSchedules(); return setURL(); }
  const f = e.target.closest('[data-state-f]');
  if (f) { const inp = document.getElementById('shift'); inp.value = /state:failed/.test(inp.value) ? inp.value.replace(/\s*state:failed/, '').trim() : (inp.value + ' state:failed').trim(); text.q = inp.value; SS.page = 1; renderSchedules(); return setURL(); }
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) selectRow(tr.dataset.id, true);
});
document.querySelector('main').addEventListener('click', e => { if (e.target.closest('[data-clear-q]')) { document.getElementById('shift').value = ''; text.q = ''; applyText(); } });

// ---------- Details pane ----------
function defaultDetails() {
  if (view === 'repos' && repoParam && pending.length) return showProposals();
  if (view === 'repos') {
    const r = repoParam && REPOS.find(x => x.path === repoParam);
    put(details, html`<p class="kind">${I('settings')} ${r ? 'Repo settings' : 'Repos'}</p><h2>${r ? r.name : 'How an edit lands'}</h2>
      <ul class="legend">
        <li><span class="route">${I('terminal')}sd-db · local</span>runner_merge and managed. The proposal shows the sd-db command; Run applies it here, with Undo.</li>
        <li><span class="route">${I('git-pull-request')}file · PR</span><code>.github/sd-review.json</code>. The proposal shows the diff and the sd-ship line to copy.</li>
        <li><span class="route">${I('shield-check')}GitHub · API</span>Protection as the nightly collector read it. The dashboard does not change it.</li>
      </ul>
      ${r ? html`<h3>Act</h3>${shell.commands.bar('repo:' + r.path)}` : ''}`);
    if (r) shell.commands.select('repo:' + r.path);
    shell.setContext(r ? `${r.name} settings` : '');
    shell.suggest(r ? [`What would change if ${r.name} went runner_merge manual?`, `Compare ${r.name}'s protection with system's`, 'Which settings here drift from the managed repos?'] : ['Which managed repos lack an sd-review.json?', 'Which repos are auto-merge but unprotected on GitHub?', 'List repos registered but never managed']);
    return swap();
  }
  const first = document.querySelector(`#view-${view} tr[data-id]`);
  if (first) selectRow(first.dataset.id, false);
}
function asgDetails(a, act) {
  const st = ASTATE[a.status] || 'unknown';
  return html`<p class="kind"><span class="g-${st}">${GLYPH[st]}</span> Assignment · ${a.status}</p><h2>${a.title || a.role}</h2><dl><dt>Assignment</dt><dd>#${a.id}${a.item ? ` · item #${a.item}` : ''}</dd><dt>Repo</dt><dd>${a.repo || '—'}</dd><dt>Role</dt><dd>${a.role} · ${a.lane} lane</dd><dt>Agent</dt><dd>${a.provider || '—'}</dd>
    <dt>Queued</dt><dd><time class="rel" datetime="${a.queued_at || ''}" data-long data-empty="never"></time></dd><dt>Started</dt><dd><time class="rel" datetime="${a.started || ''}" data-long data-empty="never"></time></dd><dt>Ended</dt><dd><time class="rel" datetime="${a.ended || ''}" data-long data-empty="never"></time></dd></dl>${act}`;
}
function selectRow(id, open) {
  markRow(id);
  const [kind, key] = [id.slice(0, id.indexOf(':')), id.slice(id.indexOf(':') + 1)];
  const act = shell.commands.get(id) ? html`<h3>Act</h3>${shell.commands.bar(id)}` : '';
  let h = '';
  if (kind === 'cron') {
    const c = CRON.find(x => x.name === key); if (!c) return;
    h = html`<p class="kind"><span class="g-${c.rank}" aria-hidden="true">${GLYPH[c.rank]}</span> Schedule · ${c.state}</p><h2>${c.name}</h2>
      <dl><dt>Service</dt><dd>${c.service || '—'}</dd><dt>Schedule</dt><dd>${human(c.schedule)}</dd><dt>Next run</dt><dd><time class="rel" datetime="${c.next}" data-future data-long data-empty="never"></time></dd><dt>Last exit</dt><dd>${c.last_signal != null ? `signal ${c.last_signal}` : c.last_exit ?? 'not run since load'}${c.last_exit === 127 ? ' · command not found' : ''}</dd></dl>
      ${c.last_exit === 127 ? html`<p class="why">Exit 127: the shell could not find the command. A retry fails the same way until the path is fixed.</p>` : ''}${act}`;
    shell.suggest([`Why did ${c.name} exit ${c.last_exit ?? 0}?`, `What does ${c.name} do and who reads its output?`, 'Which schedules overlap tonight?']);
  } else if (kind === 'merge' || kind === 'asg') {
    const a = asgOf(+key); if (!a) return;
    h = asgDetails(a, act);
    shell.suggest([`Why is #${a.id} ${a.status}?`, `What else is blocked in ${asgRepo(a).split('/').pop()}?`, 'Group blocked runs by cause']);
  } else if (kind === 'ready') {
    const t = LANE.ready.find(x => String(x.id) === key); if (!t) return;
    h = html`<p class="kind"><span class="g-queued">◌</span> Item · ready_to_send</p><h2>${t.title}</h2><dl><dt>Item</dt><dd>#${t.id}</dd><dt>Repo</dt><dd>${t.repo || '—'}</dd><dt>Lane</dt><dd>not a candidate: it waits on you</dd></dl>${act}`;
  } else if (kind === 'repo') {
    const r = ALL.find(x => x.path === key); if (!r) return;
    const g = r.git, pw = pullWhy({ row: r });
    const git = g ? html`<h3>Git state</h3><dl><dt>Branch</dt><dd>${g.branch || '?'}</dd><dt>Default</dt><dd>${g.default || '—'}</dd><dt>Dirty</dt><dd>${g.dirty === null ? 'not read' : plural(g.dirty, 'file')}</dd>
        <dt>Ahead / behind</dt><dd>+${g.ahead ?? '?'} / −${g.behind ?? '?'} on ${g.branch || '?'}</dd><dt>Default lag</dt><dd>${g.headline}${g.detail ? `: ${g.detail}` : ''}</dd>
        <dt>Last fetch</dt><dd>${age(g.fetched_iso)} before the reading</dd><dt>Last commit</dt><dd>${g.subject || '—'}</dd><dt>Read</dt><dd>${READ}</dd></dl>
        ${pw === true ? '' : html`<p class="why">No pull offered: ${pw}.</p>`}`
      : html`<h3>Git state</h3><p class="why"><span class="g-unknown" aria-hidden="true">▨</span> Not read: ${pw}.</p>`;
    h = html`<p class="kind">${I(r.registered ? 'settings' : 'folder-code')} Repo${r.registered ? '' : ' · not registered'}</p><h2>${r.name}</h2>
      <dl><dt>Path</dt><dd>${r.path}</dd><dt>Owner</dt><dd>${r.owner}</dd><dt>Managed</dt><dd>${r.managed}</dd><dt>Runner merge</dt><dd>${r.merge}</dd><dt>Status source</dt><dd>${r.source}</dd></dl>
      ${r.registered ? html`<p><a class="btn quiet" href="?view=repos&amp;repo=${encodeURIComponent(r.path)}">${I('settings')} Open settings</a></p>
      <p class="why">The settings page shows the sd-db row, sd-review.json and GitHub protection, each with the route an edit takes.</p>`
        : html`<p class="why">sd-db has no row for this checkout, so it has no settings page. <code>sd-db.sh repo add ${r.path}</code> registers it.</p>`}${git}${act}`;
  } else if (kind === 'proc') {
    const p = SESS.processes[+key]; if (!p) return;
    h = html`<p class="kind"><span class="g-ok">●</span> sd-* process</p><h2>${p.command}</h2><dl><dt>PID</dt><dd>${p.pid}</dd><dt>Running for</dt><dd>${p.elapsed}</dd></dl>
      <p class="why">A process has no command here; stop it from its own terminal.</p>`;
  } else if (kind === 'wt') {
    h = key === 'abandoned'
      ? html`<p class="kind"><span class="g-caution">▲</span> Worktrees · abandoned</p><h2>${SESS.abandoned} abandoned worktrees</h2><dl><dt>What</dt><dd>registered, directory gone</dd><dt>Source</dt><dd>fleet sessions</dd></dl>${act}`
      : html`<p class="kind"><span class="g-ok">●</span> Worktrees · live</p><h2>${SESS.registered - SESS.abandoned} live worktrees</h2><dl><dt>Registered</dt><dd>${SESS.registered}</dd></dl>`;
  } else if (kind === 'svc') {
    const s = SERVICES.find(x => x.label === key); if (!s) return;
    const st = SVCSTATE(s);
    h = html`<p class="kind"><span class="g-${st}">${GLYPH[st]}</span> Service · ${s.state}</p><h2>${s.name}</h2><dl><dt>Label</dt><dd>${s.label}</dd><dt>What</dt><dd>${s.category} · ${s.scope} · ${s.domain}</dd><dt>PID</dt><dd>${s.pid ?? '—'}</dd><dt>Previous exit</dt><dd>${s.last_exit ?? '—'}</dd><dt>Deployed at</dt><dd>▨ not recorded</dd></dl>${act}`;
  }
  put(details, h); swap();
  if (shell.commands.get(id)) shell.commands.select(id); else shell.setContext(details.querySelector('h2')?.textContent || '');
  if (!['cron', 'asg', 'merge'].includes(kind)) shell.suggest(['What changed here in the last day?', 'What should I look at first on this page?', 'Explain this row']);
  shell.row(id);
  if (open) shell.openPane('tab-details');
}

// ---------- Filter box ----------
const input = document.getElementById('shift'), ghost = document.getElementById('ghost');
const RENDER = new Map([['repos', renderRepos], ['lane', renderLane], ['sessions', renderSessions], ['deploys', renderDeploys], ['schedules', renderSchedules]]);
function render() {
  const draw = RENDER.get(view);
  if (!draw) throw new Error(`no such view: ${view}`);
  draw();
}
function applyText() {
  text.q = input.value.trim();
  const n = Object.keys(tokens().chips).length;
  ghost.textContent = n ? `${plural(n, 'chip')}` : '';
  RS.page = SS.page = 1;
  if (DOC) render();
  setURL();
}
input.addEventListener('input', applyText);
input.addEventListener('keydown', e => { if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; applyText(); } });

document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
  if (e.key === '/') { e.preventDefault(); input.focus(); }
});
window.PAGE_KEYS = [['/', 'Filter the open sub-view'], ['← / →', 'On the sub-view tabs: the previous or next one']];
window.PAGE_LIST = { rows: () => document.querySelectorAll(`#view-${view} tbody tr[data-id]`), select: id => selectRow(id, false) };

window.PAGE_COMMANDS = [
  { label: 'Show repos', icon: 'folder-code', run: () => location.href = '?view=repos' },
  { label: 'Show ship lane', icon: 'git-merge', run: () => location.href = '?view=lane' },
  { label: 'Show sessions and agents', icon: 'bot', run: () => location.href = '?view=sessions' },
  { label: 'Show deploys', icon: 'rocket', run: () => location.href = '?view=deploys' },
  { label: 'Show failed schedules', icon: 'calendar-clock', run: () => location.href = '?view=schedules&q=state:failed' },
  { label: 'Open system repo settings', icon: 'settings', run: () => location.href = '?view=repos&repo=' + encodeURIComponent('~/repos/system') },
];

// ---------- Start (build: read /api/management, then draw; again after each write) ----------
let started = false;
async function load() {
  if (!started) shell.state({ kind: 'loading', text: 'Reading repos, the lane, sessions, services and jobs. Rows appear when /api/management answers.', source: '/api/management' });
  let doc;
  try { doc = await getJSON('/api/management'); }
  catch (err) {
    shell.state({ kind: 'error', text: `Management was not read, so nothing below is current: ${err.message}. Reload retries it.`, source: '/api/management' });
    return;
  }
  absorb(doc);
  const failed = Object.entries(doc.sources || {}).filter(([, v]) => v).map(([k]) => k);
  shell.state(failed.length ? { kind: 'partial', text: `Not read: ${failed.join(', ')}. Each shows why in its view.`, source: '/api/management' } : null);
  putObjects();
  lamps();
  pageAttention();
  if (!started) {
    started = true;
    readURL();
    tabs.forEach(t => { const on = t.dataset.view === view; t.setAttribute('aria-selected', on); t.tabIndex = on ? 0 : -1; document.getElementById(t.getAttribute('aria-controls')).hidden = !on; });
    renderLane(); renderSessions(); renderDeploys(); renderSchedules(); renderRepos();
    const row = params.get('row');
    if (row && document.querySelector(`#view-${view} tr[data-id="${CSS.escape(row)}"]`)) selectRow(row, false); else defaultDetails();
    document.getElementById('subhead').textContent = repoParam ? `Settings for ${repoParam}. Chat is scoped to this repo.`
      : `Every repo setting, the merge lane, sessions, services and schedules. Read ${hhmm(READ)} UTC.`;
  } else {
    renderLane(); renderSessions(); renderDeploys(); renderSchedules(); renderRepos();
    const row = shell.row();
    if (row && document.querySelector(`#view-${view} tr[data-id="${CSS.escape(row)}"]`)) selectRow(row, false);
  }
}
document.addEventListener('DOMContentLoaded', () => {
  if (repoParam) view = 'repos';
  registerCommands();
  load();
});
