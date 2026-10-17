// Places: the HOA and Home pages' views (Overview | Tasks | Briefs | Reports) and Tasks' All | HOA | Home.
// Data: a place view with data-api reads its own document when it is shown, /api/<place>/<view> (sd_dashboard/places.py).
// Overview is the page's own read, and so is HOA's Tasks: hoa.js draws it from /api/hoa, which the annunciator and the badge
// read anyway. Which rows are a place's is decided once (places.area_of): Tasks' HOA and Home views filter on the `area` each
// /api/tasks row carries, so they hold the place pages' Tasks rows. A view that did not read is unknown with its reason, never
// an empty list. Nothing here writes. Markup is html`…` from markup.js: every value is escaped.
//
// Adapter: viewSwitch(show) is the one place a page meets the shell's view switch (shell.js view:start). The page declares
// window.PAGE_VIEWS = [{ id, label, key?, api? }] before shell.js; show(id) runs for the view shown, at shell:ready and on
// each change. With shell.view the shell draws the switch, hides the view-<id> panels and fires 'shell:view'. Without it
// (until that shell lands), this draws a segmented control into #viewswitch and keeps #view=<id>.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);

  // Called at shell:ready, after the shell's first 'shell:view': the view already shown is shown here.
  function viewSwitch(show) {
    const S = window.shell, views = window.PAGE_VIEWS || [];
    if (S.view) { document.addEventListener('shell:view', e => show(e.detail.id)); if (S.view.current()) show(S.view.current()); return; }
    const slot = $('viewswitch'); if (!slot) return;
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
    tasks: doc => [table([['ID', 'num'], ['What'], ['Kind', 'hide-sm'], ['Status'], ['Due', 'hide-sm']],
      doc.rows.map(r => html`<tr><td class="ref num"><a href="${window.shell.itemHref(r.id)}">sd:${r.id}</a></td><td class="what">${r.title}</td>
        <td class="hide-sm">${r.kind}</td><td>${r.status}</td><td class="hide-sm">${day(r.due) || '—'}</td></tr>`),
      'No open item without a repo.'), `${plural(doc.rows.length, 'open item')}`],
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
  async function load({ id: view, api }) {
    const box = $(`${view}-rows`), tally = $(`${view}-tally`), mine = seen[view] = (seen[view] || 0) + 1;
    put(box, html`<p class="note">Reading ${view}…</p>`); put(tally, html``);
    let doc;
    try { doc = await window.shell.getJSON(api); } catch (e) { doc = { state: 'unknown', reason: `${api} did not answer: ${e.message}`, rows: [] }; }
    if (mine !== seen[view]) return;
    if (doc.state !== 'read') { put(box, unknown(doc.reason)); return; }
    const [body, count] = DRAW[view](doc);
    put(box, html`${doc.reason ? html`<p class="note">${doc.reason}</p>` : ''}${body}`);
    put(tally, html`<span>${count}</span>`);
  }

  // A place page: <body data-place="hoa|home"> and one block per view, id="view-<id>" with data-place-view, its data-label,
  // and data-api when the view reads its own document. A view reads each time it shows, not only the first ('shell:view'
  // first), since nothing else reads it again. The wall display (html[data-kiosk], home-kiosk.js) stays on the Overview: it
  // declares no views.
  const blocks = [...document.querySelectorAll('[data-place-view]')];
  function place() {
    viewSwitch(id => {
      blocks.forEach(b => { b.hidden = b.dataset.placeView !== id; });
      const v = window.PAGE_VIEWS.find(x => x.id === id);
      if (v?.api) load(v);
    });
  }

  window.places = { viewSwitch, place };
  // Top level: places.js loads before shell.js, which reads PAGE_VIEWS when it loads.
  if (document.body.dataset.place && !document.documentElement.hasAttribute('data-kiosk')) {
    window.PAGE_VIEWS = blocks.map(b => ({ id: b.dataset.placeView, label: b.dataset.label, ...(b.dataset.api ? { api: b.dataset.api } : {}) }));
    document.addEventListener('shell:ready', place, { once: true });
  }
})();
