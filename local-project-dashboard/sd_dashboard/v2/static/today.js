// Today v2 (sd:2110): the page script. It loads before shell.js, which reads what it declares.
// Rows: /api/now, the document now_screen.document builds (the one v1 Today's Now section reads). Nothing here is sample data.
// Ported from ui-design products/system/designs/v2/today.html at a3861c9; the sources the build has no collector for are left out.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  // The rail's map (built sections, and the old screen each unported one opens) is /ui/sections.js, loaded before this.
  // Capture files through the same route and library call as v1 Today's capture form (POST /api/items).
  window.SHELL_CAPTURE = async ({ kind, title, item }) => {
    if (kind === 'note') throw new Error('Not filed: v2 files tasks and followups only. Add the note from the item page.');
    const body = { title, ...(kind === 'followup' ? { kind: 'followup', ...(item ? { followup_of: item } : {}) } : {}) };
    const p = title.match(/\bp([1-4])\b/i);
    if (p) { body.priority = +p[1]; body.title = title.replace(/\bp[1-4]\b/gi, '').replace(/\s+/g, ' ').trim(); }
    const out = await window.shell.post('/api/items', body).catch(e => { throw new Error(`Not filed: ${e.message}`); });
    return `Captured #${out.item?.id}: ${out.item?.title || body.title}`;
  };

  // now_screen bands → design.md state grammar.
  const STATE = { broken: 'warning', look: 'caution', queued: 'queued' };
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const SRC = { jobs: ['Jobs', 'activity', 'jobs'], prs: ['Pull requests', 'git-pull-request', 'trackers'], sessions: ['Sessions', 'bot', 'sessions'], repos: ['Repos', 'hard-drive-download', 'repos'] };
  const TYPE = { job: 'job', pr: 'pull request', ahead: 'repo', dirty: 'repo', worktree: 'sessions', dark: 'collector' };
  const KIND = { job: 'Job', pr: 'Pull request', ahead: 'Repo', dirty: 'Repo', worktree: 'Sessions', dark: 'Collector' };
  const { html, put } = window.markup;
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const area = s => `/operations?area=${SRC[s]?.[2] || s}`;

  let DOC = null, ROWS = [], srcFilter = null, query = '';
  const $ = id => document.getElementById(id);

  function facts(r) {
    const f = { Detail: r.detail, Source: SRC[r.source]?.[0] || r.source, Rank: `${r.rank} · ${r.band}`, Id: r.id };
    if (r.retry) f['Retry line'] = r.retry;
    return f;
  }

  function show(id) {
    const r = ROWS.find(x => x.id === id); if (!r) return;
    $('rows').querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id));
    const s = STATE[r.band];
    put($('details'), html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> ${KIND[r.kind] || r.kind} · ${s}</p>
      <h2>${r.what}</h2>
      <dl>${Object.entries(facts(r)).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Read</dt><dd><time class="rel" data-long datetime="${DOC.now}"></time></dd></dl>
      <p><a href="${area(r.source)}">Open ${SRC[r.source]?.[0] || r.source} in Operations</a></p>
      <h3>Act</h3>${shell.commands.bar(id)}`);
    shell.commands.select(id);
    shell.suggest([`Why is ${r.what.split(' ')[0]} on this list?`, 'What else changed around this time?', 'Rank what I should do in the next hour']);
    $('details').setAttribute('data-swap', ''); requestAnimationFrame(() => $('details').removeAttribute('data-swap'));
  }
  function select(id, open) {
    show(id);
    const u = new URL(location.href); u.searchParams.set('row', id); history.replaceState(null, '', u);
    if (open) shell.openPane('tab-details');
  }

  function lamps() {
    document.querySelectorAll('.cell[data-src]').forEach(cell => {
      const s = cell.dataset.src, why = DOC.sources[s], rows = ROWS.filter(r => r.source === s && r.kind !== 'dark');
      const val = cell.querySelector('.val');
      if (why) { cell.dataset.state = 'unknown'; cell.title = why; put(val, html`<span class="ph">not read</span>`); return; }
      cell.removeAttribute('title');
      const st = rows.map(r => STATE[r.band]);
      cell.dataset.state = st.includes('warning') ? 'warning' : st.includes('caution') ? 'caution' : 'ok';
      const n = k => rows.filter(r => r.kind === k).length;
      // sd:2014: interrupted, unloaded and unknown jobs are rows too; the lamp counts the failed ones and names the rest.
      const failed = rows.filter(r => r.state === 'failed').length, other = rows.length - failed;
      put(val, s === 'jobs' ? html`<b>${failed}</b> failed${other ? html` · <span class="ph">${other} other</span>` : ''}`
        : s === 'prs' ? html`<span class="ph"><b>${rows.length}</b> awaiting</span> you`
        : s === 'sessions' ? html`<span class="ph"><b>${rows.reduce((a, r) => a + (+(r.id.split(':')[1]) || 0), 0)}</b> abandoned</span>`
        : html`<span class="ph"><b>${n('ahead')}</b> ahead</span> · <span class="ph">${n('dirty')} dirty</span>`);
    });
    const d = new Date(DOC.now);
    put($('observed'), html`<b>${d.toISOString().slice(11, 16)}</b> UTC`);
  }

  // build (sd:2483): one object per row, for shell.read to put, and to retire once a read stops listing the row.
  function objectOf(r) {
    const pr = r.kind === 'pr' && r.id.match(/^pr:(.+)#(\d+):\d+$/);
    const job = r.kind === 'job' && r.id.match(/^job:(.+):[^:]+$/);
    return { id: r.id, type: TYPE[r.kind] || r.kind, label: r.what, failed: r.kind === 'job' && !!r.retry, retry: r.retry, job: job && job[1],
             repo: pr && pr[1], number: pr && +pr[2] };
  }

  function render() {
    const tb = $('rows');
    put(tb, ROWS.length ? html`${ROWS.map((r, i) => { const s = STATE[r.band]; return html`<tr data-id="${r.id}" data-src="${r.source}"${i && ROWS[i - 1].band !== r.band ? html` class="band-start"` : ''}>
      <td class="g g-${s}" title="${s}">${GLYPH[s]}</td>
      <td class="what"><button type="button">${r.what}</button></td>
      <td class="detail">${r.detail}</td>
      <td class="src">${I(SRC[r.source]?.[1] || 'circle-help')}${r.source}</td>
      <td class="act">${shell.commands.rowActions(r.id)}</td></tr>`; })}`
      : html`<tr class="empty"><td colspan="5">Nothing wants you: every source was read and raised no row.</td></tr>`);
    const count = s => ROWS.filter(r => STATE[r.band] === s).length;
    put($('tally'), html`<span class="g-warning">■ ${count('warning')} warning</span><span class="g-caution">▲ ${count('caution')} caution</span><span class="g-queued">◌ ${count('queued')} queued</span>`);
    const d = new Date(DOC.now);
    put($('subhead'), html`${d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' })} · <b>${ROWS.length}</b> thing${ROWS.length === 1 ? '' : 's'} want${ROWS.length === 1 ? 's' : ''} you, loudest first · read <time class="rel" datetime="${DOC.now}"></time>`);
    const w = count('warning'), c = count('caution');
    window.PAGE_ATTENTION = { state: w ? 'warning' : c ? 'caution' : 'ok', n: w || c, what: w ? 'warning rows' : 'caution rows' };
    shell.attention();
    lamps();
    hideRows();
  }

  // The filter hides rows; it moves no selection. applyFilter() also moves the selection off a row the filter hid.
  function hideRows() {
    const tb = $('rows'); let n = 0;
    tb.querySelectorAll('tr[data-id]').forEach(tr => {
      const hide = (srcFilter && tr.dataset.src !== srcFilter) || (query && !tr.textContent.toLowerCase().includes(query));
      tr.hidden = hide; if (!hide) n++;
    });
    const f = $('filtered');
    f.hidden = !(srcFilter || query);
    f.textContent = n ? `${n} of ${ROWS.length} shown${srcFilter ? ' · source ' + srcFilter : ''}${query ? ` · “${query}”` : ''}. Esc clears.`
      : `Nothing matches${query ? ` “${query}”` : ''}. Press → to capture it as a task instead.`;
    return tb;
  }
  function unselect() {
    const u = new URL(location.href); u.searchParams.delete('row'); history.replaceState(null, '', u);
    put($('details'), html`<p class="why">${!DOC ? 'Nothing is selected: Now could not be read.' : ROWS.length ? 'No row matches the filter, so nothing is selected. Esc clears it.' : 'Nothing is selected: Now has no rows.'}</p>`);
  }
  const reconcile = () => shell.reconcile({ rows: $('rows').querySelectorAll('tr[data-id]'), current: new URLSearchParams(location.search).get('row'),
    select: id => select(id, false), clear: unselect });
  function applyFilter() { hideRows(); reconcile(); }

  // ---------- Reading (build, sd:2483): shell.read reads /api/now ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a row the read no longer lists
  // runs no command, a selection whose row is gone moves to the first row the filter shows, and a failed read clears the page.
  let reading = null, failed = '', reads = 0;
  function adopt(doc) {
    DOC = doc; ROWS = doc.rows;
    document.body.dataset.observed = doc.now;
    return { objects: ROWS.map(objectOf), state: null };
  }
  function clear(err) { DOC = null; ROWS = []; failed = err.message; delete document.body.dataset.observed; }
  function draw() {
    if (DOC) return render();
    // Nothing from the last read stays on screen: rows, counts, lamps, the observed time, the selection and the badge.
    $('subhead').textContent = `Now could not be read: ${failed}. Refresh tries again.`;
    document.querySelectorAll('.cell[data-src]').forEach(cell => { cell.dataset.state = 'unknown'; cell.title = 'Now could not be read'; put(cell.querySelector('.val'), html`<span class="ph">not read</span>`); });
    put($('observed'), html`<span class="ph">not read</span>`);
    put($('tally'), html``);
    put($('rows'), html`<tr class="empty"><td colspan="5">Now could not be read: ${failed}</td></tr>`);
    shell.attention({ state: 'unknown', n: 0, what: 'rows' });
    hideRows();
  }
  const shown = id => [...$('rows').querySelectorAll('tr[data-id]')].some(tr => tr.dataset.id === id && !tr.hidden);
  function load() {
    $('refresh').setAttribute('aria-busy', 'true'); reads++;
    return reading.load().finally(() => { if (!--reads) $('refresh').removeAttribute('aria-busy'); });
  }

  function clearSource() { srcFilter = null; document.querySelectorAll('.cell[data-src]').forEach(x => x.setAttribute('aria-pressed', 'false')); applyFilter(); }

  window.PAGE_COMMANDS = [
    { label: 'Filter, capture or ask', icon: 'search', key: '/', run: () => $('shift').focus() },
    { label: 'Clear the source filter', icon: 'filter', run: clearSource },
    { label: 'Read Now again', icon: 'rotate-ccw', run: () => load() },
    { label: 'Open the classic Today', icon: 'layout-dashboard', run: () => { location.href = '/classic/today'; } },
  ];

  document.addEventListener('DOMContentLoaded', () => {
    const C = shell.commands;
    C.register(
      // jobs.retry: one declaration with ui-design commands.md. The v2 page runs no command yet, so it says where Retry runs.
      { id: 'jobs.retry', on: 'job', label: 'Retry', key: 't', risk: 'safe', bulk: true, primary: o => o.failed,
        when: o => !o.retry ? 'launchd refuses a retry for a job that is not failed or interrupted; the row says what to run'
          : 'v2 runs no commands yet; Retry runs from Operations › Jobs, and the retry line is above',
        cli: o => o.retry },
      { id: 'pr.open', on: 'pull request', label: 'Open on GitHub', key: 'o', risk: 'safe', primary: o => !!o.repo,
        when: o => !!(o.repo && o.number) || 'the row names no pull request',
        cli: o => `gh pr view --web --repo ${o.repo} ${o.number}`,
        run: o => { window.open(`https://github.com/${o.repo}/pull/${o.number}`, '_blank', 'noopener'); return 'Opened on GitHub in a new tab'; } },
    );

    $('rows').addEventListener('click', e => { if (e.target.closest('.rowact')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });
    document.addEventListener('shell:open', e => { if (ROWS.some(r => r.id === e.detail)) select(e.detail, true); });
    document.addEventListener('shell:picked', e => $('rows').querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))));
    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      if (c.id === 'refresh') return load();
      srcFilter = srcFilter === c.dataset.src ? null : c.dataset.src;
      document.querySelectorAll('.cell[data-src]').forEach(x => x.setAttribute('aria-pressed', x.dataset.src === srcFilter));
      applyFilter();
    });

    // Shapeshift bar: filter / new task / ask, previewed before commit.
    const input = $('shift'), prev = $('shift-preview'), as = $('shift-as'), ghost = $('ghost');
    let mode = 'filter';
    const guess = v => /\?$|^(why|what|how|which|when|should)\b/i.test(v) ? 'ask' : /^(p[1-4]\b|todo\b|add\b)/i.test(v) ? 'task' : 'filter';
    const setMode = m => { mode = m; prev.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', c.dataset.as === m)); paint(); };
    function paint() {
      const v = input.value.trim(), p = v.match(/\bp([1-4])\b/i);
      prev.hidden = !v; ghost.textContent = v ? `→ ${mode}` : '';
      put(as, mode === 'task' ? html`${I('list-todo')} task “${v.replace(/\bp[1-4]\b/gi, '').trim()}”${p ? ` · P${p[1]}` : ''}`
        : mode === 'ask' ? html`${I('message-square')} ask chat in fleet scope` : html`${I('filter')} filter the ledger`);
      query = mode === 'filter' ? v.toLowerCase() : '';
      if (DOC) applyFilter();
    }
    input.addEventListener('input', () => setMode(guess(input.value.trim())));
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); const o = ['filter', 'task', 'ask']; setMode(o[(o.indexOf(mode) + 1) % 3]); }
      if (e.key === 'Enter' && input.value.trim()) {
        e.preventDefault(); const v = input.value.trim();
        if (mode === 'task') { window.SHELL_CAPTURE({ kind: 'task', title: v }).then(shell.toast, err => shell.toast(err.message)); input.value = ''; paint(); }
        if (mode === 'ask') { shell.openChat(); shell.send(v); input.value = ''; paint(); }
      }
      if (e.key === 'Escape') { input.value = ''; paint(); input.blur(); }
    });
    prev.addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) { setMode(c.dataset.as); input.focus(); } });
    document.addEventListener('keydown', e => {
      if (e.target.matches('input, textarea') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest('.actmenu')) return;
      if (e.key === '/') { e.preventDefault(); input.focus(); }
      if (e.key === 'j' || e.key === 'k') {
        const vis = [...$('rows').querySelectorAll('tr[data-id]')].filter(tr => tr.getClientRects().length); if (!vis.length) return;
        const i = vis.findIndex(tr => tr.getAttribute('aria-selected') === 'true');
        const n = vis[Math.max(0, Math.min(vis.length - 1, i + (e.key === 'j' ? 1 : -1)))];
        select(n.dataset.id, false); n.scrollIntoView({ block: 'nearest' });
      }
      if (e.key === 'Escape' && srcFilter) clearSource();
    });

    reading = shell.read({
      source: '/api/now', what: 'the Now rows', adopt, clear, draw,
      current: () => new URLSearchParams(location.search).get('row'),
      first: () => [...$('rows').querySelectorAll('tr[data-id]')].find(tr => !tr.hidden)?.dataset.id,
      // A row the filter hides is not selected: reconcile moves to the first row it shows, or clears.
      select: id => (shown(id) ? select(id, false) : reconcile()),
      unselect,
    });
    load();
  });
})();
