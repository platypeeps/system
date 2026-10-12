// Settings (sd:3438): the theme buttons are theme.js's; this script wires the sections width to shell.railWidth (sd:3445).
// It loads before shell.js, so it reads the shell once the page has loaded. Nothing here talks to the server.
window.PAGE_ATTENTION = { state: 'ok', n: 0, what: 'settings' };
document.addEventListener('DOMContentLoaded', () => {
  const S = window.shell, input = document.getElementById('rail-width'), out = document.getElementById('rail-width-out');
  // null is the default width (tokens.css --rail); the slider rests on 216 px, its value.
  const show = w => { input.value = String(w || 216); out.textContent = w ? `${w} px` : 'default'; };
  input.addEventListener('input', () => show(S.railWidth(input.value)));
  document.getElementById('rail-reset').addEventListener('click', () => show(S.railWidth(null)));
  show(S.railWidth());
});
