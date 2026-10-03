// The v2 shell's reader (sd:2418): the one way a page reads its document. It loads before shell.js, which builds
// shell.read from it (the read:start block there). The page draws everything; the reader decides only when a document
// is current and which command objects are live, so each page gets the same guards without writing them:
//   generation  each read is numbered; an answer or a failure from a read that is not the newest changes nothing.
//   retirement  after each read, an object the page put before and did not put again is retired: its pick is dropped and
//               it becomes a 'not listed' object no command is declared on, so no row menu, Details bar or bulk bar runs on it.
//   selection   after each read, a selection whose row is gone moves to the page's first row, or clears.
//   failure     a failed load clears the page; a failed reread after a write keeps the rows, retires their objects and
//               says the write landed.
//   barrier     a reread never settles on a read that started before its write landed (PR 73, r4167523338).
//
// const rows = shell.read({
//   source: '/api/rows',            the document route
//   what: 'the rows',               the loading, error and partial texts name it
//   adopt: doc => ({ objects, state }),  keep the document; the objects to put, and the state (null, partial, empty, error)
//   clear: err => {},               drop the page's data after a failed read
//   draw: () => {},                 redraw from the page's data
//   current: () => id,              the page's selection; the first read asks ?row= instead
//   first: () => id,                the row to select when the current one is gone
//   select: (id, opened) => {},     select a row; opened: it is the first read's ?row=
//   unselect: () => {},             nothing selected: Details shows the page's nothing-selected text
// });
// rows.load();     the first read and each refresh
// rows.reread();   after a write landed. A refused write calls load(): only reread() says the change landed.
(() => {
  const capital = s => s.charAt(0).toUpperCase() + s.slice(1);
  window.SHELL_READ = ({ fetch, commands: C, state, row, listen }) => spec => {
    const { source, what } = spec;
    let generation = 0, applied = 0, putIds = new Set(), picks = [], flight = null, queued = null;
    listen('shell:picked', e => { picks = (e && e.detail) || []; });

    async function get() {
      const r = await fetch(source, { headers: { Accept: 'application/json' } });
      const out = await r.json().catch(() => null);
      if (!r.ok) throw new Error((out && out.error) || `HTTP ${r.status}`);
      return out;
    }
    // The pick goes first: the shell refuses to toggle a pick on a type no bulk command is on.
    function retire(keep) {
      picks.filter(id => !keep.has(id)).forEach(id => C.pick(id));
      putIds.forEach(id => { if (!keep.has(id)) C.put({ id, type: 'not listed', label: `${C.get(id)?.label || id} (no longer listed)` }); });
      putIds = keep;
    }
    function unselect() { C.select(null); spec.unselect(); }
    function settle(first) {
      const asked = first ? row() : spec.current();
      const id = putIds.has(asked) ? asked : spec.first();
      if (id != null && putIds.has(id)) spec.select(id, first && id === asked);
      else unselect();
    }
    async function run(after) {
      const mine = ++generation;
      if (!after) state({ kind: 'loading', text: `Reading ${what}. Rows appear when ${source} answers.`, source });
      let doc;
      try { doc = await get(); } catch (err) {
        if (mine !== generation) return false;
        retire(new Set());
        if (after) {
          spec.draw(); unselect();
          state({ kind: 'partial', text: `The change landed; ${what} were not read again: ${err.message}. Reload reads them.`, source });
        } else {
          spec.clear(err); spec.draw(); unselect();
          state({ kind: 'error', text: `${capital(what)} were not read: ${err.message}. Reload retries it.`, source });
        }
        return false;
      }
      if (mine !== generation) return false;
      const { objects = [], state: now = null } = spec.adopt(doc) || {};
      retire(new Set(objects.map(o => o.id)));
      objects.forEach(o => C.put(o));
      state(now);
      spec.draw();
      settle(!applied++);
      return true;
    }
    function start(after) {
      const f = { p: null };
      f.p = run(after).finally(() => { if (flight === f) flight = null; });
      flight = f;
      return f.p;
    }
    // A read in flight may have started before this write landed, so a reread never joins it: it queues one read behind
    // it, and every reread called before that read starts shares it. N writes in one bulk run end in at most two reads.
    function reread() {
      if (queued) return queued;
      if (!flight) return start(true);
      const q = queued = flight.p.catch(() => {}).then(() => { queued = null; return start(true); });
      return q;
    }
    return { load: () => start(false), reread };
  };
})();
