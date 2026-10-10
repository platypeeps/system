// Snooze (sd:1896): Today and Health hide a row until a time. It loads before the page script, which registers the
// commands once shell.js has made shell.commands and draws the rows its document lists as snoozed.
// The one write is POST /api/snooze { page, row, until, seen }; until null shows the row again. The server judges the time
// (later than now, at most 31 days away), so the page computes it and says when; it keeps no snooze of its own.
// `seen` is the row's own, as the document gave it: the snooze holds only while the row's problem reads the same.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put } = window.markup;
  // The fixed choices. The design's Snooze keeps its id and key: until the next 08:00, which is tomorrow's after 08:00.
  const at8 = now => { const d = new Date(now); d.setHours(8, 0, 0, 0); if (d <= now) d.setDate(d.getDate() + 1); return d; };
  const CHOICES = [
    { id: 'snooze', label: 'Snooze until 08:00', key: 'z', until: at8 },
    { id: 'snooze-hour', label: 'Snooze 1 hour', key: 'h', until: now => new Date(+now + 3600e3) },
    { id: 'snooze-week', label: 'Snooze 1 week', key: 'w', until: now => new Date(+now + 7 * 864e5) },
  ];
  const CLI = () => 'no sd verb: the dashboard writes the snooze (POST /api/snooze)';
  // A time today shows as 08:00; any other day carries its date.
  const hm = d => d.toTimeString().slice(0, 5);
  const when = (d, now = new Date()) => d.toDateString() === now.toDateString() ? hm(d) : `${d.toDateString().slice(0, 10)} ${hm(d)}`;

  // `types` are the page's row types; `off(o)` names why a row has nothing to snooze, or is false; `reread` reads the page
  // again, so a snoozed row leaves the list and an unsnoozed one comes back. Undo writes the other way.
  function register(C, { page, types, off = () => false, reread }) {
    const post = (row, until, seen) => window.shell.post('/api/snooze', { page, row, until, seen }).then(v => { reread(); return v; });
    types.forEach(t => CHOICES.forEach(ch => C.register({ id: `${t}.${ch.id}`, on: t, label: ch.label, key: ch.key, risk: 'undo', bulk: true,
      when: o => off(o) || true, cli: CLI,
      run: o => { const until = ch.until(new Date()); return post(o.id, until.toISOString(), o.seen).then(() => `Snoozed until ${when(until)} · ${o.label}`); },
      undo: o => post(o.id, null, o.seen) })));
    C.register({ id: 'snoozed row.unsnooze', on: 'snoozed row', label: 'Unsnooze', key: 's', risk: 'undo', bulk: true, primary: () => true, cli: CLI,
      run: o => post(o.row, null, o.seen).then(() => `Shows again · ${o.label}`),
      undo: o => post(o.row, o.until, o.seen) });
  }

  // One object per snoozed row, apart from the rows shown: its own type carries Unsnooze and nothing else.
  const object = r => ({ id: `snoozed:${r.id}`, type: 'snoozed row', label: r.what, row: r.id, until: r.until, seen: r.seen });

  // The snoozed rows under the page's list, closed until opened; a redraw keeps it open.
  function draw(el, rows) {
    const open = !!el.querySelector('details[open]');
    put(el, rows.length ? html`<details class="snoozed"${open ? html` open` : ''}><summary>Snoozed · ${rows.length}</summary>
      <table aria-label="Snoozed rows"><tbody>${rows.map(r => html`<tr><td class="what">${r.what}<span class="detail">until ${when(new Date(r.until))}</span></td>
        <td class="act">${window.shell.commands.rowActions(`snoozed:${r.id}`)}</td></tr>`)}</tbody></table></details>` : html``);
  }
  // A snooze read that failed hides nothing; the reader's partial state says so.
  const unread = (why, source) => ({ kind: 'partial', text: `Snoozes were not read: ${why}. Every row shows.`, source });

  window.snooze = { CHOICES, when, register, object, draw, unread };
})();
