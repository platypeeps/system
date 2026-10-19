// Settings (sd:3438): the theme buttons are theme.js's; this script wires the sections width to shell.railWidth (sd:3445)
// and says Saved after a change. Fleet (sd:3989) lists the sd.* keys one value serves on every machine, and Sweeps (sd:5109)
// the Run sweep settings, from /api/settings.
// It loads before shell.js, so it reads the shell once the page has loaded.
window.PAGE_ATTENTION = { state: 'ok', n: 0, what: 'settings' };
document.addEventListener('DOMContentLoaded', () => {
  const S = window.shell, set = document.getElementById('rail-width'), out = document.getElementById('rail-width-out');
  // A preset's value is its width in px; Default's is empty (tokens.css --rail, 216 px). A dragged width checks no preset.
  // A change autosaves and says so quietly (foundation patterns.md § Forms).
  const show = (w, saved) => {
    for (const r of set.querySelectorAll('input')) r.checked = r.value === (w ? String(w) : '');
    out.textContent = (w ? `${w} px` : 'default, 216 px') + (saved ? ' · Saved' : '');
  };
  set.addEventListener('change', e => show(S.railWidth(e.target.value || null), true));
  // The rail's edge and its keys set the width too; the shell says so once the width is kept.
  document.addEventListener('shell:rail-width', e => show(e.detail, true));
  // theme.js stores the choice on its own click listener; this one only says Saved.
  document.getElementById('theme').addEventListener('click', e => {
    if (e.target.closest('[data-theme-choice]')) document.getElementById('theme-saved').textContent = 'Saved';
  });
  show(S.railWidth());
  sections(S);
});

// Fleet (sd:3989) and Sweeps (sd:5109), from one read of /api/settings. A change autosaves through the section's POST and
// says Saved, or names the value the set replaced when it is not the one shown. A refusal keeps what was typed and says
// why. A set past its bound has an unknown outcome, and a sweep row changed since the read refuses (409): either way the
// row says so, and the page reads the values again. A sweep post names the value shown; an empty one unsets the key.
function sections(S) {
  const { html, put } = window.markup;
  const SECTIONS = {
    fleet: { post: '/api/settings/fleet', name: k => `sd.${k.key}`, ok: 'One value serves every machine. A change saves at once.' },
    sweeps: { post: '/api/settings/sweep', name: k => k.key, before: true,
              ok: 'What Run sweep reads on Tasks, for every machine. A change saves at once; an empty value unsets it.' },
  };
  const SOURCE = { database: 'workflow database', file: 'config.json on this machine', default: 'unset · default applies', unset: 'unset' };
  const KEYS = {};
  const row = (k, name) => html`<li data-key="${k.key}">
    <div class="what"><h3 id="f-${k.key}-h">${name}</h3><p>${k.description}</p></div>
    <div class="control">
      ${k.choices ? html`<select class="fld" id="f-${k.key}" data-key="${k.key}" aria-labelledby="f-${k.key}-h">${k.value === null ? html`<option value="" selected disabled>unset</option>` : ''}${k.choices.map(c => html`<option value="${c}"${c === k.value ? html` selected` : ''}>${c}</option>`)}</select>`
        : html`<input class="fld" id="f-${k.key}" data-key="${k.key}" type="text" value="${k.value ?? ''}" placeholder="unset" spellcheck="false" autocapitalize="off" autocomplete="off" aria-labelledby="f-${k.key}-h">`}
      <span class="src">${SOURCE[k.source] || k.source}</span>
      <span class="saved" id="f-${k.key}-out" role="status"></span>
    </div></li>`;
  async function read(said = {}) {
    let doc;
    try { doc = await S.getJSON('/api/settings'); } catch (err) { doc = { reason: `the page did not read it: ${err.message}` }; }
    for (const [id, section] of Object.entries(SECTIONS)) {
      KEYS[id] = doc[id] || [];
      put(document.getElementById(id), html`${KEYS[id].map(k => row(k, section.name(k)))}`);
      document.getElementById(`${id}-note`).textContent = doc[id] ? section.ok : `Not read: ${doc[`${id}_reason`] || doc.reason}.`;
    }
    for (const [key, text] of Object.entries(said)) { const out = document.getElementById(`f-${key}-out`); if (out) out.textContent = text; }
  }
  for (const [id, section] of Object.entries(SECTIONS)) document.getElementById(id).addEventListener('change', async e => {
    const key = e.target.dataset?.key, shown = KEYS[id].find(k => k.key === key);
    if (!shown) return;
    const out = document.getElementById(`f-${key}-out`);
    out.textContent = 'Saving…';
    let set;
    try { set = await S.post(section.post, section.before ? { key, value: e.target.value, before: shown.value } : { key, value: e.target.value }); }
    catch (err) {
      if (err.status === 409) return read({ [key]: `Not saved: ${err.message}. The value shown is the one read now.` });
      if (err.status && err.status < 500) { out.textContent = `Not saved: ${err.message}`; return; }
      return read({ [key]: `Outcome unknown: ${err.message}. The value shown is the one read after.` });
    }
    await read({ [key]: set.was === shown.value ? 'Saved' : `Saved · it held ${set.was ?? 'unset'}, not ${shown.value ?? 'unset'} as shown` });
  });
  read();
}
