// Activity (sd:2111): the design source's products/system/designs/v2/activity.js at d82daa1, ported. Each change from the
// reference is marked "build:". The events are /api/activity (activity_screen.py), never sample data. A command that executes
// posts to the route v1 already answers, and its toast comes after the write lands, not before.
// Palette "This page" group. shell.js reads it at start, so it is set before the shell runs.
const { html, put, plural } = window.markup;
window.PAGE_COMMANDS = [
  { label: 'Filter the timeline', icon: 'search', run: () => document.getElementById('shift').focus() },
];
window.PAGE_KEYS = [['/', 'Filter events, capture a task or ask'], ['→', 'In the field: the next reading (filter, task, ask)']];
addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest, openPane } = window.shell;
  const C = window.shell.commands;
  const $ = id => document.getElementById(id);

  // build: the sources are the document's. A kind with no collector is unknown, with the document's reason.
  const KINDS = [
    { id: 'merge', name: 'Merges', icon: 'git-pull-request', src: 'sd-ship delivery notes' },
    { id: 'run', name: 'Runs', icon: 'play', src: 'runner assignments and launchd jobs' },
    // build: a review is the one its merge carried, from the delivery note (sd:2211); one that never shipped is not recorded.
    { id: 'review', name: 'Reviews', icon: 'bot', src: 'sd-ship delivery notes: the review each merge carried' },
    { id: 'deploy', name: 'Deploys', icon: 'hard-drive-download', src: 'none' },
    { id: 'mail', name: 'Mail', icon: 'mail', src: 'none' },
    { id: 'command', name: 'Commands', icon: 'terminal', src: 'the execution journal: notes of kind exec' },
  ];
  const SOURCES = { merge: ['merge'], run: ['run', 'job'], review: ['review'], command: ['command'] };
  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  const kindOf = id => KINDS.find(k => k.id === id);
  const hhmm = iso => iso.slice(11, 16);
  const ago = iso => { const m = Math.round((OBSERVED - Date.parse(iso)) / 60000); return m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`; };
  // The document holds the 24 hours and, for the journal, older records too; the chart and the 24h range are the window.
  const inWindow = e => Date.parse(e.at) >= RANGE.from;

  // ---------- Data (build) ----------
  let DOC = null, EVENTS = [], OBSERVED = Date.now(), RANGE = { from: OBSERVED - 864e5, to: OBSERVED }, REPOS = [];
  // A kind is unknown when nothing collects it, or when every source behind it failed to read this time.
  const unknownWhy = k => {
    if (!DOC) return 'not read yet';
    if (DOC.unknown[k]) return DOC.unknown[k];
    const src = SOURCES[k] || [];
    return src.length && src.every(s => DOC.sources[s]) ? src.map(s => DOC.sources[s]).join('; ') : '';
  };
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
  // build: the reader (read.js, sd:2418; adopted by sd:2489) holds the guards. Of overlapping reads only the newest draws;
  // an event the read no longer lists loses its pick and runs no command; a failed load leaves no row, object or observed
  // time from the last one; and a failed reread after a write keeps the rows and says the write landed.
  const activity = window.shell.read({
    source: '/api/activity', what: 'the events',
    adopt: doc => {
      DOC = doc; EVENTS = doc.events; OBSERVED = Date.parse(doc.read); RANGE = { from: Date.parse(doc.from), to: Date.parse(doc.to) };
      document.body.dataset.observed = doc.read;
      REPOS = Object.entries(EVENTS.reduce((a, e) => (e.repo && (a[e.repo] = (a[e.repo] || 0) + 1), a), {})).sort((a, b) => b[1] - a[1]);
      const ids = new Set(EVENTS.map(e => e.id));
      Object.keys(OUTPUT).forEach(id => { if (!ids.has(id)) delete OUTPUT[id]; });
      const failed = Object.entries(doc.sources).filter(([, why]) => why);
      return { objects: EVENTS.map(objectOf),
        state: failed.length ? { kind: 'partial', text: failed.map(([s, why]) => `${s}: ${why}`).join(' · '), source: '/api/activity' }
          : !EVENTS.some(inWindow) ? { kind: 'empty', text: 'No merge, run, review or command in the last 24 hours.' } : null };
    },
    clear: () => { DOC = null; EVENTS = []; REPOS = []; Object.keys(OUTPUT).forEach(id => delete OUTPUT[id]); delete document.body.dataset.observed; },
    draw: () => update(),
    // update() has already reconciled the selection against the rows the filters show, so the reader settles on that
    // one: a ?row= the filters hide gives way to the first shown row, as a hidden row does on every redraw.
    current: () => selected,
    first: () => selected,
    select: (id, opened) => { const shown = selected; if (shown) select(shown, opened && shown === id); else unselect(); },
    unselect: () => unselect(),
  });
  const load = () => activity.load(), reread = () => activity.reread();

  // ---------- State (mirrored in the URL) ----------
  const F = { kind: new Set(), repo: new Set(), state: '', q: '', hour: false, all: false }; // all: every event read, the journal's older records too
  let page = 1, size = 50, selected = null, allRepos = false;
  function readURL() {
    const p = new URLSearchParams(location.search);
    (p.get('kind') || '').split(',').filter(Boolean).forEach(v => F.kind.add(v));
    (p.get('repo') || '').split(',').filter(Boolean).forEach(v => F.repo.add(v));
    F.state = p.get('state') || ''; F.q = p.get('q') || ''; F.hour = p.get('range') === '1h'; F.all = p.get('range') === 'all';
    const asked = Number(p.get('page'));
    page = Number.isInteger(asked) && asked > 0 ? asked : 1; // a page is a positive integer; anything else is the first page
    size = [25, 50, 100, 200].includes(+p.get('size')) ? +p.get('size') : 50;
    selected = window.shell.row() || null;
  }
  function writeURL() {
    const p = new URLSearchParams();
    if (F.kind.size) p.set('kind', [...F.kind].join(','));
    if (F.repo.size) p.set('repo', [...F.repo].join(','));
    if (F.state) p.set('state', F.state);
    if (F.q) p.set('q', F.q);
    if (F.hour) p.set('range', '1h');
    if (F.all) p.set('range', 'all');
    if (page > 1) p.set('page', page);
    if (size !== 50) p.set('size', size);
    window.shell.url(p); // build: the shell keeps ?row=
  }
  const since = () => F.hour ? OBSERVED - 36e5 : 0;
  const active = () => F.kind.size + F.repo.size + (F.state ? 1 : 0) + (F.q ? 1 : 0) + (F.hour || F.all ? 1 : 0);
  const match = e => (!F.kind.size || F.kind.has(e.k)) && (!F.repo.size || F.repo.has(e.repo)) && (!F.state || e.s === F.state) && (F.all || Date.parse(e.at) >= (since() || RANGE.from)) &&
    (!F.q || `${e.what} ${e.repo || ''} ${e.ref} ${e.detail || ''}`.toLowerCase().includes(F.q.toLowerCase())); // ?q= keeps its case

  // ---------- Annunciator ----------
  // Rail badge (shell.js): the loudest state on this page and how many events carry it.
  function pageAttention() {
    // The badge says "in 24 h", so it counts the window only: an old journal record never lights it.
    const day = EVENTS.filter(inWindow), w = day.filter(e => e.s === 'warning').length, c = day.filter(e => e.s === 'caution').length;
    if (!DOC) { window.shell.attention?.({ state: 'unknown', n: 0, what: 'events' }); return; }
    window.shell.attention?.({ state: w ? 'warning' : c ? 'caution' : 'ok', n: w || c, what: w ? 'failed events in 24 h' : 'caution events in 24 h' });
  }
  function renderAnnunciator() {
    pageAttention();
    const n = (k, s) => EVENTS.filter(e => e.k === k && (!s || e.s === s)).length;
    const worst = k => unknownWhy(k) ? 'unknown' : n(k, 'warning') ? 'warning' : n(k, 'caution') ? 'caution' : 'ok';
    const pressed = k => String(F.kind.has(k)); // a lamp, its Kind chip and its lane button are one control (design.md § Interaction rules)
    // build: a kind with no answer is a lamp that cannot be pressed, as the design's deploy cell is.
    const cell = (k, val) => unknownWhy(k.id)
      ? html`<li><div class="cell" data-kind="${k.id}" data-state="unknown"><span class="lbl">${k.name}${ICON(k.icon)}</span><span class="val"><span class="ph"><b>unknown</b></span> · <span class="ph">no source</span></span></div></li>`
      : html`<li><button class="cell" type="button" data-kind="${k.id}" data-state="${worst(k.id)}" aria-pressed="${pressed(k.id)}"><span class="lbl">${k.name}${ICON(k.icon)}</span><span class="val">${val}</span></button></li>`;
    // Runs mixes two sources: a job carries `job`, an assignment carries `status`; count each by its own field.
    const asg = EVENTS.filter(e => e.k === 'run' && e.status);
    const jobsFailed = EVENTS.filter(e => e.k === 'run' && e.job && e.s === 'warning').length;
    const VAL = {
      merge: html`<span class="ph"><b>${n('merge')}</b> merged</span> · <span class="ph">${plural(new Set(EVENTS.filter(e => e.k === 'merge').map(e => e.repo)).size, 'repo')}</span>`,
      run: html`<span class="ph"><b>${jobsFailed}</b> jobs failed</span> · <span class="ph">${asg.filter(e => e.status === 'blocked').length} of ${asg.length} runs blocked</span>`,
      command: html`<span class="ph"><b>${n('command', 'warning')}</b> failed</span> · <span class="ph">${n('command')} in the journal</span>`,
      // build: the design's "want changes · approve" has no source; a delivery records clean or adjudicated (sd:2211).
      review: html`<span class="ph"><b>${n('review', 'caution')}</b> adjudicated</span> · <span class="ph">${n('review', 'ok')} clean</span>${DOC?.review_unrecorded ? html` · <span class="ph">${plural(DOC.review_unrecorded, 'merge')} unrecorded</span>` : ''}`,
    };
    const d = new Date(OBSERVED);
    put($('annunciator'), html`
      ${KINDS.map(k => cell(k, VAL[k.id] || ''))}
      <li><button class="cell" type="button" id="refresh"><span class="lbl">Observed${ICON('rotate-ccw')}</span><span class="val"><span class="ph"><b>${d.toISOString().slice(11, 16)}</b> UTC</span> · <span class="ph">${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })}</span></span></button></li>`);
  }
  $('annunciator').addEventListener('click', e => {
    const c = e.target.closest('button.cell'); if (!c) return;
    if (c.id === 'refresh') return load(); // build: reads the document again instead of reloading the page
    const k = c.dataset.kind;
    F.kind.has(k) ? F.kind.delete(k) : F.kind.add(k); page = 1; update(); // adds to the kind filter, as the chip and the lane do
  });

  // ---------- Lane chart (hand-rolled SVG, patterns.md § Charts) ----------
  const W = 720, H = 24;
  const x = t => ((Date.parse(t) - RANGE.from) / (RANGE.to - RANGE.from)) * W;
  const hours = () => Array.from({ length: 25 }, (_, i) => RANGE.from + i * 36e5);
  function renderRange() {
    // build: the document holds 24 hours and the journal's older records, so 7d and 30d stay off and say why.
    put($('range'), html`
      <button class="chip" type="button" data-r="1h" aria-pressed="${String(F.hour)}">1h</button>
      <button class="chip" type="button" data-r="24h" aria-pressed="${String(!F.hour && !F.all)}">24h</button>
      <button class="chip" type="button" data-r="all" aria-pressed="${String(F.all)}">All read <span class="n">+${EVENTS.filter(e => !inWindow(e)).length} older</span></button>
      <button class="chip" type="button" data-r="7d" aria-pressed="false">7d <span class="n">▨ not read</span></button>
      <button class="chip" type="button" data-r="30d" aria-pressed="false">30d <span class="n">▨ not read</span></button>`);
  }
  $('range').addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c) return;
    const r = c.dataset.r;
    // build: the range stays where it was, which is 1h or All read as often as 24h.
    if (r === '7d' || r === '30d') return toast(`${r} is not read: /api/activity holds the last 24 hours. The range stays at ${F.hour ? '1h' : F.all ? 'All read' : '24h'}.`);
    F.hour = r === '1h'; F.all = r === 'all'; page = 1; update();
  });
  function renderLanes() {
    const H24 = hours();
    const grid = H24.filter((_, i) => i % 3 === 0).map(t => { const gx = ((t - RANGE.from) / (RANGE.to - RANGE.from) * W).toFixed(1); return html`<line x1="${gx}" x2="${gx}" y1="0" y2="${String(H)}"/>`; });
    const order = { ok: 0, queued: 0, unknown: 0, caution: 1, warning: 2 };
    const lanes = KINDS.map(k => {
      const evs = EVENTS.filter(e => e.k === k.id && inWindow(e) && (!F.repo.size || F.repo.has(e.repo))); // the chart is the 24 h window
      const why = unknownWhy(k.id);
      const btn = html`<button class="lane-btn" type="button" data-kind="${k.id}" aria-pressed="${String(F.kind.has(k.id))}">${ICON(k.icon)}<span>${k.name}</span><span class="n">${why ? '▨' : String(evs.length)}</span></button>`;
      if (why) return html`<div class="lane">${btn}<div class="track unknown"><svg viewBox="0 0 ${String(W)} ${String(H)}" preserveAspectRatio="none" aria-hidden="true"><g class="grid">${grid}</g></svg><p class="why-unknown"><span>▨ unknown · no ${k.name.toLowerCase().replace(/s$/, '')} source</span></p></div></div>`;
      // Draw ok first, then caution, then warning, so the loudest ticks sit on top.
      const ticks = evs.slice().sort((a, b) => order[a.s] - order[b.s]).map(e => html`<rect class="tick ${e.s}${e.id === selected ? ' sel' : ''}" data-id="${e.id}" x="${(x(e.at) - 1.5).toFixed(1)}" y="4" width="3" height="${String(H - 8)}"><title>${e.what} · ${hhmm(e.at)}Z</title></rect>`);
      return html`<div class="lane">${btn}<div class="track"><svg viewBox="0 0 ${String(W)} ${String(H)}" preserveAspectRatio="none" role="img" aria-label="${k.name}: ${String(evs.length)} in 24 hours"><g class="grid">${grid}</g>${ticks}</svg></div></div>`;
    });
    const axis = html`<div class="lane"><span class="axis-label"></span><div class="axis"><svg viewBox="0 0 ${String(W)} 16" preserveAspectRatio="none" aria-hidden="true">${H24.filter((_, i) => i % 6 === 0 && i < 24).map(t => html`<text x="${((t - RANGE.from) / (RANGE.to - RANGE.from) * W + 3).toFixed(1)}" y="12">${new Date(t).toISOString().slice(11, 16)}</text>`)}</svg></div></div>`;
    put($('lanes'), html`${lanes}${axis}`);
    const bins = H24.slice(0, 24);
    put($('chart-table'), html`<table><caption class="sr">Events per hour by kind</caption><thead><tr><th scope="col">Hour (UTC)</th>${KINDS.map(k => html`<th scope="col">${k.name}</th>`)}</tr></thead><tbody>${bins.map(t => html`<tr><td>${new Date(t).toISOString().slice(5, 16).replace('T', ' ')}</td>${KINDS.map(k => unknownWhy(k.id) ? html`<td class="u">▨</td>` : html`<td>${String(EVENTS.filter(e => e.k === k.id && (!F.repo.size || F.repo.has(e.repo)) && Date.parse(e.at) >= t && Date.parse(e.at) < t + 36e5).length)}</td>`)}</tr>`)}</tbody></table>`);
  }
  $('lanes').addEventListener('click', e => {
    const t = e.target.closest('rect.tick'); if (t) return select(t.dataset.id, true);
    const b = e.target.closest('.lane-btn'); if (!b) return;
    const k = b.dataset.kind;
    if (unknownWhy(k)) return showKind(k);
    F.kind.has(k) ? F.kind.delete(k) : F.kind.add(k); page = 1; update();
  });

  // ---------- Filters ----------
  function renderFilters() {
    const chip = (f, v, label, pressed, n) => html`<button type="button" class="chip" data-f="${f}" data-v="${v}" aria-pressed="${String(pressed)}">${label}${n != null ? html` <span class="n">${String(n)}</span>` : ''}</button>`;
    const shown = EVENTS.filter(match).length;
    const repos = allRepos ? REPOS : REPOS.filter(([r], i) => i < 8 || F.repo.has(r));
    put($('filters'), html`
      <div class="fgroup"><span class="label">Kind</span><div class="chips">${KINDS.map(k => unknownWhy(k.id) ? html`<button type="button" class="chip" data-f="kind-unknown" data-v="${k.id}" aria-pressed="false">${k.name} <span class="n">▨</span></button>` : chip('kind', k.id, k.name, F.kind.has(k.id), EVENTS.filter(e => e.k === k.id).length))}</div></div>
      <div class="fgroup"><span class="label">State</span><div class="chips">${[['warning', '■ failed'], ['caution', '▲ caution'], ['ok', '● ok']].map(([s, l]) => chip('state', s, l, F.state === s, EVENTS.filter(e => e.s === s).length))}</div></div>
      ${REPOS.length ? html`<div class="fgroup"><span class="label">Repo</span><div class="chips">${repos.map(([r, n]) => chip('repo', r, r, F.repo.has(r), n))}${REPOS.length > 8 ? html`<button type="button" class="linkbtn" id="more-repos" aria-expanded="${String(allRepos)}">${allRepos ? 'fewer' : `all ${REPOS.length} repos`}</button>` : ''}</div></div>` : ''}
      <div class="fsum"><span>${shown} of ${EVENTS.length}${F.q ? ` · “${F.q}”` : ''}</span>${active() ? html`<button class="linkbtn" type="button" id="clear">Clear all</button>` : ''}</div>`);
  }
  $('filters').addEventListener('click', e => {
    if (e.target.closest('#clear')) return clearAll();
    if (e.target.closest('#more-repos')) { allRepos = !allRepos; return renderFilters(); }
    const c = e.target.closest('.chip'); if (!c) return;
    const { f, v } = c.dataset;
    if (f === 'kind-unknown') return showKind(v);
    if (f === 'kind' || f === 'repo') F[f].has(v) ? F[f].delete(v) : F[f].add(v);
    if (f === 'state') F.state = F.state === v ? '' : v;
    page = 1; update();
  });
  function clearAll() { F.kind.clear(); F.repo.clear(); F.state = ''; F.q = ''; F.hour = false; F.all = false; input.value = ''; prev.hidden = true; ghost.textContent = ''; page = 1; update(); }

  // ---------- Timeline ----------
  const tbody = $('rows');
  const whereOf = e => e.k === 'command' || e.k === 'run' ? (e.item ? `sd:${e.item}` : e.job || e.ref || '') : e.ref || '';
  function renderRows() {
    const all = EVENTS.filter(match);
    const pages = Math.max(1, Math.ceil(all.length / size)); page = Math.min(page, pages);
    const rows = all.slice((page - 1) * size, page * size);
    const perHour = all.reduce((a, e) => (a[e.at.slice(0, 13)] = (a[e.at.slice(0, 13)] || 0) + 1, a), {});
    let last = '';
    put(tbody, html`${rows.map(e => {
      const h = e.at.slice(0, 13), band = h !== last ? html`<tr class="hour" aria-hidden="true"><td colspan="6">${h.slice(11)}:00 UTC · ${new Date(e.at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })}<span class="n">${plural(perHour[h], 'event')}</span></td></tr>` : ''; last = h;
      const sub = e.k === 'merge' ? e.repo : e.detail;
      return html`${band}<tr data-id="${e.id}" data-changed="${e.at}" aria-selected="${String(e.id === selected)}">
        <td class="t"><time datetime="${e.at}" title="${ago(e.at)}">${hhmm(e.at)}</time></td>
        <td class="g g-${e.s}">${GLYPH[e.s] || GLYPH.unknown}<span class="sr">${e.s}</span></td>
        <td class="k">${ICON(kindOf(e.k).icon)}${e.k}</td>
        <td class="what"><button type="button">${e.what}<small>${sub || ''}</small></button></td>
        <td class="ref">${whereOf(e)}</td>
        <td class="act">${C.rowActions(e.id)}</td></tr>`;
    })}`);
    const empty = $('empty');
    empty.hidden = !!all.length || !EVENTS.some(inWindow);
    put(empty, all.length || !EVENTS.some(inWindow) ? html`` : html`No event matches these filters. <button class="linkbtn" type="button" data-clear>Clear all</button>`);
    const a = all.length ? (page - 1) * size + 1 : 0, z = Math.min(page * size, all.length);
    put($('pager'), html`<span>${a}–${z} of ${all.length}</span>
      <div class="chips" role="group" aria-label="Page">${page > 1 ? html`<button class="chip" type="button" data-p="${String(page - 1)}">${ICON('chevron-left')}Previous</button>` : ''}${page < pages ? html`<button class="chip" type="button" data-p="${String(page + 1)}">Next${ICON('chevron-right')}</button>` : ''}</div>
      <div class="chips" role="group" aria-label="Rows per page">${[25, 50, 100, 200].map(n => html`<button class="chip" type="button" data-size="${String(n)}" aria-pressed="${String(n === size)}">${n}</button>`)}</div>`);
    const c = s => all.filter(e => e.s === s).length;
    put($('tally'), html`<span class="g-warning">■ ${c('warning')} failed</span><span class="g-caution">▲ ${c('caution')} caution</span><span>● ${c('ok')} ok</span>`);
    put($('sub'), html`${plural(EVENTS.filter(inWindow).length, 'event')} across the fleet · last 24 hours to ${new Date(OBSERVED).toISOString().slice(11, 16)} UTC, ${new Date(OBSERVED).toLocaleDateString('en-GB', { day: 'numeric', month: 'long', timeZone: 'UTC' })}`);
    // build: the design's journal note, with this document's counts (sd:2183: the reader lists every writer's notes).
    const journal = EVENTS.filter(e => e.k === 'command'), older = journal.filter(e => !inWindow(e)).length;
    const skipped = Object.values(DOC?.journal_skipped || {}).reduce((a, n) => a + n, 0);
    put($('journal-note'), html`Commands are the execution journal: ${plural(journal.length, 'record')} of kind exec, palette and runner runs alike.${skipped ? ` ${plural(skipped, 'exec note')} no known writer left ${skipped === 1 ? 'is' : 'are'} skipped.` : ''} All read adds the ${String(older)} older than this window.${DOC?.journal_unread ? ` The journal reads the latest ${String(DOC.journal_cap)} exec notes; ${String(DOC.journal_unread)} older are not read.` : ''}`);
    const undated = DOC?.undated || [];
    $('undated').hidden = !undated.length;
    put($('undated'), undated.length ? html`${undated.join(', ')} failed or ${undated.length === 1 ? 'was' : 'were'} interrupted with no readable log time, so ${undated.length === 1 ? 'it has' : 'they have'} no place on this timeline. Management lists every job's state.` : html``);
  }
  $('empty').addEventListener('click', e => { if (e.target.closest('[data-clear]')) clearAll(); });
  $('pager').addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c) return;
    if (c.dataset.p) page = +c.dataset.p;
    if (c.dataset.size) { size = +c.dataset.size; page = 1; }
    update();
  });
  tbody.addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

  // Saved views: each is a URL. The shell renders the chips and lists them in the palette; the chip that matches the URL is current.
  const VIEWS = [
    { name: 'Failed', params: { state: 'warning' } },
    { name: 'Last hour', params: { range: '1h' } },
    { name: 'Merges', params: { kind: 'merge' } },
    { name: 'Reviews', params: { kind: 'review' } },
    { name: 'Command journal', params: { kind: 'command', range: 'all' } },
    { name: 'Failed in the last hour', params: { state: 'warning', range: '1h' } },
  ];
  function update() {
    renderRange(); renderAnnunciator(); renderLanes(); renderFilters(); renderRows(); writeURL(); window.shell.views(VIEWS);
    // Selection follows the filter (design.md, Page contract): a hidden row gives way to the first visible one.
    const kept = window.shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, select: id => select(id, false), clear: unselect });
    if (kept !== undefined) selected = kept;
  }
  // Rows still shown with nothing selected means a reread after a write failed: the reader retired their objects.
  function unselect() {
    selected = null;
    put(details, html`<p class="why">${!DOC ? 'Activity was not read, so nothing is selected.' : EVENTS.some(match) ? 'The events were not read again after the change, so nothing is selected. Reload reads them.'
      : 'No event matches the filters, so nothing is selected.'}</p>`);
  }

  // ---------- Details ----------
  const details = $('details');
  const OUTPUT = {}; // build: an execution's output, read by command.output, shown under its facts
  function swap() { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); }
  const utc = iso => iso ? `${iso.slice(0, 16).replace('T', ' ')} UTC` : '—';
  function facts(e) {
    const f = { When: utc(e.at) };
    if (e.k === 'merge') Object.assign(f, { Repo: `${e.owner}/${e.repo}`, 'Pull request': e.ref, Item: e.item ? `sd:${e.item}` : '—', Commit: e.commit });
    // build: the design's Verdict and Summary, from the delivery note; When is the merge's, as the review's own is not recorded.
    if (e.k === 'review') Object.assign(f, { When: `${utc(e.at)} · the merge it let through`, Repo: `${e.owner}/${e.repo}`, 'Pull request': e.ref,
      Item: e.item ? `sd:${e.item}` : '—', 'Reviewed by': e.reviewers.join(', '), Requested: e.requested || 'no provider named', Verdict: e.verdict,
      'Reviewed head': e.head || 'not recorded' });
    if (e.k === 'run' && e.n) Object.assign(f, { Assignment: `#${e.n}`, Item: e.item ? `sd:${e.item}` : '—', Role: e.role, Provider: e.provider || '—', Status: e.status, Started: utc(e.started), Ended: utc(e.ended) });
    if (e.k === 'run' && e.job) Object.assign(f, { Job: e.job, 'Last run': e.rc, Detail: e.detail });
    if (e.k === 'command') Object.assign(f, { Note: String(e.note), Item: e.item ? `sd:${e.item} · ${e.title || ''}` : '—', Command: e.command, Scope: e.scope || 'not recorded',
      Started: utc(e.started), Ended: utc(e.ended), Exit: e.exit == null ? (e.ended ? 'none recorded' : 'still running') : String(e.exit), Session: e.who || '—',
      Output: e.source === 'runner' ? 'in the retained clone (a runner record)' : e.expired ? `expired ${e.expired}` : 'Show output reads it' });
    f.Source = e.src;
    return f;
  }
  function show(id) {
    const e = EVENTS.find(v => v.id === id); if (!e) return;
    tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === id)));
    document.querySelectorAll('#lanes rect.tick').forEach(r => r.classList.toggle('sel', r.dataset.id === id));
    const k = kindOf(e.k), out = OUTPUT[id];
    put(details, html`<p class="kind"><span class="g-${e.s}" aria-hidden="true">${GLYPH[e.s] || GLYPH.unknown}</span> ${k.name.replace(/s$/, '')} · ${e.s}</p>
      <h2>${e.what}</h2>
      <dl>${Object.entries(facts(e)).map(([a, b]) => html`<dt>${a}</dt><dd>${b}</dd>`)}</dl>
      <h3>Act</h3>${C.bar(id)}
      ${out ? html`<h3>Output</h3>${out.error ? html`<p class="why">${out.error}</p>` : html`<p class="why">${out.state}${out.expired ? ' · the prune removed the output file' : ''}</p><pre class="cli"><code>${out.output || '(no output)'}</code></pre>`}` : ''}
      ${e.k === 'run' && e.status === 'blocked' ? html`<p class="why">Blocked means the runner stopped and wants a decision. The reason is in the assignment log: <code>sd runner get ${String(e.n)}</code>.</p>` : ''}`);
    C.select(id);
    suggest([`Why did this ${e.k} happen?`, 'What else changed in this hour?', e.repo ? `Show all activity in ${e.repo}` : 'Rank what I should look at first']);
    swap();
  }
  function showKind(id) {
    const k = kindOf(id);
    put(details, html`<p class="kind"><span class="g-unknown" aria-hidden="true">▨</span> ${k.name} · unknown</p>
      <h2>No ${k.name.toLowerCase().replace(/s$/, '')} source</h2>
      <p class="why">${unknownWhy(id)} Unknown is not zero: ${k.name.toLowerCase()} may have happened; nothing this page can read records them.</p>`);
    window.shell.setContext?.(`${k.name}: no source`); openPane('tab-details'); swap();
  }
  function select(id, open) { selected = id; show(id); if (open) openPane('tab-details'); }

  // ---------- Commands (products/system/commands.md): declared once, rendered by the shell ----------
  // build: an event's object carries what its commands need from the document: the pull request, the assignment and its queue
  // revision, the job and its revision and retry capability, the execution record.
  const TYPE = { merge: 'pull request', review: 'review', command: 'command' };
  function objectOf(e) { return { ...e, id: e.id, type: e.k === 'run' ? (e.job ? 'job' : 'assignment') : TYPE[e.k], label: e.what, event: e.id }; }
  const ev = o => EVENTS.find(e => e.id === o.event) || o;
  // A command's run returns landing(...): the shell's run contract (shell.js, bulk:start) waits for the write, toasts its
  // text, and offers Undo only for what landed, for a bulk group too. A landed write rereads (the reader's write barrier); a
  // refused one loads the document again, so a retry sends the revision the read brought. Undo is spent once and reverses
  // this write only.
  function landing(promise, text, inverse) {
    return promise.then(v => {
      let spent = false;
      const undo = inverse && (() => { if (spent) return Promise.resolve(false); spent = true;
        return inverse(v).then(() => true, err => { if (err.stale) load(); throw err; }); });
      return { text: text(v), undo };
    }, err => { if (err.stale) load(); throw err; });
  }
  // The command's undo: the shell passes what the run answered.
  const undoOf = (o, r) => r && r.undo ? r.undo() : false;
  // build: "Open item" goes to the item on Tasks, by the shell's section map (no page names another page's address).
  const openItem = item => { const to = window.shell.pages?.Tasks; if (!to) return 'Tasks is not built yet'; location.href = `${to}?row=${item}`; return `sd:${item} opens in Tasks`; };
  const open = u => { window.open(u, '_blank', 'noopener'); return 'Opens in a new tab'; };
  const requeue = o => post(`/api/runner/${o.n}/requeue`, { revision: ev(o).revision }).then(out => reread().then(() => out));
  C.register(
    { id: 'pr.open', on: 'pull request', label: 'Open on GitHub', key: 'o', risk: 'safe', primary: () => true, cli: o => `gh pr view ${o.pr} --repo ${o.owner}/${o.repo} --web`, run: o => open(o.url) },
    { id: 'pr.item', on: 'pull request', label: 'Open item', key: 'i', risk: 'safe', when: o => !!o.item || 'the delivery names no work item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
    // build: the design's review.open; its review.lane re-runs a review, which a recorded one cannot ask for, so it is left out.
    { id: 'review.open', on: 'review', label: 'Open on GitHub', key: 'o', risk: 'safe', primary: () => true, cli: o => `gh pr view ${o.pr} --repo ${o.owner}/${o.repo} --web`, run: o => open(o.url) },
    { id: 'review.item', on: 'review', label: 'Open item', key: 'i', risk: 'safe', when: o => !!o.item || 'the delivery names no work item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
    // asg.requeue and jobs.retry are the one declarations in commands.md; Management, Reports, Today and Briefs declare them the same way.
    // build: requeue posts to /api/runner/<n>/requeue with the queue revision; Undo cancels the queued run with the revision it answered.
    { id: 'asg.requeue', on: 'assignment', label: 'Requeue', key: 'q', risk: 'undo', bulk: true, primary: o => ev(o).status === 'blocked',
      when: o => ev(o).status === 'blocked' || `the assignment is ${ev(o).status}`, cli: o => `sd runner requeue ${o.n}`,
      run: o => landing(requeue(o), () => `Requeued · #${o.n}. The runner starts it on its next tick.`, out => undoRequeue(o, out)),
      undo: undoOf },
    { id: 'asg.get', on: 'assignment', label: 'Show assignment', key: 'o', risk: 'safe', primary: o => ev(o).status !== 'blocked', cli: o => `sd runner get ${o.n}`,
      run: o => { select(o.id, true); return `Assignment #${o.n} shown in Details`; } },
    { id: 'asg.item', on: 'assignment', label: 'Open item', key: 'i', risk: 'safe', when: o => !!o.item || 'the assignment names no item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
    // Retry is `sd jobs retry`, which sends the kickstart (commands.md, one declaration, as Management declares it).
    // build: retry posts to /api/jobs/<job>/retry with the job's revision, as Operations > Jobs does; launchd starts the run.
    // Exit 127 is "command not found": a retry fails the same way until the path is fixed, so Retry is off, as in Management.
    { id: 'jobs.retry', on: 'job', label: 'Retry', key: 't', risk: 'safe', bulk: true, primary: o => !!ev(o).failed && ev(o).exit !== 127,
      when: o => !ev(o).failed ? 'no failed run to retry' : ev(o).exit === 127 ? 'exit 127: command not found; fix the path first'
        : ev(o).retry?.allowed || ev(o).retry?.reason || 'launchd refuses a retry now',
      cli: o => `sd jobs retry ${o.job}`, sends: o => `launchctl kickstart ${o.service}`,
      run: o => landing(post(`/api/jobs/${encodeURIComponent(o.job)}/retry`, { revision: ev(o).revision }).then(() => reread()), () => `Retry started · ${o.job}`) },
    // build: Management is not built, so the log is a line to copy.
    { id: 'jobs.log', on: 'job', label: 'Show log', key: 'l', risk: 'safe', executes: false, cli: o => `local-cron-jobs/cron-jobs.sh logs ${o.job}`, run: o => `Copy the line to read the log of ${o.job}` },
    // The journal (sd:2180). build: output reads /api/executions/<note>, as v1 Operations > Commands does.
    { id: 'command.output', on: 'command', label: 'Show output', key: 'o', risk: 'safe', primary: () => true,
      when: o => ev(o).source === 'runner' ? 'a runner record keeps its log in the retained clone, not in the execution log directory'
        : ev(o).expired ? `the output expired ${ev(o).expired}` : true,
      cli: o => `sd runner commands output ${o.note}`, run: o => landing(readOutput(o), () => `Output of note ${o.note} shown in Details`) },
    { id: 'command.item', on: 'command', label: 'Open item', key: 'i', risk: 'safe', when: o => !!o.item || 'the record names no item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
  );
  // Undo cancels the queued run with the revision the requeue answered, then reads the document again.
  const undoRequeue = (o, out) => post(`/api/runner/${o.n}/cancel`, { revision: out.revision }).then(() => reread());
  // read_execution answers at most 64 KiB a read (sd:2416): a full page means more follows at next_offset, up to the
  // 2 MiB it keeps of an output.
  const OUTPUT_PAGE = 65536, OUTPUT_MAX = 2 * 1024 * 1024;
  async function readOutput(o) {
    try {
      let offset = 0, text = '', out;
      for (;;) {
        out = await getJSON(`/api/executions/${o.note}?offset=${offset}`);
        text += out.output || '';
        const full = out.next_offset - offset === OUTPUT_PAGE;
        offset = out.next_offset;
        if (!full || offset >= OUTPUT_MAX) break;
      }
      OUTPUT[o.id] = { state: out.state, output: text, expired: !!out.output_expired };
    } catch (err) { OUTPUT[o.id] = { error: `The output was not read: ${err.message}` }; throw err; } finally { select(o.id, true); }
  }
  document.addEventListener('shell:open', e => { if (EVENTS.some(v => v.id === e.detail)) select(e.detail, true); });
  document.addEventListener('shell:picked', e => tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))));

  // ---------- Shapeshift bar ----------
  const input = $('shift'), prev = $('shift-preview'), as = $('shift-as'), ghost = $('ghost');
  let mode = 'filter';
  function guess(v) { return /\?$|^(why|what|how|which|when|should)\b/i.test(v) ? 'ask' : /^(p[1-4]\b|todo\b|add\b)|\bdue\b/i.test(v) ? 'task' : 'filter'; }
  function setMode(m) { mode = m; prev.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', String(c.dataset.as === m))); renderShift(); }
  function renderShift() {
    const v = input.value.trim();
    prev.hidden = !v; ghost.textContent = v ? `→ ${mode}` : '';
    put(as, mode === 'task' ? html`${ICON('list-todo')} task “${v}”` : mode === 'ask' ? html`${ICON('message-square')} ask chat in fleet scope` : html`${ICON('filter')} filter the timeline`);
    const q = mode === 'filter' ? v.toLowerCase() : '';
    if (q !== F.q) { F.q = q; page = 1; update(); }
  }
  input.addEventListener('input', () => setMode(guess(input.value.trim())));
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); const o = ['filter', 'task', 'ask']; setMode(o[(o.indexOf(mode) + 1) % 3]); }
    if (e.key === 'Enter' && input.value.trim()) {
      e.preventDefault();
      // build: a task goes to the shell's capture form, which files it; ask goes to chat.
      if (mode === 'task') { window.shell.capture(null, input.value.trim()); input.value = ''; renderShift(); }
      if (mode === 'ask') { window.shell.openChat(); window.shell.send(input.value.trim()); input.value = ''; renderShift(); }
    }
    if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; renderShift(); }
  });
  prev.addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) { setMode(c.dataset.as); input.focus(); } });
  document.addEventListener('keydown', e => {
    if (e.target.matches('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
    if (e.key === '/') { e.preventDefault(); input.focus(); }
  });
  // build: the shell owns j/k, Enter, Esc and ?row= through PAGE_LIST; Esc with nothing open clears the filters.
  window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected,
    select: id => select(id, false), clear: () => { if (!active()) return false; clearAll(); return true; } };

  readURL();
  if (F.q) input.value = F.q;
  load();
});
