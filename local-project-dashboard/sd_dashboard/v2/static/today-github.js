// GitHub's Pull requests view (sd:5608; Today's GitHub view until sd:5781): the operator's open pull requests, and those
// merged or closed in the last 7 days, from /api/today/github (today_github.document). The reading calls no GitHub API: the
// rows are what sd shadow sync and the Contributions collector stored; only Copilot review, pressed, asks GitHub. It loads
// before shell.js, and reads when the view first shows and on Refresh. sd:5899: one table, newest activity first, paged
// by the shell's pager; its issues list on Your issues (issues.with_github). A row's item, when one is known, links to
// Tasks. A row with no activity stamp says not recorded, never a time; a source that was not read is a line naming it; a
// read that failed says why.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
// sd:5633: a pull request row's title, or a press on its row, opens its Details in the right pane: what the read carries,
// a link to GitHub in a new tab, and its commands (Open on GitHub, Open item, Adversarial review, Copilot review, Merge,
// Close original), each a command object of TYPE the shell runs. An issue row keeps its title's link to GitHub. The
// selection is the page's one, pick below, named in ?row= as on Today (sd:5654).
// sd:5668: Merge copies an open pull request into a worktree and enqueues it in the lane (pr_merge.py). Its run reads the
// preview, asks with it (the pull request, its head, its author, the worktree and the item), then posts the preview's
// revision; a 4xx moved nothing, and a 502 may follow a step that moved, so the toast says Merge again resumes.
// sd:5669: Close original is off until the lane merged the copy (the row's landed, pr_close.landed). Its run reads the
// preview, asks with the exact comment, then posts the preview's revision: one comment linking the lane's pull request and
// its merge commit, then the close. A 502 may follow the comment, so the toast says Close original again resumes.
// sd:5757, sd:5899: Show mine, the default, keeps the rows assigned to you or opened by you (the read's `mine`); Show all
// adds what others opened in a registered repo, a review asked of you among them. ?gh=all keeps All across a reload.
// State open, the default, lists open pull requests; with closed adds those merged or closed in the window (?prs=closed).
(() => {
  const { html, put, plural } = window.markup;
  const count = (shown, total, noun) => window.shell.list.count(shown, total, noun);
  const $ = id => document.getElementById(id);
  const GLYPH = { caution: '▲', queued: '◌', ok: '●' };
  // What each unread source leaves out, for its line.
  const UNREAD = { intake: 'Third-party issues in repos with GitHub issues on', delivery: 'Items of merged pull requests (delivery notes)',
    refs: 'Items filed under a GitHub ref' };

  let DOC = null, failed = '', reads = 0, started = false, PUT = new Set();
  // The pager's state (shell.list): page and size, from the address when the view first draws.
  const SIZE = 25;
  let ST = null;
  const st = () => ST || (ST = window.shell.list.listParams(new URLSearchParams(location.search), { page: 1 }, { size: SIZE }));
  // The command objects' type, apart from Now's 'pull request' rows: Snooze and Now's commands do not apply here.
  const TYPE = 'github pull request';
  const oid = key => `gh:${key}`;
  const pulls = () => DOC && !DOC.error ? [...DOC.pulls, ...DOC.settled.filter(r => r.kind === 'pull')] : [];
  const pullOf = key => pulls().find(r => r.key === key);
  // The selected pull request's key: the page's one selection (pick) names it in ?row= as its object id.
  const param = k => new URLSearchParams(location.search).get(k);
  const all = () => param('gh') === 'all', closed = () => param('prs') === 'closed';
  const mine = r => !!r.mine;
  // The table's rows: open pull requests, and with closed those settled in the window, before Show.
  const listed = () => [...DOC.pulls, ...(closed() ? DOC.settled.filter(r => r.kind === 'pull') : [])];
  const shown = () => listed().filter(r => all() || mine(r));
  // A filter or the pager writes its params around the others (shell.url keeps ?row=).
  function address(changes) {
    const p = new URLSearchParams(location.search);
    Object.entries(changes).forEach(([k, v]) => v ? p.set(k, v) : p.delete(k));
    p.delete('page'); p.delete('size'); window.shell.list.listQuery(p, st(), { size: SIZE });
    window.shell.url(p.toString());
  }
  const chosen = () => { const id = new URLSearchParams(location.search).get('row') || ''; return id.startsWith('gh:') ? id.slice(3) : null; };

  const state = r => r.state !== 'open' ? 'ok' : r.why === 'waiting on you' ? 'caution' : 'queued';
  const when = r => r.at ? html`${r.at_what} <time class="rel" datetime="${r.at}"></time>` : html`<span class="ph">activity not recorded</span>`;
  const row = r => {
    const s = state(r);
    return html`<tr data-key="${r.key}" data-id="${oid(r.key)}" data-pr aria-selected="${String(chosen() === r.key)}">
      <td class="g g-${s}" title="${r.state}">${GLYPH[s]}</td>
      <td class="what"><button type="button" data-gh-open="${r.key}">${r.title || r.ref}</button><span class="repo">${r.ref}</span></td>
      <td class="detail">${r.state === 'open' ? r.why : html`${r.state} · ${r.why}`}${r.item ? html`<span class="sub"><a href="${window.shell.itemHref(r.item)}">sd:${r.item}</a></span>` : ''}</td>
      <td class="when">${when(r)}</td></tr>`;
  };
  // With closed, the window and the settled rows it could not place.
  const note = () => closed() ? html`<p class="st-line">Merged or closed: the last ${DOC.days} days${DOC.undated ? html` · <span class="g-unknown">▨</span> ${plural(DOC.undated, 'merged or closed row')} with no date recorded, not placed` : ''}</p>` : '';
  function table() {
    const L = window.shell.list, rows = shown();
    if (!rows.length) return html`<p class="st-line st-empty">No ${closed() ? '' : 'open '}pull request ${all() ? 'is listed' : 'is assigned to you or opened by you'}.</p>`;
    return html`<table class="ledger st" aria-labelledby="github-h">
      <thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Pull request</th><th scope="col">Why</th><th scope="col">Activity</th></tr></thead>
      <tbody>${L.pageOf(rows, st()).map(row)}</tbody></table>${L.pager(rows.length, st(), 'pull requests')}`;
  }

  // The counts, what waits on you, and when the GitHub collector last succeeded: a sync that is not fresh says so.
  function tally() {
    const waiting = DOC.pulls.filter(r => r.why === 'waiting on you').length, c = DOC.collector || {}, fresh = c.state === 'fresh';
    const sync = c.last_success_at
      ? html`<span${fresh ? '' : html` class="g-caution"`}>${fresh ? '' : `▲ sync ${c.state} · `}synced <time class="rel" datetime="${c.last_success_at}"></time></span>`
      : html`<span class="g-unknown">▨ sync ${c.state || 'unknown'}${c.reason ? `: ${c.reason}` : ''}</span>`;
    return html`${waiting ? html`<span class="g-caution">▲ ${waiting} waiting on you</span>` : ''}
      <span>${count(shown().length, listed().length, closed() ? 'pull request' : 'open pull request')}</span>${sync}`;
  }

  function draw() {
    if (!DOC || DOC.error) {
      const why = DOC ? `GitHub could not be read: ${DOC.error}. Refresh tries again.` : started ? `GitHub could not be read: ${failed}. Refresh tries again.` : 'Reading GitHub…';
      put($('github-tally'), html``); put($('github-filters'), html``);
      put($('github-sections'), html`<p class="st-line">${why}</p>`);
      return;
    }
    put($('github-tally'), tally());
    const L = window.shell.list, n = f => String(listed().filter(f).length);
    const open = DOC.pulls.filter(r => all() || mine(r)).length, every = open + DOC.settled.filter(r => r.kind === 'pull' && (all() || mine(r))).length;
    put($('github-filters'), L.fbar(html`${L.fseg('gh', 'Show', [['mine', mine], ['all', () => true]].map(([v, f]) =>
      ({ v, pressed: all() === (v === 'all'), label: html`${v} <span class="n">${n(f)}</span>` })))}${L.fseg('prs', 'State', [['open', open], ['closed', every]].map(([v, c]) =>
      ({ v, pressed: closed() === (v === 'closed'), label: html`${v === 'open' ? 'open' : 'with closed'} <span class="n">${String(c)}</span>` })))}`));
    const unread = Object.entries(DOC.unread || {});
    put($('github-sections'), html`${unread.map(([k, why]) => html`<p class="st-line"><span class="g-unknown">▨</span> ${UNREAD[k] || k} not read: ${why}</p>`)}${note()}${table()}`);
  }

  // ---------- Details (sd:5633) ----------
  // One selection (today.js pick, sd:5654, until this view left Today): the row is named in ?row=, marked alone
  // aria-selected, its Details drawn, and it is the shell's selection.
  function pick(id, draw, open) {
    $('github-sections').querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id));
    put($('details'), draw());
    window.shell.commands.select(id);
    const u = new URL(location.href); u.searchParams.set('row', id); history.replaceState(null, '', u);
    $('details').setAttribute('data-swap', ''); requestAnimationFrame(() => $('details').removeAttribute('data-swap'));
    if (open) window.shell.openPane('tab-details');
  }
  // A time the read carries, or not recorded: a missing stamp is never a time.
  const stamp = v => v ? html`<time class="rel" data-long datetime="${v}"></time>` : html`<span class="ph">not recorded</span>`;
  function details(key, open = true) {
    const r = pullOf(key); if (!r) return;
    const s = state(r), settled = r.merged_at ? ['Merged', r.merged_at] : r.closed_at ? ['Closed', r.closed_at] : null;
    pick(oid(key), () => html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> Pull request · ${r.state}</p>
      <h2>${r.title || r.ref}</h2>
      <dl><dt>Repo</dt><dd>${r.repo}</dd><dt>Number</dt><dd>#${r.number}</dd><dt>State</dt><dd>${r.state} · ${r.why}</dd>
        <dt>Author</dt><dd>${r.author || html`<span class="ph">not recorded</span>`}</dd>
        <dt>Branch</dt><dd>${r.branch || html`<span class="ph">not recorded</span>`}</dd>
        ${r.assignee ? html`<dt>Assignee</dt><dd>${r.assignee}</dd>` : ''}
        <dt>Opened</dt><dd>${stamp(r.created_at)}</dd><dt>Updated</dt><dd>${stamp(r.updated_at)}</dd>
        ${settled ? html`<dt>${settled[0]}</dt><dd>${stamp(settled[1])}</dd>` : ''}
        ${(r.attention || []).length ? html`<dt>Waiting on</dt><dd>${r.attention.join(' · ')}</dd>` : ''}
        <dt>Item</dt><dd>${r.item ? html`<a href="${window.shell.itemHref(r.item)}">sd:${r.item}</a>` : html`<span class="ph">none known</span>`}</dd></dl>
      ${r.url ? html`<p><a href="${r.url}" target="_blank" rel="noopener noreferrer">Open ${r.ref} on GitHub</a></p>` : ''}
      <h3>Act</h3>${window.shell.commands.bar(oid(key))}
      ${r.checkout ? window.commands.reviewPane('change', r.checkout) : ''}`, open);
  }

  // Each read puts one command object per pull request row; a row the read no longer lists is retired, as read.js does,
  // so no command runs on it. A failed read retires them all.
  function objects() {
    const C = window.shell.commands, keep = new Set();
    pulls().forEach(r => { keep.add(oid(r.key)); C.put({ id: oid(r.key), type: TYPE, label: `${r.ref} ${r.title}`.trim(), repo: r.repo,
      number: r.number, url: r.url, state: r.state, item: r.item, checkout: r.checkout, landed: r.landed || null }); });
    PUT.forEach(id => { if (!keep.has(id)) C.put({ id, type: 'not listed', label: `${C.get(id)?.label || id} (no longer listed)` }); });
    PUT = keep;
  }

  function register() {
    const K = window.commands, C = window.shell.commands;
    const closed = o => `the pull request is ${o.state}`;
    C.register(
      // The shared Open on GitHub (commands.js), as Now and Activity declare it on their pull request rows.
      K.openPullRequest({ on: TYPE, pr: o => ({ repo: o.repo, number: o.number, url: o.url }) }),
      // Activity's pr.item, on this view's rows.
      { id: `${TYPE}.item`, on: TYPE, label: 'Open item', does: 'Opens the work item this pull request belongs to.', key: 'i', risk: 'safe',
        when: o => !!o.item || 'no work item names this pull request', cli: o => `sd task show ${o.item}`, run: o => window.shell.openItem(o.item) },
      // sd:5100's pull request review (adversarial.py `_resolve_pull`): a change review at the head origin names, in the
      // registered checkout of the pull request's repository. The server checks the number against sd shadow sync's open rows.
      K.adversarialReview(TYPE, { does: 'Starts a codex review of this pull request at its head in the background; its brief lists tasks to pick.',
        payload: o => ({ kind: 'change', target: o.checkout, pr: o.number, ...(o.item ? { item: o.item } : {}) }),
        when: o => o.state !== 'open' ? `${closed(o)}; a review reads an open one`
          : o.checkout ? true : `${o.repo} is not a registered repository, so no checkout holds it to review` }),
      // Asks GitHub once per press, through the reviewer endpoint sd-ship's --copilot-review request calls (today_github.py).
      { id: `${TYPE}.copilot-review`, on: TYPE, label: 'Copilot review', does: 'Asks GitHub for one Copilot review of this pull request.', icon: 'bot',
        risk: 'confirm', executes: true,
        when: o => o.state !== 'open' ? `${closed(o)}; Copilot reviews an open one` : !!(o.repo && o.number) || 'the row names no pull request',
        consequence: o => `This asks GitHub for a Copilot review of ${o.repo}#${o.number}, once. Copilot posts its review on the pull request; nothing here takes the request back.`,
        cli: o => `gh api repos/${o.repo}/pulls/${o.number}/requested_reviewers --method POST -f 'reviewers[]=copilot-pull-request-reviewer[bot]'`,
        sends: o => `POST /api/today/github/copilot-review ${JSON.stringify({ repo: o.repo, number: o.number })}`,
        run: o => window.shell.post('/api/today/github/copilot-review', { repo: o.repo, number: o.number }).then(
          () => `Copilot review requested · ${o.repo}#${o.number}`,
          err => { throw err.status ? err : new Error(`no answer (${err.message}), so whether Copilot was asked is not known`); }) },
      // sd:5668: previews: run() reads the preview and asks with it, so the shell's own confirm does not ask first.
      { id: `${TYPE}.merge`, on: TYPE, label: 'Merge', does: 'Copies this pull request into a worktree and queues it in the lane.',
        icon: 'git-merge', risk: 'confirm', executes: true, previews: true,
        when: o => o.state !== 'open' ? `${closed(o)}; Merge copies an open one`
          : o.checkout ? true : `${o.repo} is not a registered repository, so the lane cannot take it`,
        cli: o => `git -C ${window.shell.shq(o.checkout)} fetch origin pull/${o.number}/head && sd-ship -C <worktree> lane enqueue --item <n> --title <title> --body-file <body> --expected-head <head> --deliver`,
        sends: o => `POST /api/today/github/merge ${JSON.stringify({ repo: o.repo, number: o.number, revision: '<preview>' })}`,
        run: mergePull },
      // sd:5669: previews, as Merge does; nothing closes without this press and its confirm.
      { id: `${TYPE}.close-original`, on: TYPE, label: 'Close original', does: 'Comments with a link to the lane\'s merged copy, then closes this pull request.',
        icon: 'git-pull-request', risk: 'confirm', executes: true, previews: true,
        when: o => o.state !== 'open' ? `${closed(o)}; nothing to close`
          : o.landed ? true : 'the lane has not merged a copy of it yet: Merge copies it, and the lane merges the copy',
        cli: o => `gh api repos/${o.repo}/issues/${o.number}/comments --method POST -f body=<comment> && gh api repos/${o.repo}/pulls/${o.number} --method PATCH -f state=closed`,
        sends: o => `POST /api/today/github/close ${JSON.stringify({ repo: o.repo, number: o.number, revision: '<preview>' })}`,
        run: closePull },
    );
  }

  // What Merge will do, from its preview: the confirm names every fact the copy moves on.
  const mergeText = p => [
    `${p.repo}#${p.number} "${p.title}" by ${p.author || 'an author not recorded'}, head ${p.head.slice(0, 12)}.`,
    p.mode === 'resume' ? `Resumes: ${p.branch} already holds this head${p.worktree_listed ? ` in ${p.worktree}` : `; Merge adds its worktree ${p.worktree}`}.`
      : `Fetches pull/${p.number}/head into ${p.branch} and a worktree at ${p.worktree}; the commits keep their authors.`,
    p.item ? `Item: sd:${p.item} ${p.item_title} (${p.ref}).` : `Item: files a new task, ${p.item_title}, under ${p.ref}.`,
    'Then sd-ship lane enqueue --deliver: the lane publishes its own pull request, gates, reviews and merges it. The original stays open; close it once the lane merges.',
  ].join(' ');
  async function mergePull(o) {
    const ref = `${o.repo}#${o.number}`;
    const p = await window.shell.getJSON(`/api/today/github/merge/preview?repo=${encodeURIComponent(o.repo)}&number=${o.number}`);
    const { yes } = await window.shell.ask({ title: `Merge ${ref}?`, body: mergeText(p), ok: 'Merge', keep: "Don't merge", danger: true, cli: () => p.cli });
    if (!yes) return { text: `Merge not run · ${ref}` };
    let out;
    try { out = await window.shell.post('/api/today/github/merge', { repo: o.repo, number: o.number, revision: p.revision }); }
    catch (err) {
      if (err.status >= 400 && err.status < 500) throw err;
      load();
      return { text: `Not known how far Merge got · ${ref}: ${err.message}. Merge again resumes what was copied.` };
    }
    // The next read lists the row's item: the one filed under its ref.
    load();
    const lane = out.lane || {};
    const queued = lane.status === 'not queued' ? `not queued: ${lane.error}. Merge again to retry the enqueue` : `lane ${lane.status}; Queue shows it`;
    return { text: `Copied · ${ref} at ${out.head.slice(0, 12)} into ${out.branch} · sd:${out.item}${out.filed ? ' filed' : ''} · ${queued}` };
  }

  async function closePull(o) {
    const ref = `${o.repo}#${o.number}`;
    const p = await window.shell.getJSON(`/api/today/github/close/preview?repo=${encodeURIComponent(o.repo)}&number=${o.number}`);
    const body = `Posts this comment on ${ref}, then closes it, not merged: ${p.text}`;
    const { yes } = await window.shell.ask({ title: `Close ${ref}?`, body, ok: 'Close', keep: "Don't close", danger: true, cli: () => p.cli });
    if (!yes) return { text: `Close original not run · ${ref}` };
    let out;
    try { out = await window.shell.post('/api/today/github/close', { repo: o.repo, number: o.number, revision: p.revision }); }
    catch (err) {
      if (err.status >= 400 && err.status < 500) throw err;
      return { text: `Not known how far Close original got · ${ref}: ${err.message}. Close original again posts no second comment.` };
    }
    return { text: `Closed · ${ref}${out.commented ? ' with a comment' : '; its comment was already posted'} linking ${p.merged} · the row moves once sd shadow sync reads it` };
  }

  // Of overlapping reads only the newest draws.
  async function load() {
    const n = ++reads; started = true;
    try {
      const doc = await window.shell.getJSON('/api/today/github');
      if (n !== reads) return;
      DOC = doc;
    } catch (e) {
      if (n !== reads) return;
      DOC = null; failed = e.message;
    }
    objects();
    draw();
    // The selected row's Details show this read's facts; a page opened with ?row=gh:… shows its row once read.
    if (chosen() && pullOf(chosen())) details(chosen(), false);
  }

  document.addEventListener('shell:view', e => { if (e.detail && e.detail.id === 'pulls' && !started) load(); });
  // Read again (github.js) reads this view again once it has been read.
  window.githubPulls = { again: () => started ? load() : undefined };
  document.addEventListener('DOMContentLoaded', () => {
    register();
    // A run of this view's Adversarial review draws the selected row's Details again (commands.js follows the run).
    document.addEventListener('adversarial:run', e => { const key = chosen(), r = key && pullOf(key);
      if (r && r.checkout && e.detail?.key === `change:${r.checkout}`) details(key, false); });
    document.addEventListener('shell:open', e => { const id = String(e.detail || ''); if (id.startsWith('gh:') && pullOf(id.slice(3))) details(id.slice(3)); });
    $('github-filters').addEventListener('click', e => {
      const b = e.target.closest('[data-f="gh"], [data-f="prs"]'); if (!b) return;
      st().page = 1;
      address(b.dataset.f === 'gh' ? { gh: b.dataset.v === 'all' ? 'all' : '' } : { prs: b.dataset.v === 'closed' ? 'closed' : '' });
      draw();
      $('github-filters').querySelector(`[data-f="${b.dataset.f}"][data-v="${b.dataset.v}"]`)?.focus();
    });
    $('github-sections').addEventListener('click', e => {
      const opened = e.target.closest('[data-gh-open]'); if (opened) return details(opened.dataset.ghOpen);
      // The pager (shell.list): a page or a size redraws, and focus stays on the new copy of the button.
      if (window.shell.list.paging(e, st())) { address({}); return window.shell.list.keepFocus(e, draw); }
      const tr = !e.target.closest('a, button') && e.target.closest('tr[data-pr]'); if (tr) details(tr.dataset.key);
    });
    draw();
  });
})();
