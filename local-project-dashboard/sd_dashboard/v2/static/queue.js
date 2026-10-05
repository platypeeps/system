// Queue (sd:2585): /api/queue (queue_screen.py), one section per repository lane, one row per item in its state:
// merging, next, building, blocked, landed. Up, down, top, hold and release post /api/queue/move with the lane's
// revision; the server runs sd-ship lane move|hold|release, refuses a stale revision, and the page reads again.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { live: ['live', '●'], ok: ['ok', '●'], caution: ['caution', '▲'], warning: ['warning', '■'], queued: ['queued', '◌'], unknown: ['unknown', '▨'] };
  const ICON = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const HM = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const at = s => s ? HM.format(new Date(s)) : 'not recorded';
  const dur = s => s == null ? 'not recorded' : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
  const TREND = { rising: '↑ rising', falling: '↓ falling', steady: '→ steady' };
  let DOC = null, busy = false;
  const said = {}; // lane path -> { ok, text }: the last write's result, shown in its lane until the next one

  const btn = (lane, r, act, label, icon, off) => html`<button class="btn quiet sm" type="button" data-act="${act}" data-item="${String(r.item)}" data-repo="${lane.path}" aria-label="${label} sd:${String(r.item)}" title="${label}"${off || busy ? html` disabled` : ''}>${ICON(icon)}</button>`;
  function controls(lane, r, last) {
    return html`<div class="ctl" role="group" aria-label="Order of sd:${String(r.item)}">${btn(lane, r, 'up', 'Move up', 'arrow-up', r.position === 1)}${btn(lane, r, 'down', 'Move down', 'arrow-down', r.position === last)}<button class="btn quiet sm" type="button" data-act="top" data-item="${String(r.item)}" data-repo="${lane.path}" aria-label="Move sd:${String(r.item)} to the top"${r.position === 1 || busy ? html` disabled` : ''}>Top</button><button class="btn quiet sm" type="button" data-act="${r.held ? 'release' : 'hold'}" data-item="${String(r.item)}" data-repo="${lane.path}"${busy ? html` disabled` : ''}>${r.held ? 'Release' : 'Hold'}</button></div>`;
  }
  // Each state's glyph, label and fact line. A state's glyph carries it; colour only repeats it.
  const VIEW = {
    merging: r => [G.live, 'MERGING', html`<b>${r.phase}</b> · ${dur(r.elapsed)} · head ${r.head || 'not recorded'}`],
    next: r => [r.held ? G.caution : G.queued, `${r.held ? 'HELD' : 'NEXT'} ${r.position}`, html`gate <b>${r.gate}</b>${r.gate_summary ? ` (${r.gate_summary})` : ''} · head ${r.head || 'not recorded'}${r.held ? ' · skipped until released' : ''}`],
    building: r => [{ running: G.live, pass: G.ok, fail: G.warning }[r.gate] || G.unknown, 'BUILDING', html`gate <b>${r.gate}</b>${r.summary ? ` · ${r.summary}` : ''}${r.head ? ` · head ${r.head}` : ''} · log changed ${at(new Date(r.changed * 1000).toISOString())}`],
    blocked: r => [G.caution, 'BLOCKED', html`<b>${r.who} acts</b> · ${r.status}${r.step ? ` at ${r.step}` : ''} · ${r.reason}`],
    landed: r => [G.ok, 'LANDED', html`${r.pr ? `PR #${r.pr}` : 'PR not named'} · merge ${r.commit || 'not recorded'} · ${at(r.finished)}`],
  };
  function row(lane, r, last) {
    const [g, st, facts] = VIEW[r.state](r);
    const subject = r.state === 'building' ? html`<code>${r.builder}</code>` : html`<code>sd:${String(r.item)}</code>${r.title}`;
    return html`<li class="qrow" data-state="${r.state}"${r.held ? html` data-held=""` : ''}><span class="g g-${g[0]}" aria-hidden="true">${g[1]}</span><span class="st">${st}</span><span class="s"><span><span class="sr">${g[0]} · </span>${subject}</span><small>${facts}</small></span>${r.state === 'next' ? controls(lane, r, last) : html`<span></span>`}</li>`;
  }
  function lane(l) {
    const last = l.rows.filter(r => r.state === 'next').length, s = said[l.path];
    const count = st => l.rows.filter(r => r.state === st).length;
    return html`<section class="lane" aria-labelledby="lane-${l.repo}"><header><h2 id="lane-${l.repo}">${l.repo}</h2><span class="n">${count('merging')} merging · ${last} next · ${count('blocked')} blocked · ${count('landed')} landed today</span><span class="src">${l.lane}</span></header>
      ${s ? (s.ok ? html`<p class="done" role="status">${s.text}</p>` : html`<p class="fail" role="alert">${s.text}</p>`) : ''}
      ${l.rows.length ? html`<ul class="qrows">${l.rows.map(r => row(l, r, last))}</ul>` : html`<p class="done">Nothing queued, gating or landed today in this lane.</p>`}</section>`;
  }
  function draw() {
    if (!DOC) { put($('readings'), html``); put($('lanes'), html``); return; }
    const L = DOC.load, gates = DOC.gates;
    put($('readings'), html`<div><dt>load5</dt><dd>${L.load5.toFixed(1)}<small>1m ${L.load1.toFixed(1)} · 15m ${L.load15.toFixed(1)} · ${TREND[L.trend]}</small></dd></div>
      <div><dt>gates running / cap</dt><dd>${gates.error ? html`▨ <small>${gates.error}</small>` : html`${gates.running} / ${gates.cap}<small>${gates.waiting} waiting</small>`}</dd></div>
      <div><dt>read</dt><dd>${at(DOC.read)}<small>${DOC.read.replace('T', ' ').replace('Z', ' UTC')}</small></dd></div>`);
    $('edits').textContent = DOC.edits;
    put($('lanes'), html`${DOC.problems.map(p => html`<p class="fail" role="alert">▨ ${p.repo}: lane not read. ${p.error}</p>`)}${DOC.lanes.length ? DOC.lanes.map(lane)
      : html`<p class="done">No registered repository has a lane folder${DOC.root ? ` under ${DOC.root}` : ''}. sd-ship lane enqueue makes one.</p>`}`);
  }

  async function load() {
    const S = window.shell;
    if (!DOC) S.state({ kind: 'loading', text: 'Reading the lanes. Rows appear when /api/queue answers.', source: '/api/queue' });
    try {
      DOC = await S.getJSON('/api/queue');
      S.state(DOC.problems.length ? { kind: 'partial', text: `${plural(DOC.problems.length, 'lane')} not read; the others are current.`, source: '/api/queue' } : null);
      put($('subhead'), html`${plural(DOC.lanes.length, 'lane')} of ${plural(DOC.registered, 'registered repository', 'registered repositories')} · read ${at(DOC.read)}`);
      const blocked = DOC.lanes.reduce((n, l) => n + l.rows.filter(x => x.state === 'blocked').length, 0);
      S.attention({ state: blocked ? 'caution' : 'ok', n: blocked, what: 'blocked' });
    } catch (err) {
      DOC = null;
      S.state({ kind: 'error', text: `The lanes were not read: ${err.message}. Read again retries it.`, source: '/api/queue' });
      put($('subhead'), html`The lanes could not be read.`);
    }
    draw();
  }

  const DONE = { up: 'moved up', down: 'moved down', top: 'moved to the top', hold: 'held', release: 'released' };
  async function act(b) {
    const l = DOC && DOC.lanes.find(x => x.path === b.dataset.repo);
    if (!l || busy) return;
    busy = true; draw();
    const item = Number(b.dataset.item), action = b.dataset.act;
    try {
      const out = await window.shell.post('/api/queue/move', { repo: l.path, item, action, revision: l.revision });
      const order = out.pending ? ` Order now: ${out.pending.map(n => `sd:${n}`).join(', ')}.` : '';
      said[l.path] = { ok: true, text: `sd:${item} ${DONE[action]}.${order} ${out.edits || ''}` };
    } catch (err) {
      said[l.path] = { ok: false, text: `sd:${item} not ${DONE[action]}: ${err.message}${err.stale ? '' : ' The queue is unchanged.'}` };
    }
    busy = false;
    await load();
  }
  window.queueAct = act; // for the page test

  window.PAGE_ATTENTION = { state: 'ok', n: 0, what: 'blocked' };
  document.addEventListener('DOMContentLoaded', () => {
    $('reload').addEventListener('click', load);
    $('lanes').addEventListener('click', e => { const b = e.target.closest('button[data-act]'); if (b && !b.disabled) act(b); });
    if (typeof setInterval === 'function') setInterval(() => { if (!document.hidden && !busy) load(); }, 30000);
    load();
  });
})();
