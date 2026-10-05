# local-project-dashboard

This directory contains the workflow dashboard and the six legacy collector
commands. They are separate surfaces.

**The workflow dashboard** (`sd_dashboard/`) opens on the new design's Today, and keeps
the classic Today, Contributions, Writing,
Operations, Protection, Skills, Documents, Designs, and individual Item pages.
A `/backlog` address opens Tasks with the same filters (sd:2356); the classic
Backlog was deleted in sd:2622. Its production launcher uses the
command pack's provisioned Python interpreter and installed `sd_db` package;
it refuses an adjacent source import or a mismatched database schema.
`./dashboard.sh serve` starts it on loopback;
`./dashboard.sh test` runs its tests. Capture an ordinary task, edit its title,
details, priority and due date, change its status, and add or resolve notes.
The CLI and the page call the same `sd_db.workflow` functions. Saving uses a
revision checked inside the write transaction: an old tab gets a conflict
instead of overwriting a newer change. Capture keeps its draft and offers
Refresh related item; other item forms offer a reload link.

**Today** opens with **Now**: the fleet's loudest facts, ranked, loudest first —
scheduled jobs that failed or want attention, checkouts with unpushed commits, checkouts with uncommitted files, abandoned
worktrees, and open pull requests waiting on you. The ranking is the pack
dashboard's `dashboard/now.py`, carried across with its rank numbers, in
`sd_dashboard/now_screen.py`; the band a row wears (`broken`, `look`,
`queued`) is derived from the rank alone, never from the kind. The four
sources are the ones the Operations tabs show in full: Repos and Sessions
through the fleet child, Trackers' PRs through `sd_db.shadow` (`needs_you`
rows only; the days a stale row shows count from the sync's first sighting,
because the shadow table carries no `updated_at`), and Jobs through
`sd_db.operations.inventory`. A failed job ranks 1, after a dark collector and
before every other row; it names the exit code or signal, the time its log was
last written, and its retry line, `launchctl kickstart <service>`, the one Operations Jobs sends
(a hand-run of `cron-jobs.sh run` does not clear launchd's record).
An interrupted, unloaded or unknown job ranks 2, in the look band (sd:2014). An interrupted one carries the same retry line.
An unloaded one names the `cron-jobs.sh install` that loads it; an unknown one names the `launchctl print` that shows its record.
The rows arrive by `/api/now` after
the page is up, so a Today load never waits on the fleet; Refresh reads again.
A collector that goes dark — the child refused, past its budget, exiting
non-zero, a shadow read or a launchd read that fails — is a rank-0 row naming the collector
and the reason, never an empty list. Nothing is stored and nothing on Now
writes; there is no dismiss.

**The new design is the default** (sd:2163). It is the redesign from ui-design
`products/system/` (`design.md`, `designs/pages/`), built one page at a time.
`sd_dashboard/v2/` holds the ported shell, tokens and self-hosted IBM Plex
under `static/`, served at `/ui/`; `shell.js` marks each change from the
reference with `build:`. The assets take `/ui/` because the old screens own
`/static/`, which serves exactly `dashboard.css` and `dashboard.js`. Every
v2 script builds markup with the `html` tag in `static/markup.js`, which
escapes each value put in it, and puts it in the page with `put`, which takes
only what `html` made; that file holds the one HTML sink criterion 12's grep
allows in v2. Each page registers itself with one module in
`sd_dashboard/v2/pages/` (sd:2418): its section, routes, API routes and the
old screens it keeps. A page reads its document through the shell's one
reader, `static/read.js`, once its follow-up adopts it; the page's script says
so. What each page reads and what it runs is in
[`docs/pages/`](docs/pages/), one file per page.

Pages link each asset under its content digest, `/ui/<file>?v=<digest>` and
`/static/<file>?v=<digest>`, from a map the server computes when it starts
(sd:2141). That answer is `Cache-Control: private, max-age=31536000, immutable`;
a request without the matching `v` stays `no-store`, as pages and the API are.
So an edited asset reaches the browser after a restart, with no build step.
CSS, script and text go gzipped when the browser accepts it; fonts, pages and
API answers do not. `sd_dashboard/caching.py` says why.

The old screens stay until their section is ported. A page that takes an old
screen's path moves that screen under `/classic/`, as the old Today moved to
`/classic/today`, and its page module says where. Every other old screen keeps its path.
A `/v2/` address that names a page or an asset answers 301 with its new one (`/v2/today` to
`/today`, `/v2/static/<file>` to `/ui/<file>`); any other `/v2/` path is a 404.
One map, built from the page registry and served as `/ui/sections.js`, says
where each rail section goes: `SECTIONS` for ported pages, `CLASSIC` for the
old screen an unported section opens (tagged "classic" on the rail), and
`SCREENS` for old screens without a section, which the palette lists under
Classic screens. `./dashboard.sh pages` prints that map, one line per section
or screen; a section in none of the three, such as Notes or HOA, has no old
screen, and the rail says it is not built yet. The classic screens' own nav links
each ported section to its new page, read from the same registry (sd:2473); the
palette still opens the classic screen.

Task controls need no repository, planning document, branch, or GitHub issue.
Today and Backlog default to Task. The Type selector also offers Followup
item, Followup note, Comment, Question, Decision and Proposal. The word
`followup` names two things in the store — an item kind and a note kind —
and the form labels them apart: **Followup item** files an item of kind
`followup`, what `sd task add --kind followup` creates, through the same two
library calls that verb makes (`workflow.capture_task`, then
`writes.set_item_fields(kind="followup")`); it stands alone, or names the
related item it follows up, in which case the child carries
`fields.followup_of` and the parent gets a comment note naming the child.
**Followup note** and the other four attach a note to the Related item, which
they require. The picker identifies parents by stable ID, repository and
status, including completed and parked items. Adding a note does not reopen
or unpark its parent. The short inline CLI hint follows the selected type and
related item: `sd task add "Title"`, `sd task add --kind followup "Title"` or
`sd task note ID --kind followup --body "Text"`.
The CLI's note default is Comment, matching the item note form.
Work items keep their delivery rules and their repository's status ownership;
the generic status control cannot complete code delivery or override an active
assignment. Followup and personal items close and reopen through the task
statuses. The details form stays off a personal item's screen because
`workflow.edit_item` refuses its details, so that screen holds the status
control alone. A followup gets both forms since sd:816: `edit_item` has
accepted its title, priority, due date, repository and body since sd:809, and
the panel offers those five. Reclassifying a followup is not among them, so the
`Kind` select stays a task's.
Unbuilt screens and controls are not advertised as working.

Database-owned work can update an artifact link without changing its stable
item identity, or be cancelled with a recorded reason. Cancellation closes the
local work while keeping files and delivery evidence intact. Code delivery is
still a separate CLI operation that verifies its commit. An item's external
reference is shown with its snapshot and sync freshness; an old GitHub status
does not control local task completion.

**Contributions (classic)** at `/classic/contributions` shows upstream activity, unfiled local work, evidence, dependencies, and notification state.
Today previews the first five contributions in the same order.
Newly unblocked work comes first, then work awaiting you, work awaiting others, and merged contributions.
The shared library supplies this order to both the dashboard and `sd-status`.
Each acknowledgement binds exact event IDs and the displayed revision; stale submissions refuse.
Acknowledgement leaves notification delivery history and local task status separate.

**Protection** at `/protection` is one table, one row per registered repository:
what its default branch enforces, gap by gap, with the ids and sentences
`sd-status` prints for one repository — `enforce_admins`, `required_checks`,
`strict`, `required_not_produced`, `produced_not_required`, `reviews` — and
the two merge-settings flags beside them. Two baseline flags follow (sd:1741):
"Rulesets only" (`protection_source`) and "Required check" (`required_check`).
A clean "Required check" cell names the check: `ci`, or `sd/local-gate` for a `repo.ci = local` repository.
A repository outside the operator's owners carries neither, and both cells
read as not applicable. Nothing on the page calls GitHub: the
rows are a nightly observation written by the shadow collector (`sd shadow
sync`, through `sd_db.protection`), and the page reads them. Unprotected
repositories sort first, then unknown, then protected. Unknown is not
protected: a repository whose protection could not be read (a 403, a timeout,
an exhausted request budget, or one never observed) carries its own marker and
its reason, and shows no gap cells at all.
A checkout with no row of its own borrows the latest row of a sibling checkout of the same GitHub repository.
Its Observed cell then names the lender: `<time> · borrowed from <path>` (sd:1607).

**Writing** has its own view and stage controls. Its list filters by stage,
saved readiness decision, and active or parked pieces. An item's review panel
checks the current draft and reports exactly what prevents readiness; stage
changes call the same library as `sd writing` and the writing pack. A correction
needs a reason and invalidates the prior readiness decision. Dashboard stage
writes require database ownership and never update piece files. Publication
remains a separate reviewed operation; there is no generic publish button.
Park sets a piece aside without changing its stage or files; revive brings it
back to the active writing list.

The page is `/writing`, the v2 Writing page (sd:2125): a board by stage with
four gate lamps per piece, read from `GET /api/writing`
(`writing_screen.document`). Its Stage, Correct, Park and Revive post the item
routes above; Publish and Capture are copy only. The filtered list stays at
`/classic/writing`; [`docs/pages/writing.md`](docs/pages/writing.md) names
what the page does not read yet.

**Skills** is `/skills`, the v2 Skills page (sd:2123): the command pack's skill
catalog with each skill's use in the last three weeks, read from
`GET /api/skills` (`skills_screen.document`). Try, Review, Promote and Demote
post the routes the classic screen posts; Run, Schedule, Adopt and Scan are copy
only. The classic screen stays at `/classic/skills`; [`docs/pages/skills.md`](docs/pages/skills.md) has the detail.

**Operations** has eleven tabs (`operations_screen.AREAS`). Jobs is the default;
the selected tab travels in the URL, for example `/operations?area=services`.

| Tab | Scope and controls |
| --- | --- |
| **Jobs** | Installed scheduled jobs and assignments; retry supported failed jobs, request a running job stop, or cancel a queued assignment. |
| **Services** | User LaunchAgents and third-party `/Library/LaunchDaemons`; start, stop or restart eligible long-running user services. System daemons, scheduled/startup agents and protected dashboard/access services are read-only. |
| **Ports** | Configured service ports and locally observed TCP listeners, with visible processes, PIDs and listening addresses. Read-only; filter by port, service or process. |
| **Progress** | Age in status for all active items across repositories, excluding completed and parked items. A bar opens the corresponding active bucket in Tasks; task filters do not alter this chart. |
| **Usage** | The existing weekly numbers and their inputs, bill spend and reservations, missing-trailer count (walked inside the 10-second budget Health uses; past it the tile says "not read" and that the walk stopped rather than waited on) and monthly provider scorecard. These details have moved from Today. Below the cost tile, the month (`?month=YYYY-MM`, a GET form): per bill spent, estimated (`bound`), held (`reserved` and `sending`) and cap, a gauge and a burn line with the cap rule and the projection for a capped bill, a gauge per `meter` window on a `plan` bill's card, the by-bill-provider-role table and every `bound` row as a `Listing` (filter and pager), all from `sd_db.usage.read` (`reads.usage_month` under the registry's merged caps: a legacy row cap on a `start` bill prints as no cap, on the tile as on the card), the read `sd-db.sh usage` prints; `/api/usage?month=` serves its JSON as the verb's `--json` bytes (`usage_screen.py`). Read-only: the sweep is the verb's, so a dead owner's hold shows here until the next reservation or `sd-db.sh usage` binds it. |
| **Reports** | The newest 200 recorded reports — scheduled job output with its source and findings. Open one to follow up, assign work or acknowledge it. Preview the clean reports at a date and acknowledge them in one attributed batch; the emails keep going. |
| **Resources** | The five legacy vault and machine views — Toolbox, Briefs, Vault, Research and Queues — each rendered by running `sd_tile.py` as a child. Read-only observations; nothing here starts a job or edits a note. |
| **Trackers** | PRs and Issues as `sd shadow sync` last saw them, read from `sd_db.shadow` through `progress.tracker_items`, with each tracker's sync health. A row's reference is the last path segment of its URL — `LOG-23818` for a Jira ticket, `owner/repo#4321` on GitHub. Open rows only; read-only. |
| **Repos** | Every checkout under the repository root (`REPO_ROOT`, `~/repos` by default), one level of grouping deep, as git reports it at this read: branch, dirty files, ahead/behind its upstream, lag behind its origin's default branch, last commit. The default-branch lag is `git rev-list --count HEAD..origin/<default>` from local refs, counted on the default branch only; detached, another branch, no `origin/HEAD`, and a fetch missing or older than 24 hours each say why the count is unknown or a floor. The page never fetches or pulls: it shows the age of `.git/FETCH_HEAD`, and `git -C <path> pull --ff-only` only for a clean checkout on its default branch with no local commits. Read by `sd_dashboard/fleet.py` as a child under a 12-second budget; a checkout that does not answer, whose `.git` is gone, or whose git fails is a row that says why with no dirt count, and a status the child cut at its 64 KB ceiling is a row whose dirty count is a floor. Nothing is stored. |
| **Sessions** | Every worktree the fleet's checkouts have registered under `.git/worktrees`, abandoned ones first — a registration whose directory is gone still holds its branch; one whose files cannot be read is unknown, not abandoned — and every `sd` or `sd-*` command in the process table. The same child and budget as Repos; a root that does not exist is said in place of the worktree count, and a `ps` that did not answer is said beside the worktrees that did. |
| **Commands** | Execution history for palette commands and runner outcomes: the command, its item, when it started, its session and its exit code. A palette command keeps its recorded output; an unfinished response stays visible and can be reconciled. |

Every job or service control rechecks the observed state and revision. The last
request's acceptance is shown separately from the current state; acceptance is
not proof that a job finished or that an application is healthy.
Queued assignments can be cancelled without completing their item. A blocked
assignment can be cancelled once its item is done and no runner attempt holds
its lease. A running assignment no runner attempt owns names `sd runner cancel`,
which ends it. A restore
blocks starting jobs and starting or restarting services, while supported stop
controls remain available. Stopping a service unloads it for the current login;
its plist stays installed and may load again at the next login. Equivalent
`sd services` commands are in the page's collapsible CLI help.

Ports combines `machine-setup` service candidates with one local `lsof`
listener snapshot. It reads `../local-machine-setup/machine-setup.sh` when that
tool is present beside this folder; without it the inventory is reported
incomplete, never as no ports. Docker state and profile membership stay separate from
listener observations. Observed-only ports are included, multiple visible
owners and addresses are retained, and unavailable or incomplete inspection is
reported. A listening socket does not prove application health or identify the
intended service; inspection is limited to what this user can see and performs
no network scan. Within Operations, a collector runs only when its area is opened:
Ports runs the listener inspection, a Resources view runs its tile, and Repos and
Sessions run the fleet reader. Today's Now runs the fleet reader too, once per
`/api/now` read, for both areas, reads the shadow table, and reads the launchd
jobs' last-run state as the Jobs area does. None of them
exposes prompts, environment variables, output logs or arbitrary commands.

The main server binds **local loopback**. Remote access requires an explicit
private HTTPS Tailscale Serve origin and the node owner's exact operator login.
Every remote request revalidates the private route, its loopback target and
operator; missing identity, public Funnel or unavailable inspection is refused.
Local requests remain independent of Tailscale and reject forwarded identity
headers. Configuring a proxy alone does not grant workflow access.
An optional `ip_origin` adds HTTP access on this node's exact Tailscale IPv4 address and port 8768.
That listener authenticates the TCP peer through Tailscale WhoIs and rejects forwarded identity headers.
It preserves the HTTPS route and does not bind a LAN address.

For a local service, put `{"port": 8767}` in
`~/.config/sd/dashboard.json`, then review the installation plan:

```sh
./dashboard.sh preflight --config ~/.config/sd/dashboard.json
./dashboard.sh install --config ~/.config/sd/dashboard.json
# Both commands above are previews. Apply only the fingerprint just reviewed:
./dashboard.sh install --config ~/.config/sd/dashboard.json \
  --apply --expected-fingerprint REPLACE_WITH_REVIEWED_FINGERPRINT
./dashboard.sh health --config ~/.config/sd/dashboard.json
```

The local URL is `http://127.0.0.1:8767`. The installer backs up the existing
LaunchAgent and checks the new process, schema and installed library before
accepting the replacement. Local installation does not call Tailscale. See
[RUNTIME.md](RUNTIME.md) for configuration, rollback boundaries and the
private front door. Run in the foreground with `./dashboard.sh serve`, or run
the disposable source-development tests with `./dashboard.sh test`.

*No frontend build step, and there will not be one.* No `package.json`, no
`node_modules`, no bundler config under this folder: one hand-written
stylesheet and one hand-written script in `sd_dashboard/static/`, served from
disk. The `pyproject.toml` beside this file builds a wheel of the Python so
that consumers install the package instead of adding this folder to
`PYTHONPATH`; it brings no asset pipeline with it and touches none of the
above. Everything the dashboard did not write is escaped text and Markdown is
an allow-listed subset (`markup.py`); behind both, every response carries a
`Content-Security-Policy` with no inline script and `frame-ancestors 'none'`,
so no page anywhere may frame the dashboard.

Writes are JSON POSTs under `/api/items`, `/api/notes`, `/api/jobs`,
`/api/assignments` and `/api/services`; GET and HEAD never
change workflow rows. The browser supplies its signed, expiring HttpOnly
SameSite=Strict session cookie and matching CSRF token, plus an exact same
Origin. Invalid input, stale revisions, unknown actions and untrusted hosts
are refused. No command or arbitrary file path is accepted by these routes.
`GET /api/items/ID/capture-context` returns only the selected parent's identity,
status and current revision through the same authenticated boundary. Selection
loads that revision; submitting does not silently refresh it. A stale or deleted
parent requires refreshing the context while retaining the draft. An interrupted
capture response keeps submission disabled until the user checks the outcome
and explicitly enables a retry; a successful save stays disabled while navigating.

Sessions are bound to their origin and operator. HTTPS cookies use the Secure attribute.
The optional IP origin uses HTTP over the encrypted Tailscale connection, with its own origin-bound cookie.
Both remote modes verify the operator and current Tailscale configuration on each request.

The test suite exercises real HTTP capture, readback, completion and note
resolution against a disposable database, along with cross-origin/session
rejection and stale-browser conflict checks. Service tests use disposable
plists and a fake launchctl runner to check exact action sequences and refused
requests; Ports tests cover configured and observed listener evidence.
Browser/iPad usability is a
separate visual check; unit tests do not establish it.

The Backlog list component is `listing.py` — filter, selection and paging, in
that order, with the filter applied before the paging.

**The six legacy collectors** cover the vault, the launchd job table, research
builds and the service port map. Their CLI commands remain available, and the
workflow dashboard renders all six itself: Toolbox, Briefs, Vault, Research and
Queues as Resources views through `sd_tile.py`, and Ports in-process. The
manifest no longer declares them as plugin tabs (sd:719 step 3). Retaining the
collectors does not imply feature parity with the old dashboard.

## Documents

**Documents** lists generated HTML reports and serves them whole. Everything
else on this dashboard reads database rows and renders markup; a report does
neither, because a generated report is already a finished page and the useful
thing is to hand it over rather than strip it down.

The page is `/documents`, the v2 Documents page (sd:2114). It reads
`GET /api/documents` (`documents_screen.document`), which lists every file
`/documents/<key>/<file>` serves with its title, h1, stand line, derived kind
(research or report) and render state. Its facets, search and paged ledger come
from the design source. Pin, hide and tag are off until a document store exists
(docs/work/2026-09-28-documents-view-state); render and request are copy-only
lines the dashboard does not run. The classic listing stays at
`/classic/documents`, opened from the palette. Both read the same roots
through the same readers, so the rules below hold for each.

A domain repository that publishes reports here follows
`docs/html-reports.md` in this folder: a markdown twin and HTML page built
into its gitignored `docs/dashboard`, inline SVG, and a build stamp on each
page.

**The roots are found, not listed.** Any checkout under `REPO_ROOT` (default
`~/repos`, at the root or one group deep) that holds `docs/dashboard` is a
document root. Onboarding a repository is a directory in it and nothing else:
no line to add, and no list here to fall behind the fleet. `docs/dashboard` is
gitignored in the repositories that have one, because the pages are built
rather than tracked, so the enumeration reads the disk and never asks git.

The dashboard still knows nothing about any particular repository. A report
belongs to whatever generates it, and a fleet dashboard carrying the path to
one association's reports in its source would be wrong in a way that is
awkward to undo.

`documents.conf` remains, for the things a directory cannot say. It lives in
`<config>/project-dashboard/documents.conf` (`<config>` is
`$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`), outside the
checkout, because it names the operator's own repositories and paths; copy
`documents.conf.example` there to start one, and without it every found root is
listed under its directory name:

```
label|<key>|<label>              a found root, better named
skip|<key>                       a found root the dashboard should not list
skip|<key>|<file>                one file of a root, neither listed nor served
root|<key>|<label>|<directory>   a root that is somewhere else entirely
img|<key>|<host>                 an image host the root's *.app.html pages may load
```

**Reach for `label|` first.** The derived label is the directory name, and it
reads badly for an abbreviation: `mcp-research` becomes `Mcp Research`, and no
code recovers `MCP` from that. But that is a naming complaint and nothing
more, so the line says the name and stops. It renames a found root; it never
invents one, and a `label|` line for a key nothing found does nothing at all.

`root|` is for a root that genuinely lives elsewhere, outside any checkout's
`docs/dashboard`, and it wins over a found root of the same key, its own
label included. The vault is the case it exists for: it is not a checkout
under `REPO_ROOT`, so `root|vault|Vault|~/Documents/My Vault/docs/dashboard`
is the only way its pages are served. Do not reach for it to rename one. A `root|` line
that repeats the path enumeration already found puts the default location back
into this file, where it goes stale the moment the repository moves while
still looking authoritative. That is precisely the drift the enumeration was
written to remove, so the rename gets a line form that cannot carry a path.

**A key with two owners has no owner.** The key is the checkout's directory
name, and two groups may hold a checkout of the same name — `org-a/reports`
and `org-b/reports`. Neither is served, and the listing names the contest and
the directories in it. Picking one would serve one repository's pages at
another's address; picking the first found would decide it by glob order, so a
new checkout sorting earlier would repoint an address that already worked. A
`root|` line naming the one you mean settles it, and a `skip|` line does too.
A `label|` line does not: it says what to call the root, not which directory
is the one meant, and that is the question.

**It serves rather than embeds.** Pulling a report's body into a page would run
it through the markup filter, which exists to flatten what a foreign document
brought with it. The filter is right and the report would be ruined: no
stylesheet, no chart. So each report gets its own address, `/documents/<key>/
<name>.html`, and arrives unchanged.

**That needs its own policy.** The shared `Content-Security-Policy` is
`default-src 'self'; script-src 'self'`, and with no `style-src` the fallback
refuses the inline stylesheet a standalone report carries in its head. The page
would render unstyled. A served document replaces that one header with
`default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:`,
which is tighter where it counts: the dashboard permits same-origin script and
this permits none. `_send` takes the policy per response and assigns it every
time, because the handler instance is reused across a kept-alive connection and
a policy left behind would apply to the next page down the same socket.

**A `*.app.html` page may run its own script, in a sandbox (sd:1502).** An
interactive page, such as a map, needs script, and the policy above refuses it.
A file named `<name>.app.html` opts in, and only its name opts it in. It is
served with `sandbox allow-scripts allow-downloads` and without
`allow-same-origin`, so it runs in an opaque origin: the `SameSite=Strict`
session cookie is not sent, storage is unavailable, and `/api/` refuses any
`Origin` other than the dashboard's own, `Origin: null` included. Its script is
`https://cdnjs.cloudflare.com`, which its tags pin with `integrity`, plus its
own inline scripts by SHA-256, measured from the bytes served. A digest rather
than `'unsafe-inline'` keeps an `onerror=` attribute smuggled in through the
page's data from running. Its images are `data:`, `blob:`, the script host, and
the hosts its root's `img|<key>|<host>` lines name; it has no `connect-src`, no
form and no frame. Any other file keeps the no-script policy, whatever it
carries.

**A found root may not reach past its checkout.** A `docs/dashboard` that is a
symlink out of the repository is not a root, because enumeration moved the
decision from a person to a glob: serving a directory outside a checkout is a
thing somebody asks for with a `root|` line, and a symlink is not somebody
asking. `within()` cannot catch it — it resolves the link and trusts the
result, which is the right rule for a declared root and no rule for a found
one. A symlink *inside* the same checkout is fine, and a declared root outside
every checkout, like a vault, is unaffected.

**Three conditions, all of them, before a byte is read.** The key must name a
configured root exactly; the file name must match `[A-Za-z0-9][A-Za-z0-9._-]*\.html`,
which contains no separator, so traversal is unsayable rather than defended
against; and the resolved path's parent must still be the resolved root, which
is what a symlink cannot fake. Every failure is the same 404, because
distinguishing "no such root" from "outside the root" tells a prober which of
the two they reached.

`documents.within()` is that rule, and the listing and the server both call it.
They have to agree: a listing offering a link the server then refuses is worse
than one that omits the file, because the reader believes the first.

The route sits ahead of the database connection in `do_GET`, beside `/static/`,
so a report stays readable when the workflow database is down. The listings do
not: `/api/documents` and `/classic/documents` sit behind it like every other page. That is the right way
round, because the report is the thing somebody needs in front of them during
an outage.

A configured root that does not exist on this machine is reported by name
rather than hidden. `documents.conf` is shared across machines and a root will
often be absent on one of them; a section that silently disappears looks the
same as a report that was never generated. A *found* root cannot be absent, so
this applies to the `root|` lines only.

## Designs

**Designs** lists the HTML mockups in the ui-design checkout and serves them
for review, for example from an iPad over the dashboard's Tailscale address.
The checkout is `DESIGNS_ROOT`, by default `$REPO_ROOT/platypeeps/ui-design`.
The listing shows every `.html` page under `products/`, one section per product.

The page is `/designs`, the v2 Designs page (sd:2126). It reads
`GET /api/designs` (`designs.ledger`): each drawn page's kind, last commit and
screenshot state, and each product with a brief and no page. History, Retake
screenshots and Read brief are copy-only lines the dashboard does not run. The
classic listing stays at `/classic/designs`; [`docs/pages/designs.md`](docs/pages/designs.md) has the detail.

A mockup is served at its path in the tree, under `/designs/`. Its relative
links, such as `../../../foundation/tokens.css`, therefore resolve to files in
the same checkout. `designs.resolve()` is the one rule, and the listing and the
server both call it:

- each path segment starts with a letter or a digit, so `..`, `.git` and dotfiles are refused;
- the extension must be in `designs.TYPES`, which also sets the content type;
- the resolved file must stay inside the resolved checkout, so a symlink out is refused.

Every refusal is the same 404.

A mockup page runs under `designs.POLICY`, which starts with `sandbox allow-scripts`.
Its script runs, so the drawer and palette can be tried, but its origin is
opaque: it reads no dashboard cookie and calls no dashboard endpoint.
`connect-src` and `form-action` are closed too. Because of the opaque origin,
the page's own CSS and images load as cross-origin requests. Asset responses
therefore carry `Cross-Origin-Resource-Policy: cross-origin` through the
handler's `_overrides`; every other header stays the dashboard's.
A font also carries `Access-Control-Allow-Origin: *`, because a browser fetches
fonts in CORS mode. No other file gets it: the data scripts beside the pages
hold real notes and mail, and any site could then read them from this port.

The v2 Designs mockup loads `products/system/designs/pages/data/designs-data.js`.
The tab answers that one path live, with `designs.ledger_script()`, instead of the committed file.
`designs.ledger()` ports ui-design's `tools/collect-designs.mjs` and lists:

- per page: kind, title, bytes, last commit (sha, time, subject) and a dirty flag;
- per page: its screenshots, each one's commit time and PNG size, and which are stale;
- per product: its brief path, title and **Status:** line, including a product with no page;
- `caution`: pages uncommitted, without a screenshot, or with a stale one.

A v2 screenshot is stale when `shots/inputs.json` lacks it or records another inputs hash.
`designs.inputs_hash()` ports `tools/inputs.mjs` byte for byte; `test_designs.py` pins Node's output.
Every page and screenshot the ledger names passes `designs.resolve()`.

## Legacy collector reference

Until 2026-09-01 this directory ran an earlier HTML dashboard on
`127.0.0.1:8767` from login. The parts of that implementation about *serving a
page* — `dashboard.py`, the LaunchAgent, the CSS and icons, `tailscale serve`,
the ack store, the database write path — were deleted rather than ported. What
could not be ported is what stayed: these collectors read a vault and a machine
that only exist here, so they remain system-owned.

The port went to the framework dashboard in `sd-ai-command-pack` for the week
that followed, and **came back**: the workflow dashboard at the top of this file
serves `127.0.0.1:8767` today, from this directory, and `dashboard.sh` says so
in its own help. So "the dashboard moved to the pack" describes one week of
2026-09 and nothing since. The plugin contract described below belongs to that
framework dashboard, which is historical as the deleted HTML dashboard above is:
of the three surfaces named here, only the workflow dashboard is served.
`sd-plugin.json` stopped declaring `tile` and `tabs` on 2026-09-13, before the
pack deleted the loader that read them; only the queue actions remain.

## Usage

```sh
./dashboard.sh tile toolbox     # one tab as JSON on stdout
./dashboard.sh queue-open blog  # open one decision queue in Obsidian
```

The framework dashboard's plugin loader read the six tab names and the tile
command from `../sd-plugin.json` and ran one of these commands per tab per load.
The manifest now declares only the four queue actions.

## Files

| File | What it is |
| --- | --- |
| `sd-plugin.json` (repo root) | the manifest: `prefix` and one declared action per decision queue |
| `dashboard.sh` | collector verbs, workflow server, runtime installer and tests; `<config>/project-dashboard/.env` selects the legacy collector interpreter |
| `sd_tile.py` | one tab per invocation — the part that turns a collector's output into a table |
| `collectors.py` | the collectors themselves. A library with no entry point: everything that started something went with the server |

## Tabs

| Tab | What it shows | Where it comes from |
| --- | --- | --- |
| **Toolbox** | Cron jobs with launchd's own last-exit code **and the next time each one fires**, launch agents, docker containers, and `machine-setup` drift with the age of the measurement | `launchctl list`, `jobs/*.job`, `docker ps`, and the nightly drift job's log rather than a fresh `machine-setup.sh status`, which cost most of the five-second budget |
| **Briefs** | Everything the scheduled routines wrote, newest first, grouped by kind, each readable inline | `System/AI Generated/Briefs` |
| **Vault** | One card per `* Home` area with note and open-task counts, plus overdue/due-today and Inbox pressure | vault frontmatter |
| **Research** | Every checkout carrying a `research.conf.py`, its documents, and whether the rendered HTML is **fresh**, **stale** or **not built** | the conf itself, plus the built page's mtime in `docs/dashboard/` (or the older `build/`) |
| **Ports** | Each service's effective ports, and which of them clash with another candidate or are already held — read from `machine-setup.sh`'s own conflict lines, which the collector could not see until 6b-9 | `machine-setup.sh candidates service` |
| **Queues** | The four vault decision databases: how many are waiting, and the oldest undecided notes across all of them | the database folders under `System/Databases/` |

A cron job's last exit code says the last run was fine; it cannot say the job
has quietly stopped firing. The **next run** column is computed from each job's
own crontab expression, so a job that should have fired hours ago and did not
is visible without waiting for the watchdog.

The **Ports** tab exists because `.claude/rules/services.md` says the port list in prose goes
stale and to ask the machine instead. This asks the machine on every load.

## Queues reads; Obsidian writes

The old dashboard offered a status dropdown on every database row and wrote the
chosen value into the note's frontmatter. That went with the server, and it was
a decision rather than a casualty: a plugin tab was markup, the loader dropped
script, and the pack dashboard's declared actions took **no arguments from the
page** — an action was a fixed command, so "set *this* note to *that* value" had
no way to travel. Rather than widen the action contract for one tab, the tab
read and Obsidian kept the writing. The Resources views that show it now are
read-only too.

So `sd-plugin.json` declares one action per queue instead: `queue-open <q>`
builds an `obsidian://search` URL for that folder's undecided notes and opens
it. No dashboard renders those four `sys/queue-*` actions now. sd:719 step 3
removed them from the pack dashboard, which is no longer served, and no module
of the system dashboard reads the manifest. The Queues view under Operations,
Resources shows the counts; run `./dashboard.sh queue-open <q>` to open a queue.
The decision is made where it always could be made.

The rule that made the old dropdown careful still holds wherever a status is
written: `System/Schema.md` names a single writer for each machine-owned
transition — `tips-accept` alone may write `ready`, `pack.py tips attach`
refuses anything but `approved` — and nothing in this directory may become a
second writer for them.

## How it works

- **One invocation per view.** Each Resources view runs `sd_tile.py <name> <since>` as
  a child (`<since>` is the view's start on `CLOCK_MONOTONIC`, so the tile's
  deadline counts from it), under five seconds and 64 KB (`reports_screen.collect`, reading
  through `collectors.Budget`), as the pack's loader once ran
  `dashboard.sh tile <name>` per declared name. Not a style choice: run
  behind one command these collectors take 6.66 s together and would be killed
  on every load, permanently, while each of them fits the budget several times
  over.
- **Exit codes follow the retired plugin contract** — 1 when a collector
  failed, with the reason on stderr, 2 when the tab name is wrong. A Resources
  view shows that reason in place of the view. Nothing but the payload may
  reach stdout, so `cmd_tile` execs.
- **Markup is a table and nothing else.** A tile cannot send script: the
  Resources view keeps only headings, paragraphs, tables, lists and links, and
  drops every other tag. No attribute a tile sent survives: the filter writes
  the two it needs itself, `class="listing-table"` on a table and an escaped
  `href` plus `rel="noopener noreferrer"` on an http(s) link
  (`SafeFragment` in `sd_dashboard/reports_screen.py`). The tables still carry
  `data-sd-search` and `data-sd-sort`, which asked the pack's page for a filter
  box and click-to-sort; that filter drops them.
- `run()` gives every child its own process group and kills the group on
  timeout. `subprocess.run()` kills the direct child only, so a grandchild
  holding the inherited stdout pipe — `machine-setup.sh`'s nested `python3`,
  stopped on a TCC prompt — hangs the call long past its timeout.

## macOS TCC, and the two interpreters that read the vault

The vault is in `~/Documents`, so the `python3` that reads it needs the same
one-time Documents grant `local-task-actions` needed. Three things follow, the
first two learned the hard way on 2026-08-28 when the page sat on
*collecting…* for 27 minutes:

- **Homebrew python, not Xcode's.** Not by preference — by elimination. TCC
  attributes a vault read to the executing binary (verified with a launchd test
  harness: `ls` with no grant fails even under an FDA-granted shell; Homebrew
  python with its grant passes under plain `sh`), and macOS simply ignores user
  FDA grants for Xcode's `/usr/bin/python3`: granted as `Python.app`, as the
  inner `MacOS/Python` binary, toggled off/on, and after
  `tccutil reset SystemPolicyDocumentsFolder com.apple.python3` — every variant
  still gets `Operation not permitted` (2026-08-29). The price of Homebrew is
  that `brew upgrade python` moves the Cellar path and drops the grant.
- **Two interpreters run `sd_tile.py`, so two binaries can need the grant.** A
  Resources view runs the tile as a child of the server, under the server's own
  `sys.executable` — `SD_DASHBOARD_PYTHON`, the command pack's
  `.venv/bin/python`, which is Homebrew python@3.13 and execs that framework's
  `Python.app/Contents/MacOS/Python`. `dashboard.sh tile` and
  `dashboard.sh queue-open` run it under `DASHBOARD_PYTHON` from `<config>/project-dashboard/.env`, today
  `/opt/homebrew/bin/python3`, which is Homebrew python@3.14 and execs a
  different `Python.app`. `DASHBOARD_PYTHON` therefore does not govern what the
  dashboard's own pages read. The Resources path is the one the dashboard
  exercises, and it works: on 2026-09-14 its Briefs view rendered 96 rows from
  the launchd server with no refusal, so the python@3.13 binary holds the
  grant. Whether the python@3.14 binary holds it under launchd has not been
  measured — a shell has Documents access of its own, so running the tile by
  hand cannot answer it. `dashboard.sh grants` is the measurement: it probes
  both interpreter paths for the vault read and prints one line each, naming
  the path that lacks the grant. It also says when its own answers are not
  the binaries' — from a terminal `/bin/ls`, holding no grant, lists the
  vault too, and the verb exits 3 rather than reporting a pass, and 3 again
  when the control gave no conclusive result: it never started, the call on it
  failed, or it came back nonzero saying something that is not an access
  refusal. Run it from launchd, where a child's access is its own, for a
  conclusive exit 0 when both paths hold the grant. One other run reaches 0
  (sd:845): a control still waiting when its timeout ran out is read as one
  kept out, which is what an ungranted read looks like where nobody can answer
  the prompt — it does not fail, it waits. Coming back unsettled and running
  out the clock are not one answer: the first is the 3 above, and only the
  second carries the lines it printed. So a `/bin/ls` wedged for some reason
  of its own passes the interpreters' answers through, and that needs a wedge
  that reaches `/bin/ls` and not the vault — anything wider silences the
  probes too, and a silent probe is a refusal and exit 1 — and the line
  prints the wait it passed on, so the exit is never the only thing said.
- **The vault read is probed in a child first** (`vault_blocked()`, 15 s).
  Nothing spawned by an agent can click a TCC prompt, so an ungranted read does
  not fail — it waits forever. The probe turns that into an error on the tab and
  caps the cost at 15 s instead of 1605.

## Environment

| Variable | Default |
| --- | --- |
| `SYSTEM_TOOLS_CONFIG` | `${XDG_CONFIG_HOME:-~/.config}/system` — config root: `project-dashboard/.env`, `project-dashboard/documents.conf`, and the cron job files Toolbox reads from `cron-jobs/jobs` |
| `CRON_JOBS_EXTRA_DIRS` | none — further job directories Toolbox reads, as `cron-jobs.sh` does |
| `DASHBOARD_PYTHON` | PATH `python3` (set to Homebrew in `<config>/project-dashboard/.env`); the interpreter for `tile` and `queue-open`, and one of the two paths `grants` probes |
| `SD_DASHBOARD_PYTHON` | command pack `.venv/bin/python`; runs `serve`, `preflight`, `install`, `health` and `grants`, and so the tiles a Resources view renders |
| `VAULT` | `~/Documents/Vault` — read by `collectors.py` itself; set it in `<config>/project-dashboard/.env` |
| `REPO_ROOT` | `~/repos` — likewise |
| `SYSTEM_TOOLS_LABEL_PREFIX` | `local.system-tools` — launchd label prefix; the dashboard's LaunchAgent is `<prefix>.sd-dashboard`, and Toolbox reads cron jobs as `<prefix>.cron.<job>` |

`dashboard.sh` exports whichever of `VAULT`, `REPO_ROOT`,
`SYSTEM_TOOLS_LABEL_PREFIX` and `SYSTEM_TOOLS_CONFIG` are set after sourcing
`<config>/project-dashboard/.env`, and `install` copies them (and
`CRON_JOBS_EXTRA_DIRS`) into the LaunchAgent, so the server reads the same values. The
Vault tile lists the vault's top-level `<Area> Home` folders, found on disk.

The earlier server's knobs went with it: the port, the refresh interval, the
tailnet bind, the allowed hosts, the ack file and the Jira credentials were all
about serving that page. Current workflow runtime settings are documented
above and in [RUNTIME.md](RUNTIME.md). `OBSIDIAN_VAULT` and
`DASHBOARD_REPO_ROOT` are gone too, and were never read on this path — the
tile has always taken `VAULT` and `REPO_ROOT` straight from the environment.

## What is left behind on disk

`logs/` and `state/` hold no tracked file at all now -- their `.gitkeep`s went
with the writer that needed the directories, and what is in them is ignored by
`**/logs/*` and `**/state/*` in the repository root's `.gitignore`, as it
always was. `state/acks.json` holds what the old **Needs you** tab had been
acknowledged for; nothing reads it any more, and the new dashboard has no ack
store yet, so it is left in place rather than deleted. The same goes for the
old server logs.
