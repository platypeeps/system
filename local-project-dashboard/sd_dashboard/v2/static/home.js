// Home (sd:2117): the design source's products/system/designs/v2/home.js at d82daa1, ported; home-kiosk.js sets the wall display flag. Each change from the reference is marked
// "build:". The tiles are /api/home (home_screen.py): the entities <config>/project-dashboard/home-tiles.conf names, each with its
// live Home Assistant state and last change, or a named error (sd:3427), never sample data. Every command is off with the
// document's write reason: the page sends nothing to Home Assistant. Edit tiles (sd:3987) writes the tile list's setting row.
const { html, put, plural } = window.markup;
// Palette "This page" group. shell.js reads it at start, so it is set before the shell runs.
window.PAGE_COMMANDS = [{ label: 'Wall display', icon: 'maximize-2', run: () => document.dispatchEvent(new CustomEvent('home:kiosk', { detail: true })) }];
// build: no state is read, so the page claims nothing for the rail or Today.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'HA not read' };
addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest } = window.shell;
  const C = window.shell.commands;
  const GLYPH = { ok: '●', caution: '▲', warning: '■', unknown: '▨' };
  const domain = id => id.split('.')[0];
  const EVERY = 60 * 1000; // how often a visible page reads the tiles again

  // ---------- Data (build) ----------
  // E: entity id -> { id, name, domain, group, state, error }. HEAD: the headline entity or null. REASON: why the read gave no
  // state, '' when it did. WRITES: why no command runs.
  let E = {}, HEAD = null, GROUPS = [], REASON = '', WRITES = '', READ = null, CONFIG = null;
  // The reference's state grammar per entity kind: [lamp state, words]. A tile claims only a state it was given; one without
  // says why (its error). build: the headline reads on/off (the reference counted a sample attribute), and a binary sensor is
  // lit by its domain, not by an id that holds "wan" or "fault".
  function read(id) {
    const e = E[id];
    if (!e?.state) return ['unknown', e?.error || REASON || 'no reading'];
    const s = e.state.value;
    if (s === 'unavailable' || s === 'unknown') return ['unknown', `${s} · no reading`];
    if (id === HEAD?.id) return s === 'on' ? ['caution', 'active'] : ['ok', 'none'];
    if (typeOf(id) === 'alarm') return s === 'triggered' ? ['warning', 'triggered'] : ['ok', s.replace(/_/g, ' ')];
    if (typeOf(id) === 'lock') return s === 'locked' ? ['ok', 'locked'] : s === 'jammed' ? ['warning', 'jammed'] : ['caution', s];
    if (id.startsWith('sensor.backup')) return (Date.now() - new Date(s)) / 3600000 > 36 ? ['warning', 'stale · over 36 h'] : ['ok', 'succeeded'];
    if (typeOf(id) === 'toggle') return ['ok', s];
    if (domain(id) === 'binary_sensor') return s === 'on' ? ['warning', 'detected'] : ['ok', 'clear'];
    return ['ok', e.state.unit ? `${s} ${e.state.unit}` : s];
  }
  // The age: the shell's relative-time renderer fills each <time class="rel"> from its last change.
  const changedAt = id => E[id]?.state ? html`<time class="rel" datetime="${E[id].state.changed}"></time>` : '';
  // build: one icon per domain; the reference picked one per sample entity.
  const ICONS = { alarm_control_panel: 'shield-check', lock: 'lock', input_boolean: 'circle-dot', sensor: 'gauge', binary_sensor: 'siren' };
  const iconOf = id => id === HEAD?.id ? 'bell-ring' : id.startsWith('sensor.backup') ? 'hard-drive-download' : ICONS[domain(id)] || 'circle-help';

  // ---------- Commands (products/system/commands.md) ----------
  // Home rule: safe toggles and lock run with Undo; arm, disarm and unlock ask first and get no Undo. The CLI line is the HA REST
  // service call a writer will make. build: each write is off with the document's write reason, so none runs, and none declares
  // an Undo it could not honour. Read state is copy only: its line names $HA_TOKEN and $HA_URL, never a value.
  const svcCli = svc => o => `curl -X POST -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/services/${svc(o).replace('.', '/')}" -d '{"entity_id":"${o.id}"}'`;
  const TYPE = { alarm_control_panel: 'alarm', lock: 'lock', input_boolean: 'toggle' };
  // build: a tile's configured type wins (a virtual switch says toggle); otherwise the domain decides.
  const typeOf = id => E[id]?.type || TYPE[domain(id)] || 'sensor';
  // build: the reference read the place from sample names; the tile's configured group names it here.
  const place = o => E[o.id]?.group || 'home';
  const security = svc => o => `This calls ${svc} on ${o.id}. It changes physical security (${place(o)}).`;
  const off = () => WRITES || 'not read yet';
  const notSent = o => `Not sent: ${off()} · ${o.label}`;
  function registerCommands() {
    C.register(
      { id: 'alarm.arm_home', on: 'alarm', label: 'Arm home', key: 'h', risk: 'confirm', primary: () => true, when: off, cli: svcCli(() => 'alarm_control_panel.alarm_arm_home'), consequence: security('alarm_control_panel.alarm_arm_home'), run: notSent },
      { id: 'alarm.arm_away', on: 'alarm', label: 'Arm away', key: 'w', risk: 'confirm', when: off, cli: svcCli(() => 'alarm_control_panel.alarm_arm_away'), consequence: security('alarm_control_panel.alarm_arm_away'), run: notSent },
      { id: 'alarm.disarm', on: 'alarm', label: 'Disarm', key: 'd', risk: 'confirm', primary: () => true, when: off, cli: svcCli(() => 'alarm_control_panel.alarm_disarm'), consequence: security('alarm_control_panel.alarm_disarm'), run: notSent },
      { id: 'lock.lock', on: 'lock', label: 'Lock', key: 'l', risk: 'undo', primary: () => true, when: off, cli: svcCli(() => 'lock.lock'), run: notSent },
      { id: 'lock.unlock', on: 'lock', label: 'Unlock', key: 'u', risk: 'confirm', primary: () => true, when: off, cli: svcCli(() => 'lock.unlock'), consequence: security('lock.unlock'), run: notSent },
      { id: 'toggle.on', on: 'toggle', label: 'Turn on', key: 'o', risk: 'undo', primary: () => true, when: off, cli: svcCli(() => 'input_boolean.turn_on'), run: notSent },
      { id: 'toggle.off', on: 'toggle', label: 'Turn off', key: 'f', risk: 'undo', primary: () => true, when: off, cli: svcCli(() => 'input_boolean.turn_off'), run: notSent },
      // Read-only sensors: the one command is the state read a reader makes.
      { id: 'sensor.read', on: 'sensor', label: 'Read state', key: 's', risk: 'safe', executes: false, cli: o => `curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/states/${o.id}"`, run: () => 'Copy the line; it reads HA_TOKEN and HA_URL from your shell' },
    );
  }
  // build: a tile shows its commands only once one of them can run.
  const actionable = id => !WRITES && typeOf(id) !== 'sensor';

  // ---------- Render ----------
  const groupsEl = document.getElementById('groups');
  let selected = null;
  const gid = g => `g-${g.replace(/\W+/g, '')}`;
  function tileHTML(t) {
    const [st, words] = read(t.id);
    return html`<li><div class="tile" data-id="${t.id}" data-state="${st}" aria-current="${String(t.id === selected)}">
      <button class="open" type="button" aria-label="${t.name}: ${words}. Details">
        ${ICON(iconOf(t.id))}<span class="nm">${t.name}</span>
        <span class="st"><span class="g" aria-hidden="true">${GLYPH[st]}</span>${words}</span>
        ${changedAt(t.id)}
      </button>
      <div class="act">${actionable(t.id) ? C.rowActions(t.id) : ''}</div>
    </div></li>`;
  }
  function subhead() {
    const n = Object.keys(E).length - (HEAD ? 1 : 0);
    put(document.getElementById('subhead'), html`${plural(n, 'tile')} from <code>${CONFIG?.source || 'home-tiles.conf'}</code> · read ${READ ? html`<time class="rel" datetime="${READ}"></time>` : 'not yet'} · ${REASON ? 'no HA state read' : 'HA read'}`);
  }
  function render() {
    const hb = document.getElementById('headline');
    hb.hidden = !HEAD;
    if (HEAD) {
      const [hs, hw] = read(HEAD.id);
      hb.dataset.id = HEAD.id; hb.dataset.state = hs;
      put(hb, html`${ICON('bell-ring')}<span class="what"><b>${HEAD.name}</b><span>${hs === 'unknown' ? REASON || hw : hs === 'ok' ? 'nothing reported' : 'problems reported'}</span></span><span class="st"><span aria-hidden="true">${GLYPH[hs]}</span> ${hw}</span>`);
      hb.setAttribute('aria-label', `${HEAD.name}: ${hw}. Details`);
    }
    put(groupsEl, html`${GROUPS.map(g => html`<section class="group" aria-labelledby="${gid(g.name)}"><h2 class="label" id="${gid(g.name)}">${g.name}</h2><ul class="lamps">${g.tiles.map(tileHTML)}</ul></section>`)}`);
    subhead();
  }

  // ---------- Details ----------
  const details = document.getElementById('details');
  function markSelected() { document.querySelectorAll('.tile').forEach(t => t.setAttribute('aria-current', String(t.dataset.id === selected))); }
  function showDetails(id, open = true) {
    const e = E[id]; if (!e) return;
    selected = id; markSelected();
    const [st, words] = read(id);
    put(details, html`
      <div class="kind"><span class="label">${domain(id).replace(/_/g, ' ')}</span></div>
      <h2>${e.name}</h2>
      <dl>
        <dt>State</dt><dd class="g-${st}"><span aria-hidden="true">${GLYPH[st]}</span> ${words}${e.state ? html` <span class="g-queued">(${e.state.value})</span>` : ''}</dd>
        <dt>Entity</dt><dd><code>${id}</code></dd>
        <dt>Group</dt><dd>${e.group || 'headline'}</dd>
        <dt>Last changed</dt><dd>${e.state ? changedAt(id) : 'not read'}</dd>
      </dl>
      ${e.state ? '' : html`<p class="why">Not read: ${REASON || e.error || 'no reading'}.</p>`}
      <h3 class="label">Act</h3>${C.bar(id)}
      <p class="why">${typeOf(id) === 'sensor' ? 'Read-only sensor. Nothing to act on from here.' : typeOf(id) === 'toggle' ? 'Runs at once; Undo for 10 seconds, once the dashboard sends it.' : 'Arm, disarm and unlock ask first and name the target; they have no Undo. Lock runs at once with Undo.'}</p>`);
    details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap'));
    C.select(id);
    if (open && !document.documentElement.hasAttribute('data-kiosk')) window.shell.openPane('tab-details');
  }

  // ---------- Events ----------
  document.getElementById('main').addEventListener('click', ev => {
    if (ev.target.closest('.rowact')) return;
    const tile = ev.target.closest('.tile');
    if (ev.target.closest('.open') && tile) {
      if (document.documentElement.hasAttribute('data-kiosk')) { const e = E[tile.dataset.id]; if (e) toast(`${e.name} · ${read(e.id)[1]}`); return; }
      showDetails(tile.dataset.id);
    }
    if (ev.target.closest('#headline') && HEAD) showDetails(HEAD.id);
  });
  document.addEventListener('shell:open', e => { if (E[e.detail]) showDetails(e.detail); });
  // build: j / k are the shell's (PAGE_LIST); the tiles are the list, in reading order.
  window.PAGE_LIST = {
    rows: () => document.querySelectorAll('.tile'),
    current: () => selected,
    select: id => showDetails(id, false),
  };

  // ---------- Kiosk ----------
  const root = document.documentElement;
  const clock = document.getElementById('clock');
  function tick() { const d = new Date(); clock.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); clock.dateTime = d.toISOString(); }
  tick(); setInterval(tick, 15000);
  function setKiosk(on) {
    on ? root.setAttribute('data-kiosk', '') : root.removeAttribute('data-kiosk');
    window.shell.url(on ? { kiosk: '1' } : {}); // build: the shell writes the page's params and keeps ?row=
    if (on) { window.shell.closePane(); root.requestFullscreen?.().catch(() => {}); }
    else if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    document.getElementById(on ? 'kiosk-off' : 'kiosk-on').focus();
  }
  document.getElementById('kiosk-on').addEventListener('click', () => setKiosk(true));
  document.getElementById('kiosk-off').addEventListener('click', () => setKiosk(false));
  document.addEventListener('home:kiosk', e => setKiosk(!!e.detail));

  // ---------- Start (build, sd:2486: shell.read reads /api/home, then draws) ----------
  // The reader (read.js, sd:2418) holds the guards: only the newest read draws, a tile the read no longer lists runs no
  // command, a failed read clears the grid and Details, and the first read selects the ?row= tile.
  function adopt(doc) {
    // body[data-observed] and data-interval (minutes) give the shell's staleness band on #refresh its reading time and cadence.
    READ = doc.read; document.body.dataset.observed = doc.read; document.body.dataset.interval = String(EVERY / 60000); CONFIG = doc.config; REASON = doc.reader?.available ? '' : (doc.reader?.reason || 'Home Assistant not read');
    WRITES = doc.writes?.available ? '' : (doc.writes?.reason || 'no Home Assistant writes');
    HEAD = doc.headline; GROUPS = doc.groups || []; E = {};
    if (HEAD) E[HEAD.id] = { ...HEAD, group: null };
    GROUPS.forEach(g => g.tiles.forEach(t => { E[t.id] = { ...t, group: g.name }; }));
    document.getElementById('edit-tiles').disabled = !CONFIG?.editor;
    const problems = CONFIG?.problems || [];
    const unread = Object.values(E).filter(e => !e.state);
    const state = CONFIG?.state === 'missing' ? { kind: 'empty', title: 'No tile list', text: 'Edit tiles opens on project-dashboard/home-tiles.conf.example; Save keeps the list in the workflow database.', source: CONFIG.source }
      : CONFIG?.state === 'error' ? { kind: 'error', text: `The tile list was not read: ${problems.map(p => p.replace(/^tile list not read: /, '')).join('; ')}. Home Assistant was not called.`, source: CONFIG.source }
      : problems.length ? { kind: 'partial', text: `${plural(problems.length, 'line')} skipped: ${problems.join('; ')}. ${REASON ? `No tile has a state: ${REASON}.` : ''}`, source: CONFIG.source }
      : REASON ? { kind: 'partial', title: 'No state read', text: `The tiles are the configured entities; none has a state: ${REASON}.`, source: '/api/home' }
      : unread.length ? { kind: 'partial', title: 'Some tiles not read', text: `${plural(unread.length, 'tile')} without a state: ${unread.map(e => `${e.id} (${e.error || 'no reading'})`).join('; ')}.`, source: '/api/home' }
      : null;
    return { objects: Object.values(E).map(e => ({ id: e.id, type: typeOf(e.id), label: e.name })), state };
  }
  function clear(err) { document.getElementById('edit-tiles').disabled = true; E = {}; HEAD = null; GROUPS = []; READ = null; CONFIG = null; REASON = `the tiles were not read: ${err.message}`; WRITES = REASON; delete document.body.dataset.observed; }
  function unselect() {
    selected = null; markSelected();
    put(details, html`<p class="why">Select a tile to see its entity and its state.</p>`);
  }
  const reading = window.shell.read({
    source: '/api/home', what: 'the tiles', adopt, clear, draw: render, unselect,
    current: () => selected, first: () => null,
    // The wall display opens no Details, so a ?row= tile is not selected there.
    select: (id, opened) => { if (root.hasAttribute('data-kiosk')) unselect(); else showDetails(id, opened); },
  });
  const load = () => reading.load();

  // ---------- Edit tiles (sd:3987) ----------
  // The editor opens on the text the last read showed: the row, else the file, else the example. Save posts it with that
  // text's digest. A 400 names each refused line and keeps the editor open; a 409 wrote nothing and reads again. A save
  // closes the editor and toasts with Undo, which posts the previous text with the saved text's digest.
  const ed = document.getElementById('editor'), edText = document.getElementById('editor-text'), edOut = document.getElementById('editor-out');
  const edit = document.getElementById('edit-tiles');
  const FROM = {
    database: 'Opened on the list in the workflow database.',
    file: 'Opened on home-tiles.conf. Save moves the list to the workflow database; the file is not read after that.',
    example: 'Opened on the example. Save keeps the list in the workflow database.',
  };
  let before = '';
  function openEditor() {
    const e = CONFIG?.editor; if (!e) return;
    edText.value = e.text; before = e.digest; edOut.textContent = '';
    document.getElementById('editor-from').textContent = FROM[e.from] || '';
    ed.hidden = false; edText.focus();
  }
  function closeEditor() { ed.hidden = true; edit.focus(); }
  const saveTiles = (text, digest) => window.shell.post('/api/home/tiles', { text, before: digest });
  ed.addEventListener('submit', async ev => {
    ev.preventDefault();
    edOut.textContent = 'Saving…';
    let out;
    try { out = await saveTiles(edText.value, before); }
    catch (err) {
      edOut.textContent = err.stale ? 'Not saved: the list changed since this page read it. Cancel, then Edit tiles opens on the new list.'
        : err.status ? `Not saved: ${err.message}` : `Not known whether it saved: ${err.message}. Read again before you save.`;
      if (err.stale || !err.status) reading.load(true);
      return;
    }
    ed.hidden = true; edit.focus();
    const undo = out.previous ? () => saveTiles(out.previous, out.digest).then(() => { toast('Tile list put back'); reading.reread(); },
      err => { toast(`Undo not done: ${err.message}`); if (err.stale || !err.status) reading.load(true); }) : null;
    toast('Tile list saved', undo);
    reading.reread();
  });
  document.getElementById('editor-cancel').addEventListener('click', closeEditor);
  edit.addEventListener('click', () => (ed.hidden ? openEditor() : closeEditor()));
  registerCommands();
  load();
  // Live state (sd:3522): Read again reads /api/home now, and a visible page reads it each minute, so the wall display follows
  // Home Assistant without a reload. The minute read is quiet: a loading state above the tiles would move them each minute.
  document.getElementById('refresh').addEventListener('click', () => load());
  setInterval(() => { if (!document.hidden) reading.load(true); }, EVERY);
  suggest([
    'Why is binary_sensor.problems_active on?',
    'What changed in the Home Assistant config this week?',
    'Which locks have reported unavailable today?',
  ]);
});
