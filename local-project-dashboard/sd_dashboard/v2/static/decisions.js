// Today's Decisions (sd:3012): each question note with Option: lines, one button per option. It loads before shell.js.
// Rows: /api/decisions (sd_dashboard.decisions.document). A click posts /api/decisions/answer with the item, note, option
// and the item revision the read gave; the server writes the ruling and resolves the question in one transaction.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  const repoName = path => String(path || '').split('/').filter(Boolean).pop() || '';

  // LIST is the last read; null when it failed. RULED and REFUSED hold what a click did to a row until the next read.
  let LIST = [], reads = 0, failed = '';
  const RULED = new Map(), REFUSED = new Map(), BUSY = new Set();

  function choices(d) {
    const ruled = RULED.get(d.note);
    if (ruled) return html`<p class="ruled"><span class="g-ok" aria-hidden="true">●</span> Ruled: ${ruled.option} · note #${ruled.id}</p>`;
    return html`<div class="choices" role="group" aria-label="${`Choose for note ${d.note}`}">${d.options.map((o, i) => html`<button type="button" class="btn quiet sm" data-note="${d.note}" data-option="${i}" title="${o}"${BUSY.has(d.note) ? html` disabled` : ''}><span>${o}</span></button>`)}</div>
      ${REFUSED.has(d.note) ? html`<p class="refused" role="alert">Not recorded: ${REFUSED.get(d.note)}</p>` : ''}`;
  }

  // today.js counts the waiting decisions in Today's badge; a failed read counts none.
  const tell = n => document.dispatchEvent(new CustomEvent('today:decisions', { detail: n }));

  function draw() {
    const tb = $('decisions');
    if (!LIST) {
      put(tb, html`<tr class="empty"><td colspan="4">Decisions could not be read: ${failed}. Refresh tries again.</td></tr>`);
      put($('decisions-tally'), html``);
      return tell(0);
    }
    put(tb, LIST.length ? html`${LIST.map(d => { const ruled = RULED.has(d.note); return html`<tr data-item="${d.item}"${BUSY.has(d.note) ? html` aria-busy="true"` : ''}>
      <td class="g ${ruled ? 'g-ok' : 'g-caution'}" title="${ruled ? 'ruled' : 'waiting'}">${ruled ? '●' : '▲'}</td>
      <td class="what"><a href="/tasks?row=${d.item}"><span class="id">#${d.item}</span> ${d.title}</a><span class="repo">${repoName(d.repo)}</span></td>
      <td class="question">${d.question}<span class="asked-at">note #${d.note} · asked <time class="rel" datetime="${d.asked}"></time></span></td>
      <td class="act">${choices(d)}</td></tr>`; })}`
      : html`<tr class="empty"><td colspan="4">No decision waits on you. A question note with two Option: lines appears here.</td></tr>`);
    const waiting = LIST.filter(d => !RULED.has(d.note)).length;
    put($('decisions-tally'), LIST.length ? html`<span class="g-caution">▲ ${waiting} waiting</span>${RULED.size ? html`<span class="g-ok">● ${RULED.size} ruled</span>` : ''}` : html``);
    tell(waiting);
  }

  // Of overlapping reads only the newest draws.
  async function load() {
    const n = ++reads;
    try {
      const doc = await window.shell.getJSON('/api/decisions');
      if (n !== reads) return;
      LIST = doc.decisions; RULED.clear(); REFUSED.clear(); BUSY.clear();
    } catch (e) {
      if (n !== reads) return;
      LIST = null; failed = e.message;
    }
    draw();
  }

  function say(text) { const p = $('decisions-note'); p.textContent = text; p.hidden = !text; }

  async function choose(note, i) {
    const d = (LIST || []).find(x => x.note === note);
    if (!d || BUSY.has(note) || RULED.has(note) || !(i in d.options)) return;
    BUSY.add(note); REFUSED.delete(note); draw();
    try {
      const out = await window.shell.post('/api/decisions/answer', { item: d.item, note, revision: d.revision, option: d.options[i] });
      BUSY.delete(note);
      RULED.set(note, { option: d.options[i], id: out.ruling.id });
      // The answer changed the item: its other decisions carry the revision the answer returned.
      LIST.forEach(x => { if (x.item === d.item) x.revision = out.revision; });
      draw();
    } catch (e) {
      BUSY.delete(note);
      if (e.stale) { say(`Not recorded: ${e.message}. The list was read again; choose again if the question is still open.`); return load(); }
      REFUSED.set(note, e.message); draw();
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('decisions').addEventListener('click', e => {
      const b = e.target.closest('button[data-option]');
      if (b && !b.disabled) choose(+b.dataset.note, +b.dataset.option);
    });
    $('refresh').addEventListener('click', () => { say(''); load(); });
    load();
  });
})();
