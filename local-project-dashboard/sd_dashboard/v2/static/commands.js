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
    return { id: `${on}.fix`, on, label: 'Fix', does: o => report(o) ? 'Asks first, then starts claude in herdr to fix this report.' : 'Asks first, then starts claude in herdr as a builder on this item.', key: 'f', risk: 'safe', askFirst: true, executes: true, icon: 'bot', when,
      cli: () => 'no sd verb: the dashboard runs local-herdr/herdr.sh launch',
      sends: o => `POST /api/items/${item(o)}/fix {revision}`,
      consequence: o => report(o)
        ? `Opens a herdr workspace and starts claude on report #${item(o)}: the sd-debug skill finds the cause and fixes it, and acknowledges the report after a clean run.`
        : `Opens a herdr workspace in the item's checkout and starts claude as a builder on #${item(o)}: a worktree, commits, a pull-request body and the lane.`,
      run: o => Promise.resolve().then(() => post(o, `/api/items/${item(o)}/fix`)).then(
        out => `Fix started · #${item(o)} · ${out.fix.session} session in herdr pane ${out.fix.pane}`,
        err => { throw err.status ? err : new Error(`no answer (${err.message}), so whether a session started is not known; look in herdr before you press Fix again`); }) };
  }

  // Run now (jobs.retry): `sd jobs retry`, which sends the kickstart; the run-now pack allows an idle job's run too (sd:4982).
  // `job(o)` names the row's facts: { name, service, failed, exit,
  // capability }; capability is operations.inventory's retry answer, and its reason is the off text. Exit 127 is "command
  // not found": Retry is not the row's act, and it asks first, naming the exit, so a path fixed outside the dashboard can be
  // tested (sd:3392). run(o) is the page's write; it words the answer with answered().
  function retry({ job, run }) {
    return { id: 'jobs.retry', on: 'job', label: 'Run now', does: 'Starts the job now; after a failure, this reruns it.', key: 't', risk: 'safe', bulk: true,
      primary: o => !!job(o).failed && job(o).exit !== 127,
      when: o => { const j = job(o), c = j.capability; return !c ? (j.failed || 'no failed run to retry') : c.allowed || c.reason || 'launchd refuses a retry now'; },
      askFirst: o => job(o).exit === 127,
      consequence: o => job(o).exit === 127 ? `${job(o).name}'s last run ended with exit 127: command not found. A retry tests the path, and fails the same way unless the path was fixed.` : '',
      cli: o => `sd jobs retry ${job(o).name}`, sends: o => job(o).service ? `launchctl kickstart ${job(o).service}` : job(o).sends || '',
      run };
  }

  // Show log: the cron-jobs wrapper prints the job's log; the line is copy only. run(o) shows where the page keeps it: the
  // Details on Management, the job's row on Management > Schedules elsewhere (sd:2392). `job(o)` names { name }.
  // The filter puts the row on Schedules' first page.
  const openSchedule = job => { const to = window.shell.pages?.Management; if (!to) return 'Management is not built yet';
    location.href = `${to}?view=schedules&q=${encodeURIComponent(job)}&row=${encodeURIComponent(`cron:${job}`)}`; return `${job} opens in Management > Schedules`; };
  // The one cron-jobs line (sd:3525): Reports' report.log prints it too, so no page names the wrapper or a checkout path.
  const logLine = name => `local-cron-jobs/cron-jobs.sh logs ${name}`;
  function showLog({ job, run = o => openSchedule(job(o).name) }) {
    return { id: 'jobs.log', on: 'job', label: 'Show log', does: 'Shows the job\'s log.', key: 'l', risk: 'safe', executes: false,
      cli: o => logLine(job(o).name), run };
  }

  // Print: launchd's whole record of the job, copy only. It is the row's act after exit 127, where Retry is not (sd:3392).
  // `job(o)` names { service, exit }.
  function printJob({ job }) {
    return { id: 'jobs.print', on: 'job', label: 'Print', does: 'Gives the line that prints launchd\'s full record of the job.', key: 'p', risk: 'safe', executes: false, primary: o => job(o).exit === 127,
      when: o => !!job(o).service || 'the row names no launchd service', cli: o => `launchctl print ${job(o).service}`,
      run: () => 'Copy it into a terminal: the dashboard does not show the whole launchd record' };
  }

  // Show assignment: `sd assignments get` prints what the page's Details show, so the line is copy only. run(o) shows it.
  function showAssignment({ primary = () => true, run }) {
    return { id: 'asg.get', on: 'assignment', label: 'Show assignment', does: 'Shows the assignment\'s record in Details.', key: 'o', risk: 'safe', primary, executes: false,
      cli: o => `sd assignments get ${o.n}`, run };
  }

  // Open a pull request on GitHub, in a new tab, from a pull request row or a review of one (`on`). `pr(o)` names
  // { repo: 'owner/name', number, url }.
  function openPullRequest({ on = 'pull request', pr, primary = () => true }) {
    return { id: on === 'pull request' ? 'pr.open' : `${on}.open`, on, label: 'Open on GitHub', does: 'Opens the pull request on GitHub in a new tab.', key: 'o', risk: 'safe', primary,
      when: o => !!(pr(o).repo && pr(o).number) || 'the row names no pull request',
      cli: o => `gh pr view ${pr(o).number} --repo ${pr(o).repo} --web`,
      run: o => { window.open(pr(o).url || `https://github.com/${pr(o).repo}/pull/${pr(o).number}`, '_blank', 'noopener'); return 'Opened on GitHub in a new tab'; } };
  }

  // Re-run collector (sd:2207, sd:2894): the server runs sd shadow sync in a thread, bounded to 120 s, one run at a time; a
  // second start is refused while one is live. The page asks how it went every 5 s and calls reread() once it ends. Success
  // is quiet: the reread shows it. A failed tracker or a run that broke stays as a toast with its first reason. `page`
  // names who reads again.
  const SYNC_POLL = 5000;
  function syncCollector({ reread, page }) {
    const wait = () => setTimeout(() => window.shell.getJSON('/api/shadow/state').then(s => {
      if (s.running) return wait();
      reread();
      const bad = s.error || s.trackers.filter(t => !t.ok && t.configured).map(t => `${t.tracker}: ${t.reason || 'failed'}`)[0];
      if (bad) window.shell.toast(`Shadow sync ended with a problem: ${bad}`);
    }, e => window.shell.toast(`The sync's state was not read: ${e.message}`)), SYNC_POLL);
    return { id: 'collector.sync', on: 'collector', label: 'Re-run collector', does: 'Runs shadow sync now, for up to 2 minutes, then reads again.', key: 'r', risk: 'safe', primary: () => true,
      cli: () => 'sd shadow sync --max-seconds 120',
      run: () => window.shell.post('/api/shadow/sync', {}).then(() => { wait(); return `Shadow sync started · ${page} reads again when it ends`; }) };
  }

  // Adversarial review (sd:5064): POST /api/adversarial starts a codex review of the row in the background (adversarial.py).
  // `payload(o)` is the row's { kind, target } (a change adds item and body). A 202 is followed here: GET
  // /api/adversarial/<id> every 10 s while the page is open, until the run settles: failed with no item, or `filed` set.
  // Each answer fires 'adversarial:run' on document, so a Details pane that shows reviewPane(kind, target) draws again.
  // A refusal is its toast in the server's words: a 409 names the run already going. The run is one row's, so no bulk.
  // sd:5100: a page may name its own `id`, `label`, `does`, `fields` (the shell's dialog fields, whose values reach
  // payload(o, v)) and `consequence`; Adversarial review: PR and Deep review on a repo row are two more of these.
  // sd:5101: a source review's payload adds `source`, its ledger row's key; its run key is `source:<checkout> <row key>`.
  const REVIEW_POLL = 10000, RUNS = new Map();
  const runKey = (kind, target) => `${kind}:${target}`;
  const settled = r => (r.state === 'failed' && r.item == null) || r.filed != null;
  function settledText(r) {
    if (r.state === 'failed') return `Adversarial review failed · run ${r.id}: ${r.reason || 'no reason recorded'}${r.filed && r.filed !== 'written' ? ` · ${r.filed}` : ''}`;
    return r.filed === 'written' ? `Adversarial review filed · run ${r.id} · brief sd:${r.brief_item}; Details links it` : `Adversarial review done · run ${r.id} · ${r.filed}`;
  }
  function follow(key, run) {
    RUNS.set(key, run);
    document.dispatchEvent(new CustomEvent('adversarial:run', { detail: { key, run } }));
    if (settled(run)) { window.shell.toast(settledText(run)); return; }
    // A failed read is not known, not settled: Details says so and the next poll tries again. A newer run on the same row
    // ends this run's polling: an answer for it would draw over the newer one.
    const still = window.shell.shown(`adversarial ${key}`, run.id, () => RUNS.get(key)?.id);
    setTimeout(() => still() && window.shell.getJSON(`/api/adversarial/${encodeURIComponent(run.id)}`).then(next => still() && follow(key, next),
      e => still() && follow(key, { ...run, unread: e.message })), REVIEW_POLL);
  }
  function adversarialReview(on, { payload, when = () => true, id = `${on}.adversarial-review`, label = 'Adversarial review',
    does = 'Starts a codex review in the background; its brief lists tasks to pick.', fields, consequence }) {
    return { id, on, label, does, risk: 'safe', executes: true, icon: 'shield-alert', when, fields, consequence, askFirst: !!consequence,
      cli: () => 'no sd verb: the dashboard runs local-adversarial-gate in the background',
      sends: (o, v) => `POST /api/adversarial ${JSON.stringify(payload(o, v || {}))}`,
      run: (o, v) => { const p = payload(o, v || {});
        return window.shell.post('/api/adversarial', p).then(run => { follow(runKey(p.kind, p.source ? `${p.target} ${p.source}` : p.target), run); return `Review started · run ${run.id}`; },
          err => { if ([400, 404, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether a review started is not known`); }); } };
  }
  // The Details section for the row's latest run on this page, or nothing: its state, then the brief and the item it filed.
  // A running run says how long it has run of its bound (sd:5100: a deep review runs up to 90 min). `title` names the pane.
  const minutes = s => Math.max(0, Math.round(s / 60));
  function reviewPane(kind, target, title = 'Adversarial review') {
    const r = RUNS.get(runKey(kind, target)); if (!r) return '';
    const { html } = window.markup;
    const S = window.shell;
    const links = r.brief_item ? html` · <a href="${S.itemPage(r.brief_item)}">brief sd:${r.brief_item}</a>` : '';
    const item = r.item != null ? html` · <a href="${S.itemHref(r.item)}">#${r.item}</a>` : '';
    const ran = r.state === 'running' && r.started && r.timeout ? `, ${minutes((Date.now() - Date.parse(r.started)) / 1000)} of at most ${minutes(r.timeout)} min` : '';
    const state = settled(r) ? settledText(r) : `Run ${r.id} is ${r.state}${ran}${r.unread ? `; its state was not read: ${r.unread}` : ''}. This page reads it every 10 s.`;
    return html`<section class="adv-run" aria-live="polite"><h3>${title}</h3><p class="why">${state}${links}${item}</p></section>`;
  }

  window.commands = { answered, fix, retry, showLog, logLine, printJob, showAssignment, openPullRequest, syncCollector, adversarialReview, reviewPane };
})();
