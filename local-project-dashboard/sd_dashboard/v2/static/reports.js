// Reports (sd:2121): the design source's products/system/designs/v2/reports.js at 7f50ad4, ported. Each change from the
// reference is marked "build:". The rows are /api/reports (reports_screen.document), never sample data: the newest 200
// database reports, the scheduled jobs, each job's run cadence from its log, and the job families the config folder names.
// A command that executes posts to a route server.action_route answers, and its toast comes after the write lands.
const { html, put, plural } = window.markup;
const G = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so byId(), backticks and \ stay literal
const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const CRON = '~/repos/system/local-cron-jobs/cron-jobs.sh';
const byId = id => document.getElementById(id);

// ---------- Data (build): /api/reports, read on load and after each write ----------
let DOC = null, READ = '', DAYS = [], ROWS = [], JOBS = {}, CAD = {}, FAMILIES = [], FAM_OF = {}, byJob = {};
const csrf = () => document.querySelector('meta[name="sd-csrf"]')?.content || '';
async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-SD-CSRF': csrf() }, body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(out.error || `HTTP ${r.status}`); e.stale = r.status === 409; throw e; }
  return out;
}
async function getJSON(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
  return out;
}
// build: the document's sources say which reads failed; a failed one renders as unknown with its reason, never as empty.
const why = s => DOC?.sources?.[s] || '';
const dayName = d => new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'UTC' });
// build: a log path names the account's home; Details shows it from ~ (the design replaced one fixed home).
const tilde = p => String(p || '').replace(/^\/(Users|home)\/[^/]+(?=\/|$)/, '~');
function absorb(doc) {
  DOC = doc; READ = doc.read || new Date().toISOString(); DAYS = doc.days || [];
  document.body.dataset.observed = READ; // shell time cells count from the reading, not from now
  JOBS = Object.fromEntries((doc.jobs || []).map(j => [j.name, j]));
  CAD = doc.cadence || {};
  FAMILIES = (doc.families?.list || []).map(f => [f.key, f.label, f.icon, f.jobs]);
  FAM_OF = {}; FAMILIES.forEach(([k, , , jobs]) => jobs.forEach(j => FAM_OF[j] = k));
  // Rows: database reports. build: no status mail is read (doc.mail.reason), so the list holds reports only.
  ROWS = (doc.reports || []).map(r => {
    // build: attention is the report's own fields.attention, not a match on its title.
    const attn = r.attention, open = r.status !== 'done';
    return { id: 'r' + r.id, kind: 'db', n: r.id, job: r.job || 'unknown job', what: r.title, at: r.at, open, attn,
      act: open && attn, s: open ? (attn ? 'warning' : 'queued') : 'ok',
      detail: [r.basis, r.exit != null && !/^job exited/.test(r.basis || '') ? 'exit ' + r.exit : '', r.repeats ? `${r.repeats.count} runs` : ''].filter(Boolean).join(' · '), source: r };
  });
  ROWS.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  byJob = {}; ROWS.forEach(r => (byJob[r.kind + ':' + r.job] ||= []).push(r));
}
const prevOf = r => { const l = byJob[r.kind + ':' + r.job]; return l[l.indexOf(r) + 1] || null; };

// Cadence mark for a job on a day. build: the marks come from the job's log as reports_screen.cadence reads it; a day
// the launchd calendar leaves out is not scheduled; the quiet mark needs status mail, which is not read.
function mark(job, day) {
  const c = CAD[job], i = DAYS.indexOf(day);
  if (!c || i < 0) return ['unknown', '▨', 'not read'];
  if (!c.scheduled[i]) return ['none', '·', 'not scheduled'];
  if (!c.log || (c.from && day < c.from)) return ['unknown', '▨', 'no log yet'];
  if (c.read_from && day < c.read_from) return ['unknown', '▨', 'not read'];
  const [ok, bad] = c.runs[day] || [0, 0], n = ok + bad;
  // build: today is not over; a job that has not run yet today is not a gap.
  if (!n && i === DAYS.length - 1) return ['none', '·', 'no run yet today'];
  if (!n) return ['caution', '▲', 'no run logged'];
  if (bad && !ok) return ['warning', '■', `${bad} failed`];
  if (bad) return ['caution', '▲', `${ok} ok, ${bad} failed`];
  return ['ok', '●', n > 1 ? `${ok} ran` : 'ran'];
}
// build: the newest scheduled day with a mark, so a run still to come today does not hide yesterday's failure.
function lastState(job) {
  for (let i = DAYS.length - 1; i >= 0; i--) { const s = mark(job, DAYS[i])[0]; if (s !== 'none') return s; }
  return 'none';
}
const glyphClass = s => `g-${s === 'none' ? 'queued' : s}`;

// Family lamps.
function famState(jobs) {
  const rows = ROWS.filter(r => jobs.includes(r.job));
  const act = rows.filter(r => r.act);
  // Failing = the last logged run failed outright: the run cadence table's "■ failed last run", so the lamps and the table count the same jobs.
  // A partial last day (▲) is named apart; a fresh report without a log is a report that needs action, not a failing job.
  const bad = jobs.filter(j => CAD[j] && lastState(j) === 'warning');
  const partial = jobs.filter(j => CAD[j] && lastState(j) === 'caution');
  const s = bad.length ? 'warning' : act.length || partial.length ? 'caution' : 'ok';
  return { s, act: act.length, bad, partial };
}
function lamps() {
  const out = FAMILIES.map(([k, label, icon, jobs]) => {
    const f = famState(jobs);
    const val = f.s === 'ok' ? html`<span class="ph">all clear</span>` : f.bad.length ? html`<span class="ph"><b>${f.bad.length}</b> failing</span> · <span class="ph">${f.act} need action</span>` : html`<span class="ph"><b>${f.act}</b> need action</span> · <span class="ph">${f.partial.length ? `${f.partial.length} partial` : 'none failing now'}</span>`;
    return html`<li><button class="cell" type="button" data-fam="${k}" data-state="${f.s}" aria-pressed="${String(F.fam === k)}" title="${[...f.bad, ...f.partial.map(j => j + ' (partial)')].join(', ')}"><span class="lbl">${label}${I(icon)}</span><span class="val">${val}</span></button></li>`;
  });
  // build: without a family list the page draws no family lamp; this one says which file it reads.
  const fam = DOC.families || {};
  if (fam.state !== 'read' && !FAMILIES.length) out.push(html`<li><div class="cell" data-state="unknown" title="${fam.problems?.join('; ') || ''}"><span class="lbl">Job families${I('clipboard-list')}</span><span class="val"><span class="ph">no family list</span> · <span class="ph">${fam.source || 'report-families.conf'}</span></span></div></li>`);
  // A lamp with nothing to filter is a div that says why (design.md § Lamps); the partial read above the table says the rest.
  // build: the count is the stored reports that did not come from cron-report; the HTML count is the documents roots.
  const other = ROWS.filter(r => r.source.source !== 'cron-report').length;
  out.push(html`<li><div class="cell" data-state="unknown"><span class="lbl">Runner &amp; review${I('git-pull-request')}</span><span class="val"><span class="ph">${other ? html`<b>${other}</b> other reports` : 'no reports'}</span> <span class="ph">stored</span></span></div></li>`);
  out.push(html`<li><div class="cell" data-state="unknown"><span class="lbl">HTML reports${I('file-text')}</span><span class="val">${DOC.html == null ? html`<span class="ph">not read</span>` : html`<span class="ph"><b>${DOC.html}</b> folders</span>`} · <span class="ph">not indexed</span></span></div></li>`);
  out.push(html`<li><button class="cell" type="button" id="refresh"><span class="lbl">Observed${I('rotate-ccw')}</span><span class="val"><span class="ph"><b>${READ.slice(11, 16)}</b> UTC</span> · <span class="ph">refresh</span></span></button></li>`);
  put(byId('annunciator'), html`${out}`);
}
// The shell's state slot (design.md § State slot). build: it names every source that was not read, and status mail.
function pageState() {
  const failed = Object.entries(DOC.sources || {}).filter(([, v]) => v).map(([k, v]) => `${k} (${v})`);
  const fam = DOC.families || {};
  const parts = [`Database reports only: ${DOC.mail?.reason || 'status mail is not read'}.`];
  if (failed.length) parts.push(`Not read: ${failed.join(', ')}.`);
  if (fam.state === 'missing') parts.push(`No family list: copy project-dashboard/report-families.conf.example to ${fam.source}.`);
  else if (fam.problems?.length) parts.push(`${fam.source}: ${fam.problems.join('; ')}.`);
  window.shell.state({ kind: 'partial', text: parts.join(' '), source: `/api/reports · ${READ.slice(0, 16).replace('T', ' ')} UTC` });
}

// Cadence table. build: a job no family names is listed under "Other jobs", so no installed job is hidden.
let CAD_JOBS = [];
function cadJobs() {
  const named = FAMILIES.flatMap(([k, , , jobs]) => jobs.filter(j => CAD[j]).map(j => [k, j]));
  const rest = Object.keys(CAD).filter(j => !FAM_OF[j]).sort().map(j => ['', j]);
  return [...named, ...rest];
}
const interesting = ([, j]) => DAYS.some(d => { const [s, , t] = mark(j, d); return s !== 'ok' && s !== 'none' && t !== 'no log yet'; }) || byJob['db:' + j]?.[0]?.act;
let cadAll = false, jobFilter = '';
function renderCad() {
  if (!DOC.cadence) {
    put(byId('cad-head'), html``);
    put(byId('cad-body'), html`<tr><td class="m unk">The run cadence was not read: ${why('cadence') || 'no reason given'}.</td></tr>`);
    put(byId('cad-tally'), html``); byId('cad-more').hidden = true;
    return;
  }
  put(byId('cad-head'), html`<tr><th scope="col">Job</th>${DAYS.map(d => html`<th scope="col"><abbr title="${d}">${dayName(d)}</abbr></th>`)}</tr>`);
  put(byId('cad-caption'), html`${DAYS.length ? `${dayName(DAYS[0])} ${DAYS[0].slice(8)} – ${dayName(DAYS[DAYS.length - 1])} ${DAYS[DAYS.length - 1].slice(8)}` : ''}, local time. Tap a job to see its reports.`);
  const list = cadAll ? CAD_JOBS : CAD_JOBS.filter(interesting);
  let lastFam = null;
  put(byId('cad-body'), html`${list.map(([f, j]) => {
    const fs = f !== lastFam; lastFam = f;
    return html`<tr data-job="${j}"${fs ? html` class="fam"` : ''}${j === jobFilter ? html` aria-current="true"` : ''}><th scope="row" class="job"><button type="button" aria-label="${j}: show its reports">${j}</button></th>${DAYS.map(d => { const [s, g, t] = mark(j, d); return html`<td class="m${s === 'none' ? ' none' : s === 'unknown' ? ' unk' : ''}" title="${d}: ${t}"><span class="${glyphClass(s)}" aria-hidden="true">${g}</span><span class="sr">${t}</span></td>`; })}</tr>`;
  })}`);
  const b = byId('cad-more');
  b.hidden = false; b.textContent = cadAll ? 'Show only jobs with gaps' : `Show all ${CAD_JOBS.length} jobs`; b.setAttribute('aria-expanded', String(cadAll));
  const counts = { warning: 0, caution: 0 };
  CAD_JOBS.forEach(([, j]) => { const s = lastState(j); if (s in counts) counts[s]++; });
  put(byId('cad-tally'), html`<span class="g-warning">■ ${counts.warning} failed last run</span><span class="g-caution">▲ ${counts.caution} partial</span><span>${list.length} of ${CAD_JOBS.length} shown</span>`);
}
byId('cad-more').addEventListener('click', () => { cadAll = !cadAll; renderCad(); });
byId('cad-body').addEventListener('click', e => { const tr = e.target.closest('tr[data-job]'); if (!tr) return; setJob(jobFilter === tr.dataset.job ? '' : tr.dataset.job); byId('led-h').scrollIntoView?.({ block: 'start' }); });

// Filters.
const F = { job: '', kind: '', status: '', day: '', act: false, fam: '', q: '', clean: '' };
let picks = new Set(), days = [];
function fillFilters() {
  const jobs = [...new Set(ROWS.map(r => r.job))].sort();
  put(byId('f-job'), html`<option value="">All jobs</option>${jobs.map(j => html`<option>${j}</option>`)}`);
  days = [...new Set(ROWS.map(r => r.at.slice(0, 10)))].sort().reverse();
  put(byId('f-day'), html`<option value="">Any day</option>${days.map(d => html`<option value="${d}">${new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })}</option>`)}`);
  byId('f-job').value = F.job; byId('f-day').value = F.day;
}
function setJob(j) { jobFilter = j; F.job = j; byId('f-job').value = j; page = 0; renderCad(); renderRows(); }
byId('f-job').addEventListener('change', e => setJob(e.target.value));
[['f-kind', 'kind'], ['f-status', 'status'], ['f-day', 'day']].forEach(([id, k]) => byId(id).addEventListener('change', e => { F[k] = e.target.value; page = 0; renderRows(); }));
byId('f-act').addEventListener('change', e => { F.act = e.target.checked; page = 0; renderRows(); });
byId('annunciator').addEventListener('click', e => {
  const c = e.target.closest('button.cell'); if (!c) return;
  if (c.id === 'refresh') return load(); // build: refresh reads /api/reports again, not the whole page
  F.fam = F.fam === c.dataset.fam ? '' : c.dataset.fam;
  document.querySelectorAll('.cell[data-fam]').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.fam === F.fam)));
  page = 0; renderRows();
});

const PAGE = 50; let page = 0, shown = [];
const tbody = byId('rows');
function matches(r) {
  if (F.job && r.job !== F.job) return false;
  if (F.kind && r.kind !== F.kind) return false;
  if (F.status === 'open' && !(r.kind === 'db' && r.open)) return false;
  if (F.status === 'done' && !(r.kind === 'db' && !r.open)) return false;
  if (F.day && r.at.slice(0, 10) !== F.day) return false;
  if (F.act && !r.act) return false;
  if (F.clean && !F.clean.has(r.n)) return false; // build: the clean set is the server's preview, not a rule rebuilt here
  if (F.fam && FAM_OF[r.job] !== F.fam) return false;
  if (F.q && !(r.what + ' ' + r.detail + ' ' + r.job + ' ' + (r.source.body || '')).toLowerCase().includes(F.q)) return false;
  return true;
}
function stCell(r) {
  return r.open ? html`<td class="st open">${I('inbox')}open</td>` : html`<td class="st">${I('check')}acknowledged</td>`;
}
// Rail badge (shell.js): the loudest state on this page and how many rows carry it.
const pageAttention = (xs, what) => { const w = xs.filter(s => s === 'warning').length, c = xs.filter(s => s === 'caution').length; window.PAGE_ATTENTION = { state: w ? 'warning' : c ? 'caution' : 'ok', n: w || c, what: what[w ? 'warning' : 'caution'] }; window.shell?.attention?.(); };
function renderRows() {
  pageAttention(ROWS.map(r => r.s), { warning: 'reports want you', caution: 'status mails flagged' });
  shown = ROWS.filter(matches);
  const pages = Math.max(1, Math.ceil(shown.length / PAGE)); page = Math.min(page, pages - 1);
  if (started) writeURL();
  const slice = shown.slice(page * PAGE, page * PAGE + PAGE);
  let lastDay = '';
  put(tbody, html`${slice.length ? slice.map(r => {
    const d = r.at.slice(0, 10), head = d !== lastDay; lastDay = d;
    return [head ? html`<tr class="day" aria-hidden="true"><td colspan="6">${new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' })} · UTC</td></tr>` : '',
    html`<tr data-id="${r.id}" data-changed="${r.at}" aria-selected="${String(r.id === sel)}"${picks.has(r.id) ? html` data-picked` : ''}><td class="g g-${r.s}">${G[r.s]}<span class="sr">${r.s}</span></td><td class="what"><button type="button">${r.what}</button></td><td class="detail">${r.detail}</td><td class="when"><time class="rel" datetime="${r.at}"></time></td>${stCell(r)}<td class="act">${window.shell.commands.rowActions(r.id)}</td></tr>`];
  }) : html`<tr class="day"><td colspan="6">${DOC.reports ? 'Nothing matches. Clear a filter, or press → in the bar to capture it as a task.' : `The reports were not read: ${why('reports')}.`}</td></tr>`}`);
  byId('range').textContent = shown.length ? `${page * PAGE + 1}–${Math.min(shown.length, page * PAGE + PAGE)} of ${shown.length}${shown.length !== ROWS.length ? ` (filtered from ${ROWS.length})` : ''}` : `0 of ${ROWS.length}`;
  byId('prev').disabled = page === 0; byId('next').disabled = page >= pages - 1;
  // A filter or a page change that drops the selected report from the slice moves the selection to a row on screen (sd:2306).
  // With no row on screen, Details clears too, so its buttons never act on a report the list does not show.
  // A filter that clears it leaves cleared set, so the next render with rows selects the first one again.
  if (started && (sel != null || cleared)) window.shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: sel, select: id => { cleared = false; select(id, false); },
    clear: clearDetails });
  const dbOpen = ROWS.filter(r => r.kind === 'db' && r.open);
  // build: no flagged-mail count; status mail is not read.
  put(byId('tally'), html`<span class="g-warning">■ ${dbOpen.filter(r => r.attn).length} need attention</span><span class="g-queued">◌ ${dbOpen.filter(r => !r.attn).length} open run reports</span>`);
}
byId('prev').addEventListener('click', () => { page--; renderRows(); });
byId('next').addEventListener('click', () => { page++; renderRows(); });

// Line diff against the previous report of the same job (LCS on lines).
function diff(a, b) {
  const A = a.split('\n'), B = b.split('\n'), n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) { if (A[i] === B[j]) { out.push(['same', A[i]]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) out.push(['del', A[i++]]); else out.push(['add', B[j++]]); }
  while (i < n) out.push(['del', A[i++]]); while (j < m) out.push(['add', B[j++]]);
  return out;
}
// Run ids and timestamps change every run; mask them so the diff shows what the job said, not when.
const norm = t => String(t || '').replace(/\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+-]\d{2}:?\d{2})?/g, '‹time›').replace(/\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?/g, '‹time›').trim();

const details = byId('details');
let sel = null;
let cleared = false; // set while a filter shows no report
function clearDetails() { sel = null; cleared = true; put(details, html`<p class="why">No report selected · none is visible under this filter.</p>`); }
function show(id) {
  const r = ROWS.find(x => x.id === id); if (!r) return;
  sel = id;
  tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === id)));
  const p = prevOf(r), x = r.source;
  let dff = '';
  if (p) {
    const ops = diff(norm(p.source.body), norm(x.body));
    const changed = ops.filter(o => o[0] !== 'same').length;
    dff = html`<h3>${I('git-compare')} Since the previous report · <time class="rel" datetime="${p.at}"></time></h3>${changed
      ? html`<pre class="diff" aria-label="Line diff">${ops.map(([k, t]) => html`<span class="${k}">${t || ' '}</span>`)}</pre><p class="note">${ops.filter(o => o[0] === 'add').length} lines added, ${ops.filter(o => o[0] === 'del').length} removed. Times are masked.${x.cut || p.source.cut ? ' One side is cut to its last 1400 characters, so the diff is partial.' : ''}</p>`
      : html`<p class="why">Same text as the previous report, times aside.</p>`}`;
  } else dff = html`<h3>${I('git-compare')} Since the previous report</h3><p class="why">No earlier stored report from ${r.job} in the newest ${DOC.limit || 200}.</p>`;
  const cjob = JOBS[r.job] ? r.job : null;
  const jobAct = cjob ? html`<h3>Job · ${cjob}</h3>${window.shell.commands.bar('job:' + cjob)}` : '';
  const strip = CAD[r.job] ? html`<h3>Last 7 days</h3><p class="tally">${DAYS.map(d => { const [s, g, t] = mark(r.job, d); return html`<span title="${d}: ${t}"><span class="${glyphClass(s)}" aria-hidden="true">${g}</span> ${dayName(d)}<span class="sr"> ${t}</span></span>`; })}</p>` : '';
  const rep = x.repeats ? `${x.repeats.count} runs, last ended ${x.repeats.last ? new Date(x.repeats.last).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : 'not recorded'}` : 'one run';
  // build: a report's open followups, and fields the store cannot parse, are named where the design had only the attention line.
  const waits = !x.fields_read ? html`<p class="why">This report's stored fields are missing or not valid JSON, so its source cannot be read. Acknowledge it from the command line: <code>sd reports acknowledge ${x.id}</code>.</p>`
    : r.open && x.followups.length ? html`<p class="why">An open followup (${x.followups.map(n => `note ${n}`).join(', ')}) holds this report. Resolve it with <code>sd note resolve ${x.followups[0]}</code>, then acknowledge.</p>` : '';
  put(details, html`<p class="kind"><span class="g-${r.s}" aria-hidden="true">${G[r.s]}</span> Database report #${x.id} · ${r.open ? (r.attn ? 'needs attention' : 'open') : 'acknowledged'}</p>
    <h2>${r.what}</h2>
    <h3>Act</h3>${window.shell.commands.bar(id)}${jobAct}
    ${r.open && r.attn ? html`<p class="why">Acknowledge closes the report, not the fault. The next failure opens a new one.</p>` : ''}${waits}
    <h3>Report source</h3>
    <dl><dt>Job</dt><dd>${x.job || 'not recorded'}</dd><dt>Started</dt><dd>${x.started ? html`<time class="rel" datetime="${x.started}"></time>` : 'not recorded'}</dd><dt>Ended</dt><dd>${x.ended ? html`<time class="rel" datetime="${x.ended}"></time>` : 'not recorded'}</dd><dt>Exit code</dt><dd>${x.exit ?? '—'}</dd>
      <dt>Source log</dt><dd><code>${tilde(x.src) || '—'}</code></dd><dt>Attention evidence</dt><dd>${x.basis || '—'}</dd><dt>Repeats</dt><dd>${rep}</dd><dt>Run</dt><dd><code>${x.run || '—'}</code></dd><dt>Recorded</dt><dd><time class="rel" datetime="${x.at}"></time></dd></dl>
    ${x.truncated ? html`<p class="why">The job wrote more than the store keeps; this report is truncated. The source log has the rest.</p>` : ''}
    ${strip}
    <h3>Report</h3>${x.cut ? html`<p class="note">Last 1400 characters shown.</p>` : ''}<div class="mailbody">${x.body || html`<span class="note">No text stored.</span>`}</div>
    ${dff}`);
  window.shell.commands.select(id);
  window.shell.setContext(`${r.job} · report #${x.id}`);
  window.shell.suggest([`What changed in ${r.job} since its last good run?`, `Why did ${r.job} ${r.attn ? 'fail' : 'report'} this time?`, `Show every open report from ${FAMILIES.find(f => f[0] === FAM_OF[r.job])?.[1] || 'this family'}`]);
  details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap'));
}
// The URL carries the filters and the row, so a view can be bookmarked and the saved-view chips know which one is current.
// build: no "Status mail" view, since no status mail is read.
const views = () => [
  { name: 'Needs action', params: { act: '1' } },
  { name: 'Open reports', params: { kind: 'db', status: 'open' } },
  ...(days[0] ? [{ name: 'Newest day', params: { day: days[0] } }] : []),
];
function readURL() {
  const p = new URLSearchParams(location.search);
  ['job', 'kind', 'status', 'day'].forEach(k => { const v = p.get(k) || ''; if (!v) return; const el = byId('f-' + k); if ([...el.options].some(o => o.value === v)) { F[k] = v; el.value = v; } });
  F.act = p.get('act') === '1'; byId('f-act').checked = F.act;
  if (F.job) jobFilter = F.job;
  if (FAMILIES.some(([k]) => k === p.get('fam'))) { F.fam = p.get('fam'); document.querySelectorAll('.cell[data-fam]').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.fam === F.fam))); }
  if (p.get('q')) { F.q = p.get('q').toLowerCase(); byId('shift').value = p.get('q'); }
  page = Math.max(0, (+(p.get('page') || p.get('p')) || 1) - 1); // ?page= is the shared pager key that views reset; ?p= is an old link
}
function writeURL() {
  const p = new URLSearchParams();
  ['job', 'kind', 'status', 'day'].forEach(k => { if (F[k]) p.set(k, F[k]); });
  if (F.act) p.set('act', '1');
  if (F.fam) p.set('fam', F.fam);
  if (F.q) p.set('q', F.q);
  if (page) p.set('page', page + 1);
  window.shell.url(p); // the shell keeps ?row=
  window.shell.views(views());
}
function select(id, open) { show(id); writeURL(); if (open) window.shell.openPane('tab-details'); }
tbody.addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

// Acknowledge clean: a filter plus picks; the shell's bulk bar applies (products/system/commands.md).
// build: Select clean asks the server for v1's preview (reporting.clean_reports at the date), so the selection and every
// decline reason are the library's: an open followup, queued work, or a run newer than its job's last recorded tick.
async function selectClean() {
  const before = byId('ack-date').value;
  let preview;
  try { preview = await getJSON(`/api/reports/clean?before=${encodeURIComponent(before)}`); }
  catch (err) { byId('ack-out').hidden = false; byId('ack-sum').textContent = `No preview: ${err.message}`; put(byId('ack-sel'), html``); put(byId('ack-dec'), html``); return; }
  const byN = Object.fromEntries(ROWS.map(r => [r.n, r]));
  const selected = preview.selected.filter(n => byN[n]), away = preview.selected.length - selected.length;
  byId('ack-sel-h').textContent = `Selected (${preview.selected.length})`;
  byId('ack-dec-h').textContent = `Declined (${preview.declined.length})`;
  put(byId('ack-sel'), html`${(selected.length ? selected.map(n => html`<li><b>#${n}</b> ${byN[n].what}</li>`) : html`<li>None.</li>`)}`);
  put(byId('ack-dec'), html`${(preview.declined.length ? preview.declined.map(d => html`<li><b>#${d.id}</b> ${byN[d.id]?.job || 'a report'}: ${d.why}</li>`) : html`<li>None.</li>`)}`);
  byId('ack-sum').textContent = selected.length
    ? `${plural(selected.length, 'clean report')} picked and listed under Reports. The bar at the bottom acknowledges them.${away ? ` ${away} more are older than the newest ${DOC.limit || 200}; the classic Reports screen acknowledges those.` : ''}`
    : preview.count ? `${preview.count} clean reports, none in the newest ${DOC.limit || 200}; the classic Reports screen acknowledges them.` : 'No clean open report before that date.';
  F.clean = new Set(selected); page = 0; byId('ack-out').hidden = false; byId('ack-clear').hidden = false;
  [...picks].forEach(id => window.shell.commands.pick(id));
  selected.forEach(n => window.shell.commands.pick('r' + n));
  renderRows(); byId('led-h').scrollIntoView?.({ block: 'start' });
}
byId('ack-preview').addEventListener('submit', e => { e.preventDefault(); selectClean(); });
byId('ack-clear').addEventListener('click', () => { F.clean = ''; [...picks].forEach(id => window.shell.commands.pick(id)); byId('ack-out').hidden = true; byId('ack-clear').hidden = true; renderRows(); });

// Shapeshift bar: filter / new task / ask, previewed before commit.
const input = byId('shift'), prev = byId('shift-preview'), as = byId('shift-as'), ghost = byId('ghost');
let mode = 'filter';
function guess(v) { return /\?$|^(why|what|how|which|when|should)\b/i.test(v) ? 'ask' : /^(p[1-4]\b|todo\b|add\b)|\bdue\b/i.test(v) ? 'task' : 'filter'; }
function setMode(m) { mode = m; prev.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', String(c.dataset.as === m))); render(); }
function render() {
  const v = input.value.trim();
  prev.hidden = !v;
  ghost.textContent = v ? `→ ${mode}` : '';
  put(as, html`${mode === 'task' ? html`${I('list-todo')} task “${v}”` : mode === 'ask' ? html`${I('message-square')} ask chat about reports` : html`${I('filter')} filter reports, bodies included`}`);
  F.q = mode === 'filter' ? v.toLowerCase() : ''; page = 0; renderRows();
}
input.addEventListener('input', () => { mode = guess(input.value.trim()); setMode(mode); });
input.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); const order = ['filter', 'task', 'ask']; setMode(order[(order.indexOf(mode) + 1) % 3]); }
  if (e.key === 'Enter' && input.value.trim()) {
    e.preventDefault();
    if (mode === 'task') { const v = input.value.trim(); input.value = ''; render(); window.shell.capture(null, v); }
    if (mode === 'ask') { window.shell.openChat(); window.shell.send(input.value.trim()); input.value = ''; render(); }
  }
  if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; render(); }
});
prev.addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) { setMode(c.dataset.as); input.focus(); } });
document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
  if (e.key === '/') { e.preventDefault(); input.focus(); }
});
// The shell walks these rows on j / k.
window.PAGE_KEYS = [['/', 'Filter reports, capture a task or ask'], ['→', 'In the field: the next reading (filter, task, ask)']];
window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), select: id => select(id, false) };

window.PAGE_COMMANDS = [
  { label: 'Filter, capture or ask', icon: 'search', key: '/', run: () => input.focus() },
  { label: 'Select clean reports', icon: 'check', run: () => selectClean() },
  { label: 'Show reports that need action', icon: 'filter', run: () => { byId('f-act').checked = true; F.act = true; page = 0; renderRows(); } },
];

// ---------- Commands (products/system/commands.md) ----------
// Each row is an object; each command is declared once and the shell renders row, menu, Details and palette.
const logOf = o => /\.log$/.test(o.source.src || '');
const capWhy = (o, action, fallback) => { const c = o.caps?.[action]; return !c ? fallback : c.allowed || c.reason || fallback; };
function putObjects() {
  const C = window.shell.commands;
  ROWS.forEach(r => C.put(Object.assign(r, { type: r.kind === 'db' ? 'report' : 'status mail', label: r.what })));
  // Each scheduled job is its own object; Retry acts on the job, not on the report (commands.md, one declaration per command).
  // build: the job is operations.inventory's, as Management puts it, with the revision and capability Retry posts and reads.
  Object.values(JOBS).forEach(j => C.put({ id: 'job:' + j.name, type: 'job', label: j.name, job: j.name, service: j.service,
    failed: !!j.capabilities?.retry?.allowed, exit: j.last_exit, revision: j.revision, caps: j.capabilities || {} }));
}
// One read after a group of writes: each write waits for the reading that follows it.
let reading = null;
const reload = () => reading ||= load().finally(() => { reading = null; });
async function acknowledge(o) {
  try { await post(`/api/reports/${o.n}/acknowledge`, { revision: o.source.revision }); }
  catch (err) { if (err.stale) reload(); throw err; }
  await reload();
  // build: no Undo. sd-db has no verb that reopens an acknowledged report, so the toast says so instead of offering one.
  return `Acknowledged · #${o.n}. No Undo: sd-db has no verb that reopens a report`;
}
function registerCommands() {
  const C = window.shell.commands;
  const task = on => ({ id: `${on}.task`, on, label: 'Make task', key: 'k', risk: 'safe', journal: () => 'new', cli: o => `sd task add ${shq(o.label)}`, run: o => { window.shell.capture(o, '', `${on}.task`); return null; } }); // the filed task is the write
  // build: copy only. The dashboard shows no job log yet (Management has none either).
  const log = on => ({ id: `${on}.log`, on, label: 'Show log', key: 'l', risk: 'safe', executes: false, when: o => (o.kind === 'mail' || logOf(o)) || 'this report came from sd-db.sh, not a job log', cli: o => `${CRON} logs ${o.job}`, run: () => 'Copy it into a terminal: the dashboard shows no job log yet' });
  C.register(
    // build: Acknowledge posts /api/reports/<n>/acknowledge with the report's revision, the route v1's form posts. It is off,
    // with the library's reason, for a report an open followup holds or whose fields cannot be read.
    { id: 'report.ack', on: 'report', label: 'Acknowledge', key: 'a', risk: 'undo', journal: o => o.n, bulk: true, primary: o => o.open, executes: true,
      when: o => !o.open ? 'already acknowledged' : !o.source.fields_read ? `its fields cannot be read: sd reports acknowledge ${o.n} finishes it`
        : o.source.followups.length ? `an open followup holds it: sd note resolve ${o.source.followups[0]}` : true,
      cli: o => `sd reports acknowledge ${o.n}`, run: acknowledge },
    // build: the report opens on v1's item page; Tasks lists no reports.
    { id: 'report.show', on: 'report', label: 'Open item', key: 'o', risk: 'safe', journal: o => o.n, executes: false, cli: o => `sd task show ${o.n}`,
      run: o => { location.href = `/item/${o.n}`; return `#${o.n} opens on its item page`; } },
    log('report'), task('report'),
    // build: no status mail object is put (no reader), so these two never show; they keep the design's declarations.
    { id: 'mail.ack', on: 'status mail', label: 'Acknowledge', key: 'a', risk: 'undo', journal: () => null, bulk: true, when: () => 'the message store is not built; Gmail holds the only copy', cli: o => `sd message ack ${o.source.gid}` },
    { id: 'mail.open', on: 'status mail', label: 'Open in Gmail', key: 'o', risk: 'safe', executes: false, primary: o => o.act, cli: o => `open https://mail.google.com/mail/u/0/#all/${o.source.gid}`, run: () => 'Opens in Gmail, read-only' },
    log('status mail'), task('status mail'),
    // build: the declaration Management makes (sd:2118): it posts /api/jobs/<name>/retry with the job's revision, and
    // operations.inventory's capability is the off reason. Exit 127 is "command not found": Retry is off, not asked first.
    { id: 'jobs.retry', on: 'job', label: 'Retry', key: 't', risk: 'safe', bulk: true, primary: o => o.failed,
      when: o => o.exit === 127 ? 'exit 127: command not found; fix the path first' : capWhy(o, 'retry', o.failed || 'no failed run to retry'),
      cli: o => `sd jobs retry ${o.job}`, sends: o => `launchctl kickstart ${o.service}`,
      run: async o => { await post(`/api/jobs/${encodeURIComponent(o.job)}/retry`, { revision: o.revision }); await reload(); return `Retry started · ${o.job}`; } },
  );
}
document.addEventListener('shell:open', e => { if (ROWS.some(r => r.id === e.detail)) select(e.detail, true); });
document.addEventListener('shell:picked', e => { picks = new Set(e.detail); tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', picks.has(tr.dataset.id))); });

// ---------- Start (build: read /api/reports, then draw; again after each write) ----------
let started = false;
async function load() {
  if (!started) window.shell.state({ kind: 'loading', text: 'Reading reports, jobs and job logs. Rows appear when /api/reports answers.', source: '/api/reports' });
  let doc;
  try { doc = await getJSON('/api/reports'); }
  catch (err) {
    window.shell.state({ kind: 'error', text: `Reports were not read, so nothing below is current: ${err.message}. Reload retries it.`, source: '/api/reports' });
    return;
  }
  absorb(doc);
  pageState();
  putObjects();
  CAD_JOBS = cadJobs();
  lamps();
  fillFilters();
  const needs = ROWS.filter(r => r.act).length;
  byId('sub').textContent = `${needs} want action · ${ROWS.filter(r => r.kind === 'db' && r.open).length} open in the store · newest first`;
  // build: the clean date defaults to the reading's day and cannot pass it; clean_reports refuses a later cutoff.
  if (!started) { byId('ack-date').value = READ.slice(0, 10); }
  byId('ack-date').max = READ.slice(0, 10);
  if (!started) readURL();
  renderCad();
  if (!started) {
    started = true;
    renderRows();
    // A load whose filters show no row opens with Details cleared, never on a report the list does not show (PR #31 review 2).
    if (!tbody.querySelector('tr[data-id]')) { window.shell.reconcile({ rows: [], current: null, select: () => {}, clear: clearDetails }); return; }
    const q = new URLSearchParams(location.search).get('row');
    const first = ROWS.find(r => r.act) || ROWS[0];
    select(ROWS.some(r => r.id === q) ? q : first.id, false);
  } else {
    renderRows();
    if (sel && ROWS.some(r => r.id === sel)) show(sel);
  }
}
document.addEventListener('DOMContentLoaded', () => {
  registerCommands();
  load();
});
