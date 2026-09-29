// v2 markup (sd:2110): the one way a v2 script turns text into nodes, and the v2 tree's only HTML sink.
// html`…` keeps its literal parts as markup and escapes every value put in it, unless that value is itself
// html`…`, or an array of values each treated the same way; null, undefined and false put nothing.
// nodes() and put() take only what html`…` made, so no string that a row, a person or the server sent is ever
// parsed as HTML. There is no raw() and no mark_safe: markup that is not a literal in a script cannot be made.
// This file loads before every other v2 script. tests/test_markup.py greps the rest of the dashboard for sinks.
(() => {
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const made = new WeakSet();
  const value = v => made.has(v) ? v.text
    : Array.isArray(v) ? v.map(value).join('')
    : v === null || v === undefined || v === false ? ''
    : String(v).replace(/[&<>"']/g, c => ESC[c]);
  function html(strings, ...values) {
    if (!Array.isArray(strings) || !Array.isArray(strings.raw)) throw new TypeError('html is a template tag: write html`…`');
    const m = Object.freeze({ text: strings.reduce((out, s, i) => out + value(values[i - 1]) + s), toString() { return this.text; } });
    made.add(m);
    return m;
  }
  function nodes(m) {
    if (!made.has(m)) throw new TypeError('nodes() takes html`…` only');
    const t = document.createElement('template');
    t.innerHTML = m.text;
    return t.content;
  }
  const put = (el, m) => { el.replaceChildren(nodes(m)); };
  window.markup = Object.freeze({ html, nodes, put });
})();
