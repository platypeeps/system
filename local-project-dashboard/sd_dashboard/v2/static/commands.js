// Shared command declarations (sd:3524): one declaration per command, as products/system/commands.md asks. A page that
// lists a job, a work item, an assignment, a pull request or a collector registers these, not a copy. It loads before the
// page script, as snooze.js does; the page calls them once shell.js has made shell.commands.
// Each takes what differs per page: how a row names its facts, and the write, which goes through the page's own reader
// (a reread after a landed write, a load after a refused one). The id, label, key, risk, when, consequence, CLI line and
// toast text are written here once, so a change lands on every page together. tests/test_v2_commands.py fails a command
// id declared in two page scripts.
(() => {
  // A job write's 200 carries launchctl's answer (operations._act): only `accepted` sent the request; `failed` sent nothing;
  // anything else, or an answer with no request, is not known (sd:3388).
  function answered(out, name, verb, done) {
    const r = out && out.request || {};
    return r.status === 'accepted' ? done : r.status === 'failed' ? `${name} not ${verb}: ${r.message}`
      : `Not known whether ${name} was ${verb}: ${r.message || 'the answer named no outcome'}`;
  }

  // Fix (sd:3456): a herdr workspace in the item's checkout with claude on it, a builder session for a task and a debug
  // session for a report. It asks first, naming that, and posts /api/items/<id>/fix with the row's revision through
  // post(o, path), which answers the server's reply. A post with no answer may have started a session, so it says that. No Undo:
  // a session runs on. `item(o)` is the item id; `report(o)` is true for a report.
  function fix(on, { item, report, when = () => true, post }) {
    return { id: `${on}.fix`, on, label: 'Fix', key: 'f', risk: 'safe', askFirst: true, executes: true, icon: 'bot', when,
      cli: () => 'no sd verb: the dashboard runs local-herdr/herdr.sh launch',
      sends: o => `POST /api/items/${item(o)}/fix {revision}`,
      consequence: o => report(o)
        ? `Opens a herdr workspace and starts claude on report #${item(o)}: the sd-debug skill finds the cause and fixes it, and acknowledges the report after a clean run.`
        : `Opens a herdr workspace in the item's checkout and starts claude as a builder on #${item(o)}: a worktree, commits, a pull-request body and the lane.`,
      run: o => Promise.resolve().then(() => post(o, `/api/items/${item(o)}/fix`)).then(
        out => `Fix started · #${item(o)} · ${out.fix.session} session in herdr pane ${out.fix.pane}`,
        err => { throw err.status ? err : new Error(`no answer (${err.message}), so whether a session started is not known; look in herdr before you press Fix again`); }) };
  }

  // Retry: `sd jobs retry`, which sends the kickstart. `job(o)` names the row's facts: { name, service, failed, exit,
  // capability }; capability is operations.inventory's retry answer, and its reason is the off text. Exit 127 is "command
  // not found": Retry is not the row's act, and it asks first, naming the exit, so a path fixed outside the dashboard can be
  // tested (sd:3392). run(o) is the page's write; it words the answer with answered().
  function retry({ job, run }) {
    return { id: 'jobs.retry', on: 'job', label: 'Retry', key: 't', risk: 'safe', bulk: true,
      primary: o => !!job(o).failed && job(o).exit !== 127,
      when: o => { const j = job(o), c = j.capability; return !c ? (j.failed || 'no failed run to retry') : c.allowed || c.reason || 'launchd refuses a retry now'; },
      askFirst: o => job(o).exit === 127,
      consequence: o => job(o).exit === 127 ? `${job(o).name}'s last run ended with exit 127: command not found. A retry tests the path, and fails the same way unless the path was fixed.` : '',
      cli: o => `sd jobs retry ${job(o).name}`, sends: o => job(o).service ? `launchctl kickstart ${job(o).service}` : job(o).sends || '',
      run };
  }

  // Show assignment: `sd assignments get` prints what the page's Details show, so the line is copy only. run(o) shows it.
  function showAssignment({ primary = () => true, run }) {
    return { id: 'asg.get', on: 'assignment', label: 'Show assignment', key: 'o', risk: 'safe', primary, executes: false,
      cli: o => `sd assignments get ${o.n}`, run };
  }

  // Open a pull request on GitHub, in a new tab, from a pull request row or a review of one (`on`). `pr(o)` names
  // { repo: 'owner/name', number, url }.
  function openPullRequest({ on = 'pull request', pr, primary = () => true }) {
    return { id: on === 'pull request' ? 'pr.open' : `${on}.open`, on, label: 'Open on GitHub', key: 'o', risk: 'safe', primary,
      when: o => !!(pr(o).repo && pr(o).number) || 'the row names no pull request',
      cli: o => `gh pr view ${pr(o).number} --repo ${pr(o).repo} --web`,
      run: o => { window.open(pr(o).url || `https://github.com/${pr(o).repo}/pull/${pr(o).number}`, '_blank', 'noopener'); return 'Opened on GitHub in a new tab'; } };
  }

  // Re-run collector (sd:2207, sd:2894): the server runs sd shadow sync in a thread, bounded to 120 s, one run at a time; a
  // second start is refused while one is live. `started()` is the page's wait for the end; `page` names who reads again.
  function syncCollector({ started, page }) {
    return { id: 'collector.sync', on: 'collector', label: 'Re-run collector', key: 'r', risk: 'safe', primary: () => true,
      cli: () => 'sd shadow sync --max-seconds 120',
      run: () => window.shell.post('/api/shadow/sync', {}).then(() => { started(); return `Shadow sync started · ${page} reads again when it ends`; }) };
  }

  window.commands = { answered, fix, retry, showAssignment, openPullRequest, syncCollector };
})();
