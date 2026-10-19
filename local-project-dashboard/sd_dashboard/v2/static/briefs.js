// Briefs (sd:2112): the design source's products/system/designs/v2/briefs.js at b2b8c2b, ported. Each change from the reference
// is marked "build:". The rows are /api/briefs (briefs_screen.py): the routine briefs the workflow database holds (sd:3459), never
// sample data. Read opens a brief's item page, which renders its Markdown. The design reads brief mail; no mail reader exists, so
// unread state, follow-up flags and the watchdog's failures are left out, and the Missed lamp is unknown with the document's
// reason. The one write is Make task, which opens the shell's capture form; the form files it with POST /api/items.
const { html, put, plural, cells } = window.markup;
// build: nothing here can be loud. The watchdog is not read, so the page claims nothing for the rail or Today.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'watchdog not read' };
window.PAGE_COMMANDS = [];
// sd:5080: the list and the cadence strip are the shell's views (shell.js, view:start). The Range chips sit in each view's
// head and set one range for both; a tick or a lane shows the list with that brief or that source.
window.PAGE_VIEWS = [{ id: 'list', label: 'Briefs', key: 'b' }, { id: 'cadence', label: 'Cadence' }];
window.PAGE_KEYS = [['/', 'Filter briefs, capture a task or ask'], ['→', 'In the field: the next reading (filter, task, ask)']];
addEventListener('DOMContentLoaded', () => {
  const shell = window.shell, C = shell.commands;
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
  // build: capture files through the shell's default capture (sd:3520): POST /api/items, as Today and Tasks do.

  // ---------- Data (build) ----------
  // BRIEFS: { id (the stem), item, subj, src, day, at (null when not known), words, lead, rel, open, copy }. rel and open are
  // null unless the last render vouches for the vault copy, and copy says why. READ: when the server read them.
  let BRIEFS = [], SOURCES = [], READ = null, READER = null, WATCH = '', COPIES = null, LOADED = false;
  const DAY_MS = 864e5;
  const RANGES = { '24h': 1, '7d': 7, '30d': 30 };
  // build: the reference had one fixed week of sample mail; the range here is the last 1, 7 or 30 days before the read.
  let range = '7d';
  const days = () => { const end = Date.parse((READ || new Date().toISOString()).slice(0, 10) + 'T00:00:00Z');
    return Array.from({ length: RANGES[range] }, (_, i) => new Date(end - (RANGES[range] - 1 - i) * DAY_MS).toISOString().slice(0, 10)); };
  let DAYS = [];
  const inRange = b => b.day >= DAYS[0] && b.day <= DAYS[DAYS.length - 1];
  const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'], MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const dayLabel = d => { const t = new Date(d + 'T12:00:00Z'); return `${WD[t.getUTCDay()]} ${t.getUTCDate()} ${MON[t.getUTCMonth()]}`; };
  const hhmm = iso => iso ? iso.slice(11, 16) : '–';
  const srcOf = id => SOURCES.find(s => s.id === id);
  const ago = iso => { const m = Math.round((Date.parse(READ) - Date.parse(iso)) / 60000); return m < 60 ? `${Math.max(m, 0)}m ago` : m < 2880 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`; };

  // ---------- State (mirrored in the URL) ----------
  // build: no kind or follow-up filter; no reader gives either.
  const F = { src: new Set(), day: '', q: '' };
  let page = 1, size = 50, selected = null;
  function readURL() {
    const p = new URLSearchParams(location.search);
    // A source is the free end of a file name and may hold a comma, so each one is its own `src` (review 39d6c524a35c).
    p.getAll('src').filter(Boolean).forEach(v => F.src.add(v));
    F.day = /^\d{4}-\d{2}-\d{2}$/.test(p.get('day') || '') ? p.get('day') : ''; F.q = p.get('q') || '';
    range = RANGES[p.get('range')] ? p.get('range') : '7d';
    page = Math.max(1, Math.floor(+p.get('page')) || 1); size = [25, 50, 100, 200].includes(+p.get('size')) ? +p.get('size') : 50;
  }
  function writeURL() {
    const q = [...F.src].map(s => ['src', s]);
    if (F.day) q.push(['day', F.day]);
    if (F.q) q.push(['q', F.q]);
    if (range !== '7d') q.push(['range', range]);
    if (page > 1) q.push(['page', String(page)]);
    if (size !== 50) q.push(['size', String(size)]);
    shell.url(q); // pairs, so a source repeats; the shell keeps ?row=
  }
  const active = () => F.src.size + (F.day ? 1 : 0) + (F.q ? 1 : 0);
  const match = b => inRange(b) && (!F.src.size || F.src.has(b.src)) && (!F.day || b.day === F.day) &&
    (!F.q || `${b.subj} ${b.src} ${b.lead}`.toLowerCase().includes(F.q.toLowerCase()));
  const ranged = () => BRIEFS.filter(inRange);

  // ---------- Annunciator ----------
  // build: three lamps. The reference's follow-up, quiet-days and Slack lamps read mail and the watchdog's digest; no reader
  // supplies either here. Missed is unknown with the reason, never a count.
  // A capped read holds the newest rows only, so a count reaching past its oldest day is a floor, not a total.
  // The reader says capped only when it cut rows; a skipped row is not a cut, so total > shown does not decide it.
  const capped = () => !!READER && !!READER.capped;
  const oldest = () => BRIEFS.reduce((m, b) => b.day < m ? b.day : m, '9999-12-31');
  const LAMP = { ok: 'ok', off: 'ok', failed: 'caution', unknown: 'unknown' };
  // sd:5298: a failed or unknown copies lamp links to its evidence, Health's Brief copies row (the refused stems, the reason).
  const COPIES_ROW = '/health?row=mir%3Abriefs#view=jobs';
  function renderAnnunciator() {
    const c = COPIES || { state: 'unknown', reason: 'not read', at: null };
    const n = ranged().length, srcs = new Set(ranged().map(b => b.src)).size;
    // No read behind the lamp is unknown, never ok: a refused or failed read is not zero briefs.
    const got = !!READER && READER.state !== 'error', floor = capped() && BRIEFS.length && oldest() >= DAYS[0];
    const count = got ? html`<span class="ph">${floor ? 'at least ' : ''}<b>${n}</b> in ${range === '24h' ? '24 hours' : `${RANGES[range]} days`}</span> · <span class="ph">${plural(srcs, 'source')}</span>` : html`<span class="ph"><b>not read</b></span>`;
    const read = READ ? html`<span class="ph"><b>${hhmm(READ)}</b> UTC</span> · <span class="ph">${dayLabel(READ.slice(0, 10))}</span>` : html`<span class="ph"><b>not yet</b></span>`;
    put(document.getElementById('annunciator'), cells([
      { state: got ? 'ok' : 'unknown', label: 'Briefs', mark: I('inbox'), val: count },
      { state: 'unknown', attrs: html` title="${WATCH}"`, label: 'Missed', mark: I('siren'), val: html`<span class="ph"><b>unknown</b></span> · <span class="ph">no watchdog reader</span>` },
      // build (sd:3459): the brief-copies heartbeat. Off is quiet; failed is caution, since Read never needs a copy.
      { state: LAMP[c.state] || 'unknown', href: LAMP[c.state] === 'ok' ? undefined : COPIES_ROW, attrs: html` title="${c.reason || `rendered ${c.at}`}"`, label: 'Vault copies', mark: I('hard-drive-download'),
        val: html`<span class="ph"><b>${c.state}</b></span>${c.at ? html` · <span class="ph">${c.at.slice(5, 16).replace('T', ' ')} UTC</span>` : ''}` },
      { button: true, attrs: html` id="refresh"`, label: 'Observed', mark: I('rotate-ccw'), val: read },
    ]));
  }
  document.getElementById('annunciator').addEventListener('click', e => { if (e.target.closest('#refresh')) briefs.load(); });

  // ---------- Cadence strip (hand-rolled SVG, patterns.md § Charts) ----------
  const W = 700, H = 24;
  // build: a tick sits at its time; a brief with no known time sits at noon of its day.
  const x = b => { const t0 = Date.parse(DAYS[0] + 'T00:00:00Z'), span = DAYS.length * DAY_MS;
    return ((Date.parse(b.at || b.day + 'T12:00:00Z') - t0) / span) * W; };
  const RANGE_HOSTS = () => ['range', 'range-cad'].map(id => document.getElementById(id));
  function renderRange() {
    RANGE_HOSTS().forEach(el => put(el, html`${Object.keys(RANGES).map(r => html`<button class="chip" type="button" data-r="${r}" aria-pressed="${String(r === range)}">${r}</button>`)}`));
  }
  // 7d, not 24h, is this view's default: most sources write once a day, so 24h shows one tick per lane and no cadence.
  RANGE_HOSTS().forEach(el => el.addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c || !RANGES[c.dataset.r]) return;
    range = c.dataset.r; DAYS = days(); if (F.day && !DAYS.includes(F.day)) F.day = ''; page = 1; renderRange(); update();
  }));
  function renderLanes() {
    const step = W / DAYS.length, label = DAYS.length <= 7 ? 1 : 5;
    const grid = html`${DAYS.map((d, i) => html`<line x1="${(i * step).toFixed(1)}" x2="${(i * step).toFixed(1)}" y1="0" y2="${H}"/>`)}<line x1="${W}" x2="${W}" y1="0" y2="${H}"/>`;
    const lanes = SOURCES.map(s => {
      const mine = ranged().filter(b => b.src === s.id);
      const btn = html`<button class="lane-btn" type="button" data-src="${s.id}" aria-pressed="${String(F.src.has(s.id))}"><span class="nm">${s.id}</span><span class="n">${mine.length}</span></button>`;
      const ticks = mine.map(b => html`<rect class="tick${b.id === selected ? ' sel' : ''}" data-id="${b.id}" x="${(x(b) - 1.5).toFixed(1)}" y="4" width="3" height="${H - 8}"><title>${b.subj} · ${b.day} ${b.at ? hhmm(b.at) + 'Z' : 'time not known'}</title></rect>`);
      return html`<div class="lane">${btn}<div class="track"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${s.id}: ${plural(mine.length, 'brief')} in ${DAYS.length} days"><g class="grid">${grid}</g>${ticks}</svg></div></div>`;
    });
    const axis = html`<div class="lane"><span class="axis axis-label"></span><div class="axis"><svg viewBox="0 0 ${W} 16" preserveAspectRatio="none" aria-hidden="true">${DAYS.map((d, i) => i % label ? '' : html`<text x="${(i * step + 4).toFixed(1)}" y="12">${d.slice(8)}</text>`)}</svg></div></div>`;
    put(document.getElementById('lanes'), html`${lanes}${axis}`);
    // Table fallback: counts per source per day.
    put(document.getElementById('cad-table'), html`<table><caption class="sr">Briefs per source and day</caption><thead><tr><th scope="col">Source</th>${DAYS.map(d => html`<th scope="col">${d.slice(5)}</th>`)}<th scope="col">Total</th></tr></thead><tbody>${SOURCES.map(s => html`<tr><td>${s.id}</td>${DAYS.map(d => html`<td>${BRIEFS.filter(b => b.src === s.id && b.day === d).length}</td>`)}<td>${ranged().filter(b => b.src === s.id).length}</td></tr>`)}</tbody></table>`);
  }
  document.getElementById('lanes').addEventListener('click', e => {
    const t = e.target.closest('rect.tick'); if (t) { shell.view.show('list'); return select(t.dataset.id, true); }
    const b = e.target.closest('.lane-btn'); if (!b) return;
    const s = b.dataset.src;
    F.src.has(s) ? F.src.delete(s) : F.src.add(s); page = 1; update(); shell.view.show('list');
    if (F.src.has(s)) showSource(s);
  });

  // ---------- Filters ----------
  // build (sd:5158): Source is a dropdown of checkboxes, so any sources at once, and Day a select of the days with a brief;
  // both are shell.list.pick. The picked values show as removable chips (shell.list.chips) with the count and Clear all.
  const activeFilters = () => [...[...F.src].map(s => ({ key: `src:${s}`, label: `Source: ${s}` })),
    ...(F.day ? [{ key: 'day', label: `Day: ${dayLabel(F.day)}` }] : []), ...(F.q ? [{ key: 'q', label: `Text: ${F.q}` }] : [])];
  function renderFilters() {
    const L = shell.list, all = ranged(), shown = all.filter(match).length;
    const busy = DAYS.slice().reverse().filter(d => all.some(b => b.day === d));
    put(document.getElementById('filters'), html`${L.pick('src', 'Source', SOURCES.map(s => [s.id, s.id, all.filter(b => b.src === s.id).length]), F.src, { multi: true })}${
      L.pick('day', 'Day', busy.map(d => [d, dayLabel(d), all.filter(b => b.day === d).length]), F.day, { all: 'Any day' })}${
      active() ? L.chips(activeFilters(), shown, all.length) : html`<div class="fsum"><span>${shown} of ${all.length}</span></div>`}`);
  }
  document.getElementById('filters').addEventListener('change', e => shell.list.pickChange(e, F, () => { page = 1; update(); }));
  document.getElementById('filters').addEventListener('click', e => {
    if (e.target.closest('[data-unfilter-all]')) return clearAll();
    const u = e.target.closest('[data-unfilter]'); if (!u) return;
    const [k, v] = u.dataset.unfilter.split(/:(.*)/);
    if (k === 'src') F.src.delete(v);
    if (k === 'day') F.day = '';
    if (k === 'q') { F.q = ''; input.value = ''; prev.hidden = true; ghost.textContent = ''; }
    page = 1; update();
  });
  function clearAll() { F.src.clear(); F.day = ''; F.q = ''; input.value = ''; prev.hidden = true; ghost.textContent = ''; page = 1; update(); }

  // ---------- Ledger ----------
  const tbody = document.getElementById('rows');
  function renderRows() {
    const all = BRIEFS.filter(match);
    const pages = Math.max(1, Math.ceil(all.length / size)); page = Math.min(page, pages);
    const rows = all.slice((page - 1) * size, page * size);
    let lastDay = '';
    // build: no unread dot, bold or ▲: no reader says which brief is unread or wants a follow-up.
    put(tbody, html`${rows.map(b => {
      const head = b.day !== lastDay ? html`<tr class="day" aria-hidden="true"><td colspan="5">${dayLabel(b.day)}</td></tr>` : ''; lastDay = b.day;
      return html`${head}<tr data-id="${b.id}" aria-selected="${String(b.id === selected)}">
        <td class="g"><span class="sr">brief</span></td>
        <td class="what"><button type="button">${b.subj}<small>${b.lead}</small></button></td>
        <td class="src">${b.src}</td>
        <td class="at">${b.at ? html`<time datetime="${b.at}" title="${ago(b.at)}">${b.day.slice(5)} ${hhmm(b.at)}</time>` : html`<span title="time not known: the note changed after its day">${b.day.slice(5)} –</span>`}</td>
        <td class="act">${window.commands.published(b.pub)}${C.rowActions(b.id)}</td></tr>`;
    })}`);
    const empty = document.getElementById('empty');
    empty.hidden = !LOADED || !!all.length;
    // One delegated listener: an onclick= attribute in markup is inline script under the dashboard CSP.
    empty.onclick ??= e => { if (e.target.closest('[data-act="clear"]')) clearAll(); };
    put(empty, html`${all.length || !LOADED ? html`` : active() ? html`No brief matches these filters.${F.q ? ' Press → in the search box to capture it as a task instead.' : ''} <button class="linkbtn" type="button" data-act="clear">Clear all</button>` : html`No brief in this range.`}`);
    const a = all.length ? (page - 1) * size + 1 : 0, z = Math.min(page * size, all.length);
    put(document.getElementById('pager'), html`<span>${a}–${z} of ${all.length}</span>
      <div class="chips" role="group" aria-label="Page">${page > 1 ? html`<button class="chip" type="button" data-p="${page - 1}">${I('chevron-left')}Previous</button>` : ''}${page < pages ? html`<button class="chip" type="button" data-p="${page + 1}">Next${I('chevron-right')}</button>` : ''}</div>
      <div class="chips" role="group" aria-label="Rows per page">${[25, 50, 100, 200].map(n => html`<button class="chip" type="button" data-size="${n}" aria-pressed="${String(n === size)}">${n}</button>`)}</div>`);
    put(document.getElementById('tally'), html`<span>${plural(new Set(ranged().map(b => b.src)).size, 'source')}</span><span>${all.length} shown</span>`);
    document.getElementById('sub').textContent = READER ? (capped() ? `newest ${plural(READER.shown, 'brief')} read` : `${plural(READER.shown, 'brief')}${READER.skipped ? `, ${READER.skipped} skipped` : ''}`) : 'Reading the briefs…';
  }
  document.getElementById('pager').addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c) return;
    if (c.dataset.p) page = +c.dataset.p;
    if (c.dataset.size) { size = +c.dataset.size; page = 1; }
    update();
  });
  tbody.addEventListener('click', e => { if (e.target.closest('.rowact')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

  function update() { renderAnnunciator(); renderLanes(); renderFilters(); renderRows(); writeURL(); }

  // ---------- Details ----------
  const details = document.getElementById('details');
  function swap() { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); }
  function show(id) {
    const b = BRIEFS.find(r => r.id === id); if (!b) return false;
    shownItem = b.item;
    tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id));
    document.querySelectorAll('#lanes rect.tick').forEach(r => r.classList.toggle('sel', r.dataset.id === id));
    put(details, html`<p class="kind">Brief · ${b.src}</p>
      <h2>${b.subj}</h2>
      <dl><dt>Source</dt><dd>${b.src}</dd><dt>Day</dt><dd>${dayLabel(b.day)}</dd>
        <dt>Written</dt><dd>${b.at ? `${b.at.slice(0, 16).replace('T', ' ')} UTC · ${ago(b.at)}` : 'time not known: the note changed after its day'}</dd>
        <dt>Words</dt><dd>${b.words}</dd><dt>Item</dt><dd>#${b.item}</dd>
        <dt>Vault copy</dt><dd>${b.rel ? html`<code>${b.rel}</code>` : b.copy}</dd>
        <dt>Watchdog</dt><dd><span class="g-unknown" aria-hidden="true">▨</span> ${WATCH}</dd></dl>
      <h3>Body · first lines</h3><div class="mailbody">${b.lead}${b.lead.length >= 200 ? '…' : ''}</div>
      <p class="why off">First lines only. Read opens the whole brief.</p>
      <h3>Act</h3>${C.bar(id)}
      ${window.commands.publishPane('brief', { id: b.id, label: b.subj, item: b.item, pub: b.pub })}
      ${window.commands.reviewPane('brief', b.item)}
      ${b.src === REVIEW_SRC ? html`<section class="recs" id="recs" aria-live="polite"><h3>Create tasks</h3><p class="why off">Reading its recommendations…</p></section>` : ''}`);
    if (b.src === REVIEW_SRC) loadRecs(b.item);
    C.select(id);
    shell.suggest(['Summarise this brief in three lines', `What else did ${b.src} write this week?`, 'Turn what this brief asks into tasks']);
    swap();
    return true;
  }
  // build: a source has no Act bar. The reference's Retry and Show log ran on the job behind the source; the file name gives a
  // kind, not a job, so there is nothing to run them on.
  function showSource(id) {
    const s = srcOf(id); if (!s) return;
    shownItem = null;
    const mine = ranged().filter(b => b.src === id), on = new Set(mine.map(b => b.day)).size;
    put(details, html`<p class="kind">Source · ${mine.length ? 'quiet days are gaps' : 'no brief in range'}</p>
      <h2>${s.id}</h2>
      <dl><dt>Briefs, ${DAYS.length} days</dt><dd>${mine.length} on ${on} of ${DAYS.length} days</dd><dt>Newest</dt><dd>${s.newest}</dd>
        <dt>All read</dt><dd>${s.n}${capped() ? ` of the newest ${READER.shown}` : ''}</dd><dt>Watchdog</dt><dd><span class="g-unknown" aria-hidden="true">▨</span> ${WATCH}</dd></dl>
      <p class="why">A quiet day is a gap, not a fault. Without a watchdog reader the page cannot say whether the job ran and had nothing to write, or failed. The file name gives the kind, not the job, so there is no job to retry from here.</p>`);
    C.select(null); shell.suggest([`Why is ${s.id} quiet?`, 'Which jobs missed a run this week?']); shell.openPane('tab-details'); swap();
  }
  function select(id, open) { if (!show(id)) return; selected = id; if (open) shell.openPane('tab-details'); }

  // ---------- Create tasks (sd:5064) ----------
  // An adversarial review's brief names its review batches; each question is one recommendation, read from
  // /api/briefs/<item>/recommendations. Create selected rules each ticked question Approve through POST /api/review/rule,
  // one call at a time under its batch's revision, and Approve files the task. Reject the rest rules every open one
  // Reject. An unticked question stays open; a refused call keeps the server's sentence on its own row. Details names the
  // brief it shows in shownItem; an answer or a click for another brief changes nothing (shell.shown).
  const REVIEW_SRC = 'Adversarial review';
  let RECS = null, busy = false, shownItem = null;
  const CHOSEN = new Set(), REFUSED = new Map();
  function loadRecs(item) {
    const still = shell.shown('recs', item, () => shownItem); if (!still) return Promise.resolve();
    return shell.getJSON(`/api/briefs/${item}/recommendations`).then(doc => doc, e => ({ brief: item, error: e.message }))
      .then(doc => { if (!still()) return; if (RECS?.brief !== item) { CHOSEN.clear(); REFUSED.clear(); } RECS = doc; drawRecs(); });
  }
  function drawRecs() {
    const host = document.getElementById('recs'); if (!host || !RECS || RECS.brief !== shownItem) return;
    if (RECS.error) { put(host, html`<h3>Create tasks</h3><p class="why">Its recommendations were not read: ${RECS.error}</p>`); return; }
    const open = RECS.questions.filter(q => q.open);
    put(host, html`<h3>Create tasks</h3>
      <p class="why">Each recommendation waits in a review batch. Create selected approves the ticked ones, which files each as a task; the rest stay open.</p>
      ${RECS.batches.filter(b => b.error).map(b => html`<p class="why">sd:${b.item} was not read: ${b.error}</p>`)}
      ${RECS.questions.length ? html`<ul class="rec-list">${RECS.questions.map(q => html`<li>${q.open
        ? html`<label class="chk"><input type="checkbox" data-q="${q.note}"${CHOSEN.has(q.note) ? html` checked` : ''}${busy ? html` disabled` : ''}> <span>${q.title}</span></label>`
        : html`<span class="ruled"><span class="lab">${q.label || 'expired'}</span> <span>${q.title}</span>${q.filed ? html` · <a href="${shell.itemHref(q.filed)}">sd:${q.filed}</a>` : ''}</span>`}
        <small>${q.repo || 'no repository'} · batch sd:${q.batch}</small>${REFUSED.has(q.note) ? html`<small class="refused">${REFUSED.get(q.note)}</small>` : ''}</li>`)}</ul>`
        : html`<p class="why off">This review filed no recommendation.</p>`}
      ${open.length ? html`<div class="actions"><button class="linkbtn" type="button" data-recs="all">Select all</button>
        <button class="btn sm" type="button" data-recs="create"${busy ? html` disabled` : ''}>Create selected</button>
        <button class="btn sm quiet" type="button" data-recs="reject"${busy ? html` disabled` : ''}>Reject the rest</button></div>` : ''}`);
  }
  async function ruleEach(option, picked) {
    const doc = RECS, revision = new Map(doc.batches.map(b => [b.item, b.revision]));
    if (!picked.length) { shell.toast('Tick a recommendation first'); return; }
    busy = true; REFUSED.clear(); drawRecs();
    let failed = 0;
    for (const q of picked) {
      try {
        const out = await shell.post('/api/review/rule', { item: q.batch, note: q.note, option, revision: revision.get(q.batch) });
        revision.set(q.batch, out.revision); CHOSEN.delete(q.note);
      } catch (e) { failed++; REFUSED.set(q.note, e.message); }
    }
    busy = false; drawRecs();
    shell.toast(`${picked.length - failed} of ${picked.length} ruled ${option}${failed ? `; ${failed} failed, each row says why` : ''}`);
    await loadRecs(doc.brief);
  }
  details.addEventListener('change', e => {
    const box = e.target.closest?.('input[data-q]'); if (!box) return;
    if (box.checked) CHOSEN.add(+box.dataset.q); else CHOSEN.delete(+box.dataset.q);
  });
  details.addEventListener('click', e => {
    const b = e.target.closest?.('[data-recs]'); if (!b || busy || !RECS?.questions || RECS.brief !== shownItem) return;
    const open = RECS.questions.filter(q => q.open);
    if (b.dataset.recs === 'all') { open.forEach(q => CHOSEN.add(q.note)); drawRecs(); }
    if (b.dataset.recs === 'create') ruleEach('Approve', open.filter(q => CHOSEN.has(q.note)));
    if (b.dataset.recs === 'reject') ruleEach('Reject', open.filter(q => !CHOSEN.has(q.note)));
  });
  document.addEventListener('publish:run', e => { if (selected === e.detail.key) show(selected); });
  document.addEventListener('adversarial:run', e => { const b = BRIEFS.find(r => r.id === selected); if (b && e.detail.key === `brief:${b.item}`) show(b.id); });

  // ---------- Commands (products/system/commands.md): declared once, rendered by the shell ----------
  // build: Read, then two of the reference's four brief commands. Acknowledge and Mark read wrote a message store that does not
  // exist and had nothing to change here; Open in Gmail is Open in Obsidian, the vault copy's link, off without one (sd:3459).
  function registerCommands() {
    C.register(
      // build (sd:3459): every brief is a row in the database, so Read works with or without a vault copy.
      { id: 'brief.read', on: 'brief', label: 'Read', does: 'Opens the brief on its work item\'s page.', key: 'r', risk: 'safe', primary: () => true, executes: false,
        cli: o => `sd reports brief show ${shq(o.id)}`, run: o => { location.href = shell.itemPage(o.item); return `#${o.item} opens`; } },
      // build: no verb deletes a task, so no Undo: the capture form shows the line and files it only on Capture.
      { id: 'brief.task', on: 'brief', label: 'Make task', does: 'Opens Capture with the brief\'s title, to file it as a task.', key: 'k', risk: 'safe',
        cli: o => `sd task add ${shq(o.label)}`, run: o => { shell.capture(o, o.label); return 'Check the title, then Capture'; } },
      { id: 'brief.open', on: 'brief', label: 'Open in Obsidian', does: 'Opens the brief\'s vault copy in Obsidian.', key: 'o', risk: 'safe', executes: false,
        when: o => !!o.open || o.copy || 'no vault copy for this brief', cli: o => `open ${shq(o.open)}`,
        run: o => { window.open(o.open, '_blank', 'noopener'); return 'Obsidian opens the copy'; } },
      // sd:5064: a codex review of the brief's item, in the background; its own brief lists the tasks to pick.
      window.commands.adversarialReview('brief', { payload: o => ({ kind: 'brief', target: o.item }) }),
      // sd:5102: Publish to (the item's fields.publish) and Publish now, declared once in commands.js.
      ...window.commands.publishCommands('brief', { base: '/api/briefs', payload: o => ({ item: o.item }), reread: () => briefs.reread(),
        cli: () => 'no sd verb: the dashboard writes the brief\'s fields.publish and queues it through sd-research-kit' }),
    );
  }
  document.addEventListener('shell:open', e => { if (BRIEFS.some(b => b.id === e.detail)) select(e.detail, true); });

  // ---------- Shapeshift bar ----------
  const input = document.getElementById('shift'), prev = document.getElementById('shift-preview'), as = document.getElementById('shift-as'), ghost = document.getElementById('ghost');
  let mode = 'filter';
  function guess(v) { return /\?$|^(why|what|how|which|when|should)\b/i.test(v) ? 'ask' : /^(p[1-4]\b|todo\b|add\b)|\bdue\b/i.test(v) ? 'task' : 'filter'; }
  function setMode(m) { mode = m; prev.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', c.dataset.as === m)); renderShift(); }
  function renderShift() {
    const v = input.value.trim();
    prev.hidden = !v; ghost.textContent = v ? `→ ${mode}` : '';
    put(as, html`${mode === 'task' ? html`${I('list-todo')} task “${v}”` : mode === 'ask' ? html`${I('message-square')} ask chat about briefs` : html`${I('filter')} filter subject, source and first lines`}`);
    const q = mode === 'filter' ? v.toLowerCase() : '';
    if (q !== F.q) { F.q = q; page = 1; update(); }
  }
  input.addEventListener('input', () => setMode(guess(input.value.trim())));
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); const o = ['filter', 'task', 'ask']; setMode(o[(o.indexOf(mode) + 1) % 3]); }
    if (e.key === 'Enter' && input.value.trim()) {
      e.preventDefault();
      // build: a task opens the capture form with the text; the form files it (the shell's fileCapture).
      if (mode === 'task') { const v = input.value.trim(); input.value = ''; renderShift(); shell.capture(null, v); }
      if (mode === 'ask') { shell.openChat(); shell.send(input.value.trim()); input.value = ''; renderShift(); }
    }
    if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; renderShift(); }
  });
  prev.addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) { setMode(c.dataset.as); input.focus(); } });
  document.addEventListener('keydown', e => {
    if (e.target.matches?.('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
    if (e.key === '/') { e.preventDefault(); input.focus(); }
  });
  // The shell walks these rows on j / k and clears the filter on Esc.
  window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false), clear: () => !!active() && (clearAll(), true) };

  // ---------- Start (build: read /api/briefs through shell.read, then draw) ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping Observed clicks only the newest read draws, a brief the
  // read no longer lists runs no command, and a selection whose brief is gone moves to the first shown brief or clears.
  const briefs = shell.read({
    source: '/api/briefs', what: 'the briefs',
    adopt: doc => {
      READ = doc.read; READER = doc.reader || { state: 'error', reason: 'no reader in the answer', total: 0, shown: 0 };
      WATCH = doc.watchdog?.available ? 'read' : (doc.watchdog?.reason || 'no watchdog reader');
      COPIES = READER.copies || null;
      BRIEFS = READER.state === 'error' ? [] : (doc.briefs || []);
      const by = new Map();
      BRIEFS.forEach(b => { const s = by.get(b.src) || { id: b.src, n: 0, newest: b.day }; s.n++; if (b.day > s.newest) s.newest = b.day; by.set(b.src, s); });
      // build: lanes in name order; the reference's lane order was a hand-made list of its sample jobs.
      SOURCES = [...by.values()].sort((a, b) => a.id.localeCompare(b.id));
      [...F.src].forEach(s => { if (!by.has(s)) F.src.delete(s); });
      DAYS = days(); LOADED = true;
      const source = READER.source || '/api/briefs';
      const state = READER.state === 'error' ? { kind: 'error', text: `The briefs were not read: ${READER.reason}. Reload retries it.`, source }
        : !BRIEFS.length ? { kind: 'empty', title: 'No briefs', text: 'The workflow database holds no routine brief.', source }
        : READER.skipped || capped() ? { kind: 'partial', text: [READER.skipped ? `${plural(READER.skipped, 'row')} skipped: not a brief.` : '', capped() ? `Showing the newest ${READER.shown}; older briefs are in the database. Counts before ${oldest().slice(5)} are not read.` : ''].filter(Boolean).join(' '), source }
        : null;
      return { objects: BRIEFS.map(b => ({ id: b.id, type: 'brief', label: b.subj, src: b.src, item: b.item, open: b.open, copy: b.copy, pub: b.pub || null })), state };
    },
    // Nothing from the last read stays on screen: rows, lanes and the observed time.
    clear: err => { READ = null; READER = { state: 'error', reason: err.message, total: 0, shown: 0 }; COPIES = null; BRIEFS = []; SOURCES = []; F.src.clear(); LOADED = true; },
    draw: () => { renderRange(); update(); },
    current: () => selected,
    first: () => (BRIEFS.filter(match)[0] || BRIEFS[0])?.id,
    select,
    // No current brief: nothing from an earlier read stays selected, and Details offers no command on it.
    unselect: () => { selected = null; put(details, html`<p class="why">Select a brief to see it.</p>`); },
  });
  readURL();
  if (F.q) input.value = F.q;
  registerCommands();
  briefs.load();
});
