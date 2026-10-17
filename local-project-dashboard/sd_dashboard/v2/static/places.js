// Places: the HOA and Home pages' views (Overview | Issues | Briefs | Reports) and Tasks' All | HOA | Home.
// Data: each place view reads its own document when it is shown, /api/<place>/<view> (sd_dashboard/places.py); Overview is
// the page's own read. Which rows are a place's is decided there, once (places.area_of): Tasks' HOA and Home views filter on
// the `area` each /api/tasks row carries, so they hold the Issues views' rows. A view that did not read is unknown with its
// reason, never an empty list. Nothing here writes. Markup is html`…` from markup.js: every value is escaped.
//
// Adapter: viewSwitch(views, show) is the one place a page meets the shell's view switch. Until the shell carries one
// (shell.viewSwitch), it draws a segmented control into #place-views and keeps the view as #view=<id> in the address.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);

  function viewSwitch(views, show) {
    if (typeof window.shell?.viewSwitch === 'function') return window.shell.viewSwitch(views, show);
    const slot = $('place-views'); if (!slot) return;
    const ids = views.map(v => v.id);
    const current = () => { const m = /^#view=([a-z-]+)$/.exec(location.hash); return m && ids.includes(m[1]) ? m[1] : ids[0]; };
    put(slot, html`<div class="seg" role="group" aria-label="${slot.dataset.label || 'View'}">${views.map(v => html`<button type="button" data-switch="${v.id}" aria-pressed="false">${v.label}</button>`)}</div>`);
    const apply = () => {
      const id = current();
      slot.querySelectorAll('button[data-switch]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.switch === id)));
      show(id);
    };
    slot.addEventListener('click', e => {
      const b = e.target.closest('button[data-switch]'); if (!b) return;
      if (location.hash === `#view=${b.dataset.switch}`) apply(); else location.hash = `view=${b.dataset.switch}`;
    });
    addEventListener('hashchange', apply);
    apply();
  }

  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  const unknown = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;
  const day = s => String(s || '').slice(0, 10);
  const table = (head, rows, empty) => html`<table class="ledger place-rows"><thead><tr>${head.map(([label, cls = '']) => html`<th scope="col" class="${cls}">${label}</th>`)}</tr></thead>
    <tbody>${rows.length ? rows : html`<tr class="empty"><td colspan="${head.length}">${empty}</td></tr>`}</tbody></table>`;

  // Each place view: its table head, one row, the empty text and the tally. The rows open where their page acts on them.
  const DRAW = {
    issues: (doc, place) => [table([['#'], ['What'], ['Kind', 'hide-sm'], ['Status'], ['Due', 'hide-sm']],
      doc.rows.map(r => html`<tr><td class="ref"><a href="${window.shell.itemHref(r.id)}">#${r.id}</a></td><td class="what">${r.title}</td>
        <td class="hide-sm">${r.kind}</td><td>${r.status}</td><td class="hide-sm">${day(r.due) || '—'}</td></tr>`),
      place === 'home' ? 'No open item without a repo.' : 'No open item in the hoa checkout.'), `${plural(doc.rows.length, 'open item')}`],
    briefs: doc => [table([['Day'], ['Brief'], ['Words', 'num hide-sm'], ['Lead', 'hide-sm']],
      doc.rows.map(r => html`<tr><td class="when">${r.day}</td><td class="what"><a href="/briefs?row=${encodeURIComponent(r.id)}">${r.subj}</a></td>
        <td class="num hide-sm">${r.words}</td><td class="lead hide-sm">${r.lead}</td></tr>`),
      'No brief is filed for this place.'), `${plural(doc.rows.length, 'brief')}`],
    reports: doc => {
      const state = r => r.status === 'done' ? 'ok' : r.attention ? 'warning' : 'queued';
      return [table([[html`<span class="sr">State</span>`], ['Job'], ['Report'], ['Status', 'hide-sm'], ['When']],
        doc.rows.map(r => html`<tr><td class="g ${state(r)}"><span aria-hidden="true">${GLYPH[state(r)]}</span><span class="sr">${state(r)}</span></td>
          <td class="job">${r.job}</td><td class="what"><a href="/reports?row=r${r.id}">${r.title}</a></td><td class="hide-sm">${r.status}</td><td class="when">${day(r.at)}</td></tr>`),
        `No report from ${doc.jobs.join(', ')} among the newest ${doc.limit}.`), `${plural(doc.rows.filter(r => r.status !== 'done').length, 'open report')} · ${doc.jobs.length} jobs`];
    },
  };

  const seen = {};
  async function load(place, view) {
    const box = $(`${view}-rows`), tally = $(`${view}-tally`), mine = seen[view] = (seen[view] || 0) + 1;
    put(box, html`<p class="note">Reading ${view}…</p>`); put(tally, html``);
    let doc;
    try { doc = await window.shell.getJSON(`/api/${place}/${view}`); } catch (e) { doc = { state: 'unknown', reason: `/api/${place}/${view} did not answer: ${e.message}`, rows: [] }; }
    if (mine !== seen[view]) return;
    if (doc.state !== 'read') { put(box, unknown(doc.reason)); return; }
    const [body, count] = DRAW[view](doc, place);
    put(box, html`${doc.reason ? html`<p class="note">${doc.reason}</p>` : ''}${body}`);
    put(tally, html`<span>${count}</span>`);
  }

  // A place page: <body data-place="hoa|home">, an Overview block and one section per view, each [data-place-view].
  // The wall display (html[data-kiosk], home-kiosk.js) stays on the Overview: it has no switch.
  function place() {
    if (document.documentElement.hasAttribute('data-kiosk')) return;
    const id = document.body.dataset.place;
    const views = [{ id: 'overview', label: 'Overview' }, ...['issues', 'briefs', 'reports'].map(v => ({ id: v, label: v[0].toUpperCase() + v.slice(1), api: `/api/${id}/${v}` }))];
    viewSwitch(views, view => {
      document.querySelectorAll('[data-place-view]').forEach(s => { s.hidden = s.dataset.placeView !== view; });
      if (view !== 'overview') load(id, view);
    });
  }

  window.places = { viewSwitch, place };
  if (document.body.dataset.place) document.addEventListener('shell:ready', place, { once: true });
})();
