// Contributions (sd:2113): the design source's products/system/designs/v2/contributions.js at b2b8c2b, ported. Each change from the
// reference is marked "build:". The rows are /api/contributions/page (contribution_screen.document): the projection v1
// /classic/contributions renders, never sample data. The dashboard reads nothing from GitHub, so the reference's "GitHub now"
// chip, its "settled on GitHub" lane and its settled-per-day chart have no reading here and say so. Acknowledge and Make task run
// through dashboard routes and ask first: neither has a verb that reverses it. Draft nudge, Open on GitHub and Re-run collector are
// copy only. The reference's Trackers view is not ported: its switch opens the classic Operations > Trackers.
const { html, put, plural } = window.markup;
// Palette "This page" group and the key sheet. shell.js reads both at start, so they are set before the shell runs.
window.PAGE_KEYS = [['/', 'Filter contributions']];
window.PAGE_COMMANDS = [
  { label: 'Filter contributions', icon: 'search', key: '/', run: () => document.getElementById('q').focus() },
  { label: 'Show the collector', icon: 'activity', run: () => document.dispatchEvent(new CustomEvent('shell:open', { detail: 'collector' })) },
  ...['all', 'internal', 'external'].map(v => ({ label: `Show ${v} repositories`, icon: 'filter', run: () => document.dispatchEvent(new CustomEvent('contributions:scope', { detail: v })) })),
];
// build: nothing is read yet, so the page claims nothing for the rail until /api/contributions/page answers.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'contributions not read' };
addEventListener('DOMContentLoaded', () => {
  const C = window.shell.commands;
  const LABEL = { newly_unblocked: 'Newly unblocked', awaiting_you: 'Awaiting you', awaiting_them: 'Awaiting them', merged: 'Merged', closed: 'Closed' };
  const ORDER = ['newly_unblocked', 'awaiting_you', 'awaiting_them'];
  const STATE = { newly_unblocked: 'caution', awaiting_you: 'caution', awaiting_them: 'queued' };
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const SCOPES = ['all', 'internal', 'external'];
  const FRESH = { fresh: 'ok', stale: 'caution', degraded: 'warning', never: 'unknown', unknown: 'unknown' };
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const n = x => Number(x || 0).toLocaleString('en-US');
  // build: a POSIX single-quoted word, so a title with $ or a quote copies as itself.
  const shq = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
  const csrf = () => document.querySelector('meta[name="sd-csrf"]')?.content || '';
  async function getJSON(path) {
    const r = await fetch(path, { headers: { Accept: 'application/json' } });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
    return out;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-SD-CSRF': csrf() }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
    return out;
  }
  // build: only an https github.com URL becomes a link, as v1's _link allows.
  const github = u => /^https:\/\/github\.com\/[^/@\s]+\/[^/@\s]+\//.test(String(u || ''));
  const isIssue = r => /\/issues\/\d+$/.test(String(r.url || ''));
  const filed = r => r.url ? (isIssue(r) ? 'Issue' : 'Pull request') : r.has_draft ? 'Unfiled issue draft' : 'Unfiled local work';
  const num = r => r.url ? '#' + String(r.url).split('/').pop() : '';

  // ---------- Data (build: /api/contributions/page) ----------
  let DOC = null, ROWS = [], BY = new Map(), selected = null;
  const u0 = new URLSearchParams(location.search);
  let scope = SCOPES.includes(u0.get('scope')) ? u0.get('scope') : 'all';
  let lane = ORDER.includes(u0.get('lane')) ? u0.get('lane') : null;
  const q = document.getElementById('q');
  if (u0.get('q')) q.value = u0.get('q');
  const inScope = r => scope === 'all' || (scope === 'internal') === !!r.internal;
  const scoped = () => ROWS.filter(inScope);
  // build: counts come from the document's per-scope totals, which cover every open row; ROWS may be cut at OPEN_LIMIT.
  const tot = (k, sc = scope) => (sc === 'all' ? ['internal', 'external'] : [sc]).reduce((a, s) => a + ((DOC.counts || {})[s]?.[k] || 0), 0);
  const openN = sc => ORDER.reduce((a, l) => a + tot(l, sc), 0);
  const words = r => [r.title, r.repo, r.url, LABEL[r.lane], ...(r.reasons || [])].join(' ').toLowerCase();
  const shown = () => { const v = q.value.trim().toLowerCase(); return scoped().filter(r => (!lane || r.lane === lane) && (!v || words(r).includes(v))); };
  // The shell's object map only grows and keeps its picks, so each adopted reading retires what it no longer lists, as Health
  // does: the pick is dropped and the object becomes a type no command is on, so no stale Acknowledge can act.
  let putIds = new Set(), picks = [];
  document.addEventListener('shell:picked', e => { picks = e.detail || []; });
  function retire(keep) {
    picks.filter(id => !keep.has(id)).forEach(id => C.pick(id));
    putIds.forEach(id => { if (!keep.has(id)) C.put({ id, type: 'not listed', label: `${C.get(id)?.label || id} (no longer listed)` }); });
    putIds = keep;
  }
  function adopt(doc) {
    DOC = doc;
    ROWS = (doc.rows || []).filter(r => ORDER.includes(r.lane)).map(r => ({ ...r, id: r.key, type: 'contribution', label: r.title, s: STATE[r.lane],
      event_ids: r.event_ids || [], reasons: r.reasons || [], freshness: r.freshness || { status: 'unknown', reason: '' } }));
    BY = new Map(ROWS.map(r => [r.key, r]));
    retire(new Set(BY.keys()));
    ROWS.forEach(r => C.put(r));
    C.put({ id: 'collector', type: 'collector', label: 'contributions collector' });
    document.body.dataset.observed = doc.read || '';
    // Rail badge (shell.js): rows that want the operator, in every scope.
    const mine = tot('newly_unblocked', 'all') + tot('awaiting_you', 'all');
    window.PAGE_ATTENTION = { state: mine ? 'caution' : 'ok', n: mine, what: 'contributions want you' };
    window.shell.attention?.();
  }

  // ---------- Commands (products/system/commands.md) ----------
  // build: Acknowledge and Make task ask first, since no verb un-acknowledges or deletes a task; the reference gave both Undo.
  // Draft nudge is the gh line to copy: the reference's chat proposal posted on Approve, and the dashboard posts nothing.
  // A write's answer is its own: the reread after it is started, not awaited, so a failed reread never reports a landed write
  // as "not changed". Until the reread lands, the row itself records the write, so the command that made it goes off.
  const landed = (o, change) => { Object.assign(o, change); C.put(o); if (DOC) { renderRows(); if (selected === o.key) show(o.key); } };
  const ackCli = o => `sd task contribution ack ${shq(o.key)} ${o.event_ids.map(e => `--event ${shq(e)}`).join(' ')} --if-revision ${o.revision}`;
  // build: Make task files the contribution task `sd task contribution add` files: the row's URL is its identity, so the
  // projection links the task to the row after a reread, and the library refuses a second task for the same URL.
  const linkOf = o => /^github:/.test(o.key) ? 'pull_url' : /^issue:/.test(o.key) ? 'issue_url' : null;
  const taskJson = o => JSON.stringify({ [linkOf(o)]: o.key.slice(o.key.indexOf(':') + 1) });
  const kindOf = o => isIssue(o) ? 'issue' : 'pr';
  function registerCommands() {
    C.register(
      { id: 'contribution.ack', on: 'contribution', label: 'Acknowledge', key: 'a', risk: 'confirm', bulk: true, primary: () => true,
        when: o => o.event_ids.length ? true : 'no attention event on this row',
        consequence: o => `This clears ${plural(o.event_ids.length, 'attention event')} on ${o.title}. No verb restores them; it completes no task and sends nothing.`,
        cli: ackCli,
        run: o => post('/api/contributions/acknowledge', { key: o.key, revision: o.revision, event_ids: o.event_ids })
          .then(() => { landed(o, { event_ids: [] }); reread(true); return `Acknowledged · ${o.label}`; }, e => { reread(); throw e; }) },
      { id: 'contribution.nudge', on: 'contribution', label: 'Draft nudge', key: 'd', risk: 'safe', executes: false, primary: () => true,
        when: o => !o.url ? 'not filed on GitHub' : o.lane !== 'awaiting_them' ? 'the next step is yours, not theirs' : true,
        cli: o => `gh ${isIssue(o) ? 'issue' : 'pr'} comment ${o.url} --body-file nudge.md`,
        run: () => 'Copy the line and write nudge.md: the dashboard posts nothing' },
      { id: 'contribution.task', on: 'contribution', label: 'Make task', key: 'k', risk: 'confirm', executes: true,
        when: o => o.item_id ? `already tracked as item #${o.item_id}` : linkOf(o) ? true : 'not filed on GitHub',
        consequence: o => `This files a task, "${o.title}", that tracks ${o.url || o.key} as this contribution. No verb deletes it; cancel it from Tasks.`,
        cli: o => `printf '%s\\n' ${shq(taskJson(o))} > contribution.json && sd task contribution add ${shq(o.title)} --file contribution.json`,
        run: o => post('/api/contributions/task', { key: o.key, title: o.title })
          .then(out => { landed(o, { item_id: out.item?.id }); reread(true); return `Task #${out.item?.id} filed · ${o.label.slice(0, 48)}`; }, e => { reread(); throw e; }) },
      // Copy only, as ruled: the shell still calls run, so run opens nothing. The Details link opens the page.
      { id: 'contribution.open', on: 'contribution', label: 'Open on GitHub', key: 'o', risk: 'safe', executes: false,
        when: o => github(o.url) || 'not filed on GitHub', cli: o => `gh ${kindOf(o)} view --web ${o.url}`,
        run: () => 'Copy the line, or use the link in Details: the dashboard opens nothing itself' },
      // Copy only until the dashboard has a route that runs sd shadow sync (sd:2207, as the reference has it).
      { id: 'collector.sync', on: 'collector', label: 'Re-run collector', key: 'r', risk: 'safe', primary: () => true, executes: false,
        cli: () => 'sd shadow sync', run: () => 'Copy it into a terminal: the dashboard has no route that runs sd shadow sync yet (sd:2207)' },
    );
  }

  // ---------- Head: scope counts, lamps, source line ----------
  function renderHead() {
    const c = DOC.collector || {}, st = FRESH[c.state] || 'unknown';
    put(document.getElementById('sub'), html`Pull requests and issues you opened or owe · <span id="open-n">${n(openN(scope))}</span> open · read <time class="rel" datetime="${DOC.read}"></time>`);
    document.querySelectorAll('#scope input').forEach(i => { i.checked = i.value === scope; const b = i.nextElementSibling?.querySelector('b'); if (b) b.textContent = n(openN(i.value)); });
    const cnt = g => tot(g);
    const cells = [
      ['newly_unblocked', 'Newly unblocked', 'git-merge', 'caution', 'ready'],
      ['awaiting_you', 'Awaiting you', 'flag-triangle-right', 'caution', 'rows'],
      ['awaiting_them', 'Awaiting them', 'inbox', 'ok', 'rows'],
    ];
    // A lane with no rows has nothing to filter: a div that says so, not a disabled button (design.md § Lamps).
    const lamp = ([id, lbl, icon, lit, unit]) => {
      const k = cnt(id), s = k ? lit : 'ok';
      const inner = html`<span class="lbl">${lbl}${I(icon)}</span><span class="val"><span class="ph"><b>${k}</b> ${unit}</span></span><span class="sr">state ${s}</span>`;
      return k ? html`<li><button class="cell" type="button" aria-pressed="${String(lane === id)}" data-cell="${id}" data-state="${s}">${inner}</button></li>`
        : html`<li><div class="cell" data-cell="${id}" data-state="${s}">${inner}</div></li>`;
    };
    // build: the reference's fourth lamp counted rows GitHub had already settled, from a gh read. The dashboard makes none.
    put(document.getElementById('annunciator'), html`${cells.map(lamp)}<li><div class="cell" data-cell="github" data-state="unknown"><span class="lbl">Settled on GitHub${I('circle-dot')}</span><span class="val"><span class="ph">not checked</span> · <span class="ph">the dashboard does not read GitHub</span></span><span class="sr">state unknown</span></div></li>`);
    const unknown = tot('unknown');
    put(document.getElementById('source'), html`<span class="g-${unknown ? 'unknown' : 'ok'}" aria-hidden="true">${GLYPH[unknown ? 'unknown' : 'ok']}</span>
      <button class="linkish" type="button" data-collector>Collector</button>
      <span><b>${n(unknown)}</b> of ${n(openN(scope))} open rows unknown freshness</span>
      <span class="g-${st}">GitHub sync ${c.state || 'unknown'}${c.last_success_at ? html`, last success <time class="rel" datetime="${c.last_success_at}"></time>` : ''}</span>
      <span>${n(tot('linked'))} linked items · ${n(tot('unfiled'))} unfiled</span>`);
  }

  // ---------- Ledger ----------
  const tbody = document.getElementById('rows');
  function rowHtml(r) {
    const f = r.freshness;
    return html`<tr data-id="${r.key}" data-lane="${r.lane}" aria-selected="${String(r.key === selected)}">
      <td class="g g-${r.s}"><span aria-hidden="true">${GLYPH[r.s]}</span><span class="sr">${r.s}</span></td>
      <td class="what"><button type="button">${r.title}</button>
        <span class="meta">${r.repo || 'local'} ${num(r)} · ${filed(r)}</span>
        ${r.reasons.length ? html`<span class="reason">${String(r.reasons[r.reasons.length - 1]).slice(0, 180)}</span>` : ''}</td>
      <td class="obs">${f.status === 'current' ? html`<span class="g-ok" aria-hidden="true">●</span>` : html`<span class="g-unknown" title="${f.reason}">▨</span>`}<time class="rel" datetime="${r.observed_at || ''}" data-empty="never"></time><span class="sr"> · ${f.status}</span></td>
      <td class="act"><span class="row-acts">${C.rowActions(r.key)}</span></td></tr>`;
  }
  function renderRows() {
    const all = scoped(), rows = shown(), v = q.value.trim();
    const head = l => html`<tr class="lane" data-lane="${l}"><td colspan="4"><span class="label">${LABEL[l]} · ${rows.filter(r => r.lane === l).length}</span></td></tr>`;
    put(tbody, html`${rows.length ? ORDER.filter(l => rows.some(r => r.lane === l)).map(l => [head(l), rows.filter(r => r.lane === l).map(rowHtml)])
      : html`<tr class="lane"><td colspan="4"><span class="label">${all.length ? `Nothing matches${v ? ` “${v}”` : ''}` : `Nothing open in ${scope === 'all' ? 'any' : scope} repositories`}</span></td></tr>`}`);
    const f = document.getElementById('filtered');
    const cut = DOC.truncated ? `The document lists ${n(DOC.rows.length)} of ${n(DOC.open_total)} open rows; v1 /classic/contributions lists every one.` : '';
    f.hidden = !(lane || v || cut);
    f.textContent = [lane || v ? (rows.length ? `${rows.length} of ${all.length} shown${lane ? ' · ' + LABEL[lane] : ''}${v ? ` · “${v}”` : ''}. Esc clears.` : `Nothing matches${v ? ` “${v}”` : ''}. Esc clears.`) : '', cut].filter(Boolean).join(' ');
    put(document.getElementById('tally'), html`<span class="g-caution">▲ ${n(tot('newly_unblocked') + tot('awaiting_you'))} yours</span><span class="g-queued">◌ ${n(tot('awaiting_them'))} theirs</span><span class="g-unknown">▨ ${n(tot('unknown'))} unknown freshness</span>`);
    window.shell.url({ ...(scope !== 'all' && { scope }), ...(lane && { lane }), ...(v && { q: v }) });
  }

  // ---------- Settled: counts per repository; no dates ----------
  function renderSettled() {
    const S = (DOC.settled || []).filter(inScope), other = DOC.settled_other || {};
    const sides = scope === 'all' ? ['internal', 'external'] : [scope];
    const otherN = sides.reduce((a, s) => a + (other[s]?.merged || 0) + (other[s]?.closed || 0), 0);
    const merged = S.reduce((a, e) => a + e.merged, 0) + sides.reduce((a, s) => a + (other[s]?.merged || 0), 0);
    const closed = S.reduce((a, e) => a + e.closed, 0) + sides.reduce((a, s) => a + (other[s]?.closed || 0), 0);
    document.getElementById('settled-tally').textContent = `${n(merged + closed)} rows · ${n(merged)} merged · ${n(closed)} closed`;
    // build: the reference charted merge and close dates read from GitHub. The projection records neither, so the chart says so.
    document.getElementById('tp-note').textContent = 'No merge or close dates recorded: the projection keeps each row\'s state, not when it settled, and the dashboard does not read GitHub.';
    const top = S.slice(0, 12), rest = S.slice(12).reduce((a, e) => a + e.merged + e.closed, 0) + otherN;
    const list = [...top.map(e => [e.repo, e.merged + e.closed]), ...(rest ? [['other repositories', rest]] : [])];
    const max = Math.max(1, ...list.map(x => x[1]));
    const bars = document.getElementById('bars');
    bars.setAttribute('aria-label', list.length ? 'Settled contributions per repository: ' + list.map(([r, c]) => `${r} ${c}`).join(', ') : 'No settled contributions');
    put(bars, html`${list.length ? list.map(([r, c]) => html`<span class="name" title="${r}">${r}</span><span class="v">${n(c)}</span><svg aria-hidden="true"><rect class="fill" x="0" y="0" width="${(c / max * 100).toFixed(1)}%" height="12" rx="2"/></svg>`) : html`<span class="name">No settled rows in ${scope === 'all' ? 'any' : scope} repositories</span>`}`);
  }

  // ---------- Details ----------
  const details = document.getElementById('details');
  const swap = () => { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); };
  function showCollector() {
    selected = 'collector'; mark();
    const c = DOC.collector || {}, st = FRESH[c.state] || 'unknown';
    put(details, html`<p class="kind"><span class="g-${st}" aria-hidden="true">${GLYPH[st]}</span> Collector · contributions projection</p>
      <h2>GitHub sync is ${c.state || 'unknown'}</h2>
      <dl><dt>Rows</dt><dd>${n(DOC.total)}</dd><dt>Freshness unknown</dt><dd>${n(DOC.fresh?.unknown)} of ${n(DOC.total)}</dd><dt>Freshness current</dt><dd>${n(DOC.fresh?.current)}</dd>
        <dt>Last success</dt><dd>${c.last_success_at ? html`<time class="rel" datetime="${c.last_success_at}"></time>` : 'never'}</dd>
        ${c.reason ? html`<dt>Reason</dt><dd>${c.reason}</dd>` : ''}<dt>Source</dt><dd>/api/contributions/page · progress.tracker_freshness</dd></dl>
      <h3>Act</h3>${C.bar('collector')}
      <p class="why">One sync refreshes the trackers and the contributions projection. The nightly job runs the same verb.</p>`);
    C.select('collector'); swap();
    window.shell.suggest?.(['Why is the GitHub sync stale?', 'Which rows would move after a sync?']);
  }
  function show(key) {
    if (key === 'collector') return showCollector();
    const r = BY.get(key); if (!r) return false;
    selected = key; mark();
    const where = r.internal ? `internal (${r.why_internal === 'managed' ? 'sd manages it' : 'registered with sd'})` : 'external';
    const facts = [['Repo', `${r.repo || 'local'} · ${where}`], ['Filed as', filed(r) + (r.url ? ' ' + num(r) : '')], ['Lane', LABEL[r.lane]],
      ['Collector says', r.external_state || 'not filed'], ['Local status', r.local_status || 'none'], ['Local item', r.item_id ? '#' + r.item_id : 'none'],
      ['Branch', r.local_branch || 'none'], ['Freshness', r.freshness.status + (r.freshness.reason ? ': ' + r.freshness.reason : '')]];
    put(details, html`<p class="kind"><span class="g-${r.s}" aria-hidden="true">${GLYPH[r.s]}</span> Contribution · ${LABEL[r.lane]}</p>
      <h2>${r.title}</h2>
      ${github(r.url) ? html`<p><a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${String(r.url).replace('https://github.com/', '')}${I('arrow-up-right')}</a></p>` : ''}
      <dl>${facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Observed</dt><dd><time class="rel" datetime="${r.observed_at || ''}" data-empty="never"></time></dd></dl>
      ${r.reasons.length ? html`<h3>Why it is here</h3><ul class="reasons">${r.reasons.map(x => html`<li>${x}</li>`)}</ul>` : ''}
      <h3>Act</h3>${C.bar(r.key)}
      <p class="why">Acknowledge clears attention only. It does not complete a task or send a notification.</p>`);
    C.select(r.key); swap();
    window.shell.suggest?.(r.lane === 'awaiting_them' && r.url ? ['Draft a short, polite nudge for this', 'What changed upstream since I filed it?']
      : ['Why is this still open locally?', 'What would unblock this?']);
    return true;
  }
  function mark() { tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === selected))); }
  function select(key, open) { if (show(key) !== false && open) window.shell.openPane('tab-details'); }
  const none = () => { selected = null; put(details, html`<p class="why">No contribution matches the filter, so nothing is selected. Esc clears it.</p>`); };

  // ---------- Filter ----------
  function applyFilter() {
    renderRows();
    window.shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, keep: k => k === 'collector', select: k => select(k, false), clear: none });
  }
  function setScope(v) { if (!SCOPES.includes(v) || !DOC) return; scope = v; lane = null; renderHead(); renderSettled(); applyFilter(); }
  function setLane(v) { lane = lane === v ? null : v; renderHead(); applyFilter(); }
  function render() { renderHead(); renderSettled(); renderRows(); }

  // ---------- Events ----------
  document.getElementById('annunciator').addEventListener('click', e => { const c = e.target.closest('button.cell'); if (c) setLane(c.dataset.cell); });
  document.getElementById('scope').addEventListener('change', e => setScope(e.target.value));
  document.addEventListener('contributions:scope', e => setScope(e.detail));
  document.getElementById('source').addEventListener('click', e => { if (e.target.closest('[data-collector]')) select('collector', true); });
  tbody.addEventListener('click', e => { if (e.target.closest('.rowact')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });
  document.addEventListener('shell:open', e => select(e.detail, true));
  q.addEventListener('input', () => DOC && applyFilter());
  q.addEventListener('keydown', e => { if (e.key === 'Escape' && q.value) { e.stopPropagation(); q.value = ''; DOC && applyFilter(); } });
  document.addEventListener('keydown', e => {
    if (e.target.matches?.('input, textarea, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest?.('.actmenu')) return;
    if (e.key === '/') { e.preventDefault(); q.focus(); }
  });
  // The shell walks the shown rows on j / k; Esc clears the lane filter.
  window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false), keep: k => k === 'collector',
    clear: () => { if (!lane) return false; lane = null; renderHead(); applyFilter(); return true; } };

  // ---------- Start (build: read /api/contributions/page, then draw) ----------
  // Each read takes a generation; only the newest one draws. A bulk Acknowledge starts one reread per row, and the reads can
  // answer out of order: the newest started after the last write landed, so it alone may replace the rows.
  let generation = 0;
  function stateOf(doc) {
    const c = doc.collector || {};
    if (!doc.total) return { kind: 'empty', title: 'No contributions', text: 'The projection has no rows. Register local work or run sd shadow sync to collect authored pull requests.', source: '/api/contributions/page' };
    if (c.state && c.state !== 'fresh') return { kind: 'partial', title: 'GitHub sync not fresh', text: `The GitHub sync is ${c.state}${c.reason ? ': ' + c.reason : ''}. Rows show what the last sync saw.`, source: 'progress.tracker_freshness' };
    return null;
  }
  // `wrote` says the write before this reread landed; a refused write rereads too, and its failure must not claim one.
  async function reread(wrote) {
    const mine = ++generation;
    let doc;
    try { doc = await getJSON('/api/contributions/page'); } catch (err) {
      if (mine !== generation) return;
      window.shell.state({ kind: 'error', text: `${wrote ? 'The write landed, but the' : 'The'} contributions were not read again: ${err.message}. The rows show the last read; Reload retries it.`, source: '/api/contributions/page' });
      return;
    }
    if (mine !== generation) return;
    adopt(doc); window.shell.state(stateOf(doc)); render();
    if (selected && selected !== 'collector' && !BY.has(selected)) selected = null;
    applyFilter();
    if (selected) show(selected);
  }
  async function load() {
    const mine = ++generation;
    window.shell.state({ kind: 'loading', text: 'Reading the contributions. Rows appear when /api/contributions/page answers.', source: '/api/contributions/page' });
    let doc;
    try { doc = await getJSON('/api/contributions/page'); } catch (err) {
      if (mine !== generation) return;
      window.shell.state({ kind: 'error', text: `The contributions were not read: ${err.message}. Reload retries it.`, source: '/api/contributions/page' });
      return;
    }
    if (mine !== generation) return;
    adopt(doc);
    window.shell.state(stateOf(doc));
    render();
    const row = window.shell.row?.();
    if (row && (row === 'collector' || BY.has(row))) select(row, false);
    applyFilter();
  }
  registerCommands();
  load();
});
