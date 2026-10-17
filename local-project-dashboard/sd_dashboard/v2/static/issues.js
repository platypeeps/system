// Your issues (sd:4628), on Today and Tasks: each repo's Jira and GitHub issues assigned to or filed by the operator.
// Issue intake (sd:4840), on Today only, is at the end.
// The rows are the page document's `issues` (issues.document over sd_db.repos.mine_issues); the page script hands them over
// with an issues:read event after each read, `error` set when the read failed. A failure is a row naming it, never an empty list.
// Markup is html`…` from markup.js: every value is escaped, and only an https address the server kept becomes a link.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  function draw({ issues = [], error = '' } = {}) {
    put($('issues'), error ? html`<tr class="empty"><td colspan="4">Issues could not be read: ${error}. Refresh tries again.</td></tr>`
      : issues.length ? html`${issues.map(r => html`<tr>
        <td class="ref">${r.url ? html`<a href="${r.url}">${r.tracker} ${r.ref}</a>` : html`${r.tracker} ${r.ref}`}</td>
        <td class="what">${r.title}</td>
        <td class="repo">${r.repo}</td>
        <td class="why">${r.reason}</td></tr>`)}`
      : html`<tr class="empty"><td colspan="4">No open issue is assigned to you or filed by you in a repo with issue sources.</td></tr>`);
    put($('issues-tally'), issues.length && !error ? html`<span>${issues.length} open</span>` : html``);
  }
  document.addEventListener('issues:read', e => draw(e.detail));

  // Issue intake (sd:4840), on Today: each github_issues repo's open third-party issues no item carries (issues.intake over
  // sd_db.repos.intake_issues). File as item posts /api/items with the issue's title, repo and ref, the item `sd task add
  // --ref github:<owner>/<repo>#N` files; the row then names the item until the next read leaves it out. A refusal, such as
  // a ref another item already carries, stays on the row. Rows are keyed by that ref.
  let INTAKE = [], FAILED = '';
  const FILED = new Map(), REFUSED = new Map(), BUSY = new Set();
  function control(r) {
    if (FILED.has(r.key)) return html`<a href="${window.shell.itemHref(FILED.get(r.key))}">Filed #${FILED.get(r.key)}</a>`;
    return html`<button type="button" class="btn quiet sm" data-key="${r.key}"${BUSY.has(r.key) ? html` disabled` : ''}>File as item</button>
      ${REFUSED.has(r.key) ? html`<p class="refused" role="alert">Not filed: ${REFUSED.get(r.key)}</p>` : ''}`;
  }
  function drawIntake() {
    put($('intake'), FAILED ? html`<tr class="empty"><td colspan="5">Issue intake could not be read: ${FAILED}. Refresh tries again.</td></tr>`
      : INTAKE.length ? html`${INTAKE.map(r => html`<tr${BUSY.has(r.key) ? html` aria-busy="true"` : ''}>
        <td class="ref">${r.url ? html`<a href="${r.url}">GitHub ${r.ref}</a>` : html`GitHub ${r.ref}`}</td>
        <td class="what">${r.title}</td>
        <td class="repo">${r.repo}</td>
        <td class="why">${r.author}</td>
        <td class="act">${control(r)}</td></tr>`)}`
      : html`<tr class="empty"><td colspan="5">No third-party issue waits for an item in a repo with GitHub issues on.</td></tr>`);
    const waiting = INTAKE.filter(r => !FILED.has(r.key)).length;
    put($('intake-tally'), INTAKE.length && !FAILED ? html`<span>${waiting} waiting</span>` : html``);
  }
  function file(key) {
    const r = INTAKE.find(x => x.key === key);
    if (!r || BUSY.has(key) || FILED.has(key)) return;
    BUSY.add(key); REFUSED.delete(key); drawIntake();
    window.shell.post('/api/items', { title: r.title, repo: r.repo_path, ref: r.key })
      .then(out => { FILED.set(key, out.item.id); }, e => { REFUSED.set(key, e.message); })
      .finally(() => { BUSY.delete(key); drawIntake(); });
  }
  document.addEventListener('intake:read', e => {
    const { intake = [], error = '' } = e.detail || {};
    INTAKE = intake; FAILED = error; FILED.clear(); REFUSED.clear(); BUSY.clear(); drawIntake();
  });
  document.addEventListener('click', e => {
    const b = e.target && e.target.closest && e.target.closest('#intake button[data-key]');
    if (b) file(b.dataset.key);
  });
  window.issues = { file };
})();
