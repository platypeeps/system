// v2 markup (sd:2110): the one way a v2 script turns text into nodes, and the v2 tree's only HTML sink.
// html`…` keeps its literal parts as markup and escapes every value put in it, unless that value is itself
// html`…`, or an array of values each treated the same way; null, undefined and false put nothing.
// Only html`…` makes a value that put() takes, and html takes only a template literal's own frozen strings, so no
// string that a row, a person or the server sent is ever parsed as HTML. There is no raw() and no mark_safe:
// markup that is not a literal in a script cannot be made. put(el, m, where) parses m in a <template> and puts
// the nodes in el (replace, append, prepend) or before it.
// This file loads before every other v2 script. tests/test_markup.py greps the rest of the dashboard for sinks.
(() => {
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const made = new WeakSet();
  const value = v => made.has(v) ? v.text
    : Array.isArray(v) ? v.map(value).join('')
    : v === null || v === undefined || v === false ? ''
    : String(v).replace(/[&<>"']/g, c => ESC[c]);
  // A tag call passes the literal's strings frozen, with frozen raw strings beside them; an array a script builds is neither.
  const literal = s => Array.isArray(s) && Object.isFrozen(s) && Array.isArray(s.raw) && Object.isFrozen(s.raw);
  function html(strings, ...values) {
    if (!literal(strings)) throw new TypeError('html is a template tag: write html`…`');
    const m = Object.freeze({ text: strings.reduce((out, s, i) => out + value(values[i - 1]) + s), toString() { return this.text; } });
    made.add(m);
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
  window.markup = Object.freeze({ html, put });
})();
