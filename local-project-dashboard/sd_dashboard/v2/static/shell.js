// v2 shell: grouped rail, right pane (Details | Chat), help popovers, palette, toast, keys.
// Build port (local-project-dashboard): the design source's products/system/designs/v2/shell.js at d82daa1 (sd:2124; first ported
// at a3861c9 for sd:2110). The reference carries the build hooks below; the build adds only what is marked "build (sd:2163)".
// build (sd:2163): /ui/sections.js sets window.SHELL_CLASSIC, the old screen each unported section opens (a "classic"
// tag on the rail), and window.SHELL_SCREENS, old screens without a section, which the palette offers.
// Markup (sd:2127, from the build's sd:2110 port): every string of markup is html`…` from markup.js, which escapes each
// value put in it; put() is the only way in. rowActions, bar, time and ICON return html`…` for a page to compose.
// Help text renders <b> and <code> only, without attributes.
// Build hooks, marked "build:": a built dashboard sets window.SHELL_PAGES (its page map) and window.SHELL_CAPTURE (how
// a capture is filed); the mockup sets neither and keeps its sample behaviour. The favicon count is off under SHELL_PAGES,
// since the dashboard CSP (default-src 'self') refuses a data: icon.
// A page sets <body data-page="Today" data-scope="fleet" data-scope-note="…"> and may call window.shell.*.
(() => {
  const { html, put } = window.markup;
  const ICON = (n, cls = '') => html`<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const GROUPS = [
    ['Now',       [['Today', 'sunrise', 't'], ['Briefs', 'inbox', 'i']]],
    ['Work',      [['Tasks', 'list-todo', 'k'], ['Writing', 'pen-line', 'w'], ['Research', 'flask-conical', 'r'], ['Contributions', 'git-pull-request', 'c']]],
    ['Knowledge', [['Documents', 'file-text', 'd'], ['Notes', 'notebook-pen', 'n']]],
    ['Places',    [['HOA', 'droplets', 'h'], ['Home', 'house', 'o']]],
    ['Fleet',     [['Skills', 'sparkles', 's'], ['Metrics', 'chart-no-axes-column', 'm'], ['Management', 'settings', 'g'], ['Health', 'heart-pulse', 'e'], ['Activity', 'activity', 'a'], ['Reports', 'clipboard-list', 'p'], ['Commands', 'terminal', 'q'], ['Designs', 'palette', 'x']]],
  ];
  // build: a page that is built sets window.SHELL_PAGES; a section missing from it shows as not built yet.
  const PAGES = window.SHELL_PAGES || { Today: 'today.html', Briefs: 'briefs.html', Tasks: 'tasks.html', Research: 'research.html', Documents: 'documents.html', HOA: 'hoa.html', Home: 'home.html', Skills: 'skills.html', Metrics: 'metrics.html', Activity: 'activity.html', Reports: 'reports.html', Writing: 'writing.html', Notes: 'notes.html', Contributions: 'contributions.html', Health: 'health.html', Management: 'management.html', Designs: 'designs.html', Commands: 'commands.html' };
  // build (sd:2163): an unported section opens its old screen until its port moves it into SHELL_PAGES.
  const CLASSIC = window.SHELL_CLASSIC || {}, SCREENS = window.SHELL_SCREENS || {};
  const hrefOf = name => PAGES[name] || CLASSIC[name];
  const classic = name => !PAGES[name] && !!CLASSIC[name];
  const body = document.body;
  const page = body.dataset.page;
  // Rail badges: data/counts.js (window.SHELL_COUNTS) is generated from every page's window.PAGE_ATTENTION.
  // The current page overrides its own badge with its live value, so a badge never disagrees with its page.
  const counts = Object.assign({}, window.SHELL_COUNTS && window.SHELL_COUNTS.pages);
  // The badge says what the number is, so a screen reader hears "Tasks, 3 overdue" and not just "3".
  const badge = c => c && c.n && c.state !== 'ok' ? html`<span class="count ${c.state}" title="${c.n} ${c.what}">${c.n}<span class="sr"> ${c.what}</span></span>` : html``;

  // ---------- Rail ----------
  const rail = document.createElement('aside');
  rail.className = 'rail'; rail.setAttribute('aria-label', 'Sidebar');
  put(rail, html`
    <a class="mark" href="${PAGES.Today || '#'}"><b>sd</b><span>system</span></a>
    <nav aria-label="Sections" id="sections">${GROUPS.map(([g, items]) => html`
      <div><span class="label">${g}</span><ul>${items.map(([name, icon, key]) => {
        const href = hrefOf(name) || '#';
        return html`<li><a href="${href}" data-section="${name}"${name === page ? html` aria-current="page"` : ''}${hrefOf(name) ? '' : html` data-unbuilt`}${classic(name) ? html` data-classic` : ''}>${ICON(icon)}<span class="nm">${name}</span>${classic(name) ? html`<span class="classic">classic<span class="sr"> screen</span></span>` : ''}${badge(counts[name])}<kbd>g ${key}</kbd></a></li>`;
      })}</ul></div>`)}
      <div class="theme" role="group" aria-label="Theme">
        <button type="button" data-theme-choice="dark">${ICON('moon')}Dark</button>
        <button type="button" data-theme-choice="light">${ICON('sun')}Light</button>
        <button type="button" data-theme-choice="system">${ICON('monitor')}System</button>
      </div>
    </nav>
    <div class="foot">
      <button class="cmd fold" type="button" id="fold-rail" aria-expanded="true" aria-controls="sections">${ICON('panel-left-close')}<span class="txt">Collapse</span><kbd>[</kbd></button>
      <div class="theme rail-theme" role="group" aria-label="Theme">
        <button type="button" data-theme-choice="dark">${ICON('moon')}Dark</button>
        <button type="button" data-theme-choice="light">${ICON('sun')}Light</button>
        <button type="button" data-theme-choice="system">${ICON('monitor')}System</button>
      </div>
      <button class="cmd menu" type="button" id="open-menu" aria-expanded="false" aria-controls="sections">Sections</button>
      <button class="cmd" type="button" id="open-chat" aria-controls="pane" aria-label="Chat (c)">${ICON('message-square')}<span class="txt">Chat</span><kbd>c</kbd></button>
      <button class="cmd" type="button" id="open-palette" aria-label="Commands (⌘K)">${ICON('command')}<span class="txt">Commands</span><kbd>⌘K</kbd></button>
      <button class="cmd" type="button" id="reload" title="Reload this page (r)" aria-label="Reload this page (r)">${ICON('rotate-ccw')}<span class="txt">Reload</span><kbd>r</kbd></button>
    </div>`);
  // The in-nav theme copy shows only inside the narrow-screen Sections menu.
  rail.querySelector('nav > .theme').classList.add('menu-theme');
  const shell = document.querySelector('.shell');
  shell.prepend(rail);
  // Skip link: the first tab stop, ahead of the rail's 26 (review 2026-09-29, 24). main is its target.
  const main = document.querySelector('main') || body;
  if (main !== body) { main.id ||= 'main'; main.tabIndex = -1; }
  const skip = document.createElement('a');
  skip.className = 'skip'; skip.href = `#${main.id || ''}`; skip.textContent = 'Skip to content';
  // icons.js prepends its sprite on DOMContentLoaded, so the link goes in after it and stays first.
  const skipFirst = () => body.prepend(skip);
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', skipFirst) : skipFirst();

  // A page sets window.PAGE_ATTENTION = { state, n, what } and calls shell.attention() when it changes.
  const attention = a => {
    if (a) window.PAGE_ATTENTION = a;
    a = window.PAGE_ATTENTION;
    const link = rail.querySelector(`[data-section="${page}"]`);
    if (!a || !link) return;
    link.querySelector('.count')?.remove();
    put(link.querySelector('kbd'), badge(a), 'before');
    titleCount(a.state === 'warning' ? a.n : 0);
  };
  // Warnings show in the tab: "(3) Today · system" and a count drawn on the favicon (patterns.md, Density).
  // A token is a light-dark() pair, which a canvas refuses; a probe element resolves it to the colour the theme shows.
  function tok(name) {
    const i = document.createElement('i'); i.hidden = true; i.style.color = `var(${name})`; body.append(i);
    const c = getComputedStyle(i).color; i.remove(); return c;
  }
  const baseTitle = document.title.replace(/^\(\d+\) /, '');
  function titleCount(n) {
    document.title = n ? `(${n}) ${baseTitle}` : baseTitle;
    let icon = document.querySelector('link[rel="icon"]');
    if (!icon) { icon = document.createElement('link'); icon.rel = 'icon'; document.head.append(icon); }
    const c = document.createElement('canvas'); c.width = c.height = 32; const g = c.getContext('2d');
    const cs = getComputedStyle(document.documentElement);
    g.fillStyle = tok(n ? '--color-warning' : '--color-ink-2');
    g.beginPath(); g.arc(16, 16, 15, 0, Math.PI * 2); g.fill();
    g.fillStyle = tok('--color-paper'); g.font = `700 ${n > 9 ? 17 : 20}px ${cs.getPropertyValue('--font-data') || 'monospace'}`;
    g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(n ? String(Math.min(n, 99)) : 'sd', 16, 17);
    // build: a data: icon breaks the dashboard CSP (default-src 'self'), so the drawn count stays off.
    if (!window.SHELL_PAGES) icon.href = c.toDataURL('image/png');
  }
  attention();
  window.addEventListener('load', () => attention());

  const menu = rail.querySelector('#open-menu');
  const setMenu = open => { menu.setAttribute('aria-expanded', open); open ? rail.setAttribute('data-menu', '') : rail.removeAttribute('data-menu'); };
  menu.addEventListener('click', () => setMenu(menu.getAttribute('aria-expanded') !== 'true'));
  rail.addEventListener('click', e => {
    const a = e.target.closest('a[data-unbuilt]');
    if (a) { e.preventDefault(); toast(`${a.dataset.section} is not ${window.SHELL_PAGES ? 'built yet, and has no classic screen' : 'in this mockup round'}.`); }
  });

  // ---------- Right pane: Details | Chat ----------
  const details = document.getElementById('details'); // page-owned
  const pane = document.createElement('aside');
  pane.className = 'pane'; pane.id = 'pane'; pane.setAttribute('aria-label', 'Details and chat');
  const scope = body.dataset.scope || 'fleet';
  put(pane, html`
    <div class="tabs">
      <div role="tablist" aria-label="Panel" class="tablist">
      <button role="tab" id="tab-details" aria-controls="panel-details" aria-selected="true" type="button">${ICON('layout-dashboard')}<span class="nm">Details</span></button>
      <button role="tab" id="tab-chat" aria-controls="panel-chat" aria-selected="false" type="button" tabindex="-1">${ICON('message-square')}<span class="nm">Chat</span> <kbd>c</kbd></button>
      </div>
      <button class="icon-btn fold" type="button" id="fold-pane" aria-expanded="true" aria-controls="pane" aria-label="Collapse panel (])">${ICON('panel-right-close')}</button>
      <button class="icon-btn close" type="button" id="close-pane" aria-label="Close panel">${ICON('x')}</button>
    </div>
    <div role="tabpanel" id="panel-details" aria-labelledby="tab-details"></div>
    <div role="tabpanel" id="panel-chat" aria-labelledby="tab-chat" hidden>
      <div class="chat">
        <div class="chat-head">
          <span class="scope" title="Chat scope">${ICON('bot')}<span id="scope-name">${scope}</span></span>
          <span class="ctx" id="scope-ctx"></span>
          <button class="icon-btn" type="button" id="pin-scope" aria-pressed="false" aria-label="Pin this scope">${ICON('pin')}</button>
          <button class="help" type="button" aria-label="Help: chat scope" data-help="<b>Scope follows the view.</b> The chat answers inside the scoped repo or area, with the selected row as context. Pin keeps this scope while you move. Every write becomes a proposal you approve.">${ICON('circle-help')}</button>
        </div>
        <div class="thread" id="thread"></div>
        <form class="composer" id="composer">
          <label class="sr" for="chat-input">Message</label>
          <div class="box">
            <textarea id="chat-input" rows="1" placeholder="Ask about this view"></textarea>
            <button class="icon-btn" type="submit" aria-label="Send">${ICON('send')}</button>
          </div>
          <div class="meta"><span>Reads freely · writes need your approval</span><span>claude -p · runner</span></div>
        </form>
      </div>
    </div>`);
  shell.append(pane);
  if (details) pane.querySelector('#panel-details').append(details);

  const tabs = [...pane.querySelectorAll('[role="tab"]')];
  function showTab(id) {
    tabs.forEach(t => {
      const on = t.id === id;
      t.setAttribute('aria-selected', on); t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
    });
  }
  tabs.forEach(t => t.addEventListener('click', () => showTab(t.id)));
  // Roving focus: ← → move between tabs and select (WAI-ARIA tabs pattern).
  pane.querySelector('.tablist').addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const i = tabs.indexOf(document.activeElement); if (i < 0) return;
    e.preventDefault(); const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]; showTab(n.id); n.focus();
  });
  // Collapse: the rail folds to icons (≥900px) and the pane to a strip (≥1240px). [ and ] toggle; each browser remembers.
  const store = (k, v) => { try { v === undefined ? v = localStorage.getItem(k) : localStorage.setItem(k, v); } catch { v = null; } return v; };
  const foldRail = rail.querySelector('#fold-rail'), foldPane = pane.querySelector('#fold-pane');
  function setRail(min) {
    min ? body.dataset.rail = 'min' : delete body.dataset.rail; store('sd.rail', min ? 'min' : '');
    foldRail.setAttribute('aria-expanded', !min); foldRail.querySelector('.txt').textContent = min ? 'Expand' : 'Collapse';
    foldRail.querySelector('use').setAttribute('href', `#i-panel-left-${min ? 'open' : 'close'}`); foldRail.title = min ? 'Expand sections ([)' : '';
    rail.querySelectorAll('nav a[data-section]').forEach(a => { a.title = min ? a.querySelector('.nm').textContent : ''; });
  }
  function setPane(min) {
    min ? body.dataset.pane = 'min' : delete body.dataset.pane; store('sd.pane', min ? 'min' : '');
    foldPane.setAttribute('aria-expanded', !min); foldPane.setAttribute('aria-label', min ? 'Expand panel (])' : 'Collapse panel (])');
    foldPane.querySelector('use').setAttribute('href', `#i-panel-right-${min ? 'open' : 'close'}`);
  }
  foldRail.addEventListener('click', () => setRail(!body.dataset.rail));
  foldPane.addEventListener('click', () => setPane(!body.dataset.pane));
  // A tab click in the strip expands the pane on that tab.
  pane.querySelector('.tabs').addEventListener('click', e => { if (body.dataset.pane && e.target.closest('[role="tab"]')) setPane(false); });
  if (store('sd.rail') === 'min') setRail(true);
  if (store('sd.pane') === 'min') setPane(true);
  const wide = () => matchMedia('(min-width: 1240px)').matches;
  let paneReturn = null;
  // Below 1240 the pane is a bottom sheet, last in the DOM: focus moves into it, or 89 stops sit between the row and the sheet.
  const openPane = tab => { if (tab) showTab(tab); if (body.dataset.pane) setPane(false); if (!pane.contains(document.activeElement)) paneReturn = document.activeElement; pane.setAttribute('data-open', '');
    if (!wide() && !pane.contains(document.activeElement)) pane.querySelector('[role="tab"][aria-selected="true"]')?.focus(); };
  // Close puts focus back on the row (or button) that opened the sheet, so a keyboard user does not restart at the top.
  const closePane = () => { const was = pane.hasAttribute('data-open'); pane.removeAttribute('data-open'); if (was && paneReturn?.isConnected && !wide()) paneReturn.focus?.(); };
  pane.querySelector('#close-pane').addEventListener('click', closePane);
  function openChat() { openPane('tab-chat'); document.getElementById('chat-input').focus(); }
  rail.querySelector('#open-chat').addEventListener('click', openChat);

  const thread = pane.querySelector('#thread');
  const pin = pane.querySelector('#pin-scope');
  pin.addEventListener('click', () => pin.setAttribute('aria-pressed', pin.getAttribute('aria-pressed') !== 'true'));
  function setContext(text) { if (pin.getAttribute('aria-pressed') !== 'true') pane.querySelector('#scope-ctx').textContent = text ? `+ ${text}` : ''; }
  // The thread is not a live region: a selection rewrites the suggestions on every j / k, and a live thread announced each
  // (review 2026-09-29, 28). Only a new answer speaks, through role="status" on that message; the same list is not rewritten.
  let suggested = '';
  function suggest(list) {
    const key = JSON.stringify(list); if (key === suggested && thread.querySelector('.suggest')) return; suggested = key;
    put(thread, html`<div class="msg"><span class="label">Ask in ${scope}</span></div>
      <div class="suggest">${list.map(s => html`<button type="button">${ICON('message-square')}<span>${s}</span></button>`)}</div>`);
    thread.querySelectorAll('.suggest button').forEach(b => b.addEventListener('click', () => { send(b.textContent.trim()); }));
  }
  function send(text) {
    if (!text) return;
    // build: no chat backend is connected yet; the answer says so instead of pretending.
    const answer = window.SHELL_PAGES ? `No chat backend is connected to this dashboard yet, so nothing was sent. A later slice sends this to claude -p in the ${scope} scope.`
      : `Mockup: no chat backend here. The build sends this to claude -p in the ${scope} scope and streams the answer.`;
    put(thread, html`<div class="msg"><span class="who label">${ICON('user')}You</span><p>${text}</p></div>
      <div class="msg"><span class="who label">${ICON('bot')}Claude · ${scope}</span><p class="why">${answer}</p></div>`, 'append');
    thread.querySelectorAll('[role="status"]').forEach(m => m.removeAttribute('role'));
    thread.lastElementChild.setAttribute('role', 'status'); suggested = '';
    thread.scrollTop = thread.scrollHeight;
    remember(text, answer);
  }
  // Asked here: the last three answers given with a row selected stay with that row for the session; Details shows them under the bar.
  const askedKey = `sd.asked.${page}`;
  const askedAll = () => { try { return JSON.parse(sessionStorage.getItem(askedKey) || '{}'); } catch { return {}; } };
  function remember(q, a) {
    if (!selId) return;
    const all = askedAll(); all[selId] = [{ q, a, at: new Date().toISOString() }, ...(all[selId] || [])].slice(0, 3);
    try { sessionStorage.setItem(askedKey, JSON.stringify(all)); } catch { /* private window: the thread still shows it */ }
    renderAsked();
  }
  function renderAsked() {
    if (!details || details.querySelector('.asked')) return;
    const list = selId && askedAll()[selId]; if (!list || !list.length) return;
    const o = OBJ.get(selId);
    put(details, html`<section class="asked"><h3>Asked here</h3>${list.map(x => html`<div class="qa"><p class="q">${ICON('user')}<span>${x.q}</span></p><p class="a why">${x.a}</p></div>`)}<p class="why">${o ? `About ${o.label} · ` : ''}this session, newest first.</p></section>`, 'append');
  }
  if (details) new MutationObserver(() => renderAsked()).observe(details, { childList: true });
  pane.querySelector('#composer').addEventListener('submit', e => { e.preventDefault(); const i = document.getElementById('chat-input'); send(i.value.trim()); i.value = ''; });
  document.getElementById('chat-input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); pane.querySelector('#composer').requestSubmit(); } });

  // ---------- Help popovers ----------
  const pop = document.createElement('div');
  pop.className = 'pop'; pop.id = 'help-pop'; pop.setAttribute('role', 'tooltip'); pop.hidden = true;
  body.append(pop);
  let pinned = null, hoverTimer = 0;
  const DELAY = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--delay-help')) || 600;
  function place(btn) {
    const r = btn.getBoundingClientRect();
    pop.style.left = `${Math.max(8, Math.min(r.left - 8, innerWidth - pop.offsetWidth - 8))}px`;
    const below = r.bottom + 8, above = r.top - pop.offsetHeight - 8;
    pop.style.top = `${below + pop.offsetHeight < innerHeight || above < 0 ? below : above}px`;
  }
  // help:start
  // build: help text is page markup held in an attribute. <b> and <code> render, bare; every other tag and every
  // attribute is dropped, and all text shows as written. A <b> or <code> left open closes at the end.
  const HELP_TAGS = { b: [html`<b>`, html`</b>`], code: [html`<code>`, html`</code>`] };
  const helpText = s => { const out = [], open = []; let at = 0;
    for (const m of s.matchAll(/<(\/?)([a-zA-Z][\w-]*)[^>]*>/g)) {
      out.push(s.slice(at, m.index)); at = m.index + m[0].length;
      const tag = m[2].toLowerCase();
      if (!Object.hasOwn(HELP_TAGS, tag)) continue;
      if (!m[1]) { open.push(tag); out.push(HELP_TAGS[tag][0]); }
      else if (open.includes(tag)) for (let t = null; t !== tag;) { t = open.pop(); out.push(HELP_TAGS[t][1]); }
    }
    out.push(s.slice(at));
    while (open.length) out.push(HELP_TAGS[open.pop()][1]);
    return html`${out}`; };
  // help:end
  function showHelp(btn) {
    put(pop, helpText(btn.dataset.help || '')); pop.hidden = false; place(btn);
    btn.setAttribute('aria-describedby', 'help-pop');
  }
  function hideHelp() {
    pop.hidden = true;
    document.querySelectorAll('.help[aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded', 'false'));
    pinned = null;
  }
  document.addEventListener('click', e => {
    const b = e.target.closest('.help');
    if (b) {
      e.preventDefault(); e.stopPropagation();
      if (pinned === b) return hideHelp();
      hideHelp(); pinned = b; b.setAttribute('aria-expanded', 'true'); showHelp(b); return;
    }
    if (pinned && !pop.contains(e.target)) hideHelp();
  }, true);
  document.addEventListener('pointerover', e => {
    const b = e.target.closest?.('.help');
    if (!b || pinned || e.pointerType !== 'mouse') return;
    clearTimeout(hoverTimer); hoverTimer = setTimeout(() => showHelp(b), DELAY);
  });
  document.addEventListener('pointerout', e => { if (e.target.closest?.('.help') && !pinned) { clearTimeout(hoverTimer); pop.hidden = true; } });
  document.addEventListener('focusin', e => { const b = e.target.closest?.('.help'); if (b && !pinned) showHelp(b); });
  document.addEventListener('focusout', e => { if (e.target.closest?.('.help') && !pinned) pop.hidden = true; });
  document.querySelectorAll('.help').forEach(b => b.setAttribute('aria-expanded', 'false'));
  new MutationObserver(() => document.querySelectorAll('.help:not([aria-expanded])').forEach(b => b.setAttribute('aria-expanded', 'false'))).observe(body, { childList: true, subtree: true });

  // ---------- Toast with Undo ----------
  // Two elements: t carries the Undo, tp a plain message. A plain toast while an Undo is live goes to tp, stacked above, so
  // the Undo keeps its 10 s (review 2026-09-29, item 5). A new undoable action replaces the old Undo: u undoes the latest.
  const t = document.createElement('div'), tp = document.createElement('div');
  t.className = 'toast'; t.hidden = true; t.setAttribute('role', 'status');
  tp.className = 'toast plain'; tp.hidden = true; tp.setAttribute('role', 'status');
  body.append(t, tp);
  let tTimer = 0, tpTimer = 0, tUndo = null;
  function toast(msg, undo) {
    if (!undo && tUndo) { put(tp, html`<span>${msg}</span>`); tp.hidden = false; clearTimeout(tpTimer); tpTimer = setTimeout(() => { tp.hidden = true; }, 4000); return; }
    put(t, html`<span></span>${undo ? html`<button class="btn quiet sm" type="button" aria-keyshortcuts="u">${ICON('undo-2')}Undo<kbd>u</kbd></button>` : ''}`);
    t.firstChild.textContent = msg; t.hidden = false; tUndo = undo || null; tp.hidden = true;
    if (undo) t.querySelector('button').addEventListener('click', () => { t.hidden = true; tUndo = null; undo(); });
    clearTimeout(tTimer); tTimer = setTimeout(() => { t.hidden = true; tUndo = null; }, undo ? 10000 : 4000);
  }

  // ---------- Commands ----------
  // One declaration per command; the row button, the action menu, the Details bar and the palette all read it.
  // See products/system/commands.md. A page registers commands and puts its row objects:
  //   shell.commands.register({ id, on, label, key, cli: o => '…', risk: 'safe'|'undo'|'confirm', primary: o => bool, when: o => true|'reason', run: o => 'result', undo: o => {} })
  //   shell.commands.put({ id, type, label, …facts })   shell.commands.select(id)
  const REG = [], OBJ = new Map(), picked = new Set();
  // plural(n, one, many) from markup.js: "1 session", "2 sessions" (review 2026-09-29, 18).
  const { plural } = window.markup;
  let selId = null;
  // A command without cli is dashboard-only (chat, navigation); it shows no CLI line.
  const cliOf = (c, o) => c.cli ? c.cli(o) : '';
  // A cli string that starts with "no CLI:" or "no sd verb:" is a reason, not a command.
  const noCli = s => !s || /^no (CLI|sd verb)\b/.test(s);
  const shownCli = (c, o) => { const s = cliOf(c, o); return noCli(s) ? '' : s; };
  const offWhy = (c, o) => { const w = c.when ? c.when(o) : true; return w === true ? '' : (w || 'not available'); };
  // executes: true when the dashboard runs the CLI line itself; false when the line is shown for Copy only. A declaration may
  // say so; otherwise an `sd` verb runs and anything else (git, rm, uv, gh, sd-review, a pipe) is copy-only (commands.md).
  const executes = (c, o) => typeof c.executes === 'boolean' ? c.executes : /^sd\s/.test(shownCli(c, o));
  const cmdsFor = o => o ? REG.filter(c => c.on === o.type) : [];
  const live = o => cmdsFor(o).filter(c => !offWhy(c, o));
  // Every shell dialog opens through modal(): Tab and Shift+Tab cycle inside it, Esc closes it (native), and focus goes back
  // to the control that opened it, or, when a re-render replaced that control, to the same command or the row's ⋯ (patterns.md).
  const focusables = d => [...d.querySelectorAll('button, a[href], input:not([type=hidden]):not([disabled]), select, textarea, [tabindex="0"]')].filter(e => e.getClientRects().length && !e.disabled);
  function trap(d) {
    d.addEventListener('keydown', e => {
      if (e.key !== 'Tab') return;
      const f = focusables(d); if (!f.length) return;
      const i = f.indexOf(document.activeElement), n = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i === f.length - 1 ? 0 : i + 1);
      e.preventDefault(); f[n].focus();
    });
  }
  function refocus(from) {
    if (!from) return;
    if (from.isConnected) return from.focus();
    const sel = from.dataset?.cmd && from.dataset.obj ? `[data-cmd="${CSS.escape(from.dataset.cmd)}"][data-obj="${CSS.escape(from.dataset.obj)}"]` : from.dataset?.menuFor ? `[data-menu-for="${CSS.escape(from.dataset.menuFor)}"]` : null;
    const obj = from.dataset?.obj || from.dataset?.menuFor;
    (sel && document.querySelector(sel) || obj && document.querySelector(`[data-menu-for="${CSS.escape(obj)}"]`) || document.querySelector('main'))?.focus();
  }
  function modal(d, first) {
    const from = document.activeElement !== body ? document.activeElement : null;
    d.addEventListener('close', () => { const a = document.activeElement; if (!a || a === body || d.contains(a)) refocus(from); }, { once: true });
    d.showModal(); first?.focus();
    return from;
  }
  const confirmDlg = document.createElement('dialog');
  confirmDlg.className = 'palette confirm'; confirmDlg.setAttribute('aria-labelledby', 'confirm-h');
  put(confirmDlg, html`<h2 id="confirm-h"></h2>`); // the label target exists while the dialog is closed
  body.append(confirmDlg); trap(confirmDlg);
  // Confirm: one dialog for every irreversible, force or large bulk action. It names the target and shows the command.
  function confirmAction({ title, body: text = '', cli = '', ok = 'Confirm', keep = 'Keep it', danger = true }) {
    let from = null;
    return new Promise(done => {
      put(confirmDlg, html`<form method="dialog" class="confirm-body">
        <h2 id="confirm-h">${title}</h2>${text ? html`<p>${text}</p>` : ''}
        ${cli ? html`<div class="cli"><code>${cli}</code></div>` : ''}
        <div class="actions"><button class="btn quiet" value="no">${keep}</button><button class="btn${danger ? ' danger' : ''}" value="yes">${ok}</button></div></form>`);
      confirmDlg.returnValue = '';
      confirmDlg.onclose = () => done({ yes: confirmDlg.returnValue === 'yes', from });
      from = modal(confirmDlg, confirmDlg.querySelector('[value="no"]'));
    });
  }
  // The act a confirm names: the label when it already names its object ("Remove worktree"), else the label and the type
  // ("Cancel job"). The OK button repeats it; Keep says what not doing it means, never a second "Cancel" (review item 13).
  const actOf = (c, o) => /\s/.test(c.label.trim()) ? c.label : `${c.label} ${o.type}`;
  function run(c, o) {
    const go = () => {
      const msg = c.run ? c.run(o) : '';
      if (msg === null) return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id, form: true } })); // opened a form
      const text = msg || `${c.label} · ${o.label}`;
      // Undo only where the command declares one: a toast that says "undone" over nothing is worse than no Undo.
      if (c.risk === 'undo' && c.undo) toast(text, () => { c.undo(o); toast(`${c.label} undone · ${o.label}`); });
      else toast(text);
      document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } }));
    };
    if (c.risk !== 'confirm' && !c.askFirst) return go();
    const act = actOf(c, o);
    confirmAction({ title: `${act}: ${o.label}?`, body: c.consequence ? c.consequence(o) : '', cli: shownCli(c, o), ok: act, keep: `Don't ${c.label.toLowerCase()}`, danger: c.risk === 'confirm' })
      .then(({ yes, from }) => { if (!yes) return; go(); const a = document.activeElement; if (!a || a === body) refocus(from); });
  }

  // Proposal cards (chat writes). Approve is the confirmation; both approve and discard get Undo.
  // A card's approve button may carry data-done ("Draft created in Gmail") and data-undone ("Draft deleted").
  document.addEventListener('click', e => {
    const b = e.target.closest('.proposal [data-p]'); if (!b) return;
    const card = b.closest('.proposal'), foot = card.querySelector('footer'), p = b.dataset.p;
    if (p === 'approve') {
      const was = [...foot.childNodes], done = b.dataset.done || 'Approved', undone = b.dataset.undone || 'Approval undone';
      card.dataset.state = 'approved';
      put(foot, html`<span class="stamp">${ICON('check')}${done}</span>`);
      toast(done, () => { delete card.dataset.state; foot.replaceChildren(...was); toast(undone); });
    } else if (p === 'discard') {
      card.hidden = true;
      toast('Proposal discarded', () => { card.hidden = false; });
    } else if (p === 'edit') {
      // A card with data-edit="inline" edits its draft in place; any other card hands the change to chat.
      const d = card.dataset.edit === 'inline' && card.querySelector('.draft');
      if (d) { d.contentEditable = 'true'; d.setAttribute('role', 'textbox'); d.setAttribute('aria-multiline', 'true'); d.setAttribute('aria-label', 'Draft'); d.focus(); return; }
      const t = document.getElementById('chat-input'); t.value = `Change the proposal: `; t.focus();
    }
  });
  // Row: the primary command as a button, then ⋯ for the menu.
  function rowActions(id) {
    const o = OBJ.get(id); if (!o) return html``;
    const L = live(o), p = L.find(c => c.primary?.(o));
    if (!L.length) return html`<span class="rowact"></span>`;
    return html`<span class="rowact">${p ? html`<button class="btn quiet sm" type="button" data-cmd="${p.id}" data-obj="${id}">${p.label}</button>` : ''}<button class="icon-btn" type="button" data-menu-for="${id}" aria-haspopup="menu" aria-expanded="false" aria-label="Actions for ${o.label}">${ICON('ellipsis')}</button></span>`;
  }
  // Details: every command, off ones with their reason, each with its CLI line.
  function bar(id) {
    const o = OBJ.get(id); const list = cmdsFor(o); if (!list.length) return html``;
    const on = list.filter(c => !offWhy(c, o)), off = list.filter(c => offWhy(c, o));
    const first = on.find(c => c.primary?.(o)) || on[0];
    return html`<div class="actions">${on.map(c => html`<button class="btn${c === first ? '' : ' quiet'}${c.risk === 'confirm' ? ' risky' : ''}" type="button" data-cmd="${c.id}" data-obj="${id}" aria-keyshortcuts="${c.key ? '. ' + c.key : ''}">${c.label}${c.key ? html`<kbd>${c.key}</kbd>` : ''}</button>`)}</div>
      ${off.map(c => html`<p class="why off"><b>${c.label}</b> is off: ${offWhy(c, o)}</p>`)}
      <h3>Same thing from the CLI</h3>${on.filter(c => noCli(cliOf(c, o))).map(c => html`<p class="why"><b>${c.label}</b> has no CLI${c.cli ? `: ${cliOf(c, o).replace(/^no (CLI|sd verb):?\s*/, '')}` : '; it is dashboard only'}.</p>`)}${on.filter(c => !noCli(cliOf(c, o))).map(c => html`<div class="cli fold"><button class="cli-line" type="button" aria-expanded="false" title="Show the full command"><code>${cliOf(c, o)}</code></button><button class="icon-btn" type="button" data-copy="${cliOf(c, o)}" aria-label="Copy: ${cliOf(c, o)}">${ICON('copy')}</button></div>${executes(c, o) ? (c.sends ? html`<p class="why"><b>${c.label}</b> runs here and sends <code>${c.sends(o)}</code>.</p>` : '') : html`<p class="why"><b>${c.label}</b> is copy only: the dashboard does not run this line.</p>`}`)}`;
  }
  // Action menu: opens from ⋯ or the "." key; letters run commands.
  const menuEl = document.createElement('div');
  menuEl.className = 'actmenu'; menuEl.setAttribute('role', 'menu'); menuEl.setAttribute('aria-label', 'Actions'); menuEl.hidden = true;
  body.append(menuEl);
  let menuObj = null, menuReturn = null;
  function openMenu(id, anchor) {
    const o = OBJ.get(id); if (!o) return;
    closeMenu(); menuObj = o; menuReturn = anchor || document.activeElement;
    anchor?.setAttribute?.('aria-expanded', 'true');
    menuEl.setAttribute('aria-label', `Actions for ${o.label}`);
    const on = live(o);
    put(menuEl, html`<p class="label">${o.label}</p>${on.map(c => html`<button role="menuitem" type="button" data-cmd="${c.id}" data-obj="${id}"><span>${c.label}${c.risk === 'confirm' ? '…' : ''}</span><code>${shownCli(c, o)}</code>${c.key ? html`<kbd>${c.key}</kbd>` : ''}</button>`)}
      <button role="menuitem" type="button" data-menu-open="${id}"><span>Open details</span><code></code><kbd>↵</kbd></button>
      <button role="menuitem" type="button" data-menu-ask="${id}"><span>Ask in chat</span><code></code><kbd>c</kbd></button>`);
    menuEl.hidden = false;
    const r = (anchor && anchor.getBoundingClientRect()) || { left: innerWidth / 2 - 150, right: innerWidth / 2 + 150, bottom: innerHeight / 3, top: innerHeight / 3 };
    const w = menuEl.offsetWidth, h = menuEl.offsetHeight;
    menuEl.style.left = `${Math.max(8, Math.min(r.right - w, innerWidth - w - 8))}px`;
    menuEl.style.top = `${r.bottom + h + 8 < innerHeight ? r.bottom + 4 : Math.max(8, r.top - h - 4)}px`;
    menuEl.querySelector('[role="menuitem"]').focus();
  }
  function closeMenu() { if (menuEl.hidden) return; menuEl.hidden = true; menuObj = null; menuReturn?.hasAttribute?.('data-menu-for') && menuReturn.setAttribute('aria-expanded', 'false'); menuReturn?.focus?.(); }
  menuEl.addEventListener('keydown', e => {
    const items = [...menuEl.querySelectorAll('[role="menuitem"]')], i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus(); return; }
    if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); closeMenu(); return; }
    if (e.key === 'c') { e.preventDefault(); menuEl.querySelector('[data-menu-ask]').click(); return; }
    const c = menuObj && live(menuObj).find(x => x.key === e.key);
    if (c) { e.preventDefault(); e.stopPropagation(); const o = menuObj; closeMenu(); run(c, o); }
  });
  document.addEventListener('click', e => {
    const m = e.target.closest('[data-menu-for]');
    if (m) { e.stopPropagation(); menuEl.hidden || menuObj?.id !== m.dataset.menuFor ? openMenu(m.dataset.menuFor, m) : closeMenu(); return; }
    const k = e.target.closest('[data-cmd][data-obj]');
    if (k) { e.stopPropagation(); const o = OBJ.get(k.dataset.obj), c = REG.find(x => x.id === k.dataset.cmd); if (menuEl.contains(k)) closeMenu(); if (o && c) run(c, o); return; }
    const op = e.target.closest('[data-menu-open]');
    if (op) { closeMenu(); document.dispatchEvent(new CustomEvent('shell:open', { detail: op.dataset.menuOpen })); return; }
    const ask = e.target.closest('[data-menu-ask]');
    if (ask) { const o = OBJ.get(ask.dataset.menuAsk); closeMenu(); setContext(o?.label); openChat(); return; }
    const fold = e.target.closest('.cli-line');
    if (fold) { const open = fold.getAttribute('aria-expanded') !== 'true'; fold.setAttribute('aria-expanded', open); fold.title = open ? 'Show one line' : 'Show the full command'; return; }
    const cp = e.target.closest('[data-copy]');
    if (cp) { navigator.clipboard?.writeText(cp.dataset.copy).then(() => toast('Copied.'), () => toast('Copy failed: select the line and copy it.')); return; }
    if (!menuEl.hidden && !menuEl.contains(e.target)) closeMenu();
  }, true);
  // Bulk: x adds the selected row; the bar offers commands that every picked object shares.
  const bulk = document.createElement('div');
  bulk.className = 'bulkbar'; bulk.hidden = true; bulk.setAttribute('role', 'region'); bulk.setAttribute('aria-label', 'Selected rows');
  body.append(bulk);
  function renderBulk() {
    document.dispatchEvent(new CustomEvent('shell:picked', { detail: [...picked] }));
    if (!picked.size) { bulk.hidden = true; return; }
    const objs = [...picked].map(i => OBJ.get(i)), type = objs[0].type;
    const shared = objs.every(o => o.type === type) ? REG.filter(c => c.on === type && c.bulk && objs.every(o => !offWhy(c, o))) : [];
    put(bulk, html`<span><b>${picked.size}</b> selected</span>${shared.length ? shared.map(c => html`<button class="btn quiet sm" type="button" data-bulk="${c.id}">${c.label} ${picked.size}</button>`) : html`<span class="why">No shared command</span>`}<button class="btn quiet sm" type="button" data-bulk-clear>Clear</button>`);
    bulk.hidden = false;
  }
  bulk.addEventListener('click', e => {
    if (e.target.closest('[data-bulk-clear]')) { picked.clear(); return renderBulk(); }
    const b = e.target.closest('[data-bulk]'); if (!b) return;
    const c = REG.find(x => x.id === b.dataset.bulk), objs = [...picked].map(i => OBJ.get(i));
    const group = { id: 'bulk', type: plural(2, c.on).replace(/^2 /, ''), label: plural(objs.length, c.on) };
    // The confirm dialog gets the group, which has no row fields, so consequence is rebuilt from the real objects: one line each, repeats folded.
    const consequence = c.consequence && (() => { const lines = [...new Set(objs.map(o => c.consequence(o)))]; return lines.length > 4 ? `${lines.slice(0, 4).join(' ')} And ${lines.length - 4} more.` : lines.join(' '); });
    const one = { ...c, cli: () => objs.map(o => cliOf(c, o)).join(' && '), run: () => { objs.forEach(o => c.run?.(o)); picked.clear(); renderBulk(); return `${c.label} · ${group.label}`; }, undo: c.undo && (() => objs.forEach(o => c.undo(o))),
      consequence, askFirst: objs.length > 25, risk: c.risk };
    run(one, group);
  });
  const commands = {
    register: (...defs) => { REG.push(...defs); },
    // Every declaration on this page, with a sample CLI from one object of its type: the first object the command is on for
    // and that yields a CLI line (a "no CLI: …" text is a per-object reason, not the command's capability), else the first
    // object it is on for, else the first object. The Commands page and designs/tools/collect-counts.mjs read it.
    list: () => REG.map(c => { const objs = [...OBJ.values()].filter(x => x.type === c.on);
      const on = objs.filter(x => { try { return !offWhy(c, x); } catch { return false; } });
      const o = on.find(x => { try { const l = cliOf(c, x); return l && !/^no (CLI|sd verb)\b/i.test(l); } catch { return false; } }) || on[0] || objs[0]; let cli = '', off = '';
      let runs = typeof c.executes === 'boolean' ? c.executes : false;
      try { cli = o ? cliOf(c, o) : ''; off = o ? offWhy(c, o) : ''; runs = o ? executes(c, o) : runs; } catch { cli = ''; }
      return { id: c.id, on: c.on, label: c.label, key: c.key || '', risk: c.risk || 'safe', bulk: !!c.bulk, primary: !!c.primary, cli, off, executes: runs, objects: objs.length }; }),
    // The ids of the objects a command is live for, in put order; verify.mjs runs each command on one of them.
    targets: id => { const c = REG.find(x => x.id === id); return c ? [...OBJ.values()].filter(o => { try { return o.type === c.on && !offWhy(c, o); } catch { return false; } }).map(o => o.id) : []; },
    put: o => { OBJ.set(o.id, o); return o; },
    get: id => OBJ.get(id),
    select: id => { const changed = id !== selId; selId = id; const o = OBJ.get(id); setContext(o?.label); writeRow(id); if (changed) { details?.querySelector('.asked')?.remove(); renderAsked(); } },
    selected: () => OBJ.get(selId),
    rowActions, bar, openMenu, run,
    pick: id => { const o = OBJ.get(id); if (!o) return; if (!REG.some(c => c.on === o.type && c.bulk)) { toast(`No bulk command for a ${o.type}.`); return; } picked.has(id) ? picked.delete(id) : picked.add(id); renderBulk(); },
  };

  // ---------- Capture (n) ----------
  // One capture everywhere. It files a task or followup, or a note on the selected row.
  const cap = document.createElement('dialog');
  cap.className = 'palette capture'; cap.setAttribute('aria-labelledby', 'cap-h');
  put(cap, html`<h2 id="cap-h"></h2>`);
  body.append(cap); trap(cap);
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe quoting for text a person typed: $(), backticks and \ stay literal
  // openCapture(target): target is the object (or its id) the command was run on; without one, the selected row.
  function openCapture(target, text = '') {
    const o = (typeof target === 'string' ? OBJ.get(target) : target && target.id && OBJ.get(target.id)) || (target && target.type ? target : null) || OBJ.get(selId);
    const about = o && o.item ? o : null;
    put(cap, html`<form method="dialog" class="confirm-body" id="cap-form">
      <h2 id="cap-h">Capture</h2>
      <div class="seg" role="radiogroup" aria-label="Kind">
        <label><input type="radio" name="kind" value="task" checked><span>Task</span></label>
        <label><input type="radio" name="kind" value="followup"><span>Followup</span></label>
        <label${about ? '' : html` class="off" title="Select a row that has an item first"`}><input type="radio" name="kind" value="note"${about ? '' : html` disabled`}><span>Note on #${about ? about.item : '…'}</span></label>
      </div>
      <label class="sr" for="cap-in">Title or note</label>
      <input id="cap-in" class="cap-in" autocomplete="off" placeholder="What needs doing" required>
      <p class="why" id="cap-why"></p>
      <div class="cli"><code id="cap-cli">sd task add ''</code></div>
      <div class="actions"><button class="btn quiet" value="no" formnovalidate>Cancel</button><button class="btn" value="yes">Capture</button></div></form>`);
    const f = cap.querySelector('form'), inp = f.querySelector('#cap-in'), cli = f.querySelector('#cap-cli'), why = f.querySelector('#cap-why');
    const upd = () => {
      const k = f.kind.value, t = shq(inp.value);
      // build: say what POST /api/items stores. Only a followup on a row with an item links to it (followup_of).
      why.textContent = k === 'note' ? `A comment on #${about.item}`
        : k === 'followup' && about ? `A followup of #${about.item}`
        : o ? `Files to the inbox; not linked to ${o.label}` : 'No row selected · files to the inbox';
      cli.textContent = k === 'note' ? `sd task note ${about.item} --kind comment --body ${t}` : `sd task add${k === 'followup' ? ' --kind followup' : ''} ${t}${o && o.item ? ` --followup-of ${o.item}` : ''}`;
    };
    inp.value = text; f.addEventListener('input', upd); upd();
    // build: a page that files captures sets window.SHELL_CAPTURE({ kind, title, item, about }) → Promise<toast text>.
    cap.onclose = () => { if (cap.returnValue === 'yes' && inp.value.trim() && window.SHELL_CAPTURE) { window.SHELL_CAPTURE({ kind: f.kind.value, title: inp.value.trim(), item: about ? about.item : null, about: o }).then(toast, e => toast(String(e.message || e))); return; }
      if (cap.returnValue === 'yes' && inp.value.trim()) { const v = inp.value.trim(); toast(`${f.kind.value === 'note' ? 'Note added' : 'Captured'}: ${v}`, () => { toast('Capture removed. The text is back in Capture.'); openCapture(o, v); }); } };
    modal(cap, inp);
  }

  // ---------- Palette ----------
  // Context first: the selected row's commands, then this page's, then capture, chat and go-to.
  const pal = document.createElement('dialog');
  pal.className = 'palette'; pal.setAttribute('aria-label', 'Commands');
  const extra = window.PAGE_COMMANDS || [];
  put(pal, html`<div class="pal-head">${ICON('search')}<input placeholder="Run, go to, or ask" aria-label="Filter commands" id="pal-in" role="combobox" aria-controls="pal-list" aria-expanded="true"><button class="icon-btn" type="button" id="pal-close" aria-label="Close">${ICON('x')}</button></div><ul role="listbox" id="pal-list" aria-label="Commands"></ul>`);
  body.append(pal); trap(pal);
  const palIn = pal.querySelector('#pal-in'), palList = pal.querySelector('#pal-list');
  let opts = [];
  function buildPal() {
    const o = OBJ.get(selId);
    const grp = t => html`<li role="presentation" class="grp">${t}</li>`;
    put(palList, html`${o && live(o).length ? [grp(`Selected · ${o.label}`), live(o).map(c => html`<li role="option" data-run="${c.id}">${ICON(c.icon || 'play')}<span>${c.label}</span><code>${shownCli(c, o)}</code>${c.key ? html`<kbd>. ${c.key}</kbd>` : ''}</li>`)] : ''
      }${extra.length ? [grp('This page'), extra.map((c, i) => html`<li role="option" data-cmd="${i}">${ICON(c.icon || 'play')}<span>${c.label}</span>${c.key ? html`<kbd>${c.key}</kbd>` : ''}</li>`)] : ''
      }${VIEWS.length ? [grp('Views'), VIEWS.map((v, i) => html`<li role="option" data-view="${i}">${ICON('filter')}<span>View: ${v.name}</span><code>${viewHref(v)}</code></li>`)] : ''
      }${grp('Anywhere')}<li role="option" data-capture>${ICON('plus')}<span>Capture a task, followup or note</span><kbd>n</kbd></li><li role="option" data-chat>${ICON('message-square')}<span>Ask in chat</span><kbd>c</kbd></li>${
      GROUPS.flatMap(([, items]) => items).map(([n, icon, k]) => html`<li role="option" data-go="${n}">${ICON(icon)}<span>Go to ${n}${classic(n) ? ' (classic)' : ''}</span><kbd>g ${k}</kbd></li>`)}${
      Object.keys(SCREENS).length ? [grp('Classic screens'), Object.entries(SCREENS).map(([n, href]) => html`<li role="option" data-href="${href}">${ICON('arrow-up-right')}<span>Open ${n}</span><code>${href}</code></li>`)] : ''}`);
    opts = [...palList.querySelectorAll('[role="option"]')];
    opts.forEach(x => x.addEventListener('click', () => runOpt(x)));
  }
  const vis = () => opts.filter(o => !o.classList.contains('out'));
  const markOpt = o => { opts.forEach(x => x.setAttribute('aria-selected', x === o)); if (o) { o.id ||= `opt-${opts.indexOf(o)}`; palIn.setAttribute('aria-activedescendant', o.id); o.scrollIntoView({ block: 'nearest' }); } };
  function filter() {
    const q = palIn.value.trim().toLowerCase();
    opts.forEach(o => o.classList.toggle('out', !!q && !o.textContent.toLowerCase().includes(q) && !o.hasAttribute('data-chat')));
    palList.querySelectorAll('.grp').forEach(g => { let n = g.nextElementSibling, any = false; while (n && !n.classList.contains('grp')) { if (!n.classList.contains('out')) any = true; n = n.nextElementSibling; } g.classList.toggle('out', !any); });
    markOpt(vis()[0]);
  }
  function runOpt(o) {
    pal.close();
    if (o.dataset.go) return go(o.dataset.go);
    if (o.dataset.href) { location.href = o.dataset.href; return; }
    if (o.dataset.view) { location.href = viewHref(VIEWS[+o.dataset.view]); return; }
    if (o.hasAttribute('data-chat')) { openChat(); if (palIn.value.trim()) send(palIn.value.trim()); return; }
    if (o.hasAttribute('data-capture')) return openCapture();
    if (o.dataset.run) { const s = OBJ.get(selId), c = REG.find(x => x.id === o.dataset.run); return run(c, s); }
    extra[+o.dataset.cmd]?.run?.();
  }
  function openPal() { closeMenu(); buildPal(); palIn.value = ''; filter(); modal(pal, palIn); }
  palIn.addEventListener('input', filter);
  palIn.addEventListener('keydown', e => {
    const v = vis(), i = v.findIndex(o => o.getAttribute('aria-selected') === 'true');
    if (e.key === 'ArrowDown') { e.preventDefault(); markOpt(v[Math.min(i + 1, v.length - 1)]); }
    if (e.key === 'ArrowUp') { e.preventDefault(); markOpt(v[Math.max(i - 1, 0)]); }
    if (e.key === 'Enter' && v[i]) { e.preventDefault(); runOpt(v[i]); }
  });
  pal.querySelector('#pal-close').addEventListener('click', () => pal.close());
  pal.addEventListener('click', e => { if (e.target === pal) pal.close(); });
  rail.querySelector('#open-palette').addEventListener('click', openPal);

  function go(name) {
    if (hrefOf(name)) { location.href = hrefOf(name); return; }
    toast(`${name} is not ${window.SHELL_PAGES ? 'built yet, and has no classic screen' : 'in this mockup round'}.`);
  }

  // ---------- Saved views ----------
  // shell.views([{ name, params: { due: 'overdue' } }, …]): chips in the page's #views slot, each a link that sets those URL params.
  // The current view is the one whose params all match the URL; "All" is current when none of the view keys is set.
  let VIEWS = [];
  function views(list) {
    VIEWS = list || []; const slot = document.getElementById('views'); if (!slot) return;
    const cur = new URLSearchParams(location.search), keys = new Set(VIEWS.flatMap(v => Object.keys(v.params)));
    // A view keeps every param it does not name (the text filter, the sort) and ?row=, which the page drops on load when the
    // view hides that row (shell.reconcile). Only the pager resets: page 3 of one view is not page 3 of another.
    // The drawn href leaves ?row= out, so it does not change with every selection; following a chip adds the row back.
    const keep = row => [...new URLSearchParams(location.search)].filter(([k]) => !keys.has(k) && k !== 'page' && (row || k !== 'row'));
    const href = (params, row) => { const u = new URLSearchParams(keep(row)); Object.entries(params).forEach(([k, v]) => u.set(k, v)); const s = u.toString(); return s ? `?${s}` : location.pathname.split('/').pop(); };
    const isCur = params => Object.entries(params).every(([k, v]) => cur.get(k) === v);
    const none = ![...keys].some(k => cur.has(k));
    viewHref = v => href(v.params, true); // the palette opens a view the way its chip does
    slot.className = 'views'; slot.setAttribute('role', 'navigation'); slot.setAttribute('aria-label', 'Saved views');
    put(slot, html`<span class="label">View</span><a class="chip" href="${href({})}"${none ? html` aria-current="true"` : ''}>All</a>${
      VIEWS.map(v => html`<a class="chip" href="${href(v.params)}"${!none && isCur(v.params) ? html` aria-current="true"` : ''}>${v.name}</a>`)
      }<button class="help" type="button" aria-label="Help: saved views" data-help="<b>A view is a URL.</b> Each chip sets the filters it names and clears the rest; the address bar carries it, so a view can be bookmarked, sent or opened from the palette.">${ICON('circle-help')}</button>`);
    slot.querySelectorAll('.help').forEach(b => b.setAttribute('aria-expanded', 'false'));
    // The row and the filters move after render, so a chip's href is rebuilt from the address at the moment it is followed.
    slot.onclick = e => { const a = e.target.closest('a.chip'); if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return; const v = VIEWS[[...slot.querySelectorAll('a.chip')].indexOf(a) - 1]; e.preventDefault(); location.href = href(v ? v.params : {}, true); };
  }
  let viewHref = v => { const u = new URLSearchParams(); Object.entries(v.params).forEach(([k, val]) => u.set(k, val)); return `?${u}`; };

  // ---------- The page's list: j / k, Esc and ?row= ----------
  // A page declares its list once, window.PAGE_LIST = { rows, select, id, current, clear, when }, and binds none of these keys.
  //   rows()          the row elements in reading order; j / k walk those with a box (getClientRects), so hidden rows are skipped.
  //   select(id, el)  the page's own selection; the shell then scrolls the row into view.
  //   id(el)          the row's id (default el.dataset.id). current() the selected id (default: the row with aria-selected="true").
  //   clear()         Esc when nothing is open to close: clear the page's filter (or cancel a drag); true when there was one.
  //   when()          false while j / k do not apply (Writing's editor view).
  // ?row= is the shell's too: shell.commands.select(id) writes it, shell.row() reads it, shell.row(id) writes it for a selection
  // that is not a command object (a Metrics reading), and shell.url(query) writes a page's own params around it.
  function writeRow(id) {
    const u = new URL(location.href);
    if (id == null || id === '') u.searchParams.delete('row'); else u.searchParams.set('row', id);
    if (u.href !== location.href) try { history.replaceState(history.state, '', u); } catch (_) {}
  }
  function url(query) {
    const p = new URLSearchParams(query), r = new URLSearchParams(location.search).get('row');
    p.delete('row'); if (r) p.set('row', r);
    const s = p.toString();
    try { history.replaceState(history.state, '', `${s ? '?' + s : location.pathname.split('/').pop()}${location.hash}`); } catch (_) {}
  }
  const rowParam = () => new URLSearchParams(location.search).get('row');
  function step(d) {
    const L = window.PAGE_LIST; if (!L || (L.when && !L.when())) return false;
    const idOf = L.id || (el => el.dataset.id);
    const rows = [...L.rows()].filter(el => el.getClientRects().length); if (!rows.length) return false;
    const cur = L.current ? L.current() : (() => { const on = rows.find(el => el.getAttribute('aria-selected') === 'true'); return on ? idOf(on) : null; })();
    const i = rows.findIndex(el => idOf(el) === cur);
    const n = rows[i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + d))];
    L.select(idOf(n), n); n.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return true;
  }

  // ---------- Keys ----------
  const CHORDS = Object.fromEntries(GROUPS.flatMap(([, items]) => items).map(([k2, , k]) => [k, k2]));
  // The key sheet (?): one table, closes on Esc or ?. It lists the shell keys; a page adds its own through window.PAGE_KEYS.
  const keys = document.createElement('dialog');
  keys.className = 'palette keys'; keys.setAttribute('aria-labelledby', 'keys-h');
  const KEYS = [['j / k', 'Next / previous row'], ['↵', 'Open Details for the selected row'], ['.', 'Actions for the selected row'], ['x', 'Select the row for a bulk command'], ['n', 'Capture a task, followup or note'],
    ['u', 'Undo the last undoable action while its toast shows'], ['⌘K', 'Commands'], ['c', 'Chat'], ['[ ]', 'Fold the sections rail / the panel'], ['g then a letter', 'Go to a section'], ['r', 'Reload'], ['?', 'This sheet'], ['Esc', 'Close the top panel, else clear the filter']];
  put(keys, html`<div class="confirm-body"><h2 id="keys-h">Keys</h2><table class="keys-table"><tbody>${KEYS.concat(window.PAGE_KEYS || []).map(([k, w]) => html`<tr><th scope="row"><kbd>${k}</kbd></th><td>${w}</td></tr>`)}</tbody></table><div class="actions"><button class="btn quiet" type="button" id="keys-close">Close</button></div></div>`);
  body.append(keys); trap(keys);
  keys.querySelector('#keys-close').addEventListener('click', () => keys.close());
  keys.addEventListener('click', e => { if (e.target === keys) keys.close(); });
  keys.addEventListener('keydown', e => { if (e.key === '?') { e.preventDefault(); keys.close(); } });
  let chord = 0;
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); pal.open ? pal.close() : openPal(); return; }
    // Esc does one thing: close the topmost overlay (help, action menu, the pane as a bottom sheet, the rail menu), else clear
    // the page's filter through PAGE_LIST.clear(). The docked pane is not an overlay, so a wide Esc goes to the filter.
    if (e.key === 'Escape') {
      if (document.querySelector('dialog[open]')) return; // the dialog closes itself; the pane behind it stays
      if (pinned || !pop.hidden) { hideHelp(); return; } if (!menuEl.hidden) { closeMenu(); return; }
      const sheet = !wide() && pane.hasAttribute('data-open'), menuOpen = rail.hasAttribute('data-menu');
      closePane(); setMenu(false);
      if (sheet || menuOpen) return;
      // A page's field clears its own text on Esc and stops the key there; an empty field reaches here and lets go of focus.
      if (e.target.matches('input, textarea, select, [contenteditable]')) { e.target.blur(); return; }
      if (window.PAGE_LIST?.clear?.()) e.preventDefault();
      return;
    }
    if (e.target.matches('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]') || menuEl.contains(e.target)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    // A pending g-chord takes the next key, whatever it is: "g n" goes to Notes and does not open Capture.
    if (chord && Date.now() - chord < 1500) { chord = 0; if (CHORDS[e.key]) { e.preventDefault(); go(CHORDS[e.key]); return; } }
    if (e.key === 'g') { chord = Date.now(); return; }
    if ((e.key === 'j' || e.key === 'k') && step(e.key === 'j' ? 1 : -1)) { e.preventDefault(); return; }
    // A time cell is a toggle: Enter or Space shows the exact time, again the relative one.
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('time.rel[data-rel]')) { e.preventDefault(); flipTime(e.target); return; }
    if (e.key === 'Enter' && selId && !e.target.matches('a, button, [role="button"], [role="menuitem"], [role="tab"]')) { e.preventDefault(); document.dispatchEvent(new CustomEvent('shell:open', { detail: selId })); return; }
    if (e.key === '.' && selId) { e.preventDefault(); openMenu(selId, document.querySelector(`[data-menu-for="${CSS.escape(selId)}"]`)); return; }
    if (e.key === 'x' && selId) { e.preventDefault(); commands.pick(selId); return; }
    if (e.key === 'n') { e.preventDefault(); openCapture(); return; }
    if (e.key === 'u' && tUndo) { e.preventDefault(); t.querySelector('button')?.click(); return; }
    if (e.key === 'c') { e.preventDefault(); openChat(); }
    if (e.key === 'r') { e.preventDefault(); location.reload(); }
    if (e.key === '[' && matchMedia('(min-width: 900px)').matches) { e.preventDefault(); setRail(!body.dataset.rail); }
    if (e.key === ']') { e.preventDefault(); wide() ? setPane(!body.dataset.pane) : pane.hasAttribute('data-open') ? closePane() : openPane(); }
    if (e.key === '?') { e.preventDefault(); modal(keys, keys.querySelector('#keys-close')); }
  });

  // ---------- Time cells ----------
  // One relative-time renderer for every page. A page writes <time class="rel" datetime="<ISO>"> (optional data-future, data-long,
  // data-empty="never"); the shell fills the text, the exact local and UTC time as a title, and a tab stop, on load and after every
  // re-render. The reference is body[data-observed] (the page's reading time), else now. shell.time(iso, opt) returns a filled cell.
  const observedAt = () => { const o = Date.parse(body.dataset.observed || ''); return isFinite(o) ? o : Date.now(); };
  function relText(iso, { future = false, now = false } = {}) {
    const t = Date.parse(iso || ''); if (!isFinite(t)) return '';
    const base = now ? Date.now() : observedAt();
    const m = Math.round((future ? t - base : base - t) / 60000), a = Math.abs(m);
    if (a < 1) return 'just now';
    const n = a < 60 ? `${a}m` : a < 48 * 60 ? `${Math.round(a / 60)}h` : `${Math.round(a / 1440)}d`;
    return future ? `in ${n}` : `${n} ago`;
  }
  function exactText(iso) {
    const d = new Date(iso);
    return `${d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })} · ${d.toISOString().slice(11, 16)} UTC`;
  }
  function fillTime(el) {
    const iso = el.getAttribute('datetime'), future = el.hasAttribute('data-future');
    if (!isFinite(Date.parse(iso || ''))) { el.textContent = el.dataset.empty || 'none'; el.dataset.rel = el.textContent; el.removeAttribute('tabindex'); return; }
    const label = relText(iso, { future }), exact = exactText(iso);
    el.textContent = el.hasAttribute('data-long') ? `${label} · ${exact}` : label;
    el.title = exact; el.dataset.rel = label; el.dataset.local = exact; el.setAttribute('aria-description', exact); // the title alone is hover-only
    if (!el.hasAttribute('data-long') && !el.closest('button, a')) el.tabIndex = 0; // a time inside a control is read with it
  }
  const hydrate = root => root.querySelectorAll?.('time.rel[datetime]:not([data-rel])').forEach(fillTime);
  // The toggle: a click (or Enter / Space, under Keys) swaps the relative text for the exact time and back. The click stops
  // there, so a time inside a row does not also select the row. A time inside a button or link, or a long cell, is left alone.
  function flipTime(t) { if (!t.dataset.local) return; t.textContent = t.textContent === t.dataset.rel ? t.dataset.local : t.dataset.rel; }
  document.addEventListener('click', e => { const t = e.target.closest?.('time.rel[data-rel]'); if (t && t.dataset.local && !t.hasAttribute('data-long') && !t.closest('button, a')) { e.stopPropagation(); flipTime(t); } }, true);
  function time(iso, { future = false, long = false, empty = '' } = {}) {
    const t = document.createElement('time'); t.className = 'rel'; t.setAttribute('datetime', iso || '');
    if (future) t.dataset.future = ''; if (long) t.dataset.long = ''; if (empty) t.dataset.empty = empty;
    // build: the filled cell comes back as html`…` built from its own attributes, since markup.js is the only sink.
    // Each attribute is named in the literal: markup.js takes no value where a name or a bare attribute goes.
    fillTime(t); return html`<time class="rel" datetime="${iso || ''}"${future ? html` data-future=""` : ''}${long ? html` data-long=""` : ''}${empty ? html` data-empty="${empty}"` : ''} title="${t.title}" aria-description="${t.title}" data-rel="${String(t.dataset.rel || '')}" data-local="${String(t.dataset.local || '')}"${t.hasAttribute('tabindex') ? html` tabindex="0"` : ''}>${t.textContent}</time>`;
  }
  hydrate(document);
  new MutationObserver(recs => recs.forEach(r => r.addedNodes.forEach(n => { if (n.nodeType === 1) { if (n.matches('time.rel[datetime]:not([data-rel])')) fillTime(n); hydrate(n); } }))).observe(body, { childList: true, subtree: true });

  // ---------- Staleness (design.md, Microinteractions) ----------
  // body[data-observed] is the reading time and body[data-interval] the collector's interval in minutes. Past twice the interval
  // the Observed cell (#refresh) turns caution and says "stale since …"; past five times it is unknown. It is checked against
  // real now each minute and after a re-render. A page without data-interval gets no state: the shell does not guess a cadence.
  function staleness() {
    const cell = document.getElementById('refresh'), obs = Date.parse(body.dataset.observed || ''), iv = +body.dataset.interval * 60000;
    if (!cell || !isFinite(obs) || !(iv > 0)) return;
    const age = Date.now() - obs, state = age > 5 * iv ? 'unknown' : age > 2 * iv ? 'caution' : '';
    const d = new Date(obs + 2 * iv).toISOString(), text = state ? `${state === 'unknown' ? 'unknown · ' : ''}stale since ${d.slice(5, 10)} ${d.slice(11, 16)} UTC` : '';
    const was = cell.querySelector('.stale');
    if ((was?.textContent || '') === text) return; // the text names the state, so the same text means nothing changed
    was?.remove();
    if (!state) { if (cell.dataset.stale === '') { delete cell.dataset.state; delete cell.dataset.stale; } return; }
    cell.dataset.state = state; cell.dataset.stale = '';
    put(cell.querySelector('.val') || cell, html`<small class="stale">${text}</small>`, 'append');
  }
  staleness(); addEventListener('load', staleness); setInterval(staleness, 60 * 1000);

  // ---------- Since you last looked ----------
  // A page marks rows with data-changed="<ISO>". The shell keeps the newest time seen per page (localStorage) and, on the next visit,
  // lights rows newer than it and puts a band above the list. ?since=<ISO> sets the mark for one load (screenshots, links).
  const sinceKey = `sd.seen.${page}`;
  const sinceParam = new URLSearchParams(location.search).get('since');
  let seenMark = sinceParam || store(sinceKey) || '';
  const band = document.createElement('p');
  band.className = 'since'; band.setAttribute('role', 'status'); band.hidden = true;
  // A new row says so in words as well as with the accent bar: colour alone is not a state (review 2026-09-29, 25).
  const newMark = (r, on) => {
    const host = r.matches('tr') ? r.querySelector('td, th') : r, was = host?.querySelector(':scope > .sr[data-new-sr]');
    if (on && host && !was) put(host, html`<span class="sr" data-new-sr>new</span>`, 'prepend');
    if (!on && was) was.remove();
  };
  function markNew() {
    const rows = [...main.querySelectorAll('[data-changed]')]; if (!rows.length) return;
    const newest = rows.map(r => r.dataset.changed).sort().pop();
    let n = 0;
    rows.forEach(r => { const isNew = !!seenMark && r.dataset.changed > seenMark; if (r.hasAttribute('data-new') !== isNew) r.toggleAttribute('data-new', isNew); newMark(r, isNew); if (isNew) n++; });
    if (!band.isConnected) { const first = rows[0].closest('table, ul, ol, .board, section') || rows[0].parentElement; first.parentElement.insertBefore(band, first); }
    if (!n) { band.hidden = true; delete body.dataset.onlyNew; return; }
    const only = body.dataset.onlyNew === '', said = `${n}|${only}|${relText(seenMark, { now: true })}`;
    // The band is a status: rewrite it only when what it says changed, or every re-render announces it again (review 28).
    if (band.dataset.said === said && !band.hidden) return;
    band.dataset.said = said;
    put(band, html`${ICON('eye')}<b>${n}</b> ${markup.plural.word(n, 'row')} changed since you last looked (${relText(seenMark, { now: true })}) · <button class="linkbtn" type="button" data-since="only" aria-pressed="${String(only)}">${only ? 'Show all' : 'Only these'}</button> · <button class="linkbtn" type="button" data-since="seen">Mark seen</button>`);
    band.hidden = false;
    band.dataset.newest = newest;
  }
  band.addEventListener('click', e => {
    const b = e.target.closest('[data-since]'); if (!b) return;
    if (b.dataset.since === 'only') { body.dataset.onlyNew === '' ? delete body.dataset.onlyNew : body.dataset.onlyNew = ''; markNew(); }
    if (b.dataset.since === 'seen') { seenMark = band.dataset.newest; store(sinceKey, seenMark); delete body.dataset.onlyNew; markNew(); toast('Marked seen. New rows light again when something changes.'); }
  });
  if (main) {
    let raf = 0; new MutationObserver(recs => { if (recs.every(r => band.contains(r.target))) return; cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { markNew(); staleness(); }); }).observe(main, { childList: true, subtree: true });
    markNew();
    // Leaving the page records the newest change seen, so the next visit lights only what came after.
    addEventListener('pagehide', () => { const newest = [...main.querySelectorAll('[data-changed]')].map(r => r.dataset.changed).sort().pop(); if (newest && !sinceParam && newest > (store(sinceKey) || '')) store(sinceKey, newest); });
  }

  // ---------- Reload ----------
  // A Home Screen web app has no address bar and no pull-to-refresh, so the page carries its own reload.
  // It also reloads on return when it sat hidden for 5 minutes or more, so a resumed app never shows a stale screen.
  rail.querySelector('#reload').addEventListener('click', () => location.reload());
  let hiddenAt = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) hiddenAt = Date.now();
    else if (hiddenAt && Date.now() - hiddenAt >= 5 * 60 * 1000) location.reload();
  });

  // ---------- Selection follows the filter (design.md, Page contract) ----------
  // A list's selection is always a row the viewer can see. Call after any filter, search, page or view change.
  // rows: the list's row elements. current: the selected id (default: the row with aria-selected="true").
  // select(id) is the page's own; clear() only says why nothing is selected. Returns the selected id or null.
  // keep(id): true for a selection that is not a list row (Contributions' collector, Today's briefs); the filter leaves it alone.
  function reconcile({ rows, current, select, clear, keep, id = el => el.dataset.id }) {
    const all = [...rows], vis = all.filter(el => el.getClientRects().length);
    if (current === undefined) { const on = all.find(el => el.getAttribute('aria-selected') === 'true'); current = on ? id(on) : null; }
    if (current != null && keep && keep(current)) return current;
    if (current != null && vis.some(el => id(el) === current)) return current;
    if (vis.length) { const next = id(vis[0]); select(next); return next; }
    all.forEach(el => el.setAttribute('aria-selected', 'false'));
    commands.select(null);
    if (clear) clear();
    return null;
  }

  // ---------- The state slot: loading, error, partial read, empty (review 2026-09-29, 20) ----------
  // One slot per page, #state, right under the head. A page never draws these itself: it says which one holds and why, with
  // shell.state({ kind, text, source }), or window.PAGE_STATE before shell.js runs; shell.state(null) empties the slot. A page
  // without #state in its markup gets one. The build sets 'loading' before its fetch and 'error' or 'partial' when a read fails,
  // so a collector that fails renders as a failure and never as a clean page. The glyph follows the state grammar.
  const STATES = { loading: ['queued', 'Loading'], error: ['unknown', 'Read failed'], partial: ['caution', 'Partial read'], empty: ['ok', 'Nothing here'] };
  const GLYPHS = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  let stateSlot = document.getElementById('state');
  if (!stateSlot && main !== body) { stateSlot = document.createElement('div'); stateSlot.id = 'state'; (main.querySelector(':scope > header.head') || main.firstElementChild)?.after(stateSlot); }
  // A section with a reading of its own (Today's day timeline, sd:2180) names its slot: shell.state(s, 'state-timeline') draws the
  // same grammar in that element, so an empty or failed section never draws its own empty state or a chart of nothing.
  function state(s, at) {
    const slot = at ? document.getElementById(at) : stateSlot;
    if (!slot) return;
    slot.className = 'state'; slot.setAttribute('role', 'status');
    const k = s && STATES[s.kind];
    if (!k) { slot.hidden = true; slot.replaceChildren(); delete slot.dataset.kind; return; }
    slot.hidden = false; slot.dataset.kind = s.kind;
    put(slot, html`<span class="g-${k[0]}" aria-hidden="true">${GLYPHS[k[0]]}</span><b>${s.title || k[1]}</b><span>${s.text || ''}</span>${s.source ? html`<code>${s.source}</code>` : ''}`);
  }
  // Screenshot and review hooks: #loading, #error and #empty show each variant on any page.
  const hookState = { '#loading': { kind: 'loading', text: 'Reading the collector. Rows appear when it answers.' },
    '#error': { kind: 'error', text: 'The collector did not answer, so nothing below is current. Reload retries it.' },
    '#empty': { kind: 'empty', text: 'The collector answered with no rows.' } }[location.hash];
  state(hookState || window.PAGE_STATE || null);

  // shell.ready settles after load and two frames, when every page script and the shell have drawn; 'shell:ready' fires then.
  // verify.mjs waits on it instead of a fixed sleep (sd:2137).
  const ready = new Promise(done => { const go = () => requestAnimationFrame(() => requestAnimationFrame(() => { document.dispatchEvent(new Event('shell:ready')); done(); }));
    document.readyState === 'complete' ? go() : addEventListener('load', go, { once: true }); });
  window.shell = { ready, ICON, plural, state, chording: () => !!chord && Date.now() - chord < 1500, openPane, closePane, showTab, openChat, setContext, suggest, send, toast, confirm: a => confirmAction(a).then(r => r.yes), commands, capture: openCapture, shq, time, attention, views, reconcile, url, row: (...a) => a.length ? writeRow(a[0]) : rowParam(), pages: PAGES, groups: GROUPS };
  if (location.hash === '#chat') openChat(); // screenshot hook
  if (location.hash === '#sheet') openPane('tab-details');
  if (location.hash === '#menu') setMenu(true);
})();
