// Skills (sd:2123): the design source's products/system/designs/pages/skills.js at 2729265, ported. Each change from the
// reference is marked "build:". The rows are /api/skills (skills_screen.document): the catalog the old screen renders, each
// skill's use in the last three Monday-start weeks, and the use records no catalog skill explains; never sample data.
// Try, Review, Promote and Demote ask first and post the routes the old screen posts: no verb ends a trial early, and a
// request files an sd item that closes in Tasks (system sd:5477), so none carries the reference's Undo. Run, Schedule and Adopt are copy only: the dashboard has no route that
// creates the item a run needs, adds a launchd job or adopts a skill, and the reference's chat proposals are not ported.
// Scan runs `sd skill scan` through /api/run (sd:5886). Re-import and Evaluate (sd:3992) post /api/skills/intake, which runs `sd skill intake` and answers its report;
// Approve shows only under a report that can install, asks first, and posts the URL, scope and revision that report printed.
// A skill the intake installed in ~/.claude/skills (sd:3997) is a "user skill": the Adopt panel lists it with Re-import, which
// runs the intake with the user scope. It is no catalog row, so Try, Review, Promote and Demote are not on it.
// The Intake view (system sd:5478) lists the repositories `sd skill intake --tracked` names, from /api/skills/intake/repos.
// Track, Refresh and Remove post /api/skills/intake/tracking; Remove keeps the installed skills and its Undo tracks the
// repository again. A refresh lists each skill's intake report and installs nothing: Approve under a report is the intake's.
// Rollback (system sd:5779) posts the same route with rollback: true and shows the swap back to the copy the last install
// replaced, with the pack skills the import informed; Approve rollback asks first and posts the revision that report printed.
const { html, put, plural, cells } = window.markup;
// The views, for the shell's view switch: the catalog, and the tracked repositories, read the first time the view opens.
window.PAGE_VIEWS = [{ id: 'catalog', label: 'Catalog' }, { id: 'intake', label: 'Intake', api: '/api/skills/intake/repos' }];
// Palette "This page" group and the key sheet. shell.js reads both at start, so they are set before the shell runs.
window.PAGE_KEYS = [['/', 'Filter, run or adopt a skill'], ['→', 'In the field: the next reading (filter, run, adopt)']];
window.PAGE_COMMANDS = [
  { label: 'Adopt or evaluate a skill from GitHub', icon: 'download', run: () => document.dispatchEvent(new CustomEvent('shell:open', { detail: 'adopt' })) },
  { label: 'Show unused path skills', icon: 'filter', run: () => document.dispatchEvent(new CustomEvent('skills:facet', { detail: 'unused' })) },
];
// build: nothing is read yet, so the page claims nothing for the rail until /api/skills answers.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'skills not read' };
addEventListener('DOMContentLoaded', () => {
  const C = window.shell.commands;
const shq = window.shell.shq; // every value in a line the operator copies is single-quoted: $(), backticks and ; stay literal (sd:3992)
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
    const md = d => String(d || '').slice(5, 10);
  const utc = t => t ? String(t).slice(0, 16).replace('T', ' ') + ' UTC' : '—';

  // ---------- Data (build: /api/skills) ----------
  let DOC = null, S = [], byName = {}, U = [], FAILED = '', selected = null, picked = [];
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
    U = (doc.user || []).map(u => ({ ...u, id: `user:${u.name}`, type: 'user skill', label: u.name }));
    // Attention: the read's own, unused path skills (row glyph ▲) and the use-record fault (sd:3446, skills_screen._attention).
    window.PAGE_ATTENTION = doc.attention || { state: 'unknown', n: 0, what: 'skills not read' };
    window.shell.attention?.();
    return {
      objects: [...S, { id: 'hygiene', type: 'use records', label: `${plural(doc.junk, 'skill_use row')} name a path` },
        { id: 'surfaces', type: 'use surfaces', label: 'Use records by surface' }, { id: 'adopt', type: 'adopt form', label: 'Adopt a skill' }, ...U],
      state: S.length ? null : { kind: 'empty', title: 'No skills', text: 'The pack catalog lists no skill.', source: '/api/skills' },
    };
  }
  function clear(err) {
    DOC = null; S = []; byName = {}; U = []; onPath = trials = contrib = unusedPath = []; FAILED = err.message;
    window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'skills not read' };
    window.shell.attention?.();
  }

  // ---------- Annunciator ----------
  // A lamp with nothing to filter is a div that says so, not a button (design.md § Lamps).
  const lampGlyph = state => html`<span class="g" aria-hidden="true">${state === 'caution' ? '▲' : state === 'unknown' ? '▨' : '●'}</span>`;
  const lamp = (facet, state, label, value, small) => ({ button: !!facet, state, pressed: facet ? facet === filter : undefined,
    attrs: facet ? html` data-facet="${facet}"` : '', label, mark: lampGlyph(state), val: html`<span class="ph">${value}</span>`, small });
  function drawHead() {
    const paths = Object.keys(DOC.paths), [, lastWeek, thisWeek] = DOC.totals;
    const first = trials.map(s => s.trial[1]).sort()[0];
    const days = first ? Math.max(0, Math.ceil((Date.parse(first) - Date.parse(DOC.read)) / 864e5)) : 0;
    const other = others();
    document.getElementById('sub').textContent = `The sd pack catalog: ${plural(S.length, 'skill')}. ${onPath.length} on paths, ${trials.length} on trial, ${contrib.length} in contrib.`;
    put(document.getElementById('annunciator'), cells([
      lamp(onPath.length && 'path', 'ok', 'Paths', html`<b>${onPath.length}</b> on ${plural(paths.length, 'path')}`, paths.join(' · ')),
      lamp(trials.length && 'trial', 'ok', 'Trials', html`<b>${trials.length}</b> on trial`, first ? `first ends ${md(first)} · ${plural(days, 'day')}` : 'none running'),
      lamp(contrib.length && 'contrib', 'ok', 'Contrib', html`<b>${contrib.length}</b> to try`, 'not installed'),
      // build: a plain lamp, not the reference's link to Metrics: Metrics has no v2 page yet.
      lamp('', 'ok', 'Use this week', html`<b>${thisWeek}</b> uses`, `${lastWeek} the week before · all skills`),
      lamp(unusedPath.length && 'unused', unusedPath.length ? 'caution' : 'ok', 'Unused on paths', html`<b>${unusedPath.length}</b> of ${onPath.length}`,
        unusedPath.length ? 'no use in three weeks · demote?' : 'every path skill was used'),
      { href: '?row=hygiene', attrs: html` data-open="hygiene"`, state: DOC.junk ? 'caution' : 'ok', label: 'Use records', mark: lampGlyph(DOC.junk ? 'caution' : 'ok'),
        val: html`<span class="ph"><b>${DOC.junk}</b> rows name a path</span>`, small: DOC.junk ? 'not a skill · recorder bug' : 'every row names a skill' },
      // build: read, not drawn: the reference's "not read" lamp holds only while skill_use has no row from another surface.
      { href: '?row=surfaces', attrs: html` data-open="surfaces"`, state: other ? 'ok' : 'unknown', label: 'Codex · opencode use', mark: lampGlyph(other ? 'ok' : 'unknown'),
        val: html`<span class="ph"><b>${other || '—'}</b> ${other ? 'rows' : 'not read'}</span>`, small: other ? 'from surfaces other than claude' : html`no rows · <code>sd skill scan</code> not run` },
      // build: refresh rereads /api/skills instead of reloading the page.
      { button: true, attrs: html` id="refresh"`, label: 'Observed', mark: I('rotate-ccw'), val: html`<span class="ph"><b>${String(DOC.read).slice(11, 16)}</b> UTC</span>`,
        small: `${String(DOC.read).slice(0, 10)} · refresh` },
    ]));
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
  const UNFILTER = { facet: () => { filter = 'all'; }, q: () => { q = ''; input.value = ''; window.shell.filter(''); } };
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
    window.shell.view?.count('catalog', DOC ? v.length : null);
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
  headEl.addEventListener('click', e => { if (DOC && window.shell.list.sortBy(e, L)) window.shell.list.keepFocus(e, redraw); });
  pagerEl.addEventListener('click', e => { if (DOC && window.shell.list.paging(e, L)) window.shell.list.keepFocus(e, redraw); });
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
    if (id === 'track' || /^(repo|source):/.test(id)) return showRepo(id);
    if (id === 'adopt' || String(id).startsWith('user:')) return showAdopt(); // a user skill's details are its Adopt panel line
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
        ${s.imported ? html`<dt>Imported from</dt><dd>${s.imported.url} at ${s.imported.commit.slice(0, 12)}</dd>` : ''}
        ${s.trial ? html`<dt>Trial</dt><dd>${String(s.trial[0]).slice(0, 10)} → ${String(s.trial[1]).slice(0, 10)}</dd>` : ''}<dt>Source</dt><dd>${s.source}</dd><dt>Revision</dt><dd>${String(s.revision).slice(0, 12)}</dd></dl>
      ${s.q.length ? html`<p class="why">Asked from here: ${s.q.join(' · ')}</p>` : ''}
      <details class="when"><summary>When to use</summary><p>${s.when}</p></details>
      <h3>Act</h3>${C.bar(id)}<div id="form-slot"></div><div id="intake-slot"></div>`);
    drawIntake();
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
      <p class="desc">Evaluate runs <code>sd skill intake</code>: it fetches the skill at one commit, scores its risk, its overlap with your skills and its value, and shows the diff. Nothing installs until you approve the revision its report printed.</p>
      <form class="form" id="adopt-form" novalidate><label>Source<input id="ad-src" value="${prefill}" placeholder="https://github.com/owner/repo/tree/main/path/to/skill" autocomplete="off" aria-describedby="ad-err"></label>
        <div class="parsed" id="ad-parsed" aria-live="polite"></div>
        <div class="row"><label class="target">Install to<select id="ad-scope"><option value="pack">pack · contrib/</option><option value="user">user · ~/.claude/skills</option></select></label>
          <button class="btn" type="submit">Evaluate</button></div>
        <p class="err" id="ad-err" hidden></p></form>
      ${U.length ? html`<h3>Imported to ~/.claude/skills</h3><ul class="imported">${U.map(u => html`<li><span><b>${u.name}</b><span class="src">${u.url} at ${u.commit.slice(0, 12)}</span></span>${C.rowActions(u.id)}</li>`)}</ul>` : ''}
      <div id="intake-slot"></div>
      <h3>Same thing from the CLI</h3><div class="cli"><code id="ad-intake"></code><button class="icon-btn" type="button" id="ad-intake-copy" aria-label="Copy the intake line">${I('copy')}</button></div>
      <h3>Or adopt by hand</h3><p class="why"><code>sd-skill-adopt</code> also takes owner/repo shorthand; the dashboard does not run it.</p>
      <div class="cli"><code id="ad-cli"></code><button class="icon-btn" type="button" id="ad-copy" aria-label="Copy the adopt line">${I('copy')}</button></div>`);
    const src = details.querySelector('#ad-src'), out = details.querySelector('#ad-parsed'), cli = details.querySelector('#ad-cli'), copy = details.querySelector('#ad-copy');
    const scope = details.querySelector('#ad-scope'), err = details.querySelector('#ad-err'), line2 = details.querySelector('#ad-intake'), copy2 = details.querySelector('#ad-intake-copy');
    const upd = () => { const p = parseSource(src.value), line = `claude -p ${shq(`/sd-skill-adopt ${p ? `${p.owner}/${p.repo}${p.path ? '/' + p.path : ''}` : 'SOURCE'}`)}`;
      put(out, html`${p ? html`<span class="chip">repo: ${p.owner}/${p.repo}</span><span class="chip${p.path ? '' : ' miss'}">path: ${p.path || 'repo root'}</span><span class="chip">lands as: contrib/${p.name}</span>${byName[p.name] ? html`<span class="chip miss">name taken: ${p.name} is ${byName[p.name].status}</span>` : ''}` : html``}`);
      cli.textContent = line; copy.dataset.copy = line;
      const intakeLine = `sd skill intake ${GITHUB.test(src.value.trim()) ? shq(src.value.trim()) : 'URL'} --scope ${shq(scope.value)}`;
      line2.textContent = intakeLine; copy2.dataset.copy = intakeLine; };
    src.addEventListener('input', upd); scope.addEventListener('change', upd); upd();
    // Evaluate takes a GitHub URL only: the route refuses anything else, and runs sd with it as one argument, never a shell.
    details.querySelector('#adopt-form').addEventListener('submit', e => {
      e.preventDefault(); const v = src.value.trim();
      err.hidden = GITHUB.test(v); err.textContent = err.hidden ? '' : 'Evaluate takes a https://github.com/ skill URL, such as https://github.com/owner/repo/tree/main/skill.';
      if (err.hidden) evaluate('adopt', { source: v, scope: scope.value }).then(t => window.shell.toast(t));
    });
    drawIntake();
    window.shell.setContext?.('adopt a skill'); window.shell.suggest?.(['What does sd-skill-adopt refuse?', 'Is there already a skill for this in contrib?']);
    window.shell.row?.('adopt'); window.shell.openPane('tab-details'); swap(); src.focus?.();
    return true;
  }
  document.getElementById('adopt-btn').addEventListener('click', () => showAdopt());

  // ---------- Intake: Re-import and Evaluate (sd:3992) ----------
  // One report at a time, for the selection that asked for it (a skill's name, or 'adopt'); Details redraws keep it.
  // Approve shows only under a report whose mode can install, with no refusal and nothing installed yet.
  const GITHUB = /^https:\/\/github\.com\/[\x21-\x7e]+$/;
  const DIFF_LINES = 400;
  const approveLine = r => `sd skill intake ${shq(r.url)} --scope ${shq(r.scope)} --approve ${shq(r.revision)}`;
  let intake = null;
  const refused = r => r.risk.some(f => f.severity === 'refuse');
  const approvable = r => !r.installed && (r.mode === 'new' || r.mode === 're-import') && !refused(r);
  const verdictMark = r => r.installed ? ['●', 'ok'] : refused(r) ? ['■', 'warning'] : r.verdict.action === 'install' ? ['●', 'ok'] : ['▲', 'caution'];
  const diffLine = l => html`<span class="${/^(\+\+\+|---|@@)/.test(l) ? 'hd' : l[0] === '+' ? 'add' : l[0] === '-' ? 'del' : ''}">${l}</span>`;
  // The pack skills whose Lineage or Provenance names the import's repository; a failed git log is unknown, never none.
  const informedOf = rows => rows.length ? html`<ul class="risk">${rows.map(x => html`<li>${x.commits === null
    ? html`<span class="g-unknown" aria-hidden="true">▨</span> ${x.skill} (${x.path}) names ${x.names}: unknown: ${x.unknown}`
    : `${x.skill} (${x.path}) names ${x.names}: ${x.commits.join(', ') || 'no commit adds it'}`}</li>`)}</ul>` : 'none';
  const rollbackLine = r => `sd skill intake ${shq(r.skill)} --rollback --scope ${shq(r.scope)} --approve ${shq(r.revision)}`;
  function rollbackOf(r) {
    const shown = r.diff.slice(0, DIFF_LINES), more = r.diff.length - shown.length, gone = r.mode === 'remove';
    const after = r.rolled_back ? html`<p class="why"><span class="g-ok" aria-hidden="true">●</span> Rolled back at revision <code>${r.revision}</code>. The replaced copy is kept: a second Rollback undoes it.</p>`
      : html`<div class="row"><button class="btn" type="button" data-rollback>Approve rollback ${r.revision}</button></div>
        <p class="why">Swaps the installed copy with the kept one. If either changed since this report, the rollback is refused.</p>`;
    const line = r.rolled_back ? `sd skill intake ${shq(r.skill)} --rollback --scope ${shq(r.scope)}` : rollbackLine(r);
    return html`<section class="intake" aria-labelledby="intake-h"><h3 id="intake-h">Rollback report</h3>
      <p class="verdict"><span class="g-${gone ? 'caution' : 'ok'}" aria-hidden="true">${gone ? '▲' : '●'}</span> <b>${gone ? 'removes the skill' : 'restores the kept copy'}</b> · ${gone ? 'the last install put it there new' : 'the copy the last install replaced'}</p>
      <dl><dt>Skill</dt><dd>${r.skill} · ${r.scope}</dd>
        <dt>Installed from</dt><dd>${r.installed_commit ? r.installed_commit.slice(0, 12) : 'not recorded'}</dd>
        <dt>Kept copy</dt><dd>${gone ? 'none: the skill was new' : r.kept_commit ? `from ${r.kept_commit.slice(0, 12)}` : 'records no source'}</dd>
        <dt>Informed</dt><dd>${informedOf(r.informed)}</dd>
        <dt>Revision</dt><dd>${r.revision}</dd></dl>
      ${r.informed.length ? html`<p class="note">Rollback changes none of these skills. File a revert item for each one to undo; it lands through the lane.</p>` : ''}
      ${r.recovered.length ? html`<p class="note">${r.recovered.join(' · ')}</p>` : ''}
      <h3>Diff from the installed copy to the kept one</h3>${shown.length ? html`<pre class="diff">${shown.map(diffLine)}</pre>` : html`<p class="why">No diff.</p>`}
      ${more > 0 ? html`<p class="why">${more} more lines; <code>sd skill intake --rollback --json</code> holds them all.</p>` : ''}
      ${after}<div class="cli"><code>${line}</code><button class="icon-btn" type="button" aria-label="Copy: ${line}" data-copy="${line}">${I('copy')}</button></div></section>`;
  }
  function reportOf(r) {
    // A refresh's report carries its first diff lines and diff_lines, their count (system sd:5478); an evaluation, all of them.
    const [g, st] = verdictMark(r), shown = r.diff.slice(0, DIFF_LINES), more = (r.diff_lines ?? r.diff.length) - shown.length;
    const after = r.installed ? html`<p class="why"><span class="g-ok" aria-hidden="true">●</span> Installed at revision <code>${r.revision}</code>.</p>`
      : approvable(r) ? html`<div class="row"><button class="btn" type="button" data-approve>Approve revision ${r.revision}</button></div>
        <p class="why">Installs this revision only. If the source or the installed copy changed since this report, the install is refused.</p>`
      : html`<p class="why">${refused(r) ? 'A refusal blocks any install; nothing was written.' : r.mode === 'unchanged' ? 'The installed copy matches upstream; nothing to approve.' : 'Nothing to approve.'}</p>`;
    const line = approvable(r) ? approveLine(r) : `sd skill intake ${shq(intake.body.source)}${intake.body.scope ? ` --scope ${shq(intake.body.scope)}` : ''}`;
    return html`<section class="intake" aria-labelledby="intake-h"><h3 id="intake-h">Intake report</h3>
      <p class="verdict"><span class="g-${st}" aria-hidden="true">${g}</span> <b>${r.verdict.action}</b> · ${r.verdict.reason}</p>
      <dl><dt>Source</dt><dd>${r.url} at ${r.commit.slice(0, 12)}</dd>
        <dt>Lands as</dt><dd>${r.skill} · ${r.scope} · ${r.mode}${r.installed_commit ? ` · installed from ${r.installed_commit.slice(0, 12)}` : ''}</dd>
        <dt>Risk</dt><dd>${r.risk.length ? html`<ul class="risk">${r.risk.map(f => html`<li><span class="g-${f.severity === 'refuse' ? 'warning' : 'caution'}" aria-hidden="true">${f.severity === 'refuse' ? '■' : '▲'}</span> ${f.severity}: ${f.rule} in ${f.file}${f.line ? ` line ${f.line}` : ''}</li>`)}</ul>` : 'no finding'}</dd>
        <dt>Match</dt><dd>${r.match.map(m => `${m.skill} ${m.overlap}`).join(' · ') || 'none'}</dd>
        <dt>Value</dt><dd>${plural(r.value.instruction_lines, 'instruction line')} · novelty ${r.value.novelty}</dd>
        ${r.informed ? html`<dt>Informed</dt><dd>${informedOf(r.informed)}</dd>` : ''}
        <dt>Revision</dt><dd>${r.revision}</dd></dl>
      ${r.recovered.length ? html`<p class="note">${r.recovered.join(' · ')}</p>` : ''}
      <h3>Diff against the installed copy</h3>${shown.length ? html`<pre class="diff">${shown.map(diffLine)}</pre>` : html`<p class="why">No diff${r.mode === 'blocked' ? ': the name belongs to another skill' : ''}.</p>`}
      ${more > 0 ? html`<p class="why">${more} more lines; <code>sd skill intake --json</code> holds them all.</p>` : ''}
      ${after}<div class="cli"><code>${line}</code><button class="icon-btn" type="button" aria-label="Copy: ${line}" data-copy="${line}">${I('copy')}</button></div></section>`;
  }
  function drawIntake() {
    if (!intake || intake.for !== selected) return;
    const slot = document.getElementById('intake-slot'); if (!slot) return;
    put(slot, intake.wait ? html`<p class="why" role="status"><span class="g-queued" aria-hidden="true">◌</span> ${intake.body.rollback ? `Reading the kept copy of ${intake.body.source}.` : `Evaluating ${intake.body.source}: fetching it from GitHub and scoring it.`}</p>`
      : intake.error ? html`<p class="err" role="alert"><span class="g-unknown" aria-hidden="true">▨</span> ${intake.error}</p>`
      : intake.body.rollback ? rollbackOf(intake.report) : reportOf(intake.report));
  }
  // The answer is the toast text: an evaluation that failed wrote nothing, so its reason is the result, not a refused write.
  function evaluate(key, body) {
    intake = { for: key, body, wait: true }; drawIntake();
    return window.shell.post('/api/skills/intake', body).then(r => { intake = { for: key, body, report: r }; drawIntake(); return body.rollback ? `Rollback report for ${r.skill}: ${r.mode}` : `Report for ${r.skill}: ${r.verdict.action}`; },
      e => { intake = { for: key, body, error: e.message }; drawIntake(); return e.message; });
  }
  const APPROVE = { id: 'skill.intake.approve', on: 'intake report', label: 'Approve revision', does: 'Installs this revision over the current copy; this cannot be undone.', risk: 'confirm',
    consequence: o => `This installs ${o.label} at revision ${o.revision} into ${o.destination}, replacing the copy there. It installs only what the report showed.`,
    cli: o => approveLine(o),
    run: o => { const at = intake;
      return window.shell.post('/api/skills/intake', { source: o.url, scope: o.scope, approve: o.revision }).then(r => {
        intake = { ...at, report: r }; drawIntake(); reread(); repoReader?.reread();
        return r.installed ? `Installed ${r.skill} at revision ${r.revision}` : `Nothing installed: ${r.mode}`;
      }, e => {
        // A 502 is an install whose outcome is not known; a refusal (400, or 409 for a changed source) wrote nothing.
        intake = { ...at, report: null, error: e.message }; drawIntake();
        if (e.status === 502) return `Install of ${o.label} not known: ${e.message}`;
        throw e;
      }); } };
  const ROLLBACK = { id: 'skill.intake.rollback', on: 'rollback report', label: 'Approve rollback', does: 'Swaps the installed copy with the kept one; a second rollback undoes it.', risk: 'confirm',
    consequence: o => `This swaps ${o.label} in ${o.destination} with the copy its last install replaced${o.mode === 'remove' ? ', which removes it' : ''}. The replaced copy is kept, so a second rollback undoes it.`,
    cli: o => rollbackLine(o),
    run: o => { const at = intake;
      return window.shell.post('/api/skills/intake', { source: o.skill, scope: o.scope, rollback: true, approve: o.revision }).then(r => {
        intake = { ...at, report: r }; drawIntake(); reread(); repoReader?.reread();
        return `Rolled back ${r.skill}`;
      }, e => {
        // A 502 is a rollback whose outcome is not known; a refusal (400, or 409 for a changed copy) wrote nothing.
        intake = { ...at, report: null, error: e.message }; drawIntake();
        if (e.status === 502) return `Rollback of ${o.label} not known: ${e.message}`;
        throw e;
      }); } };
  details.addEventListener('click', e => {
    if (!e.target.closest('[data-rollback]') || !intake?.body?.rollback || !intake.report || intake.report.rolled_back) return;
    const r = intake.report;
    C.run(ROLLBACK, { id: 'rollback', type: 'rollback report', label: r.skill, skill: r.skill, scope: r.scope, revision: r.revision, destination: r.destination, mode: r.mode });
  });
  details.addEventListener('click', e => {
    if (!e.target.closest('[data-approve]') || !intake?.report || !approvable(intake.report)) return;
    const r = intake.report;
    C.run(APPROVE, { id: 'intake', type: 'intake report', label: r.skill, url: r.url, scope: r.scope, revision: r.revision, destination: r.destination });
  });
  // ---------- Intake view: tracked repositories (system sd:5478) ----------
  // The list is `sd skill intake --tracked`: the skills.intake_repos setting, or, until it is first written, the repositories
  // the installed skills came from (Provenance, the skills lock file, linked checkouts); then the ones not tracked. One refresh at a
  // time per repository; its answer stays in REFRESH and draws under that repository's Details, where Show report puts one
  // skill's report in the intake slot and Approve is the intake's own (APPROVE above).
  const TRACKING = '/api/skills/intake/tracking';
  const REPO_URL = /^https:\/\/github\.com\/[A-Za-z0-9][A-Za-z0-9-]{0,38}\/(?!\.\.?$)[A-Za-z0-9._-]{1,100}$/;
  let VIEW = null, REPOS = null, REPO_FAILED = '', repoReader = null;
  const REFRESH = {}; // per URL: { wait } while it runs, then { report } or { error }
  const repoRows = document.getElementById('intake-rows');
  const repoOf = id => String(id).replace(/^(repo|source):/, '');
  const when = iso => window.shell.time ? window.shell.time(iso) : utc(iso);
  const scopeOf = skills => skills.length && skills.every(s => s.scope === 'user') ? 'user' : 'pack';
  // A refresh's skill: new, changed (a re-import), unchanged, blocked (the name is another skill's), gone or failed.
  const bucket = r => r.mode === 'gone' ? 'gone' : r.error ? 'failed' : r.mode === 're-import' ? 'changed' : r.mode;
  const BUCKET = { new: ['▲', 'caution', 'new'], changed: ['▲', 'caution', 'changed'], unchanged: ['●', 'ok', 'unchanged'],
    blocked: ['■', 'warning', 'blocked'], gone: ['▲', 'caution', 'removed upstream'], failed: ['▨', 'unknown', 'not read'] };
  // Caution outranks unknown (design.md): a refresh that found skills to review and failed on others is ▲, and says both.
  const repoGlyph = r => {
    const res = r.result || '', part = /\d+ failed\b/.test(res) ? ' and could not read every skill' : '';
    return REFRESH[r.url]?.wait ? ['◌', 'queued', 'Refreshing now']
      : !r.refreshed ? ['▨', 'unknown', 'Not refreshed yet']
      : /^failed/.test(res) ? ['▨', 'unknown', 'The last refresh failed']
      : /\d+ (new|changed|blocked|gone)\b/.test(res) ? ['▲', 'caution', `The last refresh found skills to review${part}`]
      : part ? ['▨', 'unknown', `The last refresh found nothing new${part}`]
      : ['●', 'ok', 'The last refresh found nothing new or changed'];
  };
  const refreshed = r => REFRESH[r.url]?.wait ? 'refreshing…' : r.refreshed ? html`${when(r.refreshed)} · ${r.result}` : 'never';
  function adoptRepos(doc) {
    REPOS = doc; REPO_FAILED = '';
    const n = doc.repos.length;
    return { objects: [...doc.repos.map(r => ({ id: `repo:${r.url}`, type: 'tracked repo', label: r.url, url: r.url, scope: r.scope })),
      ...doc.untracked.map(u => ({ id: `source:${u.url}`, type: 'untracked repo', label: u.url, url: u.url, scope: scopeOf(u.skills) }))],
      state: n || doc.untracked.length ? null : { kind: 'empty', title: 'No tracked repository',
        text: doc.seeded ? 'No installed skill was adopted by sd skill intake, so nothing seeds the list. Track a repository adds one.'
          : 'The list is empty. Track a repository adds one.', source: '/api/skills/intake/repos' } };
  }
  function drawRepos() {
    const rows = REPOS ? REPOS.repos : [];
    put(repoRows, html`${rows.length ? rows.map(r => { const id = `repo:${r.url}`, [g, st, why] = repoGlyph(r); return html`<tr data-id="${id}" aria-selected="${String(id === selected)}">
      <td class="g g-${st}" title="${why}">${g}<span class="sr">${why}</span></td>
      <td class="name"><button type="button">${r.url}</button><p>${r.scope === 'user' ? 'user · ~/.claude/skills' : 'pack · contrib/'}</p></td>
      <td class="avail">${r.skills.length ? r.skills.map(x => x.name).join(', ') : 'none installed'}</td>
      <td class="avail">${refreshed(r)}</td>
      <td class="act">${C.rowActions(id)}</td></tr>`; })
      : html`<tr class="lane"><td colspan="5">${REPOS ? 'No repository is tracked.' : REPO_FAILED ? `Not read: ${REPO_FAILED}` : 'Not read yet.'}</td></tr>`}`);
    put(document.getElementById('intake-untracked'), html`${REPOS?.untracked.length ? html`<h3>Adopted from, not tracked</h3>
      <ul class="imported">${REPOS.untracked.map(u => html`<li><span><b>${u.url}</b><span class="src">${u.skills.map(x => x.name).join(', ')}</span></span>${C.rowActions(`source:${u.url}`)}</li>`)}</ul>` : ''}`);
    document.getElementById('intake-tally').textContent = REPOS?.seeded ? 'From the installed skills, not yet saved' : '';
    put(document.getElementById('intake-foot'), REPOS ? html`The list is the <code>${REPOS.key}</code> setting; until it is first changed, it is read from the installed skills: their Provenance, the <code>skills</code> lock file and linked GitHub checkouts. Remove keeps the installed skills.` : html``);
    window.shell.view?.count('intake', REPOS ? rows.length : null);
  }
  const markRepos = () => repoRows.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === selected)));
  function showRepo(id) {
    if (id === 'track') return showTrack();
    const url = repoOf(id), tracked = id.startsWith('repo:');
    const r = REPOS && (tracked ? REPOS.repos : REPOS.untracked).find(x => x.url === url); if (!r) return false;
    selected = id; markRepos(); C.select(id);
    const [g, st, why] = tracked ? repoGlyph(r) : ['▨', 'unknown', 'Not tracked: Refresh does not read it'];
    put(details, html`<p class="kind"><span class="g-${st}" aria-hidden="true">${g}</span> ${tracked ? `Tracked repository · ${r.scope}` : 'Adopted from, not tracked'}</p>
      <h2>${url}</h2><p class="desc">${why}.</p>
      <dl><dt>Adopted</dt><dd>${r.skills.length ? r.skills.map(x => `${x.name} (${x.scope})`).join(' · ') : 'no installed skill'}</dd>
        ${tracked ? html`<dt>Last refresh</dt><dd>${refreshed(r)}</dd>` : ''}</dl>
      <h3>Act</h3>${C.bar(id)}<div id="refresh-slot"></div><div id="intake-slot"></div>`);
    drawRefresh(); drawIntake();
    window.shell.setContext?.(url); swap();
    return true;
  }
  function drawRefresh() {
    const slot = document.getElementById('refresh-slot'), at = REFRESH[repoOf(selected)];
    if (!slot || !String(selected).startsWith('repo:')) return;
    if (!at) return put(slot, html``);
    if (at.wait) return put(slot, html`<p class="why" role="status"><span class="g-queued" aria-hidden="true">◌</span> Refreshing ${repoOf(selected)}: fetching its default branch and reading each skill. Nothing installs.</p>`);
    if (at.error) return put(slot, html`<p class="err" role="alert"><span class="g-unknown" aria-hidden="true">▨</span> ${at.error}</p>`);
    const out = at.report, listed = out.skills.map((r, i) => [r, i]).filter(([r]) => bucket(r) !== 'unchanged'), same = out.skills.length - listed.length;
    put(slot, html`<section class="intake" aria-labelledby="refresh-h"><h3 id="refresh-h">Refresh at ${out.branch} ${out.commit.slice(0, 12)}</h3>
      <p class="verdict"><b>${out.result}</b> · nothing installed${out.recorded ? '' : ` · not saved to the list: ${out.unrecorded}`}</p>
      ${listed.length ? html`<ul class="imported">${listed.map(([r, i]) => { const [g0, st0, label] = BUCKET[bucket(r)] || BUCKET.failed, [g, st] = !r.error && refused(r) ? ['■', 'warning'] : [g0, st0];
        return html`<li><span><b><span class="g-${st}" aria-hidden="true">${g}</span> ${r.skill || r.path || 'repository root'} · ${label}</b><span class="src">${r.error || `${r.verdict.action} · ${r.verdict.reason}`}</span></span>${r.error ? '' : html`<button class="btn quiet sm" type="button" data-report="${i}">Show report</button>`}</li>`; })}</ul>` : ''}
      ${same ? html`<p class="why">${plural(same, 'skill')} ${same === 1 ? 'matches' : 'match'} the installed copy.</p>` : ''}
      ${out.recovered.length ? html`<p class="note">${out.recovered.join(' · ')}</p>` : ''}</section>`);
  }
  // Show report: that skill's report under the refresh, as Evaluate would show it; Approve there posts /api/skills/intake.
  details.addEventListener('click', e => {
    const b = e.target.closest('[data-report]'), out = REFRESH[repoOf(selected)]?.report; if (!b || !out) return;
    const r = out.skills[Number(b.dataset.report)]; if (!r || r.error) return;
    intake = { for: selected, body: { source: r.url, scope: r.scope }, report: r }; drawIntake();
  });
  // A refresh's answer is a report, not a write the list must show: it reads the list again for the refresh time it saved.
  function refresh(o) {
    REFRESH[o.url] = { wait: true }; if (intake?.for === `repo:${o.url}`) intake = null;
    drawRepos(); if (selected === `repo:${o.url}`) showRepo(selected);
    return window.shell.post(TRACKING, { action: 'refresh', url: o.url }).then(out => {
      REFRESH[o.url] = { report: out };
      // Until the reread lands, the row shows the time and result this refresh saved.
      const row = REPOS?.repos.find(x => x.url === o.url); if (row && out.recorded) Object.assign(row, { refreshed: out.refreshed, result: out.result });
      drawRepos(); drawRefresh(); repoReader.reread();
      return `Refreshed ${o.url}: ${out.result}. Nothing installed`;
    }, e => {
      REFRESH[o.url] = { error: e.message }; drawRepos(); drawRefresh(); repoReader.load();
      if (e.status === 502) return `Refresh of ${o.url} not finished: ${e.message}`;
      throw e;
    });
  }
  // A list write the verb did not answer may have landed (502): the list is read again and the toast says it is not known.
  const unknown = (o, what) => e => { repoReader.load(); if (e.status === 502) return `${what} of ${o.url} not known: ${e.message}`; throw e; };
  const trackPost = (url, scope) => window.shell.post(TRACKING, { action: 'track', url, ...(scope ? { scope } : {}) });
  function showTrack() {
    selected = 'track'; C.select(null); markRepos();
    put(details, html`<p class="kind">${I('plus')} Track a repository</p><h2>Track a repository</h2>
      <p class="desc">Refresh reads a tracked repository's default branch and lists its new and changed skills, each with its intake report. Nothing installs until you approve a report.</p>
      <form class="form" id="track-form" novalidate><label>Repository<input id="tr-url" placeholder="https://github.com/owner/repo" autocomplete="off" aria-describedby="tr-err"></label>
        <div class="row"><label class="target">New skills install to<select id="tr-scope"><option value="pack">pack · contrib/</option><option value="user">user · ~/.claude/skills</option></select></label>
          <button class="btn" type="submit">Track</button></div>
        <p class="err" id="tr-err" hidden></p></form>
      <h3>Same thing from the CLI</h3><div class="cli"><code id="tr-cli"></code><button class="icon-btn" type="button" id="tr-copy" aria-label="Copy the track line">${I('copy')}</button></div>`);
    const url = details.querySelector('#tr-url'), scope = details.querySelector('#tr-scope'), err = details.querySelector('#tr-err');
    const cli = details.querySelector('#tr-cli'), copy = details.querySelector('#tr-copy');
    const upd = () => { const line = `sd skill intake --track ${REPO_URL.test(url.value.trim()) ? shq(url.value.trim()) : 'URL'} --scope ${shq(scope.value)}`; cli.textContent = line; copy.dataset.copy = line; };
    url.addEventListener('input', upd); scope.addEventListener('change', upd); upd();
    details.querySelector('#track-form').addEventListener('submit', e => {
      e.preventDefault(); const v = url.value.trim();
      err.hidden = REPO_URL.test(v); err.textContent = err.hidden ? '' : 'Track takes a repository URL, https://github.com/<owner>/<repo>, with nothing after it.';
      if (!err.hidden) return;
      trackPost(v, scope.value).then(out => { window.shell.toast(`Tracking ${out.url}`); repoReader.reread(); }, e2 => { repoReader.load(); window.shell.toast(e2.status === 502 ? `Tracking of ${v} not known: ${e2.message}` : `${v} not tracked: ${e2.message}`); });
    });
    window.shell.setContext?.('track a repository'); window.shell.row?.('track'); window.shell.openPane('tab-details'); swap(); url.focus?.();
    return true;
  }
  document.getElementById('track-btn').addEventListener('click', () => showTrack());
  const REPO_COMMANDS = [
    { id: 'skill.intake.refresh', on: 'tracked repo', label: 'Refresh', does: 'Fetches the repository and lists its new and changed skills; installs nothing.', key: 'e', risk: 'safe', icon: 'rotate-ccw', primary: () => true,
      when: o => REFRESH[o.url]?.wait ? 'refreshing now' : true,
      cli: o => `sd skill intake --refresh ${shq(o.url)}`, run: o => { select(`repo:${o.url}`, true); return refresh(o); } },
    { id: 'skill.intake.untrack', on: 'tracked repo', label: 'Remove', does: 'Stops tracking this repository; its installed skills stay. Undo tracks it again.', key: 'm', risk: 'undo',
      cli: o => `sd skill intake --untrack ${shq(o.url)}`,
      run: o => window.shell.post(TRACKING, { action: 'untrack', url: o.url }).then(out => { delete REFRESH[o.url]; repoReader.reread();
        return { text: `Stopped tracking ${o.url}; its installed skills stay`, removed: out.removed }; }, unknown(o, 'Removal')),
      // Undo tracks the repository again with the scope it had; its last refresh time and result are not restored.
      undo: (o, v) => trackPost(o.url, v?.removed?.scope || o.scope).then(() => { repoReader.reread(); }, e => { repoReader.load(); throw e; }) },
    { id: 'skill.intake.track', on: 'untracked repo', label: 'Track', does: 'Adds this repository to the tracked list, so Refresh reads it.', key: 't', risk: 'safe', icon: 'plus', primary: () => true,
      cli: o => `sd skill intake --track ${shq(o.url)}`, run: o => trackPost(o.url).then(() => { repoReader.reread(); return `Tracking ${o.url}`; }, unknown(o, 'Tracking')) },
  ];
  repoRows.addEventListener('click', e => {
    if (e.target.closest('.rowact')) return;
    const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
  });
  const repoSpec = () => ({
    source: '/api/skills/intake/repos', what: 'the tracked repositories', slot: 'state-intake', active: () => VIEW === 'intake',
    adopt: adoptRepos, draw: drawRepos,
    clear: err => { REPOS = null; REPO_FAILED = err.message; },
    current: () => selected,
    first: () => REPOS?.repos[0] ? `repo:${REPOS.repos[0].url}` : REPOS?.untracked[0] ? `source:${REPOS.untracked[0].url}` : null,
    select: (id, opened) => (id === 'track' || /^(repo|source):/.test(id) ? select(id, opened) : null),
    unselect: () => { selected = null; markRepos(); put(details, html`<p class="why">${REPOS ? 'No repository is tracked, so nothing is selected. Track a repository adds one.' : 'The tracked repositories were not read, so nothing is selected. Reload retries it.'}</p>`); },
  });
  // The catalog's selection and the Intake view's are one: a view opens on its own first row, or keeps the one it had.
  // The list is read the first time the view opens. The shell's first shell:view can fire before this listener (it listens
  // on document, this page on window), so the start below also asks shell.view which view is shown.
  function onView(id) {
    VIEW = id;
    if (input.value) sayAs();
    if (VIEW === 'intake') {
      if (!repoReader) { repoReader = window.shell.read(repoSpec()); repoReader.load(); }
      else if (REPOS && !/^(repo|source):|^track$/.test(selected || '')) { const f = repoSpec().first(); if (f) select(f, false); }
    } else if (DOC && !(byName[selected] || KEEP(selected))) { const f = visible()[0]?.name; if (f) select(f, false); }
  }
  document.addEventListener('shell:view', e => onView(e.detail.id));
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
  // The As line; a filter names the shown view's table.
  function sayAs() {
    const v = input.value.trim(), name = (v.match(/^run\s+([\w-]+)/i) || [])[1];
    put(as, html`${mode === 'run' ? html`${I('play')} run ${name && byName[name] ? html`<b>${name}</b>` : html`<span>a skill name</span>`}`
      : mode === 'adopt' ? html`${I('download')} adopt from ${v}` : html`${I('filter')} filter ${VIEW === 'intake' ? 'the tracked repositories' : 'the catalog'}`}`);
  }
  function render() {
    const v = input.value.trim(); prev.hidden = !v; ghost.textContent = v ? `→ ${mode}` : '';
    sayAs();
    q = mode === 'filter' ? v.toLowerCase() : ''; L.page = 1;
    // The shell filters the tracked repositories with the same text (sd:5539); the catalog is this page's.
    window.shell.filter(q);
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
  window.PAGE_LIST = { rows: () => (VIEW === 'intake' ? repoRows : tbody).querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false), keep: id => KEEP(id) || id === 'track' };

  // ---------- Commands (products/system/commands.md) ----------
  // Each skill is an object; each command is declared once and the shell renders row, menu, Details and palette.
  // A write's answer is its own: the reread after it is started, not awaited, so a failed reread never reports a landed write
  // as "not changed". Until the reread lands, the row says what was asked. A refused write reads again with load().
  // Review, Promote and Demote file an sd item and queue no runner work: it waits in Tasks like any other (system sd:5477).
  const sk = o => byName[o.id] || o, asked = (o, l) => sk(o).q.includes(l) ? `${l} already` : true;
  const pend = (o, label) => { const s = byName[o.id]; if (!s) return; s.q = [...s.q.filter(x => x !== label), label]; drawRows(); if (selected === s.name) show(s.name); };
  const write = (o, action, label, done, body = {}) => window.shell.post(`/api/skills/${encodeURIComponent(o.id)}/${action}`, { revision: sk(o).revision, ...body })
    .then(out => { pend(o, label); reread(); return done(out); }, e => { load(); throw e; });
  const itemOf = out => ` in Tasks${out?.item?.id ? ` as item #${out.item.id}` : ''}`;
  const PROMOTE = { id: 'skill.promote.queue', on: 'skill form', label: 'Promote', does: 'Files a task in Tasks that moves the skill onto this path.', risk: 'safe', askFirst: true,
    consequence: o => `This files a task in Tasks to move ${o.id} into skills/ on the ${o.path} path, in an isolated checkout, with one pull request. Close the task there to drop it.`,
    cli: o => `sd skill promote ${shq(o.id)} --path ${shq(o.path)}`,
    run: o => write(o, 'promote', `promotion to ${o.path} filed`, out => `Promotion of ${o.id} to ${o.path} filed${itemOf(out)}`, { path_name: o.path }) };
  function registerCommands() {
    C.register(
      { id: 'skill.try', on: 'skill', label: 'Try', does: 'Starts a 30-day trial at the next install; it cannot end early.', key: 't', icon: 'flask-conical', risk: 'confirm', bulk: true, primary: o => sk(o).status === 'contrib',
        when: o => sk(o).status !== 'contrib' ? `already installed · ${sk(o).status}` : asked(o, 'trial queued'),
        consequence: o => `This starts a 30-day trial of ${o.id}; it installs at the next sd install. No verb ends a trial early.`,
        cli: o => `sd skill try ${shq(o.id)}`, run: o => write(o, 'try', 'trial queued', () => `Trial of ${o.id} starts at the next install`) },
      { id: 'skill.review', on: 'skill', label: 'Review', does: 'Files an item in Tasks for an independent review of this skill.', key: 'v', risk: 'safe', askFirst: true, bulk: true, when: o => asked(o, 'review filed'),
        consequence: o => `This files an item in Tasks for a review of ${o.id} by a reviewer independent of its latest author.`,
        cli: o => `sd skill review ${shq(o.id)}`, run: o => write(o, 'review', 'review filed', out => `Review of ${o.id} filed${itemOf(out)}`) },
      { id: 'skill.promote', on: 'skill', label: 'Promote', does: 'Opens the form that picks a path to promote this skill to.', key: 'p', risk: 'safe', fills: true, when: o => sk(o).status !== 'path' || `on ${sk(o).paths.join(', ')} already`,
        cli: o => `sd skill promote ${shq(o.id)} --path PATH`, run: o => { select(o.id, true); openPromote(sk(o)); return null; } },
      { id: 'skill.demote', on: 'skill', label: 'Demote', does: 'Files a task in Tasks that moves the skill off every path.', key: 'd', icon: 'arrow-down', risk: 'safe', askFirst: true, primary: o => sk(o).status === 'path' && !total(sk(o)),
        when: o => sk(o).status === 'path' ? asked(o, 'demotion filed') : 'not on a path',
        consequence: o => `This files a task in Tasks to move ${o.id} from skills/ to contrib/ and off every path, with one pull request. Close the task there to drop it.`,
        cli: o => `sd skill demote ${shq(o.id)}`, run: o => write(o, 'demote', 'demotion filed', out => `Removal of ${o.id} from paths filed${itemOf(out)}`) },
      { id: 'skill.reimport', on: 'skill', label: 'Re-import', does: 'Reads the skill\'s source again and shows the intake report.', key: 'i', risk: 'safe', when: o => !!sk(o).imported || 'not installed by sd skill intake',
        cli: o => `sd skill intake ${shq(o.id)}`, run: o => { select(o.id, true); return evaluate(o.id, { source: o.id }); } },
      { id: 'skill.reimport.user', on: 'user skill', label: 'Re-import', does: 'Reads this user skill\'s source and shows the intake report.', key: 'i', icon: 'download', risk: 'safe', primary: () => true,
        cli: o => `sd skill intake ${shq(o.name)} --scope user`, run: o => { showAdopt(); return evaluate('adopt', { source: o.name, scope: 'user' }); } },
      { id: 'skill.rollback', on: 'skill', label: 'Rollback', does: 'Shows the swap back to the copy the last install replaced.', key: 'b', risk: 'safe', when: o => !!sk(o).imported || 'not installed by sd skill intake',
        cli: o => `sd skill intake ${shq(o.id)} --rollback`, run: o => { select(o.id, true); return evaluate(o.id, { source: o.id, rollback: true }); } },
      { id: 'skill.rollback.user', on: 'user skill', label: 'Rollback', does: 'Shows the swap back to the copy this user skill\'s last install replaced.', key: 'b', risk: 'safe',
        cli: o => `sd skill intake ${shq(o.name)} --rollback --scope user`, run: o => { showAdopt(); return evaluate('adopt', { source: o.name, scope: 'user', rollback: true }); } },
      { id: 'skill.schedule', on: 'skill', label: 'Schedule', does: 'Says why not: a scheduled job is a launchd agent, and sd adds none.', key: 's', risk: 'safe', executes: false, when: o => sk(o).status !== 'contrib' || 'not installed · try it first',
        cli: () => 'no sd verb: a job is a launchd agent', run: () => 'No sd verb adds a job: a job is a launchd agent' },
      // build: applying review notes lives on the review item's page, where the notes are.
      { id: 'skill.apply', on: 'skill', label: 'Apply', does: 'Applies review notes to the skill; run it from the review item.', key: 'a', risk: 'confirm', when: () => 'apply review notes from the review item',
        cli: () => 'sd skill apply ITEM NOTE…' },
      // sd:5886: Scan runs `sd skill scan` on the server and shows its report in Details; Read again shows the new counts.
      { id: 'skill.scan', on: 'use surfaces', label: 'Scan', does: 'Reads codex sessions into skill use records, then shows the report in Details.', key: 's', icon: 'search', risk: 'safe', executes: true, primary: () => true,
        cli: () => 'sd skill scan', sends: () => 'POST /api/run {action: skill.scan}', run: o => window.commands.runAction(o, 'skill.scan') },
      ...REPO_COMMANDS,
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
  if (u.get('q')) { input.value = u.get('q'); q = u.get('q').toLowerCase(); window.shell.filter(q); }
  const reading = window.shell.read({
    source: '/api/skills', what: 'the skills', adopt, clear, draw,
    // The catalog's reads leave the Intake view's selection alone (read.js, active).
    active: () => VIEW !== 'intake',
    current: () => selected,
    first: () => visible()[0]?.name ?? null,
    // A ?row= the filter hides goes to the first shown row, as a filter change does; the panels are kept.
    select: (id, opened) => (KEEP(id) || visible().some(s => s.name === id) ? select(id, opened) : reconcile()),
    unselect: nothing,
  });
  const load = () => reading.load(), reread = () => reading.reread();
  // Read again (sd:5638): the shell's head button reads the skills again, as the Observed lamp does.
  window.PAGE_AGAIN = { title: 'Read the skills and the tracked repositories again', run: load };
  registerCommands();
  if (window.shell.view?.current()) onView(window.shell.view.current());
  load();
});
