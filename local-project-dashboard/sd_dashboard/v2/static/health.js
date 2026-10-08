// Health v2 (sd:2115): the page script. It loads before shell.js, which reads what it declares.
// Rows: /api/health, the document health_screen.document builds from the fleet child (worktree registrations),
// reads.trailer_scan (attribution), Operations > Ports' reader (ports), protection.rows (branch protection, and the
// alerts behind Dependencies and Security, sd:2205 and sd:2206), the nightly credentials heartbeat (sd:2203) and
// health_collectors (disk and merged branches, sd:2202 and sd:2204).
// Ported from the design source's products/system/designs/v2/health page, its stylesheet and script, at d82daa1.
// An area with no reader shows as unknown with what it does not read, never as a clean lamp.
// Every fix is a CLI line for Copy; Re-check reads the document again. The one write is Re-run collector, which starts
// the server's shadow sync (sd:2894).
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put, plural } = window.markup;
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const RANK = { warning: 0, caution: 1, unknown: 2, queued: 3, ok: 4 };
  const STATES = ['warning', 'caution', 'unknown', 'queued', 'ok'];
  // The design's icon and help per area; the help is page markup the shell renders with <b> and <code> only.
  const LOOK = {
    disk: ['hard-drive', '<b>Where the space went.</b> Volume use from df, then the biggest items the fleet itself creates: the storage folders <code>disk.conf</code> names and build output left in worktrees. A merged worktree must not keep build output (rule of 2026-09-25). A volume lights caution at 80% and warning at 90%. Build output is found, not sized.'],
    cred: ['key-round', '<b>Presence and expiry only.</b> Token values are never read into this page. GitHub reports a classic PAT expiry in a response header; Home Assistant tokens carry none the dashboard can read.'],
    attr: ['signature', '<b>Who wrote each commit.</b> Every commit carries <code>Authored-with:</code> in its last paragraph; sd-review reads a missing one as “authored unknown” and blocks readiness. The count is your commits (each repo’s user.email) of the last 5 weeks on each default branch (origin/HEAD), merges left out. It walks every repo inside a 10 s budget; past it the area says it stopped rather than waited on, and shows no count.'],
    wt: ['folder-x', '<b>Registered is not present.</b> A worktree whose directory is gone still holds its branch. Prune clears the registration only; it never touches a directory that exists.'],
    br: ['git-branch', '<b>Merged but not deleted.</b> Local branches already contained in origin’s default branch. <code>git branch -d</code> refuses any branch that is not merged, so the fix cannot lose work.'],
    dep: ['package', '<b>Dependabot, per repo.</b> Open alerts on the unarchived managed repos, as the nightly sync stored them. A repo whose alerts could not be read is unknown, never clean.'],
    sec: ['shield-alert', '<b>Secret scanning, public repos only.</b> Private repos are not scanned, by policy. A public repo with scanning off is a finding.'],
    ports: ['link-2', '<b>Who holds which port.</b> Configured service ports, then every other TCP listener the Mac shows. <b>Unknown is not free</b>: an uninspected listener says nothing about the port.'],
    prot: ['lock', '<b>What each default branch enforces.</b> One column per registered repo, one line per check: a filled cell is a gap, a short mark passes, an empty cell does not apply. <b>Unknown is not protected</b>: a hatched column was not read and shows no cells. Every column opens its repo; the table below carries the same cells.'],
  };
  // Protection has no lamp: the annunciator holds 2 to 8 (the design's rule), and its matrix is Health's evidence strip.
  const NO_LAMP = new Set(['prot']);
  const PH = 10; // one matrix line in the SVG's own units
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
  // The design's size wording, from a count of KiB.
  const size = kb => kb >= 1024 ** 3 ? `${(kb / 1024 ** 3).toFixed(2)} TiB` : kb >= 1024 ** 2 ? `${(kb / 1024 ** 2).toFixed(1)} GiB` : `${Math.round(kb / 1024)} MiB`;
  const fullest = a => { const v = (a.extra.volumes || []).reduce((w, x) => !w || x.capacity > w.capacity ? x : w, null);
    return v ? html`<span class="ph"><b>${v.capacity}%</b> fullest</span> <span class="ph">${v.name}</span>` : html`<span class="ph">no volume</span>`; };
  const sumFact = (a, key) => a.rows.reduce((s, r) => s + (+(r.facts?.[key]) || 0), 0);
  // An unread repo is not zero alerts, and a list cut at its page size is "N+", as in the rows' Shown fact.
  const alerts = a => {
    const unread = a.rows.some(r => r.state === 'unknown');
    if (unread && a.rows.every(r => r.state === 'unknown')) return html`<span class="ph">not read</span>`;
    const more = a.rows.some(r => String(r.facts?.Shown || '').endsWith('+')) ? '+' : '';
    return html`<span class="ph"><b>${sumFact(a, 'Open')}${more}</b> open alerts</span>${unread ? html` · <span class="ph">not all read</span>` : ''}`;
  };
  const lampValue = a => !a.read ? html`<span class="ph">no reader</span>`
    : a.error ? html`<span class="ph">not read</span>`
    : a.id === 'wt' ? html`<span class="ph"><b>${sumFact(a, 'Registered')}</b> dir gone</span>`
    // The scope decided 2026-09-30 (design 63c9d0c): the operator's own commits, 5 weeks, each repo's default branch.
    : a.id === 'attr' ? html`<span class="ph"><b>${sumFact(a, 'Missing')}</b> missing</span> <span class="ph">your commits · 5 weeks · default branch</span>`
    : a.id === 'disk' ? fullest(a)
    : a.id === 'br' ? html`<span class="ph"><b>${sumFact(a, 'Merged')}</b> merged</span> <span class="ph">not deleted</span>`
    : a.id === 'dep' || a.id === 'sec' ? alerts(a)
    : a.id === 'cred' ? html`<span class="ph"><b>${a.rows.filter(r => r.state === 'warning' || r.state === 'caution').length}</b> need you</span>`
    : a.id === 'ports' ? html`<span class="ph"><b>${a.extra.counts.unknown}</b> unknown</span> · <span class="ph">${a.extra.counts.listening} listening</span>`
    : html`<span class="ph"><b>${a.rows.length}</b> rows</span>`;

  // The box says "repo, path, branch": a path can sit only in a row's facts (a checkout) or its list (registered worktrees).
  const text = r => [r.what, r.detail, r.kind, ...Object.values(r.facts || {}), ...(r.list || [])].join(' ').toLowerCase();
  const passes = r => (!F.state.size || F.state.has(r.state)) && (!F.area.size || F.area.has(r.area))
    && (!query || text(r).includes(query));
  const filtering = () => !!(F.state.size || F.area.size || query);
  // An area stays on the page while it has a row the filters pass, or when only the Area filter names it: a lamp for an area
  // with no reader then shows what that area does not read.
  const areaShown = a => a.rows.some(passes) || (!F.state.size && !query && (!F.area.size || F.area.has(a.id)));

  function lamps() {
    put($('annunciator'), html`${AREAS.filter(a => !NO_LAMP.has(a.id)).map(a => { const s = areaState(a); return html`<li><button class="cell" type="button" data-area="${a.id}" data-state="${s}" aria-pressed="${F.area.has(a.id) ? 'true' : 'false'}"><span class="lbl">${a.name}${I(LOOK[a.id]?.[0] || 'circle-help')}</span><span class="val">${lampValue(a)}</span><span class="sr">state ${s} · shows only ${a.name} rows</span></button></li>`; })}<li><button class="cell" type="button" id="refresh"><span class="lbl">Observed${I('rotate-ccw')}</span><span class="val"><span class="ph">${DOC ? html`<b>${new Date(DOC.read).toISOString().slice(11, 16)}</b> UTC` : 'not read'}</span> · <span class="ph">refresh</span></span></button></li>`);
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
      ${a.read ? html`<p class="src-line">${a.source} · ${a.at ? html`read <time class="rel" datetime="${a.at}"></time>` : 'never observed'}</p>` : ''}
      ${!a.read ? html`<p class="unread"><b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: ${a.missing.join(', ')}.</p>`
        : a.error ? html`<p class="unread"><b>Not read:</b> ${a.error}</p>`
        : a.missing.length ? html`<p class="unread"><b>Not read here:</b> ${a.missing.join(', ')}.</p>` : ''}
      ${a.stale ? html`<p class="unread"><b>Not re-read:</b> ${a.stale}</p>` : ''}
      ${a.read && !a.error ? pre(a) : ''}
      ${rows.length ? html`<table class="ledger" aria-labelledby="h-${a.id}"><colgroup><col class="g"><col><col class="fix"></colgroup>
        <thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Finding</th><th scope="col">Fix</th></tr></thead>
        <tbody>${rows.map((r, i) => html`<tr data-id="${r.id}" aria-selected="${r.id === selected ? 'true' : 'false'}"${i > 0 && rows[i - 1].state !== r.state ? html` class="band-start"` : ''}>
          <td class="g g-${r.state}"><span aria-hidden="true">${GLYPH[r.state]}</span><span class="sr">${r.state}</span></td>
          <td class="what"><button type="button">${r.what}</button><span class="detail">${r.detail}</span></td>
          <td class="fix">${shell.commands.rowActions(r.id)}</td></tr>`)}</tbody></table>` : ''}
    </section>`; })}`);
  }

  // What an area draws above its ledger: v1's counts line and warnings, and for Protection the matrix and its table.
  const warn = w => html`<p class="warn" role="status"><span class="g-unknown" aria-hidden="true">${GLYPH.unknown}</span> ${w}</p>`;
  function pre(a) {
    const x = a.extra || {};
    if (a.id === 'disk') return x.volumes?.length ? html`<div class="bars" role="group" aria-label="Volume use: ${x.volumes.map(v => `${v.name} ${v.capacity}%`).join(', ')}">${x.volumes.map(v => html`<span class="name" title="${v.mount}">${v.name}</span><svg aria-hidden="true"><rect class="track" x="0.5" y="0.5" width="99%" height="11" rx="2"/><rect class="fill" x="0.5" y="0.5" width="${(v.used_kb / v.size_kb * 100 || 0).toFixed(1)}%" height="11" rx="2"/></svg><span class="v">${v.capacity}% · ${size(v.avail_kb)} free</span>`)}</div>` : '';
    if (a.id === 'ports') return html`<p class="counts">${x.counts.configured} configured ports · ${x.counts.listening} listening · ${x.counts.unknown} unknown</p>${x.warnings.map(warn)}`;
    if (a.id !== 'prot' || !x.columns) return '';
    const cols = x.columns.map(byId).filter(Boolean), n = x.counts;
    const lines = x.checks.length + 1;
    return html`<p class="counts">${n.protected} protected · ${n.unprotected} unprotected · ${n.unknown} unknown · ${shell.commands.rowActions('collector:protection')}</p>
      ${x.reasons.map(r => warn(`${n.unknown} unknown, ${x.reasons.length === 1 ? 'all for one reason' : 'among them'}: ${r}.`))}
      ${cols.length ? html`<p class="legend"><span><span class="sw gap"></span>gap</span><span><span class="sw pass"></span>ok</span><span><span class="sw na"></span>does not apply</span><span><span class="sw unk"></span>not read</span></p>
      <div class="pmx-wrap" role="region" aria-label="Protection matrix: ${lines} lines across ${cols.length} repositories" tabindex="0">
        <div class="pmx">
          <ul class="pmx-labels" aria-hidden="true"><li>Status</li>${x.checks.map(l => html`<li>${l}</li>`)}</ul>
          <div class="pmx-cols">${cols.map(r => html`<button type="button" class="pmx-col" data-row="${r.id}" data-status="${r.status}" aria-current="${r.id === selected ? 'true' : 'false'}" aria-label="${r.name}: ${r.status}${r.status === 'unknown' ? '' : `, ${plural(r.gaps, 'gap')}`}"><svg viewBox="0 0 10 ${lines * PH}" preserveAspectRatio="none" aria-hidden="true">
            ${r.status === 'unknown' ? '' : html`<rect class="st-${r.status}" x="1" y="1" width="8" height="${PH - 2}"/>${r.cells.map(([, c], i) => { const y = (i + 1) * PH;
              return c === 'gap' ? html`<rect class="c-gap" x="1" y="${y + 1}" width="8" height="${PH - 2}"/>` : c === 'ok' ? html`<rect class="c-ok" x="1" y="${y + PH / 2 - 0.75}" width="8" height="1.5"/>` : ''; })}`}</svg></button>`)}</div>
          <ul class="pmx-n" aria-hidden="true"><li>${n.unprotected}</li>${x.checks.map((_, i) => html`<li>${cols.filter(r => r.cells?.[i]?.[1] === 'gap').length}</li>`)}</ul>
        </div>
      </div>
      <details class="pmx-table"><summary>Table: ${cols.length} repositories × ${x.checks.length} checks</summary>
        <div class="tscroll" role="region" aria-labelledby="pmx-cap" tabindex="0"><table>
          <caption id="pmx-cap">Branch protection per registered repository, unprotected first, then unknown, then protected. GAP carries its sentence; — does not apply; not read where the repository was not read.</caption>
          <thead><tr><th scope="col">Repository</th><th scope="col">Status</th><th scope="col">Branch</th>${x.checks.map(l => html`<th scope="col">${l}</th>`)}<th scope="col">Reason</th></tr></thead>
          <tbody>${cols.map(r => html`<tr><th scope="row">${r.slug ? html`<a href="https://github.com/${r.slug}/settings/branches" target="_blank" rel="noopener noreferrer">${r.slug}</a>` : r.facts.Repository}</th><td>${r.status}</td><td>${r.branch}</td>
            ${x.checks.map((_, i) => { const c = r.cells[i]; return html`<td>${r.status === 'unknown' ? 'not read' : c?.[1] === 'gap' ? html`<b>GAP</b> <span class="sent">${c[2]}</span>` : c?.[1] === 'ok' ? 'ok' : '—'}</td>`; })}<td>${r.reason}</td></tr>`)}</tbody>
        </table></div></details>` : ''}`;
  }
  const cellText = c => c[1] === 'gap' ? `GAP · ${c[2]}` : c[1] === 'ok' ? 'ok' : '—';

  function show(id) {
    const r = byId(id); if (!r) return;
    const a = AREAS.find(x => x.id === r.area);
    put($('details'), html`<p class="kind"><span class="g-${r.state}" aria-hidden="true">${GLYPH[r.state]}</span> ${r.kind} · ${r.state}</p>
      <h2>${r.what}</h2>
      <dl>${Object.entries(r.facts || {}).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Source</dt><dd>${a.source}</dd><dt>Read</dt><dd><time class="rel" data-long datetime="${DOC.read}"></time></dd></dl>
      ${r.type === 'branch protection' ? (r.status === 'unknown' ? html`<h3>Checks</h3><p class="why">No cell is shown: ${r.reason}.</p>`
        : html`<h3>Checks</h3>${r.sentence ? html`<p class="why">${r.sentence}</p>` : ''}<dl class="checks">${r.cells.map(c => html`<dt>${c[0]}</dt><dd class="c-${c[1]}">${cellText(c)}</dd>`)}</dl>`) : ''}
      ${r.slug ? html`<p><a class="ext" href="https://github.com/${r.slug}/settings/branches" target="_blank" rel="noopener noreferrer">${r.slug} · settings/branches${I('arrow-up-right')}</a></p>` : ''}
      ${r.list?.length ? html`<h3>Rows behind it</h3><ul class="list">${r.list.map(x => html`<li>${x}</li>`)}</ul>` : ''}
      <h3>Fix</h3>${shell.commands.bar(r.id)}
      ${r.note ? html`<p class="why">${r.note}</p>` : ''}`);
    shell.commands.select(r.id);
    shell.suggest([`Why is this ${r.state}?`, 'What does the fix touch?', 'Which Health row should I fix first?']);
  }
  function select(id, open) {
    selected = id;
    document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id ? 'true' : 'false'));
    document.querySelectorAll('.pmx-col').forEach(b => b.setAttribute('aria-current', b.dataset.row === id ? 'true' : 'false'));
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

  // The reader (read.js, sd:2418) holds the guards: of overlapping re-reads only the newest draws, a row the read no longer
  // lists runs no command and loses its pick, and a failed read leaves no row, lamp or command from the last one. It is
  // built on DOMContentLoaded, once shell.js has made shell.read.
  let health = null;
  const load = () => health.load();
  const spec = {
    source: '/api/health', what: 'the Health rows',
    adopt: doc => {
      DOC = doc; AREAS = doc.areas;
      AREAS.forEach(a => a.rows.sort((x, y) => RANK[x.state] - RANK[y.state]));
      ROWS = AREAS.flatMap(a => a.rows.map(r => ({ ...r, area: a.id })));
      document.body.dataset.observed = doc.read;
      const failed = AREAS.filter(a => a.error || a.stale);
      const [w, c] = attention(), unread = AREAS.filter(a => !a.read).length;
      const want = [w ? `${w} warning` : '', c ? `${c} caution` : ''].filter(Boolean).join(', ') || 'no';
      put($('subhead'), html`${AREAS.length} areas · ${want} ${w + c === 1 ? 'row wants' : 'rows want'} you${unread ? ` · ${plural(unread, 'area')} with no reader yet` : ''} · read <time class="rel" datetime="${doc.read}"></time>`);
      return { objects: ROWS.map(r => ({ ...r, label: r.what })),
        state: failed.length ? { kind: 'partial', text: `${failed.map(a => `${a.name}: ${a.error || a.stale}`).join(' · ')}. The other areas are current.`, source: '/api/health' } : null };
    },
    // Nothing from the last read stays on screen: rows, lamps, the observed time and the badge.
    clear: () => {
      DOC = null; AREAS = []; ROWS = [];
      delete document.body.dataset.observed;
      put($('subhead'), html`Health could not be read.`);
      shell.attention({ state: 'unknown', n: 0, what: 'rows' });
    },
    draw: () => lamps(),
    current: () => selected,
    first: () => ROWS[0]?.id,
    // apply() reconciles against the rows the filters show; the new reading's Details are drawn here.
    select: id => { selected = id; apply(); if (byId(selected)) show(selected); },
    unselect: () => { selected = null; apply(); },
  };

  // ---------- Commands (the design source's commands.md) ----------
  // Ids, labels, keys and risks are the design's. The build runs none of the lines: the dashboard has no route that prunes a
  // worktree, attributes a commit, deletes a branch or removes build output, so each is copy only and its run says where the
  // line runs. A row with `disabled` (a merged branch a worktree holds) names why the line would be refused.
  const copyOnly = o => `Not run here: copy the line from Details and run it in a terminal · ${o.label}`;
  // As contributions.js's: success is quiet, since the reread shows it; a failed tracker or a broken run stays as a toast.
  const SYNC_POLL = 5000;
  const awaitSync = () => setTimeout(() => shell.getJSON('/api/shadow/state').then(s => {
    if (s.running) return awaitSync();
    load();
    const bad = s.error || s.trackers.filter(t => !t.ok && t.configured).map(t => `${t.tracker}: ${t.reason || 'failed'}`)[0];
    if (bad) shell.toast(`Shadow sync ended with a problem: ${bad}`);
  }, e => shell.toast(`The sync's state was not read: ${e.message}`)), SYNC_POLL);
  window.PAGE_COMMANDS = [
    { label: 'Filter rows', icon: 'search', key: '/', run: () => $('q').focus() },
    { label: 'Read Health again', icon: 'rotate-ccw', run: () => load() },
    { label: 'Open Sessions in Operations', icon: 'folder-x', run: () => { location.href = '/operations?area=sessions'; } },
  ];
  window.PAGE_LIST = { rows: () => document.querySelectorAll('.ledger tbody tr[data-id]'), current: () => selected,
    select: id => select(id, false), clear: () => clearFilters() };

  document.addEventListener('DOMContentLoaded', () => {
    const C = shell.commands;
    health = shell.read(spec);
    // The protection collector is one object for the page, not a row of a reading: no read retires it.
    C.put({ id: 'collector:protection', type: 'collector', label: 'protection collector' });
    C.register(
      { id: 'check.recheck', on: 'check', label: 'Re-check', key: 'e', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: () => { load(); return 'Reading Health again'; } },
      { id: 'storage folder.du', on: 'storage folder', label: 'Show biggest', key: 'b', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: copyOnly },
      { id: 'build output.rm', on: 'build output', label: 'Remove build output', key: 'r', risk: 'confirm', executes: false, primary: () => true,
        cli: o => o.cli, consequence: o => `Deletes the build output of ${o.facts.Found} merged worktrees: ${o.detail}. This cannot be undone from here.`, run: copyOnly },
      // Volume rows are the build's: the design draws volumes as bars only. Its line shows the volume's use in a terminal.
      { id: 'volume.df', on: 'volume', label: 'Show use', key: 'b', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: copyOnly },
      { id: 'attribution gap.attribute', on: 'attribution gap', label: 'Attribute', key: 'a', risk: 'safe', executes: false, primary: () => true,
        cli: () => 'sd attribute <sha|from..to> <entry>  # on a branch, then ship by PR', run: copyOnly },
      { id: 'worktree registrations.prune', on: 'worktree registrations', label: 'Prune registrations', key: 'p', risk: 'confirm', bulk: true,
        executes: false, primary: () => true, cli: o => `git -C ${shell.shq(o.repo_path)} worktree prune -v`,
        consequence: o => `Removes ${o.facts.Registered} worktree registrations whose directories are gone. No directory is touched.`, run: copyOnly },
      { id: 'merged branches.delete', on: 'merged branches', label: 'Delete merged', key: 'd', risk: 'safe', bulk: true, executes: false,
        primary: () => true, when: o => !o.disabled || o.disabled, cli: o => o.cli,
        consequence: o => `Deletes ${o.facts.Merged || o.facts.Count} local branches already in origin's default branch. git branch -d refuses any unmerged one.`, run: copyOnly },
    );
    // Ports and Protection are read-only: each line is copy only.
    C.register(
      { id: 'port.inspect', on: 'port', label: 'Inspect listener', key: 'i', risk: 'safe', primary: () => true, executes: false,
        when: o => !!o.port || 'no port configured: there is nothing to inspect', cli: o => `lsof -nP -iTCP:${o.port || '<port>'} -sTCP:LISTEN`, run: copyOnly },
      { id: 'protection.settings', on: 'branch protection', label: 'Open branch settings', key: 'o', risk: 'safe', primary: () => true, executes: false,
        when: o => !!o.slug || 'no github.com remote: there is no settings page', cli: o => `open https://github.com/${o.slug || '<owner>/<repo>'}/settings/branches`,
        run: o => { window.open(`https://github.com/${o.slug}/settings/branches`, '_blank', 'noopener'); return 'Opens in a new tab'; } },
      // Re-run collector (sd:2894) posts the route Contributions posts (sd:2207): the server runs sd shadow sync, which
      // writes the protection rows, in a thread, bounded to 120 s, one run at a time. Health reads again once it ends.
      { id: 'collector.sync', on: 'collector', label: 'Re-run collector', key: 'r', risk: 'safe', primary: () => true,
        cli: () => 'sd shadow sync --max-seconds 120',
        run: () => shell.post('/api/shadow/sync', {}).then(() => { awaitSync(); return 'Shadow sync started · Health reads again when it ends'; }) },
      // Dependencies, Security and Credentials (sd:2203, sd:2205, sd:2206) read stored rows; each line is copy only.
      { id: 'dependabot alerts.review', on: 'dependabot alerts', label: 'Review on GitHub', key: 'o', risk: 'safe', primary: () => true,
        executes: false, cli: o => o.cli, run: copyOnly },
      { id: 'secret scanning.review', on: 'secret scanning', label: 'Review on GitHub', key: 'o', risk: 'safe', primary: () => true,
        executes: false, cli: o => o.cli, run: copyOnly },
      { id: 'credential.probe', on: 'credential', label: 'Probe again', key: 'r', risk: 'safe', primary: () => true, executes: false,
        cli: o => o.cli, run: copyOnly },
    );
    // Snooze sits on every type that wants you, as the design declares it; sd has no snooze verb, so it stays off.
    ['storage folder', 'build output', 'volume', 'worktree registrations', 'unread registrations', 'attribution gap', 'merged branches', 'port', 'branch protection'].forEach(t => C.register({ id: `${t}.snooze`, on: t, label: 'Snooze', key: 'z', risk: 'undo', bulk: true,
      when: () => 'no CLI verb: sd has no snooze', cli: o => `sd now snooze ${o.id} --until 08:00`, run: o => `Snoozed until 08:00 · ${o.label}`, undo: () => {} }));

    const u = new URLSearchParams(location.search);
    (u.get('state') || '').split(',').filter(s => STATES.includes(s)).forEach(s => F.state.add(s));
    (u.get('area') || '').split(',').filter(Boolean).forEach(a => F.area.add(a));
    query = (u.get('q') || '').trim().toLowerCase(); $('q').value = u.get('q') || '';

    $('areas').addEventListener('click', e => {
      if (e.target.closest('.rowact') || e.target.closest('.help')) return;
      const col = e.target.closest('.pmx-col'); if (col) { select(col.dataset.row, true); return; }
      const tr = e.target.closest('tbody tr[data-id]'); if (tr) select(tr.dataset.id, true);
    });
    $('filters').addEventListener('click', e => {
      const c = e.target.closest('[data-f]'); if (!c) return;
      const set = F[c.dataset.f]; set.has(c.dataset.v) ? set.delete(c.dataset.v) : set.add(c.dataset.v);
      lamps(); apply();
    });
    $('filtered').addEventListener('click', e => { if (e.target.id === 'clear-f') clearFilters(); });
    // A lamp is the Area chip for its area: pressing it adds the area to the filter, pressing it again takes it out.
    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      if (c.id === 'refresh') return load();
      const id = c.dataset.area;
      F.area.has(id) ? F.area.delete(id) : F.area.add(id);
      lamps(); apply();
    });
    $('q').addEventListener('input', () => { query = $('q').value.trim().toLowerCase(); apply(); });
    $('q').addEventListener('keydown', e => { if (e.key === 'Escape' && $('q').value) { e.stopPropagation(); $('q').value = ''; query = ''; apply(); } });
    document.addEventListener('keydown', e => {
      if (e.target.matches('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest?.('.actmenu') || shell.chording()) return;
      if (e.key === '/') { e.preventDefault(); $('q').focus(); }
    });
    document.addEventListener('shell:open', e => { if (byId(e.detail)) select(e.detail, true); });
    document.addEventListener('shell:picked', e => { document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))); });

    load();
  });
})();
