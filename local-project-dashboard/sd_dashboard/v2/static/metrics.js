// Metrics (sd:2119): the design source's products/system/designs/pages/metrics.js, ported. Each change from the reference is
// marked "build:". The readings are /api/metrics (metrics_screen.py), read through the shared reader; none is sample data.
// A part that did not read, and each panel no reader covers, is unknown with its reason.
// Writes (sd:3387): Cap and Configure post /api/bills/<bill>/cap and /api/providers/configure with the revision the page
// read (controls.revision). A 400 or 409 wrote nothing; any other failure, or no answer, leaves the outcome unknown.
// build: bars are SVG rects sized by attribute, not CSSOM widths, and the age chart is the design's bar list only.
// sd:5080: the panels are the shell's views (shell.js, view:start): Spend (spend, the week, usage), Providers, CI, Skills
// and Age. A lamp shows the view that holds its reading.
window.PAGE_VIEWS = [{ id: 'spend', label: 'Spend' }, { id: 'providers', label: 'Providers' }, { id: 'ci', label: 'CI' },
  { id: 'skills', label: 'Skills', key: 'k' }, { id: 'age', label: 'Age' }];
(() => {
  const { html, put, plural, cells } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { ok: '●', caution: '▲', warning: '■', unknown: '▨', queued: '◌' };
  const RANK = { warning: 3, caution: 2, unknown: 1, ok: 0 };
  const money = v => v == null ? '—' : '$' + (+v).toFixed(2);
  const fmt = n => (+n || 0).toLocaleString('en-US');
  const pctState = p => p >= 100 ? 'warning' : p >= 75 ? 'caution' : 'ok';
  const unknownBlock = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;
  const track = (pct, cls = '') => html`<svg class="track${pct == null ? ' hatch' : ''}" viewBox="0 0 100 1" preserveAspectRatio="none" aria-hidden="true">${pct == null ? '' : html`<rect class="fill ${cls}" width="${Math.max(0, Math.min(100, pct)).toFixed(1)}" height="1"/>`}</svg>`;
  let DOC = null, EV = {}, CELLS = [], selected = null, FIRST = null, SAID = null;
  // The registry's controls, by bill and by provider; a part that did not read leaves both empty.
  const ctlBill = name => DOC?.controls?.bills?.find(b => b.name === name) || null;
  const ctlProvider = name => DOC?.controls?.providers?.find(p => p.name === name) || null;

  // ---------- Evidence: every bar, row and cell names its facts and its CLI line ----------
  function evidence() {
    EV = {};
    const d = DOC, u = d.usage, put1 = (id, e) => { EV[id] = e; return id; };
    if (!u.error) {
      u.bills.forEach(b => put1(`bill:${b.name}`, { type: 'bill', kind: `Bill · ${u.month}`, title: `${b.name} · ${b.cost_basis}`, label: `${b.name} bill`, bill: b.name, basis: b.cost_basis, cap: b.cap,
        revision: d.controls.revision, control: ctlBill(b.name),
        facts: [['Spent', money(b.spent)], ['Estimated', money(b.estimated)], ['Held', money(b.held)], ['Cap', b.cap == null ? 'none' : money(b.cap)], ['Room', b.room == null ? '—' : money(b.room)], ['Source', 'usage.read: cost run + bound rows']], cli: `sd-db usage --month ${u.month}` }));
      u.meter.forEach(m => put1(`win:${m.provider}:${m.window_minutes}`, { kind: 'Budget window', title: `${m.provider} · ${m.window_minutes} min`, s: pctState(m.used_percent),
        facts: [['Used', `${m.used_percent}%`], ['Bill', m.bill], ['Read', m.timestamp], ['Source', 'cost, source meter']], cli: 'sd-db usage --json' }));
      u.roles.forEach((r, i) => put1(`role:${i}`, { kind: `Usage · ${u.month}`, title: `${r.bill || '-'} · ${r.provider || '-'} · ${r.role || '-'}`,
        facts: [['Calls', r.calls], ['Spent', money(r.spent)], ['Held', money(r.held)], ['Tokens in', fmt(r.tokens_in)], ['Tokens out', fmt(r.tokens_out)]], cli: `sd-db usage --month ${u.month}` }));
      put1('spend', { kind: `Spend · ${u.month}`, title: `${money(u.spent)} spent, ${money(u.held)} held`, facts: u.bills.map(b => [b.name, `${money(b.spent)}${b.cap == null ? '' : ` of ${money(b.cap)}`}`]),
        note: 'Dollars only. Subscription and plan bills spend windows, shown beside the dollars.', cli: `sd-db usage --month ${u.month}` });
      put1('bound', { kind: 'Usage · bound rows', title: `${plural(u.bound.length, 'bound row')}`, s: u.bound.length ? 'caution' : 'ok',
        facts: u.bound.map(r => [`${r.bill || '—'} · ${r.provider || '—'}`, `${money(r.usd)} · ${r.timestamp}`]),
        note: 'Money a provider may have billed for a lost response; counted at the bound until corrected against the invoice.', cli: `sd-db usage --month ${u.month}` });
    }
    if (!d.trend.error) d.trend.series.forEach(s => s.points.forEach(([day, p, at], i) => put1(`t:${s.provider}:${day}`, { kind: 'Window reading', title: `${s.provider} · ${day}`, s: pctState(p),
      facts: [['Used', `${p}%`], ['Window', `${d.trend.window} min (7 days)`], ['Reading', `last meter row of the day · ${at}`], ['Source', 'reads.meter_days']], cli: 'sd-db usage --json' })));
    if (!d.numbers.error) d.numbers.rows.forEach(n => put1(`num:${n.key}`, { kind: 'The week', title: n.label, s: n.value == null ? 'unknown' : 'ok', facts: [['Value', numText(n)], ...n.inputs, ['Source', 'reads.weekly_numbers']], cli: '/operations?area=usage' }));
    const order = (role, name) => { const at = (d.controls.orders?.[role] || []).indexOf(name); return d.controls.error ? '— (not read)' : at < 0 ? 'not in the order' : `${at + 1} of ${d.controls.orders[role].length}`; };
    if (!d.scorecard.error) d.scorecard.rows.forEach(r => put1(`sc:${r.provider}`, { type: 'provider', kind: 'Provider · this month', title: r.provider, label: r.provider, p: r.provider, s: scState(r),
      revision: d.controls.revision, control: ctlProvider(r.provider),
      facts: [['Author order', order('author', r.provider)], ['Reviewer order', order('reviewer', r.provider)], ['Author rank', r.author_rank ?? '—'], ['Reviewer rank', r.reviewer_rank ?? '—'], ['Passes', r.passes], ['Blocking', r.blocking], ['$/pass', money(r.usd_per_pass)], ['Skipped', r.fallthrough], ['State', r.enabled ? 'enabled' : `disabled: ${r.reason || '—'}`]],
      note: scState(r) === 'caution' ? `Asked ${r.fallthrough} times and fell through every time this month.` : '', cli: 'sd providers configure --file CONFIG.json' }));
    if (!d.skills.error) {
      d.skills.weeks.forEach(w => put1(`week:${w.start}`, { kind: 'Skill use · week', title: `week of ${w.start}`, facts: [['Uses', w.uses], ['Source', 'reads.skill_use_days (Monday-start weeks)']], cli: 'sd skill list' }));
      d.skills.top.forEach(([n, v]) => put1(`skill:${n}`, { kind: 'Skill use · this week', title: n, facts: [['Uses this week', v], ['Source', 'reads.skill_use_days']], open: window.shell?.pages?.Skills ? [`${window.shell.pages.Skills}?row=${encodeURIComponent(n)}`, 'Open in Skills'] : null, cli: 'sd skill list' }));
    }
    if (!d.age.error) d.age.buckets.forEach(b => put1(`age:${b.lower}`, { kind: 'Age in status', title: `${b.ready_to_send + b.other} items at ${b.label}`,
      facts: [['Ready to send', b.ready_to_send], ['Other statuses', b.other], ['Scope', 'active items, done and parked excluded']], cli: 'sd task list' }));
    if (!d.observed.error) put1('observed', { kind: 'Observed activity', title: `${d.observed.since} → ${d.observed.until}`, facts: [['Requests recorded', d.observed.recorded_requests], ['Finished review attempts', d.observed.finished_review_attempts], ['Recorded deliveries', d.observed.recorded_deliveries], ['Source', 'reporting.metrics']], note: d.observed.interpretation, cli: '—' });
    d.unread.forEach(x => put1(`unread:${x.id}`, { kind: 'Not read', title: x.name, s: 'unknown', facts: [['Reason', x.reason]], cli: '—' }));
  }
  const numText = n => n.value == null ? '—' : n.unit === 'usd' ? money(n.value) : n.unit === 'hours' ? `${(+n.value).toFixed(1)} h` : n.value % 1 ? (+n.value).toFixed(1) : String(n.value);
  const scState = r => r.fallthrough && !r.passes ? 'caution' : 'ok';

  // ---------- Annunciator ----------
  function readCells() {
    // a stale month (sd:4955) keeps its numbers on show, and lights no lamp: each of its cells is unknown.
    const d = DOC, u = d.usage, out = [], stale = u.stale ? 'unknown' : '';
    out.push(u.error ? { id: 'spend', label: 'Spend month', view: 'spend', state: 'unknown', val: html`<b>—</b> not read`, small: u.error }
      : { id: 'spend', label: 'Spend month', view: 'spend', state: stale || 'ok', ev: 'spend', val: html`<b>${money(u.spent)}</b>`, small: `${u.month} to date · ${plural(u.bills.length, 'bill')}` });
    if (!u.error) u.meter.filter(m => m.window_minutes === d.trend.window).forEach(m => out.push({ id: `w-${m.provider}`, label: `${m.provider} week`, view: 'spend', state: stale || pctState(m.used_percent), ev: `win:${m.provider}:${m.window_minutes}`,
      val: html`<b>${Math.round(m.used_percent)}%</b> used`, small: `meter ${String(m.timestamp).slice(5, 16).replace('T', ' ')}Z` }));
    if (!u.error) out.push({ id: 'bound', label: 'Estimated spend', view: 'spend', state: stale || (u.bound.length ? 'caution' : 'ok'), ev: 'bound', val: html`<b>${u.bound.length}</b> bound`, small: u.bound.length ? 'rows to check against invoices' : 'no row to check' });
    out.push({ id: 'ci', label: 'CI 7d', view: 'ci', state: 'unknown', ev: 'unread:ci', val: html`<b>—</b> not read`, small: 'no local-gate store' });
    return out;
  }
  // the oldest reading on the page: a kept part (sd:4955) can be older than the request.
  const oldest = d => [d.read, d.usage.observed, d.numbers.observed].filter(Boolean).sort()[0];
  const hhmm = iso => `${iso.slice(11, 16)} UTC`;
  function lamps() {
    const clock = { button: true, attrs: html` id="refresh"`, label: 'Observed', mark: html`<kbd>r</kbd>`,
      val: html`<span class="ph"><b>${DOC ? oldest(DOC).slice(11, 16) : '—'}</b>${DOC ? ' UTC' : ''}</span>`, small: DOC ? `${oldest(DOC).slice(0, 10)} · refresh` : 'refresh' };
    put($('annunciator'), cells([...CELLS.map(c => ({ button: true, state: c.state, attrs: html` data-cell="${c.id}" data-ev="${c.ev || ''}" data-view="${c.view}"`,
      label: c.label, mark: html`<span class="${c.state === 'ok' ? 'g' : ''}" aria-hidden="true">${G[c.state]}</span>`, val: html`<span class="ph">${c.val}</span>`, small: c.small })), clock]));
  }
  // The badge is the read's own (sd:3446, metrics_screen._attention), the cells above that can light.
  function attention() {
    window.PAGE_ATTENTION = DOC && DOC.attention || { state: 'unknown', n: 0, what: 'Metrics not read' };
    window.shell.attention(window.PAGE_ATTENTION);
  }

  // ---------- Panels ----------
  const bar = (id, k, sub, pct, v, cls, glyph) => html`<li><button class="hbar" type="button" data-ev="${id}" aria-pressed="${id === selected ? 'true' : 'false'}"><span class="k">${k}${sub ? html`<small>${sub}</small>` : ''}</span>${track(pct, cls)}<span class="v">${glyph ? html`<span class="g-${glyph}" aria-hidden="true">${G[glyph]}</span>` : ''}${v}</span></button></li>`;
  function spend() {
    const u = DOC?.usage;
    if (!u || u.error) { put($('bills'), html`<li>${unknownBlock(u ? u.error : 'the Metrics document did not arrive')}</li>`); put($('windows'), html``); $('bills-note').textContent = ''; return; }
    // build: dollars are the month to date as sd usage reads them; a bill that spends a window shows hatched, not a zero.
    const windowed = b => ['subscription', 'plan'].includes(b.cost_basis), max = Math.max(0.01, ...u.bills.map(b => b.spent));
    // build (hallmark audit): a bill at 75% of its cap is caution and at its cap a warning, each with its glyph, not colour alone.
    const capState = b => b.cap == null ? 'ok' : b.spent >= b.cap ? 'warning' : b.spent / b.cap >= 0.75 ? 'caution' : 'ok';
    put($('bills'), html`${u.bills.map(b => { const st = capState(b); return bar(`bill:${b.name}`, b.name, `${b.cost_basis}${b.cap == null ? '' : ` · cap ${money(b.cap)}`}`, windowed(b) && !b.spent ? null : (b.cap ? b.spent / b.cap * 100 : b.spent / max * 100), windowed(b) && !b.spent ? '▨ window' : money(b.spent), st === 'ok' ? '' : st, st === 'ok' ? '' : st); })}`);
    $('bills-note').textContent = `Total ${money(u.spent)} spent and ${money(u.held)} held in ${u.month}. ▨ marks a bill that spends a vendor window, not a zero.`;
    put($('windows'), u.meter.length ? html`${u.meter.map(m => { const st = pctState(m.used_percent); return bar(`win:${m.provider}:${m.window_minutes}`, m.provider, `${m.window_minutes} min window`, m.used_percent, `${m.used_percent}%`, st === 'ok' ? 's1' : st, st === 'ok' ? '' : st); })}` : html`<li class="why">No meter reading yet.</li>`);
  }
  function trends() {
    const t = DOC?.trend;
    if (!t || t.error) { put($('trends'), unknownBlock(t ? t.error : 'the Metrics document did not arrive')); return; }
    if (!t.series.length) { put($('trends'), html`<p class="why">No ${t.window}-minute meter reading in the last ${t.days.length} days.</p>`); return; }
    const W = 300, H = 80, n = t.days.length, step = W / n, X = i => (i + 0.5) * step, Y = p => H - 4 - (Math.min(p, 110) / 110) * (H - 8);
    put($('trends'), html`${t.series.map(s => { const pts = s.points.map(([day, p]) => [t.days.indexOf(day), p, day]).filter(x => x[0] >= 0), last = s.points[s.points.length - 1];
      return html`<figure class="spark" data-series="${s.provider}"><header><span class="label">${s.provider} · 7-day window</span><span class="now">${last[1]}% <small>${String(last[2]).slice(5, 16).replace('T', ' ')}Z</small></span></header>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${s.provider} 7-day window, percent used, last reading per day ${t.days[0]} to ${t.days[n - 1]}">
        <line class="grid" x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}"/><line class="cap" x1="0" x2="${W}" y1="${Y(100)}" y2="${Y(100)}"/>
        <polyline class="ln" points="${pts.map(([i, p]) => `${X(i)},${Y(p)}`).join(' ')}"/>
        ${pts.map(([i, p, day]) => html`<circle class="${p >= 100 ? 'full' : 'mk'}${`t:${s.provider}:${day}` === selected ? ' sel' : ''}" cx="${X(i)}" cy="${Y(p)}" r="3.5" data-ev="t:${s.provider}:${day}" tabindex="0" role="button" aria-label="${s.provider} ${day}: ${p}%"/>`)}
      </svg><div class="axis"><span>${t.days[0].slice(5)}</span><span>dashed = 100%</span><span>${t.days[n - 1].slice(5)}</span></div>
      <table class="sr"><caption>${s.provider} 7-day window, percent used</caption><tbody>${s.points.map(([day, p]) => html`<tr><th scope="row">${day}</th><td>${p}%</td></tr>`)}</tbody></table></figure>`; })}`);
  }
  function unreadBlock(id) { const x = DOC?.unread.find(u => u.id === id); return x ? unknownBlock(`${x.reason}.`) : html``; }
  const row = (id, cells) => html`<tr data-ev="${id}" data-id="${id}" aria-selected="${id === selected ? 'true' : 'false'}">${cells}</tr>`;
  function tables() {
    const d = DOC, C = window.shell.commands;
    const nums = d && !d.numbers.error ? d.numbers.rows : [];
    put($('week-body'), !d ? html`` : html`${d.numbers.error ? html`<tr><td colspan="4">${unknownBlock(d.numbers.error)}</td></tr>` : ''}${nums.map(n => { const st = n.value == null ? 'unknown' : 'ok';
      return row(`num:${n.key}`, html`<td class="g g-${st}">${G[st]}<span class="sr">${st}</span></td><td class="what"><button type="button">${n.label}</button></td><td class="num">${numText(n)}</td><td class="mono hide-sm">${n.inputs.map(i => i.join(' ')).join(' · ')}</td>`); })}`);
    put($('sc-body'), !d ? html`` : d.scorecard.error ? html`<tr><td colspan="9">${unknownBlock(d.scorecard.error)}</td></tr>` : html`${d.scorecard.rows.map(r => { const st = scState(r);
      return row(`sc:${r.provider}`, html`<td class="g g-${st}">${G[st]}<span class="sr">${st === 'caution' ? 'caution: skipped every call' : 'normal'}</span></td><td class="what"><button type="button">${r.provider}</button>${r.enabled ? '' : html`<small>disabled</small>`}</td><td class="num hide-sm">${r.author_rank ?? '—'}</td><td class="num hide-sm">${r.reviewer_rank ?? '—'}</td><td class="num">${r.passes}</td><td class="num hide-sm">${r.blocking}</td><td class="num">${money(r.usd_per_pass)}</td><td class="num">${r.fallthrough}</td><td class="act">${C.rowActions(`sc:${r.provider}`)}</td>`); })}`);
    const u = d?.usage;
    put($('roles-body'), !d ? html`` : u.error ? html`<tr><td colspan="6">${unknownBlock(u.error)}</td></tr>` : u.roles.length ? html`${u.roles.map((r, i) => row(`role:${i}`, html`<td class="what"><button type="button">${r.bill || '—'}</button><small>${r.provider || '—'}</small></td><td class="hide-sm">${r.role || '—'}</td><td class="num">${r.calls}</td><td class="num">${money(r.spent)}</td><td class="num hide-sm">${fmt(r.tokens_in)}</td><td class="num hide-sm">${fmt(r.tokens_out)}</td>`))}`
      : html`<tr><td colspan="6" class="why">No call recorded in ${u.month} yet.</td></tr>`);
    $('usage-note').textContent = u && !u.error ? `${u.month}: ${money(u.spent)} spent, ${money(u.held)} held. ${u.bills.filter(b => b.cap != null).map(b => `${b.name} ${money(b.spent)} of its ${money(b.cap)} cap (room ${money(b.room)})`).join('; ')}`.trim() : '';
    $('us-h').textContent = u && !u.error ? `Usage · ${u.month}` : 'Usage';
  }
  function skills() {
    const s = DOC?.skills;
    if (!s || s.error) { put($('su-weeks'), html`<li>${unknownBlock(s ? s.error : 'the Metrics document did not arrive')}</li>`); put($('su-top'), html``); $('su-sub').textContent = ''; return; }
    $('su-sub').textContent = `From skill_use, Monday-start weeks since ${s.since}${s.first ? `; the first row is ${s.first}` : '; no row yet'}.`;
    const maxW = Math.max(1, ...s.weeks.map(w => w.uses)), maxT = Math.max(1, ...s.top.map(t => t[1]));
    put($('su-weeks'), html`${s.weeks.map((w, i) => bar(`week:${w.start}`, `wk ${w.start.slice(5)}`, i ? '' : 'this week', w.uses / maxW * 100, w.uses, i ? '' : 's1'))}`);
    put($('su-top'), s.top.length ? html`${s.top.map(([n, v]) => bar(`skill:${n}`, n, '', v / maxT * 100, v, ''))}` : html`<li class="why">No skill use this week.</li>`);
  }
  function age() {
    const a = DOC?.age;
    if (!a || a.error) { put($('hist'), unknownBlock(a ? a.error : 'the Metrics document did not arrive')); return; }
    const max = Math.max(1, ...a.buckets.map(b => b.ready_to_send + b.other));
    put($('hist'), html`<ul class="hbars" aria-label="Open items by days in their current status">${a.buckets.map(b => { const tot = b.ready_to_send + b.other;
      return html`<li><button class="hbar" type="button" data-ev="age:${b.lower}" aria-pressed="${`age:${b.lower}` === selected ? 'true' : 'false'}" aria-label="${b.label}: ${tot} items, ${b.ready_to_send} ready to send"><span class="k">${b.label}<small>${b.ready_to_send ? `${b.ready_to_send} ready to send` : ''}</small></span><svg class="track" viewBox="0 0 100 1" preserveAspectRatio="none" aria-hidden="true"><rect class="fill s1" width="${(b.ready_to_send / max * 100).toFixed(1)}" height="1"/><rect class="fill" x="${(b.ready_to_send / max * 100).toFixed(1)}" width="${(b.other / max * 100).toFixed(1)}" height="1"/></svg><span class="v">${tot}</span></button></li>`; })}</ul>
      <table class="sr"><caption>Open items by age in status</caption><tbody>${a.buckets.map(b => html`<tr><th scope="row">${b.label}</th><td>${b.ready_to_send} ready to send, ${b.other} other</td></tr>`)}</tbody></table>`);
  }
  function draw() {
    lamps(); spend(); trends(); tables(); skills(); age();
    put($('model'), unreadBlock('model'));
    put($('ci-state'), html`${unreadBlock('ci')}<p class="sub gap">Flaky tests</p>${unreadBlock('flaky')}`);
    const o = DOC?.observed;
    put($('observed'), !DOC ? html`` : o.error ? unknownBlock(o.error) : html`<p class="foot-note">Observed ${o.since} → ${o.until}: ${plural(o.recorded_requests, 'request')} recorded, ${plural(o.finished_review_attempts, 'finished review attempt')}, ${plural(o.recorded_deliveries, 'recorded delivery', 'recorded deliveries')}. <button class="linkbtn" type="button" data-ev="observed">Details</button></p>`);
  }

  // ---------- Details ----------
  function show(id, open) {
    const e = EV[id]; if (!e) return;
    selected = id;
    document.querySelectorAll('[data-ev]').forEach(el => { if (el.matches('.hbar')) el.setAttribute('aria-pressed', el.dataset.ev === id ? 'true' : 'false'); if (el.matches('tr')) el.setAttribute('aria-selected', el.dataset.ev === id ? 'true' : 'false'); });
    const C = window.shell.commands;
    C.select(id);
    const act = C.bar(id);
    put($('details'), html`<p class="kind">${e.s ? html`<span class="g-${e.s}" aria-hidden="true">${G[e.s]}</span> ` : ''}${e.kind}</p><h2>${e.title}</h2>
      <dl>${e.facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>${e.note ? html`<p class="note">${e.note}</p>` : ''}
      ${e.open ? html`<p><a class="open-link" href="${e.open[0]}">${e.open[1]}</a></p>` : ''}
      ${String(act) ? html`<h3>Act</h3>${SAID && SAID.id === id ? html`<p class="note" data-said="${SAID.kind}">${SAID.kind === 'unknown' ? html`<span class="g-unknown" aria-hidden="true">▨</span> ` : ''}${SAID.text}</p>` : ''}${act}` : ''}
      <h3>Same reading from the CLI</h3><div class="cli"><code>${e.cli}</code></div>`);
    window.shell.suggest([`Why is ${e.title} where it is?`, 'What changed against last week?', 'Which of these needs me today?']);
    if (open) window.shell.openPane('tab-details');
  }

  // ---------- Writes ----------
  // One write: post, then read again. Landed: reread, and a toast with Undo when the command has an inverse. A 400 or 409
  // wrote nothing (server.action_route; queue_screen.py has the same contract): load again and say so. Anything else (a
  // 503, a refused session, no answer) may follow a landed write: load again and say the outcome is not known.
  const said = (id, kind, text) => { SAID = { id, kind, text }; if (selected === id) show(id, false); };
  function write(o, what, path, body, inverse) {
    const S = window.shell;
    S.post(path, body).then(answer => {
      reader.reread().then(() => said(o.id, 'landed', `${what}.`));
      S.toast(`${what} · ${o.label}`, inverse ? () => inverse(answer).then(() => reader.reread()).then(() => { said(o.id, 'landed', `${what} undone.`); S.toast(`${what} undone · ${o.label}`); },
        e => failed(o, e, `${what} not undone`, `the undo landed on ${o.label}`)) : undefined);
    }, e => failed(o, e, 'not changed', `${o.label} changed`));
    return null;
  }
  function failed(o, e, refusal, landed) {
    const refused = e.status === 400 || e.status === 409, why = e.message.replace(/\.$/, '');
    const text = refused ? `${o.label} ${refusal}: ${why}. Nothing was written.`
      : `Not known whether ${landed}: ${why}. Metrics was read again; check it before you act again.`;
    reader.load().then(() => said(o.id, refused ? 'refused' : 'unknown', text));
    window.shell.toast(text);
    return null;
  }
  // The cap field: none clears; a decimal goes as a number; anything else goes as typed, and the library refuses it (400).
  const capOf = typed => /^none$/i.test(typed) ? null : /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(typed) && Number.isFinite(Number(typed)) ? Number(typed) : typed;
  function cap(o, v) {
    const path = `/api/bills/${encodeURIComponent(o.bill)}/cap`, before = o.control.cap_usd_month;
    const to = o.control.write === 'clear' ? null : capOf((v && v.cap) || '');
    // A legacy clear has no inverse: set_cap refuses a cap on a bill a start entry is billed to.
    return write(o, to == null ? 'Cap cleared' : `Cap set to ${typeof to === 'number' ? money(to) : to}`, path, { revision: o.revision, cap_usd_month: to },
      o.control.write === 'set' ? answer => window.shell.post(path, { revision: answer.revision, cap_usd_month: before }) : null);
  }
  // Configure's fields: blank keeps the current value, which the placeholder shows.
  const names = typed => typed.split(',').map(n => n.trim()).filter(Boolean);
  function configureFields() {
    const c = DOC?.controls; if (!c || c.error) return [];
    return [{ name: 'enabled', label: 'Enabled providers, comma-separated', placeholder: c.providers.filter(p => p.enabled).map(p => p.name).join(', '), help: `Blank keeps them. Known: ${c.providers.map(p => p.name).join(', ')}.` },
      ...['author', 'reviewer'].map(role => ({ name: role, label: `Automatic ${role} order, first choice first`, placeholder: c.orders[role].join(', '), help: 'Blank keeps it.' }))];
  }
  function configure(o, v = {}) {
    const c = DOC.controls, known = c.providers.map(p => p.name);
    const before = { enabled: Object.fromEntries(c.providers.map(p => [p.name, p.enabled])), orders: c.orders };
    const on = v.enabled ? names(v.enabled) : null, unknown = on ? on.filter(n => !known.includes(n)) : [];
    if (unknown.length) { const text = `${o.label} not changed: no provider ${unknown.join(', ')}; the registry names ${known.join(', ')}.`; said(o.id, 'refused', text); window.shell.toast(text); return null; }
    const enabled = on ? Object.fromEntries(known.map(n => [n, on.includes(n)])) : before.enabled;
    const orders = { author: v.author ? names(v.author) : c.orders.author, reviewer: v.reviewer ? names(v.reviewer) : c.orders.reviewer };
    return write(o, 'Providers configured', '/api/providers/configure', { revision: o.revision, enabled, orders },
      answer => window.shell.post('/api/providers/configure', { revision: answer.revision, ...before }));
  }

  // ---------- Reader (read.js) ----------
  let reader = null;
  const load = () => reader.load();
  const spec = {
    source: '/api/metrics', what: 'the Metrics readings',
    adopt: doc => {
      DOC = doc; evidence(); CELLS = readCells(); attention();
      document.body.dataset.observed = doc.read;
      const failed = ['usage', 'trend', 'numbers', 'scorecard', 'skills', 'age', 'observed', 'controls'].filter(k => doc[k].error || doc[k].stale);
      FIRST = CELLS.find(c => c.state === 'warning' || c.state === 'caution')?.ev || Object.keys(EV).find(k => k.startsWith('win:')) || null;
      put($('subhead'), html`Spend against budget, the week, providers, skill use and the month's usage · read <time class="rel" datetime="${doc.read}"></time>`);
      return { objects: Object.entries(EV).map(([id, e]) => ({ ...e, id, type: e.type || 'reading', label: e.label || e.title })),
        state: failed.length ? { kind: 'partial', text: `${failed.map(k => `${k}: ${doc[k].error || `stale since ${hhmm(doc[k].observed)}: ${doc[k].stale}`}`).join(' · ')}. The other readings are current.`, source: '/api/metrics' } : null };
    },
    clear: () => {
      DOC = null; EV = {}; CELLS = []; FIRST = null;
      delete document.body.dataset.observed;
      put($('subhead'), html`The Metrics readings could not be read.`);
      window.shell.attention({ state: 'unknown', n: 0, what: 'Metrics not read' });
    },
    draw,
    current: () => selected,
    first: () => FIRST,
    select: (id, opened) => show(id, opened),
    unselect: () => { selected = null; put($('details'), html`<p class="why">${DOC ? 'Select a cell, bar or row to see its evidence.' : 'Nothing is selected: the Metrics readings could not be read.'}</p>`); },
  };

  window.PAGE_COMMANDS = [
    { label: 'Read Metrics again', icon: 'rotate-ccw', run: () => load() },
    { label: 'Open Usage (classic)', icon: 'chart-no-axes-column', run: () => { location.href = '/operations?area=usage'; } },
  ];
  window.PAGE_LIST = { rows: () => document.querySelectorAll('.tbl tbody tr[data-id]'), current: () => selected, select: id => show(id, false) };

  document.addEventListener('DOMContentLoaded', () => {
    const S = window.shell, C = S.commands;
    reader = S.read(spec);
    // ---------- Commands (products/system/commands.md) ----------
    // Metrics' two write homes (sd:3387). Each posts the route the classic Usage forms post, with the revision this page
    // read, and settles itself: write() toasts what landed with its Undo, and says in Details when the outcome is unknown.
    const offWhy = o => !o.control ? (DOC?.controls?.error ? `the provider registry was not read: ${DOC.controls.error}` : 'not in the provider registry')
      : o.revision ? true : 'the page holds no registry revision';
    C.register(
      { id: 'providers.configure', on: 'provider', label: 'Configure', does: 'Saves the enabled providers and role orders; Undo restores the old ones.', key: 'f', risk: 'undo', executes: true, primary: () => true,
        when: offWhy, fields: configureFields, consequence: () => 'Saves the enabled choices and both automatic role orders together, as the registry reads them now.',
        cli: () => 'sd providers configure --file CONFIG.json', run: (o, v) => configure(o, v) },
      { id: 'bill.cap', on: 'bill', label: 'Cap', does: o => o.control?.write === 'clear' ? 'Clears the legacy cap on this bill; this cannot be undone.' : 'Sets a monthly cap on this bill; Undo restores the old cap.', key: 'p', risk: 'undo', executes: true, primary: () => true,
        when: o => { const w = offWhy(o); return w !== true ? w : o.control.write ? true : o.control.why; },
        fields: o => o.control?.write === 'set' ? [{ name: 'cap', label: 'New cap, USD a month', required: true, placeholder: o.control.cap_usd_month == null ? 'none' : String(o.control.cap_usd_month),
          help: `Now ${o.control.cap_usd_month == null ? 'no cap' : money(o.control.cap_usd_month)}. Type none to clear it.` }] : [],
        askFirst: true, consequence: o => o.control?.write === 'clear' ? `Clears the legacy cap on ${o.bill}: ${o.control.why}.` : `The ledger refuses a reservation past the cap on ${o.bill} this month.`,
        cli: o => `no CLI: sd has no verb for bill caps (bill.cap_usd_month, ${o.bill}${o.cap == null ? '' : `, now ${money(o.cap)}`})`,
        run: (o, v) => cap(o, v) },
    );
    document.addEventListener('click', e => {
      if (e.target.closest?.('.rowact')) return;
      if (e.target.closest?.('#refresh')) { load(); return; }
      const el = e.target.closest?.('[data-ev]'); if (!el) return;
      if (el.dataset.view) S.view.show(el.dataset.view);
      if (el.dataset.ev) show(el.dataset.ev, true);
    });
    // A chart point is the page's own control: its keys stop at the trends block, so the shell's Enter (shell:open) does not also run.
    $('trends').addEventListener('keydown', e => {
      const el = e.target.closest?.('svg [data-ev]'); if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.stopPropagation(); show(el.dataset.ev, true); }
    });
    $('range').addEventListener('click', e => { const c = e.target.closest('.chip[aria-disabled="true"]'); if (c) S.toast(c.title); });
    document.addEventListener('shell:open', e => { if (EV[e.detail]) show(e.detail, true); });
    load();
  });
})();
window.PAGE_KEYS = [['↵', 'On a trend point: show it in Details']];
