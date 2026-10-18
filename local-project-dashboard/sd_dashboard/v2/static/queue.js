// Queue (sd:2585): /api/queue (queue_screen.py), one section per managed repository's lane, one row per item in its state:
// merging, next, building, blocked, cancelled, landed. A lane with no row shares one Idle line, each with its host (sd:3284). Up, down, top, hold, release and cancel post /api/queue/move with the lane's
// revision; the server runs sd-ship lane move|hold|release|cancel|retry, refuses a stale revision, and the page reads again.
// Cancel asks first (sd:3012). A cancelled row offers Re-queue (sd:3398) when the installed pack's retry takes a cancelled
// entry (DOC.requeue, pack sd:3389): it posts action `requeue` with the head the row showed, and the server runs
// `lane retry --expected-head`, which refuses a moved branch, a newer entry or a merged head. It runs at once, as Retry
// does: Cancel takes it back. Without the pack's support the row says why Re-queue is off (DOC.requeue_off).
// A running row with a claim offers Release (sd:3337, D3 of pack sd:3174): it asks first, naming the holder, claim age and
// what the pack does at that step, then posts action `reclaim` with the claim the row showed (`<holder token>:<step>`);
// the server runs `lane cancel --expected-claim`, so a claim that moved on since the read releases nothing.
// A blocked row offers Retry (lane retry) and, on a runner_merge=manual repo, Approve (lane retry --manual), only when the
// installed pack's retry takes --expected-head (DOC.retry). Each button carries the head its row showed, and the server
// passes it on, so neither acts on an entry that ended at another head since the read. Retry runs at once: Cancel takes
// the new entry back. Approve asks first: it grants the runner the merge to the default branch, which nothing takes back.
// Show draws the active rows by default: merging, next (a held one keeps its place), building and blocked, each still
// waiting on the runner, a builder or the operator. All adds the ended ones, cancelled and landed; ?show=all keeps it.
// The badge and each lane's counts read every row, whatever Show draws; a lane with no row to draw joins the Idle line.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { live: ['live', '●'], ok: ['ok', '●'], caution: ['caution', '▲'], warning: ['warning', '■'], queued: ['queued', '◌'], unknown: ['unknown', '▨'] };
  const ICON = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const HM = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const at = s => s ? HM.format(new Date(s)) : 'not recorded';
  const dur = s => s == null ? 'not recorded' : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
  const TREND = { rising: '↑ rising', falling: '↓ falling', steady: '→ steady' };
  const ACTIVE = ['merging', 'next', 'building', 'blocked'];
  let DOC = null, busy = false, show = 'active';
  const drawn = l => show === 'all' ? l.rows : l.rows.filter(r => ACTIVE.includes(r.state));
  const said = {}; // lane path -> { ok, text }: the last write's result, shown in its lane until the next one

  const btn = (lane, r, act, label, icon, off) => html`<button class="btn quiet sm" type="button" data-act="${act}" data-item="${String(r.item)}" data-repo="${lane.path}" aria-label="${label} sd:${String(r.item)}" title="${label}"${off || busy ? html` disabled` : ''}>${ICON(icon)}</button>`;
  function controls(lane, r, last) {
    return html`<div class="ctl" role="group" aria-label="Order of sd:${String(r.item)}">${btn(lane, r, 'up', 'Move up', 'arrow-up', r.position === 1)}${btn(lane, r, 'down', 'Move down', 'arrow-down', r.position === last)}<button class="btn quiet sm" type="button" data-act="top" data-item="${String(r.item)}" data-repo="${lane.path}" aria-label="Move sd:${String(r.item)} to the top"${r.position === 1 || busy ? html` disabled` : ''}>Top</button><button class="btn quiet sm" type="button" data-act="${r.held ? 'release' : 'hold'}" data-item="${String(r.item)}" data-repo="${lane.path}"${busy ? html` disabled` : ''}>${r.held ? 'Release' : 'Hold'}</button><button class="btn quiet sm" type="button" data-act="cancel" data-item="${String(r.item)}" data-repo="${lane.path}" aria-label="Cancel sd:${String(r.item)} in this lane"${busy ? html` disabled` : ''}>Cancel…</button></div>`;
  }
  function again(lane, r) {
    if (!DOC.retry) return html`<span></span>`;
    const one = (act, label, aria) => html`<button class="btn quiet sm" type="button" data-act="${act}" data-item="${String(r.item)}" data-head="${r.expected_head}" data-repo="${lane.path}" aria-label="${aria}"${busy ? html` disabled` : ''}>${label}</button>`;
    return html`<div class="ctl" role="group" aria-label="Unblock sd:${String(r.item)}">${one('retry', 'Retry', `Retry sd:${r.item}: queue it again at the same head`)}${lane.runner_merge === 'manual' ? one('approve', 'Approve…', `Approve sd:${r.item}: queue it again with the merge granted`) : ''}</div>`;
  }
  function back(lane, r) {
    if (!DOC.requeue) return html`<span></span>`;
    return html`<div class="ctl" role="group" aria-label="Cancel of sd:${String(r.item)}"><button class="btn quiet sm" type="button" data-act="requeue" data-item="${String(r.item)}" data-head="${r.expected_head}" data-repo="${lane.path}" aria-label="Re-queue sd:${String(r.item)} at head ${r.head}"${busy ? html` disabled` : ''}>Re-queue</button></div>`;
  }
  function free(lane, r) {
    if (!r.claim) return html`<span></span>`;
    return html`<div class="ctl" role="group" aria-label="Claim of sd:${String(r.item)}"><button class="btn quiet sm" type="button" data-act="reclaim" data-item="${String(r.item)}" data-claim="${r.claim}" data-repo="${lane.path}" aria-label="Release sd:${String(r.item)} from ${r.host || 'its holder'}"${busy ? html` disabled` : ''}>Release…</button></div>`;
  }
  // Each state's glyph, label and fact line. A state's glyph carries it; colour only repeats it. A cancelled row borrows
  // queued's glyph (the foundation has five) and names itself to a screen reader as cancelled.
  const VIEW = {
    merging: r => [r.lease_passed ? G.caution : G.live, 'MERGING', html`<b>${r.step || 'step not recorded'}</b> on <b>${r.host || 'host not recorded'}</b> · claimed ${r.claim_age == null ? 'at a time not recorded' : `${dur(r.claim_age)} ago`} · ${r.lease_until ? `lease ${r.lease_passed ? 'passed' : 'to'} ${at(r.lease_until)}` : 'lease not recorded'} · head ${r.head || 'not recorded'}`],
    next: r => [r.held ? G.caution : G.queued, `${r.held ? 'HELD' : 'NEXT'} ${r.position}`, html`gate <b>${r.gate}</b>${r.gate_summary ? ` (${r.gate_summary})` : ''} · head ${r.head || 'not recorded'}${r.held ? ' · skipped until released' : ''}`],
    building: r => [{ running: G.live, pass: G.ok, fail: G.warning }[r.gate] || G.unknown, 'BUILDING', html`gate <b>${r.gate}</b>${r.summary ? ` · ${r.summary}` : ''}${r.head ? ` · head ${r.head}` : ''} · log changed ${at(new Date(r.changed * 1000).toISOString())}`],
    blocked: r => [G.caution, 'BLOCKED', html`<b>${r.who} acts</b> · ${r.status}${r.step ? ` at ${r.step}` : ''} · ${r.reason} · head ${r.head || 'not recorded'}`],
    cancelled: r => [[...G.queued, 'cancelled'], 'CANCELLED', html`cancelled at ${at(r.finished)} · head ${r.head || 'not recorded'}${DOC.requeue ? '' : ` · Re-queue off: ${DOC.requeue_off || 'not known whether the pack takes it'}`}`],
    landed: r => [G.ok, 'LANDED', html`${r.pr ? `PR #${r.pr}` : 'PR not named'} · merge ${r.commit || 'not recorded'} · ${at(r.finished)}`],
  };
  // Each entry row is a command object (sd:5064): its menu offers Adversarial review of the worktree the lane recorded.
  // A building row is a builder's gate log, not an entry, so it has none.
  const entryId = (lane, r) => `entry:${lane.path}:${r.item}`;
  function putEntries() {
    const C = window.shell.commands;
    DOC.lanes.forEach(l => l.rows.filter(r => r.state !== 'building').forEach(r => C.put({ id: entryId(l, r), type: 'entry', label: `sd:${r.item}`, item: r.item, worktree: r.worktree, repo: l.repo })));
  }
  function row(lane, r, last) {
    const [g, st, facts] = VIEW[r.state](r);
    const subject = r.state === 'building' ? html`<code>${r.builder}</code>` : html`<code>sd:${String(r.item)}</code>${r.title}`;
    const ctl = r.state === 'next' ? controls(lane, r, last) : r.state === 'blocked' ? again(lane, r) : r.state === 'cancelled' ? back(lane, r) : r.state === 'merging' ? free(lane, r) : html``;
    const id = r.state === 'building' ? null : entryId(lane, r);
    return html`<li class="qrow" data-state="${r.state}"${r.held ? html` data-held=""` : ''}><span class="g g-${g[0]}" aria-hidden="true">${g[1]}</span><span class="st">${st}</span><span class="s"><span><span class="sr">${g[2] || g[0]} · </span>${subject}</span><small>${facts}</small></span><span class="ends">${ctl}${id ? window.shell.commands.rowActions(id) : ''}</span></li>`;
  }
  function lane(l) {
    const last = l.rows.filter(r => r.state === 'next').length, s = said[l.path], rows = drawn(l);
    const count = st => l.rows.filter(r => r.state === st).length;
    return html`<section class="lane" aria-labelledby="lane-${l.repo}"><header><h2 id="lane-${l.repo}">${l.repo}</h2><span class="host">lane on ${l.host}</span><span class="n">${count('merging')} merging · ${last} next · ${count('blocked')} blocked · ${count('landed')} landed today</span><span class="src">${l.lane}</span></header>
      ${s ? (s.ok ? html`<p class="done" role="status">${s.text}</p>` : html`<p class="fail" role="alert">${s.text}</p>`) : ''}
      ${rows.length ? html`<ul class="qrows">${rows.map(r => row(l, r, last))}</ul>` : html`<p class="done">${l.rows.length ? 'Nothing active in this lane; All shows its ended rows.' : 'Nothing queued, gating or landed today in this lane.'}</p>`}</section>`;
  }
  function draw() {
    if (!DOC) { put($('readings'), html``); put($('show'), html``); put($('lanes'), html``); return; }
    putEntries();
    const L = DOC.load, gates = DOC.gates;
    put($('readings'), html`<div><dt>load5</dt><dd>${L.load5.toFixed(1)}<small>1m ${L.load1.toFixed(1)} · 15m ${L.load15.toFixed(1)} · ${TREND[L.trend]}</small></dd></div>
      <div><dt>gates running / cap</dt><dd>${gates.error ? html`▨ <small>${gates.error}</small>` : html`${gates.running} / ${gates.cap}<small>${gates.waiting} waiting</small>`}</dd></div>
      <div><dt>read</dt><dd>${at(DOC.read)}<small>${DOC.read.replace('T', ' ').replace('Z', ' UTC')}</small></dd></div>`);
    $('edits').textContent = DOC.edits;
    const every = DOC.lanes.flatMap(l => l.rows), chip = (v, n) => html`<button type="button" class="chip" data-show="${v}" aria-pressed="${String(show === v)}">${v} <span class="n">${String(n)}</span></button>`;
    put($('show'), html`<span class="label">Show</span><div class="chips">${chip('active', every.filter(r => ACTIVE.includes(r.state)).length)}${chip('all', every.length)}</div>`);
    // A lane with nothing to show (and no write result) folds into one line: the calm screen stays calm.
    const busyLanes = DOC.lanes.filter(l => drawn(l).length || said[l.path]), idle = DOC.lanes.filter(l => !busyLanes.includes(l));
    put($('lanes'), html`${DOC.problems.map(p => html`<p class="fail" role="alert">▨ ${p.repo}: lane not read. ${p.error}</p>`)}${busyLanes.map(lane)}${idle.length
      ? html`<p class="idle">Idle: ${idle.map((l, i) => html`${i ? ' · ' : ''}<code>${l.repo}</code> (lane on ${l.host})`)}</p>`
      : DOC.lanes.length ? '' : html`<p class="done">No managed repository's lane was read. sd-db repo managed PATH yes adds one.</p>`}`);
  }

  async function load() {
    const S = window.shell;
    if (!DOC) S.state({ kind: 'loading', text: 'Reading the lanes. Rows appear when /api/queue answers.', source: '/api/queue' });
    try {
      DOC = await S.getJSON('/api/queue');
      S.state(DOC.problems.length ? { kind: 'partial', text: `${plural(DOC.problems.length, 'lane')} not read; the others are current.`, source: '/api/queue' } : null);
      put($('subhead'), html`${plural(DOC.lanes.length, 'lane')} of ${plural(DOC.managed, 'managed repository', 'managed repositories')} · read ${at(DOC.read)}`);
      // The badge is the read's own (sd:3446, queue_screen._attention): blocked rows in every lane.
      S.attention(DOC.attention || { state: 'unknown', n: 0, what: 'lanes not read' });
    } catch (err) {
      DOC = null;
      S.state({ kind: 'error', text: `The lanes were not read: ${err.message}. Read again retries it.`, source: '/api/queue' });
      put($('subhead'), html`The lanes could not be read.`);
    }
    draw();
  }

  const DONE = { up: 'moved up', down: 'moved down', top: 'moved to the top', hold: 'held', release: 'released', cancel: 'cancelled',
    retry: 'queued again', approve: 'approved and queued again', requeue: 're-queued', reclaim: 'released from its holder' };
  // Cancel and Approve are the writes no verb undoes; each dialog names the entry and the command it runs.
  function grant(l, item, head) {
    const r = l.rows.find(x => x.state === 'blocked' && x.item === item), S = window.shell;
    return S.confirm({ title: `Approve sd:${item}${r && r.title ? ` · ${r.title}` : ''} in ${l.repo}?`,
      body: `The entry is queued again at head ${head.slice(0, 12)}, and the runner may merge it to the default branch. A merge is not taken back.`,
      cli: `sd-ship -C ${S.shq(l.path)} lane retry ${item} --expected-head ${head} --manual`, ok: 'Approve merge', keep: 'Leave it blocked' });
  }
  function sure(l, item) {
    const r = l.rows.find(x => x.state === 'next' && x.item === item), S = window.shell;
    return S.confirm({ title: `Cancel sd:${item}${r && r.title ? ` · ${r.title}` : ''} in ${l.repo}?`,
      body: `The entry leaves the queue and the runner never ships it. ${DOC.requeue
        ? 'Re-queue on its cancelled row puts it back, last, while its branch stays at this head.'
        : 'To ship it later, enqueue it again from its worktree.'}`,
      cli: `sd-ship -C ${S.shq(l.path)} lane cancel ${item}`, ok: 'Cancel entry', keep: 'Keep it queued' });
  }
  // Release names what the pack's reclaim does at the step the row showed; the claim binds it to that step.
  function letGo(l, item, claim) {
    const r = l.rows.find(x => x.state === 'merging' && x.item === item && x.claim === claim) || {}, S = window.shell;
    const host = r.host || 'a host not recorded', age = r.claim_age == null ? 'at a time not recorded' : `${dur(r.claim_age)} ago`;
    const outcome = r.step === 'prepare'
      ? 'The entry goes back to pending at its place and runs again; its third reclaim fails it instead.'
      : 'The entry fails with the release as its reason. A merge it already started may still land, so read its pull request before you enqueue it again.';
    return S.confirm({ title: `Release sd:${item}${r.title ? ` · ${r.title}` : ''} from ${host}?`,
      body: `${host} claimed it ${age}, at ${r.step || 'a step not recorded'}. ${outcome} The holder's next queue write is refused. If the entry moved on since this read, nothing is released. sd-ship refuses the release when the holder runs alive on this machine.`,
      cli: `sd-ship -C ${S.shq(l.path)} lane cancel ${item} --expected-claim ${S.shq(claim)}`, ok: 'Release entry', keep: 'Keep it running' });
  }
  async function act(b) {
    const l = DOC && DOC.lanes.find(x => x.path === b.dataset.repo);
    if (!l || busy) return;
    const item = Number(b.dataset.item), action = b.dataset.act, head = b.dataset.head || '', claim = b.dataset.claim || '';
    if (action === 'cancel' && !(await sure(l, item))) return;
    if (action === 'reclaim' && !(await letGo(l, item, claim))) return;
    if (action === 'approve' && !(await grant(l, item, head))) return;
    busy = true; draw();
    try {
      const body = { repo: l.path, item, action, revision: l.revision };
      if (action === 'retry' || action === 'approve' || action === 'requeue') body.head = head;
      if (action === 'reclaim') body.claim = claim;
      const out = await window.shell.post('/api/queue/move', body);
      const order = out.pending ? ` Order now: ${out.pending.map(n => `sd:${n}`).join(', ')}.` : '';
      said[l.path] = { ok: true, text: `sd:${item} ${DONE[action]}.${order} ${out.edits || ''}` };
    } catch (err) {
      // Only a 4xx wrote nothing (queue_screen.move); a 502, another status or no answer may follow a landed write (sd:3339).
      said[l.path] = { ok: false, text: err.status >= 400 && err.status < 500
        ? `sd:${item} not ${DONE[action]}: ${err.message}${err.stale ? '' : ' The queue is unchanged.'}`
        : `Not known whether sd:${item} was ${DONE[action]}: ${err.message} The lane below was read again; check sd:${item} there before you act again.` };
    }
    busy = false;
    await load();
  }
  window.queueAct = act; // for the page test

  // The last entry a review started for shows its run in Details; commands.js reads the run every 10 s until it settles.
  document.addEventListener('adversarial:run', e => {
    if (e.detail.key.startsWith('change:')) put($('adv'), html`${window.commands.reviewPane('change', e.detail.key.slice('change:'.length))}`);
  });

  window.PAGE_ATTENTION = { state: 'ok', n: 0, what: 'blocked' };
  document.addEventListener('DOMContentLoaded', () => {
    window.shell.commands.register(window.commands.adversarialReview('entry', {
      payload: o => ({ kind: 'change', target: o.worktree, item: o.item }),
      when: o => !!o.worktree || 'the lane recorded no worktree for this entry' }));
    $('reload').addEventListener('click', load);
    show = new URLSearchParams(location.search).get('show') === 'all' ? 'all' : 'active';
    $('show').addEventListener('click', e => {
      const b = e.target.closest('button[data-show]'); if (!b || b.dataset.show === show) return;
      show = b.dataset.show; window.shell.url(show === 'all' ? { show } : {}); draw();
    });
    $('lanes').addEventListener('click', e => { const b = e.target.closest('button[data-act]'); if (b && !b.disabled) act(b); });
    if (typeof setInterval === 'function') setInterval(() => { if (!document.hidden && !busy) load(); }, 30000);
    load();
  });
})();
