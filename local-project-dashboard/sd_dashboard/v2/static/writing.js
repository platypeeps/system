// Writing (sd:2125). Built from the design source's products/system/designs/pages/writing.js at 2729265.
// build: pieces come from /api/writing through shell.read; Stage, Correct, Park and Revive post the routes the old item page
// posts. The vault ideas column, the draft editor's save and the evidence summaries have no reader or writer yet (see below).
// Page commands for the palette; the shell reads this when it loads.
const { html, put } = window.markup;
window.PAGE_COMMANDS = [
  { label: 'Capture an idea or find a piece', icon: 'lightbulb', key: '/', run: () => document.getElementById('shift-in').focus() },
  { label: 'Board view', icon: 'kanban', key: 'v b', run: () => window.writingView?.('board') },
  { label: 'Editor view for the selected piece', icon: 'pen-line', key: 'v e', run: () => window.writingView?.('editor') },
  { label: 'Show or hide parked pieces', icon: 'archive', run: () => document.getElementById('show-parked').click() },
];
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'pieces not read' };

addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest, openPane } = window.shell;
  const C = window.shell.commands;
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
  const csrf = () => document.querySelector('meta[name="sd-csrf"]')?.content || '';
  async function post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-SD-CSRF': csrf() }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
    return out;
  }
  const utc = t => t ? String(t).slice(0, 16).replace('T', ' ') + ' UTC' : 'not recorded';
  const ago = s => { const n = Math.round(((Date.parse(D?.read) || Date.now()) - new Date(s)) / 86400000); return n <= 0 ? 'today' : `${n}d ago`; };

  // Stages: sd_db.writing.STAGES minus inbox and declined (no piece holds them); the vault inbox leads as "Ideas".
  const STAGES = [['accepted', 'Accepted'], ['researching', 'Researching'], ['drafting', 'Drafting'], ['review', 'Review'], ['ready', 'Ready'], ['published', 'Published']];
  const SL = Object.fromEntries(STAGES);
  let D = null, pieces = [];
  const byId = id => pieces.find(p => p.id === +id);

  // ---------- Gate lamps from sd_db.writing._problems ----------
  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  function gates(p) {
    const has = s => p.problems.filter(x => x.includes(s));
    const L = [];
    const file = n => p.files[n];
    // Draft
    L.push(has('draft is missing').length ? ['D', 'Draft', 'queued', 'Not written yet (index.md has no draft).'] : ['D', 'Draft', 'ok', `${p.words.toLocaleString()} words`]);
    // Research
    const r = has('research.md');
    if (!r.length) L.push(['R', 'Research', 'ok', 'Stamped against this draft.']);
    else L.push(['R', 'Research', file('research.md') ? 'caution' : 'queued', file('research.md') ? 'research.md is unstamped or stale against this draft.' : 'research.md is missing.']);
    for (const [k, name, fn] of [['F', 'Fact-check', 'fact-check'], ['A', 'Adversarial', 'adversarial']]) {
      const pr = has(fn);
      if (!pr.length) { L.push([k, name, 'ok', 'Verdict pass, recorded against this draft.']); continue; }
      if (!file(fn + '.md')) { L.push([k, name, 'queued', `${fn}.md is missing.`]); continue; }
      if (pr.some(x => x.includes('verdict is not pass'))) { L.push([k, name, 'warning', `Verdict is not pass; ${pr.filter(x => x.includes('unresolved')).length} findings unresolved.`]); continue; }
      const open = pr.filter(x => x.includes('unresolved'));
      if (open.length) { L.push([k, name, 'caution', `${markup.plural(open.length, 'finding')} unresolved: ${open.map(x => x.match(/finding (\S+)/)[1]).join(', ')}.`]); continue; }
      if (pr.some(x => x.includes('no explicit current gate record'))) { L.push([k, name, 'unknown', `${fn}.md exists, but no gate verdict is recorded for this draft.`]); continue; }
      L.push([k, name, 'caution', pr.join('; ')]);
    }
    return L;
  }
  const worst = L => L.some(l => l[2] === 'warning') ? 'warning' : L.some(l => l[2] === 'caution') ? 'caution' : '';
  const lamps = p => html`<span class="lamps" role="img" aria-label="${gates(p).map(([, n, s]) => `${n} ${s}`).join(', ')}">${gates(p).map(([k, , s]) => html`<span class="lamp" data-s="${s}"><span class="g" aria-hidden="true">${GLYPH[s]}</span>${k}</span>`)}</span>`;

  // ---------- Stage rules: sd_db.writing._normal and change_stage ----------
  // build: the dashboard's stage and park routes require row ownership (require_row), so a file-owned piece moves nowhere here.
  const owned = p => p.owner === 'row' || (p.owner === 'retiring' ? 'writing cutover is in progress' : 'the piece files own this piece; the dashboard moves it after the writing cutover');
  function legal(p, to) {
    if (p.stage === to) return { ok: false, same: true, reason: 'Already here.' };
    if (p.parked) return { ok: false, reason: 'Parked. Revive it first; a parked piece keeps its stage.' };
    if (owned(p) !== true) return { ok: false, reason: `${owned(p)[0].toUpperCase()}${owned(p).slice(1)}.` };
    if (to === 'ideas') return { ok: false, reason: 'A piece does not return to the vault inbox. Park it, or correct it to an earlier stage.' };
    if (to === 'published') return { ok: false, reason: 'Publishing runs through Publish after the ready gate, with a recorded URL.' };
    if (p.next.includes(to)) {
      if (to === 'ready' && !p.gates_ok) return { ok: false, reason: `Ready gate refused: ${markup.plural(p.problems.length, 'problem')} (${p.problems[0]}${p.problems.length > 1 ? ' …' : ''}).` };
      return { ok: true, fwd: true };
    }
    if (p.corrections.includes(to)) return { ok: true, correct: true };
    return { ok: false, reason: `From ${SL[p.stage] || p.stage} a piece moves to ${p.next.map(s => SL[s]).join(' or ') || 'nothing'}; a correction goes back to ${p.corrections.map(s => SL[s]).join(', ') || 'nothing'}.` };
  }
  function publishState(p) {
    const why = [];
    if (p.parked) why.push('The piece is parked.');
    if (p.publication) why.push(`Already published (claim ${String(p.publication.claim).slice(0, 8)}).`);
    else if (p.stage !== 'ready') why.push(`Stage is ${SL[p.stage] || p.stage}; publishing needs Ready.`);
    if (!p.gates_ok) why.push(`The ready gate fails: ${markup.plural(p.problems.length, 'problem')}.`);
    if (p.stage === 'ready' && p.gates_ok && !p.ready_recorded && !p.publication) why.push('No readiness decision is recorded for this draft. Stage it to Review and back to Ready; with gates passing, that records the digest publishing checks.');
    return { ok: !why.length, why };
  }

  // ---------- Strip ----------
  // Rail badge (shell.js): the loudest gate state across active pieces.
  const pageAttention = (xs, what) => { const w = xs.filter(s => s === 'warning').length, c = xs.filter(s => s === 'caution').length; window.PAGE_ATTENTION = { state: w ? 'warning' : c ? 'caution' : 'ok', n: w || c, what: what[w ? 'warning' : 'caution'] }; window.shell?.attention?.(window.PAGE_ATTENTION); };
  function renderStrip() {
    if (!D) { window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'pieces not read' }; window.shell?.attention?.(window.PAGE_ATTENTION); put(document.getElementById('strip'), html``); return; }
    const act = pieces.filter(p => !p.parked);
    pageAttention(act.map(p => worst(gates(p))), { warning: 'pieces fail a gate', caution: 'pieces need a gate' });
    const bad = act.filter(p => !p.gates_ok).length;
    const pub = act.filter(p => publishState(p).ok).length;
    put(document.getElementById('strip'), html`<span class="fact"><b>${act.length}</b> active</span>
       <span class="fact"><span class="g-caution" aria-hidden="true">▲</span><b>${bad}</b> gates want you</span>
       <span class="fact"><b>${act.filter(p => p.stage === 'ready').length}</b> at Ready, <b>${pub}</b> publishable</span>
       <span class="fact"><span class="g-unknown" aria-hidden="true">▨</span>vault ideas not read</span>
       <button class="help" type="button" aria-label="Help: lamps" data-help="<b>D R F A</b>: draft, research, fact-check, adversarial, in that order on every card. <b>●</b> passes against this draft, <b>▲</b> stale or findings open, <b>■</b> verdict not pass, <b>◌</b> not written yet, <b>▨</b> file exists but no gate verdict is recorded. Source: <code>sd_db.writing._problems</code>.">${ICON('circle-help')}</button>
       <span class="src">read ${String(D.read).slice(11, 16)} UTC</span>`);
    document.getElementById('parked-n').textContent = pieces.filter(p => p.parked).length;
  }

  // ---------- Board ----------
  // selected is the piece the editor and the URL follow.
  let selected = null, showParked = false, view = 'board';
  function card(p) {
    const L = gates(p), st = worst(L);
    return html`<article class="card${p.parked ? ' parked' : ''}" tabindex="0" role="option" data-id="${p.id}" aria-selected="${String(p.id === selected)}"${st ? html` data-state="${st}"` : ''} aria-label="#${p.id} ${p.title}${p.parked ? ', parked' : ''}">
      <div class="top"><span class="id">#${p.id}</span>${p.parked ? html`<span class="tag">parked</span>` : ''}${p.publication ? html`<span class="tag">published</span>` : ''}${lamps(p)}</div>
      <div class="t">${p.title}</div>
      <div class="meta"><span>${p.piece.split('/').pop()}</span><span>${p.words ? p.words.toLocaleString() + ' w' : 'no draft'}</span><span title="${p.updated}">${ago(p.updated)}</span></div>
      <div class="act">${C.rowActions(String(p.id))}</div>
    </article>`;
  }
  function renderBoard() {
    const v = pieces.filter(p => showParked || !p.parked);
    // build: no reader for the vault's blog ideas exists yet (the design read `sd store list sdw.blog-idea --json`), so the
    // column says so and keeps its drop zone, which refuses a piece as the design does.
    const ideaCol = html`<div class="col vault" data-drop="stage:ideas">
      <header><span class="label">Ideas · vault</span><span class="n" title="vault ideas are not read here">—</span></header>
      <div><p class="refusal" role="note"></p><div class="drop">
        <p class="empty">Not read: the dashboard has no reader for the vault's blog ideas yet. <code>sd store list sdw.blog-idea --json</code> lists them.</p>
      </div></div></div>`;
    put(document.getElementById('view-board'), html`<div class="board" id="board" role="listbox" aria-label="Pieces by stage">${ideaCol}${STAGES.map(([s, name]) => {
      const rows = v.filter(p => p.stage === s);
      return html`<div class="col" data-drop="stage:${s}">
        <header><span class="label">${name}</span><span class="n">${rows.length}</span></header>
        <div><p class="refusal" role="note"></p><div class="drop">${rows.length ? rows.map(card) : html`<p class="empty">Nothing ${name.toLowerCase()}.</p>`}</div></div>
      </div>`;
    })}</div>`);
  }

  // ---------- Details ----------
  function renderDetails() {
    const p = byId(selected), el = document.getElementById('details');
    if (!p) { put(el, html`<p class="note">${D ? 'Select a piece to see its gates and moves.' : 'The pieces were not read. Reload retries it.'}</p>`); return; }
    const L = gates(p), P = publishState(p);
    put(el, html`<div class="kind"><span class="label">Piece</span><span class="tag">observed</span></div>
      <h2>${p.title}</h2>
      <dl>
        <dt>Id</dt><dd>#${p.id}</dd>
        <dt>Piece</dt><dd>${p.piece}</dd>
        <dt>Stage</dt><dd>${SL[p.stage] || p.stage}${p.parked ? ' · parked' : ''}</dd>
        <dt>Draft</dt><dd>${p.words ? p.words.toLocaleString() + ' words' : 'none'}</dd>
        <dt>Updated</dt><dd>${utc(p.updated)}</dd>
        <dt>Readiness</dt><dd>${p.ready_recorded ? 'recorded for this draft' : 'not recorded'}</dd>
      </dl>
      <h3 class="label">Gates</h3>
      <ul class="gates">${L.map(([, n, s, w]) => html`<li><span class="g g-${s}" aria-hidden="true">${GLYPH[s]}</span><span>${n}</span><span class="what">${w}</span></li>`)}</ul>
      <div class="publish">
        <span class="label">Publish gate</span>
        ${P.ok ? html`<p class="why">Open: stage Ready, gates pass, readiness recorded for this draft. Publish is copy only: <code>sd writing publication-claim</code>, then <code>publication-dispatch</code> on the recorded claim.</p>` : html`<p class="why">Closed. Publish stays off until each of these clears:</p><ul>${P.why.map(w => html`<li>${w}</li>`)}</ul>`}
      </div>
      <h3>Act</h3>${C.bar(String(p.id))}
      <div class="actions"><button class="btn quiet sm" type="button" id="open-ed">${ICON('pen-line')}Open in editor</button></div>
      <p class="foot">Observed ${utc(D.read)} from the workflow database and the piece files in <code>${p.repo}/${p.folder}/${p.piece}/</code>.</p>`);
    el.querySelector('#open-ed')?.addEventListener('click', () => setView('editor'));
  }

  // ---------- Editor ----------
  // build: read only. No route writes a draft, so the design's Edit button and its save state are not drawn, and the
  // evidence tabs show each file's facts and its lamp: no reader summarises research.md, fact-check.md or adversarial.md yet.
  let evTab = 'research';
  function renderEditor() {
    const p = byId(selected), el = document.getElementById('view-editor');
    if (!p) { put(el, html`<p class="note">${D ? 'Select a piece on the board first.' : 'The pieces were not read. Reload retries it.'}</p>`); return; }
    const P = publishState(p), fx = p.files;
    const fm = n => fx[n] ? `${(fx[n].bytes / 1024).toFixed(1)} KB · ${utc(fx[n].mtime)}` : 'missing';
    const g = gates(p);
    const ev = { research: ['research.md', 1], factcheck: ['fact-check.md', 2], adversarial: ['adversarial.md', 3] };
    const [file, at] = ev[evTab];
    put(el, html`<div class="ed-head">
        <button class="btn quiet sm" type="button" id="back">${ICON('chevron-left')}Board</button>
        <h2>#${p.id} ${p.title}</h2>${lamps(p)}
        <span class="save" id="save" data-s="saved" role="status">${ICON('check')}Read only · the file on disk</span>
        <button class="btn sm" type="button" data-cmd="piece.publish" data-obj="${p.id}"${P.ok ? '' : html` disabled aria-describedby="ed-why"`}>${ICON('send')}Publish</button>
      </div>
      ${P.ok ? '' : html`<p class="why ed-why" id="ed-why">Publish is off: ${P.why[0]}${P.why.length > 1 ? ` (+${P.why.length - 1} more in Details)` : ''}</p>`}
      <div class="editor">
        <div class="draft">
          <p class="path">${p.repo}/${p.folder}/${p.piece}/index.md · ## Draft</p>
          <article class="page" aria-label="Draft">${p.draft.length ? html`<div id="draft-text" role="textbox" aria-multiline="true" aria-label="Draft text, #${p.id}" aria-readonly="true" tabindex="0">${p.draft.map(x => html`<p>${x.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')}</p>`)}</div><p class="more">First ${markup.plural(p.draft.length, 'paragraph')} of ${p.words.toLocaleString()} words. Edit the draft in its file.</p>` : html`<p class="more">No draft yet. <code>sdw-research</code> comes first, then <code>sdw-draft</code>.</p>`}</article>
        </div>
        <aside class="evidence" aria-label="Evidence">
          <div class="seg" role="group" aria-label="Evidence file">${[['research', 'Research'], ['factcheck', 'Fact-check'], ['adversarial', 'Adversarial']].map(([k, n]) => html`<button type="button" data-ev="${k}" aria-pressed="${String(evTab === k)}">${n}</button>`)}</div>
          <div class="ev"><h3 class="label">${file}</h3>${fx[file] ? html`<dl><dt>File</dt><dd>${fm(file)}</dd><dt>Lamp</dt><dd>${GLYPH[g[at][2]]} ${g[at][3]}</dd></dl>` : html`<p>${file} is missing.</p>`}</div>
          <p class="why">An edit changes the draft digest, so every gate goes stale until research is re-stamped and both verdicts are recorded again.</p>
        </aside>
      </div>`);
    el.querySelector('#back')?.addEventListener('click', () => setView('board'));
    el.querySelectorAll('[data-ev]').forEach(b => b.addEventListener('click', () => { evTab = b.dataset.ev; renderEditor(); }));
  }

  function render() { renderStrip(); if (view === 'board') renderBoard(); else renderEditor(); renderDetails(); }

  // ---------- Commands (products/system/commands.md): stage, stage --correct, park, park --revive, readiness, publish ----------
  // build: each write posts the old item page's route with the piece's revision, then rereads; a refused write reads again.
  // Stage has no Undo: going back is a correction, which takes a reason and only the operator may make. Park and Revive are
  // each other's inverse, so each undoes the other with the revision its own write returned.
  let target = null, reading = null;
  const P_ = o => byId(o.id);
  const nextOf = p => target && p.next.includes(target) ? target : p.next[0];
  const write = (p, suffix, body) => post(`/api/items/${p.id}/${suffix}`, { ...body, revision: p.revision })
    .then(out => { reading?.reread(); return out; }, e => { reading?.load(); throw e; });
  const parkWrite = (o, parked) => { const p = P_(o); return write(p, parked ? 'park' : 'revive', {})
    .then(out => ({ text: parked ? `#${p.id} parked` : `#${p.id} revived at ${SL[p.stage] || p.stage}`, revision: out.revision })); };
  const parkUndo = parked => (o, v) => post(`/api/items/${o.id}/${parked ? 'revive' : 'park'}`, { revision: v.revision })
    .then(() => reading?.reread(), e => { reading?.load(); throw e; });
  const CMDS = [
    { id: 'piece.stage', on: 'piece', label: 'Stage', key: 's', icon: 'arrow-up-right', risk: 'confirm',
      cli: o => `sd writing stage --piece ${P_(o).piece} --stage ${nextOf(P_(o)) || '…'}`,
      primary: () => true,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (p.parked || owned(p) !== true) return legal(p, '').reason;
        const to = nextOf(p); if (!to) return 'no forward stage from here'; const V = legal(p, to); return V.ok || V.reason; },
      consequence: o => { const p = P_(o), to = nextOf(p); return `Moves #${p.id} from ${SL[p.stage] || p.stage} to ${SL[to] || to}${to === 'ready' ? ' and records readiness for this draft' : ''}. Going back is a correction with a reason.`; },
      run: o => { const p = P_(o), to = nextOf(p), from = p.stage; target = null;
        return write(p, 'stage', { stage: to }).then(() => `#${p.id} ${SL[from] || from} → ${SL[to] || to}${to === 'ready' ? ' · readiness recorded' : ''}`); } },
    { id: 'piece.correct', on: 'piece', label: 'Correct', key: 'o', icon: 'undo-2', risk: 'safe', fills: true,
      cli: o => `sd writing stage --piece ${P_(o).piece} --stage ${target && P_(o).corrections.includes(target) ? target : (P_(o).corrections.at(-1) || '…')} --correct --reason "…"`,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (owned(p) !== true) return owned(p); return p.parked ? 'parked; revive first' : p.corrections.length ? true : 'no earlier stage to correct to'; },
      run: o => { const p = P_(o), to = target && p.corrections.includes(target) ? target : null; target = null; askReason(p, to); return null; } }, // the dialog's Correct is the write
    { id: 'piece.park', on: 'piece', label: 'Park', key: 'p', icon: 'archive', risk: 'undo', bulk: true,
      cli: o => `sd writing park --piece ${P_(o).piece}`,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (owned(p) !== true) return owned(p); return p.publication ? 'published pieces stay on the board' : !p.parked || 'already parked'; },
      run: o => parkWrite(o, true), undo: parkUndo(true) },
    { id: 'piece.revive', on: 'piece', label: 'Revive', key: 'v', icon: 'archive-restore', risk: 'undo', bulk: true,
      cli: o => `sd writing park --piece ${P_(o).piece} --revive`,
      primary: o => !!P_(o)?.parked,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (owned(p) !== true) return owned(p); return p.parked || 'not parked'; },
      run: o => parkWrite(o, false), undo: parkUndo(false) },
    { id: 'piece.readiness', on: 'piece', label: 'Readiness', key: 'r', icon: 'shield-check', risk: 'safe',
      cli: o => `sd writing readiness --piece ${P_(o).piece}`,
      run: o => { const p = P_(o); return p.gates_ok ? `#${p.id}: review checks pass` : `#${p.id}: ${markup.plural(p.problems.length, 'gate problem')}; first: ${p.problems[0]}`; } },
    { id: 'piece.publish', on: 'piece', label: 'Publish', key: 'u', icon: 'send', risk: 'confirm',
      // sd writing publication-claim writes the claim (--piece, --context-file, --payload-file); dispatch then publishes it.
      // build: copy only, with the reference's own executes: false; the dashboard sends nothing.
      executes: false,
      cli: o => `sd writing publication-claim --piece ${P_(o).piece} --context-file context.json --payload-file payload.json\nsd writing publication-dispatch --piece ${P_(o).piece} --claim <claim> --context-file context.json --confirmed`,
      primary: o => !!P_(o) && publishState(P_(o)).ok,
      when: o => { const P = publishState(P_(o)); return P.ok || P.why.join(' '); },
      consequence: () => 'Publishes to the destinations in sd-plugin.json; the canonical destination comes first. The publication claim records the URL. The dashboard sends nothing: copy the two lines.',
      run: o => `Copy the two lines to publish #${P_(o).id}; the dashboard does not run them` },
  ];
  C.register(...CMDS);
  const cmd = id => CMDS.find(c => c.id === id);
  function move(id, to) {
    const p = byId(id), V = legal(p, to);
    if (!V.ok) { if (!V.same) toast(`#${p.id} not moved: ${V.reason}`); return; }
    target = to;
    C.run(cmd(V.correct ? 'piece.correct' : 'piece.stage'), C.get(String(id)));
  }
  // Correction reason (change_stage --correct needs one; only the operator may correct).
  const dlg = document.createElement('dialog'); dlg.className = 'palette dlg'; dlg.setAttribute('aria-labelledby', 'dlg-h'); put(dlg, html`<h2 id="dlg-h"></h2>`); document.body.append(dlg);
  function correct(p, to, reason) {
    return write(p, 'stage', { stage: to, correct: true, reason }).then(() => toast(`#${p.id} corrected to ${SL[to] || to}`), e => toast(`#${p.id} not changed: ${e.message}`));
  }
  function askReason(p, to) {
    const opts = p.corrections;
    put(dlg, html`<form method="dialog" class="dlg-form"><h2 id="dlg-h">Correct #${p.id}</h2>
      <p class="why">A correction sends the piece back and records why. Its review checks must be renewed afterwards.</p>
      <label class="label" for="to-in">Back to</label><select id="to-in">${opts.map(s => html`<option value="${s}"${s === (to || opts.at(-1)) ? html` selected` : ''}>${SL[s]}</option>`)}</select>
      <label class="label" for="why-in">Reason</label><textarea id="why-in" rows="3" required></textarea>
      <div class="cli"><code id="corr-cli"></code></div>
      <div class="actions"><button class="btn quiet" value="cancel" type="submit" formnovalidate>Keep it</button><button class="btn" value="ok" type="submit">Correct</button></div></form>`);
    const sel = dlg.querySelector('#to-in'), why = dlg.querySelector('#why-in'), cli = dlg.querySelector('#corr-cli');
    const upd = () => { cli.textContent = `sd writing stage --piece ${p.piece} --stage ${sel.value} --correct --reason ${shq(why.value)}`; };
    sel.addEventListener('change', upd); why.addEventListener('input', upd); upd();
    dlg.onclose = () => { if (dlg.returnValue === 'ok' && why.value.trim()) correct(p, sel.value, why.value.trim()); else toast(`#${p.id} not moved.`); };
    dlg.returnValue = ''; dlg.showModal(); why.focus();
  }

  // ---------- Selection ----------
  const curId = () => selected == null ? null : String(selected);
  function select(id, open) {
    selected = +id;
    document.querySelectorAll('.card[data-id]').forEach(n => n.setAttribute('aria-selected', n.dataset.id === curId()));
    if (view === 'editor') renderEditor();
    renderDetails();
    const scope = document.getElementById('scope-name'); if (scope && byId(selected)) scope.textContent = byId(selected).piece; // chat scope = the selected piece
    C.select(curId()); // the shell writes ?row=
    window.shell.url({ view });
    if (open && innerWidth < 1240) openPane('tab-details');
  }
  function unselect() { selected = null; renderDetails(); if (view === 'editor') renderEditor(); }
  document.getElementById('main').addEventListener('click', e => { const c = e.target.closest?.('.card[data-id]'); if (c && !suppress) select(c.dataset.id, true); });
  // Selection follows focus on a card, so Enter, which the shell sends as shell:open, opens the focused one.
  document.getElementById('main').addEventListener('focusin', e => { const c = e.target.closest?.('.card[data-id]'); if (c && e.target === c && c.dataset.id !== curId()) select(c.dataset.id, false); });
  document.addEventListener('shell:open', e => { if (byId(e.detail)) select(e.detail, true); });
  document.addEventListener('shell:picked', e => document.querySelectorAll('.card[data-id]').forEach(c => c.toggleAttribute('data-picked', e.detail.includes(c.dataset.id))));
  document.getElementById('show-parked').addEventListener('click', e => { showParked = !showParked; e.currentTarget.setAttribute('aria-pressed', showParked); render(); });

  // ---------- Drag (mouse and pen; touch uses the Details buttons) ----------
  let drag = null, suppress = false;
  const verdict = (p, z) => { const to = z.split(':')[1], V = legal(p, to); return { ok: V.ok, same: V.same, text: V.ok ? (V.correct ? `correct to ${SL[to]} (reason asked)` : `→ ${SL[to]}`) : V.reason }; };
  document.addEventListener('pointerdown', e => {
    const c = e.target.closest?.('.card[data-id]:not(.idea)');
    if (!c || e.button !== 0 || e.pointerType === 'touch' || e.target.closest('button')) return;
    drag = { id: +c.dataset.id, el: c, x: e.clientX, y: e.clientY, on: false, over: null };
  });
  document.addEventListener('pointermove', e => {
    if (!drag) return;
    if (!drag.on) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5) return;
      drag.on = true;
      const p = byId(drag.id), r = drag.el.getBoundingClientRect();
      drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top;
      drag.ghost = drag.el.cloneNode(true); drag.ghost.classList.add('ghost-card'); drag.ghost.removeAttribute('tabindex'); drag.ghost.setAttribute('aria-hidden', 'true');
      drag.ghost.style.width = r.width + 'px'; put(drag.ghost, html`<div class="verdict"></div>`, 'append');
      document.body.append(drag.ghost); drag.el.classList.add('lifted');
      document.getElementById('main').setAttribute('data-dragging', '');
      document.querySelectorAll('[data-drop]').forEach(z => {
        const V = verdict(p, z.dataset.drop);
        z.dataset.legal = V.ok || V.same ? 'yes' : 'no';
        const rf = z.querySelector('.refusal'); if (rf) put(rf, html`${V.ok || V.same ? html`` : html`<b>■ Refused.</b> ${V.text}`}`);
      });
    }
    drag.ghost.style.left = e.clientX - drag.dx + 'px'; drag.ghost.style.top = e.clientY - drag.dy + 'px';
    const z = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-drop]');
    if (z !== drag.over) {
      drag.over?.removeAttribute('data-over'); drag.over = z; z?.setAttribute('data-over', 'yes');
      const V = z ? verdict(byId(drag.id), z.dataset.drop) : null, vd = drag.ghost.querySelector('.verdict');
      vd.textContent = V ? (V.same ? 'no change' : V.ok ? V.text : '■ refused: ' + V.text) : '';
      vd.classList.toggle('no', !!V && !V.ok && !V.same);
    }
  });
  function endDrag(commit) {
    if (!drag) return; const d = drag; drag = null; if (!d.on) return;
    suppress = true; setTimeout(() => { suppress = false; }, 0);
    d.ghost.remove(); d.el.classList.remove('lifted'); document.getElementById('main').removeAttribute('data-dragging');
    document.querySelectorAll('[data-drop]').forEach(z => { z.removeAttribute('data-over'); z.removeAttribute('data-legal'); });
    if (commit && d.over) move(d.id, d.over.dataset.drop.split(':')[1]);
  }
  document.addEventListener('pointerup', () => endDrag(true));
  document.addEventListener('pointercancel', () => endDrag(false));

  // ---------- Views and keys ----------
  function subhead() {
    const sub = document.getElementById('subhead');
    if (!D) { sub.textContent = 'The pieces were not read.'; return; }
    sub.textContent = `${pieces.length} pieces · read ${String(D.read).slice(11, 16)} UTC · ` + (view === 'board' ? 'Pieces by stage. Four lamps per piece: draft, research, fact-check, adversarial.' : 'Focused editor: the draft on the left, its evidence on the right.');
  }
  function setView(v) {
    view = v;
    document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', b.dataset.view === v));
    ['board', 'editor'].forEach(x => { const el = document.getElementById('view-' + x); el.hidden = x !== v; if (x !== v) el.replaceChildren(); });
    document.getElementById('strip').hidden = v === 'editor';
    subhead();
    window.shell.url({ view: v }); // the shell keeps ?row=
    render();
  }
  document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));
  window.writingView = setView;
  let vChord = 0;
  document.addEventListener('keydown', e => {
    if (e.target.matches?.('input, textarea, select, [contenteditable="true"]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || window.shell.chording()) return;
    if (vChord && Date.now() - vChord < 1500) { const m = { b: 'board', e: 'editor' }[e.key]; vChord = 0; if (m) { e.preventDefault(); setView(m); return; } }
    if (e.key === 'v') { vChord = Date.now(); return; }
  });
  // The shell walks the board's cards on j / k (a card takes focus) and sends Esc here to cancel a drag.
  window.PAGE_LIST = { rows: () => document.querySelectorAll('#board .card[data-id]'), current: () => curId(), when: () => view === 'board',
    select: (id, n) => { select(id); n.focus(); }, clear: () => !!drag && (endDrag(false), true) };

  // ---------- Shapeshift: capture an idea, or find a piece ----------
  // build: capture is copy only. It writes a vault note, and no dashboard route writes to the vault.
  const inp = document.getElementById('shift-in'), prev = document.getElementById('shift-preview'), ghost = document.getElementById('shift-ghost');
  const captureCli = t => `sd store add sdw.blog-idea ${shq(t)} --field status=inbox`;
  function parse(t) {
    const s = t.trim(); let m;
    if ((m = s.match(/^#(\d+)$/))) return { find: byId(m[1]) ? [byId(m[1])] : [], id: m[1] };
    const q = s.replace(/^idea:\s*/i, '');
    const hits = q.length > 2 ? pieces.filter(p => (p.title + ' ' + p.piece).toLowerCase().includes(q.toLowerCase())).slice(0, 3) : [];
    return { idea: q, find: hits };
  }
  function showPrev() {
    const t = inp.value; ghost.hidden = !!t;
    if (!t.trim()) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); return; }
    const P = parse(t);
    put(prev, html`${(P.idea ? html`<div class="as"><span class="label">Enter copies</span>${ICON('lightbulb')}<span>${P.idea}</span></div><div class="cli"><code>${captureCli(P.idea)}</code><button class="icon-btn" type="button" aria-label="Copy: ${captureCli(P.idea)}" data-copy="${captureCli(P.idea)}">${ICON('copy')}</button></div>` : '') }${
      (P.find.length ? html`<p class="hint">${P.idea ? 'Or open' : 'Enter opens'}</p><div class="suggest">${P.find.map(p => html`<button type="button" data-open-find="${p.id}">${ICON('search')}<span>#${p.id} · ${p.title}</span></button>`)}</div>` : P.id ? html`<p class="hint">No piece #${P.id}.</p>` : '') }${
      html`<p class="hint">Capture writes to the vault, which the dashboard does not write · Esc clears</p>`}`);
    prev.hidden = false; inp.setAttribute('aria-expanded', 'true');
  }
  inp.addEventListener('input', showPrev); inp.addEventListener('focus', showPrev);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Escape') { if (inp.value) { e.stopPropagation(); inp.value = ''; showPrev(); } return; }
    if (e.key !== 'Enter') return; e.preventDefault();
    const P = parse(inp.value);
    if (!P.idea && P.find[0]) { inp.value = ''; showPrev(); select(P.find[0].id, true); return; }
    if (!P.idea) return;
    const line = captureCli(P.idea); inp.value = ''; showPrev();
    navigator.clipboard?.writeText(line).catch(() => {});
    toast(`Copy the line to capture the idea; the dashboard does not write to the vault: ${line}`);
  });
  prev.addEventListener('click', e => { const b = e.target.closest?.('[data-open-find]'); if (b) { inp.value = ''; showPrev(); select(b.dataset.openFind, true); } });
  document.addEventListener('click', e => { if (!e.target.closest?.('#shift')) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); } });

  // ---------- Start (build: read /api/writing through shell.read, then draw) ----------
  // The reader (read.js) holds the guards: of overlapping reads only the newest draws, a piece the read no longer lists runs no
  // command, and a failed read clears the board, the rail and Details.
  const q = new URLSearchParams(location.search);
  view = q.get('view') === 'editor' ? 'editor' : 'board';
  const firstShown = () => (pieces.find(p => showParked || !p.parked) || pieces[0])?.id;
  reading = window.shell.read({
    source: '/api/writing', what: 'the pieces',
    adopt: doc => {
      D = doc; pieces = doc.pieces || [];
      document.body.dataset.observed = D.read;
      return { objects: pieces.map(p => ({ id: String(p.id), type: 'piece', label: `#${p.id} ${p.title}` })),
        state: pieces.length ? null : { kind: 'empty', title: 'No pieces', text: 'No writing piece is registered. Import the writing collection to see its pieces here.', source: '/api/writing' } };
    },
    clear: () => { D = null; pieces = []; delete document.body.dataset.observed; },
    draw: () => { setView(view); },
    current: () => curId(),
    first: () => (firstShown() == null ? null : String(firstShown())),
    select: (id, opened) => select(id, opened),
    unselect,
  });
  reading.load();
  suggest(['Which fact-check findings block this piece?', 'Summarise what changed since research was stamped', 'Draft the reason to correct this piece to Review']);
});
// Top level, not inside DOMContentLoaded: shell.js builds the ? sheet from PAGE_KEYS when it loads.
window.PAGE_KEYS = [['v b / v e', 'Board or editor view']];
