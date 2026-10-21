// Today's GitHub view (sd:5608): the operator's open pull requests and issues, and those merged or closed in the last 7
// days, from /api/today/github (today_github.document). Nothing here calls GitHub: the rows are what sd shadow sync and the
// Contributions collector stored. It loads before shell.js, and reads when the view first shows and on Refresh.
// Each part shows its first CAP rows, newest activity first, and Show all opens the rest in place; the parts with nothing
// in them share one line. A row's title links to GitHub and its item, when one is known, to Tasks. A row with no activity
// stamp says not recorded, never a time; a source that was not read is a line naming it; a read that failed says why.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
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

  let DOC = null, failed = '', reads = 0, started = false;
  const OPEN = new Set();

  const state = r => r.state !== 'open' ? 'ok' : r.why === 'waiting on you' ? 'caution' : 'queued';
  const when = r => r.at ? html`${r.at_what} <time class="rel" datetime="${r.at}"></time>` : html`<span class="ph">activity not recorded</span>`;
  const row = r => {
    const s = state(r);
    return html`<tr data-key="${r.key}">
      <td class="g g-${s}" title="${r.state}">${GLYPH[s]}</td>
      <td class="what">${r.url ? html`<a href="${r.url}" rel="noopener noreferrer">${r.title || r.ref}</a>` : r.title || r.ref}<span class="repo">${KIND[r.kind]} · ${r.ref}</span></td>
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
    draw();
  }

  document.addEventListener('shell:view', e => { if (e.detail && e.detail.id === 'github' && !started) load(); });
  document.addEventListener('DOMContentLoaded', () => {
    $('refresh').addEventListener('click', () => { if (started) load(); });
    $('github-sections').addEventListener('click', e => {
      const b = e.target.closest('button[data-gh-more]'); if (!b) return;
      const key = b.dataset.ghMore;
      if (OPEN.has(key)) OPEN.delete(key); else OPEN.add(key);
      draw();
      // The redraw made a new button: focus moves to it, so a keyboard reader keeps their place.
      $('github-sections').querySelector(`button[data-gh-more="${key}"]`)?.focus();
    });
    draw();
  });
})();
