// HOA (sd:2116): the design source's products/system/designs/pages/hoa.js, ported. Each change from the reference is marked
// "build:". The data is /api/hoa (hoa_screen.py): the asset map, the Mission export and the open followups of the checkout
// <config>/project-dashboard/hoa.conf names, never sample data. A part that did not read is unknown with its reason.
// build: no Leaflet. The design vendored Leaflet 1.9.4; this page draws the same vectors as one SVG with Fit, zoom and drag,
// so the dashboard ships no third-party script. Clusters and the aerial button are not built (docs/pages/hoa.md).
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);
  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  const RANK = { warning: 3, caution: 2, unknown: 1, ok: 0, queued: 0 };
  const dueText = o => o.due_days == null ? 'no due date' : o.due_days < 0 ? `overdue ${plural(-o.due_days, 'day')}` : o.due_days === 0 ? 'due today' : `in ${plural(o.due_days, 'day')}`;
  const reduce = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  let DOC = null, M = null, ASSETS = [], LOTS = [], OB = [], AL = [], CELLS = [];
  let selected = null, range = 7, selDay = null, zoom = 1, center = null, P = null;
  const hidden = new Set();

  const dl = pairs => html`<dl>${pairs.filter(p => p[1] !== '' && p[1] != null).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`;
  const num = v => v !== '' && v != null && !Number.isNaN(+v);
  const gal = v => num(v) ? (+v).toLocaleString('en-US') : '—';
  const day = s => String(s || '').slice(5, 10);
  const unknownBlock = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;

  // ---------- Annunciator ----------
  // build: every cell is a reading of the document or unknown with its reason; the design's mail-only facts (the 05:50 fault,
  // "9 overdue") have no reader, so Water is unknown and Followups counts the checkout's open followup notes.
  function cells() {
    const m = DOC.mission, la = m.analog[m.analog.length - 1], lp = m.pump[m.pump.length - 1], f = DOC.followups;
    const why = m.error || 'no rows in the export';
    const out = [{ id: 'water', label: 'Water', state: 'unknown', val: html`<b>—</b> not read`, small: 'no live reader: the export holds daily rows, not the pump now' }];
    out.push(la && num(la.tank_ft_mean) ? { id: 'tank', label: 'Tank level', state: 'ok', open: `day:${la.date}`, val: html`<b>${(+la.tank_ft_mean).toFixed(1)}</b> ft mean`, small: `${la.tank_ft_min}–${la.tank_ft_max} · ${day(la.date)} (latest row)` }
      : { id: 'tank', label: 'Tank level', state: 'unknown', val: html`<b>—</b> not read`, small: why });
    out.push(lp && num(lp.a2_runtime_hours) ? { id: 'pumps', label: 'Pumps', state: 'ok', open: `day:${lp.date}`, val: html`<b>${(+lp.a2_runtime_hours).toFixed(1)}</b> h A2`, small: `${plural(+lp.a2_starts || 0, 'start')} · ${gal(lp.a2_gallons)} gal · ${day(lp.date)}` }
      : { id: 'pumps', label: 'Pumps', state: 'unknown', val: html`<b>—</b> not read`, small: why });
    out.push(la && num(la.aquifer_ft_mean) ? { id: 'aquifer', label: 'Aquifer', state: 'ok', open: `day:${la.date}`, val: html`<b>${Math.round(+la.aquifer_ft_mean)}</b> ft mean`, small: `${la.aquifer_ft_min}–${la.aquifer_ft_max} · ${day(la.date)}` }
      : { id: 'aquifer', label: 'Aquifer', state: 'unknown', val: html`<b>—</b> not read`, small: why });
    const at = m.alarms_today, real = AL.filter(a => a.st === 'warning').length, tests = AL.filter(a => a.kind === 'generator' && /Running/.test(a.event)).length;
    out.push(at.count == null ? { id: 'alarms', label: 'Alarms today', state: 'unknown', open: AL[0]?.id, val: html`<b>—</b> ${at.reason}`, small: `${plural(real, 'alarm')} in 30 d · ${plural(tests, 'generator test')}` }
      : { id: 'alarms', label: 'Alarms today', state: at.count ? 'warning' : 'ok', open: AL[0]?.id, val: html`<b>${at.count}</b> today`, small: `${plural(real, 'alarm')} in 30 d · ${plural(tests, 'generator test')}` });
    const late = OB.filter(o => o.state === 'warning').length, soon = OB.filter(o => o.state === 'caution').length;
    out.push(f.error ? { id: 'followups', label: 'Followups', state: 'unknown', val: html`<b>—</b> not read`, small: f.error }
      : late ? { id: 'followups', label: 'Followups', state: 'warning', open: OB[0].id, val: html`<b>${late}</b> overdue`, small: `${OB.length} open · ${soon} due within a week` }
      : { id: 'followups', label: 'Followups', state: soon ? 'caution' : 'ok', open: OB[0]?.id, val: html`<b>${soon || OB.length}</b> ${soon ? 'due soon' : 'open'}`, small: soon ? `${OB.length} open · none overdue` : 'none overdue or due within a week' });
    out.push(m.latest ? { id: 'mission', label: 'Mission data', state: m.stale ? 'caution' : 'ok', open: 'source:mission', val: html`<b>${m.age_days} d</b> old`, small: `newest row ${m.latest}${m.error ? ' · a file did not read' : ''}` }
      : { id: 'mission', label: 'Mission data', state: 'unknown', open: m.dir ? 'source:mission' : null, val: html`<b>—</b> not read`, small: why });
    return out;
  }
  function lamps() {
    const clock = html`<li><button class="cell clock" type="button" id="refresh"><span class="lbl"><span><span class="live" aria-hidden="true"></span> Observed</span> <kbd>r</kbd></span><span class="val"><span class="ph"><b>${DOC ? DOC.read.slice(11, 16) : '—'}</b>${DOC ? ' UTC' : ''}</span><small>refresh</small></span></button></li>`;
    put($('annunciator'), html`${CELLS.map(c => html`<li><button class="cell" type="button" data-cell="${c.id}" data-state="${c.state}"${c.open ? html` data-open="${c.open}"` : ''}><span class="lbl">${c.label} <span class="${c.state === 'ok' ? 'g' : ''}" aria-hidden="true">${GLYPH[c.state]}</span></span><span class="val"><span class="ph">${c.val}</span><small>${c.small}</small></span></button></li>`)}${clock}`);
  }
  function attention() {
    const lit = CELLS.filter(c => c.state === 'warning' || c.state === 'caution').sort((a, b) => RANK[b.state] - RANK[a.state]);
    const worst = lit[0]?.state;
    window.PAGE_ATTENTION = worst ? { state: worst, n: lit.filter(c => c.state === worst).length, what: 'HOA annunciator cells' } : { state: 'ok', n: 0, what: 'HOA water system' };
    window.shell.attention(window.PAGE_ATTENTION);
  }

  // ---------- Map ----------
  // build: the design's glyphs, drawn into one SVG. Coordinates project equirectangular around the HOA's middle latitude: at
  // this size the error is far below a glyph. Glyphs keep their screen size at every zoom.
  const TYPES = {
    tank:    { label: 'Tank',    size: 26, svg: html`<path class="halo" d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><ellipse class="halo" cx="12" cy="6" rx="8" ry="3"/><path class="body" d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6c0 1.7-3.6 3-8 3S4 7.7 4 6z"/><ellipse class="body" cx="12" cy="6" rx="8" ry="3" fill-opacity=".55"/>` },
    well:    { label: 'Well',    size: 22, svg: html`<circle class="halo" cx="12" cy="12" r="8"/><circle class="body" cx="12" cy="12" r="8" fill-opacity=".2"/><circle class="body" cx="12" cy="12" r="3.2"/>` },
    hydrant: { label: 'Hydrant', size: 18, svg: html`<path class="halo" d="M8 8a4 4 0 0 1 8 0v11H8z"/><path class="body" d="M8 8a4 4 0 0 1 8 0v11H8z"/><path class="body" d="M5 11h3v4H5zM16 11h3v4h-3zM6 19h12v2.5H6z"/>` },
    valve:   { label: 'Valve',   size: 16, svg: html`<path class="halo" d="M3 6l9 6-9 6zM21 6l-9 6 9 6z"/><path class="body" d="M3 6l9 6-9 6zM21 6l-9 6 9 6z"/><path class="line" d="M12 12V4M9 4h6"/>` },
  };
  const ORDER = ['tank', 'well', 'hydrant', 'valve'];
  const glyph = type => html`<svg viewBox="0 0 24 24" aria-hidden="true">${TYPES[type].svg}</svg>`;
  const stClass = s => s === 'in service' ? 'st-ok' : s === 'unknown' ? 'st-unknown' : 'st-out';
  const W = 1000, ZOOMS = [1, 2, 4, 8, 16, 32];

  function project() {
    const pts = [...LOTS.flatMap(l => l.rings.flat()), ...ASSETS.map(a => [a.lng, a.lat])];
    if (!pts.length) return null;
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const k = Math.cos(((minY + maxY) / 2) * Math.PI / 180), span = Math.max((maxX - minX) * k, maxY - minY) || 1e-6, s = W * 0.94 / span;
    const ox = (W - (maxX - minX) * k * s) / 2, h = (maxY - minY) * s + W * 0.06;
    return { x: lng => ox + (lng - minX) * k * s, y: lat => W * 0.03 + (maxY - lat) * s, h: Math.max(h, W * 0.4) };
  }
  function view() {
    const w = W / zoom, h = P.h / zoom, c = center || [W / 2, P.h / 2];
    return [c[0] - w / 2, c[1] - h / 2, w, h];
  }
  function drawMap() {
    const box = $('map');
    if (!P) { put(box, unknownBlock(DOC ? [DOC.base.error, DOC.assets.error].filter(Boolean).join(' · ') || 'the map files hold no features' : 'the HOA document did not arrive')); return; }
    const [vx, vy, vw, vh] = view(), u = vw / (box.clientWidth || W);
    const ring = r => `M${r.map(([x, y]) => `${P.x(x).toFixed(1)},${P.y(y).toFixed(1)}`).join('L')}Z`;
    put(box, html`<svg class="map" viewBox="${vx} ${vy} ${vw} ${vh}" preserveAspectRatio="xMidYMid meet" aria-label="HOA water assets map">
      ${LOTS.map(l => html`<path class="lot k-${l.kind}"${l.kind === 'tract' ? html` data-tract="${l.ain || l.label}"` : ''} d="${l.rings.map(ring).join('')}"><title>${l.label || l.kind}</title></path>`)}
      ${ASSETS.filter(a => !hidden.has(a.type) && TYPES[a.type]).map(a => { const t = TYPES[a.type], g = t.size * u;
        return html`<g class="asset t-${a.type} ${stClass(a.status)}" data-asset="${a.id}" tabindex="0" role="button" aria-label="${a.name}, ${a.type}, ${a.status}"${a.id === selected?.slice(6) ? html` aria-current="true"` : ''} transform="translate(${(P.x(a.lng) - g / 2).toFixed(2)} ${(P.y(a.lat) - g / 2).toFixed(2)}) scale(${(g / 24).toFixed(4)})"><title>${a.name} · ${a.status}</title>${TYPES[a.type].svg}</g>`; })}
    </svg>`);
  }
  function zoomTo(z, at) { zoom = Math.max(ZOOMS[0], Math.min(ZOOMS[ZOOMS.length - 1], z)); if (at) center = at; drawMap(); }
  function legend() {
    const byType = {}, byStatus = {};
    ASSETS.forEach(a => { byType[a.type] = (byType[a.type] || 0) + 1; byStatus[a.status] = (byStatus[a.status] || 0) + 1; });
    put($('legend'), html`${ORDER.filter(t => byType[t]).map(t => html`<li><button type="button" data-type="${t}" aria-pressed="${hidden.has(t) ? 'false' : 'true'}">${glyph(t)}${TYPES[t].label}s <b>${byType[t]}</b></button></li>`)}<li class="sep" aria-hidden="true"></li>${
      Object.entries(byStatus).map(([s, n]) => html`<li class="status"><i class="${s === 'in service' ? '' : s === 'unknown' ? 'unk' : 'out'}" aria-hidden="true"></i>${s} <b class="data">${n}</b></li>`)}`);
  }

  // ---------- Details ----------
  function show(m, id, open) {
    put($('details'), m);
    window.shell.commands.select(id);
    if (open) window.shell.openPane('tab-details');
  }
  function mark() {
    document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === selected ? 'true' : 'false'));
  }
  function selectAsset(id, open) {
    const a = ASSETS.find(x => `asset:${x.id}` === id); if (!a) return;
    selected = id; mark();
    if (open && P) { center = [P.x(a.lng), P.y(a.lat)]; zoom = Math.max(zoom, 4); }
    drawMap();
    const m = DOC.mission, g = a.status === 'in service' ? ['g-ok', '●'] : a.status === 'unknown' ? ['g-unknown', '▨'] : ['g-queued', '◌'];
    let extra = '';
    // build: the design keyed these on two sample ids; here a tank reads the analog file and the A2 well the pump file.
    if (a.type === 'well' && /A2/.test(a.id)) {
      const lp = m.pump[m.pump.length - 1], a2 = AL.filter(x => /A2/.test(x.event));
      extra = html`${lp ? html`<h3 class="label">Latest pump row · ${lp.date}</h3>${dl([['Runtime', `${lp.a2_runtime_hours} h`], ['Starts', lp.a2_starts], ['Gallons', gal(lp.a2_gallons)], ['Flow running', `${lp.a2_flow_gpm_running} gpm`]])}` : ''}
        <h3 class="label">A2 alarms · last 30 days of export</h3>${a2.length ? html`<ul class="note">${a2.slice(0, 12).map(x => html`<li><code>${x.datetime.slice(0, 16)}</code> ${x.event}</li>`)}</ul>` : html`<p class="note">None.</p>`}`;
    }
    if (a.type === 'tank') {
      const la = m.analog[m.analog.length - 1];
      if (la) extra = html`<h3 class="label">Latest level · ${la.date}</h3>${dl([['Mean', `${la.tank_ft_mean} ft`], ['Min–max', `${la.tank_ft_min}–${la.tank_ft_max} ft`], ['Samples', la.samples]])}`;
    }
    show(html`<div class="kind"><span class="${g[0]}" aria-hidden="true">${g[1]}</span><span class="label">${TYPES[a.type]?.label || a.type} · ${a.id}</span></div>
      <h2>${a.name}</h2>
      ${dl([['Status', a.status], ['Accuracy', a.accuracy], ['Position', `${a.lat.toFixed(5)}, ${a.lng.toFixed(5)}`], ['Updated', a.updated], ['Source', a.source]])}
      ${a.notes ? html`<p class="note">${a.notes}</p>` : ''}${extra}
      <h3 class="label">Act</h3>${window.shell.commands.bar(id)}`, id, open);
  }
  function selectTract(key, open) {
    const l = LOTS.find(x => (x.ain || x.label) === key); if (!l) return;
    show(html`<div class="kind"><span class="label">Tract</span></div><h2>${l.label || `Tract ${l.ain}`}</h2>${dl([['AIN', l.ain], ['Land use', l.luc], ['Acres', l.acres], ['Source', 'base.geojson']])}`, null, open);
  }
  function selectOb(id, open) {
    const o = OB.find(x => x.id === id); if (!o) return;
    selected = id; mark(); drawMap();
    show(html`<div class="kind"><span class="g-${o.state}" aria-hidden="true">${GLYPH[o.state]}</span><span class="label">Followup · sd:${o.item}</span></div>
      <h2>${o.title}</h2>${dl([['Due', o.due ? `${o.due.slice(0, 10)} · ${dueText(o)}` : 'no due date'], ['Priority', o.priority == null ? '' : `P${o.priority}`], ['Status', o.status], ['Item', `sd:${o.item}`], ['Source', 'sd item, kind followup']])}
      <h3 class="label">Act</h3>${window.shell.commands.bar(id)}
      <p class="why">Drafting asks the HOA chat for an email. Nothing is sent: the HOA rule is no external action without your approval.</p>`, id, open);
  }
  function selectAlarm(id, open) {
    const a = AL.find(x => x.id === id); if (!a) return;
    selected = id; mark(); drawMap();
    show(html`<div class="kind"><span class="g-${a.st}" aria-hidden="true">${GLYPH[a.st]}</span><span class="label">Alarm event · ${a.kind}</span></div><h2>${a.event}</h2>
      ${dl([['When', `${a.datetime} (unit clock)`], ['Reads as', a.why], ['Minutes', a.minutes], ['Notified', a.notified], ['Source', 'alarm-events.csv']])}`, id, open);
  }
  function selectSource(open) {
    const m = DOC.mission;
    selected = 'source:mission'; mark();
    show(html`<div class="kind"><span class="g-${m.stale ? 'caution' : m.latest ? 'ok' : 'unknown'}" aria-hidden="true">${GLYPH[m.stale ? 'caution' : m.latest ? 'ok' : 'unknown']}</span><span class="label">Source · Mission export</span></div>
      <h2>Mission telemetry export</h2>
      ${dl([['Path', m.dir], ['Cadence', 'daily export, one row per day'], ['State', m.latest ? `${m.stale ? 'stale' : 'current'} · newest row ${m.latest} · ${m.age_days} d old` : 'not read']])}
      <h3 class="label">Files in this page</h3>${dl(m.files.map(f => [f.name, f.error ? `not read: ${f.error}` : `${f.rows} rows · latest ${f.latest.slice(0, 10)}`]))}
      <p class="note">Tank level and Aquifer read analog-daily.csv; Pumps reads pump-daily.csv; Alarms reads alarm-events.csv. The pump export lags the analog export by one day.</p>`, 'source:mission', open);
  }
  function selectDay(d, open, series) {
    const m = DOC.mission, a = m.analog.find(r => r.date === d), p = m.pump.find(r => r.date === d);
    selected = `day:${d}`; selDay = d; mark(); drawTrends();
    if (series) $('trend-list').querySelector(`.scrub[data-series="${series}"]`)?.focus();
    const C = window.shell.commands;
    if (!C.get(selected)) C.put({ id: selected, type: 'day', label: `day ${d}` });
    show(html`<div class="kind"><span class="label">Mission day</span></div><h2>${d}</h2>
      <h3 class="label">analog-daily.csv</h3>${a ? dl([['Tank', `${a.tank_ft_mean} ft (${a.tank_ft_min}–${a.tank_ft_max})`], ['Aquifer', `${a.aquifer_ft_mean} ft (${a.aquifer_ft_min}–${a.aquifer_ft_max})`], ['A2 flow', `${a.a2_flow_gpm_mean} gpm mean`], ['Booster', `${a.booster_psi_mean} psi mean`], ['Samples', a.samples]]) : html`<p class="note">No analog row for this day.</p>`}
      <h3 class="label">pump-daily.csv</h3>${p ? dl([['A2 runtime', `${p.a2_runtime_hours} h`], ['Starts', p.a2_starts], ['Gallons', gal(p.a2_gallons)], ['Metered', gal(p.a2_gallons_metered)]]) : html`<p class="note">No pump row for this day yet; the export lags one day.</p>`}`, selected, open);
  }
  function select(id, open) {
    if (!id) return;
    if (id.startsWith('asset:')) selectAsset(id, open);
    else if (id.startsWith('followup:')) selectOb(id, open);
    else if (id.startsWith('alarm:')) selectAlarm(id, open);
    else if (id.startsWith('day:')) selectDay(id.slice(4), open);
    else if (id === 'source:mission') selectSource(open);
  }

  // ---------- Trends ----------
  const SERIES = [
    { key: 'tank', title: 'Tank level', unit: 'ft', file: 'analog', y: 'tank_ft_mean', lo: 'tank_ft_min', hi: 'tank_ft_max', kind: 'line', src: 'analog-daily.csv' },
    { key: 'runtime', title: 'A2 pump runtime', unit: 'h', file: 'pump', y: 'a2_runtime_hours', kind: 'bar', src: 'pump-daily.csv', zero: true },
    { key: 'aquifer', title: 'Aquifer', unit: 'ft', file: 'analog', y: 'aquifer_ft_mean', lo: 'aquifer_ft_min', hi: 'aquifer_ft_max', kind: 'line', src: 'analog-daily.csv' },
  ];
  const TW = 300, TH = 72;
  const rowsOf = s => (DOC ? DOC.mission[s.file] : []).filter(r => num(r[s.y]) && (!s.lo || (num(r[s.lo]) && num(r[s.hi])))).slice(-range);
  function spark(s) {
    const rows = rowsOf(s);
    if (!rows.length) return html`<figure class="spark" data-series="${s.key}"><header><span class="label">${s.title}</span></header>${unknownBlock(DOC?.mission.error || `${s.src} has no rows`)}</figure>`;
    const ys = rows.flatMap(r => [+r[s.y], s.lo ? +r[s.lo] : +r[s.y], s.hi ? +r[s.hi] : +r[s.y]]);
    let min = s.zero ? 0 : Math.min(...ys), max = Math.max(...ys); if (max === min) max = min + 1;
    const n = rows.length, step = TW / n;
    const X = i => (i + 0.5) * step, Y = v => TH - ((v - min) / (max - min)) * (TH - 6) - 3;
    const marks = [];
    if (s.kind === 'line') {
      if (s.lo) marks.push(html`<path class="band" d="M${rows.map((r, i) => `${X(i)},${Y(+r[s.hi])}`).join('L')}L${rows.map((r, i) => `${X(i)},${Y(+r[s.lo])}`).reverse().join('L')}Z"/>`);
      marks.push(html`<polyline class="ln" points="${rows.map((r, i) => `${X(i)},${Y(+r[s.y])}`).join(' ')}"/>`);
      marks.push(html`<circle class="last" cx="${X(n - 1)}" cy="${Y(+rows[n - 1][s.y])}" r="2.5"/>`);
    } else {
      marks.push(rows.map((r, i) => html`<rect class="bar${r.date === selDay ? ' sel' : ''}" x="${X(i) - step * 0.35}" y="${Y(+r[s.y])}" width="${step * 0.7}" height="${TH - 3 - Y(+r[s.y])}"/>`));
    }
    const picked = rows.find(r => r.date === selDay);
    const hits = html`${s.kind === 'line' ? rows.map((r, i) => html`<circle class="mk${r.date === selDay ? ' sel' : ''}" cx="${X(i)}" cy="${Y(+r[s.y])}" r="3.5"/>`) : ''}<rect class="hit scrub" x="0" y="0" width="${TW}" height="${TH}" tabindex="0" role="button" data-series="${s.key}" data-n="${n}" aria-label="${s.title}, ${n} days. ${picked ? `${picked.date}: ${picked[s.y]} ${s.unit}. ` : ''}Tap a day, or use the left and right arrow keys."/>`;
    const last = rows[n - 1];
    return html`<figure class="spark" data-series="${s.key}">
      <header><span class="label">${s.title}</span><span class="now">${(+last[s.y]).toFixed(1)} <small>${s.unit} · ${day(last.date)}</small></span></header>
      <svg viewBox="0 0 ${TW} ${TH}" preserveAspectRatio="none" role="img" aria-label="${s.title}, ${n} days">
        <line class="grid" x1="0" x2="${TW}" y1="${TH - 3}" y2="${TH - 3}"/>${marks}${hits}</svg>
      <div class="axis"><span>${day(rows[0].date)}</span><span>${min.toFixed(s.zero ? 0 : 1)}–${max.toFixed(1)} ${s.unit}</span><span>${day(last.date)}</span></div>
      <div class="sr"><table><caption>${s.title} (${s.unit}), from ${s.src}</caption><tbody>${rows.map(r => html`<tr><th scope="row">${r.date}</th><td>${r[s.y]}</td></tr>`)}</tbody></table></div>
    </figure>`;
  }
  const drawTrends = () => put($('trend-list'), html`${SERIES.map(spark)}`);
  const daysOf = sc => rowsOf(SERIES.find(x => x.key === sc.dataset.series)).map(r => r.date);

  // ---------- Ledgers ----------
  // build: the obligations are the checkout's open followup items (hoa_screen ranks them: overdue, due within a week, the rest).
  // Every row is real; the design's sample rows are gone.
  function drawLedgers() {
    const C = window.shell.commands, f = DOC?.followups;
    put($('ob-body'), f?.error ? html`<tr><td colspan="5">${unknownBlock(f.error)}</td></tr>`
      : OB.length ? html`${OB.map(o => html`<tr data-id="${o.id}" aria-selected="${o.id === selected ? 'true' : 'false'}">
        <td class="g g-${o.state}" aria-label="${o.state}">${GLYPH[o.state]}</td>
        <td class="what"><button type="button">${o.title}</button> <span class="tag real">${o.priority == null ? '' : `P${o.priority} `}#${o.item}</span><div class="detail">${dueText(o)}</div></td>
        <td class="detail hide-sm">${dueText(o)}</td><td class="src hide-sm">${o.status}</td>
        <td class="act">${C.rowActions(o.id)}</td></tr>`)}`
      : html`<tr><td colspan="5" class="why">No open followup item in the checkout.</td></tr>`);
    const m = DOC?.mission, alErr = m?.files.find(x => x.name === 'alarm-events.csv')?.error;
    put($('al-body'), !DOC ? html`` : alErr ? html`<tr><td colspan="6">${unknownBlock(alErr)}</td></tr>`
      : AL.length ? html`${AL.map(a => html`<tr data-id="${a.id}" aria-selected="${a.id === selected ? 'true' : 'false'}"><td class="g g-${a.st}" aria-label="${a.st}">${GLYPH[a.st]}</td><td class="what"><button type="button">${a.event}</button></td><td class="src">${a.datetime.slice(5, 16)}</td><td class="detail hide-sm">${a.kind}</td><td class="num hide-sm">${a.minutes}</td><td class="detail hide-sm">${a.notified}</td></tr>`)}`
      : html`<tr><td colspan="6" class="why">No alarm event in the last 30 days of the export.</td></tr>`);
  }
  const alarmState = a => a.kind === 'generator' ? ['queued', 'weekly generator test'] : /Normal/.test(a.event) ? ['ok', 'return to normal'] : ['warning', 'alarm'];

  function draw() { lamps(); drawMap(); legend(); drawTrends(); drawLedgers(); }

  // ---------- Reader (read.js) ----------
  let reader = null;
  const load = () => reader.load();
  const spec = {
    source: '/api/hoa', what: 'the HOA readings',
    adopt: doc => {
      DOC = doc; M = doc.mission;
      ASSETS = doc.assets.features; LOTS = doc.base.features;
      OB = doc.followups.rows;
      AL = M.alarms.slice().reverse().map((a, i) => { const [st, why] = alarmState(a); return { ...a, id: `alarm:${a.datetime}:${i}`, st, why }; });
      P = project(); if (!P) { zoom = 1; center = null; }
      CELLS = cells(); attention();
      document.body.dataset.observed = doc.read;
      const c = doc.config, failed = [M.error && `Mission export: ${M.error}`, doc.assets.error && `Assets: ${doc.assets.error}`, doc.base.error && `Base map: ${doc.base.error}`, doc.followups.error && `Followups: ${doc.followups.error}`].filter(Boolean);
      put($('subhead'), html`Water system: ${plural(ASSETS.length, 'mapped asset')}, ${plural(LOTS.length, 'lot and tract outline', 'lot and tract outlines')}; Mission export ${M.latest ? `through ${M.latest}` : 'not read'} · read <time class="rel" datetime="${doc.read}"></time>`);
      const objects = [...ASSETS.map(a => ({ id: `asset:${a.id}`, type: 'asset', label: `${a.id} · ${a.name}`, asset: a.id, name: a.name })),
        ...OB.map(o => ({ id: o.id, type: 'followup', label: `sd:${o.item} · ${o.title}`, item: o.item, what: o.title })),
        ...AL.map(a => ({ id: a.id, type: 'alarm', label: a.event })),
        ...(M.dir ? [{ id: 'source:mission', type: 'source', label: 'Mission export' }] : [])];
      const st = c.state === 'missing' ? { kind: 'empty', title: 'No HOA checkout', text: `Copy project-dashboard/hoa.conf.example to ${c.source} and name the HOA checkout.`, source: c.source }
        : c.problems.length ? { kind: 'partial', text: `${c.source}: ${c.problems.join(' · ')}.`, source: c.source }
        : failed.length ? { kind: 'partial', text: `${failed.join(' · ')}. The other parts are current.`, source: '/api/hoa' } : null;
      return { objects, state: st };
    },
    clear: () => {
      DOC = null; M = null; ASSETS = []; LOTS = []; OB = []; AL = []; P = null; CELLS = [];
      delete document.body.dataset.observed;
      put($('subhead'), html`The HOA readings could not be read.`);
      window.shell.attention({ state: 'unknown', n: 0, what: 'HOA not read' });
    },
    draw,
    current: () => selected,
    first: () => OB[0]?.id,
    select: (id, opened) => select(id, opened),
    unselect: () => { selected = null; mark(); put($('details'), html`<p class="note">${DOC ? 'Select a marker, a followup, an alarm or a trend day to see it here.' : 'Nothing is selected: the HOA readings could not be read.'}</p>`); },
  };

  window.PAGE_COMMANDS = [
    { label: 'Read HOA again', icon: 'rotate-ccw', run: () => load() },
    { label: 'Fit the map to the HOA', icon: 'maximize-2', run: () => { center = null; zoomTo(1); } },
  ];
  // The shell walks the ledger rows on j / k; a row selects by its own click.
  window.PAGE_LIST = { rows: () => document.querySelectorAll('.ledger tbody tr[data-id]'), current: () => selected, select: id => select(id, false) };

  document.addEventListener('DOMContentLoaded', () => {
    const S = window.shell, C = S.commands;
    reader = S.read(spec);
    // ---------- Commands (products/system/commands.md) ----------
    // Drafting is safe: it only asks in chat. Email is never sent from the dashboard.
    const openItem = o => { const to = S.pages?.Tasks; if (!to) return 'Tasks is not built yet'; location.href = `${to}?row=${o.item}`; return `sd:${o.item} opens in Tasks`; };
    C.register(
      { id: 'followup.draft', on: 'followup', label: 'Draft followup', key: 'd', risk: 'safe', primary: () => true,
        run: o => { S.openChat(); S.send(`Draft a followup for sd:${o.item}: ${o.what}`); return 'Asked in chat · nothing is sent'; } },
      { id: 'followup.open', on: 'followup', label: 'Open item', key: 'o', risk: 'safe', journal: o => o.item, cli: o => `sd task show ${o.item}`, run: openItem },
      { id: 'asset.ask', on: 'asset', label: 'Ask', key: 'a', risk: 'safe', primary: () => true,
        run: o => { S.openChat(); S.send(`What do we know about ${o.asset} (${o.name})?`); return ''; } },
    );

    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      if (c.id === 'refresh') { load(); return; }
      const k = c.dataset.open; if (!k) return;
      if (k.startsWith('day:')) $('trends').scrollIntoView({ behavior: reduce() ? 'auto' : 'smooth' });
      select(k, true);
    });
    $('map').addEventListener('click', e => {
      if (dragged) return;
      const a = e.target.closest('[data-asset]'); if (a) { select(`asset:${a.dataset.asset}`, true); return; }
      const t = e.target.closest('[data-tract]'); if (t) selectTract(t.dataset.tract, true);
    });
    $('map').addEventListener('keydown', e => {
      const a = e.target.closest('[data-asset]');
      if (a && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.stopPropagation(); select(`asset:${a.dataset.asset}`, true); }
    });
    // Drag pans: a pointer move turns into viewBox units at the map's drawn width.
    let drag = null, dragged = false;
    $('map').addEventListener('pointerdown', e => { if (!P || zoom === 1) return; drag = { x: e.clientX, y: e.clientY, c: view() }; dragged = false; });
    $('map').addEventListener('pointermove', e => {
      if (!drag) return;
      const u = drag.c[2] / ($('map').clientWidth || W), dx = (e.clientX - drag.x) * u, dy = (e.clientY - drag.y) * u;
      if (Math.abs(dx) + Math.abs(dy) > 2 * u) dragged = true;
      center = [drag.c[0] + drag.c[2] / 2 - dx, drag.c[1] + drag.c[3] / 2 - dy]; drawMap();
    });
    addEventListener('pointerup', () => { drag = null; setTimeout(() => { dragged = false; }, 0); });
    $('fit').addEventListener('click', () => { center = null; zoomTo(1); });
    $('zoom-in').addEventListener('click', () => zoomTo(zoom * 2));
    $('zoom-out').addEventListener('click', () => { zoomTo(zoom / 2); if (zoom === 1) { center = null; drawMap(); } });
    $('legend').addEventListener('click', e => {
      const b = e.target.closest('button[data-type]'); if (!b) return;
      hidden.has(b.dataset.type) ? hidden.delete(b.dataset.type) : hidden.add(b.dataset.type);
      legend(); drawMap();
    });
    $('trend-list').addEventListener('click', e => {
      const sc = e.target.closest('.scrub'); if (!sc) return;
      const r = sc.getBoundingClientRect(), days = daysOf(sc), i = Math.max(0, Math.min(days.length - 1, Math.floor((e.clientX - r.left) / r.width * days.length)));
      selectDay(days[i], true, sc.dataset.series);
    });
    // The chart's own keys stop here, so the shell's Enter (shell:open) does not also run.
    $('trend-list').addEventListener('keydown', e => {
      const sc = e.target.closest('.scrub'); if (!sc || !['ArrowLeft', 'ArrowRight', 'Enter', ' '].includes(e.key)) return;
      e.preventDefault(); e.stopPropagation();
      const days = daysOf(sc), at = days.indexOf(selDay), i = at < 0 ? days.length - 1 : at + (e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0);
      selectDay(days[Math.max(0, Math.min(days.length - 1, i))], true, sc.dataset.series);
    });
    $('range').addEventListener('click', e => {
      const c = e.target.closest('.chip'); if (!c) return;
      if (c.getAttribute('aria-disabled') === 'true') { S.toast('Daily data: 24h needs the hourly Mission export.'); return; }
      range = +c.dataset.range;
      $('range').querySelectorAll('.chip').forEach(x => x.setAttribute('aria-pressed', x === c ? 'true' : 'false'));
      drawTrends();
    });
    ['ob-body', 'al-body'].forEach(b => $(b).addEventListener('click', e => {
      if (e.target.closest('.rowact')) return;
      const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
    }));
    document.addEventListener('shell:open', e => select(e.detail, true));
    S.suggest(['Which HOA followup is most overdue, and who is it waiting on?', 'How did A2 pump runtime change over the last 30 days?', 'Which assets are not in service?']);
    load();
  });
})();
// Top level, not inside DOMContentLoaded: shell.js builds the ? sheet from PAGE_KEYS when it loads.
window.PAGE_KEYS = [['↵', 'On a map asset: show it in Details'], ['← / →', 'On a trend: the previous or next day, shown in Details']];
