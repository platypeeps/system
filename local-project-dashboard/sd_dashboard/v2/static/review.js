// Review (sd:3709): the labelling queue. It loads before shell.js, which reads PAGE_LIST and PAGE_KEYS from it.
// Rows: /api/review (review_screen.document): one lamp per open batch, one ledger row per open question, nightly
// suggestions first. Details shows the selected question large, its options as numbered buttons.
// Keys (products/system/design.md, Keys): 1-9 label the selected question and open the next open one; j / k move without
// a label (a skip); u is the shell's Undo while the label's toast shows (10 s); n is the shell's capture, which files a
// comment "On note #N: ..." on the batch; Esc clears the batch filter; ? lists the keys. No other digit is bound.
// Writes run one at a time, each with the batch revision the last write returned, so ten quick digits are ten labels.
// A refused write puts its question back; a stale one (409) rereads and keeps the place; a write with no answer is unknown
// and rereads. Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way in.
(() => {
  const { html, put, plural, cells } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { caution: '▲', queued: '◌', unknown: '▨', ok: '●' };
  const rowId = note => `q${note}`;
  const noteOf = id => +String(id || '').replace(/^q/, '');

  // DOC is the last read (null when it failed); B the batches by item, Q the open questions in ledger order.
  // PENDING holds a label sent and not yet answered (note -> option); its row is off the ledger until the answer.
  let DOC = null, FAILED = '', READ = null, reads = 0, sel = null, batch = null, labelled = 0;
  let B = new Map(), Q = [];
  const PENDING = new Map(), SKIPPED = new Set();
  let chain = Promise.resolve();
  const queue = f => (chain = chain.catch(() => {}).then(f));

  const shown = () => Q.filter(q => !PENDING.has(q.note) && (batch === null || q.item === batch));
  const find = note => Q.find(q => q.note === note);
  const firstLine = t => String(t || '').split('\n')[0];
  const clock = d => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' });

  // ---------- Draw ----------
  function lamps() {
    const ul = $('annunciator'), list = DOC ? DOC.batches : [];
    ul.hidden = !list.length;
    ul.style.setProperty('--cells', String(Math.min(4, Math.max(1, list.length))));
    put(ul, cells(list.map(b => b.error
      ? { label: b.title, mark: html`<span class="g g-unknown" aria-hidden="true">${G.unknown}</span>`, state: 'unknown',
          val: 'not read', small: b.error, attrs: html` data-batch="${b.item}" title="${`sd:${b.item}`}"` }
      : { label: b.title, button: true, pressed: batch === b.item, state: b.open ? 'caution' : 'ok',
          mark: html`<span class="g" aria-hidden="true">${b.open ? G.caution : G.ok}</span>`,
          val: html`<b>${b.open}</b> of ${b.questions} open`, small: `${b.nightly ? 'nightly · ' : ''}${b.mode} · sd:${b.item}`,
          sr: `${b.title}: ${b.open} of ${b.questions} open`, attrs: html` data-batch="${b.item}"` })));
  }

  function progress() {
    const open = [...B.values()].reduce((n, b) => n + (b.open || 0), 0) - PENDING.size;
    $('sub').textContent = DOC
      ? `${labelled} labelled · ${Math.max(0, open)} open · ${SKIPPED.size} skipped · read ${clock(READ)}`
      : 'The review queue was not read';
    const list = shown();
    put($('tally'), batch === null ? html`${plural(list.length, 'question')}`
      : html`${plural(list.length, 'question')} in sd:${batch} · <button class="linkbtn" type="button" id="clear-batch">Show every batch</button>`);
    window.PAGE_ATTENTION = { state: open > 0 ? 'caution' : 'ok', n: Math.max(0, open), what: 'questions open' };
    window.shell?.attention?.();
  }

  function rows() {
    const tb = $('rows'), list = shown();
    if (!DOC) return put(tb, html`<tr class="empty"><td colspan="4"><span class="g-unknown" aria-hidden="true">${G.unknown}</span> Not read: ${FAILED}. Read again retries it.</td></tr>`);
    put(tb, list.length ? html`${list.map(q => { const b = B.get(q.item); return html`<tr data-id="${rowId(q.note)}" aria-selected="${String(sel === q.note)}">
      <td class="g g-caution" title="open">${G.caution}<span class="sr"> open</span></td>
      <td class="q">${firstLine(q.text)}${q.subject ? html`<span class="subject">${q.subject}</span>` : ''}</td>
      <td class="b">${b ? b.title : `sd:${q.item}`}${b && b.nightly ? html`<span class="tag">nightly</span>` : ''}</td>
      <td class="at"><time class="rel" datetime="${q.asked}"></time></td></tr>`; })}`
      : html`<tr class="empty"><td colspan="4">${batch === null ? 'No question is open. A batch filed with sd-db review file appears here.' : 'No question is open in this batch.'}</td></tr>`);
  }

  function details() {
    const d = $('details'), q = sel === null ? null : find(sel);
    if (!q || PENDING.has(q.note)) {
      return put(d, html`<p class="note">${DOC ? 'No question selected. Digits 1 to 9 label the selected question; j and k move without a label.' : 'The review queue was not read, so nothing is selected.'}</p>`);
    }
    const b = B.get(q.item) || {};
    const blind = b.mode === 'blind' || b.mode === 'sealed';
    const exportable = !blind || b.open === 0;
    put(d, html`<p class="kind"><span class="g-caution" aria-hidden="true">${G.caution}</span> Question · note #${q.note} · <a href="/tasks?row=${q.item}">sd:${q.item}</a></p>
      <h2 class="ask">${q.text}</h2>
      <div class="options" role="group" aria-label="${`Label note ${q.note}`}">${q.options.map((o, i) => html`<button type="button" class="btn quiet opt" data-i="${i}"><kbd>${i + 1}</kbd><span>${o}</span></button>`)}</div>
      ${blind ? html`<p class="why">A ${b.mode} batch: its subject and evidence stay hidden until it is exported.</p>` : ''}
      <dl>${q.subject ? html`<dt>Subject</dt><dd>${q.subject}</dd>` : ''}${(q.evidence || []).map(e => html`<dt>Evidence</dt><dd>${e}</dd>`)}
        <dt>Batch</dt><dd>${b.title || `sd:${q.item}`}</dd><dt>Queue</dt><dd>${b.queue || '—'} · ${b.mode || '—'}</dd>
        <dt>Source</dt><dd>${b.source || '—'}</dd><dt>Asked</dt><dd><time class="rel" datetime="${q.asked}"></time></dd></dl>
      <p class="note">Undo (u) withdraws a label while its toast shows. n adds a comment on this question to the batch.</p>
      <button class="btn quiet sm" type="button" id="export"${exportable ? '' : html` disabled`}>Download labels</button>
      ${exportable ? '' : html`<p class="why">A ${b.mode} batch downloads once every question is ruled.</p>`}`);
  }

  function draw() { lamps(); rows(); details(); progress(); }

  // ---------- Selection ----------
  function select(note, opened) {
    if (sel !== null && sel !== note && find(sel) && !PENDING.has(sel)) SKIPPED.add(sel);
    sel = note;
    const q = note === null ? null : find(note);
    $('rows').querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === rowId(note))));
    if (q) {
      window.shell.commands.put({ id: rowId(note), type: 'question', label: `note #${note}`, item: q.item });
      window.shell.commands.select(rowId(note));
      if (opened) window.shell.openPane('tab-details');
    } else window.shell.commands.select(null);
    details(); progress();
  }
  // The question after `note` in the ledger, else the one before it, else none.
  function neighbour(note) {
    const list = shown(), i = list.findIndex(q => q.note === note);
    const next = list[i + 1] || list[i - 1];
    return next ? next.note : null;
  }
  function writeUrl() {
    const p = new URLSearchParams();
    if (batch !== null) p.set('batch', batch);
    window.shell.url(p);
  }
  function filter(item) {
    batch = item;
    writeUrl(); draw();
    const list = shown();
    select(list.some(q => q.note === sel) ? sel : (list[0] ? list[0].note : null));
  }

  // ---------- Read ----------
  async function load() {
    const n = ++reads;
    if (!DOC) window.shell.state({ kind: 'loading', text: 'Reading the review queue. Questions appear when it answers.', source: '/api/review' });
    let doc;
    try { doc = await window.shell.getJSON('/api/review'); } catch (e) {
      if (n !== reads) return;
      // A failed read is unknown, never an empty queue: nothing from the last read stays on screen as current.
      DOC = null; FAILED = e.message; B = new Map(); Q = [];
      window.shell.state({ kind: 'error', text: `The review queue was not read: ${e.message}. Read again retries it.`, source: '/api/review' });
      draw(); return;
    }
    if (n !== reads) return;
    DOC = doc; READ = new Date(); FAILED = '';
    B = new Map(doc.batches.map(b => [b.item, b]));
    Q = doc.questions;
    if (batch !== null && !B.has(batch)) batch = null;
    window.shell.state(doc.batches.length ? null : { kind: 'empty', text: 'No batch is open. A batch filed with sd-db review file appears here.', source: '/api/review' });
    draw();
    const list = shown();
    select(list.some(q => q.note === sel) ? sel : (list[0] ? list[0].note : null));
  }

  // ---------- Write ----------
  function label(note, i) {
    const q = find(note);
    if (!q || PENDING.has(note) || !(i in q.options)) return;
    const option = q.options[i], next = neighbour(note);
    PENDING.set(note, option);
    SKIPPED.delete(note);
    sel = null; draw(); select(next);
    return queue(async () => {
      const b = B.get(q.item);
      try {
        const out = await window.shell.post('/api/review/rule', { item: q.item, note, option, revision: b ? b.revision : '' });
        PENDING.delete(note);
        if (b) { b.revision = out.revision; b.open -= 1; b.labels += 1; }
        Q = Q.filter(x => x.note !== note); labelled += 1;
        draw();
        window.shell.toast(`Labelled note #${note}: ${option}`, () => undo(q, out.ruling));
      } catch (e) {
        PENDING.delete(note);
        if (e.stale) { window.shell.toast(`${G.caution} The batch changed; reread. Note #${note} was not labelled.`); return load(); }
        if (!e.status) { window.shell.toast(`Not known whether note #${note} was labelled: ${e.message}. Reading the queue again.`); return load(); }
        draw();
        window.shell.toast(`Not labelled · note #${note}: ${e.message}`);
      }
    });
  }

  function undo(q, ruling) {
    return queue(async () => {
      const b = B.get(q.item);
      try {
        await window.shell.post('/api/review/withdraw', { item: q.item, ruling, revision: b ? b.revision : '' });
        labelled -= 1;
        await load();
        // The label moved the selection on by itself, so going back is no skip.
        sel = null; select(q.note);
        window.shell.toast(`Label withdrawn · note #${q.note} is open again`);
      } catch (e) {
        window.shell.toast(`Not undone · note #${q.note}: ${e.message}`);
        if (e.stale || !e.status) await load();
      }
    });
  }

  // n: the shell's capture. A note on a question is a comment on its batch, "On note #N: ...", under the batch revision.
  window.SHELL_CAPTURE = async ({ kind, title, item, about }) => {
    if (kind === 'note') {
      const note = noteOf(about && about.id), b = B.get(item);
      if (!note || !b) throw new Error('Not filed: select a question first.');
      const out = await queue(() => window.shell.post(`/api/items/${item}/notes`, { body: `On note #${note}: ${title}`, kind: 'comment', revision: b.revision }))
        .catch(e => { if (e.stale) load(); throw new Error(`Not filed: ${e.message}`); });
      b.revision = out.revision;
      return `Comment on note #${note} added to sd:${item}`;
    }
    const out = await window.shell.post('/api/items', { title, ...(kind === 'followup' ? { kind: 'followup', ...(item ? { followup_of: item } : {}) } : {}) })
      .catch(e => { throw new Error(`Not filed: ${e.message}`); });
    return `Captured #${out.item?.id}: ${out.item?.title || title}`;
  };

  // The batch's labels as the JSONL sd-db review export writes, saved by the browser; the key never leaves its file.
  async function download(item) {
    try {
      const out = await window.shell.getJSON(`/api/review/${item}/export`);
      const text = out.lines.map(line => JSON.stringify(line)).join('\n') + '\n';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([text], { type: 'application/x-ndjson' }));
      a.download = out.name; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      window.shell.toast(`Downloaded ${out.name} · ${plural(out.lines.length - 1, 'question')}`);
    } catch (e) { window.shell.toast(`Not downloaded: ${e.message}`); }
  }

  // ---------- Keys and the shell's list ----------
  window.PAGE_KEYS = [['1 – 9', 'Label the selected question with that option and open the next one'],
    ['j / k', 'Next / previous question, without a label (a skip)'], ['u', 'Undo the last label while its toast shows'],
    ['n', 'Comment on the selected question'], ['Esc', 'Show every batch again']];
  window.PAGE_LIST = { rows: () => $('rows').querySelectorAll('tr[data-id]'), select: id => select(noteOf(id), false),
    current: () => (sel === null ? null : rowId(sel)), clear: () => batch !== null && (filter(null), true) };

  document.addEventListener('keydown', e => {
    if (!/^[1-9]$/.test(e.key) || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target?.matches?.('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]')) return;
    if (window.shell?.chording?.() || sel === null) return;
    e.preventDefault();
    label(sel, +e.key - 1);
  });

  document.addEventListener('DOMContentLoaded', () => {
    const asked = new URLSearchParams(location.search).get('batch');
    if (/^[1-9][0-9]*$/.test(asked || '')) batch = +asked;
    $('rows').addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) select(noteOf(tr.dataset.id), true); });
    $('annunciator').addEventListener('click', e => { const c = e.target.closest('button[data-batch]'); if (c) filter(batch === +c.dataset.batch ? null : +c.dataset.batch); });
    $('details').addEventListener('click', e => {
      const o = e.target.closest('button[data-i]'); if (o && sel !== null) return void label(sel, +o.dataset.i);
      const x = e.target.closest('#export'); if (x && !x.disabled && sel !== null) return void download(find(sel).item);
    });
    $('tally').addEventListener('click', e => { if (e.target.closest('#clear-batch')) filter(null); });
    $('reload').addEventListener('click', () => load());
    document.addEventListener('shell:open', e => { const n = noteOf(e.detail); if (find(n)) select(n, true); });
    load();
  });
})();
