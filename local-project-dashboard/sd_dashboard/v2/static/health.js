// Health v2 (sd:2115): the page script. It loads before shell.js, which reads what it declares.
// Rows: /api/health, the document health_screen.document builds from the fleet child (worktree registrations) and
// reads.missing_trailers (attribution). Ported from the design source's products/system/designs/v2/health.html at e301f82.
// The design's other areas have no reader yet: each shows as unknown with what it does not read, never as a clean lamp.
// Nothing here writes. Every fix is a CLI line for Copy; Re-check reads the document again.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put, plural } = window.markup;
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const RANK = { warning: 0, caution: 1, unknown: 2, queued: 3, ok: 4 };
  const STATES = ['warning', 'caution', 'unknown', 'queued', 'ok'];
  // The design's icon and help per area; the help is page markup the shell renders with <b> and <code> only.
  const LOOK = {
    disk: ['hard-drive', '<b>Where the space went.</b> Volume use from df, then the biggest items the fleet itself creates: repo-storage folders and build output left in worktrees. A merged worktree must not keep build output (rule of 2026-09-25).'],
    cred: ['key-round', '<b>Presence and expiry only.</b> Token values are never read into this page. GitHub reports a classic PAT expiry in a response header; Home Assistant tokens carry none the dashboard can read.'],
    attr: ['signature', '<b>Who wrote each commit.</b> Every commit carries <code>Authored-with:</code> in its last paragraph; sd-review reads a missing one as “authored unknown” and blocks readiness. The count here is every author’s commits of the last 7 days in the registered repos.'],
    wt: ['folder-x', '<b>Registered is not present.</b> A worktree whose directory is gone still holds its branch. Prune clears the registration only; it never touches a directory that exists.'],
    br: ['git-branch', '<b>Merged but not deleted.</b> Local branches already contained in origin’s default branch. <code>git branch -d</code> refuses any branch that is not merged, so the fix cannot lose work.'],
    dep: ['package', '<b>Dependabot, per repo.</b> Open alerts on the unarchived repos. A repo whose alerts could not be read is unknown, never clean.'],
    sec: ['shield-alert', '<b>Secret scanning, public repos only.</b> Private repos are not scanned, by policy. A public repo with scanning off is a finding.'],
  };
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const $ = id => document.getElementById(id);

  let DOC = null, AREAS = [], ROWS = [], selected = null, query = '';
  const F = { state: new Set(), area: new Set() };
  const byId = id => ROWS.find(r => r.id === id);

  // An area with no reader, or whose reader failed, is unknown; queued rows light nothing (Today's grammar).
  const areaState = a => {
    if (!a.read || a.error) return 'unknown';
    const w = a.rows.reduce((s, r) => RANK[r.state] < RANK[s] ? r.state : s, 'ok');
    return w === 'queued' ? 'ok' : w;
  };
  const sumFact = (a, key) => a.rows.reduce((s, r) => s + (+(r.facts?.[key]) || 0), 0);
  const lampValue = a => !a.read ? html`<span class="ph">no reader</span>`
    : a.error ? html`<span class="ph">not read</span>`
    : a.id === 'wt' ? html`<span class="ph"><b>${sumFact(a, 'Registered')}</b> dir gone</span>`
    : a.id === 'attr' ? html`<span class="ph"><b>${sumFact(a, 'Missing')}</b> missing</span> <span class="ph">7 days</span>`
    : html`<span class="ph"><b>${a.rows.length}</b> rows</span>`;

  const passes = r => (!F.state.size || F.state.has(r.state)) && (!F.area.size || F.area.has(r.area))
    && (!query || `${r.what} ${r.detail} ${r.kind}`.toLowerCase().includes(query));
  const filtering = () => !!(F.state.size || F.area.size || query);
  // An area stays on the page while it has a row the filters pass, or when only the Area filter names it: a lamp for an area
  // with no reader then shows what that area does not read.
  const areaShown = a => a.rows.some(passes) || (!F.state.size && !query && (!F.area.size || F.area.has(a.id)));

  function lamps() {
    put($('annunciator'), html`${AREAS.map(a => { const s = areaState(a); return html`<li><button class="cell" type="button" data-area="${a.id}" data-state="${s}" aria-pressed="${F.area.has(a.id) ? 'true' : 'false'}"><span class="lbl">${a.name}${I(LOOK[a.id]?.[0] || 'circle-help')}</span><span class="val">${lampValue(a)}</span><span class="sr">state ${s} · shows only ${a.name} rows</span></button></li>`; })}<li><button class="cell" type="button" id="refresh"><span class="lbl">Observed${I('rotate-ccw')}</span><span class="val"><span class="ph">${DOC ? html`<b>${new Date(DOC.read).toISOString().slice(11, 16)}</b> UTC` : 'not read'}</span> · <span class="ph">refresh</span></span></button></li>`);
  }

  function renderFilters() {
    const grp = (key, name, opts) => html`<div class="fgroup"><span class="label">${name}</span><div class="chips">${opts.map(([v, l, n]) => html`<button type="button" class="chip" data-f="${key}" data-v="${v}" aria-pressed="${F[key].has(v) ? 'true' : 'false'}">${l} <span class="n">${n}</span></button>`)}</div></div>`;
    put($('filters'), html`${grp('state', 'State', STATES.filter(s => ROWS.some(r => r.state === s)).map(s => [s, html`<span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> ${s}`, ROWS.filter(r => r.state === s).length]))}${grp('area', 'Area', AREAS.map(a => [a.id, a.name, a.rows.length]))}`);
  }

  function renderAreas() {
    const count = (a, s) => a.rows.filter(r => r.state === s).length;
    put($('areas'), html`${AREAS.filter(areaShown).map(a => { const rows = a.rows.filter(passes); return html`<section class="area" id="a-${a.id}" aria-labelledby="h-${a.id}">
      <div class="sec-head"><h2 id="h-${a.id}">${I(LOOK[a.id]?.[0] || 'circle-help')}${a.name}<button class="help" type="button" aria-label="Help: ${a.name}" data-help="${LOOK[a.id]?.[1] || ''}">${I('circle-help')}</button></h2>
        <p class="tally">${a.read && !a.error ? STATES.filter(s => count(a, s)).map(s => html`<span class="g-${s}">${GLYPH[s]} ${count(a, s)} ${s}</span>`) : html`<span class="g-unknown">${GLYPH.unknown} not read</span>`}</p></div>
      ${a.read ? html`<p class="src-line">${a.source} · read <time class="rel" datetime="${DOC.read}"></time></p>` : ''}
      ${!a.read ? html`<p class="unread"><b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: ${a.missing.join(', ')}.</p>`
        : a.error ? html`<p class="unread"><b>Not read:</b> ${a.error}</p>`
        : a.missing.length ? html`<p class="unread"><b>Not read here:</b> ${a.missing.join(', ')}.</p>` : ''}
      ${rows.length ? html`<table class="ledger" aria-labelledby="h-${a.id}"><colgroup><col class="g"><col><col class="fix"></colgroup>
        <thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Finding</th><th scope="col">Fix</th></tr></thead>
        <tbody>${rows.map((r, i) => html`<tr data-id="${r.id}" aria-selected="${r.id === selected ? 'true' : 'false'}"${i > 0 && rows[i - 1].state !== r.state ? html` class="band-start"` : ''}>
          <td class="g g-${r.state}"><span aria-hidden="true">${GLYPH[r.state]}</span><span class="sr">${r.state}</span></td>
          <td class="what"><button type="button">${r.what}</button><span class="detail">${r.detail}</span></td>
          <td class="fix">${shell.commands.rowActions(r.id)}</td></tr>`)}</tbody></table>` : ''}
    </section>`; })}`);
  }

  function show(id) {
    const r = byId(id); if (!r) return;
    const a = AREAS.find(x => x.id === r.area);
    put($('details'), html`<p class="kind"><span class="g-${r.state}" aria-hidden="true">${GLYPH[r.state]}</span> ${r.kind} · ${r.state}</p>
      <h2>${r.what}</h2>
      <dl>${Object.entries(r.facts || {}).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Source</dt><dd>${a.source}</dd><dt>Read</dt><dd><time class="rel" data-long datetime="${DOC.read}"></time></dd></dl>
      ${r.list?.length ? html`<h3>Rows behind it</h3><ul class="list">${r.list.map(x => html`<li>${x}</li>`)}</ul>` : ''}
      <h3>Fix</h3>${shell.commands.bar(r.id)}`);
    shell.commands.select(r.id);
    shell.suggest([`Why is this ${r.state}?`, 'What does the fix touch?', 'Which Health row should I fix first?']);
  }
  function select(id, open) {
    selected = id;
    document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id ? 'true' : 'false'));
    show(id);
    if (open) shell.openPane('tab-details');
  }

  function writeURL() {
    const q = new URLSearchParams();
    ['state', 'area'].forEach(k => { if (F[k].size) q.set(k, [...F[k]].join(',')); });
    if (query) q.set('q', query);
    shell.url(q); // the shell keeps ?row=
  }
  function apply() {
    renderFilters(); renderAreas();
    const on = filtering(), shown = ROWS.filter(passes).length;
    const what = [F.area.size ? `${AREAS.filter(a => F.area.has(a.id)).map(a => a.name).join(', ')} only` : '', [...F.state].join(' or '), query ? `“${query}”` : ''].filter(Boolean).join(' · ');
    $('filtered').hidden = !on;
    put($('filtered'), on ? html`${shown ? `Filtered to ${what}: ${shown} of ${ROWS.length} rows.` : `No row matches ${what}.`} <button class="linkbtn" type="button" id="clear-f">Show all rows</button>` : html``);
    // The selection is always a row the viewer can see: the first visible one, or none with the reason.
    shell.reconcile({ rows: document.querySelectorAll('.ledger tbody tr[data-id]'), current: selected, select: id => select(id, false),
      clear: () => { selected = null; put($('details'), html`<p class="why">${!DOC ? 'Nothing is selected: Health could not be read.' : ROWS.length ? 'No row matches the filter, so nothing is selected. Show all rows to pick one.' : 'Nothing is selected: no area raised a row.'}</p>`); } });
    writeURL();
  }
  function clearFilters() { const had = filtering(); F.state.clear(); F.area.clear(); query = ''; $('q').value = ''; lamps(); apply(); return had; }

  function attention() {
    const w = ROWS.filter(r => r.state === 'warning').length, c = ROWS.filter(r => r.state === 'caution').length;
    window.PAGE_ATTENTION = { state: w ? 'warning' : c ? 'caution' : 'ok', n: w || c, what: w ? 'findings want you' : 'findings to watch' };
    shell.attention(window.PAGE_ATTENTION);
    return [w, c];
  }

  async function load() {
    shell.state({ kind: 'loading', text: 'Reading the fleet. Rows appear when /api/health answers.', source: '/api/health' });
    try {
      const r = await fetch('/api/health', { headers: { Accept: 'application/json' } });
      const doc = await r.json();
      if (!r.ok) throw new Error(doc.error || `HTTP ${r.status}`);
      DOC = doc; AREAS = doc.areas;
      AREAS.forEach(a => a.rows.sort((x, y) => RANK[x.state] - RANK[y.state]));
      ROWS = AREAS.flatMap(a => a.rows.map(r => ({ ...r, area: a.id })));
      document.body.dataset.observed = doc.read;
      ROWS.forEach(r => shell.commands.put({ ...r, label: r.what }));
      const failed = AREAS.filter(a => a.error);
      shell.state(failed.length ? { kind: 'partial', text: `${failed.map(a => `${a.name}: ${a.error}`).join(' · ')}. The other areas are current.`, source: '/api/health' } : null);
      const [w, c] = attention(), unread = AREAS.filter(a => !a.read).length;
      const want = [w ? `${w} warning` : '', c ? `${c} caution` : ''].filter(Boolean).join(', ') || 'no';
      put($('subhead'), html`${AREAS.length} areas · ${want} ${w + c === 1 ? 'row wants' : 'rows want'} you · ${plural(unread, 'area')} with no reader yet · read <time class="rel" datetime="${doc.read}"></time>`);
    } catch (e) {
      // Nothing from the last read stays on screen: rows, lamps, the observed time, the selection and the badge.
      DOC = null; AREAS = []; ROWS = [];
      delete document.body.dataset.observed;
      shell.state({ kind: 'error', text: `Health was not read, so nothing below is current: ${e.message}. Refresh tries again.`, source: '/api/health' });
      put($('subhead'), html`Health could not be read.`);
      shell.attention({ state: 'unknown', n: 0, what: 'rows' });
    }
    lamps();
    if (!selected || !byId(selected)) selected = shell.row() && byId(shell.row()) ? shell.row() : ROWS[0]?.id || null;
    apply();
    // reconcile keeps a selection that is still visible without drawing it; the new reading's Details are drawn here.
    if (selected && byId(selected)) show(selected);
  }

  // ---------- Commands (the design source's commands.md) ----------
  // Ids, labels, keys and risks are the design's. The build runs none of the lines: the dashboard has no route that prunes a
  // worktree or attributes a commit, so each is copy only and its run says where the line runs.
  const copyOnly = o => `Not run here: copy the line from Details and run it in a terminal · ${o.label}`;
  window.PAGE_COMMANDS = [
    { label: 'Filter rows', icon: 'search', key: '/', run: () => $('q').focus() },
    { label: 'Read Health again', icon: 'rotate-ccw', run: () => load() },
    { label: 'Open Sessions in Operations', icon: 'folder-x', run: () => { location.href = '/operations?area=sessions'; } },
  ];
  window.PAGE_LIST = { rows: () => document.querySelectorAll('.ledger tbody tr[data-id]'), current: () => selected,
    select: id => select(id, false), clear: () => clearFilters() };

  document.addEventListener('DOMContentLoaded', () => {
    const C = shell.commands;
    C.register(
      { id: 'check.recheck', on: 'check', label: 'Re-check', key: 'e', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: () => { load(); return 'Reading Health again'; } },
      { id: 'attribution gap.attribute', on: 'attribution gap', label: 'Attribute', key: 'a', risk: 'safe', executes: false, primary: () => true,
        cli: () => 'sd attribute <sha|from..to> <entry>  # on a branch, then ship by PR', run: copyOnly },
      { id: 'worktree registrations.prune', on: 'worktree registrations', label: 'Prune registrations', key: 'p', risk: 'confirm', bulk: true,
        executes: false, primary: () => true, cli: o => `git -C ${shell.shq(o.repo_path)} worktree prune -v`,
        consequence: o => `Removes ${o.facts.Registered} worktree registrations whose directories are gone. No directory is touched.`, run: copyOnly },
    );
    // Snooze sits on every type that wants you, as the design declares it; sd has no snooze verb, so it stays off.
    ['worktree registrations', 'unread registrations', 'attribution gap'].forEach(t => C.register({ id: `${t}.snooze`, on: t, label: 'Snooze', key: 'z', risk: 'undo', bulk: true,
      when: () => 'no CLI verb: sd has no snooze', cli: o => `sd now snooze ${o.id} --until 08:00`, run: o => `Snoozed until 08:00 · ${o.label}`, undo: () => {} }));

    const u = new URLSearchParams(location.search);
    (u.get('state') || '').split(',').filter(s => STATES.includes(s)).forEach(s => F.state.add(s));
    (u.get('area') || '').split(',').filter(Boolean).forEach(a => F.area.add(a));
    query = (u.get('q') || '').trim().toLowerCase(); $('q').value = u.get('q') || '';

    $('areas').addEventListener('click', e => {
      if (e.target.closest('.rowact') || e.target.closest('.help')) return;
      const tr = e.target.closest('tbody tr[data-id]'); if (tr) select(tr.dataset.id, true);
    });
    $('filters').addEventListener('click', e => {
      const c = e.target.closest('[data-f]'); if (!c) return;
      const set = F[c.dataset.f]; set.has(c.dataset.v) ? set.delete(c.dataset.v) : set.add(c.dataset.v);
      lamps(); apply();
    });
    $('filtered').addEventListener('click', e => { if (e.target.id === 'clear-f') clearFilters(); });
    // A lamp is the Area chip for its area: pressing it shows only that area, pressing it again shows all.
    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      if (c.id === 'refresh') return load();
      const id = c.dataset.area, only = F.area.size === 1 && F.area.has(id);
      F.area.clear(); if (!only) F.area.add(id);
      lamps(); apply();
    });
    $('q').addEventListener('input', () => { query = $('q').value.trim().toLowerCase(); apply(); });
    $('q').addEventListener('keydown', e => { if (e.key === 'Escape' && $('q').value) { e.stopPropagation(); $('q').value = ''; query = ''; apply(); } });
    document.addEventListener('keydown', e => {
      if (e.target.matches('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest?.('.actmenu') || shell.chording()) return;
      if (e.key === '/') { e.preventDefault(); $('q').focus(); }
    });
    document.addEventListener('shell:open', e => { if (byId(e.detail)) select(e.detail, true); });
    document.addEventListener('shell:picked', e => document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))));

    load();
  });
})();
