// Today's Status view (sd:5099): the sections `sd today` and `sd-status` print, from /api/today/status, which is the pack's
// sd_db.today.sections (pack sd:5098) as it stands; nothing here re-derives a section. It loads before shell.js.
// The view reads when it first shows and on Refresh. The flow strip draws where the work is (delivered in the last 24 h,
// landing, planned, unscheduled, blocked) as one bar with a legend whose entries link to their sections; the tally counts
// what waits on you. A section shows its first CAP rows, as `sd today` does, and Show all opens the rest in place; the
// sections with nothing in them share one line, so an empty day stays one screen. Every row links to its item.
// A read that failed says why and draws no count; a gate slot read that failed is unknown with its reason, never idle.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);
  // sd_views.ROWS: the rows a section shows before Show all.
  const CAP = 5;
  const HEAD = { attention: 'Attention', decisions: 'Decisions', landing: 'Landing now', delivered: 'Delivered', planned: 'Planned',
    blocked: 'Blocked', unscheduled: 'Unscheduled', followups: 'Followups' };
  const ORDER = Object.keys(HEAD);
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  // A lane entry's state in design.md's grammar: running is live, a failure is a warning, a handback waits on someone.
  const LANE = { running: 'ok', pending: 'queued', prepared: 'queued', handed_back: 'caution', failed: 'warning', skipped: 'queued' };
  const STATE = { attention: () => 'caution', decisions: () => 'caution', landing: r => LANE[r.status] || 'unknown', delivered: () => 'ok',
    planned: () => 'queued', blocked: () => 'caution', unscheduled: () => 'queued', followups: () => 'queued' };
  const DETAIL = { attention: 'Why', decisions: 'Question', landing: 'Lane', delivered: 'Landed as', planned: 'Due',
    blocked: 'Waiting on', unscheduled: 'Priority', followups: 'Followup' };
  // The flow strip's segments, in the order work moves; Blocked stands aside, so it closes the bar.
  const FLOW = ['delivered', 'landing', 'planned', 'unscheduled', 'blocked'];

  let DOC = null, failed = '', reads = 0, started = false;
  const OPEN = new Set();
  let REPO = ''; // Unscheduled's Repo select (sd:5653): '' draws every repo

  // sd_db.repos.short: `~/repos/system` is `system`, `owner/name` is `name`, and none is `fleet`.
  const repo = r => String(r.repo || r.repository || '').split('/').filter(Boolean).pop() || 'fleet';
  const rows = key => !DOC ? [] : key === 'landing' ? DOC.landing.entries : DOC[key];
  // Unscheduled draws the picked repo's rows; a repo gone from the read drops the pick.
  const picked = key => key === 'unscheduled' && REPO ? rows(key).filter(r => repo(r) === REPO) : rows(key);
  const prio = p => p === null || p === undefined ? 'no priority' : `P${p}`;
  // A due date against the read's local day, as sd_views.days counts it.
  function days(due) {
    const d = Date.parse(`${due}T12:00:00`), today = new Date(DOC.now), noon = new Date(today.getFullYear(), today.getMonth(), today.getDate(), 12);
    if (Number.isNaN(d)) return '';
    const n = Math.round((d - noon.getTime()) / 864e5);
    return n === 0 ? 'today' : n > 0 ? `in ${n} d` : `${-n} d ago`;
  }
  const when = iso => iso ? html`<time class="rel" datetime="${iso}"></time>` : html`<span class="ph">not known</span>`;

  function detail(key, r) {
    switch (key) {
      case 'attention': return r.why || 'needs attention';
      case 'decisions': case 'followups': return html`${r.title}<span class="sub">note #${r.note}</span>`;
      case 'landing': return html`${r.status}${r.step && !r.place ? ` at ${r.step}` : ''}${r.place ? html`<span class="sub">position ${r.place}</span>` : ''}`;
      case 'delivered': return html`${r.pr ? `PR #${r.pr}` : 'delivered'}<span class="sub">${String(r.commit || '').slice(0, 9)}</span>`;
      case 'planned': return html`${r.due}<span class="sub">${prio(r.priority)}</span>`;
      case 'blocked': return r.why || (r.status === 'blocked' ? 'no reason recorded' : 'waiting');
      default: return html`${prio(r.priority)}<span class="sub">${r.status}</span>`;
    }
  }
  function moment(key, r) {
    if (key === 'planned') return days(r.due);
    return when({ attention: r.at, decisions: r.at, followups: r.at, delivered: r.at, landing: r.since, blocked: r.since, unscheduled: r.created_at }[key]);
  }
  const row = key => r => {
    const s = STATE[key](r), title = key === 'decisions' || key === 'followups' ? r.item_title : r.title;
    return html`<tr data-item="${r.id}">
      <td class="g g-${s}" title="${s}">${GLYPH[s]}</td>
      <td class="what"><a href="${window.shell.itemHref(r.id)}"><span class="id">#${r.id}</span> ${title || ''}</a><span class="repo">${repo(r)}</span></td>
      <td class="detail">${detail(key, r)}</td>
      <td class="when">${moment(key, r)}</td></tr>`;
  };

  // The machine's gate slots, `sd today`'s line under Landing now. It sits under the strip, so it shows with no lane entry too.
  function gate() {
    const g = DOC.landing.gate;
    if (!g) return '';
    if (g.error) return html`<p class="st-line"><span class="g-unknown" aria-hidden="true">▨</span> Gate slots unknown: ${g.error}</p>`;
    return html`<p class="st-line">Gate slots: ${g.holders.length} holding${g.holders.length ? html` (${g.holders.map(h => h.label || 'unlabelled').join(', ')})` : ''} · ${g.waiters.length} waiting</p>`;
  }

  // The line under a heading: what `sd today` prints after the table (closed by hand, review questions), or a summary.
  function note(key) {
    const sum = DOC.summary;
    if (key === 'delivered' && sum.closed_by_hand) return html`<p class="st-line">+${sum.closed_by_hand} closed by hand</p>`;
    if (key === 'decisions') return html`<p class="st-line"><a href="#view=decisions">Answer on Decisions</a>${sum.review_questions ? html` · <a href="/review">${plural(sum.review_questions, 'review question')} on Review</a>` : ''}</p>`;
    if (key === 'attention') return html`<p class="st-line"><a href="#view=inbox">Acknowledge in Inbox</a></p>`;
    if (key === 'planned') return html`<p class="st-line">${sum.planned_this_week} due in the next 7 days</p>`;
    if (key === 'unscheduled') {
      const mix = new Map();
      picked(key).forEach(r => mix.set(prio(r.priority), (mix.get(prio(r.priority)) || 0) + 1));
      return html`<p class="st-line">${[...mix].sort((a, b) => (a[0] > b[0]) - (a[0] < b[0])).map(([p, n]) => `${p} ${n}`).join(' · ')}</p>`;
    }
    return '';
  }

  function repoPick() {
    const n = new Map();
    rows('unscheduled').forEach(r => n.set(repo(r), (n.get(repo(r)) || 0) + 1));
    return html`<label class="st-pick">Repo <select data-st-repo><option value="">All repos (${String(rows('unscheduled').length)})</option>${[...n].sort((a, b) => a[0].localeCompare(b[0])).map(([r, c]) => html`<option value="${r}"${r === REPO ? html` selected` : ''}>${r} (${String(c)})</option>`)}</select></label>`;
  }

  // A section shows when it has rows, or a line that counts what it leaves out (closed by hand, review questions).
  const shows = key => rows(key).length || (key === 'delivered' && DOC.summary.closed_by_hand) || (key === 'decisions' && DOC.summary.review_questions);
  function section(key) {
    if (key === 'unscheduled' && REPO && !rows(key).some(r => repo(r) === REPO)) REPO = '';
    const all = picked(key), open = OPEN.has(key), shown = open ? all : all.slice(0, CAP);
    return html`<section class="st-sec" id="st-${key}" aria-labelledby="st-${key}-h">
      <div class="sec-head"><h3 id="st-${key}-h">${HEAD[key]} <span class="n">${all.length}</span></h3>${key === 'unscheduled' ? repoPick() : ''}</div>
      ${note(key)}
      ${all.length ? html`<table class="ledger st" aria-labelledby="st-${key}-h">
        <thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Item</th><th scope="col">${DETAIL[key]}</th><th scope="col">When</th></tr></thead>
        <tbody>${shown.map(row(key))}</tbody></table>` : ''}
      ${all.length > CAP ? html`<button type="button" class="linkbtn more" data-st-more="${key}" aria-expanded="${String(open)}">${open ? `Show the first ${CAP}` : `Show all ${all.length}`}</button>` : ''}
    </section>`;
  }

  // The flow strip: one bar, a segment per section in FLOW with work in it, each at least a sliver so a small count shows;
  // its key links each count to its section, and an empty one is text, since its section is on the empty line.
  function flow() {
    const n = key => rows(key).length, live = FLOW.filter(k => n(k)), total = live.reduce((a, k) => a + n(k), 0);
    const label = key => key === 'delivered' ? 'Delivered, 24 h' : HEAD[key];
    const summary = FLOW.map(k => `${label(k)} ${n(k)}`).join(', ');
    let x = 0;
    const widths = live.map(k => Math.max(n(k) / total * 1000, 12)), scale = 1000 / widths.reduce((a, w) => a + w, 0);
    return html`${total ? html`<svg class="flow-bar" viewBox="0 0 1000 12" preserveAspectRatio="none" role="img" aria-label="${summary}">${live.map((k, i) => {
        const w = widths[i] * scale, at = x; x += w;
        return html`<rect class="seg-${k}" x="${at.toFixed(1)}" y="0" width="${Math.max(w - 2, 1).toFixed(1)}" height="12"><title>${label(k)}: ${n(k)}</title></rect>`;
      })}</svg>` : html`<p class="st-line">Nothing delivered, landing, planned, unscheduled or blocked.</p>`}
      <ul class="flow-key">${FLOW.map(k => { const text = html`<span class="sw seg-${k}" aria-hidden="true"></span>${label(k)} <b>${n(k)}</b>${k === 'planned' && n(k) ? html` <span class="ph">${DOC.summary.planned_this_week} this week</span>` : ''}`;
        return html`<li>${shows(k) ? html`<a href="#st-${k}">${text}</a>` : html`<span>${text}</span>`}</li>`; })}</ul>${gate()}`;
  }

  // What waits on you, the summary line's first half: each count links to its section.
  function tally() {
    const n = key => rows(key).length, mark = key => n(key) ? (key === 'followups' ? 'g-queued' : 'g-caution') : '';
    const glyph = key => n(key) ? (key === 'followups' ? '◌ ' : '▲ ') : '';
    return html`${['attention', 'decisions', 'followups'].map(k => shows(k) ? html`<a class="${mark(k)}" href="#st-${k}">${glyph(k)}${n(k)} ${k}</a>` : html`<span>${n(k)} ${k}</span>`)}<a href="#view=work">${DOC.work.length} in your work</a><span>read <time class="rel" datetime="${DOC.now}"></time></span>`;
  }

  function draw() {
    if (!DOC) {
      const why = started ? `Status could not be read: ${failed}. Refresh tries again.` : 'Reading status…';
      put($('status-tally'), html``); put($('status-flow'), html``);
      put($('status-sections'), html`<p class="st-line">${why}</p>`);
      return;
    }
    put($('status-tally'), tally());
    put($('status-flow'), flow());
    const full = ORDER.filter(shows), empty = ORDER.filter(k => !shows(k));
    put($('status-sections'), html`${full.map(section)}${empty.length ? html`<p class="st-line st-empty">Nothing in ${empty.map(k => HEAD[k]).join(' · ')}.</p>` : ''}`);
  }

  // Of overlapping reads only the newest draws.
  async function load() {
    const n = ++reads; started = true;
    try {
      const doc = await window.shell.getJSON('/api/today/status');
      if (n !== reads) return;
      DOC = doc;
    } catch (e) {
      if (n !== reads) return;
      DOC = null; failed = e.message;
    }
    draw();
  }

  document.addEventListener('shell:view', e => { if (e.detail && e.detail.id === 'status' && !started) load(); });
  document.addEventListener('DOMContentLoaded', () => {
    $('refresh').addEventListener('click', () => { if (started) load(); });
    $('status-sections').addEventListener('click', e => {
      const b = e.target.closest('button[data-st-more]'); if (!b) return;
      const key = b.dataset.stMore;
      if (OPEN.has(key)) OPEN.delete(key); else OPEN.add(key);
      draw();
      // The redraw made a new button: focus moves to it, so a keyboard reader keeps their place.
      $('status-sections').querySelector(`button[data-st-more="${key}"]`)?.focus();
    });
    $('status-sections').addEventListener('change', e => {
      const s = e.target.closest('select[data-st-repo]'); if (!s) return;
      REPO = s.value; draw();
      $('status-sections').querySelector('select[data-st-repo]')?.focus();
    });
    draw();
  });
})();
