// Places (sd:5663): the page of a realm with a report family of its own id, at /places?realm=<id>. It runs before places.js
// and shell.js: it names the page after the realm (SHELL_REALMS, from /ui/sections.js, so the rail marks its entry), and
// points each view at /api/place/<id>/<view>, which places.js reads when the view shows. A realm the list does not name
// keeps its views: each read says why it is unknown, with the line to add. Without ?realm= the page lists the realm places.
(() => {
  const { html } = window.markup;
  const $ = id => document.getElementById(id);
  const ID = /^[a-z0-9][a-z0-9-]*$/;
  const realms = (window.SHELL_REALMS || []).filter(r => ID.test(r.id));
  const id = new URLSearchParams(location.search).get('realm') || '';
  if (!ID.test(id)) {
    const places = realms.filter(r => r.place);
    window.PAGE_STATE = places.length
      ? { kind: 'empty', title: 'Pick a place', text: html`${places.map((r, i) => html`${i ? ' · ' : ''}<a href="/places?realm=${r.id}">${r.label}</a>`)}` }
      : { kind: 'empty', title: 'No realm place', text: 'A realm is a place when dashboard.report_families has a family with its id: add family|<id>|<label>|<icon>|<job>,... on Settings.' };
    return;
  }
  const realm = realms.find(r => r.id === id);
  // The shell finds the rail entry by [data-section="<label>"]: a label with a quote or a backslash is left to the id.
  const label = realm && !/["\\]/.test(realm.label) ? realm.label : id;
  document.body.dataset.page = label;
  document.body.dataset.place = id;
  document.title = `${label} · system`;
  $('title').textContent = label;
  $('subhead').textContent = !realm ? `dashboard.realms names no realm ${id}, or it was not read: each view says why.`
    : realm.place ? `The open items, briefs and reports of the realm ${label}.`
    : `Not in the rail: dashboard.report_families has no family ${id}. Reports says the line to add.`;
  document.querySelectorAll('[data-place-view]').forEach(b => { b.dataset.api = `/api/place/${id}/${b.dataset.placeView}`; });
  // Read again (sd:5638): the shell's head button reads the shown view again (places.js, which loads after this script).
  window.PAGE_AGAIN = { title: 'Read the shown view again', run: () => window.places.again() };
})();
