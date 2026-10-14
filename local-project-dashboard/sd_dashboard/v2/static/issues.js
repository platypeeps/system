// Your issues (sd:4628), on Today and Tasks: each repo's Jira and GitHub issues assigned to or filed by the operator.
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
})();
