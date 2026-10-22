// Decide (sd:5781): what waits on a ruling. It loads before review.js, decisions.js and shell.js, which read what it declares.
// Two views: Decisions (decisions.js, Today's view until sd:5781) and Scoring (review.js, the Review page until sd:5781).
// Each script reports what waits in its view through window.decideCount(view, n), and n null means not read, which hides
// the tab's count. The rail badge is the sum of the known counts, the rule pages/decide.py serves for the other pages.
// Read again reads both views.
// moved:start
// The old Review address (/review) serves this page: it opens on Scoring, keeping its query (?batch=, ?reviewed=).
if (location.pathname === '/review') history.replaceState(history.state, '', `/decide${location.search}#view=scoring`);
// moved:end
window.PAGE_VIEWS = [{ id: 'decisions', label: 'Decisions' }, { id: 'scoring', label: 'Scoring' }];
(() => {
  const COUNTS = {}, WHAT = { decisions: 'decisions waiting', scoring: 'questions open' };
  window.decideCount = (view, n) => {
    COUNTS[view] = n;
    window.shell?.view?.count(view, n);
    const known = Object.keys(WHAT).filter(k => COUNTS[k]), sum = known.reduce((a, k) => a + COUNTS[k], 0);
    window.shell?.attention?.({ state: sum ? 'caution' : 'ok', n: sum, what: known.map(k => WHAT[k]).join(' and ') || 'decisions or questions' });
  };
  // review.js sets its own Read again for the queue; this one runs it and reads Decisions too.
  addEventListener('DOMContentLoaded', () => {
    const queue = window.PAGE_AGAIN;
    window.PAGE_AGAIN = { title: 'Read decisions and the review queue again',
      run: () => Promise.all([queue && queue.run(), window.decisions && window.decisions.load()]) };
  });
})();
