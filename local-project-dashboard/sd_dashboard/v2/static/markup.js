// v2 markup (sd:2110): the one way a v2 script turns text into nodes, and the v2 tree's only HTML sink.
// html`…` keeps its literal parts as markup and escapes every value put in it, unless that value is itself
// html`…`, or an array of values each treated the same way; null, undefined and false put nothing.
// Only html`…` makes a value that put() takes, and html takes only a template literal's own frozen strings, so no
// string that a row, a person or the server sent is ever parsed as HTML. There is no raw() and no mark_safe:
// markup that is not a literal in a script cannot be made. put(el, m, where) parses m in a <template> and puts
// the nodes in el (replace, append, prepend) or before it.
// Escaping is enough in text and in a quoted attribute value, not inside a tag: `<div ${v}>` or `href=${v}` would make
// v an attribute. There html takes only html`…` (or nothing), and refuses a fragment whose own values sit bare in text,
// since such a fragment is only safe in text. No script can name a template's strings: tests/test_v2_today.py fails
// if a v2 script other than this one calls html other than as a tag, or reads markup.js any way but by destructuring.
// This file loads before every other v2 script. tests/test_markup.py greps the rest of the dashboard for sinks.
(() => {
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const made = new WeakSet();
  const value = v => made.has(v) ? v.text
    : Array.isArray(v) ? v.map(value).join('')
    : v === null || v === undefined || v === false ? ''
    : String(v).replace(/[&<>"']/g, c => ESC[c]);
  // A tag call passes a template object: frozen strings, and a frozen raw array held in a property that is neither
  // enumerable, writable nor configurable. In-page code could build that shape on purpose; the test above is what
  // stops a v2 script from trying, and this check stops an array or JSON value that merely arrives.
  const literal = s => {
    const raw = Array.isArray(s) && Object.getOwnPropertyDescriptor(s, 'raw');
    return !!raw && Object.isFrozen(s) && Array.isArray(raw.value) && Object.isFrozen(raw.value)
      && !raw.enumerable && !raw.writable && !raw.configurable && raw.value.length === s.length;
  };
  // Where the text so far leaves the next value: text, lt (just after <), tag (inside a tag), dq or sq (quoted value).
  const after = (state, text) => {
    for (const c of text) state = state === 'text' ? (c === '<' ? 'lt' : 'text')
      : state === 'lt' ? (/[A-Za-z\/!?]/.test(c) ? 'tag' : c === '<' ? 'lt' : 'text')
      : state === 'tag' ? (c === '"' ? 'dq' : c === "'" ? 'sq' : c === '>' ? 'text' : 'tag')
      : state === 'dq' ? (c === '"' ? 'tag' : 'dq')
      : (c === "'" ? 'tag' : 'sq');
    return state;
  };
  const bare = new WeakSet(); // html`…` with a value put straight into text: safe in text, not inside a tag
  const empty = v => v === null || v === undefined || v === false || v === '';
  const fits = v => empty(v) || (made.has(v) && !bare.has(v)) || (Array.isArray(v) && v.every(fits));
  const loose = v => made.has(v) ? bare.has(v) : Array.isArray(v) ? v.some(loose) : !empty(v);
  function html(strings, ...values) {
    if (!literal(strings)) throw new TypeError('html is a template tag: write html`…`');
    let text = strings[0], state = after('text', strings[0]), open = false;
    values.forEach((v, i) => {
      const before = strings[i], next = strings[i + 1];
      // A value right after =" or =' and closed by the same quote is a quoted attribute value, wherever the fragment goes.
      const q = /=\s*(["'])[^"'<>]*$/.exec(before), quoted = state === 'dq' || state === 'sq' || (q && next.startsWith(q[1]));
      if ((state === 'tag' || state === 'lt' || /=\s*$/.test(before)) && !quoted && !fits(v))
        throw new TypeError(`html: only html\`…\` goes inside a tag, not ${JSON.stringify(String(v)).slice(0, 60)}`);
      if (!quoted && state === 'text' && loose(v)) open = true;
      const out = value(v);
      text += out + next; state = after(after(state, out), next);
    });
    const m = Object.freeze({ text, toString() { return this.text; } });
    made.add(m);
    if (open) bare.add(m);
    return m;
  }
  const WHERE = { replace: 'replaceChildren', append: 'append', prepend: 'prepend', before: 'before' };
  function put(el, m, where = 'replace') {
    if (!made.has(m)) throw new TypeError('put() takes html`…` only');
    if (!Object.hasOwn(WHERE, where)) throw new TypeError(`put() where is one of ${Object.keys(WHERE).join(', ')}`);
    const t = document.createElement('template');
    t.innerHTML = m.text;
    el[WHERE[where]](t.content);
  }
  // A count and its noun: plural(2, 'session') is "2 sessions"; word() gives the noun alone, for a count set in <b>. A noun
  // that is already plural ("worktrees") is its own many. A hand-written plural ternary is how "2 sessionss" happened
  // (design review 2026-09-29, item 18). shell.js and tasks.js read it (sd:2124, from the design source at d82daa1).
  const word = (n, one, many = /s$/.test(one) ? one : `${one}s`) => (n === 1 ? one : many);
  const plural = Object.assign((n, one, many) => `${n} ${word(n, one, many)}`, { word });
  // An annunciator's lamps (sd:2680): one <li> per spec, the same markup on every page, so shell.css styles a lamp once.
  // A spec is { label, mark, val, small, sr, state, pressed, button, href, cls, attrs }. label, val, small and sr are text or
  // html`…`; mark is html`…` set at the label's end (an icon, a state glyph or a key). A lamp with href is a link, one with
  // button: true a button, any other a div that reads out and does nothing. attrs is html`…` holding the page's own
  // attributes (id, data-*, title); pressed sets aria-pressed. A falsy spec draws nothing. tests/test_v2_shell_shared.py
  // holds the grammar each page's suite checks its lamps against.
  const cells = specs => html`${specs.filter(Boolean).map(c => {
    const cls = c.cls ? ` ${c.cls}` : '';
    const at = html`${c.attrs}${c.state ? html` data-state="${c.state}"` : ''}${c.pressed === undefined ? '' : html` aria-pressed="${String(!!c.pressed)}"`}`;
    const inner = html`<span class="lbl">${c.label}${c.mark}</span><span class="val">${c.val}${c.small ? html`<small>${c.small}</small>` : ''}</span>${c.sr ? html`<span class="sr">${c.sr}</span>` : ''}`;
    return c.href ? html`<li><a class="cell${cls}" href="${c.href}"${at}>${inner}</a></li>`
      : c.button ? html`<li><button class="cell${cls}" type="button"${at}>${inner}</button></li>`
      : html`<li><div class="cell${cls}"${at}>${inner}</div></li>`;
  })}`;
  window.markup = Object.freeze({ html, put, plural, cells });
})();
