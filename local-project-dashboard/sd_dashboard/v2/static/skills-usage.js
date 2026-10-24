// Skills' Usage view (sd:5881; Metrics' Skills view until then): skill use per Monday-start week for four weeks, this week
// first, and this week's ten most used skills, from /api/skills/usage (skills_screen.usage over reads.skill_use_days), read
// the first time the view shows. A skill links to its catalog row. A failed read is a row naming it, never an empty list,
// and the next showing reads it again (sd:5964). Read again (skills.js's PAGE_AGAIN) rereads it once it was shown.
// It loads after skills.js, which declares the view, and before shell.js.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  function draw(u, error) {
    if (!u || error) {
      put($('su-weeks'), html`<tr class="empty"><td colspan="2">Skill use could not be read: ${error || 'no answer'}. Read again tries again.</td></tr>`);
      put($('su-top'), html``); $('usage-tally').textContent = '';
      return;
    }
    $('usage-tally').textContent = `Monday-start weeks since ${u.since}${u.first ? `; the first row is ${u.first}` : '; no row yet'}`;
    put($('su-weeks'), html`${u.weeks.map((w, i) => html`<tr><td>${w.start}${i ? '' : ' · this week'}</td><td>${w.uses}</td></tr>`)}`);
    put($('su-top'), u.top.length ? html`${u.top.map(([n, v]) => html`<tr><td><a href="/skills?row=${encodeURIComponent(n)}#view=catalog">${n}</a></td><td>${v}</td></tr>`)}`
      : html`<tr class="empty"><td colspan="2">No skill use this week.</td></tr>`);
  }
  let started = false;
  const read = () => {
    started = true;
    return window.shell.getJSON('/api/skills/usage').then(u => draw(u, ''), err => { started = false; draw(null, err.message); });
  };
  document.addEventListener('shell:view', e => { if (e.detail?.id === 'usage' && !started) read(); });
  const again = () => (started || window.shell.view?.current() === 'usage' ? read() : null);
  window.skillsUsage = { draw, again };
})();
