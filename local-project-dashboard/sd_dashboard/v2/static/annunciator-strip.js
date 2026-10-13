// <annunciator-strip>: the row of lamps at the top of a dashboard page (patterns.md § The annunciator strip).
// Plain JS, light DOM, no framework, no build step, no inline style: it survives a CSP with no 'unsafe-inline'.
//
// The server renders <ul class="annunciator"> inside the element; without scripts every lamp still shows.
// A lamp takes one of three forms: a button.cell[data-key][aria-pressed] toggles a filter, an a.cell opens a reading,
// and a div.cell has nothing to filter and says why. The Observed clock (button.cell.clock) is not a lamp.
//
//   strip.render(lamps)  draws the lamps from the page's data; each lamp is
//                        { key | href, label, state, value: [phrase…], note?, icon?, pressed? } or { clock: true, label, value, note? }.
//                        A phrase is text or [number, text]: the number shows bold. A missing or unknown state reads unknown.
//                        Any lamp or the clock may add id, title, data ({ open: … } sets data-open) and kbd (a key hint
//                        after the label), the hooks a page's own script and checks read.
//   strip.press(keys)    sets aria-pressed on every lamp from the page's filter set (a lamp and its chip are one control).
//   strip.wait()         shows the wait (aria-busy) and holds the lamps until the next render().
//   strip.unknown(why)   a failed read: every lamp turns unknown with the reason; the clock stays. render() recovers.
//
// Events, from the element: annunciator:toggle { key, pressed } (cancelable: preventDefault keeps the lamp as it was)
// and annunciator:refresh from the clock.
(() => {
  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  // The word a screen reader hears after the label; ok stays quiet (principles.md § 2).
  const WORD = { ok: '', caution: 'caution', warning: 'warning', queued: 'queued', unknown: 'not read' };
  const SVG = 'http://www.w3.org/2000/svg';

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  // The glyph and the spoken word that go with a state colour: colour never carries state alone.
  const mark = state => {
    const g = el('span', 'g', GLYPH[state]);
    g.setAttribute('aria-hidden', 'true');
    return WORD[state] ? [' ', g, el('span', 'sr', ` ${WORD[state]}`)] : [' ', g];
  };
  const value = (phrases = [], note) => {
    const v = el('span', 'val');
    phrases.forEach((p, i) => {
      if (i) v.append(' · ');
      const ph = el('span', 'ph');
      if (Array.isArray(p)) ph.append(el('b', '', String(p[0])), ` ${p[1] ?? ''}`.trimEnd());
      else ph.textContent = String(p);
      v.append(ph);
    });
    if (note != null) v.append(el('small', '', String(note)));
    return v;
  };
  const icon = id => {
    const svg = document.createElementNS(SVG, 'svg'), use = document.createElementNS(SVG, 'use');
    svg.setAttribute('class', 'i');
    svg.setAttribute('aria-hidden', 'true');
    use.setAttribute('href', id);
    svg.append(use);
    return svg;
  };

  // The page's own hooks on a lamp: an id, a title, data attributes and a key hint after the label.
  const hooks = (c, l) => {
    if (l.id) c.id = l.id;
    if (l.title) c.title = l.title;
    for (const [k, v] of Object.entries(l.data ?? {})) c.dataset[k] = String(v);
    if (l.kbd) c.querySelector('.lbl').append(' ', el('kbd', '', l.kbd));
    return c;
  };

  function cell(l) {
    let c;
    if (l.clock) {
      c = el('button', 'cell clock');
      c.type = 'button';
      const lbl = el('span', 'lbl', l.label ?? 'Observed');
      if (l.icon) lbl.append(icon(l.icon));
      c.append(lbl, ' ', value(l.value, l.note));
      return hooks(c, l);
    }
    const state = Object.hasOwn(GLYPH, l.state) ? l.state : 'unknown';
    if (l.href) { c = el('a', 'cell'); c.href = l.href; }
    else if (l.key != null) { c = el('button', 'cell'); c.type = 'button'; c.dataset.key = l.key; c.setAttribute('aria-pressed', String(!!l.pressed)); }
    else c = el('div', 'cell');
    c.dataset.state = state;
    const lbl = el('span', 'lbl');
    if (l.icon) lbl.append(icon(l.icon));
    lbl.append(String(l.label ?? ''), ...mark(state));
    c.append(lbl, ' ', value(l.value, l.note));
    return hooks(c, l);
  }

  class AnnunciatorStrip extends HTMLElement {
    connectedCallback() {
      if (this.wired) return;
      this.wired = true;
      this.addEventListener('click', e => this.onClick(e));
    }
    get list() {
      let ul = this.querySelector(':scope > ul.annunciator');
      if (!ul) { ul = el('ul', 'annunciator'); this.append(ul); }
      return ul;
    }
    get busy() { return this.getAttribute('aria-busy') === 'true'; }

    // Which control holds focus, by what survives a redraw: the clock, a lamp's key or a reading's address.
    focused() {
      const c = document.activeElement?.closest?.('.cell');
      if (!c || !this.contains(c)) return null;
      return c.classList.contains('clock') ? 'clock' : c.dataset.key != null ? `key:${c.dataset.key}` : c.getAttribute('href') ? `href:${c.getAttribute('href')}` : null;
    }
    find(id) {
      return [...this.querySelectorAll('.cell')].find(c => id === (c.classList.contains('clock') ? 'clock'
        : c.localName === 'button' && c.dataset.key != null ? `key:${c.dataset.key}` : c.localName === 'a' ? `href:${c.getAttribute('href')}` : null));
    }

    render(lamps) {
      const was = this.focused();
      this.list.replaceChildren(...lamps.map(l => { const li = el('li'); li.append(cell(l)); return li; }));
      this.removeAttribute('aria-busy');
      // A redraw keeps focus on the same control; a lamp that left the strip hands it to the clock.
      if (was) (this.find(was) || this.find('clock'))?.focus();
    }
    press(keys) {
      const on = new Set(keys);
      for (const c of this.querySelectorAll('button.cell[data-key]')) c.setAttribute('aria-pressed', String(on.has(c.dataset.key)));
    }
    wait() { this.setAttribute('aria-busy', 'true'); }
    unknown(reason) {
      const was = this.focused();
      for (const c of this.querySelectorAll('.cell:not(.clock)')) {
        const lbl = c.querySelector('.lbl')?.cloneNode(true) ?? el('span', 'lbl');
        lbl.querySelectorAll('.g, .sr').forEach(e => e.remove());
        lbl.normalize();
        if (lbl.lastChild?.nodeType === Node.TEXT_NODE) lbl.lastChild.textContent = lbl.lastChild.textContent.trimEnd();
        lbl.append(...mark('unknown'));
        const d = el('div', 'cell');
        d.dataset.state = 'unknown';
        d.append(lbl, ' ', value(['not read'], reason));
        c.replaceWith(d);
      }
      this.removeAttribute('aria-busy');
      // An unknown lamp does nothing, so focus on a lamp moves to the clock.
      if (was && was !== 'clock') this.find('clock')?.focus();
    }

    onClick(e) {
      const c = e.target.closest('button.cell');
      if (!c || !this.contains(c) || this.busy) return;
      if (c.classList.contains('clock')) { this.dispatchEvent(new CustomEvent('annunciator:refresh', { bubbles: true })); return; }
      if (c.dataset.key == null) return;
      const pressed = c.getAttribute('aria-pressed') !== 'true';
      c.setAttribute('aria-pressed', String(pressed));
      const go = this.dispatchEvent(new CustomEvent('annunciator:toggle', { bubbles: true, cancelable: true, detail: { key: c.dataset.key, pressed } }));
      if (!go) c.setAttribute('aria-pressed', String(!pressed));
    }
  }

  if (!customElements.get('annunciator-strip')) customElements.define('annunciator-strip', AnnunciatorStrip);
})();
