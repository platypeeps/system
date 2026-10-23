// GitHub (sd:5781): the page script. It loads before issues.js, today-github.js and shell.js, which read what it declares.
// Three views: Pull requests (today-github.js, sd:5608), Your issues (sd:4628) and Intake (sd:4840), Today's GitHub and
// Issues views until sd:5781. Your issues and Intake are one read, /api/github/issues (issues.document, issues.intake),
// made when either view first shows and on Read again; issues.js draws them from the issues:read and intake:read events,
// and counts each tab. A failed read is each table's row naming it, never an empty list.
window.PAGE_VIEWS = [{ id: 'pulls', label: 'Pull requests' }, { id: 'issues', label: 'Your issues', key: 'y' },
  { id: 'intake', label: 'Intake' }];
(() => {
  let reads = 0, started = false;
  async function load() {
    const n = ++reads; started = true;
    let detail;
    try { detail = await window.shell.getJSON('/api/github/issues'); } catch (e) { detail = { issues_error: e.message, intake_error: e.message }; }
    if (n !== reads) return; // of overlapping reads only the newest draws
    document.dispatchEvent(new CustomEvent('issues:read', { detail: { issues: detail.issues || [], error: detail.issues_error || '', github: detail.github_error || '' } }));
    document.dispatchEvent(new CustomEvent('intake:read', { detail: { intake: detail.intake || [], error: detail.intake_error || '' } }));
  }
  document.addEventListener('shell:view', e => { if (['issues', 'intake'].includes(e.detail && e.detail.id) && !started) load(); });
  // Read again (sd:5638): every view that has been read reads again; Pull requests is today-github.js's own read.
  window.PAGE_AGAIN = { title: 'Read GitHub again', run: () => Promise.all([window.githubPulls && window.githubPulls.again(), started && load()]) };
})();
