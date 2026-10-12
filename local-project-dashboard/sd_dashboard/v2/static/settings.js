// Settings (sd:3438): the theme buttons are theme.js's; this script wires the sections width to shell.railWidth (sd:3445)
// and says Saved after a change.
// It loads before shell.js, so it reads the shell once the page has loaded. Nothing here talks to the server.
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
});
