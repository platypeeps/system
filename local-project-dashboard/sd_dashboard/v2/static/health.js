// Health v2 (sd:2115): the page script. It loads before shell.js, which reads what it declares.
// Views: one at a time, Checkouts, Machine, GitHub and Jobs, each its own reader of /api/health/<view> (health_screen.part),
// started the first time the view opens; PAGE_VIEWS declares them to the shell's view switch, which owns #view=<id>. The
// lamps, the badge and each view's count are /api/health/summary, read at start and again after a view's read; it waits on
// no walker, so the strip draws before any view's rows. Show draws the rows that want something (warning, caution,
// unknown) by default; All adds the ok and queued ones, and ?show=all keeps it. Ignored (sd:5470) lists the rows Ignore hid,
// a snooze with no end, instead of the areas' rows, each with Unignore; ?show=ignored keeps it.
// Rows: the areas health_screen.document builds from the fleet child (worktree registrations),
// Operations > Ports' reader (ports), protection.rows (branch protection, and the
// alerts behind Dependencies and Security, sd:2205 and sd:2206), the nightly credentials heartbeat (sd:2203) and
// health_collectors (disk and merged branches, sd:2202 and sd:2204, and the machine-setup doctor, sd:3393).
// Ported from the design source's products/system/designs/v2/health page, its stylesheet and script, at d82daa1.
// An area with no reader shows as unknown with what it does not read, never as a clean lamp.
// Most fixes are a CLI line for Copy; Re-check reads the document again. The writes are Re-run collector, which starts
// the server's shadow sync (sd:2894), Snooze (sd:1896): an area's `snoozed` rows leave it, and snooze.js draws them, Ignore
// (sd:5470), the same write with no end, whose rows Show ignored lists, and
// the cleanups (sd:3393, sd:5105) and Clean all safe, which lists every ready one first, each behind a confirm that names what goes, posting the row's revision, and a service's
// Restart and Start (sd:3707), each a confirm that posts /api/services/<label>/<action>, `sd services`' own code.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put, plural, cells } = window.markup;
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const RANK = { warning: 0, caution: 1, unknown: 2, queued: 3, ok: 4 };
  const STATES = ['warning', 'caution', 'unknown', 'queued', 'ok'];
  // The design's icon and help per area; the help is page markup the shell renders with <b> and <code> only.
  const LOOK = {
    disk: ['hard-drive', '<b>Where the space went.</b> Volume use from df, then the biggest items the fleet itself creates: the storage folders Settings names (<code>dashboard.disk_storage</code>) and build output left in worktrees. A merged worktree must not keep build output (rule of 2026-09-25). A volume lights caution at 80% and warning at 90%. Build output is found, not sized.'],
    cred: ['key-round', '<b>Presence and expiry only.</b> Token values are never read into this page. GitHub reports a classic PAT expiry in a response header; Home Assistant tokens carry none the dashboard can read.'],
    wt: ['folder-x', '<b>Registered is not present.</b> A worktree whose directory is gone still holds its branch. Prune clears the registration only; it never touches a directory that exists.'],
    br: ['git-branch', '<b>Merged but not deleted.</b> Local branches already contained in origin’s default branch. <code>git branch -d</code> refuses any branch that is not merged, so the fix cannot lose work. Merged worktrees are those at the head a lane entry merged: Remove takes the tree, its branch and origin’s, and keeps a tree with files, a process in it or a branch that moved.'],
    dep: ['package', '<b>Dependabot, per repo.</b> Open alerts on the unarchived managed repos, as the nightly sync stored them. A repo whose alerts could not be read is unknown, never clean.'],
    sec: ['shield-alert', '<b>Secret scanning, public repos only.</b> Private repos are not scanned, by policy. A public repo with scanning off is a finding.'],
    ports: ['link-2', '<b>Who holds which port.</b> Configured service ports, then every other TCP listener the Mac shows. <b>Unknown is not free</b>: an uninspected listener says nothing about the port.'],
    doc: ['shield-check', '<b>What machine-setup doctor says.</b> Its WARN and FAIL lines, one row each, and the lines that pass. The doctor runs <code>brew doctor</code>, so an answer stands for ten minutes; a run still going, stopped at its deadline or failed is unknown, never clean.'],
    svc: ['activity', '<b>Long-running user services.</b> Each LaunchAgent that keeps a process alive, as <code>sd services list</code> reads it. Restart and Start run <code>sd services restart</code> and <code>start</code> here, against the row as shown. The dashboard compares the commit it serves with the checkout; its own service is protected, so a stale one gives the kickstart line to copy.'],
    mir: ['book-open', '<b>The vault mirror of base rows.</b> The <code>records-mirror</code> heartbeat a base row write leaves, read-only: this page writes no vault note.'],
    prot: ['lock', '<b>What each default branch enforces.</b> One column per registered repo, one line per check: a filled cell is a gap, a short mark passes, an empty cell does not apply. <b>Unknown is not protected</b>: a hatched column was not read and shows no cells. Every column opens its repo; the table below carries the same cells.'],
  };
  // Protection has no lamp: the annunciator holds 2 to 8 (the design's rule), and its matrix is Health's evidence strip.
  // Services (sd:3707) is a further area past a full strip: no lamp, its chip and section only (design.md, lamp: false).
  const NO_LAMP = new Set(['prot', 'svc']);
  const PH = 10; // one matrix line in the SVG's own units
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const $ = id => document.getElementById(id);
  // The views, for the shell's view switch: each panel is #view-<id> in health.html. `areas` is the page's own: the view
  // an area's lamp or an ?area= value opens. health_screen.VIEWS is the same list.
  window.PAGE_VIEWS = [
    { id: 'checkouts', label: 'Checkouts', api: '/api/health/checkouts', areas: ['wt', 'br'] },
    { id: 'machine', label: 'Machine', api: '/api/health/machine', areas: ['disk', 'svc', 'ports', 'doc'] },
    { id: 'github', label: 'GitHub', api: '/api/health/github', areas: ['prot', 'dep', 'sec'] },
    { id: 'jobs', label: 'Jobs', api: '/api/health/jobs', areas: ['cred', 'mir'] },
  ];

  let DOC = null, AREAS = [], ROWS = [], SNOOZED = [], IGNORED = [], selected = null, query = '';
  let SUMMARY = null, VIEW = null, showing = 'findings';
  const S = {}, readers = {}; // each view's last reading and its reader
  const F = { state: new Set(), area: new Set() };
  const byId = id => ROWS.find(r => r.id === id);
  // Show: the rows that want something, or all of them, or the ignored ones. A filter on ok or queued names those rows, so it shows them.
  const QUIET = new Set(['ok', 'queued']);
  const drawn = r => showing !== 'findings' || !QUIET.has(r.state) || F.state.has(r.state);
  const listed = () => showing === 'ignored' ? IGNORED : ROWS;
  const viewOf = area => PAGE_VIEWS.find(v => v.areas.includes(area))?.id;

  // The design's size wording, from a count of KiB.
  const size = kb => kb >= 1024 ** 3 ? `${(kb / 1024 ** 3).toFixed(2)} TiB` : kb >= 1024 ** 2 ? `${(kb / 1024 ** 2).toFixed(1)} GiB` : `${Math.round(kb / 1024)} MiB`;
  // A lamp's value is the summary's phrases (health_screen._lamp): `b` bold, then `t`.
  const lampValue = l => l.value.map((v, i) => html`${i ? ' · ' : ''}<span class="ph">${v.b ? html`<b>${v.b}</b> ` : ''}${v.t}</span>`);

  // The box says "repo, path, branch": a path can sit only in a row's facts (a checkout) or its list (registered worktrees).
  const text = r => [r.what, r.detail, r.kind, ...Object.values(r.facts || {}), ...(r.list || [])].join(' ').toLowerCase();
  const passes = r => drawn(r) && (!F.state.size || F.state.has(r.state)) && (!F.area.size || F.area.has(r.area))
    && (!query || text(r).includes(query));
  const filtering = () => !!(F.state.size || F.area.size || query);
  const quiet = a => a.rows.filter(r => !drawn(r)).length;
  // An area stays on the page while it has a row the filters pass, or when only the Area filter names it: a lamp for an area
  // with no reader then shows what that area does not read.
  const areaShown = a => a.rows.some(passes) || (!F.state.size && !query && (!F.area.size || F.area.has(a.id)));
  const loud = { warning: 0, caution: 1, unknown: 2 };

  // Every area's lamp, from the summary: pressing one opens its view with its area alone, or toggles it in this view.
  function lamps() {
    const L = SUMMARY ? SUMMARY.lamps : [];
    put($('annunciator'), cells([...L.filter(l => !NO_LAMP.has(l.id)).map(l => ({ button: true, state: l.state, pressed: F.area.has(l.id),
      attrs: html` data-area="${l.id}"`, label: l.name, mark: I(LOOK[l.id]?.[0] || 'circle-help'), val: lampValue(l),
      sr: `state ${l.state} · ${l.view === VIEW ? `shows only ${l.name} rows` : `opens ${PAGE_VIEWS.find(v => v.id === l.view)?.label}`}` })),
      { button: true, attrs: html` id="refresh"`, label: 'Observed', mark: I('rotate-ccw'),
        val: html`<span class="ph">${SUMMARY ? html`<b>${new Date(SUMMARY.read).toISOString().slice(11, 16)}</b> UTC` : 'not read'}</span> · <span class="ph">refresh</span>` }]));
  }

  // ---------- Views ----------
  // The shell draws the switch from PAGE_VIEWS (shell.js, view:start); onView reads a view on its first shell:view.
  const hashView = () => (/(?:^#|&)view=([a-z0-9-]+)/.exec(location.hash || '') || [])[1];
  // A view's reading becomes the page's rows; an unread view has none.
  function use(id) {
    ({ DOC, AREAS, ROWS, SNOOZED, IGNORED } = S[id] || { DOC: null, AREAS: [], ROWS: [], SNOOZED: [], IGNORED: [] });
    if (DOC) document.body.dataset.observed = DOC.read; else delete document.body.dataset.observed;
  }
  // A view opens with its own rows, keeping only the Area values it holds; the first time, its reader starts.
  function onView({ id, first }) {
    VIEW = id; use(id);
    const keep = PAGE_VIEWS.find(v => v.id === id).areas;
    [...F.area].forEach(a => { if (!keep.includes(a)) F.area.delete(a); });
    lamps(); window.snooze.draw($('snoozed'), SNOOZED); apply();
    if (first) { readers[id] = shell.read(spec(id)); load(); }
  }

  function filters(sum) {
    const grp = (key, name, opts) => html`<div class="fgroup"><span class="label">${name}</span><div class="chips">${opts.map(([v, l, n]) => html`<button type="button" class="chip" data-f="${key}" data-v="${v}" aria-pressed="${F[key].has(v) ? 'true' : 'false'}">${l} <span class="n">${n}</span></button>`)}</div></div>`;
    const chip = (v, l, n) => html`<button type="button" class="chip" data-show="${v}" aria-pressed="${String(showing === v)}">${l} <span class="n">${n}</span></button>`;
    const L = listed();
    return html`<div class="filters toolbar-2" role="group" aria-label="Filters"><div class="fgroup"><span class="label">Show</span><div class="chips">${chip('findings', 'findings', ROWS.filter(r => !QUIET.has(r.state)).length)}${chip('all', 'all', ROWS.length)}${chip('ignored', 'ignored', IGNORED.length)}</div></div>${grp('state', 'State', STATES.filter(s => L.some(r => r.state === s)).map(s => [s, html`<span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> ${s}`, L.filter(r => r.state === s).length]))}${AREAS.length > 1 ? grp('area', 'Area', AREAS.map(a => [a.id, a.name, L.filter(r => r.area === a.id).length])) : ''}${sum}</div>`;
  }

  function areas() {
    const count = (a, s) => a.rows.filter(r => r.state === s).length;
    return html`${AREAS.filter(areaShown).map(a => { const rows = a.rows.filter(passes); return html`<section class="area" id="a-${a.id}" aria-labelledby="h-${a.id}">
      <div class="sec-head"><h2 id="h-${a.id}">${I(LOOK[a.id]?.[0] || 'circle-help')}${a.name}<button class="help" type="button" aria-label="Help: ${a.name}" data-help="${LOOK[a.id]?.[1] || ''}">${I('circle-help')}</button></h2>
        <p class="tally">${a.read && !a.error ? STATES.filter(s => count(a, s)).map(s => html`<span class="g-${s}">${GLYPH[s]} ${count(a, s)} ${s}</span>`) : html`<span class="g-unknown">${GLYPH.unknown} not read</span>`}${a.at ? html`<span>read <time class="rel" datetime="${a.at}"></time></span>` : ''}</p></div>
      ${!a.read ? html`<p class="unread"><b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: ${a.missing.join(', ')}.</p>`
        : a.error ? html`<p class="unread"><b>Not read:</b> ${a.error}</p>`
        : a.missing.length ? html`<p class="unread"><b>Not read here:</b> ${a.missing.join(', ')}.</p>` : ''}
      ${a.stale ? html`<p class="unread"><b>Not re-read:</b> ${a.stale}</p>` : ''}
      ${a.read && !a.error ? pre(a) : ''}
      ${rows.length ? html`<table class="ledger" aria-labelledby="h-${a.id}" data-tfilter="page" data-toolbar="toolbar">
        <thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Finding</th><th scope="col">Fix</th></tr></thead>
        <tbody>${rows.map((r, i) => html`<tr data-id="${r.id}" aria-selected="${r.id === selected ? 'true' : 'false'}"${i > 0 && rows[i - 1].state !== r.state ? html` class="band-start"` : ''}>
          <td class="g g-${r.state}"><span aria-hidden="true">${GLYPH[r.state]}</span><span class="sr">${r.state}</span></td>
          <td class="what"><button type="button">${r.what}</button><span class="detail">${r.detail}</span></td>
          <td class="fix">${shell.commands.rowActions(r.id)}</td></tr>`)}</tbody></table>` : ''}
      ${a.read && !a.error && quiet(a) && !filtering() ? html`<p class="quiet">${rows.length ? '' : 'Nothing wants you here. '}${plural(quiet(a), 'ok or queued row')} not shown: Show all lists ${quiet(a) === 1 ? 'it' : 'them'}.</p>` : ''}
    </section>`; })}`;
  }

  // Show ignored (sd:5470): the rows Ignore hid, which the filters narrow, in place of the areas. A row here has no Details,
  // only Unignore, so it carries no data-id and the selection passes it by.
  function ignored() {
    const rows = IGNORED.filter(passes), name = id => AREAS.find(a => a.id === id)?.name;
    return html`<section class="area" aria-labelledby="h-ignored"><div class="sec-head"><h2 id="h-ignored">${I('eye-off')}Ignored</h2></div>
      ${rows.length ? html`<table class="ledger" aria-labelledby="h-ignored" data-tfilter="page" data-toolbar="toolbar"><thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Finding</th><th scope="col">Unignore</th></tr></thead>
        <tbody>${rows.map(r => html`<tr><td class="g g-${r.state}"><span aria-hidden="true">${GLYPH[r.state]}</span><span class="sr">${r.state}</span></td>
          <td class="what">${r.what}<span class="detail">${name(r.area)} · ${r.detail}</span></td>
          <td class="fix">${shell.commands.rowActions(`ignored:${r.id}`)}</td></tr>`)}</tbody></table>`
        : html`<p class="quiet">${IGNORED.length ? 'No ignored row matches the filter.' : 'No row here is ignored. Ignore on a row hides it until its problem changes.'}</p>`}
    </section>`;
  }

  // What an area draws above its ledger: v1's counts line and warnings, and for Protection the matrix and its table.
  const warn = w => html`<p class="warn" role="status"><span class="g-unknown" aria-hidden="true">${GLYPH.unknown}</span> ${w}</p>`;
  function pre(a) {
    const x = a.extra || {};
    if (a.id === 'disk') return x.volumes?.length ? html`<div class="bars" role="group" aria-label="Volume use: ${x.volumes.map(v => `${v.name} ${v.capacity}%`).join(', ')}">${x.volumes.map(v => html`<span class="name" title="${v.mount}">${v.name}</span><svg aria-hidden="true"><rect class="track" x="0.5" y="0.5" width="99%" height="11" rx="2"/><rect class="fill" x="0.5" y="0.5" width="${(v.used_kb / v.size_kb * 100 || 0).toFixed(1)}%" height="11" rx="2"/></svg><span class="v">${v.capacity}% · ${size(v.avail_kb)} free</span>`)}</div>` : '';
    // Clean all safe (sd:5105) heads Checkouts: one button for every cleanup row there whose own command would run.
    if (a.id === 'wt') return html`<p class="counts">${plural(safe().length, 'cleanup')} ready in Checkouts · ${shell.commands.rowActions('cleanup:safe')}</p>`;
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
      <dl>${Object.entries(r.facts || {}).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Source</dt><dd>${a.source}</dd><dt>Read</dt><dd><time class="rel" data-long datetime="${a.at || DOC.read}"></time></dd></dl>
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
    if (showing !== 'findings') q.set('show', showing);
    shell.url(q); // the shell keeps ?row=
  }
  // The open view's panel: its filters, the filter note and its areas. Another view's panel keeps what it last drew.
  function apply() {
    if (!VIEW) return;
    const on = filtering(), shown = listed().filter(passes).length;
    const total = listed().filter(drawn).length;
    // sd:5469: the count and Clear all end the filter bar, as on Activity; the pressed chips already say what filters.
    const sum = html`<div class="fsum"><span>${shown} of ${total}</span>${on ? html`<button class="linkbtn" type="button" data-clear>Clear all</button>` : ''}</div>`;
    put($(`body-${VIEW}`), html`${DOC ? filters(sum) : ''}${DOC && showing === 'ignored' ? ignored() : areas()}`);
    // The selection is always a row the viewer can see: the first visible one, or none with the reason.
    shell.reconcile({ rows: document.querySelectorAll('.ledger tbody tr[data-id]'), current: selected, select: id => select(id, false),
      clear: () => { selected = null; put($('details'), html`<p class="why">${!DOC ? 'Nothing is selected: Health could not be read.' : showing === 'ignored' ? 'Nothing is selected: an ignored row has Unignore only. Show findings to pick a row.' : ROWS.some(drawn) ? 'No row matches the filter, so nothing is selected. Show all rows to pick one.' : ROWS.length ? 'Nothing is selected: no row here wants you. Show all lists the ok rows.' : 'Nothing is selected: no area raised a row.'}</p>`); } });
    writeURL();
  }
  function clearFilters() { const had = filtering(); F.state.clear(); F.area.clear(); query = ''; $('q').value = ''; lamps(); apply(); return had; }

  // The badge is the summary's (sd:3446, health_screen._attention over every area); the subhead says it in words.
  function attention() {
    window.PAGE_ATTENTION = SUMMARY && SUMMARY.attention || { state: 'unknown', n: 0, what: 'rows' };
    shell.attention(window.PAGE_ATTENTION);
    const a = window.PAGE_ATTENTION, failed = SUMMARY ? SUMMARY.lamps.filter(l => l.error).length : 0;
    put($('subhead'), SUMMARY ? html`${plural(PAGE_VIEWS.length, 'view')} · ${a.n ? `${a.n} ${a.state} ${a.n === 1 ? 'row wants' : 'rows want'} you` : 'no row wants you'}${failed ? ` · ${plural(failed, 'area')} not read` : ''} · read <time class="rel" datetime="${SUMMARY.read}"></time>`
      : html`Health's summary could not be read.`);
  }
  // The summary: every lamp, the badge and each view's count of rows that want you, read at start and again after a view's
  // read. A failed one clears the strip; a view with an area not read has no count.
  const readSummary = () => shell.getJSON('/api/health/summary').then(doc => { SUMMARY = doc; }, () => { SUMMARY = null; })
    .then(() => {
      attention(); lamps();
      PAGE_VIEWS.forEach(v => { const L = SUMMARY ? SUMMARY.lamps.filter(l => l.view === v.id) : [];
        shell.view.count(v.id, L.length && L.every(l => l.n != null) ? L.reduce((n, l) => n + l.n, 0) : null); });
    });

  // One reader per view (read.js, sd:2418) holds the guards: of overlapping re-reads only the newest draws, a row the read no
  // longer lists runs no command and loses its pick, and a failed read leaves no row, lamp or command from the last one.
  // Each puts and retires only its own view's rows; one for a view not open keeps its reading and draws nothing.
  const load = () => Promise.resolve(readers[VIEW] && readers[VIEW].load()).then(() => readSummary());
  const spec = view => ({
    source: `/api/health/${view}`, what: `the ${PAGE_VIEWS.find(v => v.id === view).label} rows`,
    // Each view's reading state sits in its own panel, and a late read of a view not open leaves the selection alone.
    slot: `state-${view}`, active: () => view === VIEW,
    adopt: doc => {
      const areas = doc.areas;
      // Each row carries its area: the filter reads r.area on a.rows as well as on ROWS (sd:3708).
      areas.forEach(a => { [...a.rows, ...a.snoozed].forEach(r => { r.area = a.id; }); a.rows.sort((x, y) => RANK[x.state] - RANK[y.state]); });
      // An ignored row (sd:5470) is a snooze with no end: Show ignored lists it, not the Snoozed list under the page.
      const hidden = areas.flatMap(a => a.snoozed), forever = r => r.until === window.snooze.FOREVER;
      S[view] = { DOC: doc, AREAS: areas, ROWS: areas.flatMap(a => a.rows), SNOOZED: hidden.filter(r => !forever(r)), IGNORED: hidden.filter(forever) };
      if (view === VIEW) use(view);
      const { ROWS: rows } = S[view], snoozed = hidden, failed = areas.filter(a => a.error || a.stale), source = `/api/health/${view}`;
      return { objects: [...rows.map(r => ({ ...r, label: r.what })), ...snoozed.map(window.snooze.object)],
        state: failed.length ? { kind: 'partial', text: `${failed.map(a => `${a.name}: ${a.error || a.stale}`).join(' · ')}. The other areas are current.`, source }
          : doc.snooze_error ? window.snooze.unread(doc.snooze_error, source) : null };
    },
    // Nothing from the last read stays on screen: rows, lamps, the observed time and the badge.
    clear: () => { S[view] = null; if (view === VIEW) use(view); },
    draw: () => { if (view === VIEW) { lamps(); window.snooze.draw($('snoozed'), SNOOZED); } },
    current: () => view === VIEW ? selected : null,
    first: () => S[view]?.ROWS[0]?.id,
    // apply() reconciles against the rows the filters show; the new reading's Details are drawn here.
    select: id => { if (view !== VIEW) return; selected = id; apply(); if (byId(selected)) show(selected); },
    unselect: () => { if (view !== VIEW) return; selected = null; apply(); },
  });

  // ---------- Commands (the design source's commands.md) ----------
  // Ids, labels and keys are the design's. Prune, Delete merged and Remove build output run here (sd:3393): each confirms
  // with the count and the names, then posts the row and the revision it showed; the server reads the subject again and
  // refuses a changed one. A row with `disabled` (a merged branch a worktree holds) names why git would refuse it.
  // None runs in the shell's bulk: its confirm names four rows at most, and its tally counts an unknown outcome as landed.
  // Clean all safe (sd:5105) is its own command instead: its confirm names every row, and an unknown outcome stops it.
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
  // The Checkouts cleanups by row type: when each runs, what it does in a few words, and its line. Each command's `when`
  // is its `ready`, and Clean all safe runs a row only where that answers true.
  const CLEAN = {
    'worktree registrations': { ready: o => o.unreadable ? `${plural(o.unreadable, 'registration')} of this checkout could not be read, and prune would judge them itself` : revised(o),
      what: o => `prunes ${names(o.list)}`, cli: o => `git -C ${shell.shq(o.repo_path)} worktree prune -v` },
    'merged branches': { ready: o => o.disabled || revised(o), what: o => `deletes ${names(o.branches)}`, cli: o => o.cli },
    'merged worktrees': { ready: o => revised(o) === true ? (o.removable.length ? true : 'every merged worktree has a reason to stay: Details lists each') : revised(o),
      what: o => `removes ${names(o.removable)} with their branches`, cli: o => o.cli },
  };
  const safe = () => (S.checkouts?.ROWS || []).filter(r => CLEAN[r.type]?.ready(r) === true);
  // It runs the rows its confirm listed, each with its own revision, so a read in between adds none the operator did not
  // see. A refusal (4xx) wrote nothing for that row and the run goes on; an unknown outcome stops it there.
  const cleanAll = async rows => {
    const said = [];
    for (const r of rows) {
      try {
        const out = await shell.post('/api/health/cleanup', { row: r.id, revision: r.revision });
        said.push(`${r.what}: ${out.done.length} done${out.refused.length ? `, kept ${out.refused.length}` : ''}`);
      } catch (e) {
        if (!(e.status >= 400 && e.status < 500)) {
          load();
          return `Clean all safe stopped · not known what changed · ${r.what}: ${e.message}.${said.length ? ` Before it: ${said.join(' · ')}.` : ''} Health was read again; check the rows before you act again.`;
        }
        said.push(`${r.what}: not changed (${e.message})`);
      }
    }
    load();
    return `Clean all safe · ${said.join(' · ')}`;
  };
  // A service write (sd:3707) answers the request launchd took and what it reads now; the page reads again either way.
  const service = action => o => shell.post(`/api/services/${encodeURIComponent(o.facts.Label)}/${action}`, { revision: o.revision }).then(out => {
    load();
    return `${o.facts.Label}: ${out.request.status} · ${out.request.message}`;
  }, e => {
    load();
    if (e.status >= 400 && e.status < 500) throw e;
    return `Not known what changed · ${o.facts.Label}: ${e.message}. Health was read again; check the row before you act again.`;
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
    // The protection collector is one object for the page, not a row of a reading: no read retires it.
    C.put({ id: 'collector:protection', type: 'collector', label: 'protection collector' });
    C.put({ id: 'cleanup:safe', type: 'safe cleanups', label: 'Clean all safe' });
    C.register(
      { id: 'check.recheck', on: 'check', label: 'Re-check', does: 'Reads Health again; the line reruns this check.', key: 'e', icon: 'rotate-ccw', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: () => { load(); return 'Reading Health again'; } },
      { id: 'storage folder.du', on: 'storage folder', label: 'Show biggest', does: 'Gives the line that lists this folder\'s biggest entries.', key: 'b', icon: 'chart-no-axes-column', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: copyOnly },
      { id: 'build output.rm', on: 'build output', label: 'Remove build output', does: 'Deletes build output in merged, clean worktrees; this cannot be undone.', key: 'r', icon: 'folder-x', risk: 'confirm', executes: true, primary: () => true,
        when: o => revised(o) === true ? (o.remove.length ? true : 'every merged worktree has uncommitted changes: nothing is removed') : revised(o),
        cli: o => o.cli, consequence: o => `Deletes ${o.remove.length} build output folders in merged, clean worktrees: ${names(o.remove)}.`
          + (o.kept.length ? ` Keeps ${o.kept.length} with uncommitted changes: ${names(o.kept)}.` : '') + ' Each worktree is checked again first. This cannot be undone.', run: cleanup },
      // Volume rows are the build's: the design draws volumes as bars only. Its line shows the volume's use in a terminal.
      { id: 'volume.df', on: 'volume', label: 'Show use', does: 'Gives the line that shows this volume\'s use.', key: 'b', icon: 'gauge', risk: 'safe', executes: false, primary: () => true,
        cli: o => o.cli, run: copyOnly },
      { id: 'worktree registrations.prune', on: 'worktree registrations', label: 'Prune registrations', does: 'Removes registrations of worktrees whose folders are gone; cannot be undone.', key: 'p', risk: 'confirm',
        executes: true, primary: () => true, cli: CLEAN['worktree registrations'].cli, when: CLEAN['worktree registrations'].ready,
        consequence: o => `Removes ${o.facts.Registered} worktree registrations whose directories are gone: ${names(o.list)}. No directory is touched.`, run: cleanup },
      { id: 'merged branches.delete', on: 'merged branches', label: 'Delete merged', does: 'Deletes local branches already merged; this cannot be undone.', key: 'd', risk: 'confirm', executes: true,
        primary: () => true, when: CLEAN['merged branches'].ready, cli: o => o.cli,
        consequence: o => `Deletes ${o.facts.Merged} local branches already in origin's default branch: ${names(o.branches)}. git branch -d refuses any unmerged one.`, run: cleanup },
      { id: 'merged worktrees.remove', on: 'merged worktrees', label: 'Remove merged', does: 'Removes worktrees the lane merged, with their branches; cannot be undone.', key: 'r', icon: 'folder-x', risk: 'confirm',
        executes: true, primary: () => true, when: CLEAN['merged worktrees'].ready, cli: o => o.cli,
        consequence: o => `Removes ${o.removable.length} worktrees the lane merged, with their build output, their local branch and origin's: ${names(o.removable)}.`
          + (o.kept.length ? ` Keeps ${o.kept.length}: ${names(o.kept.map(x => `${x.name} (${x.why})`))}.` : '') + ' Each is checked again first. This cannot be undone.', run: cleanup },
      { id: 'safe cleanups.clean', on: 'safe cleanups', label: 'Clean all safe', does: 'Runs every Checkouts cleanup that is ready, after listing each.', key: 'a', risk: 'confirm',
        executes: true, primary: () => true, when: () => safe().length ? true : 'no Checkouts cleanup is ready: each row says why',
        cli: () => safe().map(r => CLEAN[r.type].cli(r)).join('\n'),
        consequence: o => { o.listed = safe(); return `Runs ${plural(o.listed.length, 'cleanup')} in turn, each checked again first: ${o.listed.map(r => `${r.what} (${CLEAN[r.type].what(r)})`).join('; ')}. This cannot be undone.`; },
        run: o => cleanAll(o.listed || safe()) },
    );
    // Services (sd:3707): Restart and Start run sd services' code through Operations' route. sd_db.services protects the
    // dashboard's own service, and a restart from its page would end the request that asks, so that row is copy only.
    C.register(
      { id: 'service.restart', on: 'service', label: 'Restart', does: 'Stops and starts the service through launchctl; connections drop.', key: 'r', icon: 'rotate-ccw', risk: 'confirm', executes: true,
        primary: o => o.facts.State === 'running', when: can('restart'),
        cli: o => `sd services restart ${o.facts.Label}`,
        consequence: o => `Stops ${o.facts.Label} and starts it again through launchctl (bootout, bootstrap, kickstart). Its open connections drop.`,
        run: service('restart') },
      { id: 'service.start', on: 'service', label: 'Start', does: 'Loads and starts the service through launchctl.', key: 's', icon: 'play', risk: 'confirm', executes: true,
        primary: o => o.facts.State !== 'running', when: can('start'),
        cli: o => `sd services start ${o.facts.Label}`,
        consequence: o => `Loads ${o.facts.Label} if it is unloaded and kickstarts it through launchctl.`,
        run: service('start') },
      { id: 'dashboard service.kickstart', on: 'dashboard service', label: 'Kickstart', does: 'Gives the line that restarts the dashboard service.', key: 'l', icon: 'rocket', risk: 'safe', executes: false,
        primary: () => true, cli: o => o.kickstart, run: copyOnly },
    );
    // Ports and Protection are read-only: each line is copy only.
    C.register(
      { id: 'port.inspect', on: 'port', label: 'Inspect listener', does: 'Gives the lsof line that shows what listens on this port.', key: 'i', icon: 'search', risk: 'safe', primary: () => true, executes: false,
        when: o => !!o.port || 'no port configured: there is nothing to inspect', cli: o => `lsof -nP -iTCP:${o.port || '<port>'} -sTCP:LISTEN`, run: copyOnly },
      { id: 'protection.settings', on: 'branch protection', label: 'Open branch settings', does: 'Opens the repo\'s branch protection settings on GitHub.', key: 'o', icon: 'settings', risk: 'safe', primary: () => true, executes: false,
        when: o => !!o.slug || 'no github.com remote: there is no settings page', cli: o => `open https://github.com/${o.slug || '<owner>/<repo>'}/settings/branches`,
        run: o => { window.open(`https://github.com/${o.slug}/settings/branches`, '_blank', 'noopener'); return 'Opens in a new tab'; } },
      // Re-run collector (commands.js); Health reads again once the run ends.
      window.commands.syncCollector({ reread: () => load(), page: 'Health' }),
      // Dependencies, Security and Credentials (sd:2203, sd:2205, sd:2206) read stored rows; each line is copy only.
      { id: 'dependabot alerts.review', on: 'dependabot alerts', label: 'Review on GitHub', does: 'Gives the line that opens the Dependabot alerts on GitHub.', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: () => true,
        executes: false, cli: o => o.cli, run: copyOnly },
      { id: 'secret scanning.review', on: 'secret scanning', label: 'Review on GitHub', does: 'Gives the line that opens the secret scanning alerts on GitHub.', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: () => true,
        executes: false, cli: o => o.cli, run: copyOnly },
      { id: 'credential.probe', on: 'credential', label: 'Probe again', does: 'Gives the line that probes this credential again.', key: 'r', icon: 'rotate-ccw', risk: 'safe', primary: () => true, executes: false,
        cli: o => o.cli, run: copyOnly },
    );
    // Snooze (sd:1896) and Ignore (sd:5470) on every row type; an ok row wants nothing, so it has nothing to snooze.
    window.snooze.register(C, { page: 'health', ignore: true, reread: () => load(), off: o => o.state === 'ok' && 'an ok row has nothing to snooze',
      types: ['check', 'storage folder', 'build output', 'volume', 'worktree registrations', 'unread registrations', 'merged branches', 'merged worktrees', 'port',
        'branch protection', 'dependabot alerts', 'secret scanning', 'credential', 'service', 'dashboard service', 'vault mirror'] });

    const u = new URLSearchParams(location.search);
    (u.get('state') || '').split(',').filter(s => STATES.includes(s)).forEach(s => F.state.add(s));
    (u.get('area') || '').split(',').filter(Boolean).forEach(a => F.area.add(a));
    query = (u.get('q') || '').trim().toLowerCase(); $('q').value = u.get('q') || '';
    showing = ['all', 'ignored'].includes(u.get('show')) ? u.get('show') : 'findings';
    // An ?area= link opens that area's view unless #view= names one. The shell's first shell:view fires after this listener.
    const asked = viewOf([...F.area][0]);
    if (asked && !hashView()) { history.replaceState(null, '', `${location.pathname}${location.search}#view=${asked}`); shell.view.show(asked); }

    // Each panel: its Show and filter chips, the filter note's clear, a matrix column and a row.
    PAGE_VIEWS.forEach(v => $(`view-${v.id}`).addEventListener('click', e => {
      if (e.target.closest('[data-clear]')) { clearFilters(); return; }
      const b = e.target.closest('[data-show]');
      if (b) { if (b.dataset.show !== showing) { showing = b.dataset.show; apply(); } return; }
      const c = e.target.closest('[data-f]');
      if (c) { const set = F[c.dataset.f]; set.has(c.dataset.v) ? set.delete(c.dataset.v) : set.add(c.dataset.v); lamps(); apply(); return; }
      if (e.target.closest('.rowact') || e.target.closest('.help')) return;
      const col = e.target.closest('.pmx-col'); if (col) { select(col.dataset.row, true); return; }
      const tr = e.target.closest('tbody tr[data-id]'); if (tr) select(tr.dataset.id, true);
    }));
    // A lamp is the Area chip for its area: pressing it adds the area to the filter, pressing it again takes it out.
    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      if (c.id === 'refresh') return load();
      const id = c.dataset.area, lamp = SUMMARY?.lamps.find(l => l.id === id);
      if (lamp && lamp.view !== VIEW) { F.area.clear(); F.area.add(id); shell.view.show(lamp.view); return; }
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

    document.addEventListener('shell:view', e => onView(e.detail));
    readSummary();
  });
})();
