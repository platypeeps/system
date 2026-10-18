// Review (sd:3709): the labelling queue. It loads before shell.js, which reads PAGE_LIST and PAGE_KEYS from it.
// Rows: /api/review (review_screen.document): one lamp per open batch, one ledger row per question, nightly suggestions
// first. A row carries its question (sd:5123): the first line and its subject and evidence, the options as numbered buttons,
// and the whole question folded in place. No Details pane: the shell's pane opens only for Chat. One click on an option
// labels that question; its row hides at once and the next open question takes the selection. Show reviewed, in the tally,
// brings back the ruled and expired rows of the open batches, each with its choice and a Withdraw for a standing label.
// Keys (products/system/design.md, Keys): 1-9 label the selected question and open the next open one; j / k move without
// a label (a skip); Enter opens or folds the selected question; u is the shell's Undo while the label's toast shows (10 s);
// n is the shell's capture, which files a comment "On note #N: ..." on the batch; Esc clears the batch filter and Show
// reviewed; ? lists the keys. No other digit is bound.
// Writes run one at a time, each with the batch revision the last write returned, so ten quick digits are ten labels.
// A refused write puts its question back; a stale one (409) rereads and keeps the place; a write with no answer is unknown
// and rereads. Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way in.
(() => {
  const { html, put, plural, cells } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { caution: '▲', queued: '◌', unknown: '▨', ok: '●' };
  const rowId = note => `q${note}`;
  const noteOf = id => +String(id || '').replace(/^q/, '');

  // DOC is the last read (null when it failed); B the batches by item, Q every question in ledger order, open or reviewed.
  // PENDING holds a label sent and not yet answered (note -> option); its row reads as reviewed until the answer.
  // REVIEWED is Show reviewed (?reviewed=1); OPENED the questions unfolded in place.
  let DOC = null, FAILED = '', READ = null, sel = null, batch = null, labelled = 0, REVIEWED = false;
  let B = new Map(), Q = [];
  const PENDING = new Map(), SKIPPED = new Set(), OPENED = new Set();
  let chain = Promise.resolve();
  const queue = f => (chain = chain.catch(() => {}).then(f));

  const isOpen = q => q.open !== false && !PENDING.has(q.note);
  const inBatch = q => batch === null || q.item === batch;
  const shown = () => Q.filter(q => inBatch(q) && (REVIEWED || isOpen(q)));
  const find = note => Q.find(q => q.note === note);
  const firstLine = t => String(t || '').split('\n')[0];
  const clock = d => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' });

  // ---------- Draw ----------
  function lamps() {
    const ul = $('annunciator'), list = DOC ? DOC.batches : [];
    ul.hidden = !list.length;
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
    const list = shown().filter(isOpen), hidden = Q.filter(q => inBatch(q) && !isOpen(q)).length;
    // Show reviewed sits in the tally beside the batch filter: the page's one filter line, kept in the address.
    const toggle = html` · <button class="btn quiet sm" type="button" id="show-reviewed" aria-pressed="${String(REVIEWED)}">Show reviewed <span class="n">${hidden}</span></button>`;
    put($('tally'), html`${batch === null ? html`${plural(list.length, 'question')} open` : header(list.length)}${toggle}`);
    // The rail's rule is the read's (sd:3446, review_screen.document); this page keeps it current as its labels land.
    window.PAGE_ATTENTION = { state: open > 0 ? 'caution' : 'ok', n: Math.max(0, open), what: 'questions open' };
    window.shell?.attention?.();
  }

  // The batch header, open while a lamp filters the ledger: the batch's own actions, none waiting on a selected question.
  // Download is on whenever the batch exports: an open batch at once, a blind or sealed one once nothing is open.
  function header(count) {
    const b = B.get(batch) || {}, blind = b.mode === 'blind' || b.mode === 'sealed';
    const exportable = !b.error && b.mode && (!blind || b.open === 0);
    return html`${plural(count, 'question')} open in <a href="${window.shell.itemHref(batch)}">sd:${batch}</a> · <button class="linkbtn" type="button" id="clear-batch">Show every batch</button>
      · <button class="btn quiet sm" type="button" id="export"${exportable ? '' : html` disabled`}>Download labels</button>${exportable ? ''
      : html` <span class="why">${b.error ? 'Not read, so it does not download.' : `A ${b.mode} batch downloads once every question is ruled.`}</span>`}`;
  }

  // A Kev file's proposal is an evidence line (sd:4789); its digit is the accept. A blind batch shows none.
  const proposal = (q, blind) => blind ? null : ((q.evidence || []).find(e => e.startsWith('Proposed label: ')) || '').slice(16) || null;

  // One row per question: the first line and one line of context (subject, evidence) under it, then on one line the options
  // or the choice made and the whole question, folded in place (Enter, or its summary, opens it).
  function row(q) {
    const b = B.get(q.item) || {}, blind = b.mode === 'blind' || b.mode === 'sealed', open = isOpen(q);
    const proposed = proposal(q, blind);
    const context = (q.evidence || []).filter(e => !e.startsWith('Proposed label: '));
    const label = PENDING.get(q.note) || q.label;
    const state = open ? html`<td class="g g-caution" title="open">${G.caution}<span class="sr"> open</span></td>`
      : label ? html`<td class="g g-ok" title="labelled">${G.ok}<span class="sr"> labelled</span></td>`
      : html`<td class="g g-queued" title="expired">${G.queued}<span class="sr"> expired</span></td>`;
    const choose = open
      ? html`<div class="opts" role="group" aria-label="${`Label note ${q.note}`}">${q.options.map((o, i) => html`<button type="button" class="btn quiet sm opt" data-note="${q.note}" data-i="${i}" aria-keyshortcuts="${i + 1}"><kbd>${i + 1}</kbd><span>${o}</span>${o === proposed ? html`<span class="tag">proposed</span>` : ''}</button>`)}</div>`
      : html`<p class="chose">${label ? html`${PENDING.has(q.note) ? 'Labelling' : 'Labelled'} <b>${label}</b>` : 'Expired with no label'}${!PENDING.has(q.note) && q.ruling
        ? html` · <button class="linkbtn" type="button" data-withdraw="${q.note}">Withdraw label</button>` : ''}</p>`;
    return html`<tr data-id="${rowId(q.note)}"${open ? '' : html` data-reviewed=""`} aria-selected="${String(sel === q.note)}">
      ${state}
      <td class="what"><p class="ask">${firstLine(q.text)}</p>${q.subject || context.length ? html`<p class="ctx">${q.subject ? html`<span class="subject">${q.subject}</span>` : ''}${context.map((e, i) => html`${i || q.subject ? ' · ' : ''}${e}`)}</p>` : ''}
        <div class="pick">${choose}
        <details class="more"${OPENED.has(q.note) ? html` open` : ''}><summary>Whole question</summary><p class="full">${q.text}</p>${blind
          ? html`<p class="why">A ${b.mode} batch: its subject and evidence stay hidden until it is exported.</p>` : ''}<dl>${q.subject ? html`<dt>Subject</dt><dd>${q.subject}</dd>` : ''}${(q.evidence || []).map(e => html`<dt>Evidence</dt><dd>${e}</dd>`)}
          <dt>Note</dt><dd>#${q.note} · <a href="${window.shell.itemHref(q.item)}">sd:${q.item}</a></dd><dt>Queue</dt><dd>${b.queue || '—'} · ${b.mode || '—'}</dd><dt>Source</dt><dd>${b.source || '—'}</dd></dl></details></div></td>
      <td class="b">${b.title || `sd:${q.item}`}${b.nightly ? html`<span class="tag">nightly</span>` : ''}</td>
      <td class="at"><time class="rel" datetime="${q.asked}"></time></td></tr>`;
  }

  // A redraw replaces the buttons: focus on one comes back to the same button, else to the selected row's first option.
  function refocus(tb, was) {
    if (!was) return;
    const d = was.dataset || {}, same = d.i !== undefined ? `button[data-note="${d.note}"][data-i="${d.i}"]`
      : d.withdraw ? `button[data-withdraw="${d.withdraw}"]` : null;
    (same && tb.querySelector(same) || tb.querySelector(`tr[data-id="${rowId(sel)}"] .opt`))?.focus({ preventScroll: true });
  }

  function rows() {
    const tb = $('rows'), list = shown(), was = tb.contains?.(document.activeElement) ? document.activeElement : null;
    if (!DOC) return put(tb, html`<tr class="empty"><td colspan="4"><span class="g-unknown" aria-hidden="true">${G.unknown}</span> Not read: ${FAILED}. Read again retries it.</td></tr>`);
    const hidden = REVIEWED ? 0 : Q.filter(q => inBatch(q) && !isOpen(q)).length;
    put(tb, html`${list.map(row)}${list.some(isOpen) ? '' : html`<tr class="empty"><td colspan="4">${batch === null ? 'No question is open. A batch filed with sd-db review file appears here.' : 'No question is open in this batch.'}${hidden
      ? ` ${plural(hidden, 'reviewed question')} hidden: Show reviewed lists them.` : ''}</td></tr>`}`);
    refocus(tb, was);
  }

  function draw() { lamps(); rows(); progress(); }

  // ---------- Selection ----------
  function select(note) {
    if (sel !== null && sel !== note && find(sel) && isOpen(find(sel))) SKIPPED.add(sel);
    sel = note;
    const q = note === null ? null : find(note);
    $('rows').querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === rowId(note))));
    if (q) {
      window.shell.commands.put({ id: rowId(note), type: 'question', label: `note #${note}`, item: q.item });
      window.shell.commands.select(rowId(note));
    } else window.shell.commands.select(null);
    // Focus in the ledger follows the selection (j / k, Undo), so a digit and the focused button name the same question.
    const tb = $('rows'), tr = q && tb.querySelector(`tr[data-id="${rowId(note)}"]`);
    if (tr && tb.contains?.(document.activeElement) && !tr.contains(document.activeElement)) tr.querySelector('.opt, [data-withdraw], summary')?.focus({ preventScroll: true });
    progress();
  }
  // The open question after `note` in the ledger, else the one before it, else none.
  function neighbour(note) {
    const list = shown().filter(q => q.note === note || isOpen(q)), i = list.findIndex(q => q.note === note);
    const next = list[i + 1] || list[i - 1];
    return next ? next.note : null;
  }
  // The selection keeps the next row in view; a label sent from a row's button hands focus to the next row's first option.
  function reveal(note, focus) {
    if (note === null) return;
    const tb = $('rows'), tr = tb.querySelector(`tr[data-id="${rowId(note)}"]`);
    if (focus) tb.querySelector(`tr[data-id="${rowId(note)}"] .opt`)?.focus({ preventScroll: true });
    tr?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function writeUrl() {
    const p = new URLSearchParams();
    if (batch !== null) p.set('batch', batch);
    if (REVIEWED) p.set('reviewed', 1);
    window.shell.url(p);
  }
  function first() {
    const list = shown(), open = list.filter(isOpen);
    select(list.some(q => q.note === sel) ? sel : (open[0] || list[0] || { note: null }).note);
  }
  function filter(item, reviewed = REVIEWED) {
    batch = item; REVIEWED = reviewed;
    writeUrl(); draw(); first();
  }

  // ---------- Read ----------
  async function load() {
    // The shell's stale-response guard (shell.shown): only the newest read applies its answer.
    const still = window.shell.shown('review', 'queue', () => 'queue');
    if (!DOC) window.shell.state({ kind: 'loading', text: 'Reading the review queue. Questions appear when it answers.', source: '/api/review' });
    let doc;
    try { doc = await window.shell.getJSON('/api/review'); } catch (e) {
      if (!still()) return;
      // A failed read is unknown, never an empty queue: nothing from the last read stays on screen as current.
      DOC = null; FAILED = e.message; B = new Map(); Q = [];
      window.shell.state({ kind: 'error', text: `The review queue was not read: ${e.message}. Read again retries it.`, source: '/api/review' });
      draw(); return;
    }
    if (!still()) return;
    DOC = doc; READ = new Date(); FAILED = '';
    B = new Map(doc.batches.map(b => [b.item, b]));
    // The open questions and, for Show reviewed, the ruled and expired ones (`reviewed`), in one ledger order.
    const at = new Map(doc.batches.map((b, i) => [b.item, i]));
    Q = doc.questions.concat(doc.reviewed || []).sort((x, y) => at.get(x.item) - at.get(y.item) || x.note - y.note);
    if (batch !== null && !B.has(batch)) batch = null;
    window.shell.state(doc.batches.length ? null : { kind: 'empty', text: 'No batch is open. A batch filed with sd-db review file appears here.', source: '/api/review' });
    draw(); first();
  }

  // ---------- Write ----------
  function label(note, i, focus) {
    const q = find(note);
    if (!q || !isOpen(q) || !(i in q.options)) return;
    const option = q.options[i], next = neighbour(note);
    PENDING.set(note, option);
    SKIPPED.delete(note);
    sel = null; draw(); select(next); reveal(next, focus);
    return queue(async () => {
      const b = B.get(q.item);
      try {
        const out = await window.shell.post('/api/review/rule', { item: q.item, note, option, revision: b ? b.revision : '' });
        PENDING.delete(note);
        if (b) { b.revision = out.revision; b.open -= 1; b.labels += 1; }
        Object.assign(q, { open: false, label: option, ruling: out.ruling }); labelled += 1;
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

  // Undo and a reviewed row's Withdraw label: the withdrawal reopens the question, which takes the selection.
  function undo(q, ruling) {
    return queue(async () => {
      const b = B.get(q.item);
      try {
        await window.shell.post('/api/review/withdraw', { item: q.item, ruling, revision: b ? b.revision : '' });
        if (labelled > 0) labelled -= 1;
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
  window.SHELL_CAPTURE = async ({ kind, title, item, priority, repo, about }) => {
    if (kind === 'note') {
      const note = noteOf(about && about.id), b = B.get(item);
      if (!note || !b) throw new Error('Not filed: select a question first.');
      const out = await queue(() => window.shell.post(`/api/items/${item}/notes`, { body: `On note #${note}: ${title}`, kind: 'comment', revision: b.revision }))
        .catch(e => { if (e.stale) load(); throw new Error(`Not filed: ${e.message}`); });
      b.revision = out.revision;
      return `Comment on note #${note} added to sd:${item}`;
    }
    // A task or a followup files as on every page, with the box's priority and repo (shell.js fileCapture, sd:3521).
    return window.shell.fileCapture({ kind, title, item, priority, repo });
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
    ['j / k', 'Next / previous question, without a label (a skip)'], ['↵', 'Open or fold the whole question in its row'],
    ['u', 'Undo the last label while its toast shows'], ['n', 'Comment on the selected question'],
    ['Esc', 'Show every batch again and hide reviewed questions']];
  window.PAGE_LIST = { rows: () => $('rows').querySelectorAll('tr[data-id]'), select: id => select(noteOf(id)),
    current: () => (sel === null ? null : rowId(sel)), clear: () => (batch !== null || REVIEWED) && (filter(null, false), true) };

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
    REVIEWED = new URLSearchParams(location.search).get('reviewed') === '1';
    // One click on an option labels its row's question; Withdraw label pulls a reviewed row's standing label; a click
    // elsewhere in a row selects it, and its summary records the fold so a redraw keeps it.
    $('rows').addEventListener('click', e => {
      const o = e.target.closest('button[data-i]');
      if (o) return void label(+o.dataset.note, +o.dataset.i, true);
      const w = e.target.closest('button[data-withdraw]'), q = w && find(+w.dataset.withdraw);
      if (q) { if (q.ruling && !PENDING.has(q.note)) undo(q, q.ruling); return; }
      const s = e.target.closest('summary'), tr = e.target.closest('tr[data-id]');
      if (!tr) return;
      const note = noteOf(tr.dataset.id);
      if (s) OPENED.has(note) ? OPENED.delete(note) : OPENED.add(note);
      if (note !== sel) select(note);
    });
    $('annunciator').addEventListener('click', e => { const c = e.target.closest('button[data-batch]'); if (c) filter(batch === +c.dataset.batch ? null : +c.dataset.batch); });
    $('tally').addEventListener('click', e => {
      if (e.target.closest('#show-reviewed')) return void filter(batch, !REVIEWED);
      if (e.target.closest('#clear-batch')) return void filter(null);
      const x = e.target.closest('#export'); if (x && !x.disabled && batch !== null) download(batch);
    });
    $('reload').addEventListener('click', () => load());
    // Enter on the selected row: open or fold its whole question in place.
    document.addEventListener('shell:open', e => {
      const n = noteOf(e.detail); if (!find(n)) return;
      OPENED.has(n) ? OPENED.delete(n) : OPENED.add(n);
      rows(); select(n);
    });
    load();
  });
})();
