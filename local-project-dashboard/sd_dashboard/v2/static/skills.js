// Skills (sd:2123): the design source's products/system/designs/pages/skills.js at 2729265, ported. Each change from the
// reference is marked "build:". The rows are /api/skills (skills_screen.document): the catalog the old screen renders, each
// skill's use in the last three Monday-start weeks, and the use records no catalog skill explains; never sample data.
// Try, Review, Promote and Demote ask first and post the routes the old screen posts: no verb withdraws a trial or a queued
// request, so none carries the reference's Undo. Run, Schedule, Adopt and Scan are copy only: the dashboard has no route that
// creates the item a run needs, adds a launchd job, adopts a skill or scans sessions, and the reference's chat proposals are
// not ported.
const { html, put, plural } = window.markup;
// Palette "This page" group and the key sheet. shell.js reads both at start, so they are set before the shell runs.
window.PAGE_KEYS = [['/', 'Filter, run or adopt a skill'], ['→', 'In the field: the next reading (filter, run, adopt)']];
window.PAGE_COMMANDS = [
  { label: 'Adopt a skill from GitHub', icon: 'download', run: () => document.dispatchEvent(new CustomEvent('shell:open', { detail: 'adopt' })) },
  { label: 'Show unused path skills', icon: 'filter', run: () => document.dispatchEvent(new CustomEvent('skills:facet', { detail: 'unused' })) },
];
// build: nothing is read yet, so the page claims nothing for the rail until /api/skills answers.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'skills not read' };
addEventListener('DOMContentLoaded', () => {
  const C = window.shell.commands;
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
    const md = d => String(d || '').slice(5, 10);
  const utc = t => t ? String(t).slice(0, 16).replace('T', ' ') + ' UTC' : '—';

  // ---------- Data (build: /api/skills) ----------
  let DOC = null, S = [], byName = {}, FAILED = '', selected = null, picked = [];
  let onPath = [], trials = [], contrib = [], unusedPath = [];
  const total = s => s.weeks.reduce((a, b) => a + b, 0);
  const now = s => s.weeks[s.weeks.length - 1];
  // A skill row's glyph: ▲ a path skill with no use in the three weeks; ◌ on trial (waiting on a keep/drop decision); ● the
  // rest installed; none for contrib.
  const glyph = s => s.status === 'path' && !total(s) ? ['▲', 'caution', 'on a path, no recorded use'] : s.status === 'trial' ? ['◌', 'queued', 'on trial']
    : s.status === 'path' ? ['●', 'ok', 'on a path, used'] : ['', 'none', 'contrib, not installed'];
  const avail = s => s.status === 'path' ? `path · ${s.paths.join(', ')}` : s.status === 'trial' ? `trial · to ${md(s.trial[1])}` : 'contrib';
  const weekOf = i => md(DOC.weeks[i]);
  // build: the reference's "*recording starts 09-11" is read: the first skill_use row, when it falls inside the first week.
  const startsLate = () => DOC.use.first && DOC.use.first.slice(0, 10) > DOC.weeks[0] ? md(DOC.use.first) : '';
  const others = () => Object.entries(DOC.surfaces).filter(([k]) => k !== 'claude').reduce((a, [, n]) => a + n, 0);
  // The reader (read.js) puts the objects adopt returns and retires the ones a reading no longer lists.
  function adopt(doc) {
    DOC = doc;
    S = doc.skills.map(s => ({ ...s, id: s.name, type: 'skill', label: s.name, q: [] }));
    byName = Object.fromEntries(S.map(s => [s.name, s]));
    onPath = S.filter(s => s.status === 'path'); trials = S.filter(s => s.status === 'trial'); contrib = S.filter(s => s.status === 'contrib');
    unusedPath = onPath.filter(s => !total(s));
    // Attention: unused path skills (row glyph ▲) and the use-record fault (annunciator ▲). Nothing on this page is warning.
    const n = unusedPath.length + (doc.junk ? 1 : 0);
    window.PAGE_ATTENTION = n ? { state: 'caution', n, what: 'unused path skills and use-record faults' } : { state: 'ok', n: 0, what: 'skill flags' };
    window.shell.attention?.();
    return {
      objects: [...S, { id: 'hygiene', type: 'use records', label: `${plural(doc.junk, 'skill_use row')} name a path` },
        { id: 'surfaces', type: 'use surfaces', label: 'Use records by surface' }, { id: 'adopt', type: 'adopt form', label: 'Adopt a skill' }],
      state: S.length ? null : { kind: 'empty', title: 'No skills', text: 'The pack catalog lists no skill.', source: '/api/skills' },
    };
  }
  function clear(err) {
    DOC = null; S = []; byName = {}; onPath = trials = contrib = unusedPath = []; FAILED = err.message;
    window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'skills not read' };
    window.shell.attention?.();
  }

  // ---------- Annunciator ----------
  // A lamp with nothing to filter is a div that says so, not a button (design.md § Lamps).
  const lamp = (facet, state, label, value, small) => {
    const inner = html`<span class="lbl">${label} <span class="g" aria-hidden="true">${state === 'caution' ? '▲' : state === 'unknown' ? '▨' : '●'}</span></span><span class="val"><span class="ph">${value}</span><small>${small}</small></span>`;
    return facet ? html`<li><button class="cell" type="button" data-facet="${facet}" data-state="${state}" aria-pressed="${String(facet === filter)}">${inner}</button></li>`
      : html`<li><div class="cell" data-state="${state}">${inner}</div></li>`;
  };
  function drawHead() {
    const paths = Object.keys(DOC.paths), [, lastWeek, thisWeek] = DOC.totals;
    const first = trials.map(s => s.trial[1]).sort()[0];
    const days = first ? Math.max(0, Math.ceil((Date.parse(first) - Date.parse(DOC.read)) / 864e5)) : 0;
    const other = others();
    document.getElementById('sub').textContent = `The sd pack catalog: ${plural(S.length, 'skill')}. ${onPath.length} on paths, ${trials.length} on trial, ${contrib.length} in contrib.`;
    put(document.getElementById('annunciator'), html`${[
      lamp(onPath.length && 'path', 'ok', 'Paths', html`<b>${onPath.length}</b> on ${plural(paths.length, 'path')}`, paths.join(' · ')),
      lamp(trials.length && 'trial', 'ok', 'Trials', html`<b>${trials.length}</b> on trial`, first ? `first ends ${md(first)} · ${plural(days, 'day')}` : 'none running'),
      lamp(contrib.length && 'contrib', 'ok', 'Contrib', html`<b>${contrib.length}</b> to try`, 'not installed'),
      // build: a plain lamp, not the reference's link to Metrics: Metrics has no v2 page yet.
      lamp('', 'ok', 'Use this week', html`<b>${thisWeek}</b> uses`, `${lastWeek} the week before · all skills`),
      lamp(unusedPath.length && 'unused', unusedPath.length ? 'caution' : 'ok', 'Unused on paths', html`<b>${unusedPath.length}</b> of ${onPath.length}`,
        unusedPath.length ? 'no use in three weeks · demote?' : 'every path skill was used'),
      html`<li><a class="cell" href="?row=hygiene" data-open="hygiene" data-state="${DOC.junk ? 'caution' : 'ok'}"><span class="lbl">Use records <span aria-hidden="true">${DOC.junk ? '▲' : '●'}</span></span><span class="val"><span class="ph"><b>${DOC.junk}</b> rows name a path</span><small>${DOC.junk ? 'not a skill · recorder bug' : 'every row names a skill'}</small></span></a></li>`,
      // build: read, not drawn: the reference's "not read" lamp holds only while skill_use has no row from another surface.
      html`<li><a class="cell" href="?row=surfaces" data-open="surfaces" data-state="${other ? 'ok' : 'unknown'}"><span class="lbl">Codex · opencode use <span aria-hidden="true">${other ? '●' : '▨'}</span></span><span class="val"><span class="ph"><b>${other || '—'}</b> ${other ? 'rows' : 'not read'}</span><small>${other ? 'from surfaces other than claude' : html`no rows · <code>sd skill scan</code> not run`}</small></span></a></li>`,
      // build: refresh rereads /api/skills instead of reloading the page.
      html`<li><button class="cell" type="button" id="refresh"><span class="lbl">Observed${I('rotate-ccw')}</span><span class="val"><span class="ph"><b>${String(DOC.read).slice(11, 16)}</b> UTC</span><small>${String(DOC.read).slice(0, 10)} · refresh</small></span></button></li>`,
    ]}`);
  }

  // ---------- Catalog ledger: filter, sort, page (shell.list, sd:2682) ----------
  const FACETS = () => [['all', 'All', S.length], ['path', 'Path', onPath.length], ['trial', 'Trial', trials.length], ['contrib', 'Contrib', contrib.length], ['unused', 'Unused on path', unusedPath.length]];
  const KEYS = ['n', 'st', 'u'], LIST = { sort: 'u', dir: -1, size: 25 }, L = { ...LIST, page: 1 };
  const COLS = [[null, html`<span class="sr">State</span>`, 'g'], ['n', 'Skill'], ['st', 'Availability'], ['u', 'Use / week', 'use'], [null, html`<span class="sr">Actions</span>`, 'act']];
  let filter = 'all', q = '';
  const facetsEl = document.getElementById('facets');
  function drawFacets() { put(facetsEl, html`${FACETS().map(([k, l, n]) => html`<button class="chip" type="button" data-facet="${k}" aria-pressed="${String(filter === k)}">${l} <span>${n}</span></button>`)}`); }
  function visible() {
    let v = S.filter(s => filter === 'all' || (filter === 'unused' ? s.status === 'path' && !total(s) : s.status === filter));
    if (q) v = v.filter(s => (s.name + ' ' + s.description + ' ' + s.paths.join(' ')).toLowerCase().includes(q));
    const ord = { path: 0, trial: 1, contrib: 2 };
    const key = s => L.sort === 'n' ? s.name : L.sort === 'st' ? ord[s.status] : now(s) * 1000 + total(s);
    return v.sort((a, b) => key(a) < key(b) ? -L.dir : key(a) > key(b) ? L.dir : a.name < b.name ? -1 : 1);
  }
  function mini(s) {
    const max = Math.max(1, ...S.map(x => Math.max(...x.weeks)));
    const h = v => v ? Math.max(2, (v / max) * 14) : 0;
    return html`<svg class="mini" viewBox="0 0 36 16" aria-hidden="true"><line class="base" x1="0" x2="36" y1="15.5" y2="15.5"/>${s.weeks.map((v, i) => html`<rect class="bar${i === 2 ? ' now' : ''}" x="${i * 12 + 1}" y="${15 - h(v)}" width="10" height="${h(v)}" rx="1"/>`)}</svg>`;
  }
  const tbody = document.getElementById('rows'), headEl = document.getElementById('rows-head'), chipsEl = document.getElementById('chips'), pagerEl = document.getElementById('pager');
  // Active filters, one chip each; each remover leaves the list to draw once, after it.
  const activeFilters = () => [filter !== 'all' && { key: 'facet', label: `Availability: ${FACETS().find(x => x[0] === filter)[1]}` }, q && { key: 'q', label: `Text: ${q}` }].filter(Boolean);
  const UNFILTER = { facet: () => { filter = 'all'; }, q: () => { q = ''; input.value = ''; } };
  function drawRows() {
    const had = document.activeElement?.closest?.('tr[data-id]')?.dataset.id; // keep focus on the same row across a redraw
    const list = window.shell.list, v = visible(), shown = list.pageOf(v, L);
    put(headEl, list.sortHead(COLS, L));
    put(chipsEl, DOC ? list.chips(activeFilters(), v.length, S.length) : html``);
    put(tbody, html`${shown.length ? shown.map(s => { const [g, st, why] = glyph(s); return html`<tr data-id="${s.name}" aria-selected="${String(s.name === selected)}"${picked.includes(s.name) ? html` data-picked` : ''}>
      <td class="g g-${st}" title="${why}">${g}<span class="sr">${why}</span></td>
      <td class="name"><button type="button">${s.name}</button><p>${s.description}</p></td>
      <td class="avail">${avail(s)}${s.q.length ? html`<span class="pend"> · ${s.q.join(' · ')}</span>` : ''}</td>
      <td class="use" title="${s.weeks.map((n, i) => `${weekOf(i)}: ${n}`).join(' · ')}">${mini(s)}<b>${now(s)}</b><span class="sr"> this week; ${s.weeks[1]} the week before</span></td>
      <td class="act">${C.rowActions(s.name)}</td></tr>`; })
      : html`<tr class="lane"><td colspan="5">${DOC ? `No skill matches${q ? ` “${q}”` : ''}.` : `Not read: ${FAILED}`}</td></tr>`}`);
    put(pagerEl, DOC ? list.pager(v.length, L, 'skills') : html``);
    document.getElementById('tally').textContent = DOC ? `${DOC.totals[DOC.totals.length - 1]} uses this week · ${S.filter(s => now(s)).length} catalog skills used` : '';
    document.querySelectorAll('.cell[data-facet]').forEach(c => c.setAttribute('aria-pressed', String(c.dataset.facet === filter)));
    if (had && window.CSS) tbody.querySelector(`tr[data-id="${CSS.escape(had)}"] .name button`)?.focus();
    drawFacets();
    const p = new URLSearchParams();
    if (filter !== 'all') p.set('facet', filter);
    if (q) p.set('q', q);
    window.shell.url(list.listQuery(p, L, LIST)); // the shell keeps ?row=
  }
  function drawFoot() {
    const o = DOC.other;
    put(document.getElementById('foot'), html`Catalog from the pack, observed ${utc(DOC.read)}. Use from <code>skill_use</code> (${plural(DOC.use.rows, 'row')}${DOC.use.first ? `, ${DOC.use.first.slice(0, 10)} → ${DOC.use.last.slice(0, 10)}` : ''}).${o.length ? ` ${plural(o.length, 'skill')} outside the pack also recorded use, led by ${o.slice(0, 3).map(([n, c]) => `${n} (${c})`).join(', ')}.` : ''}`);
  }
  // Not read: every box says so, and the state slot above says why.
  function blank() {
    document.getElementById('sub').textContent = 'The sd pack catalog was not read.';
    put(document.getElementById('annunciator'), html``);
    put(facetsEl, html``);
    put(document.getElementById('foot'), html``);
    drawRows();
  }
  const draw = () => { if (!DOC) return blank(); drawHead(); drawRows(); drawFoot(); };
  // Each filter, sort or page change keeps the selection on a row the viewer can see (shell.reconcile).
  const reconcile = () => window.shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, keep: KEEP, select: id => select(id, false), clear: nothing });
  const redraw = () => { drawRows(); reconcile(); };
  headEl.addEventListener('click', e => { if (DOC && window.shell.list.sortBy(e, L)) redraw(); });
  pagerEl.addEventListener('click', e => { if (DOC && window.shell.list.paging(e, L)) redraw(); });
  const unfilter = keys => { keys.forEach(k => UNFILTER[k]()); L.page = 1; redraw(); };
  chipsEl.addEventListener('click', e => {
    const b = e.target.closest('[data-unfilter]'); if (b) return unfilter([b.dataset.unfilter]);
    if (e.target.closest('[data-unfilter-all]')) unfilter(activeFilters().map(f => f.key));
  });
  const setFacet = v => { if (!DOC) return; filter = v; L.page = 1; redraw(); };
  document.addEventListener('skills:facet', e => setFacet(e.detail));
  document.addEventListener('click', e => {
    const c = e.target.closest?.('[data-facet]'); if (c) return setFacet(filter === c.dataset.facet && c.classList.contains('cell') ? 'all' : c.dataset.facet);
  });
  tbody.addEventListener('click', e => {
    if (e.target.closest('.rowact')) return;
    const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
  });

  // ---------- Details ----------
  const details = document.getElementById('details');
  function weeksChart(s) {
    const W = 300, H = 104, max = Math.max(1, ...s.weeks), bw = 56, gap = (W - bw * 3) / 3, late = startsLate();
    const y = v => 84 - (v / max) * 64;
    return html`<figure class="weeks"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${s.name} use per week: ${s.weeks.map((n, i) => `week of ${weekOf(i)}, ${n}`).join('; ')}">
      <line class="base" x1="0" x2="${W}" y1="84.5" y2="84.5"/>
      ${s.weeks.map((v, i) => { const x = gap / 2 + i * (bw + gap); return html`<rect class="bar${i === 2 ? ' now' : ''}" x="${x}" y="${y(v)}" width="${bw}" height="${84 - y(v)}" rx="2"/><text class="v" x="${x + bw / 2}" y="${y(v) - 5}">${v}</text><text class="x" x="${x + bw / 2}" y="100">wk ${weekOf(i)}${i === 0 && late ? '*' : ''}</text>`; })}
    </svg><figcaption>Uses per Monday-start week from <code>skill_use</code>, every surface it records.${late ? ` *Recording starts ${late}.` : ''}</figcaption>
    <table class="sr"><caption>${s.name} uses per week</caption><tbody>${s.weeks.map((n, i) => html`<tr><th scope="row">week of ${weekOf(i)}</th><td>${n}</td></tr>`)}</tbody></table></figure>`;
  }
  const mark = () => tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === selected)));
  function show(id) {
    if (id === 'adopt') return showAdopt();
    if (!DOC) return false;
    if (id === 'hygiene' || id === 'surfaces') return showNote(id);
    const s = byName[id]; if (!s) return false;
    selected = id; mark();
    const [g, st] = glyph(s);
    const modes = Object.entries(s.modes).map(([k, v]) => `${k} ${v}`).join(' · ') || 'none recorded';
    C.select(id);
    put(details, html`<p class="kind">${g ? html`<span class="g-${st}" aria-hidden="true">${g}</span> ` : ''}Skill · ${avail(s)}</p>
      <h2>${s.name}</h2><p class="desc">${s.description}</p>
      <h3>Use per week</h3>${total(s) ? weeksChart(s) : html`<p class="weeks zero">No recorded use in the last three weeks${s.status === 'path' ? ': a demotion candidate' : ''}.</p>`}
      <dl><dt>Recorded as</dt><dd>${modes}</dd><dt>Last use</dt><dd>${utc(s.last)}</dd>
        <dt>Installed</dt><dd>${s.installed.length ? s.installed.join(' · ') + ' · current' : 'not installed'}${s.changed.length ? ` · changed on ${s.changed.join(', ')}` : ''}</dd>
        ${s.trial ? html`<dt>Trial</dt><dd>${String(s.trial[0]).slice(0, 10)} → ${String(s.trial[1]).slice(0, 10)}</dd>` : ''}<dt>Source</dt><dd>${s.source}</dd><dt>Revision</dt><dd>${String(s.revision).slice(0, 12)}</dd></dl>
      ${s.q.length ? html`<p class="why">Queued from here: ${s.q.join(' · ')}</p>` : ''}
      <details class="when"><summary>When to use</summary><p>${s.when}</p></details>
      <h3>Act</h3>${C.bar(id)}<div id="form-slot"></div>`);
    window.shell.setContext?.(s.name);
    window.shell.suggest?.([`When should I reach for ${s.name}?`, `Which of my repos would ${s.name} help this week?`, total(s) ? `What did the last ${s.name} runs produce?` : `Why has nobody used ${s.name}?`]);
    swap();
    return true;
  }
  // build: both notes are read, not written: the reference's example row and its counts were the mockup's reading.
  function showNote(id) {
    selected = id; mark(); C.select(id);
    const rows = DOC.use.rows, by = Object.entries(DOC.surfaces).map(([k, n]) => `${k} ${n}`).join(' · ') || 'none';
    const note = id === 'hygiene'
      ? { g: DOC.junk ? ['▲', 'caution'] : ['●', 'ok'], kind: 'Use records · data quality', title: `${plural(DOC.junk, 'skill_use row')} name a path`,
        facts: [['Rows', `${DOC.junk} of ${rows}`], ['Effect', 'counted in the weekly totals, matched to no skill']],
        why: DOC.junk ? 'The recorder took a slash-leading path in a prompt for a skill name. These rows count toward the weekly totals and match no skill.' : 'Every skill_use row names a skill.',
        cli: "sqlite3 'file:sd.db?mode=ro' \"select * from skill_use where skill like '%/%'\"" }
      : { g: others() ? ['●', 'ok'] : ['▨', 'unknown'], kind: 'Use records · surfaces', title: others() ? 'Use is recorded from more than one surface' : 'Codex and opencode use is not read',
        facts: [['Rows by surface', by], ['Writer', 'sd skill scan reads ~/.codex/sessions']],
        why: others() ? 'Each chart here counts every surface skill_use records.' : 'No row from another surface means the scan has not run, not that Codex used no skills. Until it runs, every usage chart here is claude-only.',
        cli: 'sd skill scan' };
    const act = C.bar(id);
    put(details, html`<p class="kind"><span class="g-${note.g[1]}" aria-hidden="true">${note.g[0]}</span> ${note.kind}</p><h2>${note.title}</h2>
      <dl>${note.facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl><p class="note">${note.why}</p>
      ${String(act) ? html`<h3>Act</h3>${act}` : html`<h3>Same reading from the CLI</h3><div class="cli"><code>${note.cli}</code><button class="icon-btn" type="button" aria-label="Copy: ${note.cli}" data-copy="${note.cli}">${I('copy')}</button></div>`}`);
    window.shell.setContext?.(note.title); swap();
    return true;
  }
  const swap = () => { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); };
  function select(id, open) { if (show(id) !== false && open) window.shell.openPane('tab-details'); }
  const KEEP = id => id === 'hygiene' || id === 'surfaces' || id === 'adopt';
  // The reader found no row to select, or a filter hides them all.
  function nothing() {
    selected = null; mark();
    put(details, html`<p class="why">${DOC ? (S.length ? 'No skill matches the filter, so nothing is selected. Clear filters to see every skill.' : 'The catalog lists no skill, so nothing is selected.')
      : 'The skills were not read, so nothing is selected. Reload retries it.'}</p>`);
  }

  // ---------- Forms: promote (posts); adopt (copy only) ----------
  // build: the reference's run and schedule forms built proposals; with no route behind either, Run and Schedule are copy only.
  function openPromote(s) {
    const slot = document.getElementById('form-slot'); if (!slot) return;
    put(slot, html`<form class="form" id="pro-form"><div class="parsed" role="group" aria-label="Path">${Object.entries(DOC.paths).map(([k, v], i) => html`<button class="chip" type="button" data-path="${k}" aria-pressed="${String(!i)}">${k} · ${v}</button>`)}</div>
      <div class="row"><button class="btn" type="submit">Queue promotion</button><button class="btn quiet" type="button" data-cancel>Cancel</button></div>
      <p class="why">Runs in an isolated checkout and opens a pull request for you to merge.</p></form>`);
    slot.querySelector('.parsed').addEventListener('click', e => { const b = e.target.closest('[data-path]'); if (b) slot.querySelectorAll('[data-path]').forEach(x => x.setAttribute('aria-pressed', String(x === b))); });
    slot.querySelector('#pro-form').addEventListener('submit', e => { e.preventDefault(); const p = slot.querySelector('[data-path][aria-pressed="true"]').dataset.path; slot.replaceChildren(); C.run(PROMOTE, { ...C.get(s.name), path: p }); });
    slot.querySelector('[data-cancel]').addEventListener('click', () => { slot.replaceChildren(); });
    slot.querySelector('button')?.focus();
  }
  const parseSource = v => { const m = v.trim().replace(/^https?:\/\/github\.com\//, '').replace(/\/(tree|blob)\/[^/]+/, '').match(/^([\w.-]+)\/([\w.-]+)(?:\/(.+?))?\/?$/);
    return m && { owner: m[1], repo: m[2], path: m[3] || '', name: (m[3] || m[2]).split('/').filter(x => x !== 'SKILL.md').pop() }; };
  function showAdopt(prefill = '') {
    selected = 'adopt'; C.select(null); mark();
    put(details, html`<p class="kind">${I('download')} Adopt a skill</p><h2>Adopt from GitHub</h2>
      <p class="desc">Bring an outside skill into contrib/ through <code>sd-skill-adopt</code>: safety pre-screen, lint, canonical transform, provenance. The dashboard runs none of it: copy the line into a terminal.</p>
      <form class="form" id="adopt-form" novalidate><label>Source<input id="ad-src" value="${prefill}" placeholder="owner/repo/path/to/skill or a GitHub URL" autocomplete="off"></label>
        <div class="parsed" id="ad-parsed" aria-live="polite"></div></form>
      <h3>Same thing from the CLI</h3><div class="cli"><code id="ad-cli"></code><button class="icon-btn" type="button" id="ad-copy" aria-label="Copy the adopt line">${I('copy')}</button></div>`);
    const src = details.querySelector('#ad-src'), out = details.querySelector('#ad-parsed'), cli = details.querySelector('#ad-cli'), copy = details.querySelector('#ad-copy');
    const upd = () => { const p = parseSource(src.value), line = `claude -p "/sd-skill-adopt ${p ? `${p.owner}/${p.repo}${p.path ? '/' + p.path : ''}` : 'SOURCE'}"`;
      put(out, html`${p ? html`<span class="chip">repo: ${p.owner}/${p.repo}</span><span class="chip${p.path ? '' : ' miss'}">path: ${p.path || 'repo root'}</span><span class="chip">lands as: contrib/${p.name}</span>${byName[p.name] ? html`<span class="chip miss">name taken: ${p.name} is ${byName[p.name].status}</span>` : ''}` : html``}`);
      cli.textContent = line; copy.dataset.copy = line; };
    src.addEventListener('input', upd); upd();
    details.querySelector('#adopt-form').addEventListener('submit', e => e.preventDefault());
    window.shell.setContext?.('adopt a skill'); window.shell.suggest?.(['What does sd-skill-adopt refuse?', 'Is there already a skill for this in contrib?']);
    window.shell.row?.('adopt'); window.shell.openPane('tab-details'); swap(); src.focus?.();
    return true;
  }
  document.getElementById('adopt-btn').addEventListener('click', () => showAdopt());
  document.getElementById('annunciator').addEventListener('click', e => {
    // design.md § Lamps: a button lamp toggles a filter; a lamp that opens a reading is a link to it (here, Details).
    // A modified or non-primary click is the browser's: a new tab at the link's ?row= (PR #31 review).
    const a = e.target.closest('a.cell[data-open]'); if (a) { if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; e.preventDefault(); select(a.dataset.open, true); return; }
    if (e.target.closest('#refresh')) load();
  });

  // ---------- Shapeshift bar: filter · run skill · adopt source ----------
  const input = document.getElementById('shift'), prev = document.getElementById('shift-preview'), as = document.getElementById('shift-as'), ghost = document.getElementById('ghost');
  let mode = 'filter';
  const guess = v => /^run\b/i.test(v) ? 'run' : /github\.com\/|^[\w.-]+\/[\w.-]+(\/|$)/.test(v) ? 'adopt' : 'filter';
  function setMode(m) { mode = m; prev.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', String(c.dataset.as === m))); render(); }
  function render() {
    const v = input.value.trim(); prev.hidden = !v; ghost.textContent = v ? `→ ${mode}` : '';
    const name = (v.match(/^run\s+([\w-]+)/i) || [])[1];
    put(as, html`${mode === 'run' ? html`${I('play')} run ${name && byName[name] ? html`<b>${name}</b>` : html`<span>a skill name</span>`}`
      : mode === 'adopt' ? html`${I('download')} adopt from ${v}` : html`${I('filter')} filter the catalog`}`);
    q = mode === 'filter' ? v.toLowerCase() : ''; L.page = 1;
    if (DOC) redraw();
  }
  input.addEventListener('input', () => setMode(guess(input.value.trim())));
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); const o = ['filter', 'run', 'adopt']; setMode(o[(o.indexOf(mode) + 1) % 3]); }
    if (e.key === 'Enter' && input.value.trim()) {
      e.preventDefault(); const v = input.value.trim();
      // build: run selects the skill; its Details hold Run's line to copy.
      if (mode === 'run') { const name = (v.match(/^run\s+([\w-]+)/i) || [])[1]; if (!byName[name]) return window.shell.toast(`No skill named “${name || ''}” in the catalog.`); select(name, true); input.value = ''; render(); }
      if (mode === 'adopt') { showAdopt(v); input.value = ''; render(); }
    }
    if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; render(); }
  });
  prev.addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) { setMode(c.dataset.as); input.focus(); } });
  document.addEventListener('keydown', e => {
    if (e.target.matches?.('input, textarea') || document.querySelector('dialog[open], .actmenu:not([hidden])') || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '/') { e.preventDefault(); input.focus(); }
  });
  // The shell walks these rows on j / k. The Use records panels are selections that are not list rows (sd:2306).
  window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false), keep: KEEP };

  // ---------- Commands (products/system/commands.md) ----------
  // Each skill is an object; each command is declared once and the shell renders row, menu, Details and palette.
  // A write's answer is its own: the reread after it is started, not awaited, so a failed reread never reports a landed write
  // as "not changed". Until the reread lands, the row says what was queued. A refused write reads again with load().
  const sk = o => byName[o.id] || o, queued = (o, l) => sk(o).q.includes(l) ? `${l} already` : true;
  const pend = (o, label) => { const s = byName[o.id]; if (!s) return; s.q = [...s.q.filter(x => x !== label), label]; drawRows(); if (selected === s.name) show(s.name); };
  const write = (o, action, label, done, body = {}) => window.shell.post(`/api/skills/${encodeURIComponent(o.id)}/${action}`, { revision: sk(o).revision, ...body })
    .then(out => { pend(o, label); reread(); return done(out); }, e => { load(); throw e; });
  const itemOf = out => out?.item?.id ? ` as item #${out.item.id}` : '';
  const PROMOTE = { id: 'skill.promote.queue', on: 'skill form', label: 'Promote', risk: 'confirm',
    consequence: o => `This queues a task that moves ${o.id} into skills/ on the ${o.path} path, in an isolated checkout, and opens a pull request. No verb withdraws it.`,
    cli: o => `sd skill promote ${o.id} --path ${o.path}`,
    run: o => write(o, 'promote', `promotion to ${o.path} queued`, out => `Promotion of ${o.id} to ${o.path} queued${itemOf(out)}`, { path_name: o.path }) };
  function registerCommands() {
    C.register(
      { id: 'skill.try', on: 'skill', label: 'Try', key: 't', risk: 'confirm', bulk: true, primary: o => sk(o).status === 'contrib',
        when: o => sk(o).status !== 'contrib' ? `already installed · ${sk(o).status}` : queued(o, 'trial queued'),
        consequence: o => `This starts a 30-day trial of ${o.id}; it installs at the next sd install. No verb ends a trial early.`,
        cli: o => `sd skill try ${o.id}`, run: o => write(o, 'try', 'trial queued', () => `Trial of ${o.id} starts at the next install`) },
      // Copy only: the dashboard has no route that creates the item a run carries (the reference's form built a proposal).
      { id: 'skill.run', on: 'skill', label: 'Run', key: 'r', risk: 'safe', executes: false, primary: o => sk(o).status !== 'contrib' && !(sk(o).status === 'path' && !total(sk(o))),
        when: o => sk(o).status !== 'contrib' || 'not installed · try it first',
        cli: () => 'sd run --sequential --role author --scope SCOPE --budget-minutes N ITEM', run: () => 'Copy the line: the dashboard has no route that creates the item a run carries' },
      { id: 'skill.review', on: 'skill', label: 'Review', key: 'v', risk: 'confirm', bulk: true, when: o => queued(o, 'review queued'),
        consequence: o => `This queues a review of ${o.id} by a reviewer independent of its latest author. No verb withdraws it.`,
        cli: o => `sd skill review ${o.id}`, run: o => write(o, 'review', 'review queued', out => `Review of ${o.id} queued${itemOf(out)}`) },
      { id: 'skill.promote', on: 'skill', label: 'Promote', key: 'p', risk: 'safe', fills: true, when: o => sk(o).status !== 'path' || `on ${sk(o).paths.join(', ')} already`,
        cli: o => `sd skill promote ${o.id} --path PATH`, run: o => { select(o.id, true); openPromote(sk(o)); return null; } },
      { id: 'skill.demote', on: 'skill', label: 'Demote', key: 'd', risk: 'confirm', primary: o => sk(o).status === 'path' && !total(sk(o)),
        when: o => sk(o).status === 'path' ? queued(o, 'demotion queued') : 'not on a path',
        consequence: o => `This queues a task that moves ${o.id} from skills/ to contrib/ and off every path, and opens a pull request. No verb withdraws it.`,
        cli: o => `sd skill demote ${o.id}`, run: o => write(o, 'demote', 'demotion queued', out => `Removal of ${o.id} from paths queued${itemOf(out)}`) },
      { id: 'skill.schedule', on: 'skill', label: 'Schedule', key: 's', risk: 'safe', executes: false, when: o => sk(o).status !== 'contrib' || 'not installed · try it first',
        cli: () => 'no sd verb: a job is a launchd agent', run: () => 'No sd verb adds a job: a job is a launchd agent' },
      // build: applying review notes lives on the review item's page, where the notes are.
      { id: 'skill.apply', on: 'skill', label: 'Apply', key: 'a', risk: 'confirm', when: () => 'apply review notes from the review item',
        cli: () => 'sd skill apply ITEM NOTE…' },
      { id: 'skill.scan', on: 'use surfaces', label: 'Scan', key: 's', risk: 'safe', executes: false, primary: () => true,
        cli: () => 'sd skill scan', run: () => 'Copy it into a terminal: the dashboard has no route that runs sd skill scan' },
    );
  }
  document.addEventListener('shell:open', e => select(e.detail, true));
  document.addEventListener('shell:picked', e => { picked = e.detail || []; tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', picked.includes(tr.dataset.id))); });

  // ---------- Start (build: read /api/skills through shell.read, then draw) ----------
  // The reader (read.js) numbers each read so only the newest draws, retires the objects a reading drops, moves a selection
  // whose row is gone, and queues a reread behind a read that started before its write landed.
  const u = new URLSearchParams(location.search);
  if (['path', 'trial', 'contrib', 'unused'].includes(u.get('facet'))) filter = u.get('facet');
  window.shell.list.listParams(u, L, { sorts: KEYS, size: LIST.size });
  if (/^-(n|st|u)$/.test(u.get('sort') || '')) { L.sort = u.get('sort').slice(1); L.dir = -1; } // an older link's ?sort=-key
  if (u.get('q')) { input.value = u.get('q'); q = u.get('q').toLowerCase(); }
  const reading = window.shell.read({
    source: '/api/skills', what: 'the skills', adopt, clear, draw,
    current: () => selected,
    first: () => visible()[0]?.name ?? null,
    // A ?row= the filter hides goes to the first shown row, as a filter change does; the panels are kept.
    select: (id, opened) => (KEEP(id) || visible().some(s => s.name === id) ? select(id, opened) : reconcile()),
    unselect: nothing,
  });
  const load = () => reading.load(), reread = () => reading.reread();
  registerCommands();
  load();
});
