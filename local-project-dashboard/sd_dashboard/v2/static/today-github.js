// Today's GitHub view (sd:5608): the operator's open pull requests and issues, and those merged or closed in the last 7
// days, from /api/today/github (today_github.document). The reading calls no GitHub API: the rows are what sd shadow sync
// and the Contributions collector stored; only Copilot review, pressed, asks GitHub. It loads before shell.js, and reads
// when the view first shows and on Refresh. Each part shows its first CAP rows, newest activity first, and Show all opens
// the rest in place; the parts with nothing in them share one line. An issue row's title links to GitHub, and a row's
// item, when one is known, to Tasks. A row with no activity stamp says not recorded, never a time; a source that was not
// read is a line naming it; a read that failed says why.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
// sd:5633: a pull request row's title, or a press on its row, opens its Details in the right pane: what the read carries,
// a link to GitHub in a new tab, and its commands (Open on GitHub, Open item, Adversarial review, Copilot review), each a
// command object of TYPE the shell runs. An issue row keeps its title's link to GitHub.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);
  // Status's cap (today-status.js): the rows a part shows before Show all.
  const CAP = 5;
  const HEAD = { pulls: 'Open pull requests', issues: 'Open issues', settled: 'Merged or closed' };
  const ORDER = Object.keys(HEAD);
  const GLYPH = { caution: '▲', queued: '◌', ok: '●' };
  const KIND = { pull: 'PR', issue: 'Issue' };
  // What each unread source leaves out, for its line.
  const UNREAD = { intake: 'Third-party issues in repos with GitHub issues on', delivery: 'Items of merged pull requests (delivery notes)',
    refs: 'Items filed under a GitHub ref' };

  let DOC = null, failed = '', reads = 0, started = false, SELECTED = null, PUT = new Set();
  const OPEN = new Set();
  // The command objects' type, apart from Now's 'pull request' rows: Snooze and Now's commands do not apply here.
  const TYPE = 'github pull request';
  const oid = key => `gh:${key}`;
  const pulls = () => DOC && !DOC.error ? [...DOC.pulls, ...DOC.settled.filter(r => r.kind === 'pull')] : [];
  const pullOf = key => pulls().find(r => r.key === key);

  const state = r => r.state !== 'open' ? 'ok' : r.why === 'waiting on you' ? 'caution' : 'queued';
  const when = r => r.at ? html`${r.at_what} <time class="rel" datetime="${r.at}"></time>` : html`<span class="ph">activity not recorded</span>`;
  const row = r => {
    const s = state(r);
    const pr = r.kind === 'pull';
    return html`<tr data-key="${r.key}"${pr ? html` data-pr aria-selected="${String(SELECTED === r.key)}"` : ''}>
      <td class="g g-${s}" title="${r.state}">${GLYPH[s]}</td>
      <td class="what">${pr ? html`<button type="button" data-gh-open="${r.key}">${r.title || r.ref}</button>`
        : r.url ? html`<a href="${r.url}" rel="noopener noreferrer">${r.title || r.ref}</a>` : r.title || r.ref}<span class="repo">${KIND[r.kind]} · ${r.ref}</span></td>
      <td class="detail">${r.state === 'open' ? r.why : html`${r.state} · ${r.why}`}${r.item ? html`<span class="sub"><a href="${window.shell.itemHref(r.item)}">sd:${r.item}</a></span>` : ''}</td>
      <td class="when">${when(r)}</td></tr>`;
  };

  // The line under a part's heading: the window, and the settled rows it could not place.
  function note(key) {
    if (key !== 'settled') return '';
    return html`<p class="st-line">The last ${DOC.days} days${DOC.undated ? html` · <span class="g-unknown">▨</span> ${plural(DOC.undated, 'merged or closed row')} with no date recorded, not placed` : ''}</p>`;
  }
  const shows = key => DOC[key].length || (key === 'settled' && DOC.undated);
  function section(key) {
    const all = DOC[key], open = OPEN.has(key), shown = open ? all : all.slice(0, CAP);
    return html`<section class="st-sec" id="gh-${key}" aria-labelledby="gh-${key}-h">
      <div class="sec-head"><h3 id="gh-${key}-h">${HEAD[key]} <span class="n">${all.length}</span></h3></div>
      ${note(key)}
      ${all.length ? html`<table class="ledger st" aria-labelledby="gh-${key}-h">
        <thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Pull request or issue</th><th scope="col">Why</th><th scope="col">Activity</th></tr></thead>
        <tbody>${shown.map(row)}</tbody></table>` : ''}
      ${all.length > CAP ? html`<button type="button" class="linkbtn more" data-gh-more="${key}" aria-expanded="${String(open)}">${open ? `Show the first ${CAP}` : `Show all ${all.length}`}</button>` : ''}
    </section>`;
  }

  // The counts, what waits on you, and when the GitHub collector last succeeded: a sync that is not fresh says so.
  function tally() {
    const waiting = DOC.pulls.filter(r => r.why === 'waiting on you').length, c = DOC.collector || {}, fresh = c.state === 'fresh';
    const sync = c.last_success_at
      ? html`<span${fresh ? '' : html` class="g-caution"`}>${fresh ? '' : `▲ sync ${c.state} · `}synced <time class="rel" datetime="${c.last_success_at}"></time></span>`
      : html`<span class="g-unknown">▨ sync ${c.state || 'unknown'}${c.reason ? `: ${c.reason}` : ''}</span>`;
    return html`${waiting ? html`<a class="g-caution" href="#gh-pulls">▲ ${waiting} waiting on you</a>` : ''}
      <span>${plural(DOC.pulls.length, 'open pull request')}</span><span>${plural(DOC.issues.length, 'open issue')}</span>
      <span>${DOC.settled.length} settled in ${DOC.days} d</span>${sync}`;
  }

  function draw() {
    if (!DOC || DOC.error) {
      const why = DOC ? `GitHub could not be read: ${DOC.error}. Refresh tries again.` : started ? `GitHub could not be read: ${failed}. Refresh tries again.` : 'Reading GitHub…';
      put($('github-tally'), html``);
      put($('github-sections'), html`<p class="st-line">${why}</p>`);
      return;
    }
    put($('github-tally'), tally());
    const full = ORDER.filter(shows), empty = ORDER.filter(k => !shows(k));
    const unread = Object.entries(DOC.unread || {});
    put($('github-sections'), html`${unread.map(([k, why]) => html`<p class="st-line"><span class="g-unknown">▨</span> ${UNREAD[k] || k} not read: ${why}</p>`)}${full.map(section)}${empty.length ? html`<p class="st-line st-empty">Nothing in ${empty.map(k => HEAD[k]).join(' · ')}.</p>` : ''}`);
  }

  // ---------- Details (sd:5633) ----------
  // A time the read carries, or not recorded: a missing stamp is never a time.
  const stamp = v => v ? html`<time class="rel" data-long datetime="${v}"></time>` : html`<span class="ph">not recorded</span>`;
  function details(key) {
    const r = pullOf(key); if (!r) return;
    SELECTED = key;
    $('github-sections').querySelectorAll('tr[data-pr]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.key === key)));
    window.shell.commands.select(oid(key));
    const s = state(r), settled = r.merged_at ? ['Merged', r.merged_at] : r.closed_at ? ['Closed', r.closed_at] : null;
    put($('details'), html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> Pull request · ${r.state}</p>
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
      ${r.checkout ? window.commands.reviewPane('change', r.checkout) : ''}`);
    window.shell.openPane('tab-details');
  }

  // Each read puts one command object per pull request row; a row the read no longer lists is retired, as read.js does,
  // so no command runs on it. A failed read retires them all.
  function objects() {
    const C = window.shell.commands, keep = new Set();
    pulls().forEach(r => { keep.add(oid(r.key)); C.put({ id: oid(r.key), type: TYPE, label: `${r.ref} ${r.title}`.trim(), repo: r.repo,
      number: r.number, url: r.url, state: r.state, item: r.item, checkout: r.checkout }); });
    PUT.forEach(id => { if (!keep.has(id)) C.put({ id, type: 'not listed', label: `${C.get(id)?.label || id} (no longer listed)` }); });
    PUT = keep;
    if (SELECTED && !pullOf(SELECTED)) SELECTED = null;
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
    );
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
    // The selected row's Details show this read's facts while it is still the shell's selection.
    if (SELECTED && window.shell.commands.selected?.()?.id === oid(SELECTED)) details(SELECTED);
  }

  document.addEventListener('shell:view', e => { if (e.detail && e.detail.id === 'github' && !started) load(); });
  document.addEventListener('DOMContentLoaded', () => {
    $('refresh').addEventListener('click', () => { if (started) load(); });
    register();
    // A run of this view's Adversarial review draws the selected row's Details again (commands.js follows the run).
    document.addEventListener('adversarial:run', e => { const r = SELECTED && pullOf(SELECTED);
      if (r && r.checkout && e.detail?.key === `change:${r.checkout}` && window.shell.commands.selected?.()?.id === oid(SELECTED)) details(SELECTED); });
    document.addEventListener('shell:open', e => { const id = String(e.detail || ''); if (id.startsWith('gh:') && pullOf(id.slice(3))) details(id.slice(3)); });
    $('github-sections').addEventListener('click', e => {
      const opened = e.target.closest('[data-gh-open]'); if (opened) return details(opened.dataset.ghOpen);
      const b = e.target.closest('button[data-gh-more]');
      if (!b) { const tr = !e.target.closest('a, button') && e.target.closest('tr[data-pr]'); if (tr) details(tr.dataset.key); return; }
      const key = b.dataset.ghMore;
      if (OPEN.has(key)) OPEN.delete(key); else OPEN.add(key);
      draw();
      // The redraw made a new button: focus moves to it, so a keyboard reader keeps their place.
      $('github-sections').querySelector(`button[data-gh-more="${key}"]`)?.focus();
    });
    draw();
  });
})();
