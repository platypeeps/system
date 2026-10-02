// Briefs (sd:2112): the design source's products/system/designs/v2/briefs.js at b2b8c2b, ported. Each change from the reference
// is marked "build:". The rows are /api/briefs (briefs_screen.py): the brief notes the vault's System/AI Generated/Briefs folder
// holds, never sample data. The design reads brief mail; no mail reader exists, so unread state, follow-up flags and the
// watchdog's failures are left out, and the Missed lamp is unknown with the document's reason. The one write is Make task, which
// opens the shell's capture form; the form files it with POST /api/items.
const { html, put, plural } = window.markup;
// build: nothing here can be loud. The watchdog is not read, so the page claims nothing for the rail or Today.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'watchdog not read' };
window.PAGE_COMMANDS = [];
window.PAGE_KEYS = [['/', 'Filter briefs, capture a task or ask'], ['→', 'In the field: the next reading (filter, task, ask)']];
addEventListener('DOMContentLoaded', () => {
  const shell = window.shell, C = shell.commands;
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
  const csrf = () => document.querySelector('meta[name="sd-csrf"]')?.content || '';
  // build: capture files through the route and library call Today and Tasks use (POST /api/items, sd task add).
  window.SHELL_CAPTURE = async ({ kind, title, item }) => {
    if (kind === 'note') throw new Error('Not filed: a brief has no item to note. File a task or a followup.');
    const body = { title, ...(kind === 'followup' ? { kind: 'followup', ...(item ? { followup_of: item } : {}) } : {}) };
    const r = await fetch('/api/items', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-SD-CSRF': csrf() }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(`Not filed: ${out.error || r.status}`);
    return `Captured #${out.item?.id}: ${out.item?.title || title}`;
  };

  // ---------- Data (build) ----------
  // BRIEFS: { id, subj, src, day, at (null when not known), words, lead, rel, open }. READ: when the server read them.
  let BRIEFS = [], SOURCES = [], READ = null, READER = null, WATCH = '', LOADED = false;
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
  async function getJSON(path) {
    const r = await fetch(path, { headers: { Accept: 'application/json' } });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
    return out;
  }

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
  const capped = () => !!READER && READER.total > READER.shown;
  const oldest = () => BRIEFS.reduce((m, b) => b.day < m ? b.day : m, '9999-12-31');
  function renderAnnunciator() {
    const n = ranged().length, srcs = new Set(ranged().map(b => b.src)).size;
    // No read behind the lamp is unknown, never ok: a refused or failed read is not zero briefs.
    const got = !!READER && READER.state !== 'error', floor = capped() && BRIEFS.length && oldest() >= DAYS[0];
    const count = got ? html`<span class="ph">${floor ? 'at least ' : ''}<b>${n}</b> in ${range === '24h' ? '24 hours' : `${RANGES[range]} days`}</span> · <span class="ph">${plural(srcs, 'source')}</span>` : html`<span class="ph"><b>not read</b></span>`;
    const read = READ ? html`<span class="ph"><b>${hhmm(READ)}</b> UTC</span> · <span class="ph">${dayLabel(READ.slice(0, 10))}</span>` : html`<span class="ph"><b>not yet</b></span>`;
    put(document.getElementById('annunciator'), html`
      <li><div class="cell" data-state="${got ? 'ok' : 'unknown'}"><span class="lbl">Briefs${I('inbox')}</span><span class="val">${count}</span></div></li>
      <li><div class="cell" data-state="unknown" title="${WATCH}"><span class="lbl">Missed${I('siren')}</span><span class="val"><span class="ph"><b>unknown</b></span> · <span class="ph">no watchdog reader</span></span></div></li>
      <li><button class="cell" type="button" id="refresh"><span class="lbl">Observed${I('rotate-ccw')}</span><span class="val">${read}</span></button></li>`);
  }
  document.getElementById('annunciator').addEventListener('click', e => { if (e.target.closest('#refresh')) load(); });

  // ---------- Cadence strip (hand-rolled SVG, patterns.md § Charts) ----------
  const W = 700, H = 24;
  // build: a tick sits at its time; a brief with no known time sits at noon of its day.
  const x = b => { const t0 = Date.parse(DAYS[0] + 'T00:00:00Z'), span = DAYS.length * DAY_MS;
    return ((Date.parse(b.at || b.day + 'T12:00:00Z') - t0) / span) * W; };
  function renderRange() {
    put(document.getElementById('range'), html`${Object.keys(RANGES).map(r => html`<button class="chip" type="button" data-r="${r}" aria-pressed="${String(r === range)}">${r}</button>`)}`);
  }
  // 7d, not 24h, is this view's default: most sources write once a day, so 24h shows one tick per lane and no cadence.
  document.getElementById('range').addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c || !RANGES[c.dataset.r]) return;
    range = c.dataset.r; DAYS = days(); if (F.day && !DAYS.includes(F.day)) F.day = ''; page = 1; renderRange(); update();
  });
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
    const t = e.target.closest('rect.tick'); if (t) return select(t.dataset.id, true);
    const b = e.target.closest('.lane-btn'); if (!b) return;
    const s = b.dataset.src;
    F.src.has(s) ? F.src.delete(s) : F.src.add(s); page = 1; update();
    if (F.src.has(s)) showSource(s);
  });

  // ---------- Filters ----------
  function renderFilters() {
    const chip = (f, v, label, pressed, n) => html`<button type="button" class="chip" data-f="${f}" data-v="${v}" aria-pressed="${String(pressed)}">${label}${n != null ? html` <span class="n">${n}</span>` : ''}</button>`;
    const all = ranged(), shown = all.filter(match).length;
    const busy = DAYS.slice().reverse().filter(d => all.some(b => b.day === d));
    put(document.getElementById('filters'), html`
      <div class="fgroup"><span class="label">Source</span><div class="chips">${SOURCES.map(s => chip('src', s.id, s.id, F.src.has(s.id), all.filter(b => b.src === s.id).length))}</div></div>
      <div class="fgroup"><span class="label">Day</span><div class="chips">${chip('day', '', 'all', !F.day)}${busy.map(d => chip('day', d, d.slice(8) + ' ' + dayLabel(d).split(' ')[0], F.day === d))}</div></div>
      <div class="fsum"><span>${shown} of ${all.length}${F.q ? ` · “${F.q}”` : ''}</span>${active() ? html`<button class="linkbtn" type="button" id="clear">Clear all</button>` : ''}</div>`);
  }
  document.getElementById('filters').addEventListener('click', e => {
    if (e.target.closest('#clear')) return clearAll();
    const c = e.target.closest('.chip'); if (!c) return;
    const { f, v } = c.dataset;
    if (f === 'src') { F.src.has(v) ? F.src.delete(v) : F.src.add(v); }
    if (f === 'day') F.day = F.day === v ? '' : v;
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
        <td class="act">${C.rowActions(b.id)}</td></tr>`;
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
    document.getElementById('sub').textContent = READER ? `${plural(READER.total, 'brief')}${READER.total > READER.shown ? `, newest ${READER.shown} read` : ''}` : 'Reading the briefs…';
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
    tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id));
    document.querySelectorAll('#lanes rect.tick').forEach(r => r.classList.toggle('sel', r.dataset.id === id));
    put(details, html`<p class="kind">Brief · ${b.src}</p>
      <h2>${b.subj}</h2>
      <dl><dt>Source</dt><dd>${b.src}</dd><dt>Day</dt><dd>${dayLabel(b.day)}</dd>
        <dt>Written</dt><dd>${b.at ? `${b.at.slice(0, 16).replace('T', ' ')} UTC · ${ago(b.at)}` : 'time not known: the note changed after its day'}</dd>
        <dt>Words</dt><dd>${b.words}</dd><dt>Note</dt><dd><code>${b.rel}</code></dd>
        <dt>Watchdog</dt><dd><span class="g-unknown" aria-hidden="true">▨</span> ${WATCH}</dd></dl>
      <h3>Body · first lines</h3><div class="mailbody">${b.lead}${b.lead.length >= 200 ? '…' : ''}</div>
      <p class="why off">First lines only. The note in the vault holds the whole brief.</p>
      <h3>Act</h3>${C.bar(id)}`);
    C.select(id);
    shell.suggest(['Summarise this brief in three lines', `What else did ${b.src} write this week?`, 'Turn what this brief asks into tasks']);
    swap();
    return true;
  }
  // build: a source has no Act bar. The reference's Retry and Show log ran on the job behind the source; the file name gives a
  // kind, not a job, so there is nothing to run them on.
  function showSource(id) {
    const s = srcOf(id); if (!s) return;
    const mine = ranged().filter(b => b.src === id), on = new Set(mine.map(b => b.day)).size;
    put(details, html`<p class="kind">Source · ${mine.length ? 'quiet days are gaps' : 'no brief in range'}</p>
      <h2>${s.id}</h2>
      <dl><dt>Briefs, ${DAYS.length} days</dt><dd>${mine.length} on ${on} of ${DAYS.length} days</dd><dt>Newest</dt><dd>${s.newest}</dd>
        <dt>All read</dt><dd>${s.n}${capped() ? ` of the newest ${READER.shown}` : ''}</dd><dt>Watchdog</dt><dd><span class="g-unknown" aria-hidden="true">▨</span> ${WATCH}</dd></dl>
      <p class="why">A quiet day is a gap, not a fault. Without a watchdog reader the page cannot say whether the job ran and had nothing to write, or failed. The file name gives the kind, not the job, so there is no job to retry from here.</p>`);
    C.select(null); shell.suggest([`Why is ${s.id} quiet?`, 'Which jobs missed a run this week?']); shell.openPane('tab-details'); swap();
  }
  function select(id, open) { if (!show(id)) return; selected = id; if (open) shell.openPane('tab-details'); }
  // No current row: nothing from an earlier read stays selected, and Details offers no command on it.
  function unselect() { selected = null; C.select(null); put(details, html`<p class="why">Select a brief to see its note.</p>`); }

  // ---------- Commands (products/system/commands.md): declared once, rendered by the shell ----------
  // build: two of the reference's four brief commands. Acknowledge and Mark read wrote a message store that does not exist and
  // had nothing to change here; Open in Gmail is Open in Obsidian, the note's own link.
  function registerCommands() {
    C.register(
      // build: no verb deletes a task, so no Undo: the capture form shows the line and files it only on Capture.
      { id: 'brief.task', on: 'brief', label: 'Make task', key: 'k', risk: 'safe', primary: () => true,
        cli: o => `sd task add ${shq(o.label)}`, run: o => { shell.capture(o, o.label); return 'Check the title, then Capture'; } },
      { id: 'brief.open', on: 'brief', label: 'Open in Obsidian', key: 'o', risk: 'safe', executes: false,
        when: o => !!o.open || 'no vault link for this note', cli: o => `open ${shq(o.open)}`,
        run: o => { window.open(o.open, '_blank', 'noopener'); return 'Obsidian opens the note'; } },
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
      // build: a task opens the capture form with the text; the form files it (SHELL_CAPTURE above).
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

  // ---------- Start (build: read /api/briefs, then draw) ----------
  // An Observed click can start a read while one is out; only the newest read draws, whatever order they answer in.
  let generation = 0;
  async function load() {
    const mine = ++generation;
    shell.state({ kind: 'loading', text: 'Reading the briefs. Rows appear when /api/briefs answers.', source: '/api/briefs' });
    let doc;
    try { doc = await getJSON('/api/briefs'); } catch (err) {
      if (mine !== generation) return;
      shell.state({ kind: 'error', text: `The briefs were not read: ${err.message}. Reload retries it.`, source: '/api/briefs' });
      // Nothing from the last read stays on screen: rows, lanes, the observed time and the selection.
      READ = null; READER = { state: 'error', reason: err.message, total: 0, shown: 0 }; BRIEFS = []; SOURCES = []; F.src.clear(); LOADED = true; update(); unselect();
      return;
    }
    if (mine !== generation) return;
    READ = doc.read; READER = doc.reader || { state: 'error', reason: 'no reader in the answer', total: 0, shown: 0 };
    WATCH = doc.watchdog?.available ? 'read' : (doc.watchdog?.reason || 'no watchdog reader');
    BRIEFS = READER.state === 'error' ? [] : (doc.briefs || []);
    const by = new Map();
    BRIEFS.forEach(b => { const s = by.get(b.src) || { id: b.src, n: 0, newest: b.day }; s.n++; if (b.day > s.newest) s.newest = b.day; by.set(b.src, s); });
    // build: lanes in name order; the reference's lane order was a hand-made list of its sample jobs.
    SOURCES = [...by.values()].sort((a, b) => a.id.localeCompare(b.id));
    [...F.src].forEach(s => { if (!by.has(s)) F.src.delete(s); });
    BRIEFS.forEach(b => C.put({ id: b.id, type: 'brief', label: b.subj, src: b.src, open: b.open }));
    DAYS = days(); LOADED = true;
    const source = READER.source || '/api/briefs';
    if (READER.state === 'error') shell.state({ kind: 'error', text: `The briefs were not read: ${READER.reason}. Reload retries it.`, source });
    else if (!BRIEFS.length) shell.state({ kind: 'empty', title: 'No briefs', text: 'The Briefs folder holds no note.', source });
    else if (READER.skipped || READER.total > READER.shown) shell.state({ kind: 'partial', text: [READER.skipped ? `${plural(READER.skipped, 'row')} skipped: not a brief.` : '', READER.total > READER.shown ? `Showing the newest ${READER.shown} of ${READER.total}; the rest are in the folder. Counts before ${oldest().slice(5)} are not read.` : ''].filter(Boolean).join(' '), source });
    else shell.state(null);
    renderRange(); update();
    const row = shell.row?.();
    const vis = BRIEFS.filter(match);
    const first = BRIEFS.some(b => b.id === row) ? row : (vis[0] || BRIEFS[0])?.id;
    if (first) select(first, !!row && first === row); else unselect();
  }
  readURL();
  if (F.q) input.value = F.q;
  registerCommands();
  load();
});
