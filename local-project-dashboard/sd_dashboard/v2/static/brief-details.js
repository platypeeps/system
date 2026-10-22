// A brief's Details and commands (sd:5620): the one renderer for a row of /api/briefs's shape (briefs_screen.document).
// Briefs draws it for its selected row, and the HOA and Home pages' Briefs views (places.js) draw it in their Details pane,
// so a brief reads and acts the same on all three. It loads after commands.js and before the page script; a page calls
// attach() once the shell is up, and gets show(b) and object(b).
//   attach({ details, reread, read: () => iso, watch: () => text, more: b => markup })
//     details  the Details element; reread  the page reader's reread, after a publish write
//     read     when the rows were read (the age in Written); watch  why the watchdog is not read
//     more     optional markup under the brief: the place pages' link to Briefs
(() => {
  const { html, put } = window.markup;
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
  const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'], MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const dayLabel = d => { const t = new Date(d + 'T12:00:00Z'); return `${WD[t.getUTCDay()]} ${t.getUTCDate()} ${MON[t.getUTCMonth()]}`; };
  const ago = (read, iso) => { const m = Math.round((Date.parse(read) - Date.parse(iso)) / 60000); return m < 60 ? `${Math.max(m, 0)}m ago` : m < 2880 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`; };
  // The command object a brief row puts (shell.commands.put).
  const object = b => ({ id: b.id, type: 'brief', label: b.subj, src: b.src, item: b.item, open: b.open, copy: b.copy, pub: b.pub || null });
  const REVIEW_SRC = 'Adversarial review';

  function attach({ details, reread, read, watch, more = () => '' }) {
    const shell = window.shell, C = shell.commands;

    // ---------- Commands (products/system/commands.md): declared once, rendered by the shell ----------
    // build: Read, then two of the reference's four brief commands. Acknowledge and Mark read wrote a message store that does not
    // exist and had nothing to change here; Open in Gmail is Open in Obsidian, the vault copy's link, off without one (sd:3459).
    C.register(
      // build (sd:3459): every brief is a row in the database, so Read works with or without a vault copy.
      { id: 'brief.read', on: 'brief', label: 'Read', does: 'Opens the brief on its work item\'s page.', key: 'r', icon: 'book-open', risk: 'safe', primary: () => true, executes: false,
        cli: o => `sd reports brief show ${shq(o.id)}`, run: o => { location.href = shell.itemPage(o.item); return `#${o.item} opens`; } },
      // build: no verb deletes a task, so no Undo: the capture form shows the line and files it only on Capture.
      { id: 'brief.task', on: 'brief', label: 'Make task', does: 'Opens Capture with the brief\'s title, to file it as a task.', key: 'k', risk: 'safe',
        cli: o => `sd task add ${shq(o.label)}`, run: o => { shell.capture(o, o.label); return 'Check the title, then Capture'; } },
      { id: 'brief.open', on: 'brief', label: 'Open in Obsidian', does: 'Opens the brief\'s vault copy in Obsidian.', key: 'o', risk: 'safe', executes: false,
        when: o => !!o.open || o.copy || 'no vault copy for this brief', cli: o => `open ${shq(o.open)}`,
        run: o => { window.open(o.open, '_blank', 'noopener'); return 'Obsidian opens the copy'; } },
      // sd:5064: a codex review of the brief's item, in the background; its own brief lists the tasks to pick.
      window.commands.adversarialReview('brief', { payload: o => ({ kind: 'brief', target: o.item }) }),
      // sd:5102: Publish to (the item's fields.publish) and Publish now, declared once in commands.js.
      ...window.commands.publishCommands('brief', { base: '/api/briefs', payload: o => ({ item: o.item }), reread,
        cli: () => 'no sd verb: the dashboard writes the brief\'s fields.publish and queues it through sd-research-kit' }),
    );

    // ---------- Details ----------
    function show(b) {
      shownItem = b.item;
      const at = read();
      put(details, html`<div class="brief-det"><p class="kind">Brief · ${b.src}</p>
      <h2>${b.subj}</h2>
      <dl><dt>Source</dt><dd>${b.src}</dd><dt>Day</dt><dd>${dayLabel(b.day)}</dd>
        <dt>Written</dt><dd>${b.at ? `${b.at.slice(0, 16).replace('T', ' ')} UTC · ${ago(at, b.at)}` : 'time not known: the note changed after its day'}</dd>
        <dt>Words</dt><dd>${b.words}</dd><dt>Item</dt><dd>#${b.item}</dd>
        <dt>Vault copy</dt><dd>${b.rel ? html`<code>${b.rel}</code>` : b.copy}</dd>
        <dt>Watchdog</dt><dd><span class="g-unknown" aria-hidden="true">▨</span> ${watch()}</dd></dl>
      <h3>Body · first lines</h3><div class="mailbody">${b.lead}${b.lead.length >= 200 ? '…' : ''}</div>
      <p class="why off">First lines only. Read opens the whole brief.</p>
      <h3>Act</h3>${C.bar(b.id)}
      ${window.commands.publishPane('brief', { id: b.id, label: b.subj, item: b.item, pub: b.pub })}
      ${window.commands.reviewPane('brief', b.item)}
      ${b.src === REVIEW_SRC ? html`<section class="recs" id="recs" aria-live="polite"><h3>Create tasks</h3><p class="why off">Reading its recommendations…</p></section>` : ''}
      ${more(b)}</div>`);
      if (b.src === REVIEW_SRC) loadRecs(b.item);
      C.select(b.id);
      shell.suggest(['Summarise this brief in three lines', `What else did ${b.src} write this week?`, 'Turn what this brief asks into tasks']);
      details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap'));
    }

    // ---------- Create tasks (sd:5064) ----------
    // An adversarial review's brief names its review batches; each question is one recommendation, read from
    // /api/briefs/<item>/recommendations. Create selected rules each ticked question Approve through POST /api/review/rule,
    // one call at a time under its batch's revision, and Approve files the task. Reject the rest rules every open one
    // Reject. An unticked question stays open; a refused call keeps the server's sentence on its own row. Details names the
    // brief it shows in shownItem; an answer or a click for another brief changes nothing (shell.shown).
    let RECS = null, busy = false, shownItem = null;
    const CHOSEN = new Set(), REFUSED = new Map();
    function loadRecs(item) {
      const still = shell.shown('recs', item, () => shownItem); if (!still) return Promise.resolve();
      return shell.getJSON(`/api/briefs/${item}/recommendations`).then(doc => doc, e => ({ brief: item, error: e.message }))
        .then(doc => { if (!still()) return; if (RECS?.brief !== item) { CHOSEN.clear(); REFUSED.clear(); } RECS = doc; drawRecs(); });
    }
    function drawRecs() {
      const host = document.getElementById('recs'); if (!host || !RECS || RECS.brief !== shownItem) return;
      if (RECS.error) { put(host, html`<h3>Create tasks</h3><p class="why">Its recommendations were not read: ${RECS.error}</p>`); return; }
      const open = RECS.questions.filter(q => q.open);
      put(host, html`<h3>Create tasks</h3>
        <p class="why">Each recommendation waits in a review batch. Create selected approves the ticked ones, which files each as a task; the rest stay open.</p>
        ${RECS.batches.filter(b => b.error).map(b => html`<p class="why">sd:${b.item} was not read: ${b.error}</p>`)}
        ${RECS.questions.length ? html`<ul class="rec-list">${RECS.questions.map(q => html`<li>${q.open
          ? html`<label class="chk"><input type="checkbox" data-q="${q.note}"${CHOSEN.has(q.note) ? html` checked` : ''}${busy ? html` disabled` : ''}> <span>${q.title}</span></label>`
          : html`<span class="ruled"><span class="lab">${q.label || 'expired'}</span> <span>${q.title}</span>${q.filed ? html` · <a href="${shell.itemHref(q.filed)}">sd:${q.filed}</a>` : ''}</span>`}
          <small>${q.repo || 'no repository'} · batch sd:${q.batch}</small>${REFUSED.has(q.note) ? html`<small class="refused">${REFUSED.get(q.note)}</small>` : ''}</li>`)}</ul>`
          : html`<p class="why off">This review filed no recommendation.</p>`}
        ${open.length ? html`<div class="actions"><button class="linkbtn" type="button" data-recs="all">Select all</button>
          <button class="btn sm" type="button" data-recs="create"${busy ? html` disabled` : ''}>Create selected</button>
          <button class="btn sm quiet" type="button" data-recs="reject"${busy ? html` disabled` : ''}>Reject the rest</button></div>` : ''}`);
    }
    async function ruleEach(option, picked) {
      const doc = RECS, revision = new Map(doc.batches.map(b => [b.item, b.revision]));
      if (!picked.length) { shell.toast('Tick a recommendation first'); return; }
      busy = true; REFUSED.clear(); drawRecs();
      let failed = 0;
      for (const q of picked) {
        try {
          const out = await shell.post('/api/review/rule', { item: q.batch, note: q.note, option, revision: revision.get(q.batch) });
          revision.set(q.batch, out.revision); CHOSEN.delete(q.note);
        } catch (e) { failed++; REFUSED.set(q.note, e.message); }
      }
      busy = false; drawRecs();
      shell.toast(`${picked.length - failed} of ${picked.length} ruled ${option}${failed ? `; ${failed} failed, each row says why` : ''}`);
      await loadRecs(doc.brief);
    }
    details.addEventListener('change', e => {
      const box = e.target.closest?.('input[data-q]'); if (!box) return;
      if (box.checked) CHOSEN.add(+box.dataset.q); else CHOSEN.delete(+box.dataset.q);
    });
    details.addEventListener('click', e => {
      const b = e.target.closest?.('[data-recs]'); if (!b || busy || !RECS?.questions || RECS.brief !== shownItem) return;
      const open = RECS.questions.filter(q => q.open);
      if (b.dataset.recs === 'all') { open.forEach(q => CHOSEN.add(q.note)); drawRecs(); }
      if (b.dataset.recs === 'create') ruleEach('Approve', open.filter(q => CHOSEN.has(q.note)));
      if (b.dataset.recs === 'reject') ruleEach('Reject', open.filter(q => !CHOSEN.has(q.note)));
    });
    // forget(): Details shows something other than a brief, so a late recommendations answer draws nothing.
    return { show, forget: () => { shownItem = null; } };
  }

  window.briefDetails = { attach, object, dayLabel };
})();
