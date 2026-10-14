// Health v2 (sd:2115): the page script. It loads before shell.js, which reads what it declares.
// Rows: /api/health, the document health_screen.document builds from the fleet child (worktree registrations),
// Operations > Ports' reader (ports), protection.rows (branch protection, and the
// alerts behind Dependencies and Security, sd:2205 and sd:2206), the nightly credentials heartbeat (sd:2203) and
// health_collectors (disk and merged branches, sd:2202 and sd:2204, and the machine-setup doctor, sd:3393).
// Ported from the design source's products/system/designs/v2/health page, its stylesheet and script, at d82daa1.
// An area with no reader shows as unknown with what it does not read, never as a clean lamp.
// Most fixes are a CLI line for Copy; Re-check reads the document again. The writes are Re-run collector, which starts
// the server's shadow sync (sd:2894), Snooze (sd:1896): an area's `snoozed` rows leave it, and snooze.js draws them, and
// the three cleanups (sd:3393), each behind a confirm that names what goes, posting the row's revision, and a service's
// Restart and Start (sd:3707), each a confirm that posts /api/services/<label>/<action>, `sd services`' own code.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put, plural, cells } = window.markup;
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const RANK = { warning: 0, caution: 1, unknown: 2, queued: 3, ok: 4 };
  const STATES = ['warning', 'caution', 'unknown', 'queued', 'ok'];
  // The design's icon and help per area; the help is page markup the shell renders with <b> and <code> only.
  const LOOK = {
    disk: ['hard-drive', '<b>Where the space went.</b> Volume use from df, then the biggest items the fleet itself creates: the storage folders <code>disk.conf</code> names and build output left in worktrees. A merged worktree must not keep build output (rule of 2026-09-25). A volume lights caution at 80% and warning at 90%. Build output is found, not sized.'],
    cred: ['key-round', '<b>Presence and expiry only.</b> Token values are never read into this page. GitHub reports a classic PAT expiry in a response header; Home Assistant tokens carry none the dashboard can read.'],
    wt: ['folder-x', '<b>Registered is not present.</b> A worktree whose directory is gone still holds its branch. Prune clears the registration only; it never touches a directory that exists.'],
    br: ['git-branch', '<b>Merged but not deleted.</b> Local branches already contained in origin’s default branch. <code>git branch -d</code> refuses any branch that is not merged, so the fix cannot lose work.'],
    dep: ['package', '<b>Dependabot, per repo.</b> Open alerts on the unarchived managed repos, as the nightly sync stored them. A repo whose alerts could not be read is unknown, never clean.'],
    sec: ['shield-alert', '<b>Secret scanning, public repos only.</b> Private repos are not scanned, by policy. A public repo with scanning off is a finding.'],
    ports: ['link-2', '<b>Who holds which port.</b> Configured service ports, then every other TCP listener the Mac shows. <b>Unknown is not free</b>: an uninspected listener says nothing about the port.'],
    doc: ['shield-check', '<b>What machine-setup doctor says.</b> Its WARN and FAIL lines, one row each, and the lines that pass. The doctor runs <code>brew doctor</code>, so an answer stands for ten minutes; a run still going, stopped at its deadline or failed is unknown, never clean.'],
    svc: ['activity', '<b>Long-running user services.</b> Each LaunchAgent that keeps a process alive, as <code>sd services list</code> reads it. Restart and Start run <code>sd services restart</code> and <code>start</code> here, against the row as shown. The dashboard compares the commit it serves with the checkout; its own service is protected, so a stale one gives the kickstart line to copy.'],
    mir: ['book-open', '<b>The vault mirror of base rows.</b> Each base row write renders its notes and its page under <code>docs/dashboard</code> in the vault; a hand-edited note is kept and listed. Render mirror runs <code>sd-db records render</code> here, to catch up after a failed render.'],
    prot: ['lock', '<b>What each default branch enforces.</b> One column per registered repo, one line per check: a filled cell is a gap, a short mark passes, an empty cell does not apply. <b>Unknown is not protected</b>: a hatched column was not read and shows no cells. Every column opens its repo; the table below carries the same cells.'],
  };
  // Protection has no lamp: the annunciator holds 2 to 8 (the design's rule), and its matrix is Health's evidence strip.
  // Services (sd:3707) is a further area past a full strip: no lamp, its chip and section only (design.md, lamp: false).
  const NO_LAMP = new Set(['prot', 'svc']);
  const PH = 10; // one matrix line in the SVG's own units
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const $ = id => document.getElementById(id);

  let DOC = null, AREAS = [], ROWS = [], SNOOZED = [], selected = null, query = '';
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
    : a.id === 'disk' ? fullest(a)
    : a.id === 'br' ? html`<span class="ph"><b>${sumFact(a, 'Merged')}</b> merged</span> <span class="ph">not deleted</span>`
    : a.id === 'dep' || a.id === 'sec' ? alerts(a)
    : a.id === 'cred' || a.id === 'doc' || a.id === 'mir' ? html`<span class="ph"><b>${a.rows.filter(r => r.state === 'warning' || r.state === 'caution').length}</b> need you</span>`
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
    put($('annunciator'), cells([...AREAS.filter(a => !NO_LAMP.has(a.id)).map(a => { const s = areaState(a); return { button: true, state: s, pressed: F.area.has(a.id),
      attrs: html` data-area="${a.id}"`, label: a.name, mark: I(LOOK[a.id]?.[0] || 'circle-help'), val: lampValue(a), sr: `state ${s} · shows only ${a.name} rows` }; }),
      { button: true, attrs: html` id="refresh"`, label: 'Observed', mark: I('rotate-ccw'),
        val: html`<span class="ph">${DOC ? html`<b>${new Date(DOC.read).toISOString().slice(11, 16)}</b> UTC` : 'not read'}</span> · <span class="ph">refresh</span>` }]));
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
      ${rows.length ? html`<table class="ledger" aria-labelledby="h-${a.id}">
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

  // The badge is the read's own (sd:3446, health_screen._attention); the subhead counts both states.
  function attention() {
    window.PAGE_ATTENTION = DOC && DOC.attention || { state: 'unknown', n: 0, what: 'rows' };
    shell.attention(window.PAGE_ATTENTION);
    return ['warning', 'caution'].map(s => ROWS.filter(r => r.state === s).length);
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
      // Each row carries its area: the filter reads r.area on a.rows as well as on ROWS (sd:3708).
      AREAS.forEach(a => { a.rows.forEach(r => { r.area = a.id; }); a.rows.sort((x, y) => RANK[x.state] - RANK[y.state]); });
      ROWS = AREAS.flatMap(a => a.rows);
      SNOOZED = AREAS.flatMap(a => a.snoozed);
      document.body.dataset.observed = doc.read;
      const failed = AREAS.filter(a => a.error || a.stale);
      const [w, c] = attention(), unread = AREAS.filter(a => !a.read).length;
      const want = [w ? `${w} warning` : '', c ? `${c} caution` : ''].filter(Boolean).join(', ') || 'no';
      put($('subhead'), html`${AREAS.length} areas · ${want} ${w + c === 1 ? 'row wants' : 'rows want'} you${unread ? ` · ${plural(unread, 'area')} with no reader yet` : ''} · read <time class="rel" datetime="${doc.read}"></time>`);
      return { objects: [...ROWS.map(r => ({ ...r, label: r.what })), ...SNOOZED.map(window.snooze.object)],
        state: failed.length ? { kind: 'partial', text: `${failed.map(a => `${a.name}: ${a.error || a.stale}`).join(' · ')}. The other areas are current.`, source: '/api/health' }
          : doc.snooze_error ? window.snooze.unread(doc.snooze_error, '/api/health') : null };
    },
    // Nothing from the last read stays on screen: rows, lamps, the observed time and the badge.
    clear: () => {
      DOC = null; AREAS = []; ROWS = []; SNOOZED = [];
      delete document.body.dataset.observed;
      put($('subhead'), html`Health could not be read.`);
      shell.attention({ state: 'unknown', n: 0, what: 'rows' });
    },
    draw: () => { lamps(); window.snooze.draw($('snoozed'), SNOOZED); },
    current: () => selected,
    first: () => ROWS[0]?.id,
    // apply() reconciles against the rows the filters show; the new reading's Details are drawn here.
    select: id => { selected = id; apply(); if (byId(selected)) show(selected); },
    unselect: () => { selected = null; apply(); },
  };

  // ---------- Commands (the design source's commands.md) ----------
  // Ids, labels and keys are the design's. Prune, Delete merged and Remove build output run here (sd:3393): each confirms
  // with the count and the names, then posts the row and the revision it showed; the server reads the subject again and
  // refuses a changed one. A row with `disabled` (a merged branch a worktree holds) names why git would refuse it.
  // None runs in bulk: the bulk confirm names four rows at most, and its tally counts an unknown outcome as landed.
  const copyOnly = o => `Not run here: copy the line from Details and run it in a terminal · ${o.label}`;
  const names = xs => xs.length > 6 ? `${xs.slice(0, 6).join(', ')} and ${xs.length - 6} more` : xs.join(', ');
  // A 4xx wrote nothing (health_cleanup); a 502 or no answer may follow a step that landed, so the page reads again either way.
  const cleanup = o => shell.post('/api/health/cleanup', { row: o.id, revision: o.revision }).then(out => {
    load();
    const kept = out.refused.map(x => `${x.name} (${x.why})`);
    return `${o.label}: ${out.done.length} done${kept.length ? ` · kept ${kept.length}: ${kept.join(', ')}` : ''}`;
  }, e => {
    load();
    if (e.status >= 400 && e.status < 500) throw e;
    return `Not known what changed · ${o.label}: ${e.message}. Health was read again; check the row before you act again.`;
  });
  const revised = o => !!o.revision || 'the row carries no revision: read Health again';
  // A service write (sd:3707) answers the request launchd took and what it reads now; the page reads again either way.
  const service = action => o => shell.post(`/api/services/${encodeURIComponent(o.facts.Label)}/${action}`, { revision: o.revision }).then(out => {
    load();
    return `${o.facts.Label}: ${out.request.status} · ${out.request.message}`;
  }, e => {
    load();
    if (e.status >= 400 && e.status < 500) throw e;
    return `Not known what changed · ${o.facts.Label}: ${e.message}. Health was read again; check the row before you act again.`;
  });
  // Render mirror (sd:3977) answers what it wrote, or a 409 that names why it did not; the page reads again either way.
  const render = o => shell.post('/api/records/render', {}).then(out => {
    load();
    return `Vault mirror rendered: ${out.kinds.join(', ') || 'no kind'} · ${plural(out.written.length, 'file')} written`;
  }, e => {
    load();
    if (e.status >= 400 && e.status < 500) throw e;
    return `Not known what changed · vault mirror: ${e.message}. Health was read again; check the row before you act again.`;
  });
  const can = action => o => revised(o) === true ? (o.can[action].allowed || o.can[action].reason) : revised(o);
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
      { id: 'build output.rm', on: 'build output', label: 'Remove build output', key: 'r', risk: 'confirm', executes: true, primary: () => true,
        when: o => revised(o) === true ? (o.remove.length ? true : 'every merged worktree has uncommitted changes: nothing is removed') : revised(o),
        cli: o => o.cli, consequence: o => `Deletes ${o.remove.length} build output folders in merged, clean worktrees: ${names(o.remove)}.`
          + (o.kept.length ? ` Keeps ${o.kept.length} with uncommitted changes: ${names(o.kept)}.` : '') + ' Each worktree is checked again first. This cannot be undone.', run: cleanup },
      // Volume rows are the build's: the design draws volumes as bars only. Its line shows the volume's use in a terminal.
      { id: 'volume.df', on: 'volume', label: 'Show use', key: 'b', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: copyOnly },
      { id: 'worktree registrations.prune', on: 'worktree registrations', label: 'Prune registrations', key: 'p', risk: 'confirm',
        executes: true, primary: () => true, cli: o => `git -C ${shell.shq(o.repo_path)} worktree prune -v`,
        when: o => o.unreadable ? `${plural(o.unreadable, 'registration')} of this checkout could not be read, and prune would judge them itself` : revised(o),
        consequence: o => `Removes ${o.facts.Registered} worktree registrations whose directories are gone: ${names(o.list)}. No directory is touched.`, run: cleanup },
      { id: 'merged branches.delete', on: 'merged branches', label: 'Delete merged', key: 'd', risk: 'confirm', executes: true,
        primary: () => true, when: o => o.disabled || revised(o), cli: o => o.cli,
        consequence: o => `Deletes ${o.facts.Merged} local branches already in origin's default branch: ${names(o.branches)}. git branch -d refuses any unmerged one.`, run: cleanup },
    );
    // Services (sd:3707): Restart and Start run sd services' code through Operations' route. sd_db.services protects the
    // dashboard's own service, and a restart from its page would end the request that asks, so that row is copy only.
    C.register(
      { id: 'service.restart', on: 'service', label: 'Restart', key: 'r', risk: 'confirm', executes: true,
        primary: o => o.facts.State === 'running', when: can('restart'),
        cli: o => `sd services restart ${o.facts.Label}`,
        consequence: o => `Stops ${o.facts.Label} and starts it again through launchctl (bootout, bootstrap, kickstart). Its open connections drop.`,
        run: service('restart') },
      { id: 'service.start', on: 'service', label: 'Start', key: 's', risk: 'confirm', executes: true,
        primary: o => o.facts.State !== 'running', when: can('start'),
        cli: o => `sd services start ${o.facts.Label}`,
        consequence: o => `Loads ${o.facts.Label} if it is unloaded and kickstarts it through launchctl.`,
        run: service('start') },
      { id: 'dashboard service.kickstart', on: 'dashboard service', label: 'Kickstart', key: 'l', risk: 'safe', executes: false,
        primary: () => true, cli: o => o.kickstart, run: copyOnly },
    );
    // Ports and Protection are read-only: each line is copy only.
    C.register(
      { id: 'port.inspect', on: 'port', label: 'Inspect listener', key: 'i', risk: 'safe', primary: () => true, executes: false,
        when: o => !!o.port || 'no port configured: there is nothing to inspect', cli: o => `lsof -nP -iTCP:${o.port || '<port>'} -sTCP:LISTEN`, run: copyOnly },
      { id: 'protection.settings', on: 'branch protection', label: 'Open branch settings', key: 'o', risk: 'safe', primary: () => true, executes: false,
        when: o => !!o.slug || 'no github.com remote: there is no settings page', cli: o => `open https://github.com/${o.slug || '<owner>/<repo>'}/settings/branches`,
        run: o => { window.open(`https://github.com/${o.slug}/settings/branches`, '_blank', 'noopener'); return 'Opens in a new tab'; } },
      // Re-run collector (commands.js); Health reads again once the run ends.
      window.commands.syncCollector({ reread: () => load(), page: 'Health' }),
      // Dependencies, Security and Credentials (sd:2203, sd:2205, sd:2206) read stored rows; each line is copy only.
      { id: 'dependabot alerts.review', on: 'dependabot alerts', label: 'Review on GitHub', key: 'o', risk: 'safe', primary: () => true,
        executes: false, cli: o => o.cli, run: copyOnly },
      { id: 'secret scanning.review', on: 'secret scanning', label: 'Review on GitHub', key: 'o', risk: 'safe', primary: () => true,
        executes: false, cli: o => o.cli, run: copyOnly },
      { id: 'vault mirror.render', on: 'vault mirror', label: 'Render mirror', key: 'r', risk: 'confirm', executes: true,
        primary: () => true, cli: o => o.cli,
        consequence: () => 'Writes every base row\'s mirror note and page into the vault. A hand-edited note is kept and listed, not overwritten.',
        run: render },
      { id: 'credential.probe', on: 'credential', label: 'Probe again', key: 'r', risk: 'safe', primary: () => true, executes: false,
        cli: o => o.cli, run: copyOnly },
    );
    // Snooze (sd:1896) on every row type; an ok row wants nothing, so it has nothing to snooze.
    window.snooze.register(C, { page: 'health', reread: () => load(), off: o => o.state === 'ok' && 'an ok row has nothing to snooze',
      types: ['check', 'storage folder', 'build output', 'volume', 'worktree registrations', 'unread registrations', 'merged branches', 'port',
        'branch protection', 'dependabot alerts', 'secret scanning', 'credential', 'service', 'dashboard service', 'vault mirror'] });

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
