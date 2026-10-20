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
  // fetch:start
  // build (sd:2588): the page's reads and writes, once for every page. post sends the page's CSRF token and rejects with the
  // server's error text and its HTTP status; a 409 marks the error stale, so a page can reread before it says why. An error
  // with no status got no answer, so a write's outcome is not known. A page script runs before shell.js, so it calls
  // window.shell.post / shell.getJSON when it acts, not while it loads.
  const csrf = () => document.querySelector('meta[name="sd-csrf"]')?.content || '';
  // A Blob body (a dropped file, sd:5112) is sent as is, with the caller's extra headers.
  async function post(path, body, headers) {
    const blob = typeof Blob !== 'undefined' && body instanceof Blob;
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': blob ? 'application/octet-stream' : 'application/json', 'X-SD-CSRF': csrf(), ...headers },
      body: blob ? body : JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(out.error || `HTTP ${r.status}`); e.stale = r.status === 409; e.status = r.status; throw e; }
    return out;
  }
  async function getJSON(path) {
    const r = await fetch(path, { headers: { Accept: 'application/json' } });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
    return out;
  }
  // build (sd:5064): a read for what the page shows applies its answer only while the page still shows it. shown(slot, key,
  // now) starts a read for key in slot, or returns null when now() is not key; the check it returns is true while now() is
  // still key and no newer read started in that slot. A page calls the check when the answer arrives and before it acts.
  const asks = new Map();
  function stillShown(slot, key, now) {
    if (now() !== key) return null;
    const ask = {}; asks.set(slot, ask);
    return () => asks.get(slot) === ask && now() === key;
  }
  // fetch:end
  // item:start
  // build (sd:3526): an item's one address, its row on Tasks, whose Details read it, from every page; the classic /item/<n>
  // page is retiring (sd:3402). A page links with itemHref(n); an Open item command runs openItem(n), whose answer is its toast.
  // Tasks lists no quiet report and nothing done before the week; it hands such an item on to itemPage(n), its classic page.
  const itemHref = n => `/tasks?row=${encodeURIComponent(n)}`;
  const itemPage = n => `/item/${encodeURIComponent(n)}`;
  const openItem = n => { location.href = itemHref(n); return `#${n} opens in Tasks`; };
  // item:end
  const GROUPS = [
    ['Now',       [['Today', 'sunrise', 't'], ['Briefs', 'inbox', 'i'], ['Review', 'list-checks', 'l'], ['Queue', 'git-merge', 'u']]],
    ['Work',      [['Tasks', 'list-todo', 'k'], ['Writing', 'pen-line', 'w'], ['Research', 'flask-conical', 'r'], ['Contributions', 'git-pull-request', 'c']]],
    ['Knowledge', [['Documents', 'file-text', 'd'], ['Notes', 'notebook-pen', 'n']]],
    ['Places',    [['HOA', 'droplets', 'h'], ['Home', 'house', 'o']]],
    ['Fleet',     [['Skills', 'sparkles', 's'], ['Metrics', 'chart-no-axes-column', 'm'], ['Management', 'settings', 'g'], ['Health', 'heart-pulse', 'e'], ['Activity', 'activity', 'a'], ['Reports', 'clipboard-list', 'p'], ['Designs', 'palette', 'x'], ['Settings', 'sun-moon', 'v']]],
  ];
  // build: a page that is built sets window.SHELL_PAGES; a section missing from it shows as not built yet.
  const PAGES = window.SHELL_PAGES || { Today: 'today.html', Briefs: 'briefs.html', Tasks: 'tasks.html', Research: 'research.html', Documents: 'documents.html', HOA: 'hoa.html', Home: 'home.html', Skills: 'skills.html', Metrics: 'metrics.html', Activity: 'activity.html', Reports: 'reports.html', Writing: 'writing.html', Notes: 'notes.html', Contributions: 'contributions.html', Health: 'health.html', Management: 'management.html', Designs: 'designs.html' };
  // build (sd:2163): an unported section opens its old screen until its port moves it into SHELL_PAGES.
  const CLASSIC = window.SHELL_CLASSIC || {}, SCREENS = window.SHELL_SCREENS || {};
  const hrefOf = name => PAGES[name] || CLASSIC[name];
  const classic = name => !PAGES[name] && !!CLASSIC[name];
  const body = document.body;
  const page = body.dataset.page;
  // Rail badges: data/counts.js (window.SHELL_COUNTS) is generated from every page's window.PAGE_ATTENTION.
  // The current page overrides its own badge with its live value, so a badge never disagrees with its page.
  // build (sd:3446): /ui/sections.js sets SHELL_COUNTS on each request from the server's rule for each page. A badge with
  // `at` is the page's last read, not one made now (Health, Today and the others that leave the database), so it says when.
  const counts = Object.assign({}, window.SHELL_COUNTS && window.SHELL_COUNTS.pages);
  const seen = c => c.at && !Number.isNaN(Date.parse(c.at)) ? `, read ${new Date(c.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : '';
  // The badge says what the number is, so a screen reader hears "Tasks, 3 overdue" and not just "3".
  const badge = c => c && c.n && c.state !== 'ok' ? html`<span class="count ${c.state}" title="${c.n} ${c.what}${seen(c)}">${c.n}<span class="sr"> ${c.what}${seen(c)}</span></span>` : html``;

  // ---------- Rail ----------
  const rail = document.createElement('aside');
  rail.className = 'rail'; rail.setAttribute('aria-label', 'Sidebar');
  put(rail, html`
    <div class="rail-head">
      <a class="mark" href="${PAGES.Today || '#'}"><b>sd</b><span>system</span></a>
      <button class="icon-btn" type="button" id="reload" title="Reload this page (r)" aria-label="Reload this page (r)">${ICON('rotate-ccw')}</button>
      <button class="icon-btn" type="button" id="fold-rail" aria-expanded="true" aria-controls="sections" aria-label="Collapse sections ([)" title="Collapse sections ([)">${ICON('panel-left-close')}</button>
    </div>
    <nav aria-label="Sections" id="sections">${GROUPS.map(([g, items]) => html`
      <div><span class="label">${g}</span><ul>${items.map(([name, icon, key]) => {
        const href = hrefOf(name) || '#';
        return html`<li><a href="${href}" data-section="${name}"${name === page ? html` aria-current="page"` : ''}${hrefOf(name) ? '' : html` data-unbuilt`}${classic(name) ? html` data-classic` : ''}>${ICON(icon)}<span class="nm">${name}</span>${classic(name) ? html`<span class="classic">classic<span class="sr"> screen</span></span>` : ''}${badge(counts[name])}<kbd>g ${key}</kbd></a></li>`;
      })}</ul></div>`)}
    </nav>
    <div class="foot">
      <button class="cmd menu" type="button" id="open-menu" aria-expanded="false" aria-controls="sections">Sections</button>
      <button class="cmd" type="button" id="open-chat" aria-controls="pane" aria-label="Chat (c)">${ICON('message-square')}<span class="txt">Chat</span><kbd>c</kbd></button>
      <button class="cmd" type="button" id="open-palette" aria-label="Commands (⌘K)">${ICON('command')}<span class="txt">Commands</span><kbd>⌘K</kbd></button>
    </div>`);
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

  // ---------- View switch: a page's sub-sections ----------
  // view:start
  // build (view switch): the one contract for a top-level page with sub-sections. The page declares them once, before shell.js:
  //   window.PAGE_VIEWS = [{ id, label, key?, api? }, …]   id is [a-z0-9-]+; the first view is the default.
  //   Each view's panel is the page's element id="view-<id>"; a view with no panel is not drawn. The shell makes the panels
  //   tabpanels, hides all but the shown one, and puts the switch (WAI-ARIA tabs: ← → Home End) before the first panel, under
  //   the lamps. The rail lists the views under the current page, each linking <page>#view=<id>.
  //   A page whose views filter one surface instead (Tasks: All, HOA, Home) puts an empty <div id="viewswitch"> where the
  //   switch goes: the shell draws it there and keeps every declared view; a view-<id> panel is still hidden where one exists.
  //   With no #viewswitch, a page's row-1 toolbar (id="toolbar", sd:5281) takes the switch at its start, else the first panel.
  //   key: the letter after v that shows the view (default the id's first letter).
  //   The address keeps the view as #view=<id>; another hash (#main, #chat) leaves the view as it is.
  //   The last view the reader chose is kept per page in localStorage (sd:5064) and shown when the address names none; a
  //   view with keep: false (Designs' Hidden) is never restored, so the page opens on its default.
  //   shell.view.current() is the shown id; shell.view.show(id) shows a view and writes the hash (a lamp that filters a view
  //   calls it); shell.view.count(id, n) puts n on the view's segment, and null hides it. shell.view.hide(id, on) hides a
  //   view's segment and rail entry, for a view with nothing in it (Writing's realms, sd:5064); a hidden view does not show,
  //   and hiding the shown one shows the first view left.
  //   'shell:view' fires on document with detail { id, first, api }: once at DOMContentLoaded, after the page's own listener,
  //   then on every change (a segment, a key, a rail sub-entry, back and forward). first is true the first time a view shows,
  //   so a page starts that view's reader (shell.read with source: api) then; the shell fetches nothing itself.
  const viewHost = document.getElementById('viewswitch'), viewPanel = v => document.getElementById(`view-${v.panel || v.id}`);
  const VIEW_LIST = (window.PAGE_VIEWS || []).filter(v => /^[a-z0-9-]+$/.test(v.id) && (viewHost || viewPanel(v)));
  const viewKey = v => v.key || v.id[0];
  let viewNow = null, viewStarted = false, viewChord = 0;
  const viewSeen = new Set(), viewTabs = [], viewLinks = [], viewItems = [];
  const VKEY = `sd.view.${location.pathname}`;
  const viewKept = () => { try { const id = localStorage.getItem(VKEY); return VIEW_LIST.some(v => v.id === id && v.keep !== false) ? id : null; } catch (_) { return null; } };
  const viewOf = h => { const m = /^#view=([a-z0-9-]+)$/.exec(h || ''); return m && VIEW_LIST.some(v => v.id === m[1]) ? m[1] : null; };
  function viewFire(id) {
    const first = !viewSeen.has(id); viewSeen.add(id);
    document.dispatchEvent(new CustomEvent('shell:view', { detail: { id, first, api: VIEW_LIST.find(v => v.id === id).api || null } }));
  }
  function viewApply(id) {
    if (id === viewNow) return;
    viewNow = id;
    VIEW_LIST.forEach((v, i) => {
      const on = v.id === id;
      viewTabs[i].setAttribute('aria-selected', on); viewTabs[i].tabIndex = on ? 0 : -1;
      // A view may show another view's panel (v.panel, Reports' Needs attention): the panel shows while either is on.
      const panel = viewPanel(v); if (panel) panel.hidden = viewPanel(VIEW_LIST.find(w => w.id === id) || {}) !== panel;
      if (viewLinks[i]) on ? viewLinks[i].setAttribute('aria-current', 'true') : viewLinks[i].removeAttribute('aria-current');
    });
    if (viewStarted) viewFire(id);
  }
  const viewOpen = i => i >= 0 && !viewTabs[i].hidden;
  function viewShow(id) {
    if (!viewOpen(VIEW_LIST.findIndex(v => v.id === id))) return false;
    if (location.hash !== `#view=${id}`) try { history.pushState(history.state, '', `#view=${id}`); } catch (_) {}
    try { localStorage.setItem(VKEY, id); } catch (_) {}
    viewApply(id);
    return true;
  }
  function viewCount(id, n) {
    const t = viewTabs[VIEW_LIST.findIndex(v => v.id === id)]; if (!t) return;
    const c = t.children[1]; c.hidden = n == null; c.textContent = n == null ? '' : String(n);
  }
  function viewHide(id, on) {
    const i = VIEW_LIST.findIndex(v => v.id === id); if (i < 0) return;
    viewTabs[i].hidden = !!on; viewItems[i].hidden = !!on;
    if (on && viewNow === id) { const left = VIEW_LIST.find((v, j) => viewOpen(j)); if (left) viewApply(left.id); }
  }
  const view = { current: () => viewNow, show: viewShow, count: viewCount, hide: viewHide };
  if (VIEW_LIST.length) {
    const bar = document.createElement('div');
    bar.className = 'viewswitch'; bar.setAttribute('role', 'tablist'); bar.setAttribute('aria-label', `${page} views`);
    const sub = document.createElement('ul');
    sub.className = 'subnav'; sub.setAttribute('aria-label', `${page} views`);
    VIEW_LIST.forEach(v => {
      const t = document.createElement('button'), nm = document.createElement('span'), n = document.createElement('span');
      t.type = 'button'; t.setAttribute('role', 'tab'); t.setAttribute('id', `vt-${v.id}`);
      nm.className = 'nm'; nm.textContent = v.label; n.className = 'n'; n.hidden = true;
      t.append(nm, n); t.addEventListener('click', () => viewShow(v.id));
      bar.append(t); viewTabs.push(t);
      const panel = viewPanel(v);
      if (panel) { t.setAttribute('aria-controls', panel.id); if (!v.panel) { panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', `vt-${v.id}`); } }
      const li = document.createElement('li'), a = document.createElement('a');
      a.setAttribute('href', `${hrefOf(page) || ''}#view=${v.id}`); a.textContent = v.label;
      // On the page a sub-entry switches in place, so the page keeps its query (?job=); the href opens a new tab there.
      a.addEventListener('click', e => { e.preventDefault(); viewShow(v.id); });
      li.append(a); sub.append(li); viewLinks.push(a); viewItems.push(li);
    });
    // Roving focus: ← → Home End move between segments and show each (WAI-ARIA tabs, automatic activation).
    bar.addEventListener('keydown', e => {
      const open = viewTabs.filter(t => !t.hidden), i = open.indexOf(e.target), last = open.length - 1;
      const k = { ArrowRight: i + 1 > last ? 0 : i + 1, ArrowLeft: i < 1 ? last : i - 1, Home: 0, End: last }[e.key];
      if (i < 0 || k === undefined) return;
      const to = viewTabs.indexOf(open[k]);
      e.preventDefault(); viewShow(VIEW_LIST[to].id); viewTabs[to].focus();
    });
    const toolbar = document.getElementById('toolbar');
    viewHost ? viewHost.append(bar) : toolbar ? toolbar.prepend(bar) : viewPanel(VIEW_LIST[0]).before(bar);
    rail.querySelector(`[data-section="${page}"]`)?.closest('li')?.append(sub);
    viewApply(viewOf(location.hash) || viewKept() || VIEW_LIST[0].id);
    const follow = () => { const id = viewOf(location.hash); if (id) viewApply(id); else if (!location.hash) viewApply(VIEW_LIST[0].id); };
    addEventListener('hashchange', follow); addEventListener('popstate', follow);
    // v then the view's key. Registered before the shell's keys, so the second key (v r) does not also reload the page.
    document.addEventListener('keydown', e => {
      if (e.target.matches('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest?.('.actmenu') || window.shell?.chording?.()) return;
      if (viewChord && Date.now() - viewChord < 1500) {
        viewChord = 0; const v = VIEW_LIST.find(x => viewKey(x) === e.key);
        if (v) { e.preventDefault(); e.stopImmediatePropagation(); viewShow(v.id); }
        return;
      }
      if (e.key === 'v') viewChord = Date.now();
    });
    const names = VIEW_LIST.map(v => v.label);
    window.PAGE_KEYS = (window.PAGE_KEYS || []).concat([[VIEW_LIST.map(v => `v ${viewKey(v)}`).join(' / '),
      `${names.length > 1 ? `${names.slice(0, -1).join(', ')} or ${names.at(-1)}` : names[0]} view`]]);
    const start = () => { viewStarted = true; viewFire(viewNow); };
    document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
  }
  // view:end

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
          <button class="help" type="button" aria-label="Help: chat scope" data-help="<b>Scope follows the view.</b> The chat answers inside the scoped repo or area, with the selected row as context: the selected item's repo, else the scope's checkout. Pin keeps this scope while you move. It reads files and changes nothing.">${ICON('circle-help')}</button>
        </div>
        <div class="thread" id="thread"></div>
        <form class="composer" id="composer">
          <label class="sr" for="chat-input">Message</label>
          <div class="box">
            <textarea id="chat-input" rows="1" placeholder="Ask about this view" data-gramm="false" data-gramm_editor="false" data-enable-grammarly="false" data-lt-active="false"></textarea>
            <button class="icon-btn" type="submit" aria-label="Send">${ICON('send')}</button>
          </div>
          <div class="meta"><span>Reads files only · changes nothing</span><span>claude -p · 2 min</span></div>
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
    // sd:3442: the fold is an icon button beside the mark, so its name lives in aria-label and title.
    const label = `${min ? 'Expand' : 'Collapse'} sections ([)`;
    foldRail.setAttribute('aria-expanded', !min); foldRail.setAttribute('aria-label', label); foldRail.title = label;
    foldRail.querySelector('use').setAttribute('href', `#i-panel-left-${min ? 'open' : 'close'}`);
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
  // Width: the rail's right edge is a separator (WAI-ARIA window splitter). Drag it, or step it with ← → (Home, End) when it
  // has focus; a double-click goes back to the default. Hidden while the rail is folded, below 900px and on touch, where
  // a page sets it through shell.railWidth instead.
  const grip = document.createElement('div');
  grip.className = 'rail-grip'; grip.tabIndex = 0; grip.title = 'Drag to resize the sections; double-click resets';
  rail.append(grip);
  // width:start
  // sd:3445: each browser keeps the width as sd.rail.width, as it keeps the fold. A value is clamped to RAIL_BOUNDS (px); one
  // that is not a positive number, or none, leaves --rail at its tokens.css default. keep=false moves without storing (a drag).
  const RAIL_BOUNDS = [176, 384];
  const railWidth = v => { const n = Math.round(parseFloat(v)); return n > 0 ? Math.min(RAIL_BOUNDS[1], Math.max(RAIL_BOUNDS[0], n)) : null; };
  function setWidth(v, keep = true) {
    const w = railWidth(v), root = document.documentElement;
    w ? root.style.setProperty('--rail', `${w}px`) : root.style.removeProperty('--rail');
    // A kept width tells the page (Settings shows it); a drag in progress does not.
    if (keep) { store('sd.rail.width', w ? String(w) : ''); document.dispatchEvent(new CustomEvent('shell:rail-width', { detail: w })); }
    for (const [k, x] of [['role', 'separator'], ['aria-orientation', 'vertical'], ['aria-controls', 'sections'], ['aria-label', 'Sections width'],
      ['aria-valuemin', RAIL_BOUNDS[0]], ['aria-valuemax', RAIL_BOUNDS[1]], ['aria-valuenow', w || Math.round(rail.getBoundingClientRect().width)]]) grip.setAttribute(k, String(x));
    return w;
  }
  // width:end
  setWidth(store('sd.rail.width'), false);
  grip.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault(); grip.setPointerCapture(e.pointerId); body.dataset.resizing = '';
    const left = rail.getBoundingClientRect().left;
    let last = null;
    const move = m => { last = setWidth(m.clientX - left, false); };
    const up = () => { ['pointermove', 'pointerup', 'pointercancel'].forEach((t, i) => grip.removeEventListener(t, i ? up : move)); delete body.dataset.resizing; if (last) setWidth(last); };
    grip.addEventListener('pointermove', move); grip.addEventListener('pointerup', up); grip.addEventListener('pointercancel', up);
  });
  grip.addEventListener('keydown', e => {
    const now = +grip.getAttribute('aria-valuenow');
    const to = { ArrowLeft: now - 16, ArrowRight: now + 16, Home: RAIL_BOUNDS[0], End: RAIL_BOUNDS[1] }[e.key];
    if (to === undefined) return;
    e.preventDefault(); e.stopPropagation(); setWidth(to);
  });
  grip.addEventListener('dblclick', () => setWidth(null));
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
  // build (sd:5294): POST /api/chat runs one read-only claude -p (Read, Grep, Glob) and waits for its answer, up to 2
  // minutes. The question, the page and the selected row go with it; the row is taken when it is sent, so an answer that
  // comes back after j / k stays with the row it is about. One question at a time: Send is off while one runs.
  // chat:start
  // The row as the server takes it, and the post: it resolves to the text to show and whether it is a failure, never rejects.
  const chatRow = o => o ? { id: String(o.id), type: String(o.type || ''), label: String(o.label || ''), item: +o.item > 0 ? +o.item : null } : null;
  function askChat(body) {
    return post('/api/chat', body).then(
      out => out.answer ? { text: out.answer, failed: false } : { text: 'No answer: the server sent none.', failed: true },
      // An error with no status got no answer from the server, so the run's outcome there is not known.
      e => ({ text: e.status ? e.message : `No answer: the dashboard did not reply (${(e && e.message) || e}).`, failed: true }));
  }
  // chat:end
  let asking = false;
  const sendBtn = pane.querySelector('#composer [type="submit"]');
  function send(text) {
    if (!text || asking) return;
    const about = selId;
    put(thread, html`<div class="msg"><span class="who label">${ICON('user')}You</span><p>${text}</p></div>
      <div class="msg" aria-busy="true"><span class="who label">${ICON('bot')}Claude · ${scope}</span><p class="why">Asking claude -p in the ${scope} scope; an answer takes up to 2 minutes.</p></div>`, 'append');
    const reply = thread.lastElementChild; suggested = '';
    thread.scrollTop = thread.scrollHeight;
    const show = (answer, failed) => {
      const p = reply.querySelector('p'); put(p, html`${answer}`); p.className = failed ? 'failed' : 'answer';
      reply.removeAttribute('aria-busy');
      thread.querySelectorAll('[role="status"]').forEach(m => m.removeAttribute('role'));
      reply.setAttribute('role', 'status'); thread.scrollTop = thread.scrollHeight;
      if (!failed) remember(text, answer, about);
    };
    // The mockup has no server; it says so instead of pretending.
    if (!window.SHELL_PAGES) return show(`Mockup: no chat backend here. The build sends this to claude -p in the ${scope} scope.`, false);
    asking = true; sendBtn.disabled = true;
    askChat({ scope, page: page || '', selected: chatRow(OBJ.get(about)), text })
      .then(a => show(a.text, a.failed))
      .finally(() => { asking = false; sendBtn.disabled = false; });
  }
  // Asked here: the last three answers given with a row selected stay with that row for the session; Details shows them under the bar.
  const askedKey = `sd.asked.${page}`;
  const askedAll = () => { try { return JSON.parse(sessionStorage.getItem(askedKey) || '{}'); } catch { return {}; } };
  function remember(q, a, id = selId) {
    if (!id) return;
    const all = askedAll(); all[id] = [{ q, a, at: new Date().toISOString() }, ...(all[id] || [])].slice(0, 3);
    try { sessionStorage.setItem(askedKey, JSON.stringify(all)); } catch { /* private window: the thread still shows it */ }
    if (id === selId) { details?.querySelector('.asked')?.remove(); renderAsked(); }
  }
  function renderAsked() {
    if (!details || details.querySelector('.asked')) return;
    const list = selId && askedAll()[selId]; if (!list || !list.length) return;
    const o = OBJ.get(selId);
    put(details, html`<section class="asked"><h3>Asked here</h3>${list.map(x => html`<div class="qa"><p class="q">${ICON('user')}<span>${x.q}</span></p><p class="a why">${x.a}</p></div>`)}<p class="why">${o ? `About ${o.label} · ` : ''}this session, newest first.</p></section>`, 'append');
  }
  if (details) new MutationObserver(() => renderAsked()).observe(details, { childList: true });
  pane.querySelector('#composer').addEventListener('submit', e => { e.preventDefault(); if (asking) return; const i = document.getElementById('chat-input'); send(i.value.trim()); i.value = ''; });
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
  // A tip's control names the popover only while it shows; a help icon keeps its own aria-expanded.
  const untip = () => document.querySelectorAll('[data-tip][aria-describedby="help-pop"]').forEach(b => b.removeAttribute('aria-describedby'));
  function showHelp(btn) {
    untip();
    put(pop, btn.dataset.help != null ? helpText(btn.dataset.help) : html`${btn.dataset.tip}`); pop.hidden = false; place(btn);
    btn.setAttribute('aria-describedby', 'help-pop');
  }
  function hideHelp() {
    pop.hidden = true; untip();
    document.querySelectorAll('.help[aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded', 'false'));
    pinned = null;
  }
  // A help icon that names a command (data-copy, sd:3444) copies it when pressed, and pins its text as any help does.
  function copy(text) {
    const w = navigator.clipboard?.writeText(text);
    (w || Promise.reject(new Error('no clipboard'))).then(() => toast('Copied.'), () => toast('Copy failed: select the line and copy it.'));
  }
  document.addEventListener('click', e => {
    if (e.target.closest?.('[data-tip]')) { clearTimeout(hoverTimer); if (!pinned) pop.hidden = true; }
    const b = e.target.closest('.help');
    if (b) {
      e.preventDefault(); e.stopPropagation();
      if (b.dataset.copy) copy(b.dataset.copy);
      if (pinned === b) return hideHelp();
      hideHelp(); pinned = b; b.setAttribute('aria-expanded', 'true'); showHelp(b); return;
    }
    if (pinned && !pop.contains(e.target)) hideHelp();
  }, true);
  // A tip (data-tip: a command button, a lamp) shows on a mouse hover after the delay and at once on focus (sd:5287), and the
  // focused control names it through aria-describedby while it shows. A press runs the command, so a tip never pins. A move
  // onto the button's own icon keeps it.
  document.addEventListener('pointerover', e => {
    const b = e.target.closest?.('.help, [data-tip]');
    if (!b || pinned || e.pointerType !== 'mouse' || b.contains(e.relatedTarget)) return;
    clearTimeout(hoverTimer); hoverTimer = setTimeout(() => showHelp(b), DELAY);
  });
  document.addEventListener('pointerout', e => {
    const b = e.target.closest?.('.help, [data-tip]');
    if (b && !pinned && !b.contains(e.relatedTarget) && b !== document.activeElement) { clearTimeout(hoverTimer); pop.hidden = true; untip(); }
  });
  document.addEventListener('focusin', e => { const b = e.target.closest?.('.help, [data-tip]'); if (b && !pinned) { clearTimeout(hoverTimer); showHelp(b); } });
  document.addEventListener('focusout', e => { if (e.target.closest?.('.help, [data-tip]') && !pinned) { pop.hidden = true; untip(); } });
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
  //   bulk: true offers it in the bulk bar, one run per picked object; batch: (objs, done) => {} hands the bar's group over in one call.
  //   shell.commands.put({ id, type, label, …facts })   shell.commands.select(id)
  // build (sd:2124): run may return a promise for a write; see settleOne and settleBulk below.
  const REG = [], OBJ = new Map(), picked = new Set();
  // bulk:start
  // build (sd:2124, review of PR #50): the run contract for writes. run(o) returns a string (done: the shell toasts it), null
  // (a form opened, or the page toasts), or a promise. The promise resolves when the write landed, to a toast text or to
  // { text, … }, and rejects with the reason it did not. Undo is offered only for what landed: c.undo(o, answer) gets
  // what that run answered, may return a promise, and resolves false when it had nothing to reverse.
  const whyOf = e => (e && e.message) || String(e);
  const thenable = v => !!v && typeof v.then === 'function';
  const textOf = (v, fallback) => (typeof v === 'string' && v) || (v && typeof v.text === 'string' && v.text) || fallback;
  const undoOne = (c, o, v) => Promise.resolve().then(() => c.undo(o, v));
  // askFirst is a flag or a test of the object (commands.md): Retry asks only after exit 127 (sd:3392). A safe command that
  // asks confirms first; a function is never itself a yes.
  const asksFirst = (c, o) => typeof c.askFirst === 'function' ? !!c.askFirst(o) : !!c.askFirst;
  // One run per picked object, each its own promise: a run that throws is that row's rejection, so the rows after it
  // still run and the group's toast names it (review, PR #46). `v` is what the group's confirm took, the same for every row.
  const runEach = (c, objs, v) => objs.map(o => { try { return c.run?.(o, v); } catch (e) { return Promise.reject(e); } });
  function settleOne(c, o, p, { toast }) {
    return p.then(v => {
      const undo = c.risk === 'undo' && c.undo ? () => undoOne(c, o, v).then(
        ok => toast(ok === false ? `${c.label} not undone · ${o.label}: nothing to reverse` : `${c.label} undone · ${o.label}`),
        e => toast(`${c.label} not undone · ${o.label}: ${whyOf(e)}`)) : null;
      toast(textOf(v, `${c.label} · ${o.label}`), undo);
      return true;
    }, e => { toast(`${o.label} not changed: ${whyOf(e)}`); return false; });
  }
  // A bulk run waits for every run, then toasts once: how many landed, which rows the server refused as stale (a 409: the
  // page reads them again), and which did not change, each with its reason (sd:3012). Its Undo reverses only the rows that
  // landed, waits for each, and says how many of the group were reversed and which were not.
  function settleBulk(c, objs, results, { toast, plural }) {
    return Promise.allSettled(results.map(r => Promise.resolve(r))).then(rs => {
      const landed = [], stale = [], failed = [];
      rs.forEach((r, i) => r.status === 'fulfilled' ? landed.push({ o: objs[i], v: r.value })
        : (r.reason && r.reason.stale ? stale : failed).push({ o: objs[i], why: whyOf(r.reason) }));
      const of = n => `${n} of ${objs.length}`;
      const text = `${c.label} · ${plural(landed.length, c.on)}${stale.length ? ` · ${of(stale.length)} refused stale, read again: ${stale.map(x => x.o.label).join(', ')}` : ''}`
        + (failed.length ? ` · ${of(failed.length)} not changed: ${failed.map(x => `${x.o.label} (${x.why})`).join(', ')}` : '');
      const undo = c.risk === 'undo' && c.undo && landed.length ? () => Promise.allSettled(landed.map(x => undoOne(c, x.o, x.v))).then(us => {
        const not = stale.concat(failed).map(x => `${x.o.label} (its change did not land)`).concat(us.map((u, i) => u.status === 'rejected' ? `${landed[i].o.label} (${whyOf(u.reason)})`
          : u.value === false ? `${landed[i].o.label} (nothing to reverse)` : '').filter(Boolean));
        toast(`${c.label} undone · ${objs.length - not.length} of ${objs.length} reversed${not.length ? ` · not reversed: ${not.join(', ')}` : ''}`);
      }) : null;
      toast(text, undo);
      return { landed: landed.length, stale: stale.length, failed: failed.length };
    });
  }
  // build (sd:2590): a command with batch(objs, done) takes the whole group in one call.
  // Its own form names the count and asks first; it calls done() to clear the picks once the group was sent, and
  // the page toasts the result. Answers whether the command took the group.
  function handOver(c, objs, done) {
    if (!c.batch) return false;
    c.batch(objs, done);
    return true;
  }
  // A command without cli is dashboard-only (chat, navigation); it shows no CLI line.
  // A command with fields passes what the operator typed; the line shows each field's placeholder until then.
  const cliOf = (c, o, v) => c.cli ? c.cli(o, v || {}) : '';
  // A cli string that starts with "no CLI:" or "no sd verb:" is a reason, not a command.
  const noCli = s => !s || /^no (CLI|sd verb)\b/.test(s);
  const shownCli = (c, o) => { const s = cliOf(c, o); return noCli(s) ? '' : s; };
  // executes: true when the dashboard runs the CLI line itself; false when the line is shown for Copy only. A declaration may
  // say so; otherwise an `sd` verb runs and anything else (git, rm, uv, gh, sd-review, a pipe) is copy-only (commands.md).
  const executes = (c, o) => typeof c.executes === 'boolean' ? c.executes : /^sd\s/.test(shownCli(c, o));
  // Hover help on a command button (sd:3457, sd:5287), the one helper for every command button: what the command does, then
  // the line it runs (or that it has none) and whether it asks first or offers Undo. The button carries it as data-tip; the
  // help popover shows it after --delay-help on a hover, so a pass over the buttons shows nothing, and at once on focus.
  const tipOf = (c, o) => {
    const s = shownCli(c, o), d = doesOf(c, o), r = c.previews ? ' Asks first, with a preview.' : c.risk === 'confirm' || asksFirst(c, o) ? ' Asks first.' : c.risk === 'undo' && c.undo ? ' Undo offered.' : '';
    const line = !s ? 'dashboard only, no CLI line' : executes(c, o) ? `runs ${s}` : `copy only, the dashboard does not run ${s}`;
    return d ? `${/[.!?]$/.test(d) ? d : `${d}.`} ${line[0].toUpperCase()}${line.slice(1)}.${r}` : `${c.label}: ${line}.${r}`;
  };
  // What a command does, in one plain sentence (sd:4963): a declaration's `does`, a string or o => a string. The button's tip
  // leads with it (sd:5287) and the ⋯ menu shows it under the label.
  const doesOf = (c, o) => (typeof c.does === 'function' ? c.does(o) : c.does) || '';
  // bulk:end
  // plural(n, one, many) from markup.js: "1 session", "2 sessions" (review 2026-09-29, 18).
  const { plural } = window.markup;
  let selId = null;
  const offWhy = (c, o) => { const w = c.when ? c.when(o) : true; return w === true ? '' : (w || 'not available'); };
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
  // confirm:start
  // fields: [{ name, label, required, placeholder, help, options }], text the command needs from the operator (a reason, a path), as the
  // design source's shell has them (sd:2200). The line under them follows what is typed (cli(values)). OK stays off while a
  // required field is empty or spaces only, and the close asks again, so no path runs the command without its text. Enter in a
  // field is OK, not the form's first button, which is Keep and would drop what was typed (the design's sd:2369).
  // A field with options ([{ value, label }]) is a choice, not text: a select whose first option is the default (sd:3450).
  // A text field's value, when given, is typed in already and can be changed (sd:3523).
  const fieldOf = f => html`<label class="label" for="cf-${f.name}">${f.label}</label>${f.options
    ? html`<select class="cap-in" id="cf-${f.name}" name="${f.name}"${f.help ? html` aria-describedby="${`cf-${f.name}-help`}"` : ''}>${f.options.map(x => html`<option value="${x.value}">${x.label}</option>`)}</select>`
    : html`<input class="cap-in" id="cf-${f.name}" name="${f.name}" autocomplete="off" spellcheck="false"${f.required ? html` required aria-required="true"` : ''} value="${f.value || ''}" placeholder="${f.placeholder || ''}"${f.help ? html` aria-describedby="${`cf-${f.name}-help`}"` : ''}>`}${f.help ? html`<p id="cf-${f.name}-help">${f.help}</p>` : ''}`;
  function confirmAction({ title, body: text = '', cli = '', ok = 'Confirm', keep = 'Keep it', danger = true, fields = [] }) {
    let from = null;
    const line = v => typeof cli === 'function' ? cli(v) : cli;
    return new Promise(done => {
      put(confirmDlg, html`<form method="dialog" class="confirm-body">
        <h2 id="confirm-h">${title}</h2>${text ? html`<p>${text}</p>` : ''}
        ${fields.map(fieldOf)}
        ${line({}) ? html`<div class="cli"><code id="confirm-cli">${line({})}</code></div>` : ''}
        <div class="actions"><button class="btn quiet" value="no" formnovalidate>${keep}</button><button class="btn${danger ? ' danger' : ''}" value="yes">${ok}</button></div></form>`);
      const f = confirmDlg.querySelector('form'), yes = f.querySelector('[value="yes"]');
      const values = () => Object.fromEntries(fields.map(x => [x.name, f.elements[x.name].value.trim()]));
      const filled = () => fields.every(x => !x.required || values()[x.name]);
      const show = () => { const c = f.querySelector('#confirm-cli'); if (c) c.textContent = line(values()); yes.disabled = !filled(); };
      if (fields.length) {
        f.addEventListener('input', show); show();
        f.addEventListener('keydown', e => { if (e.key !== 'Enter' || e.isComposing || e.target.tagName !== 'INPUT') return; e.preventDefault(); if (!yes.disabled) f.requestSubmit(yes); });
      }
      confirmDlg.returnValue = '';
      confirmDlg.onclose = () => done({ yes: confirmDlg.returnValue === 'yes' && filled(), from, values: values() });
      from = modal(confirmDlg, fields.length ? f.elements[fields[0].name] : confirmDlg.querySelector('[value="no"]'));
    });
  }
  // confirm:end
  // The act a confirm names: the label when it already names its object ("Remove worktree"), else the label and the type
  // ("Cancel job"). The OK button repeats it; Keep says what not doing it means, never a second "Cancel" (review item 13).
  const actOf = (c, o) => /\s/.test(c.label.trim()) ? c.label : `${c.label} ${o.type}`;
  function run(c, o) {
    const go = v => {
      const msg = c.run ? c.run(o, v) : '';
      if (thenable(msg)) { settleOne(c, o, msg, { toast }); return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } })); }
      if (msg === null) return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id, form: true } })); // opened a form
      const text = msg || `${c.label} · ${o.label}`;
      // Undo only where the command declares one: a toast that says "undone" over nothing is worse than no Undo.
      if (c.risk === 'undo' && c.undo) toast(text, () => { c.undo(o); toast(`${c.label} undone · ${o.label}`); });
      else toast(text);
      document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } }));
    };
    // A command with fields always asks: the dialog is where the operator types them. Its colour follows the risk, or the
    // command's danger(o) where one object's outcome is graver than another's (Tasks' item.complete, sd:2250).
    // previews: true (Land, sd:5104) is a confirm whose run reads a preview first and asks with it through shell.ask, so the
    // shell does not ask a second time.
    const fields = c.fields ? c.fields(o) : [];
    if (c.previews || (c.risk !== 'confirm' && !asksFirst(c, o) && !fields.length)) return go();
    const act = actOf(c, o);
    confirmAction({ title: `${act}: ${o.label}?`, body: c.consequence ? c.consequence(o) : '', cli: v => { const s = cliOf(c, o, v); return noCli(s) ? '' : s; }, ok: act, keep: `Don't ${c.label.toLowerCase()}`, danger: c.danger ? !!c.danger(o) : c.risk === 'confirm', fields })
      .then(({ yes, from, values }) => { if (!yes) return; go(values); const a = document.activeElement; if (!a || a === body) refocus(from); });
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
    return html`<span class="rowact">${p ? html`<button class="btn quiet sm" type="button" data-cmd="${p.id}" data-obj="${id}" data-tip="${tipOf(p, o)}">${p.label}</button>` : ''}<button class="icon-btn" type="button" data-menu-for="${id}" aria-haspopup="menu" aria-expanded="false" aria-label="Actions for ${o.label}">${ICON('ellipsis')}</button></span>`;
  }
  // Details: every command as a button; what it does and its line are the button's tip (sd:5287), its CLI line and the
  // reason an off one is off fold under "Same thing from the CLI", closed until opened (sd:3464).
  function bar(id) {
    // sd:5295: `bar: false` is a command an on-page field or dropdown already does; its key, the ⋯ menu and bulk keep it.
    const o = OBJ.get(id); const list = cmdsFor(o).filter(c => c.bar !== false); if (!list.length) return html``;
    const on = list.filter(c => !offWhy(c, o)), off = list.filter(c => offWhy(c, o));
    const first = on.find(c => c.primary?.(o)) || on[0];
    const button = c => html`<button class="btn${c === first ? '' : ' quiet'}${c.risk === 'confirm' ? ' risky' : ''}" type="button" data-cmd="${c.id}" data-obj="${id}" data-tip="${tipOf(c, o)}" aria-keyshortcuts="${c.key ? '. ' + c.key : ''}">${c.label}${c.key ? html`<kbd>${c.key}</kbd>` : ''}</button>`;
    return html`<div class="actions">${on.map(button)}</div>
      <details class="cli-all"><summary>Same thing from the CLI</summary>${off.map(c => html`<p class="why off"><b>${c.label}</b> is off: ${offWhy(c, o)}</p>`)}${on.filter(c => noCli(cliOf(c, o))).map(c => html`<p class="why"><b>${c.label}</b> has no CLI${c.cli ? `: ${cliOf(c, o).replace(/^no (CLI|sd verb):?\s*/, '')}` : '; it is dashboard only'}.</p>`)}${on.filter(c => !noCli(cliOf(c, o))).map(c => html`<div class="cli fold"><button class="cli-line" type="button" aria-expanded="false" title="Show the full command"><code>${cliOf(c, o)}</code></button><button class="icon-btn" type="button" data-copy="${cliOf(c, o)}" aria-label="Copy: ${cliOf(c, o)}">${ICON('copy')}</button></div>${executes(c, o) ? (c.sends ? html`<p class="why"><b>${c.label}</b> runs here and sends <code>${c.sends(o)}</code>.</p>` : '') : html`<p class="why"><b>${c.label}</b> is copy only: the dashboard does not run this line.</p>`}`)}</details>`;
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
    put(menuEl, html`<p class="label">${o.label}</p>${on.map(c => html`<button role="menuitem" type="button" data-cmd="${c.id}" data-obj="${id}"><span>${c.label}${c.risk === 'confirm' || c.fields ? '…' : ''}</span><small>${doesOf(c, o)}</small><code>${shownCli(c, o)}</code>${c.key ? html`<kbd>${c.key}</kbd>` : ''}</button>`)}
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
    if (cp) { copy(cp.dataset.copy); return; }
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
    runBulk(REG.find(x => x.id === b.dataset.bulk), [...picked].map(i => OBJ.get(i)));
  });
  function runBulk(c, objs) {
    const group = { id: 'bulk', type: plural(2, c.on).replace(/^2 /, ''), label: plural(objs.length, c.on) };
    // The confirm dialog gets the group, which has no row fields, so consequence is rebuilt from the real objects: one line each, repeats and blanks folded; the group asks first when any row would (sd:3392).
    const consequence = c.consequence && (() => { const lines = [...new Set(objs.map(o => c.consequence(o)).filter(Boolean))]; return lines.length > 4 ? `${lines.slice(0, 4).join(' ')} And ${lines.length - 4} more.` : lines.join(' '); });
    // build (sd:2124): the group's toast and Undo wait for every run (settleBulk); run() sees null and toasts nothing itself.
    if (handOver(c, objs, () => { picked.clear(); renderBulk(); })) return;
    // A command with fields asks once for the group (run's confirm); each row's run and CLI line get what was typed.
    const one = { ...c, cli: (_, v) => objs.map(o => cliOf(c, o, v)).join(' && '), run: (_, v) => { const rs = runEach(c, objs, v); picked.clear(); renderBulk(); settleBulk(c, objs, rs, { toast, plural }); return null; },
      consequence, askFirst: objs.length > 25 || objs.some(o => asksFirst(c, o)), risk: c.risk };
    run(one, group);
  }
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
    rowActions, bar, openMenu, run, runBulk,
    pick: id => { const o = OBJ.get(id); if (!o) return; if (!REG.some(c => c.on === o.type && c.bulk)) { toast(`No bulk command for a ${o.type}.`); return; } picked.has(id) ? picked.delete(id) : picked.add(id); renderBulk(); },
  };

  // ---------- Capture (n) ----------
  // One capture everywhere. It files a task or followup, or a note on the selected row.
  const cap = document.createElement('dialog');
  cap.className = 'palette capture'; cap.setAttribute('aria-labelledby', 'cap-h');
  put(cap, html`<h2 id="cap-h"></h2>`);
  body.append(cap); trap(cap);
  // capture:start
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe quoting for text a person typed: $(), backticks and \ stay literal
  // build (sd:3520): the shell files every capture, so no page drops one. A task or followup is POST /api/items; a note is
  // POST /api/items/<id>/notes with the revision /api/items/<id>/capture-context reads. It answers "Captured sd:N" with the
  // new id, or throws "Not filed" with the reason; an error with no status got no answer, so whether it filed is not known.
  // build (sd:3521): the box also picks a priority (P3 unless changed) and a repo, defaulting to the page's: a page sets
  // window.PAGE_REPO(o) → a repo path or name for the row o (or its filter), and captureRepo finds it in /api/capture-repos.
  // A p1 to p4 typed in the text is the priority, as Today's field shows it, and leaves the title.
  const captureRepo = (list, hint) => { if (!hint) return ''; const h = String(hint), n = h.split('/').pop();
    return (list.find(r => r.path === h) || list.find(r => r.name === h) || list.find(r => r.name === n))?.path || ''; };
  const typedP = title => { const p = title.match(/\bp([1-4])\b/i); return p ? [+p[1], title.replace(/\bp[1-4]\b/gi, '').replace(/\s+/g, ' ').trim()] : [null, title]; };
  // The CLI files into the checkout it runs in, so a repo is a cd; with none, --no-repo.
  const captureCli = ({ kind, title, item, priority, repo }) => {
    if (kind === 'note') return `sd task note ${item} ${shq(title)} --kind comment`;
    const [p, named] = typedP(title), where = repo ? `cd ${/^~?[\w./-]+$/.test(repo) ? repo : shq(repo)} && ` : '';
    return `${where}sd task add${repo ? '' : ' --no-repo'}${kind === 'followup' ? ' --kind followup' : ''}${p || priority ? ` --priority ${p || priority}` : ''} ${shq(named)}`;
  };
  async function fileCapture({ kind, title, item, priority = null, repo = '' }) {
    const unknown = why => new Error(`Not known if it filed: ${why}. Look in Tasks before you capture it again.`);
    try {
      if (kind === 'note') {
        if (!item) throw Object.assign(new Error('select a row that has an item first'), { status: 0 });
        const { revision } = await getJSON(`/api/items/${item}/capture-context`).catch(e => { throw Object.assign(e, { status: 0 }); });
        await post(`/api/items/${item}/notes`, { body: title, kind: 'comment', revision });
        return `Note added to sd:${item}`;
      }
      const [p, named] = typedP(title), pri = p || priority;
      const out = await post('/api/items', { title: named, ...(pri ? { priority: pri } : {}), ...(repo ? { repo } : {}),
        ...(kind === 'followup' ? { kind: 'followup', ...(item ? { followup_of: item } : {}) } : {}) });
      if (!out.item?.id) throw unknown('the server named no item');
      return `Captured sd:${out.item.id}: ${out.item.title || named}`;
    } catch (e) {
      if (e.message.startsWith('Not known')) throw e;
      throw e.status === undefined ? unknown(e.message) : new Error(`Not filed: ${e.message}`);
    }
  }
  let repoList = null; // /api/capture-repos, read once per page; a failed read is retried on the next open
  const readRepos = () => repoList || (repoList = getJSON('/api/capture-repos').then(d => d.repos || [], e => { repoList = null; throw e; }));
  // capture:end
  // openCapture(target): target is the object (or its id) the command was run on; without one, the selected row. A refused
  // capture reopens with its text and picks (kind, priority, repo), so nothing the operator chose is lost.
  function openCapture(target, text = '', picks = null) {
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
      <fieldset class="cap-pick"><legend class="sr">Priority and repo</legend>
        <div class="seg" role="radiogroup" aria-label="Priority">${[1, 2, 3, 4].map(n => html`<label><input type="radio" name="priority" value="${n}"${n === 3 ? html` checked` : ''}><span>P${n}</span></label>`)}</div>
        <label class="label" for="cap-repo">Repo <kbd>Alt R</kbd></label>
        <select id="cap-repo" name="repo" aria-keyshortcuts="Alt+R"><option value="">No repo</option></select>
      </fieldset>
      <p class="why" id="cap-why"></p>
      <div class="cli"><code id="cap-cli">sd task add ''</code></div>
      <div class="actions"><button class="btn quiet" value="no" formnovalidate>Cancel</button><button class="btn" value="yes">Capture</button></div></form>`);
    const f = cap.querySelector('form'), inp = f.querySelector('#cap-in'), cli = f.querySelector('#cap-cli'), why = f.querySelector('#cap-why');
    const sel = f.querySelector('#cap-repo'), pick = f.querySelector('.cap-pick');
    const fields = () => ({ kind: f.kind.value, title: inp.value.trim(), item: about ? about.item : null, priority: +f.priority.value, repo: sel.value });
    const upd = () => {
      const k = f.kind.value;
      pick.disabled = k === 'note';
      // build: say what POST /api/items stores. Only a followup on a row with an item links to it (followup_of).
      why.textContent = k === 'note' ? `A comment on #${about.item}`
        : k === 'followup' && about ? `A followup of #${about.item}`
        : o ? `Files to the inbox; not linked to ${o.label}` : 'No row selected · files to the inbox';
      cli.textContent = captureCli(fields());
    };
    if (picks) { if (!f.querySelector(`[name="kind"][value="${picks.kind}"]`)?.disabled) f.kind.value = picks.kind; f.priority.value = String(picks.priority); }
    inp.value = text; f.addEventListener('input', upd); f.addEventListener('change', upd); upd();
    // Enter in a field captures, as in the confirm dialog: the browser's own Enter presses the first button, Cancel.
    f.addEventListener('keydown', e => { if (e.altKey && e.code === 'KeyR') { e.preventDefault(); sel.focus(); }
      if (e.key === 'Enter' && !e.isComposing && e.target.tagName === 'INPUT') { e.preventDefault(); f.requestSubmit(f.querySelector('[value="yes"]')); } });
    // The page's repo is the default once the list is read; a failed read leaves No repo and says so.
    let hint = null; try { hint = window.PAGE_REPO ? window.PAGE_REPO(o) : null; } catch { hint = null; }
    readRepos().then(list => { if (!cap.open) return;
      put(sel, html`<option value="">No repo</option>${list.map(r => html`<option value="${r.path}">${r.name}</option>`)}`);
      sel.value = captureRepo(list, picks ? picks.repo : hint); upd(); },
    e => { if (!cap.open) return; put(sel, html`<option value="">No repo (list not read: ${e.message})</option>`); });
    // build: a page that must do more (Tasks rereads its board) sets window.SHELL_CAPTURE({ kind, title, item, about }) →
    // Promise<toast text>; every other page files through fileCapture. A refusal opens the dialog again with the text.
    cap.onclose = () => { if (cap.returnValue !== 'yes' || !inp.value.trim()) return; const v = inp.value.trim(), picks = fields();
      (window.SHELL_CAPTURE || fileCapture)({ ...picks, about: o })
        .then(toast, e => { const m = String(e.message || e); toast(m); if (m.startsWith('Not filed')) openCapture(o, v, picks); }); };
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
      }${VIEW_LIST.length > 1 ? [grp('Views'), VIEW_LIST.map(v => html`<li role="option" data-pview="${v.id}">${ICON('layout-dashboard')}<span>View: ${v.label}</span><kbd>v ${viewKey(v)}</kbd></li>`)] : ''
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
    if (o.dataset.pview) { viewShow(o.dataset.pview); return; }
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

  // ---------- List grammar: sort, pages, filter chips (foundation patterns.md, Lists) ----------
  // list:start
  // build (sd:2527, sd:2528, sd:2529): one sort header, one pager and one chip row for every list page. A page keeps its list
  // state as st = { sort, dir, page, size } (dir 1 ascending, -1 descending; page from 1), draws with these and keeps st in
  // the URL with listParams / listQuery. Each returns html`…` for the page to put, and the click helpers answer whether st changed.
  const SIZES = [25, 50, 100, 200];
  const num = n => Number(n).toLocaleString('en-US');
  // cols: [[key, label, cls]]; a column without a key has no order. The sorted <th> alone carries aria-sort (W3C APG
  // sortable table); its button names the order a click gives, since the arrow is drawn, not read.
  function sortHead(cols, st) {
    return html`<tr>${cols.map(([key, label, cls = '']) => {
      if (!key) return html`<th scope="col" class="${cls}">${label}</th>`;
      const on = st.sort === key, next = on && st.dir > 0 ? 'descending' : 'ascending';
      return html`<th scope="col" class="${cls}"${on ? html` aria-sort="${st.dir > 0 ? 'ascending' : 'descending'}"` : ''}><button class="sorter" type="button" data-sort="${key}" title="Sort ${next}">${label}${ICON(on ? (st.dir > 0 ? 'arrow-up' : 'arrow-down') : 'arrow-up-down')}</button></th>`;
    })}</tr>`;
  }
  // A header click: the sorted key flips its direction, another key starts ascending. Back to page 1 either way.
  function sortBy(e, st) {
    const b = e.target.closest?.('button[data-sort]'); if (!b) return false;
    if (st.sort === b.dataset.sort) st.dir = -st.dir; else { st.sort = b.dataset.sort; st.dir = 1; }
    st.page = 1; return true;
  }
  // Rows of the page st points at; st.page is clamped first, so a filter that shrinks the list never shows an empty page.
  function pageOf(rows, st) {
    const pages = Math.max(1, Math.ceil(rows.length / st.size));
    st.page = Math.min(Math.max(1, st.page), pages);
    return rows.slice((st.page - 1) * st.size, st.page * st.size);
  }
  const rangeText = (total, st) => total ? `${num((st.page - 1) * st.size + 1)}–${num(Math.min(total, st.page * st.size))} of ${num(total)}` : `0 of ${num(total)}`;
  // The first and last page, and two either side of the current one; a skipped run is one "…".
  function pageList(pages, cur) {
    const out = [];
    for (let n = 1; n <= pages; n++) {
      if (n === 1 || n === pages || Math.abs(n - cur) <= 2) out.push(n);
      else if (out[out.length - 1] !== '…') out.push('…');
    }
    return out;
  }
  // The range line always shows; numbered pages when there is more than one; sizes once the list is longer than the smallest.
  function pager(total, st, label = 'rows') {
    const pages = Math.max(1, Math.ceil(total / st.size));
    return html`<nav class="list-pager" aria-label="Pages of ${label}"><span class="range">${rangeText(total, st)}</span>${pages > 1 ? html`<span class="pages">${pageList(pages, st.page).map(n => n === '…'
      ? html`<span class="gap" aria-hidden="true">…</span>`
      : html`<button class="chip" type="button" data-page="${n}" aria-label="Page ${n}"${n === st.page ? html` aria-current="page"` : ''}>${n}</button>`)}</span>` : ''}${total > SIZES[0] ? html`<span class="sizes" role="group" aria-label="Rows per page"><span class="label">Per page</span>${SIZES.map(n => html`<button class="chip" type="button" data-size="${n}" aria-pressed="${String(n === st.size)}">${n}</button>`)}</span>` : ''}</nav>`;
  }
  // A pager click: a page number moves there; a size starts again at page 1.
  function paging(e, st) {
    const p = e.target.closest?.('.list-pager [data-page]'), s = e.target.closest?.('.list-pager [data-size]');
    if (p) { st.page = +p.dataset.page; return true; }
    if (s) { st.size = +s.dataset.size; st.page = 1; return true; }
    return false;
  }
  // A redraw replaces the header and the pager; focus moves to the new copy of the clicked sort, page or size button.
  function keepFocus(e, draw) {
    const b = e.target.closest?.('button[data-sort], button[data-page], button[data-size]'), root = e.currentTarget;
    draw();
    const k = b && ['sort', 'page', 'size'].find(a => b.dataset[a] != null);
    if (k) root?.querySelector(`button[data-${k}="${b.dataset[k]}"]`)?.focus();
  }
  // Active filters above the list: one removable chip each, how many rows they leave, and one Clear all.
  // list: [{ key, label }]; a click on a chip names its key in data-unfilter, Clear all carries data-unfilter-all.
  // The table grammar (sd:5064) passes tunfilter, so a page's own [data-unfilter] handler never takes its chips.
  function chips(list, shown, total, attr = 'unfilter') {
    if (!list.length) return html``;
    const t = attr === 'tunfilter';
    return html`<div class="list-chips" role="group" aria-label="Active filters"><span class="n">${plural(list.length, 'filter')} · ${num(shown)} of ${num(total)}</span>${list.map(f => t
      ? html`<button class="chip" type="button" data-tunfilter="${f.key}" aria-label="Remove filter: ${f.label}">${f.label}${ICON('x')}</button>`
      : html`<button class="chip" type="button" data-unfilter="${f.key}" aria-label="Remove filter: ${f.label}">${f.label}${ICON('x')}</button>`)}${t
      ? html`<button class="linkbtn" type="button" data-tunfilter-all>Clear all</button>` : html`<button class="linkbtn" type="button" data-unfilter-all>Clear all</button>`}</div>`;
  }
  // The URL: page and size as the pager sets them, sort and dir as the header does. A value the page does not offer is ignored,
  // and a page is decimal digits only (?page=1.5 or 0x2 is not a page, sd:2427). Defaults stay out of the URL.
  function listParams(p, st, { sorts = [], size = 50 } = {}) {
    if (/^[1-9]\d*$/.test(p.get('page') || '')) st.page = +p.get('page');
    if (SIZES.includes(+p.get('size'))) st.size = +p.get('size');
    if (sorts.includes(p.get('sort'))) { st.sort = p.get('sort'); st.dir = p.get('dir') === 'desc' ? -1 : 1; }
    st.size ||= size;
    return st;
  }
  function listQuery(p, st, { sort = '', dir = 1, size = 50 } = {}) {
    if (st.page > 1) p.set('page', st.page);
    if (st.size !== size) p.set('size', st.size);
    if (st.sort && (st.sort !== sort || st.dir !== dir)) { p.set('sort', st.sort); p.set('dir', st.dir > 0 ? 'asc' : 'desc'); }
    return p;
  }
  // Filter dropdowns (sd:5158): one control for a filter with many values, in place of a row of chips. A multi filter is a
  // <details> menu of checkboxes whose summary names the pick (All, the one value, or a count); a single filter is a native
  // <select> with an All option; help is html`…` drawn after it. opts: [[value, label, n, off]], n the count beside it, off why it cannot be picked: its box
  // keeps aria-disabled and the tab order, and describes itself by the reason. on is the page's Set (multi) or string
  // (single); a value in on that no option has is still an option, so an address from another day can be unpicked.
  // Every control carries data-pick="key"; pickChange applies a change event to the page's F, redraws, and keeps the menu
  // open (PICKS) and focus on the same control.
  const PICKS = new Set();
  function pick(key, name, opts, on, { multi = false, all = 'All', help = '' } = {}) {
    const set = on instanceof Set ? on : new Set(on ? [on] : []);
    const vals = [...opts, ...[...set].filter(v => !opts.some(o => o[0] === v)).map(v => [v, v])];
    if (!multi) {
      return html`<div class="fgroup"><label class="label" for="f-${key}">${name}</label><select id="f-${key}" class="fsel" data-pick="${key}"><option value=""${set.size ? '' : html` selected`}>${all}</option>${
        vals.map(([v, l, n]) => html`<option value="${v}"${set.size === 1 && set.has(v) ? html` selected` : ''}>${l}${n != null ? ` · ${n}` : ''}</option>`)}${set.size > 1 ? html`<option value="" disabled selected>${set.size} picked</option>` : ''}</select>${help}</div>`;
    }
    const one = set.size === 1 && vals.find(o => set.has(o[0]));
    return html`<div class="fgroup"><span class="label" id="f-${key}-l">${name}</span><details class="fpick" data-pick="${key}"${PICKS.has(key) ? html` open` : ''}><summary aria-labelledby="f-${key}-l f-${key}-v"><span id="f-${key}-v">${!set.size ? all : one ? one[1] : `${set.size} picked`}</span></summary><div class="fpick-menu" role="group" aria-labelledby="f-${key}-l">${
      vals.map(([v, l, n, off], i) => { const why = `f-${key}-off-${i}`;
        return html`<label class="fpick-opt"><input type="checkbox" data-pick="${key}" value="${v}"${set.has(v) ? html` checked` : ''}${off ? html` aria-disabled="true" aria-describedby="${why}"` : ''}><span class="nm">${l}</span>${n != null ? html`<span class="n">${n}</span>` : ''}${off ? html`<small class="why" id="${why}">${off}</small>` : ''}</label>`; })}</div></details>${help}</div>`;
  }
  // A change on a pick control: true when it was one. A box that is off goes back as it was and changes nothing.
  function pickChange(e, F, draw) {
    const t = e.target, key = t?.dataset?.pick;
    if (!key || !(key in F)) return false;
    const box = t.type === 'checkbox';
    if (box && t.getAttribute('aria-disabled') === 'true') { t.checked = !t.checked; return true; }
    if (box) { if (t.checked) F[key].add(t.value); else F[key].delete(t.value); PICKS.add(key); }
    else if (F[key] instanceof Set) { F[key].clear(); if (t.value) F[key].add(t.value); }
    else F[key] = t.value;
    draw();
    [...(e.currentTarget?.querySelectorAll?.('input[data-pick], select[data-pick]') || [])]
      .find(x => x.dataset.pick === key && (!box || x.value === t.value))?.focus();
    return true;
  }
  const list = { SIZES, sortHead, sortBy, pageOf, pager, paging, keepFocus, rangeText, pageList, chips, listParams, listQuery, pick, pickChange };
  // list:end
  // A filter dropdown in use stays open across a redraw; a press outside it or focus leaving it closes it, and so does Esc
  // (Keys, below). toggle does not bubble, so it is heard on the way down.
  document.addEventListener('toggle', e => { const d = e.target; if (d.matches?.('details.fpick')) { if (d.open) PICKS.add(d.dataset.pick); else PICKS.delete(d.dataset.pick); } }, true);
  document.addEventListener('pointerdown', e => document.querySelectorAll('details.fpick[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; }));
  document.addEventListener('focusout', e => { const d = e.target.closest?.('details.fpick[open]'); if (d && e.relatedTarget && !d.contains(e.relatedTarget)) d.open = false; });

  // ---------- Table grammar: every table sorts, filters and folds (sd:5064) ----------
  // tables:start
  // build (sd:5064): one grammar for every table on every page, the classic screens too. A table whose header the page
  // draws with shell.list.sortHead (a button[data-sort]) keeps the page's sort; one marked data-tfilter="page" keeps the
  // page's filter. Otherwise a table gets a sort button in each labelled header, a filter box and a column filter once it
  // has rows enough, a removable chip per active filter, and every table gets a fold on each band row. These functions work on a plain model, so the tests
  // run them without a page: rows of { kind, cells, pos }, kind 'row' (data), 'group' (a band heading the rows below it)
  // or 'static' (a note); cells are the cell values in column order; pos is the row's place in the page's own order.
  // st = { sort: column label or null, dir: 1 | -1, q: text filter, f: { label: value }, closed: [band labels] }.
  const TCOLL = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });
  const TRANK = { warning: 0, caution: 1, unknown: 2, queued: 3, ok: 4 };
  const tnum = s => /^[-+−]?[\d,]*\.?\d+\s*%?$/.test(s) ? parseFloat(s.replace(/[,%\s]/g, '').replace('−', '-')) : null;
  // Empty cells sort last either way; states by severity; numbers as numbers; the rest in natural order (#9 before #10).
  function tcompare(a, b, dir) {
    if (a === '' || b === '') return a === b ? 0 : a === '' ? 1 : -1;
    if (a in TRANK && b in TRANK) return dir * (TRANK[a] - TRANK[b]);
    const x = tnum(a), y = tnum(b);
    return dir * (x != null && y != null ? x - y : TCOLL.compare(a, b));
  }
  // A band heads the rows below it up to the next band. A band with no data row under it is a note, not a group.
  function tsegments(rows) {
    const segs = [{ head: null, body: [] }];
    rows.forEach(r => r.kind === 'group' ? segs.push({ head: r, body: [] }) : segs[segs.length - 1].body.push(r));
    return segs.filter(s => s.head || s.body.length)
      .map(s => s.head && !s.body.some(r => r.kind === 'row') ? { head: null, body: [{ ...s.head, kind: 'static' }, ...s.body] } : s);
  }
  // The order st asks for. Each band keeps its place; the data rows under it sort (ties, and no sort, keep the page's
  // order) and its notes stay below them. cols names the columns, so a sort on a column the table lost is no sort.
  function torder(rows, st, cols) {
    const i = st.sort == null ? -1 : cols.indexOf(st.sort);
    return tsegments(rows).flatMap(s => {
      const data = s.body.filter(r => r.kind === 'row').sort((a, b) => (i < 0 ? 0 : tcompare(a.cells[i] ?? '', b.cells[i] ?? '', st.dir)) || a.pos - b.pos);
      return [...(s.head ? [s.head] : []), ...data, ...s.body.filter(r => r.kind !== 'row')];
    });
  }
  // A data row shows when every word of the text filter is in its cells and each column filter equals its cell; a filter
  // on a column the table no longer has passes. Bands and notes always show.
  function tshows(r, st, cols) {
    if (r.kind !== 'row') return true;
    const hay = r.cells.join(' ').toLowerCase();
    return (st.q || '').toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w))
      && Object.entries(st.f || {}).every(([label, v]) => { const i = cols.indexOf(label); return i < 0 || (r.cells[i] ?? '') === v; });
  }
  // A column offers its values as filters when, over at least 4 rows, it has 2 to 12 of them, some repeat and not all are
  // numbers: state, kind, repo; not a count or a size.
  function tfacets(cols, rows, skip = []) {
    const data = rows.filter(r => r.kind === 'row'); if (data.length < 4) return [];
    return cols.map((label, i) => ({ label, values: [...new Set(data.map(r => r.cells[i] ?? '').filter(Boolean))].sort(TCOLL.compare) }))
      .filter((c, i) => c.label && !skip[i] && c.values.length >= 2 && c.values.length <= 12 && c.values.length < data.length && !c.values.every(v => tnum(v) != null));
  }
  // A header click: another column starts ascending, the sorted one turns descending, then back to the page's order.
  function tsortBy(st, label) {
    if (st.sort !== label) { st.sort = label; st.dir = 1; } else if (st.dir > 0) st.dir = -1; else { st.sort = null; st.dir = 1; }
    return st;
  }
  // The chips for list.chips, and what removing one clears.
  const tchips = st => [...(st.q ? [{ key: 'q', label: `Text: ${st.q}` }] : []), ...Object.entries(st.f || {}).map(([l, v]) => ({ key: `f:${l}`, label: `${l}: ${v}` }))];
  function tunfilter(st, key) {
    if (key == null) { st.q = ''; st.f = {}; } else if (key === 'q') st.q = ''; else delete st.f[key.slice(2)];
    return st;
  }
  // A kept state is read back field by field; anything malformed falls back to the default, since the store is only a convenience.
  function tstate(v) {
    const o = v && typeof v === 'object' ? v : {}, f = o.f && typeof o.f === 'object' && !Array.isArray(o.f) ? o.f : {};
    return { sort: typeof o.sort === 'string' ? o.sort : null, dir: o.dir === -1 ? -1 : 1, q: typeof o.q === 'string' ? o.q : '',
      f: Object.fromEntries(Object.entries(f).filter(([, x]) => typeof x === 'string')),
      closed: Array.isArray(o.closed) ? o.closed.filter(x => typeof x === 'string') : [] };
  }
  // The toolbar standard (sd:5281): every table with a data row gets the text filter and the column filter, and its count
  // right-aligned on the same row; a page that filters the table itself (ownFilter) keeps its own text filter and gets the
  // column filter alone. Folds come with 2 bands.
  function tbar({ total = 0, filterOn = false, ownFilter = false, bands = 0 } = {}) {
    const facet = total > 0 || filterOn;
    return { find: !ownFilter && facet, facet, folds: bands >= 2 };
  }
  // The count at the row's right end: every row, or the shown ones of every row while a filter hides some.
  const tcount = (shown, total) => shown === total ? plural(total, 'row') : `${shown} of ${plural(total, 'row')}`;
  const tables = { tcompare, tsegments, torder, tshows, tfacets, tsortBy, tchips, tunfilter, tstate, tbar, tcount };
  // tables:end

  // The page side of the table grammar. State is kept per page and table in localStorage, a convenience: a blocked store
  // keeps it for this page only. A redraw (put into the tbody, the thead or the table's parent) is seen by one observer,
  // which reapplies the state in the next frame; it writes to the page only what differs, so a steady page sees no writes.
  const TKEY = `sd.tables.${location.pathname}`;
  const TKEPT = (() => { try { const v = JSON.parse(localStorage.getItem(TKEY) || '{}'); return v && typeof v === 'object' ? v : {}; } catch { return {}; } })();
  const TST = {};
  const tst = key => TST[key] ||= tstate(TKEPT[key]);
  const tkeep = key => { TKEPT[key] = TST[key]; try { localStorage.setItem(TKEY, JSON.stringify(TKEPT)); } catch { /* blocked: this page only */ } };
  const twrap = t => { const p = t.parentElement; return p && p.children.length === 1 && p.matches('div:not(main)') ? p : t; };
  const tlabel = el => el.textContent.replace(/\s+/g, ' ').trim();
  // A state cell (td.g) reads as its state's name; a <time> as its datetime; data-sort-value wins over both.
  function tcell(td) {
    if (!td) return '';
    if (td.dataset.sortValue != null) return td.dataset.sortValue;
    if (td.classList.contains('g')) return tlabel(td.querySelector('.sr') || td).toLowerCase();
    const tm = td.querySelector('time[datetime]');
    return tm ? tm.getAttribute('datetime') : tlabel(td);
  }
  function tkeyOf(t) {
    if (t.dataset.tgrid) return t.dataset.tgrid;
    const base = t.getAttribute('aria-labelledby') || t.id || t.closest('[id], [data-listing]')?.id || t.closest('[data-listing]')?.dataset.listing || 'table';
    let key = base, n = 1;
    while (document.querySelector(`table[data-tgrid="${CSS.escape(key)}"]`)) key = `${base}:${++n}`;
    return (t.dataset.tgrid = key);
  }
  const theads = t => { const r = t.tHead?.rows; return r && r.length === 1 && ![...r[0].cells].some(c => c.colSpan > 1) ? [...r[0].cells] : null; };
  // The table's rows as the model has them; a row the page drew since the last pass takes its place after the row above it.
  function tmodel(t) {
    let last = -1, k = 0;
    return [...t.tBodies].flatMap(tb => [...tb.rows]).map(tr => {
      if (tr._tpos == null) tr._tpos = last + ++k * 1e-6; else { last = tr._tpos; k = 0; }
      const one = tr.cells.length === 1 && tr.cells[0].colSpan > 1;
      const kind = tr.matches('.empty, .skeleton') || (one && !tlabel(tr)) ? 'static' : one ? 'group' : 'row';
      return { kind, cells: kind === 'row' ? [...tr.cells].map(tcell) : [tlabel(tr)], pos: tr._tpos, tr };
    });
  }
  function tapply(t) {
    if (!t.tHead || !t.tBodies.length || t.matches('.sr, [data-tgrid="off"]') || t.closest('.sr, dialog')) return;
    const key = tkeyOf(t), st = tst(key), ths = theads(t);
    const owned = !!t.tHead.querySelector('button[data-sort]') || !ths;
    // A page with its own filter for the table marks it data-tfilter="page"; a classic listing has its server form.
    const ownFilter = t.dataset.tfilter === 'page' || !!t.closest('.listing')?.querySelector('.listing-form');
    const cols = ths ? ths.map(tlabel) : [];
    // A header whose words are all for screen readers names an action column (Acknowledge, Seen), unless it is the state glyph's.
    const seen = th => [...th.childNodes].some(n => n.nodeType === 3 ? n.textContent.trim() : n.nodeType === 1 && !n.matches('.sr') && tlabel(n));
    const first = [...t.tBodies].flatMap(tb => [...tb.rows]).find(tr => tr.cells.length === (ths || []).length);
    const sortable = ths ? ths.map((th, i) => !owned && !!tlabel(th) && (th.matches('.g') || first?.cells[i]?.matches('.g') || seen(th) || !!th.querySelector('button[data-tsort]')) && !th.matches('.act, .listing-select') && !th.querySelector('input, a, button:not([data-tsort])')) : [];
    let rows = tmodel(t);
    const groups = tsegments(rows).filter(s => s.head);
    if (owned && ownFilter && !groups.length && !t._tbar) return;
    // Header buttons, drawn once per header cell; aria-sort on the sorted th alone, as shell.list.sortHead draws it.
    if (!owned) ths.forEach((th, i) => {
      if (!sortable[i]) return;
      let b = th.querySelector('button[data-tsort]');
      if (!b) { b = document.createElement('button'); b.type = 'button'; b.className = 'sorter'; b.append(...th.childNodes); th.append(b); }
      b.dataset.tsort = cols[i];
      const on = st.sort === cols[i], dir = on ? (st.dir > 0 ? 'ascending' : 'descending') : null;
      if ((th.getAttribute('aria-sort') || null) !== dir) dir ? th.setAttribute('aria-sort', dir) : th.removeAttribute('aria-sort');
      const next = !on ? 'Sort ascending' : st.dir > 0 ? 'Sort descending' : 'Back to the page order';
      if (b.title !== next) b.title = next;
      const icon = on ? (st.dir > 0 ? 'arrow-up' : 'arrow-down') : 'arrow-up-down';
      let use = b.querySelector('svg.i[data-ticon] use');
      if (!use) { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('class', 'i'); s.setAttribute('aria-hidden', 'true'); s.dataset.ticon = ''; use = document.createElementNS('http://www.w3.org/2000/svg', 'use'); s.append(use); b.append(s); }
      if (use.getAttribute('href') !== `#i-${icon}`) use.setAttribute('href', `#i-${icon}`);
    });
    // Order: only when it differs, so the page's own order stands until a header asks for another.
    if (!owned) {
      const want = torder(rows, st, cols);
      [...t.tBodies].forEach(tb => {
        const mine = want.filter(r => r.tr.parentNode === tb).map(r => r.tr);
        if (mine.some((tr, i) => tb.rows[i] !== tr)) tb.append(...mine);
      });
    }
    // Folds: a band becomes a button that shows or hides the rows under it.
    const closed = new Set(st.closed), hid = new Set();
    groups.forEach(s => {
      const label = tlabel(s.head.tr), td = s.head.tr.cells[0], open = !closed.has(label);
      let b = td.querySelector(':scope > button.tfold');
      if (!b) {
        b = document.createElement('button'); b.type = 'button'; b.className = 'tfold';
        // One span holds the label, so its mixed font sizes share a baseline instead of each centring as a flex item.
        const text = document.createElement('span'); text.append(...[...td.childNodes]);
        put(b, html`${ICON('chevron-right')}`); b.append(text); td.append(b);
        s.head.tr.removeAttribute('aria-hidden');
      }
      b.dataset.tfold = label;
      if (b.getAttribute('aria-expanded') !== String(open)) b.setAttribute('aria-expanded', String(open));
      if (!open) s.body.forEach(r => hid.add(r.tr));
    });
    // A page's own text filter stands; the column filter still narrows its rows.
    const use = ownFilter ? { ...st, q: '' } : st, filterOn = tchips(use).length > 0;
    let shown = 0, total = 0;
    rows.forEach(r => {
      const out = hid.has(r.tr) || !tshows(r, use, cols);
      if (r.kind === 'row') { total++; if (tshows(r, use, cols)) shown++; }
      if (r.tr.hasAttribute('data-tout') !== out) r.tr.toggleAttribute('data-tout', out);
    });
    // The bar above the table (sd:5281, the toolbar standard): filter box and column filter from the first row (or while a
    // filter is on) and folds for 2 bands or more. A table that names its toolbar row (data-toolbar="<id>", the row with the
    // view buttons) puts the bar's parts in that row; otherwise the bar is its own row. A table the page's text filter covers
    // (data-tfilter="page") names the row that holds that filter, so the column filter sits beside it and no table gets two
    // text filters (the sd:5281 ruling; test_v2_shell_shared walks every page). The count sits right-aligned on the
    // table's title line (the .sec-head or .panel-head of the heading its or its section's aria-labelledby names); a title
    // line with the page's own .tally shows it, before the tally, only while a filter hides rows. A table with no title line
    // keeps the count at the bar's end.
    const { find: findOn, facet: facetOn, folds: foldsOn } = tbar({ total, filterOn, ownFilter, bands: groups.length });
    const facets = facetOn ? tfacets(cols, rows, sortable.map(s => !s)) : [];
    let bar = t._tbar;
    // A table in a hidden view keeps its parts out of the shared row; the view change rescans.
    const away = !!t.dataset.toolbar && !!t.closest('[hidden]');
    if (away || (!facetOn && !foldsOn)) { if (bar?.isConnected) bar.hidden = true; return; }
    if (!bar || !bar.isConnected) {
      bar = t._tbar = document.createElement('div'); bar.className = 'tgrid'; bar.dataset.tfor = key; bar._t = t;
      bar._name = t.getAttribute('aria-labelledby') ? tlabel(document.getElementById(t.getAttribute('aria-labelledby')) || t) : '';
      bar.setAttribute('role', 'group'); bar.setAttribute('aria-label', `Table controls${bar._name ? ': ' + bar._name : ''}`);
      put(bar, html`<label class="tfind"><span class="sr">Filter rows</span>${ICON('filter')}<input type="search" placeholder="Filter rows" autocomplete="off" spellcheck="false"></label><label class="tfacet"><span class="sr">Filter by a column value</span><select></select></label><span class="tfolds"><button class="linkbtn" type="button" data-tfold-all="open">Expand all</button><button class="linkbtn" type="button" data-tfold-all="shut">Collapse all</button></span><span class="tcount" aria-live="polite"></span><div class="tchips"></div>`);
      const row = t.dataset.toolbar && document.getElementById(t.dataset.toolbar);
      row ? row.append(bar) : twrap(t).before(bar);
      bar._count = bar.querySelector('.tcount');
      const named = t.getAttribute('aria-labelledby') || t.closest('[aria-labelledby]')?.getAttribute('aria-labelledby') || '';
      const line = document.getElementById(named)?.closest('.sec-head, .panel-head') || t.closest('section')?.querySelector(':scope > .sec-head, :scope > .panel-head'), tally = line?.querySelector(':scope > .tally');
      if (tally) tally.before(bar._count); else if (line) line.append(bar._count);
    }
    bar.hidden = false;
    const input = bar.querySelector('input'), sel = bar.querySelector('select'), count = bar._count;
    bar.querySelector('.tfind').hidden = !findOn; bar.querySelector('.tfolds').hidden = !foldsOn; bar.querySelector('.tfacet').hidden = !facets.length;
    const quiet = !facetOn || (shown === total && !!count.parentElement?.querySelector(':scope > .tally'));
    if (count.hidden !== quiet) count.hidden = quiet;
    const said = quiet ? '' : tcount(shown, total);
    if (count.textContent !== said) count.textContent = said;
    if (document.activeElement !== input && input.value !== st.q) input.value = st.q;
    // A shared row that serves two shown tables or more (Health's areas) names each one's table in its column filter.
    const many = !!t.dataset.toolbar && [...document.querySelectorAll(`main table[data-toolbar="${CSS.escape(t.dataset.toolbar)}"]`)].filter(x => !x.closest('[hidden]')).length > 1;
    const opts = html`<option value="">${many && bar._name ? `${bar._name}: filter by column…` : 'Filter by column…'}</option>${facets.map(c => html`<optgroup label="${c.label}">${c.values.map(v => html`<option value="${JSON.stringify([c.label, v])}">${v}</option>`)}</optgroup>`)}`;
    if (sel._t !== opts.text && document.activeElement !== sel) { put(sel, opts); sel._t = opts.text; }
    const chipsHtml = facetOn ? list.chips(tchips(use), shown, total, 'tunfilter') : html``, slot = bar.querySelector('.tchips');
    if (slot._t !== chipsHtml.text) { put(slot, chipsHtml); slot._t = chipsHtml.text; }
  }
  let tq = 0;
  // A bar whose table a redraw replaced goes, with its count: in a shared row (data-toolbar) nothing else removes it.
  const tscan = () => {
    tq = 0; document.querySelectorAll('main table').forEach(t => { try { tapply(t); } catch (e) { console.error('table grammar', e); } });
    document.querySelectorAll('.tgrid').forEach(bar => { if (!bar._t?.isConnected) { bar._count?.remove(); bar.remove(); } });
    tobs.takeRecords();
  };
  const tobs = new MutationObserver(recs => { if (!tq && !recs.every(r => r.target.closest?.('.tgrid'))) tq = requestAnimationFrame(tscan); });
  tobs.observe(body, { childList: true, subtree: true, characterData: false });
  document.addEventListener('shell:view', () => { if (!tq) tq = requestAnimationFrame(tscan); });
  const tfor = el => { const t = el.closest('table'); if (t) return t; const bar = el.closest('.tgrid'); return bar && document.querySelector(`table[data-tgrid="${CSS.escape(bar.dataset.tfor)}"]`); };
  const tdo = (el, f) => { const t = tfor(el); if (!t) return; const key = tkeyOf(t); f(tst(key)); tkeep(key); tapply(t); tobs.takeRecords(); };
  document.addEventListener('click', e => {
    const b = e.target.closest?.('button[data-tsort], button.tfold, .tgrid [data-tunfilter], .tgrid [data-tunfilter-all], .tgrid [data-tfold-all]'); if (!b) return;
    if (b.dataset.tsort != null) return tdo(b, st => tsortBy(st, b.dataset.tsort));
    if (b.classList.contains('tfold')) return tdo(b, st => { const s = new Set(st.closed); s.has(b.dataset.tfold) ? s.delete(b.dataset.tfold) : s.add(b.dataset.tfold); st.closed = [...s]; });
    if (b.dataset.tfoldAll) return tdo(b, st => { st.closed = b.dataset.tfoldAll === 'open' ? [] : [...tfor(b).querySelectorAll('button.tfold')].map(x => x.dataset.tfold); });
    const bar = b.closest('.tgrid');
    tdo(b, st => tunfilter(st, b.hasAttribute('data-tunfilter-all') ? null : b.dataset.tunfilter));
    bar.querySelector('input')?.focus();
  });
  document.addEventListener('input', e => { if (e.target.matches?.('.tgrid input')) tdo(e.target, st => { st.q = e.target.value.trim(); }); });
  document.addEventListener('change', e => {
    if (!e.target.matches?.('.tgrid select') || !e.target.value) return;
    const [label, v] = JSON.parse(e.target.value); e.target.value = '';
    tdo(e.target, st => { st.f = { ...st.f, [label]: v }; });
  });
  tscan();

  // ---------- The page's list: j / k, Esc and ?row= ----------
  // A page declares its list once, window.PAGE_LIST = { rows, select, id, current, clear, when }, and binds none of these keys.
  //   rows()          the row elements in reading order; j / k walk the shown ones (shownRow), so a hidden view's rows are skipped.
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
  // walk:start
  // A row is shown when it has a box and no hidden ancestor: a view panel the switch hid (view-<id>) holds no row j / k may
  // reach, whatever the page's stylesheet gives it (sd:5113).
  const shownRow = el => el.getClientRects().length > 0 && !el.closest('[hidden]');
  function step(d) {
    const L = window.PAGE_LIST; if (!L || (L.when && !L.when())) return false;
    const idOf = L.id || (el => el.dataset.id);
    const rows = [...L.rows()].filter(shownRow); if (!rows.length) return false;
    const cur = L.current ? L.current() : (() => { const on = rows.find(el => el.getAttribute('aria-selected') === 'true'); return on ? idOf(on) : null; })();
    const i = rows.findIndex(el => idOf(el) === cur);
    const n = rows[i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + d))];
    L.select(idOf(n), n); n.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return true;
  }
  // walk:end

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
      const fp = document.querySelector('details.fpick[open]'); if (fp) { fp.open = false; fp.querySelector('summary').focus(); return; }
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
    const all = [...rows], vis = all.filter(shownRow);
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
  // read:start
  // build: (sd:2418) shell.read(spec) is the page reader read.js defines (window.SHELL_READ): generation, retirement, selection
  // clearing, the failure states and the write barrier, in one place. A page without read.js before shell.js has no shell.read.
  const read = window.SHELL_READ && window.SHELL_READ({ fetch: (...a) => fetch(...a), commands, state, row: rowParam, listen: (t, f) => document.addEventListener(t, f) });
  // read:end
  // build: (sd:2418) `read` joins the shell's API; (view switch) so does `view`.
  window.shell = { ready, ICON, plural, state, read, post, getJSON, shown: stillShown, list, tables, chording: () => !!chord && Date.now() - chord < 1500, openPane, closePane, showTab, openChat, setContext, suggest, send, toast, confirm: a => confirmAction(a).then(r => r.yes), ask: a => confirmAction(a).then(({ yes, values }) => ({ yes, values })), commands, capture: openCapture, fileCapture, repos: readRepos, shq, itemHref, itemPage, openItem, time, attention, view, reconcile, url, row: (...a) => a.length ? writeRow(a[0]) : rowParam(), pages: PAGES, groups: GROUPS,
    // sd:3445: the rail's width in px, or null at the default; railWidth(px) sets and keeps it, railWidth(null) resets it.
    railWidth: (...a) => a.length ? setWidth(a[0]) : railWidth(store('sd.rail.width')) };
  if (location.hash === '#chat') openChat(); // screenshot hook
  if (location.hash === '#sheet') openPane('tab-details');
  if (location.hash === '#menu') setMenu(true);
})();
