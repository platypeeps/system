// Problem reports and new briefs, on Today (operator ruling 2026-10-17): what the reports view used to push by ntfy or mail.
// The rows are the page document's `problems` and `briefs` (today_reports.document); today.js hands them over with a
// today-reports:read event after each read, `error` set when the whole read failed. A list that could not be read is a row
// naming why, never an empty list. A row's title opens it in Details.
// Acknowledge posts /api/reports/<id>/acknowledge with the revision the read gave, the Reports page's route; Seen posts the
// Today snooze /api/snooze for the brief until its `until`, after it ages off Today. A refusal stays on the row; a landed
// write says so until Refresh, pressed for it, drops the row. Markup is html`…` from markup.js: every value is escaped.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  let DOC = {}, FAILED = '';
  const DONE = new Map(), REFUSED = new Map(), BUSY = new Set();
  const day = iso => String(iso || '').slice(0, 10);
  const hm = iso => String(iso || '').slice(11, 16);

  function control(id, label) {
    if (DONE.has(id)) return html`<span class="why">${DONE.get(id)}</span>`;
    return html`<button type="button" class="btn quiet sm" data-today-report="${id}"${BUSY.has(id) ? html` disabled` : ''}>${label}</button>
      ${REFUSED.has(id) ? html`<p class="refused" role="alert">Not done: ${REFUSED.get(id)}</p>` : ''}`;
  }
  const ackControl = r => r.fields_read ? control(r.id, 'Acknowledge')
    : html`<span class="why">fields unreadable: <code>sd reports acknowledge ${r.item}</code></span>`;

  function draw() {
    const problems = DOC.problems || [], briefs = DOC.briefs || [];
    const pErr = FAILED || DOC.problems_error, bErr = FAILED || DOC.briefs_error;
    // A full read: open reports older than the newest 200 are on Reports only, so the counts are at least this.
    const cap = DOC.problems_capped ? '+' : '';
    put($('problems'), pErr ? html`<tr class="empty"><td colspan="5">Reports could not be read: ${pErr}. Refresh tries again.</td></tr>`
      : problems.length ? html`${problems.map(r => html`<tr${BUSY.has(r.id) ? html` aria-busy="true"` : ''}>
        <td class="ref"><time datetime="${r.at}">${day(r.at)} ${hm(r.at)}</time></td>
        <td class="what"><button type="button" data-open="${r.id}">${r.title}</button></td>
        <td class="repo">${r.host || ''}</td>
        <td class="why">${r.why}</td>
        <td class="act">${ackControl(r)}</td></tr>`)}${DOC.problems_more ? html`<tr class="empty"><td colspan="5">${DOC.problems_more}${cap} more open on Reports.</td></tr>` : ''}`
      : html`<tr class="empty"><td colspan="5">No open report asks for you.</td></tr>`);
    const open = problems.filter(r => !DONE.has(r.id)).length + (DOC.problems_more || 0);
    put($('problems-tally'), problems.length && !pErr ? html`<span class="g-caution">▲ ${open}${cap} open</span>` : html``);
    put($('briefs'), bErr ? html`<tr class="empty"><td colspan="5">Briefs could not be read: ${bErr}. Refresh tries again.</td></tr>`
      : briefs.length ? html`${briefs.map(b => html`<tr${BUSY.has(b.id) ? html` aria-busy="true"` : ''}>
        <td class="ref"><time datetime="${b.at || b.day}">${b.day}${b.at ? ` ${hm(b.at)}` : ''}</time></td>
        <td class="what"><button type="button" data-open="${b.id}">${b.subj}</button></td>
        <td class="repo">${b.src}</td>
        <td class="why">${b.words} words</td>
        <td class="act">${control(b.id, 'Seen')}</td></tr>`)}${DOC.briefs_more ? html`<tr class="empty"><td colspan="5">${DOC.briefs_more} more on Briefs.</td></tr>` : ''}${
        DOC.briefs_skipped ? html`<tr class="empty"><td colspan="5">${DOC.briefs_skipped} rows skipped: not a brief.</td></tr>` : ''}`
      : html`<tr class="empty"><td colspan="5">No new brief this week. Older ones are on Briefs.</td></tr>`);
    const fresh = briefs.filter(b => !DONE.has(b.id)).length;
    put($('briefs-tally'), briefs.length && !bErr ? html`<span>${fresh} new</span>` : html``);
  }

  function details(id) {
    const r = (DOC.problems || []).find(x => x.id === id), b = (DOC.briefs || []).find(x => x.id === id);
    if (!r && !b) return;
    window.shell.commands.select(null);
    put($('details'), r ? html`<p class="kind"><span class="g-caution" aria-hidden="true">▲</span> Report · open</p>
      <h2>${r.title}</h2>
      <dl><dt>Filed</dt><dd>${day(r.at)} ${hm(r.at)} UTC</dd><dt>Job</dt><dd>${r.job || 'not recorded'}</dd>
        <dt>Host</dt><dd>${r.host || 'not recorded'}</dd><dt>Exit</dt><dd>${r.exit ?? 'not recorded'}</dd>
        ${r.repeats ? html`<dt>Repeats</dt><dd>${r.repeats}</dd>` : ''}<dt>Item</dt><dd><a href="${window.shell.itemHref(r.item)}">#${r.item}</a></dd></dl>
      ${r.basis ? html`<h3>Why</h3><div class="mailbody">${r.basis}</div>` : ''}
      <h3>Log${r.cut ? ' · last lines' : ''}</h3><div class="mailbody">${r.body || 'no text kept'}</div>
      <p class="why">Acknowledge closes the report, not the fault. The next failure opens a new one. <a href="/reports">Open Reports</a></p>
      <h3>Act</h3>${ackControl(r)}`
      : html`<p class="kind">Brief · ${b.src}</p>
      <h2>${b.subj}</h2>
      <dl><dt>Day</dt><dd>${b.day}</dd><dt>Filed</dt><dd>${b.at ? `${hm(b.at)} UTC` : 'time not known'}</dd><dt>Words</dt><dd>${b.words}</dd>
        <dt>Item</dt><dd><a href="${window.shell.itemHref(b.item)}">#${b.item}</a></dd></dl>
      <h3>Body · first lines</h3><div class="mailbody">${b.lead}${b.lead.length >= 200 ? '…' : ''}</div>
      <p><a href="${window.shell.itemHref(b.item)}">Read the whole brief</a></p>
      <h3>Act</h3>${control(b.id, 'Seen')}`);
    window.shell.openPane('tab-details');
  }

  function act(id) {
    const r = (DOC.problems || []).find(x => x.id === id), b = (DOC.briefs || []).find(x => x.id === id);
    if ((!r && !b) || BUSY.has(id) || DONE.has(id)) return;
    BUSY.add(id); REFUSED.delete(id); draw();
    const write = r ? window.shell.post(`/api/reports/${r.item}/acknowledge`, { revision: r.revision })
      : window.shell.post('/api/snooze', { page: 'today', row: b.id, until: b.until, seen: b.seen });
    write.then(() => { DONE.set(id, r ? 'Acknowledged' : 'Seen'); $('refresh')?.click(); }, e => { REFUSED.set(id, e.message); })
      .finally(() => { BUSY.delete(id); draw(); });
  }

  document.addEventListener('today-reports:read', e => {
    const d = e.detail || {};
    DOC = d; FAILED = d.error || ''; DONE.clear(); REFUSED.clear(); BUSY.clear(); draw();
  });
  document.addEventListener('click', e => {
    const t = e.target && e.target.closest;
    if (!t) return;
    const go = e.target.closest('[data-today-report]'); if (go) return act(go.dataset.todayReport);
    const open = e.target.closest('#problems [data-open], #briefs [data-open]'); if (open) details(open.dataset.open);
  });
})();
