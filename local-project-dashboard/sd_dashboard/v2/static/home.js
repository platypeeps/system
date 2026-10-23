// Home (sd:2117): the design source's products/system/designs/v2/home.js at d82daa1, ported; home-kiosk.js sets the wall display flag. Each change from the reference is marked
// "build:". The tiles are /api/home (home_screen.py): the entities <config>/project-dashboard/home-tiles.conf names, each with its
// live Home Assistant state and last change, or a named error (sd:3427), never sample data. A tile's Act section calls the
// Home Assistant services its domain allows through /api/home/service (sd:4966). Edit tiles (sd:3987) writes the tile list's setting row.
const { html, put, plural } = window.markup;
// Palette "This page" group. shell.js reads it at start, so it is set before the shell runs.
window.PAGE_COMMANDS = [{ label: 'Wall display', icon: 'maximize-2', run: () => document.dispatchEvent(new CustomEvent('home:kiosk', { detail: true })) }];
// build: no state is read, so the page claims nothing for the rail or Today.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'HA not read' };
addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest } = window.shell;
  const C = window.shell.commands;
  // neutral is no state: a door's open or closed is a fact, not a fault, so it has no colour and no glyph.
  const GLYPH = { ok: '●', caution: '▲', warning: '■', unknown: '▨', neutral: '' };
  const domain = id => id.split('.')[0];
  const EVERY = 60 * 1000; // how often a visible page reads the tiles again

  // ---------- Data (build) ----------
  // E: entity id -> { id, name, domain, group, state, error }. HEAD: the headline entity or null. REASON: why the read gave no
  // state, '' when it did. WRITES: why no command runs, '' when they can. SERVICES: the server's allowlist, domain -> service -> risk.
  let E = {}, HEAD = null, GROUPS = [], REASON = '', WRITES = '', SERVICES = {}, READ = null, CONFIG = null;
  // build (sd:4959): a binary sensor reads by its Home Assistant device_class: [lamp when on, lamp when off, on words, off
  // words, what on means]. On is good for the first group (connectivity off is caution, power off a plain fact), a fault for
  // the second, and a plain fact for the third; a sensor with no class is neutral on/off, never a warning. The words follow Home Assistant's own state names.
  const BINARY = {
    connectivity: ['ok', 'caution', 'connected', 'disconnected', 'it is connected'], // off: a lost link is worth a look
    power: ['ok', 'neutral', 'connected', 'disconnected', 'it is connected to power'],
    plug: ['ok', 'neutral', 'plugged in', 'unplugged', 'it is plugged in'],
    running: ['ok', 'neutral', 'running', 'not running', 'it is running'],
    battery_charging: ['ok', 'neutral', 'charging', 'not charging', 'the battery is charging'],
    presence: ['ok', 'neutral', 'home', 'away', 'someone is home'],
    problem: ['warning', 'ok', 'problem', 'clear', 'a problem is reported'],
    smoke: ['warning', 'ok', 'detected', 'clear', 'smoke is detected'],
    gas: ['warning', 'ok', 'detected', 'clear', 'gas is detected'],
    carbon_monoxide: ['warning', 'ok', 'detected', 'clear', 'carbon monoxide is detected'],
    moisture: ['warning', 'ok', 'wet', 'dry', 'water is detected'],
    safety: ['warning', 'ok', 'unsafe', 'safe', 'it is unsafe'],
    tamper: ['warning', 'ok', 'tampered', 'clear', 'tampering is detected'],
    heat: ['warning', 'ok', 'hot', 'normal', 'it is too hot'],
    cold: ['warning', 'ok', 'cold', 'normal', 'it is too cold'],
    door: ['neutral', 'neutral', 'open', 'closed', 'the door is open'],
    window: ['neutral', 'neutral', 'open', 'closed', 'the window is open'],
    opening: ['neutral', 'neutral', 'open', 'closed', 'it is open'],
    garage_door: ['neutral', 'neutral', 'open', 'closed', 'the garage door is open'],
    motion: ['neutral', 'neutral', 'motion', 'clear', 'motion is detected'],
    occupancy: ['neutral', 'neutral', 'occupied', 'clear', 'the space is occupied'],
  };
  const binaryOf = id => BINARY[E[id]?.state?.device_class];
  // What a state means, in one line for Details: the binary table above, else the domain's.
  const DOMAIN_MEANS = { alarm_control_panel: 'the alarm mode: disarmed, armed home, armed away or triggered', lock: 'locked or unlocked; jammed means the lock did not finish moving',
    input_boolean: 'on or off, a switch Home Assistant keeps', sensor: 'a value Home Assistant reports' };
  function means(id) {
    const e = E[id], b = binaryOf(id), dc = e.state?.device_class;
    if (id === HEAD?.id) return 'on means problems are reported';
    if (domain(id) === 'binary_sensor') return b ? `on means ${b[4]}` : 'on or off, with no device class to judge it by';
    if (domain(id) === 'sensor' && dc) return `a ${dc.replace(/_/g, ' ')} Home Assistant reports`;
    return DOMAIN_MEANS[domain(id)] || 'a state Home Assistant reports';
  }
  // The reference's state grammar per entity kind: [lamp state, words]. A tile claims only a state it was given; one without
  // says why (its error). build: the headline reads on/off (the reference counted a sample attribute), and a binary sensor is
  // lit by its device_class (BINARY), not by an id that holds "wan" or "fault".
  // sd:5727: a script or automation (Good night) reads as its last run, Home Assistant's last_triggered, in this machine's
  // local time with its zone; a run the read did not report is unknown, never "off".
  const RUNS = ['script', 'automation'];
  const ranAt = id => { const t = Date.parse(E[id]?.state?.ran || '');
    return isFinite(t) ? new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }) : ''; };
  function read(id) {
    const e = E[id];
    if (!e?.state) return ['unknown', e?.error || REASON || 'no reading'];
    const s = e.state.value;
    if (s === 'unavailable' || s === 'unknown') return ['unknown', `${s} · no reading`];
    if (id === HEAD?.id) return s === 'on' ? ['caution', 'active'] : ['ok', 'none'];
    if (RUNS.includes(domain(id))) return s === 'on' ? ['ok', 'running'] : ranAt(id) ? ['ok', `last run ${ranAt(id)}`] : ['unknown', 'last run not read'];
    if (typeOf(id) === 'alarm') return s === 'triggered' ? ['warning', 'triggered'] : ['ok', s.replace(/_/g, ' ')];
    if (typeOf(id) === 'lock') return s === 'locked' ? ['ok', 'locked'] : s === 'jammed' ? ['warning', 'jammed'] : ['caution', s];
    if (id.startsWith('sensor.backup')) return (Date.now() - new Date(s)) / 3600000 > 36 ? ['warning', 'stale · over 36 h'] : ['ok', 'succeeded'];
    if (typeOf(id) === 'toggle') return ['ok', s];
    if (domain(id) === 'binary_sensor') { const b = binaryOf(id) || ['neutral', 'neutral', 'on', 'off']; return s === 'on' ? [b[0], b[2]] : s === 'off' ? [b[1], b[3]] : ['neutral', s]; }
    return ['ok', e.state.unit ? `${s} ${e.state.unit}` : s];
  }
  // The age: the shell's relative-time renderer fills each <time class="rel"> from its last change.
  const changedAt = id => E[id]?.state ? html`<time class="rel" datetime="${E[id].state.changed}"></time>` : '';
  // build: one icon per domain; the reference picked one per sample entity.
  const ICONS = { script: 'play', automation: 'play', alarm_control_panel: 'shield-check', lock: 'lock', input_boolean: 'circle-dot', sensor: 'gauge', binary_sensor: 'siren' };
  const iconOf = id => id === HEAD?.id ? 'bell-ring' : id.startsWith('sensor.backup') ? 'hard-drive-download' : ICONS[domain(id)] || 'circle-help';

  // ---------- Commands (products/system/commands.md) ----------
  // sd:4966: a tile's commands are the Home Assistant services its domain allows, as the server's allowlist (writes.services)
  // says; a domain with none is a read-only sensor. On and off run at once with Undo (the other one; Toggle undoes itself);
  // scene, script and button run once with no Undo. Lock, alarm and cover run at once too, with no Undo: the operator
  // ruled out every confirm on Home (sd:5479). Each posts /api/home/service with the tile list's digest; the
  // answer carries the tile's state read again, which the tile shows at once; any failure but a 400 reads the tiles again. The CLI line is the same REST call; it names
  // $HA_TOKEN and $HA_URL, never a value.
  const svcCli = svc => o => `curl -X POST -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/services/${svc(o).replace('.', '/')}" -d '{"entity_id":"${o.id}"}'`;
  // build: a tile's configured type wins for how it reads (a virtual switch says toggle); otherwise the domain decides.
  const TYPE = { alarm_control_panel: 'alarm', lock: 'lock', input_boolean: 'toggle' };
  const typeOf = id => E[id]?.type || TYPE[domain(id)] || 'sensor';
  // The command set: the domain when the allowlist names it, else the read-only sensor's.
  const kindOf = id => SERVICES[domain(id)] ? domain(id) : 'sensor';
  // build: the reference read the place from sample names; the tile's configured group names it here.
  const place = o => E[o.id]?.group || 'home';
  const valueOf = o => E[o.id]?.state?.value;
  // A service is off when writes are, when the allowlist no longer names it, or when the tile has no state to act from.
  const offFor = (d, svc) => o => WRITES || (!SERVICES[d]?.[svc] ? `the server does not allow ${d}.${svc}` : !E[o.id]?.state ? `no state read: ${E[o.id]?.error || REASON || 'not read'}` : true);
  function send(o, svc) {
    const body = { entity_id: o.id, service: svc, before: CONFIG?.editor?.digest || '' };
    return window.shell.post('/api/home/service', body).then(out => {
      if (E[o.id] && out.state) { E[o.id].state = out.state; render(); if (selected === o.id) showDetails(o.id, false); }
      const extra = [out.reread, out.journal].filter(Boolean).join('; ');
      return `${o.label} · ${domain(o.id)}.${svc} sent${extra ? ` · ${extra}` : ''}`;
    }, err => { if (err.status !== 400) reading.load(true); throw err; }); // only a 400 surely sent nothing
  }
  // One command per allowed service: domain, service, label, key, does, and the states it is the row's button in (none
  // given: always; [] never).
  const SWITCHES = ['light', 'switch', 'fan', 'input_boolean'];
  const OPPOSITE = { turn_on: 'turn_off', turn_off: 'turn_on', toggle: 'toggle' };
  const one = (d, svc, label, key, does, primaryOn) => ({ id: `${d}.${svc}`, on: d, label, key, does, executes: true,
    primary: primaryOn ? o => primaryOn.includes(valueOf(o)) : () => true, when: offFor(d, svc), cli: svcCli(() => `${d}.${svc}`) });
  const undoable = (d, svc, label, key, does, primaryOn) => ({ ...one(d, svc, label, key, does, primaryOn), risk: 'undo',
    run: o => send(o, svc), undo: o => send(o, OPPOSITE[svc]).then(() => true) });
  const safe = (d, svc, label, key, does, primaryOn) => ({ ...one(d, svc, label, key, does, primaryOn), risk: 'safe', run: o => send(o, svc) });
  function registerCommands() {
    C.register(
      ...SWITCHES.flatMap(d => [
        undoable(d, 'turn_on', 'Turn on', 'o', 'Turns it on through Home Assistant. Undo turns it off.', ['off']),
        undoable(d, 'turn_off', 'Turn off', 'f', 'Turns it off through Home Assistant. Undo turns it on.', ['on']),
        undoable(d, 'toggle', 'Toggle', 't', 'Flips it through Home Assistant. Undo flips it back.', []),
      ]),
      safe('scene', 'turn_on', 'Activate', 'a', 'Activates the scene. No Undo: the scene keeps no prior state.'),
      safe('script', 'turn_on', 'Run', 'r', 'Runs the script through Home Assistant. No Undo.'),
      safe('button', 'press', 'Press', 'p', 'Presses the button through Home Assistant. No Undo.'),
      safe('lock', 'unlock', 'Unlock', 'u', 'Unlocks the door. No Undo.', ['locked', 'jammed']),
      safe('lock', 'lock', 'Lock', 'l', 'Locks the door. No Undo.', ['unlocked', 'open']),
      safe('alarm_control_panel', 'alarm_disarm', 'Disarm', 'd', 'Disarms the alarm. No Undo.', ['armed_home', 'armed_away', 'armed_night', 'triggered']),
      safe('alarm_control_panel', 'alarm_arm_home', 'Arm home', 'h', 'Arms the alarm for home. No Undo.', ['disarmed']),
      safe('alarm_control_panel', 'alarm_arm_away', 'Arm away', 'w', 'Arms the alarm for away. No Undo.', []),
      safe('alarm_control_panel', 'alarm_arm_night', 'Arm night', 'n', 'Arms the alarm for night. No Undo.', []),
      safe('cover', 'open_cover', 'Open', 'o', 'Opens the cover or garage door. No Undo.', ['closed']),
      safe('cover', 'close_cover', 'Close', 'x', 'Closes the cover or garage door. No Undo.', ['open']),
      // Read-only sensors: the one command is the state read a reader makes.
      { id: 'sensor.read', on: 'sensor', label: 'Read state', key: 's', risk: 'safe', executes: false, does: 'Shows the curl line that reads this state; the dashboard does not run it.', cli: o => `curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/states/${o.id}"`, run: () => 'Copy the line; it reads HA_TOKEN and HA_URL from your shell' },
    );
  }
  // build: a tile shows its commands only once one of them can run.
  const actionable = id => !WRITES && kindOf(id) !== 'sensor';

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
  // What the Act section's commands do, by command set (sd:4966).
  const SECURITY = 'Changes physical security; runs at once, with no Undo.';
  const ACT = { sensor: 'Read-only: Home Assistant has no service for this domain that the dashboard calls.', lock: SECURITY, alarm_control_panel: SECURITY, cover: SECURITY,
    ...Object.fromEntries(SWITCHES.map(d => [d, 'Runs at once through Home Assistant; Undo for 10 seconds.'])), once: 'Runs once through Home Assistant; no Undo.' };
  function markSelected() { document.querySelectorAll('.tile').forEach(t => t.setAttribute('aria-current', String(t.dataset.id === selected))); }
  function showDetails(id, open = true) {
    const e = E[id]; if (!e) return;
    selected = id; markSelected();
    const [st, words] = read(id);
    put(details, html`
      <div class="kind"><span class="label">${domain(id).replace(/_/g, ' ')}</span></div>
      <h2>${e.name}</h2>
      <p class="means">${e.name}: ${means(id)}.</p>
      <dl>
        <dt>State</dt><dd class="g-${st}"><span aria-hidden="true">${GLYPH[st]}</span> ${words}${e.state ? html` <span class="g-queued">(${e.state.value})</span>` : ''}</dd>
        <dt>Entity</dt><dd><code>${id}</code></dd>
        <dt>Group</dt><dd>${e.group || 'headline'}</dd>
        <dt>Last changed</dt><dd>${e.state ? changedAt(id) : 'not read'}</dd>${RUNS.includes(domain(id)) ? html`
        <dt>Last run</dt><dd>${ranAt(id) || 'not read'}</dd>` : ''}
        <dt>Source</dt><dd>Home Assistant <code>/api/states</code>${e.state?.device_class ? html`, device class <code>${e.state.device_class}</code>` : ''}</dd>
      </dl>
      ${e.state ? '' : html`<p class="why">Not read: ${REASON || e.error || 'no reading'}.</p>`}
      <h3 class="label">Act</h3>${C.bar(id)}
      <p class="why">${ACT[kindOf(id)] || ACT.once}</p>`);
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
    // body[data-observed] and data-interval (minutes) give the shell's staleness band on Read again its reading time and cadence.
    READ = doc.read; document.body.dataset.observed = doc.read; document.body.dataset.interval = String(EVERY / 60000); CONFIG = doc.config; REASON = doc.reader?.available ? '' : (doc.reader?.reason || 'Home Assistant not read');
    WRITES = doc.writes?.available ? '' : (doc.writes?.reason || 'no Home Assistant writes'); SERVICES = doc.writes?.services || {};
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
    return { objects: Object.values(E).map(e => ({ id: e.id, type: kindOf(e.id), label: e.name })), state };
  }
  function clear(err) { document.getElementById('edit-tiles').disabled = true; E = {}; HEAD = null; GROUPS = []; READ = null; CONFIG = null; REASON = `the tiles were not read: ${err.message}`; WRITES = REASON; SERVICES = {}; delete document.body.dataset.observed; }
  function unselect() {
    selected = null; markSelected();
    put(details, html`<p class="why">Select a tile to see its entity and its state.</p>`);
  }
  const reading = window.shell.read({
    source: '/api/home', what: 'the tiles', adopt, clear, draw: render, unselect,
    current: () => selected, first: () => null,
    // sd:5620: another view's row (a brief) may fill Details; the minute read selects a tile only while the Overview shows.
    active: () => (window.shell.view?.current?.() || 'overview') === 'overview',
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
        : err.status ? err.message : `Not known whether it saved: ${err.message}. Read again before you save.`;
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
  // sd:5638: it reads the shown Tasks, Briefs or Reports view again too. sd:5750: it is the shell's button, in the head's tools.
  window.PAGE_AGAIN = { title: 'Read the tiles and the shown view again', run: () => Promise.all([load(), window.places.again()]) };
  setInterval(() => { if (!document.hidden) reading.load(true); }, EVERY);
  suggest([
    'Why is binary_sensor.problems_active on?',
    'What changed in the Home Assistant config this week?',
    'Which locks have reported unavailable today?',
  ]);
});
