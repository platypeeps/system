// Notes (sd:2120): the design source's products/system/designs/pages/notes.js, ported. Each change from the reference is
// marked "build:". The days are /api/notes (notes_screen.py), read through the shared reader; none is sample data.
// build: days are this machine's (the document's tz), times show in the viewer's own zone; mail and the daily note text are
// unknown on every day, with the document's reason.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { ok: ['ok', '●'], caution: ['caution', '▲'], warning: ['warning', '■'], queued: ['queued', '◌'], unknown: ['unknown', '▨'] };
  const DOW = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'], MON = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const dt = d => new Date(d + 'T12:00:00Z');
  const addDays = (d, n) => { const x = dt(d); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
  const long = d => { const x = dt(d); return `${DOW[x.getUTCDay()]}, ${MON[x.getUTCMonth()]} ${x.getUTCDate()}`; };
  // Times in the viewer's zone: Intl applies its offset for that date, so DST and any other zone read right.
  const HM = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const local = s => HM.format(new Date(s));
  const stamp = s => s ? `${local(s)} · ${s.replace('T', ' ').replace('Z', ' UTC')}` : 'not recorded';
  let DOC = null, DAYS = [], TODAY = null, day = null, selected = null, OBJS = [];
  const expanded = new Set(), openRepos = new Set();

  // Objects for selection, the ⋯ menu and the palette. Activity rows are records: no write commands.
  function rows(d) {
    const A = DOC.days[d], out = [];
    A.merges.forEach(m => out.push({ id: m.id, type: 'merge', label: m.ref, at: m.at, g: G.ok, s: m.what, small: m.ref, repo: m.repo || '(no repository)',
      facts: [['Repository', m.repo || '—'], ['Pull request', m.ref], ['Commit', m.commit.slice(0, 12)], ['Merged', stamp(m.at)], ...(m.item ? [['Item', `sd:${m.item}`]] : [])],
      src: 'sd-ship delivery note (ship.note_merge)', open: m.url ? [m.url, 'Open the pull request'] : null }));
    A.done.forEach(x => out.push({ id: x.id, type: 'done task', label: `#${x.item} ${x.title}`, at: x.at, g: G.ok, s: x.title, small: `#${x.item}`, item: x.item,
      facts: [['Item', `#${x.item}`], ['Kind', x.kind], ['Repository', x.repo || 'none'], ['From', x.from], ['Done at', stamp(x.at)], ['By', x.by || 'not recorded']],
      src: 'sd.db note.kind = status_change, "-> done"', open: [`${window.shell.pages?.Tasks || '/tasks'}?row=${x.item}`, 'Open in Tasks'] }));
    A.runs.forEach(r => out.push({ id: r.id, type: 'run', label: `assignment ${r.n}`, at: r.at, g: G[r.s] || G.unknown, s: r.what, small: `${r.status} · ${r.provider || 'no provider'}`, item: r.item,
      facts: [['Assignment', r.n], ['Item', r.item ? `sd:${r.item}` : '—'], ['Repository', r.repo || '—'], ['Role', r.role], ['Provider', r.provider || '—'], ['Status', r.status], ['Started', stamp(r.started)], ['Ended', stamp(r.ended)]],
      src: 'runner assignments (operations.assignment_state)', open: null }));
    return out;
  }
  const rowHtml = o => html`<li class="row" data-id="${o.id}" aria-selected="${o.id === selected ? 'true' : 'false'}"><span class="t">${local(o.at)}</span><span class="g g-${o.g[0]}" aria-hidden="true">${o.g[1]}</span><button class="s" type="button" data-open="${o.id}"><span class="sr">${o.g[0]} · ${o.type} · </span>${o.s}${o.small ? html`<small>${o.small}</small>` : ''}</button>${window.shell.commands.rowActions(o.id)}</li>`;
  const LIMIT = 12;
  function list(objs, key) {
    const open = expanded.has(key), shown = open ? objs : objs.slice(0, LIMIT);
    return html`<ul class="rows" aria-label="${key}">${shown.map(rowHtml)}</ul>${objs.length > LIMIT ? html`<button class="btn quiet sm more" type="button" data-more="${key}">${open ? 'Show fewer' : `Show all ${objs.length}`}</button>` : ''}`;
  }
  const sect = (id, title, n, src, body) => html`<section class="act" id="${id}" aria-labelledby="${id}-h"><header><h2 id="${id}-h">${title}</h2><span class="n">${n}</span><span class="src">${src}</span></header>${body}</section>`;
  const failed = name => DOC?.sources?.[name];
  const notRead = why => html`<div class="unknown"><b>▨ Not read.</b> ${why}</div>`;

  function render() {
    const el = $('daily');
    $('day-name').textContent = day ? long(day) : 'Notes not read';
    $('next').disabled = !day || day >= TODAY; $('next').title = day && day >= TODAY ? 'Today is the newest day' : '';
    $('prev').disabled = !day || day <= DAYS[0]; $('prev').title = day && day <= DAYS[0] ? `The read starts ${DAYS[0]}` : '';
    $('today').disabled = !day || day === TODAY;
    if (!DOC) { put(el, html``); return; }
    const prose = html`<div class="prose" role="note"><p><b>▨ Daily note text unknown.</b> ${DOC.unknown.daily}</p><p>The activity below is that day's record, read from its sources.</p></div>`;
    const A = DOC.days[day];
    if (!A) { put(el, html`${prose}${notRead(`The read covers ${long(DAYS[0])} to ${long(TODAY)} only.`)}`); return; }
    const merges = OBJS.filter(o => o.type === 'merge'), done = OBJS.filter(o => o.type === 'done task'), runs = OBJS.filter(o => o.type === 'run');
    const by = {}; merges.forEach(o => (by[o.repo] ||= []).push(o));
    const repos = Object.keys(by).sort((a, b) => by[b].length - by[a].length || a.localeCompare(b));
    const blocked = runs.filter(o => o.g[0] === 'caution').length;
    const read = DOC.read.slice(11, 16);
    put(el, html`${prose}<nav class="tally" aria-label="The day in numbers">
        <a href="#merges"${failed('merges') ? html` class="unk"` : ''}><b>${failed('merges') ? '▨' : merges.length}</b><span>${failed('merges') ? 'merges not read' : `merges in ${plural(repos.length, 'repo')}`}</span></a>
        <a href="#done"${failed('changes') ? html` class="unk"` : ''}><b>${failed('changes') ? '▨' : done.length}</b><span>${failed('changes') ? 'status changes not read' : `tasks done · ${A.opened} opened`}</span></a>
        <a href="#runs"${failed('runs') ? html` class="unk"` : ''}><b>${failed('runs') ? '▨' : runs.length}</b><span>${failed('runs') ? 'runs not read' : `runs${blocked ? ` · ${blocked} blocked` : ''}`}</span></a>
        <a href="#mail" class="unk"><b>▨</b><span>mail not read</span></a>
      </nav>${sect('merges', 'Merges', failed('merges') ? '▨' : merges.length, `sd-ship delivery notes · read ${read} UTC`,
        failed('merges') ? notRead(failed('merges')) : repos.length ? html`${repos.map((r, i) => html`<details class="repo" data-repo="${r}"${i === 0 || openRepos.has(r) ? html` open` : ''}><summary><b>${r}</b><span class="n">${by[r].length}</span></summary>${list(by[r], 'merges ' + r)}</details>`)}` : html`<p class="why">No merges this day.</p>`)
      }${sect('done', 'Tasks done', failed('changes') ? '▨' : done.length, `sd.db status changes · read ${read} UTC`, failed('changes') ? notRead(failed('changes')) : done.length ? list(done, 'tasks done') : html`<p class="why">No task reached done this day.</p>`)
      }${sect('runs', 'Runs', failed('runs') ? '▨' : runs.length, `runner assignments by start or end · read ${read} UTC`, failed('runs') ? notRead(failed('runs')) : runs.length ? list(runs, 'runs') : html`<p class="why">No runner run started or ended this day.</p>`)
      }${sect('mail', 'Mail', '▨', 'not read', html`<div class="unknown"><b>▨ Unknown.</b> ${DOC.unknown.mail}</div>`)}`);
  }
  function renderDetails() {
    const el = $('details'), o = selected && window.shell.commands.get(selected);
    if (!o || !o.facts) {
      put(el, DOC ? html`<div class="kind"><span class="label">Daily note</span></div><h2>${long(day)}</h2><p class="note">Select a merge, task or run row to see where it came from.</p>`
        : html`<p class="note">Nothing is selected: the last week could not be read.</p>`);
      return;
    }
    put(el, html`<div class="kind"><span class="label">${o.type}</span><span class="tag">observed</span></div>
      <h2>${o.s}</h2>
      <dl>${o.facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Day</dt><dd>${long(day)} (${DOC.tz})</dd></dl>
      ${o.open ? html`<p><a class="open-link" href="${o.open[0]}">${o.open[1]}</a></p>` : ''}
      <p class="note">Source: ${o.src}. Read ${DOC.read.replace('T', ' ').replace('Z', ' UTC')}.</p>`);
  }
  function select(id, open) {
    selected = id;
    document.querySelectorAll('.row[data-id]').forEach(r => r.setAttribute('aria-selected', r.dataset.id === id ? 'true' : 'false'));
    window.shell.commands.select(id); renderDetails();
    if (open && innerWidth < 1240) window.shell.openPane('tab-details');
  }
  // build: a day change redraws from the one document; the shared reader keeps the selection only when it is on that day.
  function go(d) {
    day = d; selected = null; expanded.clear(); openRepos.clear();
    OBJS = DOC ? rows(day) : [];
    window.shell.commands.select(null);
    window.shell.setContext?.(long(day));
    window.shell.url?.({ day });
    render(); renderDetails();
  }
  window.notesDay = n => { if (!day) return; const d = addDays(day, n); if (d <= TODAY && d >= DAYS[0]) go(d); };

  let reader = null;
  const spec = {
    source: '/api/notes', what: 'the last week',
    adopt: doc => {
      DOC = doc; DAYS = Object.keys(doc.days).sort(); TODAY = doc.today;
      const asked = new URLSearchParams(location.search).get('day');
      if (!day || !doc.days[day]) day = asked && doc.days[asked] ? asked : TODAY;
      OBJS = rows(day);
      put($('subhead'), html`${plural(DAYS.length, 'day')} · days in ${doc.tz}, times in your zone · read <time class="rel" datetime="${doc.read}"></time>`);
      const broken = Object.entries(doc.sources);
      // Every day's rows are objects, so the palette and ?row= reach a row on another day; Details shows the day it is on.
      const all = DAYS.flatMap(d => d === day ? OBJS : rows(d));
      return { objects: all, state: broken.length ? { kind: 'partial', text: `${broken.map(([k, v]) => `${k}: ${v}`).join(' · ')}. The other sources are current.`, source: '/api/notes' } : null };
    },
    clear: () => { DOC = null; DAYS = []; OBJS = []; day = null; put($('subhead'), html`The last week could not be read.`); },
    draw: () => render(),
    current: () => selected,
    first: () => null,
    select: (id, opened) => {
      const d = DAYS.find(k => DOC.days[k].merges.concat(DOC.days[k].done, DOC.days[k].runs).some(r => r.id === id));
      if (d && d !== day) { day = d; OBJS = rows(day); render(); }
      select(id, opened);
    },
    unselect: () => { selected = null; renderDetails(); },
  };

  window.PAGE_COMMANDS = [
    { label: 'Write a quick note', icon: 'notebook-pen', key: '/', run: () => $('qn-in').focus() },
    { label: 'Previous day', icon: 'chevron-left', key: '←', run: () => window.notesDay(-1) },
    { label: 'Next day', icon: 'chevron-right', key: '→', run: () => window.notesDay(1) },
  ];
  // Notes is a record, not a queue: it never badges the rail.
  window.PAGE_ATTENTION = { state: 'ok', n: 0, what: '' };
  // The shell walks the day's rows on j / k; the row's button takes focus.
  window.PAGE_LIST = { rows: () => document.querySelectorAll('#daily .row[data-id]'), current: () => selected, select: (id, r) => { select(id); r?.querySelector('button.s')?.focus(); } };

  document.addEventListener('DOMContentLoaded', () => {
    const S = window.shell, C = S.commands;
    reader = S.read(spec);
    S.attention(window.PAGE_ATTENTION);
    $('prev').addEventListener('click', () => window.notesDay(-1));
    $('next').addEventListener('click', () => window.notesDay(1));
    $('today').addEventListener('click', () => { if (TODAY) go(TODAY); });
    $('daily').addEventListener('click', e => {
      const m = e.target.closest('[data-more]');
      if (m) { const k = m.dataset.more; expanded.has(k) ? expanded.delete(k) : expanded.add(k); document.querySelectorAll('details.repo[open]').forEach(d => openRepos.add(d.dataset.repo)); render(); return; }
      // Only the ledger's own row buttons open a row; the id is checked against the objects (template.html).
      const b = e.target.closest('.rows [data-open]'); if (b) { if (C.get(b.dataset.open)) select(b.dataset.open, true); return; }
      const r = e.target.closest('.row[data-id]'); if (r && !e.target.closest('button, a, time')) select(r.dataset.id, false);
    });
    document.addEventListener('shell:open', e => { if (OBJS.some(o => o.id === e.detail)) select(e.detail, true); });
    document.addEventListener('keydown', e => {
      if (e.target.matches?.('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); window.notesDay(-1); return; }
      if (e.key === 'ArrowRight') { e.preventDefault(); window.notesDay(1); }
    });

    // Quick notes: no sd store kind holds a loose note, and adding one is a pack change (sd:2120 records the decision), so a
    // kept note stays on this page and says so. No verb is mocked: sd store add needs a registered kind.
    const qn = [], qIn = $('qn-in'), qList = $('qn-list');
    C.register({ id: 'quicknote.discard', on: 'quick note', label: 'Discard', key: 'd', risk: 'undo', cli: () => 'no CLI: sd store has no loose-note kind',
      run: o => { const i = qn.indexOf(o.n); if (i >= 0) qn.splice(i, 1); o.at = i; paintQ(); return 'Quick note discarded'; },
      undo: o => { qn.splice(o.at, 0, o.n); paintQ(); } });
    function paintQ() {
      put(qList, html`${qn.length ? qn.map(n => { const id = `q-${n.t}`; C.put({ id, type: 'quick note', label: n.text.slice(0, 40), n }); return html`<li class="qn">${n.text}<small>▲ Not saved: no loose-note kind in sd store · ${local(n.t)}</small>${C.rowActions(id)}</li>`; })
        : html`<li><p class="empty">No quick notes. sd store has no kind for them yet, so this list starts empty on every visit.</p></li>`}`);
    }
    function keep() {
      const t = qIn.value.trim(); if (!t) { qIn.focus(); return; }
      qn.unshift({ text: t, t: new Date().toISOString() }); qIn.value = ''; paintQ();
      S.toast('Kept on this page only: sd store has no loose-note kind.');
    }
    $('qn-keep').addEventListener('click', keep);
    qIn.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); keep(); } });
    paintQ();
    S.suggest(['What did I merge today in system?', 'Which runs were blocked this week?', 'Draft this day\'s summary from its activity']);
    reader.load();
  });
})();
// Top level, not inside DOMContentLoaded: shell.js builds the ? sheet from PAGE_KEYS when it loads.
window.PAGE_KEYS = [['← / →', 'The previous or next day'], ['⌘↵', 'In the note field: keep the note']];
