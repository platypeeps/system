// v2 shell: grouped rail, right pane (Details | Chat), help popovers, palette, toast, keys.
// Mockup behaviour only. The build replaces the chat stub with the runner-backed service.
// Build port (local-project-dashboard, sd:2110): ui-design products/system/designs/v2/shell.js at a3861c9.
// Changes from the reference, each marked "build:": the page map comes from window.SHELL_PAGES, the rail's
// narrow-screen rule moved to shell.css and the favicon count is off (the dashboard CSP is default-src 'self',
// which refuses an inline <style> and a data: icon), capture calls window.SHELL_CAPTURE when a page sets it,
// and the chat stub says that no chat backend is connected.
// build: every string of markup is html`…` from markup.js, which escapes each value put in it; nodes() and put() are
// the only way in, so the hand escaping is gone. rowActions, bar, time and ICON return html`…` for a page to compose.
// Help text renders <b> and <code> only.
// A page sets <body data-page="Today" data-scope="fleet" data-scope-note="…"> and may call window.shell.*.
(() => {
  const { html, nodes, put } = window.markup;
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
        const href = PAGES[name] || '#';
        return html`<li><a href="${href}" data-section="${name}"${name === page ? html` aria-current="page"` : ''}${PAGES[name] ? '' : html` data-unbuilt`}>${ICON(icon)}<span class="nm">${name}</span>${badge(counts[name])}<kbd>g ${key}</kbd></a></li>`;
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
  // build: the rule that hid one theme copy per width was an injected <style>; it is in shell.css now.

  // A page sets window.PAGE_ATTENTION = { state, n, what } and calls shell.attention() when it changes.
  const attention = a => {
    if (a) window.PAGE_ATTENTION = a;
    a = window.PAGE_ATTENTION;
    const link = rail.querySelector(`[data-section="${page}"]`);
    if (!a || !link) return;
    link.querySelector('.count')?.remove();
    link.querySelector('kbd').before(nodes(badge(a)));
    titleCount(a.state === 'warning' ? a.n : 0);
  };
  // Warnings show in the tab: "(3) Today · system" and a count drawn on the favicon (patterns.md, Density).
  const baseTitle = document.title.replace(/^\(\d+\) /, '');
  function titleCount(n) {
    document.title = n ? `(${n}) ${baseTitle}` : baseTitle;
    let icon = document.querySelector('link[rel="icon"]');
    if (!icon) { icon = document.createElement('link'); icon.rel = 'icon'; document.head.append(icon); }
    const c = document.createElement('canvas'); c.width = c.height = 32; const g = c.getContext('2d');
    const cs = getComputedStyle(document.documentElement);
    g.fillStyle = n ? cs.getPropertyValue('--color-warning').trim() || '#c33' : cs.getPropertyValue('--color-ink-2').trim() || '#888';
    g.beginPath(); g.arc(16, 16, 15, 0, Math.PI * 2); g.fill();
    g.fillStyle = cs.getPropertyValue('--color-paper').trim() || '#000'; g.font = `700 ${n > 9 ? 17 : 20}px ${cs.getPropertyValue('--font-data') || 'monospace'}`;
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
    if (a) { e.preventDefault(); toast(`${a.dataset.section} is not ${window.SHELL_PAGES ? 'built in v2 yet' : 'in this mockup round'}.`); }
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
        <div class="thread" id="thread" aria-live="polite"></div>
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
  const openPane = tab => { if (tab) showTab(tab); if (body.dataset.pane) setPane(false); if (!pane.contains(document.activeElement)) paneReturn = document.activeElement; pane.setAttribute('data-open', ''); };
  // Close puts focus back on the row (or button) that opened the sheet, so a keyboard user does not restart at the top.
  const closePane = () => { const was = pane.hasAttribute('data-open'); pane.removeAttribute('data-open'); if (was && paneReturn?.isConnected && !wide()) paneReturn.focus?.(); };
  pane.querySelector('#close-pane').addEventListener('click', closePane);
  function openChat() { openPane('tab-chat'); document.getElementById('chat-input').focus(); }
  rail.querySelector('#open-chat').addEventListener('click', openChat);

  const thread = pane.querySelector('#thread');
  const pin = pane.querySelector('#pin-scope');
  pin.addEventListener('click', () => pin.setAttribute('aria-pressed', pin.getAttribute('aria-pressed') !== 'true'));
  function setContext(text) { if (pin.getAttribute('aria-pressed') !== 'true') pane.querySelector('#scope-ctx').textContent = text ? `+ ${text}` : ''; }
  function suggest(list) {
    put(thread, html`<div class="msg"><span class="label">Ask in ${scope}</span></div>
      <div class="suggest">${list.map(s => html`<button type="button">${ICON('message-square')}<span>${s}</span></button>`)}</div>`);
    thread.querySelectorAll('.suggest button').forEach(b => b.addEventListener('click', () => { send(b.textContent.trim()); }));
  }
  function send(text) {
    if (!text) return;
    // build: no chat backend is connected yet; the answer says so instead of pretending.
    const answer = window.SHELL_PAGES ? `No chat backend is connected to this dashboard yet, so nothing was sent. A later slice sends this to claude -p in the ${scope} scope.`
      : `Mockup: no chat backend here. The build sends this to claude -p in the ${scope} scope and streams the answer.`;
    thread.append(nodes(html`<div class="msg"><span class="who label">${ICON('user')}You</span><p>${text}</p></div>
      <div class="msg"><span class="who label">${ICON('bot')}Claude · ${scope}</span><p class="why">${answer}</p></div>`));
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
    details.append(nodes(html`<section class="asked"><h3>Asked here</h3>${list.map(x => html`<div class="qa"><p class="q">${ICON('user')}<span>${x.q}</span></p><p class="a why">${x.a}</p></div>`)}<p class="why">${o ? `About ${o.label} · ` : ''}this session, newest first.</p></section>`));
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
  // build: help text is page markup held in an attribute; <b> and <code> render and anything else shows as written.
  const helpText = s => { const out = []; let at = 0;
    s.replace(/<(b|code)>([^<]*)<\/\1>/g, (m, tag, inner, i) => { out.push(s.slice(at, i), tag === 'b' ? html`<b>${inner}</b>` : html`<code>${inner}</code>`); at = i + m.length; return m; });
    out.push(s.slice(at)); return html`${out}`; };
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
  const t = document.createElement('div');
  t.className = 'toast'; t.hidden = true; t.setAttribute('role', 'status');
  body.append(t);
  let tTimer = 0, tUndo = null;
  function toast(msg, undo) {
    put(t, html`<span></span>${undo ? html`<button class="btn quiet sm" type="button" aria-keyshortcuts="u">${ICON('undo-2')}Undo<kbd>u</kbd></button>` : ''}`);
    t.firstChild.textContent = msg; t.hidden = false; tUndo = undo || null;
    if (undo) t.querySelector('button').addEventListener('click', () => { t.hidden = true; tUndo = null; undo(); });
    clearTimeout(tTimer); tTimer = setTimeout(() => { t.hidden = true; tUndo = null; }, undo ? 10000 : 4000);
  }

  // ---------- Commands ----------
  // One declaration per command; the row button, the action menu, the Details bar and the palette all read it.
  // See products/system/commands.md. A page registers commands and puts its row objects:
  //   shell.commands.register({ id, on, label, key, cli: o => '…', risk: 'safe'|'undo'|'confirm', primary: o => bool, when: o => true|'reason', run: o => 'result', undo: o => {} })
  //   shell.commands.put({ id, type, label, …facts })   shell.commands.select(id)
  const REG = [], OBJ = new Map(), picked = new Set();
  let selId = null;
  // A command without cli is dashboard-only (chat, navigation); it shows no CLI line.
  const cliOf = (c, o) => c.cli ? c.cli(o) : '';
  // A cli string that starts with "no CLI:" or "no sd verb:" is a reason, not a command.
  const noCli = s => !s || /^no (CLI|sd verb)\b/.test(s);
  const shownCli = (c, o) => { const s = cliOf(c, o); return noCli(s) ? '' : s; };
  const offWhy = (c, o) => { const w = c.when ? c.when(o) : true; return w === true ? '' : (w || 'not available'); };
  const cmdsFor = o => o ? REG.filter(c => c.on === o.type) : [];
  const live = o => cmdsFor(o).filter(c => !offWhy(c, o));
  const confirmDlg = document.createElement('dialog');
  confirmDlg.className = 'palette confirm'; confirmDlg.setAttribute('aria-labelledby', 'confirm-h');
  put(confirmDlg, html`<h2 id="confirm-h"></h2>`); // the label target exists while the dialog is closed
  body.append(confirmDlg);
  // Confirm: one dialog for every irreversible, force or large bulk action. It names the target and shows the command.
  function confirmAction({ title, body: text = '', cli = '', ok = 'Confirm', keep = 'Keep it', danger = true }) {
    return new Promise(done => {
      put(confirmDlg, html`<form method="dialog" class="confirm-body">
        <h2 id="confirm-h">${title}</h2>${text ? html`<p>${text}</p>` : ''}
        ${cli ? html`<div class="cli"><code>${cli}</code></div>` : ''}
        <div class="actions"><button class="btn quiet" value="no">${keep}</button><button class="btn${danger ? ' danger' : ''}" value="yes">${ok}</button></div></form>`);
      confirmDlg.returnValue = '';
      confirmDlg.onclose = () => done(confirmDlg.returnValue === 'yes');
      confirmDlg.showModal(); confirmDlg.querySelector('[value="no"]').focus();
    });
  }
  function run(c, o) {
    const go = () => {
      const msg = c.run ? c.run(o) : '';
      if (msg === null) return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } })); // opened a form
      const text = msg || `${c.label} · ${o.label}`;
      if (c.risk === 'undo') toast(text, () => { c.undo?.(o); toast(`${c.label} undone.`); });
      else toast(text);
      document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } }));
    };
    if (c.risk !== 'confirm' && !c.askFirst) return go();
    confirmAction({ title: `${c.label} ${o.label}?`, body: c.consequence ? c.consequence(o) : '', cli: shownCli(c, o), ok: c.label, danger: c.risk === 'confirm' }).then(yes => yes && go());
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
      <h3>Same thing from the CLI</h3>${on.filter(c => noCli(cliOf(c, o))).map(c => html`<p class="why"><b>${c.label}</b> has no CLI${c.cli ? `: ${cliOf(c, o).replace(/^no (CLI|sd verb):?\s*/, '')}` : '; it is dashboard only'}.</p>`)}${on.filter(c => !noCli(cliOf(c, o))).map(c => html`<div class="cli fold"><button class="cli-line" type="button" aria-expanded="false" title="Show the full command"><code>${cliOf(c, o)}</code></button><button class="icon-btn" type="button" data-copy="${cliOf(c, o)}" aria-label="Copy: ${cliOf(c, o)}">${ICON('copy')}</button></div>`)}`;
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
    const group = { id: 'bulk', type: c.on, label: `${objs.length} ${c.on}s` };
    // The confirm dialog gets the group, which has no row fields, so consequence is rebuilt from the real objects: one line each, repeats folded.
    const consequence = c.consequence && (() => { const lines = [...new Set(objs.map(o => c.consequence(o)))]; return lines.length > 4 ? `${lines.slice(0, 4).join(' ')} And ${lines.length - 4} more.` : lines.join(' '); });
    const one = { ...c, cli: () => objs.map(o => cliOf(c, o)).join(' && '), run: () => { objs.forEach(o => c.run?.(o)); picked.clear(); renderBulk(); return `${c.label} · ${objs.length} ${c.on}s`; }, undo: () => objs.forEach(o => c.undo?.(o)),
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
      try { cli = o ? cliOf(c, o) : ''; off = o ? offWhy(c, o) : ''; } catch { cli = ''; }
      return { id: c.id, on: c.on, label: c.label, key: c.key || '', risk: c.risk || 'safe', bulk: !!c.bulk, primary: !!c.primary, cli, off, objects: objs.length }; }),
    put: o => { OBJ.set(o.id, o); return o; },
    get: id => OBJ.get(id),
    select: id => { const changed = id !== selId; selId = id; const o = OBJ.get(id); setContext(o?.label); if (changed) { details?.querySelector('.asked')?.remove(); renderAsked(); } },
    selected: () => OBJ.get(selId),
    rowActions, bar, openMenu, run,
    pick: id => { const o = OBJ.get(id); if (!o) return; if (!REG.some(c => c.on === o.type && c.bulk)) { toast(`No bulk command for a ${o.type}.`); return; } picked.has(id) ? picked.delete(id) : picked.add(id); renderBulk(); },
  };

  // ---------- Capture (n) ----------
  // One capture everywhere. It files a task or followup, or a note on the selected row.
  const cap = document.createElement('dialog');
  cap.className = 'palette capture'; cap.setAttribute('aria-labelledby', 'cap-h');
  put(cap, html`<h2 id="cap-h"></h2>`);
  body.append(cap);
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe quoting for text a person typed: $(), backticks and \ stay literal
  // openCapture(target): target is the object (or its id) the command was run on; without one, the selected row.
  function openCapture(target) {
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
      ${o ? html`<p class="why">About: <b>${o.label}</b> · linked as its source</p>` : html`<p class="why">No row selected · files to the inbox</p>`}
      <div class="cli"><code id="cap-cli">sd task add ''</code></div>
      <div class="actions"><button class="btn quiet" value="no" formnovalidate>Cancel</button><button class="btn" value="yes">Capture</button></div></form>`);
    const f = cap.querySelector('form'), inp = f.querySelector('#cap-in'), cli = f.querySelector('#cap-cli');
    const upd = () => {
      const k = f.kind.value, t = shq(inp.value);
      cli.textContent = k === 'note' ? `sd task note ${about.item} --kind comment --body ${t}` : `sd task add${k === 'followup' ? ' --kind followup' : ''} ${t}${o && o.item ? ` --followup-of ${o.item}` : ''}`;
    };
    f.addEventListener('input', upd); upd();
    // build: a page that files captures sets window.SHELL_CAPTURE({ kind, title, item, about }) → Promise<toast text>.
    cap.onclose = () => { if (cap.returnValue === 'yes' && inp.value.trim() && window.SHELL_CAPTURE) { window.SHELL_CAPTURE({ kind: f.kind.value, title: inp.value.trim(), item: about ? about.item : null, about: o }).then(toast, e => toast(String(e.message || e))); return; }
      if (cap.returnValue === 'yes' && inp.value.trim()) toast(`${f.kind.value === 'note' ? 'Note added' : 'Captured'}: ${inp.value.trim()}`, () => toast('Capture removed.')); };
    cap.showModal(); inp.focus();
  }

  // ---------- Palette ----------
  // Context first: the selected row's commands, then this page's, then capture, chat and go-to.
  const pal = document.createElement('dialog');
  pal.className = 'palette'; pal.setAttribute('aria-label', 'Commands');
  const extra = window.PAGE_COMMANDS || [];
  put(pal, html`<div class="pal-head">${ICON('search')}<input placeholder="Run, go to, or ask" aria-label="Filter commands" id="pal-in" role="combobox" aria-controls="pal-list" aria-expanded="true"><button class="icon-btn" type="button" id="pal-close" aria-label="Close">${ICON('x')}</button></div><ul role="listbox" id="pal-list" aria-label="Commands"></ul>`);
  body.append(pal);
  const palIn = pal.querySelector('#pal-in'), palList = pal.querySelector('#pal-list');
  let opts = [];
  function buildPal() {
    const o = OBJ.get(selId);
    const grp = t => html`<li role="presentation" class="grp">${t}</li>`;
    put(palList, html`${o && live(o).length ? [grp(`Selected · ${o.label}`), live(o).map(c => html`<li role="option" data-run="${c.id}">${ICON(c.icon || 'play')}<span>${c.label}</span><code>${shownCli(c, o)}</code>${c.key ? html`<kbd>. ${c.key}</kbd>` : ''}</li>`)] : ''
      }${extra.length ? [grp('This page'), extra.map((c, i) => html`<li role="option" data-cmd="${i}">${ICON(c.icon || 'play')}<span>${c.label}</span>${c.key ? html`<kbd>${c.key}</kbd>` : ''}</li>`)] : ''
      }${VIEWS.length ? [grp('Views'), VIEWS.map((v, i) => html`<li role="option" data-view="${i}">${ICON('filter')}<span>View: ${v.name}</span><code>${viewHref(v)}</code></li>`)] : ''
      }${grp('Anywhere')}<li role="option" data-capture>${ICON('plus')}<span>Capture a task, followup or note</span><kbd>n</kbd></li><li role="option" data-chat>${ICON('message-square')}<span>Ask in chat</span><kbd>c</kbd></li>${
      GROUPS.flatMap(([, items]) => items).map(([n, icon, k]) => html`<li role="option" data-go="${n}">${ICON(icon)}<span>Go to ${n}</span><kbd>g ${k}</kbd></li>`)}`);
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
    if (o.dataset.view) { location.search = viewHref(VIEWS[+o.dataset.view]); return; }
    if (o.hasAttribute('data-chat')) { openChat(); if (palIn.value.trim()) send(palIn.value.trim()); return; }
    if (o.hasAttribute('data-capture')) return openCapture();
    if (o.dataset.run) { const s = OBJ.get(selId), c = REG.find(x => x.id === o.dataset.run); return run(c, s); }
    extra[+o.dataset.cmd]?.run?.();
  }
  function openPal() { closeMenu(); buildPal(); palIn.value = ''; filter(); pal.showModal(); palIn.focus(); }
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
    if (PAGES[name]) { location.href = PAGES[name]; return; }
    toast(`${name} is not ${window.SHELL_PAGES ? 'built in v2 yet' : 'in this mockup round'}.`);
  }

  // ---------- Saved views ----------
  // shell.views([{ name, params: { due: 'overdue' } }, …]): chips in the page's #views slot, each a link that sets those URL params.
  // The current view is the one whose params all match the URL; "All" is current when none of the view keys is set.
  let VIEWS = [];
  function views(list) {
    VIEWS = list || []; const slot = document.getElementById('views'); if (!slot) return;
    const cur = new URLSearchParams(location.search), keys = new Set(VIEWS.flatMap(v => Object.keys(v.params)));
    const keep = [...cur].filter(([k]) => !keys.has(k) && k !== 'page' && k !== 'row');
    const href = params => { const u = new URLSearchParams(keep); Object.entries(params).forEach(([k, v]) => u.set(k, v)); const s = u.toString(); return s ? `?${s}` : location.pathname.split('/').pop(); };
    const isCur = params => Object.entries(params).every(([k, v]) => cur.get(k) === v);
    const none = ![...keys].some(k => cur.has(k));
    slot.className = 'views'; slot.setAttribute('role', 'navigation'); slot.setAttribute('aria-label', 'Saved views');
    put(slot, html`<span class="label">View</span><a class="chip" href="${href({})}"${none ? html` aria-current="true"` : ''}>All</a>${
      VIEWS.map(v => html`<a class="chip" href="${href(v.params)}"${!none && isCur(v.params) ? html` aria-current="true"` : ''}>${v.name}</a>`)
      }<button class="help" type="button" aria-label="Help: saved views" data-help="<b>A view is a URL.</b> Each chip sets the filters it names and clears the rest; the address bar carries it, so a view can be bookmarked, sent or opened from the palette.">${ICON('circle-help')}</button>`);
    slot.querySelectorAll('.help').forEach(b => b.setAttribute('aria-expanded', 'false'));
  }
  const viewHref = v => { const u = new URLSearchParams(); Object.entries(v.params).forEach(([k, val]) => u.set(k, val)); return `?${u}`; };

  // ---------- Keys ----------
  const CHORDS = Object.fromEntries(GROUPS.flatMap(([, items]) => items).map(([k2, , k]) => [k, k2]));
  // The key sheet (?): one table, closes on Esc or ?. It lists the shell keys; a page adds its own through window.PAGE_KEYS.
  const keys = document.createElement('dialog');
  keys.className = 'palette keys'; keys.setAttribute('aria-labelledby', 'keys-h');
  const KEYS = [['j / k', 'Next / previous row'], ['↵', 'Open Details for the selected row'], ['.', 'Actions for the selected row'], ['x', 'Select the row for a bulk command'], ['n', 'Capture a task, followup or note'],
    ['u', 'Undo the last undoable action while its toast shows'], ['⌘K', 'Commands'], ['c', 'Chat'], ['[ ]', 'Fold the sections rail / the panel'], ['g then a letter', 'Go to a section'], ['r', 'Reload'], ['?', 'This sheet'], ['Esc', 'Close']];
  put(keys, html`<div class="confirm-body"><h2 id="keys-h">Keys</h2><table class="keys-table"><tbody>${KEYS.concat(window.PAGE_KEYS || []).map(([k, w]) => html`<tr><th scope="row"><kbd>${k}</kbd></th><td>${w}</td></tr>`)}</tbody></table><div class="actions"><button class="btn quiet" type="button" id="keys-close">Close</button></div></div>`);
  body.append(keys);
  keys.querySelector('#keys-close').addEventListener('click', () => keys.close());
  keys.addEventListener('click', e => { if (e.target === keys) keys.close(); });
  keys.addEventListener('keydown', e => { if (e.key === '?') { e.preventDefault(); keys.close(); } });
  let chord = 0;
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); pal.open ? pal.close() : openPal(); return; }
    if (e.key === 'Escape') {
      if (document.querySelector('dialog[open]')) return; // the dialog closes itself; the pane behind it stays
      if (pinned || !pop.hidden) { hideHelp(); return; } if (!menuEl.hidden) { closeMenu(); return; } closePane(); setMenu(false); return;
    }
    if (e.target.matches('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]') || menuEl.contains(e.target)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    // A pending g-chord takes the next key, whatever it is: "g n" goes to Notes and does not open Capture.
    if (chord && Date.now() - chord < 1500) { chord = 0; if (CHORDS[e.key]) { e.preventDefault(); go(CHORDS[e.key]); return; } }
    if (e.key === 'g') { chord = Date.now(); return; }
    if (e.key === 'Enter' && selId && !e.target.matches('a, button, [role="button"], [role="menuitem"], [role="tab"]')) { e.preventDefault(); document.dispatchEvent(new CustomEvent('shell:open', { detail: selId })); return; }
    if (e.key === '.' && selId) { e.preventDefault(); openMenu(selId, document.querySelector(`[data-menu-for="${CSS.escape(selId)}"]`)); return; }
    if (e.key === 'x' && selId) { e.preventDefault(); commands.pick(selId); return; }
    if (e.key === 'n') { e.preventDefault(); openCapture(); return; }
    if (e.key === 'u' && tUndo) { e.preventDefault(); t.querySelector('button')?.click(); return; }
    if (e.key === 'c') { e.preventDefault(); openChat(); }
    if (e.key === 'r') { e.preventDefault(); location.reload(); }
    if (e.key === '[' && matchMedia('(min-width: 900px)').matches) { e.preventDefault(); setRail(!body.dataset.rail); }
    if (e.key === ']') { e.preventDefault(); wide() ? setPane(!body.dataset.pane) : pane.hasAttribute('data-open') ? closePane() : openPane(); }
    if (e.key === '?') { e.preventDefault(); keys.showModal(); }
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
    el.title = exact; el.dataset.rel = label; el.dataset.local = exact;
    if (!el.hasAttribute('data-long') && !el.closest('button, a')) el.tabIndex = 0; // a time inside a control is read with it
  }
  const hydrate = root => root.querySelectorAll?.('time.rel[datetime]:not([data-rel])').forEach(fillTime);
  function time(iso, { future = false, long = false, empty = '' } = {}) {
    const t = document.createElement('time'); t.className = 'rel'; t.setAttribute('datetime', iso || '');
    if (future) t.dataset.future = ''; if (long) t.dataset.long = ''; if (empty) t.dataset.empty = empty;
    // build: the filled cell comes back as html`…` built from its own attributes, since markup.js is the only sink.
    fillTime(t); return html`<time${[...t.attributes].map(a => html` ${a.name}="${a.value}"`)}>${t.textContent}</time>`;
  }
  hydrate(document);
  new MutationObserver(recs => recs.forEach(r => r.addedNodes.forEach(n => { if (n.nodeType === 1) { if (n.matches('time.rel[datetime]:not([data-rel])')) fillTime(n); hydrate(n); } }))).observe(body, { childList: true, subtree: true });

  // ---------- Since you last looked ----------
  // A page marks rows with data-changed="<ISO>". The shell keeps the newest time seen per page (localStorage) and, on the next visit,
  // lights rows newer than it and puts a band above the list. ?since=<ISO> sets the mark for one load (screenshots, links).
  const sinceKey = `sd.seen.${page}`;
  const sinceParam = new URLSearchParams(location.search).get('since');
  let seenMark = sinceParam || store(sinceKey) || '';
  const band = document.createElement('p');
  band.className = 'since'; band.setAttribute('role', 'status'); band.hidden = true;
  const main = document.querySelector('main') || body;
  function markNew() {
    const rows = [...main.querySelectorAll('[data-changed]')]; if (!rows.length) return;
    const newest = rows.map(r => r.dataset.changed).sort().pop();
    let n = 0;
    rows.forEach(r => { const isNew = !!seenMark && r.dataset.changed > seenMark; r.toggleAttribute('data-new', isNew); if (isNew) n++; });
    if (!band.isConnected) { const first = rows[0].closest('table, ul, ol, .board, section') || rows[0].parentElement; first.parentElement.insertBefore(band, first); }
    if (!n) { band.hidden = true; delete body.dataset.onlyNew; return; }
    const only = body.dataset.onlyNew === '';
    put(band, html`${ICON('eye')}<b>${n}</b> row${n > 1 ? 's' : ''} changed since you last looked (${relText(seenMark, { now: true })}) · <button class="linkbtn" type="button" data-since="only" aria-pressed="${only}">${only ? 'Show all' : 'Only these'}</button> · <button class="linkbtn" type="button" data-since="seen">Mark seen</button>`);
    band.hidden = false;
    band.dataset.newest = newest;
  }
  band.addEventListener('click', e => {
    const b = e.target.closest('[data-since]'); if (!b) return;
    if (b.dataset.since === 'only') { body.dataset.onlyNew === '' ? delete body.dataset.onlyNew : body.dataset.onlyNew = ''; markNew(); }
    if (b.dataset.since === 'seen') { seenMark = band.dataset.newest; store(sinceKey, seenMark); delete body.dataset.onlyNew; markNew(); toast('Marked seen. New rows light again when something changes.'); }
  });
  if (main) {
    let raf = 0; new MutationObserver(recs => { if (recs.every(r => band.contains(r.target))) return; cancelAnimationFrame(raf); raf = requestAnimationFrame(markNew); }).observe(main, { childList: true, subtree: true });
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

  window.shell = { ICON, openPane, closePane, showTab, openChat, setContext, suggest, send, toast, confirm: confirmAction, commands, capture: openCapture, shq, time, attention, views, reconcile, pages: PAGES, groups: GROUPS };
  if (location.hash === '#chat') openChat(); // screenshot hook
  if (location.hash === '#sheet') openPane('tab-details');
  if (location.hash === '#menu') setMenu(true);
})();
