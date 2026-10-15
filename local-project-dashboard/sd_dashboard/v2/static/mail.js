// Your mail (sd:4191), on Today: the mail threads waiting on the operator, under the repo whose alias they came to.
// The rows are the page document's `mail` (today_mail.document over local-mail-intake's state); today.js hands them over
// with a mail:read event after each read, `mail_error` set when the read failed. A repo whose mail cannot be read is a row
// naming why, never an empty list. Markup is html`…` from markup.js: every value is escaped; the server builds each link.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  function draw({ mail = [], mail_more: more = 0, mail_unread: unread = [], mail_error: error = '' } = {}) {
    put($('mail'), error ? html`<tr class="empty"><td colspan="4">Mail could not be read: ${error}. Refresh tries again.</td></tr>`
      : mail.length || more || unread.length ? html`${mail.map(r => html`<tr>
        <td class="ref"><time class="rel" datetime="${r.at}"></time></td>
        <td class="what"><a href="${r.url}">${r.subject}</a></td>
        <td class="repo">${r.repo}</td>
        <td class="why">${r.who}</td></tr>`)}${more ? html`<tr class="empty"><td colspan="4">${more} more wait in Gmail.</td></tr>` : ''}${
        unread.map(u => html`<tr class="empty"><td colspan="4">${u.repo}: mail not read: ${u.reason}</td></tr>`)}`
      : html`<tr class="empty"><td colspan="4">No thread waits on you in a repo with mail aliases.</td></tr>`);
    put($('mail-tally'), mail.length && !error ? html`<span>${mail.length + more} waiting</span>` : html``);
  }
  document.addEventListener('mail:read', e => draw(e.detail));
})();
