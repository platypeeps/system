// Today's Decisions (sd:3012): each open question note, one button per option. It loads before shell.js.
// Rows: /api/decisions (sd_dashboard.decisions.document). A click posts /api/decisions/answer with the item, note, option
// and the item revision the read gave; the server writes the ruling and resolves the question in one transaction.
// A question with fewer than two Option: lines (sd:5064) gets a text box: its words post as the option, the same ruling
// `sd task note N "<words>" --kind decision --answers <note>` writes.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  const repoName = path => String(path || '').split('/').filter(Boolean).pop() || '';

  // LIST is the last read; null when it failed. RULED and REFUSED hold what a click did to a row until the next read.
  // REVIEW counts the questions on review batches (sd:3709): Review asks them, so Today draws one line that links there.
  let LIST = [], REVIEW = 0, reads = 0, failed = '';
  const RULED = new Map(), REFUSED = new Map(), BUSY = new Set(), DRAFT = new Map();
  // The marker an option carries (sd:5306); the label drops it, the posted text keeps it.
  const REC = /^\s*(?:\(recommended\)|recommended:)\s*|\s*\(recommended\)\s*$/gi, label = o => o.replace(REC, '');
  // Sort (sd:3455): the shell's sort header, in memory as Today's other tables. The read's order (priority, then the oldest)
  // stands until a header is pressed; a tie keeps it.
  const low = s => String(s || '').toLowerCase();
  const SORT = { sort: '', dir: 1, page: 1 };
  const BY = { s: d => RULED.has(d.note) ? 1 : 0, what: d => low(d.title), question: d => low(d.question) };
  const COLS = [['s', html`<span class="sr">State</span>`, 'g'], ['what', 'Item'], ['question', 'Question'], [null, 'Choose', 'act']];
  const sorted = list => !SORT.sort ? list : [...list].sort((a, b) => { const x = BY[SORT.sort](a), y = BY[SORT.sort](b); return (x < y ? -1 : x > y ? 1 : 0) * SORT.dir; });

  const advice = d => { const rec = d.options[d.recommended]; return rec != null ? html`<span class="advice">Recommended: ${label(rec)}</span>` : html`<span class="advice none">No recommendation</span>`; };
  const refused = d => REFUSED.has(d.note) ? html`<p class="refused" role="alert">Not recorded: ${REFUSED.get(d.note)}</p>` : '';
  function choices(d) {
    const ruled = RULED.get(d.note);
    if (ruled) return html`<p class="ruled"><span class="g-ok" aria-hidden="true">●</span> Ruled: ${ruled.option} · note #${ruled.id}</p>`;
    // sd:5064: a free-text question; a typed draft survives a redraw.
    if (d.options.length < 2) {
      const off = BUSY.has(d.note) ? html` disabled` : '';
      return html`<form class="answer" data-note="${d.note}"><label class="sr" for="${`answer-${d.note}`}">${`Answer note ${d.note}`}</label><input id="${`answer-${d.note}`}" name="answer" autocomplete="off" maxlength="4000" placeholder="${d.options[0] || 'Your answer'}" value="${DRAFT.get(d.note) || ''}"${off}><button type="submit" class="btn sm"${off}>Answer</button></form>${refused(d)}`;
    }
    // sd:5306: the server names the recommended option; a filled button picks it, the others stay quiet in written order.
    const off = BUSY.has(d.note) ? html` disabled` : '', rec = d.options[d.recommended];
    return html`<div class="choices" role="group" aria-label="${`Choose for note ${d.note}`}">${rec != null ? html`<button type="button" class="btn sm" data-note="${d.note}" data-option="${d.recommended}" title="${rec}"${off}><span>Choose recommended</span></button>` : ''}${d.options.map((o, i) => html`<button type="button" class="btn quiet sm" data-note="${d.note}" data-option="${i}" title="${o}"${off}><span>${label(o)}</span></button>`)}</div>
      ${refused(d)}`;
  }

  // today.js counts the waiting decisions in Today's badge; a failed read counts none.
  const tell = n => document.dispatchEvent(new CustomEvent('today:decisions', { detail: n }));

  function draw() {
    const tb = $('decisions'), line = $('decisions-review'), n = LIST ? REVIEW : 0;
    put($('decisions-head'), window.shell.list.sortHead(COLS, SORT));
    put(line, n ? html`<span class="g-caution" aria-hidden="true">▲</span> <a href="/review">${n} ${n === 1 ? 'question waits' : 'questions wait'} on Review</a>` : html``);
    line.hidden = !n;
    if (!LIST) {
      put(tb, html`<tr class="empty"><td colspan="4">Decisions could not be read: ${failed}. Refresh tries again.</td></tr>`);
      put($('decisions-tally'), html``); window.todayCount?.('decisions', null);
      return tell(0);
    }
    put(tb, LIST.length ? html`${sorted(LIST).map(d => { const ruled = RULED.has(d.note); return html`<tr data-item="${d.item}"${BUSY.has(d.note) ? html` aria-busy="true"` : ''}>
      <td class="g ${ruled ? 'g-ok' : 'g-caution'}" title="${ruled ? 'ruled' : 'waiting'}">${ruled ? '●' : '▲'}</td>
      <td class="what"><a href="${window.shell.itemHref(d.item)}"><span class="id">#${d.item}</span> ${d.title}</a><span class="repo">${repoName(d.repo)}</span></td>
      <td class="question">${d.question}${advice(d)}<span class="asked-at">note #${d.note} · asked <time class="rel" datetime="${d.asked}"></time></span></td>
      <td class="act">${choices(d)}</td></tr>`; })}`
      : html`<tr class="empty"><td colspan="4">No decision waits on you. An open question note appears here; one with two Option: lines gets a button each.</td></tr>`);
    const waiting = LIST.filter(d => !RULED.has(d.note)).length;
    put($('decisions-tally'), LIST.length ? html`<span class="g-caution">▲ ${waiting} waiting</span>${RULED.size ? html`<span class="g-ok">● ${RULED.size} ruled</span>` : ''}` : html``);
    window.todayCount?.('decisions', waiting);
    tell(waiting);
  }

  // Of overlapping reads only the newest draws.
  async function load() {
    const n = ++reads;
    try {
      const doc = await window.shell.getJSON('/api/decisions');
      if (n !== reads) return;
      LIST = doc.decisions; REVIEW = doc.review || 0; RULED.clear(); REFUSED.clear(); BUSY.clear();
    } catch (e) {
      if (n !== reads) return;
      LIST = null; failed = e.message;
    }
    draw();
  }

  function say(text) { const p = $('decisions-note'); p.textContent = text; p.hidden = !text; }

  // i picks an option; words answer a free-text question.
  async function choose(note, i, words) {
    const d = (LIST || []).find(x => x.note === note);
    const option = words != null ? words.trim() : d && d.options.length >= 2 ? d.options[i] : undefined;
    if (!d || BUSY.has(note) || RULED.has(note) || !option || (words != null) !== (d.options.length < 2)) return;
    BUSY.add(note); REFUSED.delete(note); draw();
    try {
      const out = await window.shell.post('/api/decisions/answer', { item: d.item, note, revision: d.revision, option });
      BUSY.delete(note); DRAFT.delete(note);
      RULED.set(note, { option, id: out.ruling.id });
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
    $('decisions').addEventListener('submit', e => {
      const f = e.target.closest('form.answer'); if (!f) return;
      e.preventDefault(); choose(+f.dataset.note, null, f.elements.answer.value);
    });
    $('decisions').addEventListener('input', e => { const f = e.target.closest('form.answer'); if (f) DRAFT.set(+f.dataset.note, e.target.value); });
    $('refresh').addEventListener('click', () => { say(''); load(); });
    $('decisions-head').addEventListener('click', e => { if (window.shell.list.sortBy(e, SORT)) window.shell.list.keepFocus(e, draw); });
    put($('decisions-head'), window.shell.list.sortHead(COLS, SORT)); // drawn before the read, so a failed one keeps the columns
    load();
  });
})();
