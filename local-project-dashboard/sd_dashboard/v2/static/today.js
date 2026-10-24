// Today v2 (sd:2110): the page script. It loads before shell.js, which reads what it declares.
// One page, no tabs (sd:5921): the annunciator, Needs you and In motion, all from /api/now (now_screen.document).
// Needs you is the document's `needs`: every fact that asks for an act, ranked in the bands, a kind of many rows folded into
// one group row. A group opens in place to its first rows, then "N more". A lamp or an intent shows only its rows; the
// Shapeshift bar's text filters the same list. The badge is the read's own (now_screen._attention), the tally's count.
// A snoozed row (sd:1896) is in the document's `snoozed`: snooze.js draws it under the list with its Unsnooze.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
// moved:start
// sd:5781: an old address of a view that moved opens the view's new page, keeping its query (?row=). sd:5921: an old Today
// view (#view=now|work|inbox|status) is this page now, so its hash leaves the address.
{
  const was = (/^#view=([a-z]+)$/.exec(location.hash) || [])[1];
  const moved = { decisions: '/decide#view=decisions', github: '/github#view=pulls', issues: '/github#view=issues' }[was];
  if (moved) location.replace(moved.replace('#', `${location.search}#`));
  else if (['now', 'work', 'inbox', 'status'].includes(was)) history.replaceState(null, '', `/today${location.search}`);
}
// moved:end
window.PAGE_FILTER = { '': 'Needs you' };
(() => {
  // The rail's map (built sections, and the old screen each unported one opens) is /ui/sections.js, loaded before this.
  // Capture (n) and the field's task mode file through the shell's default capture (sd:3520): POST /api/items.

  // now_screen bands → design.md state grammar.
  const STATE = { broken: 'warning', look: 'caution', queued: 'queued' };
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  // Each source's label, icon, and the page that shows it in full, with that page's name (sd:3402).
  const SRC = { jobs: ['Jobs', 'activity', '/management', 'Management'], prs: ['Pull requests', 'git-pull-request', '/operations?area=trackers', 'Trackers'], sessions: ['Sessions', 'bot', '/operations?area=sessions', 'Operations'], repos: ['Repos', 'hard-drive-download', '/management', 'Management'],
    lane: ['Lane', 'git-merge', '/queue', 'Queue'], reports: ['Reports', 'file-text', '/reports', 'Reports'], mail: ['Mail', 'mail', '', ''], tasks: ['Tasks', 'list-checks', '/tasks', 'Tasks'], briefs: ['Briefs', 'book-open', '/briefs', 'Briefs'] };
  const TYPE = { job: 'job', pr: 'pull request', ahead: 'repo', dirty: 'repo', pinned: 'repo', worktree: 'sessions', dark: 'collector', item: 'work item',
    report: 'report', mail: 'mail thread', lane: 'lane entry', brief: 'brief', group: 'group' };
  const KIND = { job: 'Job', pr: 'Pull request', ahead: 'Repo', dirty: 'Repo', pinned: 'Repo', worktree: 'Sessions', dark: 'Collector', item: 'Work item',
    report: 'Report', mail: 'Mail', lane: 'Lane entry', brief: 'Brief', group: 'Group' };
  // The intents (sd:5921): views over the one list, not tabs. Each says which rows it keeps.
  const INTENTS = [['fix', 'Fix', r => r.band === 'broken'], ['reply', 'Reply', r => ['reports', 'mail', 'pulls'].includes(r.lamp)],
    ['ship', 'Ship', r => r.kind === 'lane' || r.id === 'group:ready' || r.state === 'ready_to_send'], ['read', 'Read', r => r.source === 'briefs']];
  const SHOWN = 5; // a group's members, and In motion's items, before "N more"
  const { html, put } = window.markup;
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;

  let DOC = null, NEEDS = [], ALL = new Map(), SNOOZED = [], ACTIVE = null, srcFilter = null, intent = null, query = '', picked = [];
  const OPEN = new Set(), FULL = new Set(); // the groups opened, and the ones showing every member
  let motionAll = false;
  const $ = id => document.getElementById(id);
  const row = () => new URLSearchParams(location.search).get('row'); // the selected row
  const low = s => String(s || '').toLowerCase();
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const SR = text => html`<span class="sr">${text}</span>`;
  const COLS = html`<tr><th class="g">${SR('State')}</th><th>What</th><th>Why · next step</th><th>Source</th><th class="num">Age</th><th class="act">${SR('Actions')}</th></tr>`;

  // Age: a row's time against the read's, in the largest whole unit. A row with no time has none.
  function age(at) {
    const t = Date.parse(String(at || '').replace(' ', 'T')), now = Date.parse(DOC.now);
    if (!at || isNaN(t) || isNaN(now)) return '';
    const m = Math.max(0, Math.round((now - t) / 6e4));
    return m < 60 ? `${m}m` : m < 2880 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
  }
  // A row passes the lamp, the intent and the text; a group passes when it or any member does.
  function passes(r) {
    const one = x => (!srcFilter || x.lamp === srcFilter) && (!intent || INTENTS.find(i => i[0] === intent)[2](x))
      && (!query || low(`${x.what} ${x.detail} ${x.source}`).includes(query));
    return one(r) || (r.members || []).some(one);
  }

  function facts(r) {
    const f = { Detail: r.detail, Source: SRC[r.source]?.[0] || r.source, Band: `${STATE[r.band]} · rank ${r.rank}`, Id: r.id };
    if (r.retry) f['Retry line'] = r.retry;
    return f;
  }

  // One selection (sd:5654): pick names the row in ?row=, marks it alone aria-selected, puts its Details and makes it the
  // shell's selection.
  function pick(id, draw, open) {
    mark(id);
    put($('details'), draw());
    shell.commands.select(id);
    const u = new URL(location.href); u.searchParams.set('row', id); history.replaceState(null, '', u);
    $('details').setAttribute('data-swap', ''); requestAnimationFrame(() => $('details').removeAttribute('data-swap'));
    if (open) shell.openPane('tab-details');
  }
  const listed = id => ALL.has(id);
  const day = iso => String(iso || '').slice(0, 10), hm = iso => String(iso || '').slice(11, 16);

  function details(r) {
    const s = STATE[r.band], head = html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> ${KIND[r.kind] || r.kind} · ${s}</p>`;
    const act = html`<h3>Act</h3>${shell.commands.bar(r.id)}`;
    if (r.group) return html`${head}<h2>${r.what}</h2><p class="why">${r.detail}</p>
      <ul class="members">${r.members.slice(0, 10).map(m => html`<li>${m.item ? html`<a href="${shell.itemHref(m.item)}">${m.what}</a>` : m.what}<span class="via">${m.detail}</span></li>`)}</ul>
      ${r.members.length > 10 ? html`<p class="why">${r.members.length - 10} more; open the group in the list for each.</p>` : ''}${act}`;
    if (r.kind === 'report') return html`${head}<h2>${r.title}</h2>
      <dl><dt>Filed</dt><dd>${day(r.at)} ${hm(r.at)} UTC</dd><dt>Job</dt><dd>${r.job || 'not recorded'}</dd>
        <dt>Host</dt><dd>${r.host || 'not recorded'}</dd><dt>Exit</dt><dd>${r.exit ?? 'not recorded'}</dd>
        ${r.repeats ? html`<dt>Repeats</dt><dd>${r.repeats}</dd>` : ''}<dt>Item</dt><dd><a href="${shell.itemHref(r.item)}">#${r.item}</a></dd></dl>
      ${r.basis ? html`<h3>Why</h3><div class="mailbody">${r.basis}</div>` : ''}
      <h3>Log${r.cut ? ' · last lines' : ''}</h3><div class="mailbody">${r.body || 'no text kept'}</div>
      <p class="why">Acknowledge closes the report, not the fault. The next failure opens a new one.</p>${act}`;
    if (r.kind === 'mail') return html`${head}<h2><a href="${r.url}">${r.subject || '(no subject)'}</a></h2>
      <dl><dt>From</dt><dd>${r.who || 'not recorded'}</dd><dt>Repo</dt><dd>${r.repo}</dd><dt>Sent</dt><dd>${r.at}</dd></dl>${act}`;
    if (r.kind === 'lane') return html`${head}<h2><a href="${shell.itemHref(r.item)}">sd:${r.item} ${r.title}</a></h2>
      <dl><dt>Repo</dt><dd>${r.repo}</dd><dt>Why</dt><dd>${r.detail}</dd><dt>Head</dt><dd>${(r.expected_head || '').slice(0, 12) || 'not recorded'}</dd>
        <dt>Ended</dt><dd>${r.at || 'not recorded'}</dd><dt>Merge</dt><dd>${r.runner_merge}</dd></dl>
      <p><a href="/queue">Open the lane in Queue</a></p>${act}`;
    if (r.kind === 'brief') return html`${head}<h2>${r.subj}</h2>
      <dl><dt>Day</dt><dd>${r.day}</dd><dt>Series</dt><dd>${r.src}</dd><dt>Words</dt><dd>${r.words}</dd></dl>
      <h3>Body · first lines</h3><div class="mailbody">${r.lead || ''}</div>${act}`;
    if (r.kind === 'item') return html`${head}<h2><a href="${shell.itemHref(r.item)}">${r.what}</a></h2>
      <dl><dt>Status</dt><dd>${r.state}</dd><dt>Due</dt><dd>${r.due || html`<span class="ph">none</span>`}</dd>
        ${r.repo ? html`<dt>Repo</dt><dd>${r.repo}</dd>` : ''}${r.item_kind ? html`<dt>Kind</dt><dd>${r.item_kind}</dd>` : ''}<dt>Id</dt><dd>sd:${r.item}</dd></dl>${act}`;
    return html`${head}<h2>${r.what}</h2>
      <dl>${Object.entries(facts(r)).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Read</dt><dd><time class="rel" data-long datetime="${DOC.now}"></time></dd></dl>
      ${SRC[r.source]?.[2] ? html`<p><a href="${SRC[r.source][2]}">Open ${SRC[r.source][0]} in ${SRC[r.source][3]}</a></p>` : ''}${act}`;
  }
  function select(id, open) {
    const r = ALL.get(id); if (!r) return;
    pick(id, () => details(r), open);
    shell.suggest([`Why is ${r.what.split(' ')[0]} on this list?`, 'What else changed around this time?', 'Rank what I should do in the next hour']);
  }

  // The lamps (sd:5921): each number counts the rows its filter shows; a source that was not read is unknown, never ok.
  function lamps() {
    const L = DOC.lane || {}, gates = L.gates || {}, why = { ...DOC.sources, reports: DOC.problems_error, mail: DOC.mail_error }, unread = DOC.mail_unread || [];
    document.querySelectorAll('.cell[data-src]').forEach(cell => {
      const s = cell.dataset.src, rows = NEEDS.filter(r => r.lamp === s && r.kind !== 'dark'), val = cell.querySelector('.val');
      const dark = NEEDS.find(r => r.lamp === s && r.kind === 'dark'), seen = (DOC.observed || {})[s];
      if ((dark && !dark.partial) || why[s]) { cell.dataset.state = 'unknown'; cell.title = dark?.detail || why[s]; put(val, html`<span class="ph">not read</span>`); return; }
      if (seen) cell.title = `observed ${seen.at.slice(11, 16)} UTC${seen.stale ? ` · stale: ${seen.stale}` : ''}`; else cell.removeAttribute('title');
      const st = rows.map(r => STATE[r.band]);
      cell.dataset.state = seen?.stale ? 'unknown' : st.includes('warning') ? 'warning' : st.includes('caution') ? 'caution' : st.length ? 'queued' : 'ok';
      const n = k => rows.filter(r => r.kind === k).length;
      // sd:2014: interrupted, unloaded and unknown jobs are rows too; the lamp counts the failed ones and names the rest.
      const failed = rows.filter(r => r.state === 'failed').length, other = rows.length - failed;
      put(val, s === 'jobs' ? html`<b>${failed}</b> failed${other ? html`<small>${other} other</small>` : ''}`
        : s === 'lane' ? html`<b>${rows.length}</b> blocked<small>${L.merging ?? 0} merging · ${L.next ?? 0} next${L.unread ? ` · ${L.unread} not read` : ''}</small>`
        : s === 'reports' ? html`<b>${rows.length}</b> open${DOC.problems_more ? html`<small>${DOC.problems_more}${DOC.problems_capped ? '+' : ''} more on Reports</small>` : ''}`
        : s === 'pulls' ? html`<b>${rows.length}</b> for you`
        : s === 'mail' ? html`<b>${rows.length}</b> waiting${DOC.mail_more || unread.length ? html`<small>${DOC.mail_more ? `${DOC.mail_more} more in Gmail` : ''}${DOC.mail_more && unread.length ? ' · ' : ''}${unread.length ? `${plural(unread.length, 'repo')} not read` : ''}</small>` : ''}`
        : html`<b>${rows.length}</b> to tidy<small>${n('ahead')} ahead · ${n('dirty')} dirty${n('pinned') ? ` · ${n('pinned')} pinned` : ''}</small>`);
      if (s === 'lane' && L.unread) cell.title = L.unread_why;
      // A repo whose mail could not be read is unknown, never none: the lamp names each reason (sd:4191).
      if (s === 'mail' && unread.length) cell.title = unread.map(u => `${u.repo}: mail not read: ${u.reason}`).join('\n');
      // A source read only in part (sd:5939) keeps its counts but reads unknown, never ok: its dark row says what is missing.
      if (dark) { cell.dataset.state = 'unknown'; cell.title = cell.title || dark.detail; }
    });
    const a = ACTIVE || {}, items = new Set((a.rows || []).map(r => r.item)), ses = $('lamp-sessions');
    const liveDark = NEEDS.find(r => r.lamp === 'sessions' && r.kind === 'dark');
    ses.dataset.state = a.error || liveDark ? 'unknown' : 'ok'; ses.title = a.error || liveDark?.detail || '';
    put(ses.querySelector('.val'), a.error ? html`<span class="ph">not read</span>` : html`<b>${items.size}</b> live<small>${plural((a.rows || []).length, 'session')}</small>`);
    const gate = $('lamp-gate'), load = (L.load || {}).load5, off = DOC.sources.lane || gates.error;
    gate.dataset.state = off ? 'unknown' : gates.waiting ? 'caution' : 'ok';
    gate.title = off || '';
    put(gate.querySelector('.val'), off ? html`<span class="ph">not read</span>`
      : html`<b>${gates.running}/${gates.cap}</b> ${gates.waiting} waiting${load != null ? html`<small>load5 ${load}</small>` : ''}`);
    // the oldest reading on the page: a kept fleet area can be older than the document.
    const d = new Date([DOC.now, ...Object.values(DOC.observed || {}).map(o => o.at)].map(t => new Date(t).getTime()).reduce((x, y) => Math.min(x, y)));
    put($('observed'), html`<b>${d.toISOString().slice(11, 16)}</b> UTC`);
  }

  // build (sd:2483): one object per row, for shell.read to put, and to retire once a read stops listing the row.
  function objectOf(r) {
    const pr = r.kind === 'pr' && r.id.match(/^pr:(.+)#(\d+):\d+$/);
    const job = r.kind === 'job' && r.id.match(/^job:(.+):[^:]+$/);
    return { id: r.id, type: r.group ? (r.id === 'group:briefs' ? 'brief group' : 'item group') : TYPE[r.kind] || r.kind, label: r.what,
             failed: r.kind === 'job' && !!r.retry, retry: r.retry, job: job && job[1], service: r.service,
             exit: r.exit, revision: r.revision, capability: r.capability, repo: pr ? pr[1] : r.repo, number: pr && +pr[2], seen: r.seen,
             item: r.item, itemKind: r.item_kind, state: r.state, url: r.url, fieldsRead: r.fields_read, until: r.until,
             repoPath: r.repo_path, laneRevision: r.lane_revision, head: r.expected_head, runnerMerge: r.runner_merge,
             members: r.members && r.members.map(m => ({ id: m.id, seen: m.seen, until: m.until, item: m.item })), group: r.id };
  }

  // The selected row, and the picked ones, marked again on the rows a redraw made.
  function mark(id) {
    $('rows').querySelectorAll('tr[data-id]').forEach(tr => { tr.setAttribute('aria-selected', tr.dataset.id === id); tr.toggleAttribute('data-picked', picked.includes(tr.dataset.id)); });
  }
  const BAND_NAME = { broken: 'Broken', look: 'Look', queued: 'Queued' };
  function line(r, member) {
    const s = STATE[r.band], src = SRC[r.source];
    return html`<tr data-id="${r.id}" data-src="${r.lamp || ''}"${member ? html` class="member" data-in="${member}"` : r.group ? html` class="grp"` : ''}>
      <td class="g g-${s}" title="${s}">${GLYPH[s]}</td>
      <td class="what">${r.group ? html`<button type="button" class="disc" data-toggle="${r.id}" aria-expanded="${String(OPEN.has(r.id))}" aria-label="${OPEN.has(r.id) ? 'Close' : 'Open'} ${r.what}">${I('chevron-right')}</button>` : ''}<button type="button" class="pick">${r.what}</button></td>
      <td class="detail">${r.detail}</td>
      <td class="src">${src ? I(src[1]) : ''}${r.source}</td>
      <td class="num age">${age(r.at)}</td>
      <td class="act">${shell.commands.rowActions(r.id)}</td></tr>`;
  }
  // Needs you: the rows the lamp, the intent and the text keep, a band head where the band changes, and each open group's
  // members under it. Nothing hides a row after the draw: a filter draws the list again.
  function needsTable() {
    const rows = NEEDS.filter(passes);
    put($('rows'), rows.length ? html`${rows.map((r, i) => {
      const head = !i || rows[i - 1].band !== r.band ? html`<tr class="band"><th colspan="6" scope="rowgroup">${BAND_NAME[r.band]} · ${rows.filter(x => x.band === r.band).length}</th></tr>` : '';
      if (!r.group || !OPEN.has(r.id)) return html`${head}${line(r)}`;
      const shown = FULL.has(r.id) ? r.members : r.members.slice(0, SHOWN), more = r.members.length - shown.length;
      return html`${head}${line(r)}${shown.map(m => line(m, r.id))}${more ? html`<tr class="more"><td></td><td colspan="5"><button type="button" class="linkbtn" data-more="${r.id}">${more} more</button></td></tr>` : ''}`;
    })}`
      : html`<tr class="empty"><td colspan="6">${NEEDS.length ? 'Nothing matches the filter. Esc clears it.' : 'Nothing needs you: every source was read and raised no row.'}</td></tr>`);
    const f = $('filtered'), n = rows.length;
    f.hidden = !(srcFilter || intent || query);
    f.textContent = n ? `${n} of ${NEEDS.length} shown${srcFilter ? ' · lamp ' + srcFilter : ''}${intent ? ' · ' + intent : ''}${query ? ` · “${query}”` : ''}. Esc clears.`
      : `Nothing matches${query ? ` “${query}”` : ''}. Press → to capture it as a task instead.`;
    mark(row());
  }
  function intents() {
    put($('intents'), html`<button class="chip" type="button" data-intent="" aria-pressed="${String(!intent)}">All <b>${NEEDS.length}</b></button>${INTENTS.map(([id, label, test]) => {
      const n = NEEDS.filter(r => test(r) || (r.members || []).some(test)).length;
      return html`<button class="chip" type="button" data-intent="${id}" aria-pressed="${String(intent === id)}"${n ? '' : html` disabled`}>${label} <b>${n}</b></button>`; })}`);
  }
  function render() {
    const count = s => NEEDS.filter(r => STATE[r.band] === s).length;
    put($('tally'), html`<span class="g-warning">■ ${count('warning')} broken</span><span class="g-caution">▲ ${count('caution')} look</span><span class="g-queued">◌ ${count('queued')} queued</span>${SNOOZED.length ? html`<span>${SNOOZED.length} snoozed</span>` : ''}`);
    const d = new Date(DOC.now), live = new Set(((ACTIVE || {}).rows || []).map(r => r.item)).size;
    put($('subhead'), html`${d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' })} · <b>${count('warning')} broken</b>, ${count('caution')} to look at · ${plural(live, 'item')} live · read <time class="rel" datetime="${DOC.now}"></time>`);
    attend();
    lamps();
    intents();
    needsTable();
    // A part of the list that could not be read is said under it, never left out quietly.
    const unread = [['Work', DOC.work_error], ['Briefs', DOC.briefs_error]].filter(([, why]) => why);
    $('unread').hidden = !unread.length;
    $('unread').textContent = unread.map(([k, why]) => `${k} could not be read: ${why}. Refresh tries again.`).join(' ');
    motion();
    dayTrack();
    window.snooze.draw($('snoozed'), SNOOZED);
  }

  // In motion (sd:3466, sd:5921): the document's `active`, one row per open item, its sessions in the row, longest-running
  // first; the first five, then "N more". Its title opens the item on Tasks. A launch herdr could not confirm is ▨, not ●;
  // a source that was not read is said under the table, never an empty list. The tally counts the lanes; Queue has the rest.
  const SOURCE = { fleet: 'worktrees and processes', lane: 'lane entries', launch: 'Fix notes', herdr: 'herdr agents' };
  const clock = iso => new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  function motion() {
    const a = ACTIVE || { rows: [], sources: {}, error: 'the document carried no sessions' }, rows = a.rows || [], L = DOC.lane || {};
    const items = [];
    rows.forEach(r => { const last = items[items.length - 1]; if (last && last.item === r.item) last.rows.push(r); else items.push({ item: r.item, title: r.title, rows: [r] }); });
    const shown = motionAll ? items : items.slice(0, SHOWN), more = items.length - shown.length;
    put($('active'), a.error ? html`<tr class="empty"><td colspan="5">Sessions could not be read: ${a.error}. Refresh tries again.</td></tr>`
      : items.length ? html`${shown.map(it => { const sure = it.rows.every(r => r.verified), s = sure ? 'ok' : 'unknown', top = it.rows[0]; return html`<tr data-item="${it.item}">
        <td class="g g-${s}" title="${sure ? 'live' : 'a launch not confirmed: herdr was not read'}">${GLYPH[s]}</td>
        <td class="what"><a href="${window.shell.itemHref(it.item)}">#${it.item} ${it.title}</a></td>
        <td class="detail"><ul class="sessions">${it.rows.map(r => html`<li>${r.verified ? '' : html`<span class="g-unknown" title="not confirmed">${GLYPH.unknown} </span>`}${r.session}${r.elapsed ? html` · ${r.elapsed}` : ''}<span class="via">${r.via}</span></li>`)}</ul></td>
        <td class="when">${top.started ? html`<time datetime="${top.started}" title="${top.started.slice(11, 16)} UTC">${clock(top.started)}</time>` : 'not known'}</td>
        <td class="num">${top.elapsed || 'not known'}</td></tr>`; })}${more ? html`<tr class="more"><td></td><td colspan="4"><button type="button" class="linkbtn" id="motion-more">${more} more: ${items.slice(SHOWN, SHOWN + 3).map(it => `sd:${it.item}`).join(', ')}${more > 3 ? ' …' : ''}</button></td></tr>` : ''}`
      : html`<tr class="empty"><td colspan="5">No open item has a live session.</td></tr>`);
    const dark = Object.entries(a.sources || {}).filter(([, why]) => why);
    put($('active-tally'), html`<span>${plural(items.length, 'item')} live</span><span>${L.merging ?? 0} merging</span><span>${L.landed ?? 0} landed today in ${plural(L.landed_lanes ?? 0, 'lane')}</span><a href="/queue?show=all">Queue</a>${dark.length ? html`<span class="g-caution">▲ ${dark.length} not read</span>` : ''}`);
    const note = $('active-note');
    note.hidden = !dark.length;
    note.textContent = dark.map(([k, why]) => `${SOURCE[k] || k} not read: ${why}`).join(' · ');
  }

  // The day track (sd:5921): the document's `track`, the last 24 hours as marks in four lanes, each linked to its row, with
  // the same marks as a table in a details and a key of four counts linked to Tasks. A track that was not read says so;
  // a day with no mark says that in one line, never an empty axis. Landed links the items done less than a day ago
  // (sd:5934): Tasks' In status bucket 0, as a task has no merged or delivered status.
  const LANES = [['landed', 'Landed', 18], ['jobs', 'Jobs', 40], ['briefs', 'Briefs', 58], ['live', 'Live', 74]];
  const markHref = (lane, m) => lane === 'landed' || lane === 'live' ? shell.itemHref(m.item) : `?row=${encodeURIComponent(m.id)}`;
  function dayTrack() {
    const T = DOC.track || { error: 'the document carried no track' };
    if (T.error) {
      put($('track-plot'), html`<p class="why">The day track was not read: ${T.error}. Refresh tries again.</p>`);
      put($('flowkey'), html``); put($('track-rows'), html``); $('track-table').hidden = true; return;
    }
    const t0 = Date.parse(T.since), t1 = Date.parse(T.until), X0 = 64, W = 900, x = at => (X0 + (W - X0 - 8) * (Date.parse(at) - t0) / (t1 - t0)).toFixed(1);
    const marks = LANES.flatMap(([k, label]) => T[k].map(m => ({ ...m, lane: k, label })));
    const said = `Last 24 hours: ${T.landed.length} landed, ${plural(T.jobs.length, 'job')} failed, ${plural(T.briefs.length, 'brief')} filed, ${plural(T.live.length, 'session')} started`;
    const ticks = [];
    for (let t = Math.ceil(t0 / 36e5) * 36e5; t <= t1; t += 36e5) if (new Date(t).getHours() % 3 === 0) ticks.push(t);
    const mark = m => { const at = x(m.at), tip = html`<title>${hm(m.at)} UTC · ${m.what}</title>`;
      return html`<a href="${markHref(m.lane, m)}">${m.lane === 'landed' ? html`<line class="land" x1="${at}" y1="10" x2="${at}" y2="26">${tip}</line>`
        : m.lane === 'jobs' ? html`<rect class="fail" x="${(at - 4).toFixed(1)}" y="36" width="8" height="8">${tip}</rect>`
        : m.lane === 'briefs' ? html`<circle class="brief" cx="${at}" cy="58" r="3.5">${tip}</circle>`
        : html`<rect class="sess" x="${at}" y="72" width="${Math.max(2, W - 8 - at).toFixed(1)}" height="3">${tip}</rect>`}</a>`; };
    put($('track-plot'), marks.length ? html`<svg viewBox="0 0 ${W} 100" role="img" aria-label="${said}">
      ${ticks.map(t => html`<line class="grid" x1="${x(new Date(t).toISOString())}" y1="6" x2="${x(new Date(t).toISOString())}" y2="80"/><text class="axis" x="${x(new Date(t).toISOString())}" y="94" text-anchor="middle">${String(new Date(t).getHours()).padStart(2, '0')}:00</text>`)}
      ${LANES.map(([, label, y]) => html`<text class="lane-l" x="0" y="${y + 4}">${label}</text>`)}
      ${marks.map(mark)}<line class="now" x1="${x(T.until)}" y1="4" x2="${x(T.until)}" y2="82"/></svg>`
      : html`<p class="why">Nothing landed, failed, was filed or started in the last 24 hours.</p>`);
    const K2 = T.key;
    put($('flowkey'), html`<li><a href="/tasks?view=list&amp;status=done&amp;age=0&amp;realm=all">Landed 24h</a></li><li><b>${K2.landed}</b></li>
      <li><a href="/queue">Landing</a></li><li><b>${K2.landing}</b></li>
      <li><a href="/tasks?view=list&amp;due=week&amp;realm=all">Due this week</a></li><li><b>${K2.due_week}</b></li>
      <li><a href="/tasks?view=list&amp;status=blocked&amp;realm=all">Blocked</a></li><li><b>${K2.blocked}</b></li>`);
    $('track-table').hidden = !marks.length;
    put($('track-rows'), html`${[...marks].sort((a, b) => a.at < b.at ? 1 : -1).map(m => html`<tr><td class="when"><time datetime="${m.at}">${clock(m.at)}</time></td><td>${m.label}</td><td class="what"><a href="${markHref(m.lane, m)}">${m.what}</a></td></tr>`)}`);
  }

  // The badge is the read's own (sd:3446, now_screen._attention): Needs you's broken rows, else its look rows.
  function attend() {
    shell.attention(DOC && DOC.attention || { state: 'unknown', n: 0, what: 'rows' });
  }
  function unselect() {
    const u = new URL(location.href); u.searchParams.delete('row'); history.replaceState(null, '', u);
    put($('details'), html`<p class="why">${!DOC ? 'Nothing is selected: Today could not be read.' : NEEDS.length ? 'No row matches the filter, so nothing is selected. Esc clears it.' : 'Nothing is selected: nothing needs you.'}</p>`);
  }
  const reconcile = () => shell.reconcile({ rows: $('rows').querySelectorAll('tr[data-id]'), current: row(),
    keep: () => false, select: id => select(id, false), clear: unselect });
  function applyFilter() { needsTable(); reconcile(); }

  // ---------- Reading (build, sd:2483): shell.read reads /api/now ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a row the read no longer lists
  // runs no command, a selection whose row is gone moves to the first row the filter shows, and a failed read clears the page.
  let reading = null, failed = '', reads = 0;
  function adopt(doc) {
    DOC = doc; NEEDS = doc.needs || []; SNOOZED = doc.snoozed; ACTIVE = doc.active || null;
    ALL = new Map(); NEEDS.forEach(r => { ALL.set(r.id, r); (r.members || []).forEach(m => ALL.set(m.id, m)); });
    document.body.dataset.observed = doc.now;
    return { objects: [...[...ALL.values()].map(objectOf), ...SNOOZED.map(window.snooze.object)],
             state: doc.snooze_error ? window.snooze.unread(doc.snooze_error, '/api/now') : null };
  }
  function clear(err) { DOC = null; NEEDS = []; ALL = new Map(); SNOOZED = []; ACTIVE = null; failed = err.message; delete document.body.dataset.observed; }
  function draw() {
    if (DOC) return render();
    // Nothing from the last read stays on screen: rows, counts, lamps, the observed time, the selection and the badge.
    $('subhead').textContent = `Today could not be read: ${failed}. Refresh tries again.`;
    document.querySelectorAll('#annunciator .cell:not(.clock)').forEach(cell => { cell.dataset.state = 'unknown'; cell.title = 'Today could not be read'; put(cell.querySelector('.val'), html`<span class="ph">not read</span>`); });
    put($('observed'), html`<span class="ph">not read</span>`);
    put($('tally'), html``); put($('intents'), html``);
    put($('rows'), html`<tr class="empty"><td colspan="6">Needs you could not be read: ${failed}</td></tr>`);
    put($('active'), html`<tr class="empty"><td colspan="5">Sessions could not be read: ${failed}. Refresh tries again.</td></tr>`);
    put($('active-tally'), html``);
    $('active-note').hidden = true; $('unread').hidden = true;
    put($('track-plot'), html`<p class="why">The day track was not read: ${failed}.</p>`); put($('flowkey'), html``); $('track-table').hidden = true;
    window.snooze.draw($('snoozed'), []);
    attend();
  }
  function load() {
    $('refresh').setAttribute('aria-busy', 'true'); reads++;
    return reading.load().finally(() => { if (!--reads) $('refresh').removeAttribute('aria-busy'); });
  }

  function clearSource() { srcFilter = null; intent = null; document.querySelectorAll('.cell[data-src]').forEach(x => x.setAttribute('aria-pressed', 'false')); if (DOC) { intents(); applyFilter(); } }

  // build (sd:3521): capture's repo default is the row's: a repo row names it in its id, a pull request as owner/name.
  window.PAGE_REPO = o => o && o.type === 'repo' ? o.id.split(':')[1] : (o && o.repo) || null;

  // Read again (sd:5638): the shell's head button presses the Observed lamp.
  window.PAGE_AGAIN = { title: 'Read Today again', run: () => $('refresh').click() };

  window.PAGE_COMMANDS = [
    { label: 'Clear the filter', icon: 'filter', run: clearSource },
    { label: 'Read Today again', icon: 'rotate-ccw', run: () => load() },
  ];

  document.addEventListener('DOMContentLoaded', () => {
    const C = shell.commands;
    const K = window.commands;
    const post = (path, body) => window.shell.post(path, body).then(out => reading.reread().then(() => out), err => { load(); throw err; });
    // jobs.retry (commands.js). build (sd:3386): it posts /api/jobs/<job>/retry with the revision the read brought, as
    // Management does; launchd starts the run. A refused retry reads Today again; one with no answer may have started, so it
    // says the outcome is not known.
    const retry = K.retry({ job: o => ({ name: o.job, failed: o.failed, exit: o.exit, capability: o.capability, sends: o.retry }),
      run: o => window.shell.post(`/api/jobs/${encodeURIComponent(o.job)}/retry`, { revision: o.revision }).then(
        out => reading.reread().then(() => K.answered(out, o.job, 'retried', `Retry started · ${o.job}`)),
        err => { load(); throw err.status ? err : new Error(`no answer (${err.message}), so whether launchd started it is not known; Today is read again`); }) });
    const open = K.openPullRequest({ pr: o => ({ repo: o.repo, number: o.number }), primary: o => !!o.repo });
    // Fix (sd:5652): each Now row's act, routed by kind to what already addresses it: a failed or interrupted job's Run now,
    // a pull request's Open on GitHub, the pull request collector's Re-run collector, and for every other row claude in
    // herdr on it (fix_session.start_row). Fix is the route's own declaration under its label, so its when, ask, line and
    // run are the route's: a row the route is off for has Fix off with that reason, never a Fix that does nothing.
    const sync = K.syncCollector({ reread: () => reading.reread(), page: 'Today' });
    const session = { label: 'Claude in herdr', does: 'Asks first, then starts claude in herdr to fix this row.', askFirst: true,
      cli: () => 'no sd verb: the dashboard runs local-herdr/herdr.sh launch', sends: o => `POST /api/now/fix ${JSON.stringify({ row: o.id })}`,
      consequence: o => `Opens a herdr workspace and starts claude on "${o.label}", in ${o.type === 'repo' ? 'its checkout' : 'the system checkout'}. A second Fix starts a second session: look in herdr first.`,
      run: o => window.shell.post('/api/now/fix', { row: o.id }).then(
        out => reading.reread().then(() => `Fix started · ${o.label} · claude in herdr workspace ${out.fix.workspace}, pane ${out.fix.pane}`),
        err => { load(); throw err.status ? err : new Error(`no answer (${err.message}), so whether a session started is not known; look in herdr before you press Fix again`); }) };
    const fix = (on, glyph, route) => {
      const via = (k, none) => (o, v) => { const r = route(o), f = r[k]; return typeof f === 'function' ? f(o, v) : f === undefined ? none : f; };
      return { id: `${on}.fix`, on, label: 'Fix', key: 'f', icon: glyph, risk: 'safe',
        does: o => { const d = route(o).does; return typeof d === 'function' ? d(o) : d; },
        when: via('when', true), askFirst: via('askFirst', false), consequence: via('consequence', ''), cli: via('cli', ''), sends: via('sends', ''),
        primary: o => { const r = route(o); return (!r.when || r.when(o) === true) && (!r.primary || !!r.primary(o)); },
        run: (o, v) => route(o).run(o, v) };
    };
    // A group's act on every member (sd:5921): one write each, through the route a member row's own act posts.
    const each = (o, body) => Promise.all(o.members.map(m => window.shell.post('/api/snooze', body(m))))
      .then(() => reading.reread(), err => { load(); throw err; });
    const week = () => new Date(Date.now() + 7 * 864e5).toISOString();
    const BULK = 25; // shell.js asks first for a bulk act on more rows than this; a group act on more members asks too
    const lane = action => o => post('/api/queue/move', { repo: o.repoPath, item: o.item, action, revision: o.laneRevision, head: o.head });
    const go = (href, said) => () => { location.href = href; return said; };
    C.register(
      fix('job', 'play', () => retry),
      fix('pull request', 'arrow-up-right', () => open),
      fix('collector', 'bot', o => o.id === 'dark:prs' ? sync : session),
      fix('repo', 'bot', () => session),
      fix('sessions', 'bot', () => session),
      retry,
      // Each row kind's commands from the page that shows it in full (sd:3449).
      K.showLog({ job: o => ({ name: o.job }) }),
      K.printJob({ job: o => ({ name: o.job, service: o.service, exit: o.exit }) }),
      open,
      // Fix (sd:3456, commands.js) on a work row, with the revision the read gave; a refused one reads Today again.
      K.fix('work item', { item: o => o.item, report: o => o.itemKind === 'report', post: (o, path) => post(path, { revision: o.revision }) }),
      { id: 'today.item.open', on: 'work item', label: 'Open', does: 'Opens the item on Tasks.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false, primary: () => true,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      // A problem report (sd:5921): Acknowledge posts the Reports page's route with the revision the read gave.
      { id: 'today.report.ack', on: 'report', label: 'Acknowledge', does: 'Closes the report, not the fault: the next failure opens a new one.', key: 'a', icon: 'check', risk: 'safe', bulk: true, primary: () => true,
        when: o => o.fieldsRead || `its fields cannot be read: sd reports acknowledge ${o.item}`,
        cli: o => `sd reports acknowledge ${o.item}`, sends: o => `POST /api/reports/${o.item}/acknowledge {revision}`,
        run: o => post(`/api/reports/${o.item}/acknowledge`, { revision: o.revision }).then(() => `Acknowledged · ${o.label}`) },
      { id: 'today.report.open', on: 'report', label: 'Open report', does: 'Opens the report on Tasks.', key: 'o', risk: 'safe', executes: false,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      { id: 'today.mail.open', on: 'mail thread', label: 'Open thread', does: 'Opens the thread in Gmail in a new tab.', key: 'o', icon: 'mail', risk: 'safe', executes: false, primary: () => true,
        cli: () => 'no CLI: the thread is in Gmail', run: o => { window.open(o.url, '_blank', 'noopener'); return `${o.label} opens in Gmail`; } },
      // A blocked lane entry (sd:5921): Queue's Retry and Approve, posted with the lane's revision and the head the read showed.
      { id: 'today.lane.retry', on: 'lane entry', label: 'Retry', does: 'Queues the entry again at the head it ended at.', key: 'r', icon: 'rotate-ccw', risk: 'safe', primary: () => true,
        cli: o => `sd-ship -C ${o.repoPath} lane retry ${o.item} --expected-head ${o.head}`, sends: () => 'POST /api/queue/move {action: retry}',
        when: o => !!o.head || 'the entry recorded no head; retry it from Queue', run: o => lane('retry')(o).then(() => `Queued again · sd:${o.item}`) },
      // Approve grants the merge, which nothing takes back, so it asks first as Queue's does; Retry runs at once there too.
      { id: 'today.lane.approve', on: 'lane entry', label: 'Approve', does: 'Asks first, then queues the entry again with the merge grant.', key: 'p', risk: 'safe', askFirst: true,
        consequence: o => `The entry is queued again at head ${String(o.head).slice(0, 12)}, and the runner may merge it to the default branch. A merge is not taken back.`,
        cli: o => `sd-ship -C ${o.repoPath} lane retry ${o.item} --expected-head ${o.head} --manual`, sends: () => 'POST /api/queue/move {action: approve}',
        when: o => o.runnerMerge === 'manual' || 'this repo merges without a grant (runner_merge auto)', run: o => lane('approve')(o).then(() => `Approved · sd:${o.item}`) },
      { id: 'today.lane.queue', on: 'lane entry', label: 'Open in Queue', does: 'Opens the lane on Queue, where Cancel is.', key: 'q', risk: 'safe', executes: false,
        cli: o => `sd-ship -C ${o.repoPath} lane list`, run: go('/queue', 'Queue opens') },
      // A new brief: Seen is the Today snooze held until the brief changes (sd:5752), the mark the place pages read too.
      { id: 'today.brief.seen', on: 'brief', label: 'Seen', does: 'Hides the brief from Today until its text grows.', key: 's', icon: 'eye', risk: 'safe', bulk: true, primary: () => true,
        cli: () => 'no sd verb: the dashboard writes the snooze (POST /api/snooze)',
        run: o => post('/api/snooze', { page: 'today', row: o.id, until: o.until, seen: o.seen }).then(() => `Seen · ${o.label}`) },
      { id: 'today.brief.read', on: 'brief', label: 'Read', does: 'Opens the brief on Tasks.', key: 'o', risk: 'safe', executes: false,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      // A group row (sd:5921): one act for every member.
      { id: 'today.group.triage', on: 'item group', label: 'Triage', does: 'Opens Tasks on the past-due items, every realm.', key: 't', icon: 'list-checks', risk: 'safe', executes: false,
        primary: o => o.group === 'group:late', when: o => o.group === 'group:late' || 'only the past-due group triages',
        cli: () => 'sd today', run: go('/tasks?view=list&due=overdue&realm=all', 'Tasks opens on the past-due items') },
      { id: 'today.group.open', on: 'item group', label: 'Open', does: 'Opens the members on Tasks or, for posts ready to send, on Writing.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false,
        primary: o => o.group !== 'group:late', cli: () => 'sd today',
        run: o => { location.href = o.group === 'group:ready' ? '/writing' : '/tasks?view=list&realm=all'; return 'Opens'; } },
      { id: 'today.group.snooze', on: 'item group', label: 'Snooze all 1w', does: 'Hides every member for a week, one snooze each.', key: 'z', risk: 'safe',
        askFirst: o => o.members.length > BULK, consequence: o => `Writes ${o.members.length} snoozes, one per member; each member comes back on its own after a week or by Unsnooze.`,
        cli: () => 'no sd verb: the dashboard writes one snooze per member (POST /api/snooze)',
        run: o => each(o, m => ({ page: 'today', row: m.id, until: week(), seen: m.seen })).then(() => `Snoozed ${o.members.length} for a week · ${o.label}`) },
      { id: 'today.group.read', on: 'brief group', label: 'Read', does: 'Opens Briefs.', key: 'o', icon: 'book-open', risk: 'safe', executes: false, primary: () => true,
        cli: () => 'sd reports brief list', run: go('/briefs', 'Briefs opens') },
      { id: 'today.group.seen', on: 'brief group', label: 'Mark all seen', does: 'Marks every new brief seen, one mark each, until its text grows.', key: 's', risk: 'safe',
        askFirst: o => o.members.length > BULK, consequence: o => `Marks ${o.members.length} briefs seen; each comes back when its text grows.`,
        cli: () => 'no sd verb: the dashboard writes one snooze per brief (POST /api/snooze)',
        run: o => each(o, m => ({ page: 'today', row: m.id, until: m.until, seen: m.seen })).then(() => `Seen · ${o.members.length} briefs`) },
    );
    // Snooze (sd:1896) on every row type that ends by time: the row leaves Today until then, and Unsnooze brings it back.
    window.snooze.register(C, { page: 'today', types: ['job', 'pull request', 'repo', 'sessions', 'collector', 'work item', 'report', 'mail thread'], reread: () => load() });

    $('rows').addEventListener('click', e => {
      if (e.target.closest('.rowact')) return;
      const t = e.target.closest('[data-toggle]'), more = e.target.closest('[data-more]');
      if (t) { OPEN.has(t.dataset.toggle) ? OPEN.delete(t.dataset.toggle) : OPEN.add(t.dataset.toggle); return needsTable(); }
      if (more) { FULL.add(more.dataset.more); return needsTable(); }
      const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
    });
    $('active').addEventListener('click', e => { if (e.target.closest('#motion-more')) { motionAll = true; motion(); } });
    $('intents').addEventListener('click', e => {
      const c = e.target.closest('[data-intent]'); if (!c) return;
      intent = c.dataset.intent && c.dataset.intent !== intent ? c.dataset.intent : null; intents(); applyFilter();
    });
    document.addEventListener('shell:open', e => { if (listed(e.detail)) select(e.detail, true); });
    document.addEventListener('shell:picked', e => { picked = e.detail; mark(row()); });
    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      if (c.id === 'refresh') return load();
      srcFilter = srcFilter === c.dataset.src ? null : c.dataset.src;
      document.querySelectorAll('.cell[data-src]').forEach(x => x.setAttribute('aria-pressed', x.dataset.src === srcFilter));
      if (DOC) applyFilter();
    });
    put($('rows-head'), COLS); // drawn before the read, so a failed one keeps the columns
    put($('active-head'), html`<tr><th class="g">${SR('State')}</th><th>Item</th><th>Session</th><th>Started</th><th class="num">Elapsed</th></tr>`);

    // The shell's Shapeshift bar (sd:5539) filters Needs you through shell:filter.
    document.addEventListener('shell:filter', e => { query = e.detail.q.toLowerCase(); if (DOC) applyFilter(); });
    // The shell walks the rows on j / k, and Esc clears the lamp and intent through clear() (sd:3525).
    window.PAGE_LIST = { rows: () => $('rows').querySelectorAll('tr[data-id]'), select: id => select(id, false),
      clear: () => !!(srcFilter || intent) && (clearSource(), true) };

    reading = shell.read({
      source: '/api/now', what: 'The Today rows', adopt, clear, draw,
      current: row,
      first: () => $('rows').querySelector('tr[data-id]')?.dataset.id,
      // ?row= on a group member opens its group, so the selection is a row on screen.
      select: id => {
        const g = NEEDS.find(r => (r.members || []).some(m => m.id === id));
        if (g && !OPEN.has(g.id)) { OPEN.add(g.id); FULL.add(g.id); needsTable(); }
        return [...$('rows').querySelectorAll('tr[data-id]')].some(tr => tr.dataset.id === id) ? select(id, false) : reconcile();
      },
      unselect,
    });
    load();
  });
})();
